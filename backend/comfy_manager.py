"""Version-aware ComfyUI Manager client for node, model, and queue operations."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx

logger = logging.getLogger(__name__)


class ManagerError(Exception):
    """Base exception for ComfyUI Manager errors."""


class ManagerNotInstalledError(ManagerError):
    """Raised when ComfyUI Manager is not available."""


class ManagerConnectionError(ManagerError):
    """Raised when ComfyUI is unreachable."""


class ManagerAPIError(ManagerError):
    """Raised when Manager API returns an error."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class NodePackInfo:
    id: str
    name: str
    description: str
    author: str
    repository: str
    installed: str
    updatable: bool
    stars: int
    last_update: str
    category: str
    files: List[str]
    matched_nodes: Optional[List[str]] = None


@dataclass
class ManagerVersion:
    version: str
    installed: bool
    supports_v4: bool = False


@dataclass
class NodeMapping:
    node_type: str
    node_pack_id: str
    node_pack_name: str


@dataclass
class ExternalModelInfo:
    name: str
    filename: str
    type: str
    base: str
    description: str
    reference: str
    save_path: str
    size: str
    url: str
    installed: bool


class ManagerCache:
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Any] = {}
        self._cache_times: Dict[str, float] = {}
        self._ttl = ttl_seconds

    async def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None
        if time.time() - self._cache_times[key] > self._ttl:
            self._cache.pop(key, None)
            self._cache_times.pop(key, None)
            return None
        return self._cache[key]

    async def set(self, key: str, data: Any) -> None:
        self._cache[key] = data
        self._cache_times[key] = time.time()

    async def invalidate(self, key: Optional[str] = None) -> None:
        if key:
            self._cache.pop(key, None)
            self._cache_times.pop(key, None)
            return
        self._cache.clear()
        self._cache_times.clear()


class ComfyManagerClient:
    """HTTP client for the installed ComfyUI Manager API.

    Stable Manager releases expose unversioned, action-specific queue routes.
    The experimental Manager v4 branch exposes a versioned generic task route.
    ComfyUI's ``/features`` response cannot distinguish those protocols, so the
    client probes concrete, read-only Manager routes and caches the result.
    """

    def __init__(self, server_url: str = "http://127.0.0.1:8188", timeout: int = 10):
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self.cache = ManagerCache(ttl_seconds=300)
        self._manager_version: Optional[ManagerVersion] = None
        self._queue_protocol: Optional[
            Literal["unversioned", "v2", "v2_legacy"]
        ] = None
        self._compatible_routes: Dict[str, str] = {}
        self._compatible_methods: Dict[str, str] = {}

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.server_url}{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                response = await client.request(method, url, params=params, json=json_data)
        except httpx.TimeoutException as exc:
            raise ManagerConnectionError(f"Timeout accessing Manager API: {endpoint}") from exc
        except httpx.RequestError as exc:
            raise ManagerConnectionError(f"Failed to connect to Manager API: {exc}") from exc

        if not 200 <= response.status_code < 300:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise ManagerAPIError(
                f"{method} {endpoint} failed with {response.status_code}: {detail}",
                status_code=response.status_code,
            )

        if not response.content:
            return {"success": True, "status": response.status_code}
        try:
            return response.json()
        except Exception:
            return {"success": True, "status": response.status_code, "data": response.text}

    async def _request_with_fallback(
        self,
        method: str,
        endpoint: str,
        fallback_endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        preferred = self._compatible_routes.get(endpoint, endpoint)
        alternate = fallback_endpoint if preferred == endpoint else endpoint
        try:
            result = await self._request(method, preferred, params=params)
        except ManagerAPIError as exc:
            if exc.status_code != 404:
                raise
            result = await self._request(method, alternate, params=params)
            preferred = alternate
        self._compatible_routes[endpoint] = preferred
        return result

    async def _request_with_method_fallback(
        self,
        endpoint: str,
        *,
        preferred_method: str = "POST",
        fallback_method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Use the current Manager method, falling back for legacy releases.

        Current stable Manager uses POST for no-body mutations such as queue
        start/reset. Manager 3.39.x used GET. Retry only on 405, which proves the
        first request was rejected before the operation ran, and remember the
        working method afterwards.
        """
        method = self._compatible_methods.get(endpoint, preferred_method)
        alternate = fallback_method if method == preferred_method else preferred_method
        try:
            result = await self._request(
                method,
                endpoint,
                params=params,
                json_data={} if method == "POST" else None,
            )
        except ManagerAPIError as exc:
            if exc.status_code != 405:
                raise
            result = await self._request(
                alternate,
                endpoint,
                params=params,
                json_data={} if alternate == "POST" else None,
            )
            method = alternate
        self._compatible_methods[endpoint] = method
        return result

    async def check_installed(self) -> ManagerVersion:
        """Detect Manager and its concrete queue protocol with read-only probes."""
        if self._manager_version is not None:
            return self._manager_version

        version = "unknown"
        version_route_available = False
        try:
            version_response = await self._request("GET", "/manager/version")
            version_route_available = True
            if isinstance(version_response, dict):
                raw_version = version_response.get("data") or version_response.get("version")
            else:
                raw_version = version_response
            if raw_version is not None and str(raw_version).strip():
                version = str(raw_version).strip()
        except ManagerConnectionError:
            raise
        except ManagerAPIError as exc:
            if exc.status_code not in {404, 405}:
                raise

        # Stable Manager, including current releases, uses these unversioned
        # routes. Prefer it so a ComfyUI core feature flag can never make us
        # accidentally send the experimental v2 task envelope.
        probes: tuple[tuple[Literal["unversioned", "v2"], str], ...] = (
            ("unversioned", "/manager/queue/status"),
            ("v2", "/v2/manager/queue/status"),
        )
        for protocol, endpoint in probes:
            try:
                await self._request("GET", endpoint)
            except ManagerConnectionError:
                raise
            except ManagerAPIError as exc:
                if exc.status_code in {404, 405}:
                    continue
                raise
            detected_protocol: Literal["unversioned", "v2", "v2_legacy"] = protocol
            if protocol == "v2":
                try:
                    legacy_ui = await self._request(
                        "GET",
                        "/v2/manager/is_legacy_manager_ui",
                    )
                except ManagerAPIError as exc:
                    if exc.status_code not in {404, 405}:
                        raise
                else:
                    if isinstance(legacy_ui, dict) and legacy_ui.get(
                        "is_legacy_manager_ui"
                    ) is True:
                        detected_protocol = "v2_legacy"

                if version == "unknown":
                    try:
                        v2_version = await self._request(
                            "GET",
                            "/v2/manager/version",
                        )
                    except ManagerAPIError as exc:
                        if exc.status_code not in {404, 405}:
                            raise
                    else:
                        if isinstance(v2_version, dict):
                            raw_version = v2_version.get("data") or v2_version.get(
                                "version"
                            )
                        else:
                            raw_version = v2_version
                        if raw_version is not None and str(raw_version).strip():
                            version = str(raw_version).strip()

            self._queue_protocol = detected_protocol
            self._manager_version = ManagerVersion(
                version=version,
                installed=True,
                supports_v4=detected_protocol in {"v2", "v2_legacy"},
            )
            return self._manager_version

        self._manager_version = ManagerVersion(
            version=version if version_route_available else "",
            installed=version_route_available,
            supports_v4=False,
        )
        return self._manager_version

    async def _ensure_installed(self) -> ManagerVersion:
        version_info = await self.check_installed()
        if not version_info.installed:
            raise ManagerNotInstalledError("ComfyUI Manager is not available on this ComfyUI server")
        return version_info

    async def _ensure_v4(self) -> ManagerVersion:
        version_info = await self._ensure_installed()
        if not version_info.supports_v4 or self._queue_protocol not in {
            "v2",
            "v2_legacy",
        }:
            raise ManagerNotInstalledError(
                "This operation requires the experimental ComfyUI Manager v2 API"
            )
        return version_info

    async def _ensure_queue_protocol(
        self,
    ) -> Literal["unversioned", "v2", "v2_legacy"]:
        await self._ensure_installed()
        if self._queue_protocol is None:
            raise ManagerAPIError(
                "ComfyUI Manager is installed, but no supported queue API was found"
            )
        return self._queue_protocol

    async def status(self) -> Dict[str, Any]:
        version = await self.check_installed()
        queue = None
        if version.installed:
            try:
                queue = await self.queue_status()
            except ManagerError as exc:
                queue = {"success": False, "error": str(exc)}
        return {
            "installed": version.installed,
            "supports_v4": version.supports_v4,
            "version": version.version,
            "api_protocol": self._queue_protocol,
            "queue": queue,
        }

    async def list_installed_packs(self, mode: Literal["default", "imported"] = "default") -> Dict[str, Any]:
        await self._ensure_installed()
        if self._queue_protocol == "unversioned":
            self._compatible_routes.setdefault(
                "/v2/customnode/installed",
                "/customnode/installed",
            )
        return await self._request_with_fallback(
            "GET",
            "/v2/customnode/installed",
            "/customnode/installed",
            params={"mode": mode},
        )

    async def queue_status(self, client_id: Optional[str] = None) -> Dict[str, Any]:
        protocol = await self._ensure_queue_protocol()
        if protocol in {"v2", "v2_legacy"}:
            params = {"client_id": client_id} if client_id else None
            return await self._request("GET", "/v2/manager/queue/status", params=params)
        return await self._request("GET", "/manager/queue/status")

    async def queue_start(self) -> Dict[str, Any]:
        protocol = await self._ensure_queue_protocol()
        if protocol in {"v2", "v2_legacy"}:
            return await self._request("GET", "/v2/manager/queue/start")
        return await self._request_with_method_fallback("/manager/queue/start")

    async def queue_reset(self) -> Dict[str, Any]:
        protocol = await self._ensure_queue_protocol()
        if protocol in {"v2", "v2_legacy"}:
            return await self._request("GET", "/v2/manager/queue/reset")
        return await self._request_with_method_fallback("/manager/queue/reset")

    async def queue_history_list(self) -> Dict[str, Any]:
        await self._ensure_v4()
        return await self._request("GET", "/v2/manager/queue/history_list")

    async def list_snapshots(self) -> Dict[str, Any]:
        await self._ensure_v4()
        return await self._request("GET", "/v2/snapshot/getlist")

    async def get_node_mappings(
        self,
        mode: Literal["local", "remote", "cache", "nickname"] = "local",
    ) -> Dict[str, NodeMapping]:
        await self._ensure_installed()
        if self._queue_protocol == "unversioned":
            self._compatible_routes.setdefault(
                "/v2/customnode/getmappings",
                "/customnode/getmappings",
            )
        data = await self._request_with_fallback(
            "GET",
            "/v2/customnode/getmappings",
            "/customnode/getmappings",
            params={"mode": mode},
        )

        mappings: Dict[str, NodeMapping] = {}
        for pack_id, pack_data in (data or {}).items():
            if isinstance(pack_data, list) and pack_data:
                node_list = pack_data[0]
                metadata = pack_data[1] if len(pack_data) > 1 and isinstance(pack_data[1], dict) else {}
                pack_name = metadata.get("title_aux") or metadata.get("title") or pack_id
                if isinstance(node_list, list):
                    for node_type in node_list:
                        mappings[str(node_type)] = NodeMapping(
                            node_type=str(node_type),
                            node_pack_id=str(pack_id),
                            node_pack_name=str(pack_name),
                        )
        return mappings

    async def search_node_packs(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        node_filter: Optional[str] = None,
        installed_only: bool = False,
        updates_available: bool = False,
        mode: Literal["local", "remote", "cache"] = "cache",
        max_results: int = 20,
    ) -> List[NodePackInfo]:
        await self._ensure_installed()

        installed = await self.list_installed_packs()
        packs: Dict[str, NodePackInfo] = {}
        for name, info in (installed or {}).items():
            cnr_id = info.get("cnr_id") or ""
            aux_id = info.get("aux_id") or ""
            pack_id = cnr_id or aux_id or name
            packs[pack_id] = NodePackInfo(
                id=pack_id,
                name=name,
                description="",
                author=(aux_id.split("/", 1)[0] if "/" in aux_id else ""),
                repository=(f"https://github.com/{aux_id}" if "/" in aux_id else ""),
                installed="True" if info.get("enabled", True) else "Disabled",
                updatable=False,
                stars=0,
                last_update="",
                category="installed",
                files=[],
            )

        if node_filter:
            try:
                pattern = re.compile(node_filter, re.IGNORECASE)
                mappings = await self.get_node_mappings(mode="local")
                matched_by_pack: Dict[str, List[str]] = {}
                for node_type, mapping in mappings.items():
                    if pattern.search(node_type):
                        matched_by_pack.setdefault(mapping.node_pack_id, []).append(node_type)
                for pack_id, matched_nodes in matched_by_pack.items():
                    packs.setdefault(
                        pack_id,
                        NodePackInfo(pack_id, pack_id, "", "", "", "Unknown", False, 0, "", "", []),
                    ).matched_nodes = matched_nodes
            except re.error as exc:
                raise ManagerAPIError(f"Invalid node_filter regex: {exc}") from exc

        results = list(packs.values())
        if query:
            query_lower = query.lower()
            results = [
                pack for pack in results
                if query_lower in pack.id.lower()
                or query_lower in pack.name.lower()
                or query_lower in pack.description.lower()
                or query_lower in pack.author.lower()
                or query_lower in pack.repository.lower()
            ]
        if category:
            results = [pack for pack in results if pack.category.lower() == category.lower()]
        if installed_only:
            results = [pack for pack in results if pack.installed != "False"]
        if updates_available:
            results = [pack for pack in results if pack.updatable]
        if node_filter:
            results = [pack for pack in results if pack.matched_nodes]
        return results[:max_results]

    async def search_external_models(
        self,
        query: Optional[str] = None,
        base_filter: Optional[str] = None,
        type_filter: Optional[str] = None,
        name_filter: Optional[str] = None,
        description_filter: Optional[str] = None,
        reference_filter: Optional[str] = None,
        uninstalled_only: bool = True,
        installed_only: bool = False,
        max_results: int = 10,
        mode: Literal["cache", "remote"] = "cache",
    ) -> List[ExternalModelInfo]:
        await self._ensure_v4()
        try:
            data = await self._request("GET", "/v2/externalmodel/getlist", params={"mode": mode})
        except ManagerAPIError as exc:
            if "404" not in str(exc):
                raise
            data = self._load_local_external_model_db()
        raw_models = data.get("models", []) if isinstance(data, dict) else []
        models = [
            ExternalModelInfo(
                name=item.get("name", ""),
                filename=item.get("filename", ""),
                type=item.get("type", ""),
                base=item.get("base", ""),
                description=item.get("description", ""),
                reference=item.get("reference", ""),
                save_path=item.get("save_path", ""),
                size=item.get("size", ""),
                url=item.get("url", ""),
                installed=item.get("installed", "False") == "True",
            )
            for item in raw_models
        ]

        def matches_regex(value: Optional[str], candidate: str) -> bool:
            if not value:
                return True
            try:
                return bool(re.search(value, candidate or "", re.IGNORECASE))
            except re.error:
                return True

        results: List[ExternalModelInfo] = []
        for model in models:
            if uninstalled_only and model.installed:
                continue
            if installed_only and not model.installed:
                continue
            if query and not any(
                matches_regex(query, value)
                for value in (model.name, model.description, model.filename)
            ):
                continue
            if not matches_regex(base_filter, model.base):
                continue
            if not matches_regex(type_filter, model.type):
                continue
            if not matches_regex(name_filter, model.name):
                continue
            if not matches_regex(description_filter, model.description):
                continue
            if not matches_regex(reference_filter, model.reference):
                continue
            results.append(model)
            if len(results) >= max_results:
                break
        return results

    def _load_local_external_model_db(self) -> Dict[str, Any]:
        """Load Manager's packaged model database when v4 omits the HTTP route."""
        try:
            import comfyui_manager

            model_db_path = Path(comfyui_manager.__file__).resolve().parent / "model-list.json"
            with model_db_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise ManagerAPIError(f"Invalid Manager model database: {model_db_path}")
            return data
        except ManagerAPIError:
            raise
        except Exception as exc:
            raise ManagerAPIError(
                "Manager v4 external model HTTP route is unavailable and the local "
                f"model database could not be loaded: {exc}"
            ) from exc

    async def check_updates(self, mode: Literal["local", "remote", "cache"] = "remote") -> Dict[str, Any]:
        await self._ensure_v4()
        installed = await self.list_installed_packs()
        return {
            "updates_available": False,
            "message": "Manager v4 removed the legacy fetch_updates API; use confirmed queue actions for updates.",
            "installed_count": len(installed or {}),
            "mode": mode,
        }

    @staticmethod
    def _normalize_install_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Build the install shape shared by stable Manager and Manager v4."""
        body = dict(payload)
        package_id = str(body.get("id") or "").strip()
        files = body.get("files")

        if package_id:
            body["id"] = package_id
            version = str(body.get("version") or "latest").strip()
            selected_version = str(body.get("selected_version") or version).strip()
            if version == "unknown" or selected_version == "unknown":
                if not isinstance(files, list) or not files:
                    raise ManagerAPIError(
                        "Unknown-version installs require a non-empty files list"
                    )
                version = "unknown"
            body["version"] = version
            body["selected_version"] = selected_version
        elif isinstance(files, list) and files:
            body["version"] = "unknown"
            body.pop("selected_version", None)
        else:
            raise ManagerAPIError(
                "Install payload must include a Registry package id or a non-empty files list"
            )

        if body.get("selected_version") == "nightly" and not body.get("repository"):
            raise ManagerAPIError("Nightly installs require the package repository URL")
        body.setdefault("channel", "default")
        body.setdefault("mode", "remote")
        body.setdefault("skip_post_install", False)
        return body

    async def _queue_action_specific(
        self,
        kind: str,
        payload: Dict[str, Any],
        *,
        ui_id: str,
        route_prefix: str,
        method_fallback: bool,
    ) -> Any:
        """Queue through an unversioned or v2 legacy action-specific API."""
        if kind == "update-all":
            params = {"mode": str(payload.get("mode") or "remote")}
            endpoint = f"{route_prefix}/update_all"
            if method_fallback:
                return await self._request_with_method_fallback(
                    endpoint,
                    params=params,
                )
            return await self._request(
                "GET",
                endpoint,
                params=params,
            )
        if kind == "update-comfyui":
            endpoint = f"{route_prefix}/update_comfyui"
            if method_fallback:
                return await self._request_with_method_fallback(endpoint)
            return await self._request(
                "GET",
                endpoint,
            )
        if kind == "install-model":
            body = dict(payload)
            body["ui_id"] = ui_id
            return await self._request(
                "POST",
                f"{route_prefix}/install_model",
                json_data=body,
            )

        endpoint_kind = kind
        if kind in {"install", "enable"}:
            body = self._normalize_install_payload(payload)
            if kind == "enable":
                endpoint_kind = "install"
                body["skip_post_install"] = True
        else:
            body = dict(payload)
        body["ui_id"] = ui_id
        return await self._request(
            "POST",
            f"{route_prefix}/{endpoint_kind}",
            json_data=body,
        )

    async def _queue_v2_action(
        self,
        kind: str,
        payload: Dict[str, Any],
        *,
        client_id: str,
        ui_id: str,
    ) -> Any:
        """Queue an action through the experimental Manager v2 API."""
        if kind == "update-all":
            params = {"client_id": client_id, "ui_id": ui_id}
            if payload.get("mode"):
                params["mode"] = payload["mode"]
            return await self._request(
                "GET",
                "/v2/manager/queue/update_all",
                params=params,
            )
        if kind == "update-comfyui":
            params = {"client_id": client_id, "ui_id": ui_id}
            if "stable" in payload:
                params["stable"] = payload["stable"]
            return await self._request(
                "GET",
                "/v2/manager/queue/update_comfyui",
                params=params,
            )
        if kind == "install-model":
            body = dict(payload)
            body.update({"client_id": client_id, "ui_id": ui_id})
            return await self._request(
                "POST",
                "/v2/manager/queue/install_model",
                json_data=body,
            )

        params = self._normalize_install_payload(payload) if kind == "install" else dict(payload)
        return await self._request(
            "POST",
            "/v2/manager/queue/task",
            json_data={
                "ui_id": ui_id,
                "client_id": client_id,
                "kind": kind,
                "params": params,
            },
        )

    async def queue_action(
        self,
        kind: Literal[
            "install",
            "update",
            "fix",
            "uninstall",
            "disable",
            "enable",
            "install-model",
            "update-comfyui",
            "update-all",
        ],
        payload: Dict[str, Any],
        *,
        client_id: str = "ren",
        ui_id: Optional[str] = None,
        start_queue: bool = True,
    ) -> Dict[str, Any]:
        protocol = await self._ensure_queue_protocol()
        ui_id = ui_id or f"fl_mcp_{kind.replace('-', '_')}_{uuid.uuid4().hex[:10]}"

        if protocol == "v2":
            result = await self._queue_v2_action(
                kind,
                payload,
                client_id=client_id,
                ui_id=ui_id,
            )
        else:
            result = await self._queue_action_specific(
                kind,
                payload,
                ui_id=ui_id,
                route_prefix=(
                    "/v2/manager/queue"
                    if protocol == "v2_legacy"
                    else "/manager/queue"
                ),
                method_fallback=protocol == "unversioned",
            )

        start_result = None
        start_error = None
        if start_queue:
            try:
                start_result = await self.queue_start()
            except ManagerError as exc:
                # The action has already been accepted. Preserve that fact so a
                # caller can retry only queue_start instead of enqueueing the
                # same install or removal a second time.
                start_error = str(exc)
                start_result = {"success": False, "error": start_error}
        await self.cache.invalidate()
        return {
            "success": start_error is None,
            "queued": True,
            "queue_started": start_error is None if start_queue else False,
            "kind": kind,
            "ui_id": ui_id,
            "client_id": client_id,
            "manager_protocol": protocol,
            "result": result,
            "queue_start": start_result,
            "queue_start_error": start_error,
            "dependency_installation": (
                "managed_by_comfyui_manager_in_comfyui_python"
                if kind == "install"
                else None
            ),
            "installation_complete": False if kind == "install" else None,
            "verification_state": (
                "pending_queue_completion_and_comfyui_restart"
                if kind == "install"
                else None
            ),
            "verification_required": kind == "install",
            "requires_restart": kind in {"install", "update", "fix", "uninstall", "disable", "enable", "update-comfyui", "update-all"},
        }


_comfy_manager_client: Optional[ComfyManagerClient] = None


def get_comfy_manager_client(
    server_url: str = "http://127.0.0.1:8188",
    timeout: int = 10,
) -> ComfyManagerClient:
    global _comfy_manager_client
    if _comfy_manager_client is None:
        _comfy_manager_client = ComfyManagerClient(server_url, timeout)
    return _comfy_manager_client
