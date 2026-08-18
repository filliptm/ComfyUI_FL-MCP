"""ComfyUI node library discovery via HTTP API.

Provides intelligent search and discovery of installed ComfyUI node types
through the /object_info API endpoint.
"""

import asyncio
import copy
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

logger = logging.getLogger(__name__)


class NodeCatalogPersistence(Protocol):
    """Narrow persistence boundary owned by the node-library client."""

    def reconcile(
        self,
        catalog: dict[str, Any],
        *,
        source: str,
        catalog_hash: str | None = None,
        observed_catalog_hash: str | None = None,
        node_schema_hashes: dict[str, str] | None = None,
    ) -> Any: ...

    def record_refresh_failure(self, error: str) -> None: ...

    def status(self, *, max_age_seconds: float | None = None) -> dict[str, Any]: ...


CATALOG_HASH_SCHEMA = "fl-mcp.comfy-node-catalog-contract.v1"
NODE_SCHEMA_HASH_SCHEMA = "fl-mcp.comfy-node-schema-contract.v1"


# ============================================================================
# Exceptions
# ============================================================================


class NodeLibraryError(Exception):
    """Base exception for node library errors."""

    pass


class NodeLibraryConnectionError(NodeLibraryError):
    """Raised when ComfyUI server is unreachable."""

    pass


class NodeTypeNotFoundError(NodeLibraryError):
    """Raised when a node type doesn't exist."""

    pass


def canonical_schema_hash(value: Any) -> str:
    """Return a stable identity for JSON-compatible runtime schema data."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_value_type(value: Any) -> str:
    """Return the JSON type represented by a decoded value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def normalize_node_schema_contract(node_info: dict[str, Any]) -> dict[str, Any]:
    """Normalize runtime widget defaults out of a node's schema identity.

    ComfyUI calls ``INPUT_TYPES()`` while serving ``/object_info``. Some custom
    nodes generate a fresh widget default on every call, even though the node's
    input/output contract has not changed. A widget default value initializes a
    newly-created node; it does not alter whether the input exists or what can
    connect to it.

    Only the value at the documented input-spec metadata location is replaced.
    The presence and JSON type of the default remain part of the contract, as
    do enum choices, constraints, input ordering, outputs, and provenance. The
    returned copy is safe to hash without changing the raw catalog used by Ren.
    """
    normalized = copy.deepcopy(node_info)
    inputs = normalized.get("input")
    if not isinstance(inputs, dict):
        return normalized

    for input_group in inputs.values():
        if not isinstance(input_group, dict):
            continue
        for input_spec in input_group.values():
            if not (
                isinstance(input_spec, list)
                and len(input_spec) > 1
                and isinstance(input_spec[1], dict)
                and "default" in input_spec[1]
            ):
                continue
            input_spec[1]["default"] = {
                "$contract": "widget-default",
                "json_type": _json_value_type(input_spec[1]["default"]),
            }

    return normalized


def catalog_contract_hash(catalog: dict[str, Any]) -> str:
    """Hash the loaded node catalog's stable workflow-building contract."""
    normalized_catalog = {
        node_type: normalize_node_schema_contract(node_info)
        if isinstance(node_info, dict)
        else node_info
        for node_type, node_info in catalog.items()
    }
    return canonical_schema_hash(
        {
            "hash_schema": CATALOG_HASH_SCHEMA,
            "catalog": normalized_catalog,
        }
    )


def node_schema_hash(node_type: str, node_info: dict[str, Any]) -> str:
    return canonical_schema_hash(
        {
            "hash_schema": NODE_SCHEMA_HASH_SCHEMA,
            "node_type": node_type,
            "schema": normalize_node_schema_contract(node_info),
        }
    )


def classify_node_origin(node_info: dict[str, Any]) -> str:
    """Classify a loaded node from the provenance exposed by /object_info."""
    python_module = str(node_info.get("python_module") or "")
    category = str(node_info.get("category") or "").lower()
    if (
        bool(node_info.get("api_node"))
        or python_module.startswith("comfy_api_nodes.")
        or category == "partner"
        or category.startswith("partner/")
    ):
        return "partner"
    if python_module == "nodes" or python_module.startswith("comfy_extras."):
        return "native"
    if python_module.startswith("custom_nodes."):
        return "custom"
    return "unknown"


def catalog_origin_counts(catalog: dict[str, Any]) -> dict[str, int]:
    """Count loaded node classes by deterministic /object_info provenance."""
    counts = {"native": 0, "partner": 0, "custom": 0, "unknown": 0}
    for node_info in catalog.values():
        origin = (
            classify_node_origin(node_info)
            if isinstance(node_info, dict)
            else "unknown"
        )
        counts[origin] += 1
    return counts


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class NodeSearchResult:
    """Result from node library search."""

    node_type: str
    display_name: str
    category: str
    description: str
    inputs: dict[str, Any]
    outputs: list[str]
    match_reason: str
    origin: str
    python_module: str
    schema_hash: str
    score: int


@dataclass
class CompatibleNode:
    """Compatible node type for connection."""

    node_type: str
    display_name: str
    category: str
    direction: Literal["downstream", "upstream"]
    connection: dict[str, str]
    description: str


@dataclass(frozen=True)
class NodeCatalogSnapshot:
    data: dict[str, Any]
    source: str
    catalog_hash: str
    observed_catalog_hash: str
    catalog_hash_schema: str
    fetched_at: float
    expires_at: float
    node_schema_hashes: dict[str, str] = field(default_factory=dict)


# ============================================================================
# Cache
# ============================================================================


class NodeLibraryCache:
    """Cache for ComfyUI node library data."""

    def __init__(self, ttl_seconds: int = 300, clock: Callable[[], float] = time.time):
        self._snapshot: NodeCatalogSnapshot | None = None
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()

    async def get(self, *, max_age_seconds: float | None = None) -> NodeCatalogSnapshot | None:
        """Get cached data if valid, optionally against a stricter horizon than the TTL."""
        async with self._lock:
            if self._snapshot is None:
                return None

            age = self._clock() - self._snapshot.fetched_at
            horizon = self._ttl if max_age_seconds is None else max_age_seconds
            if age > horizon:
                logger.debug(f"[NodeLibrary] Cache expired (age: {age:.1f}s, horizon: {horizon:.1f}s)")
                return None

            logger.debug(f"[NodeLibrary] Cache hit (age: {age:.1f}s, horizon: {horizon:.1f}s)")
            return self._snapshot

    async def set(self, data: dict[str, Any], source: str) -> NodeCatalogSnapshot:
        """Set cache data."""
        async with self._lock:
            fetched_at = self._clock()
            self._snapshot = NodeCatalogSnapshot(
                data=data,
                source=source,
                catalog_hash=catalog_contract_hash(data),
                observed_catalog_hash=canonical_schema_hash(data),
                catalog_hash_schema=CATALOG_HASH_SCHEMA,
                fetched_at=fetched_at,
                expires_at=fetched_at + self._ttl,
                node_schema_hashes={
                    node_type: node_schema_hash(node_type, node_info)
                    for node_type, node_info in data.items()
                    if isinstance(node_info, dict)
                },
            )
            logger.debug(f"[NodeLibrary] Cache updated ({len(data)} nodes)")
            return self._snapshot

    async def status(self, source: str) -> dict[str, Any]:
        async with self._lock:
            if self._snapshot is None:
                return {
                    "state": "empty",
                    "source": source,
                    "node_count": 0,
                    "origin_counts": catalog_origin_counts({}),
                    "catalog_hash": None,
                    "observed_catalog_hash": None,
                    "catalog_hash_schema": CATALOG_HASH_SCHEMA,
                    "fetched_at": None,
                    "expires_at": None,
                }
            state = "fresh" if self._clock() <= self._snapshot.expires_at else "stale"
            return {
                "state": state,
                "source": self._snapshot.source,
                "node_count": len(self._snapshot.data),
                "origin_counts": catalog_origin_counts(self._snapshot.data),
                "catalog_hash": self._snapshot.catalog_hash,
                "observed_catalog_hash": self._snapshot.observed_catalog_hash,
                "catalog_hash_schema": self._snapshot.catalog_hash_schema,
                "fetched_at": self._snapshot.fetched_at,
                "expires_at": self._snapshot.expires_at,
            }

    async def snapshot(self) -> NodeCatalogSnapshot | None:
        """Return the current data and identity as one atomic generation."""
        async with self._lock:
            return self._snapshot

    async def invalidate(self):
        """Clear cache."""
        async with self._lock:
            self._snapshot = None
            logger.debug("[NodeLibrary] Cache invalidated")


# ============================================================================
# Core Client
# ============================================================================


class NodeLibraryClient:
    """Client for ComfyUI node library discovery."""

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8188",
        timeout: int = 10,
        *,
        cache_ttl: int = 300,
        clock: Callable[[], float] = time.time,
    ):
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self.cache = NodeLibraryCache(ttl_seconds=cache_ttl, clock=clock)
        self._fetch_lock = asyncio.Lock()
        self._persistence_lock = threading.RLock()
        self._persistence: NodeCatalogPersistence | None = None

    @property
    def source(self) -> str:
        return f"{self.server_url}/object_info"

    def bind_persistence(self, persistence: NodeCatalogPersistence) -> None:
        """Bind an optional persistent knowledge sidecar to this live client."""

        required_methods = ("reconcile", "record_refresh_failure", "status")
        if any(not callable(getattr(persistence, name, None)) for name in required_methods):
            raise TypeError(
                "persistence must provide reconcile, record_refresh_failure, and status"
            )
        with self._persistence_lock:
            self._persistence = persistence

    def unbind_persistence(
        self,
        persistence: NodeCatalogPersistence | None = None,
    ) -> bool:
        """Unbind persistence, optionally only when the supplied store is current."""

        with self._persistence_lock:
            if self._persistence is None:
                return False
            if persistence is not None and self._persistence is not persistence:
                return False
            self._persistence = None
            return True

    def _bound_persistence(self) -> NodeCatalogPersistence | None:
        with self._persistence_lock:
            return self._persistence

    async def persisted_catalog_status(
        self,
        *,
        max_age_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Return sidecar status for diagnostics, never as live catalog data."""

        persistence = self._bound_persistence()
        if persistence is None:
            return None
        try:
            return await asyncio.to_thread(
                persistence.status,
                max_age_seconds=max_age_seconds,
            )
        except Exception as exc:
            logger.warning(f"[NodeLibrary] Failed to read persisted status: {exc}")
            return {"state": "error", "error": str(exc)}

    async def _record_persistence_failure(
        self,
        error: str,
        *,
        persistence: NodeCatalogPersistence | None = None,
    ) -> None:
        if persistence is None:
            persistence = self._bound_persistence()
        if persistence is None:
            return
        try:
            await asyncio.to_thread(persistence.record_refresh_failure, error)
        except Exception as exc:
            logger.warning(f"[NodeLibrary] Failed to persist refresh error: {exc}")

    async def _persist_snapshot(self, snapshot: NodeCatalogSnapshot) -> None:
        persistence = self._bound_persistence()
        if persistence is None:
            return
        try:
            await asyncio.to_thread(
                persistence.reconcile,
                snapshot.data,
                source=snapshot.source,
                catalog_hash=snapshot.catalog_hash,
                observed_catalog_hash=snapshot.observed_catalog_hash,
                node_schema_hashes=snapshot.node_schema_hashes,
            )
        except Exception as exc:
            logger.warning(f"[NodeLibrary] Failed to reconcile persistent catalog: {exc}")
            await self._record_persistence_failure(
                f"Persistent catalog reconciliation failed: {exc}",
                persistence=persistence,
            )

    async def fetch_node_library(
        self,
        *,
        force_refresh: bool = False,
        max_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Fetch node library from ComfyUI /object_info endpoint.

        ``max_age_seconds`` lets a caller that needs a near-current catalog
        (e.g. before pinning a compile/apply hash) skip a real re-fetch when
        the cache is already fresh enough, without accepting the full
        cache_ttl staleness window ``force_refresh=False`` alone would allow.

        Returns:
            Dictionary mapping node type names to node metadata

        Raises:
            NodeLibraryConnectionError: If ComfyUI server is unreachable
        """
        if not force_refresh:
            cached = await self.cache.get(max_age_seconds=max_age_seconds)
            if cached is not None:
                return cached.data

        async with self._fetch_lock:
            if not force_refresh:
                cached = await self.cache.get(max_age_seconds=max_age_seconds)
                if cached is not None:
                    return cached.data

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    logger.info(f"[NodeLibrary] Fetching from {self.source}")
                    response = await client.get(self.source)
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise NodeLibraryConnectionError(
                            f"Invalid response from ComfyUI: expected dict, got {type(data)}"
                        )
                    logger.info(f"[NodeLibrary] Fetched {len(data)} node types")
                    snapshot = await self.cache.set(data, self.source)
                    await self._persist_snapshot(snapshot)
                    return data
            except NodeLibraryConnectionError as exc:
                await self._record_persistence_failure(str(exc))
                raise
            except httpx.TimeoutException as exc:
                error = NodeLibraryConnectionError(
                    f"ComfyUI server timeout. Is ComfyUI running at {self.server_url}?"
                )
                await self._record_persistence_failure(str(error))
                raise error from exc
            except httpx.HTTPStatusError as exc:
                error = NodeLibraryConnectionError(
                    f"ComfyUI server error: {exc.response.status_code}"
                )
                await self._record_persistence_failure(str(error))
                raise error from exc
            except httpx.RequestError as exc:
                error = NodeLibraryConnectionError(
                    f"Failed to connect to ComfyUI at {self.server_url}: {exc}"
                )
                await self._record_persistence_failure(str(error))
                raise error from exc
            except Exception as exc:
                logger.error(f"[NodeLibrary] Unexpected error: {exc}")
                error = NodeLibraryConnectionError(f"Failed to fetch node library: {exc}")
                await self._record_persistence_failure(str(error))
                raise error from exc

    async def catalog_snapshot(
        self,
        *,
        force_refresh: bool = False,
        max_age_seconds: float | None = None,
    ) -> NodeCatalogSnapshot:
        """Return one internally consistent catalog data/hash generation."""
        await self.fetch_node_library(force_refresh=force_refresh, max_age_seconds=max_age_seconds)
        snapshot = await self.cache.snapshot()
        if snapshot is None:
            raise NodeLibraryError("Loaded-node catalog snapshot is unavailable")
        return snapshot

    async def catalog_status(self, *, refresh: bool = False) -> dict[str, Any]:
        status = await self.cache.status(self.source)
        if refresh or status["state"] == "empty":
            await self.fetch_node_library(force_refresh=refresh)
            status = await self.cache.status(self.source)
        return status

    async def search_nodes(
        self,
        query: str | None = None,
        category: str | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        max_results: int = 20,
    ) -> list[NodeSearchResult]:
        """Search for node types by various criteria.

        Args:
            query: Text search in node names/descriptions
            category: Filter by category
            input_type: Find nodes accepting this input type
            output_type: Find nodes producing this output type
            max_results: Maximum results to return

        Returns:
            List of matching node search results
        """
        node_library = await self.fetch_node_library()
        ranked_results = []

        for node_type, node_info in node_library.items():
            match_reasons = []
            score = 0

            if query:
                text_match = self._text_match_score(node_type, node_info, query)
                if text_match is None:
                    continue
                score, reason = text_match
                match_reasons.append(reason)

            if category and not self._matches_category(node_info, category):
                continue
            if category:
                match_reasons.append(f"category='{category}'")

            if input_type and not self._has_input_type(node_info, input_type):
                continue
            if input_type:
                match_reasons.append(f"accepts input type '{input_type}'")

            if output_type and not self._has_output_type(node_info, output_type):
                continue
            if output_type:
                match_reasons.append(f"outputs type '{output_type}'")

            ranked_results.append(
                (
                    score,
                    NodeSearchResult(
                        node_type=node_type,
                        display_name=node_info.get("display_name", node_type),
                        category=node_info.get("category", ""),
                        description=node_info.get("description", ""),
                        inputs=node_info.get("input", {}),
                        outputs=node_info.get("output", []),
                        match_reason=", ".join(match_reasons) if match_reasons else "all nodes",
                        origin=classify_node_origin(node_info),
                        python_module=str(node_info.get("python_module") or ""),
                        schema_hash=node_schema_hash(node_type, node_info),
                        score=score,
                    ),
                )
            )

        ranked_results.sort(
            key=lambda item: (-item[0], item[1].node_type.casefold(), item[1].node_type)
        )
        results = [result for _, result in ranked_results[:max_results]]
        logger.info(f"[NodeLibrary] Search found {len(results)} results")
        return results

    async def get_node_details(self, node_type: str) -> dict[str, Any]:
        """Get detailed information about a specific node type.

        Args:
            node_type: Exact node type name

        Returns:
            Complete node metadata

        Raises:
            NodeTypeNotFoundError: If node type doesn't exist
        """
        node_library = await self.fetch_node_library()

        if node_type not in node_library:
            # Find similar nodes for suggestion
            similar = self._find_similar_node_types(node_type, node_library, max_suggestions=5)
            raise NodeTypeNotFoundError(
                f"Node type '{node_type}' not found.\n"
                + (f"Did you mean: {', '.join(similar)}?" if similar else "")
            )

        node_info = dict(node_library[node_type])
        status = await self.catalog_status()
        node_info.update(
            {
                "origin": classify_node_origin(node_info),
                "schema_hash": node_schema_hash(node_type, node_library[node_type]),
                "schema_hash_schema": NODE_SCHEMA_HASH_SCHEMA,
                "catalog_hash": status.get("catalog_hash"),
                "catalog_hash_schema": status.get("catalog_hash_schema"),
                "observed_catalog_hash": status.get("observed_catalog_hash"),
                "source": self.source,
            }
        )
        return node_info

    async def find_compatible_nodes(
        self,
        node_type: str,
        direction: Literal["downstream", "upstream", "both"] = "downstream",
        output_slot: str | None = None,
        input_slot: str | None = None,
        max_results: int = 30,
    ) -> list[CompatibleNode]:
        """Find node types compatible with a given node type.

        Args:
            node_type: Source node type name
            direction: Search direction (downstream/upstream/both)
            output_slot: Specific output slot to match (downstream only)
            input_slot: Specific input slot to match (upstream only)
            max_results: Maximum results per direction

        Returns:
            List of compatible node types

        Raises:
            NodeTypeNotFoundError: If source node type doesn't exist
        """
        node_library = await self.fetch_node_library()

        # Validate source node exists
        if node_type not in node_library:
            similar = self._find_similar_node_types(node_type, node_library, max_suggestions=5)
            raise NodeTypeNotFoundError(
                f"Node type '{node_type}' not found.\n"
                + (f"Did you mean: {', '.join(similar)}?" if similar else "")
            )

        source_node_info = node_library[node_type]
        compatible = []

        # Find downstream compatible (what can connect after)
        if direction in ["downstream", "both"]:
            downstream = self._find_downstream_compatible(
                source_node_info, node_library, output_slot, max_results
            )
            compatible.extend(downstream)

        # Find upstream compatible (what can connect before)
        if direction in ["upstream", "both"]:
            upstream = self._find_upstream_compatible(
                source_node_info, node_library, input_slot, max_results
            )
            compatible.extend(upstream)

        logger.info(f"[NodeLibrary] Found {len(compatible)} compatible nodes")
        return compatible

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _matches_text_query(self, node_type: str, node_info: dict[str, Any], query: str) -> bool:
        """Check if node matches text query."""
        return self._text_match_score(node_type, node_info, query) is not None

    def _text_match_score(
        self,
        node_type: str,
        node_info: dict[str, Any],
        query: str,
    ) -> tuple[int, str] | None:
        query_lower = query.strip().casefold()
        if not query_lower:
            return 0, "all nodes"

        display_name = str(node_info.get("display_name") or node_type)
        aliases = node_info.get("search_aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        fields = [
            (node_type, 100, 70, 50, "node type"),
            (display_name, 90, 65, 45, "display name"),
            *((str(alias), 80, 60, 40, "search alias") for alias in aliases),
        ]
        matches = []
        for value, exact_score, prefix_score, contains_score, label in fields:
            normalized = value.casefold()
            if normalized == query_lower:
                matches.append((exact_score, f"exact {label} match"))
            elif normalized.startswith(query_lower):
                matches.append((prefix_score, f"{label} prefix match"))
            elif query_lower in normalized:
                matches.append((contains_score, f"{label} contains query"))
        if matches:
            return max(matches, key=lambda item: item[0])

        description = str(node_info.get("description") or "").casefold()
        if query_lower in description:
            return 20, "description contains query"
        category = str(node_info.get("category") or "").casefold()
        if query_lower in category:
            return 10, "category contains query"
        return None

    def _has_input_type(self, node_info: dict[str, Any], input_type: str) -> bool:
        """Check if node has input of specified type."""
        inputs = node_info.get("input", {})
        required = inputs.get("required", {})
        optional = inputs.get("optional", {})

        for param_spec in {**required, **optional}.values():
            if isinstance(param_spec, list) and len(param_spec) > 0:
                if param_spec[0] == input_type:
                    return True

        return False

    def _has_output_type(self, node_info: dict[str, Any], output_type: str) -> bool:
        """Check if node has output of specified type."""
        outputs = node_info.get("output", [])
        return output_type in outputs

    def _matches_category(self, node_info: dict[str, Any], category: str) -> bool:
        """Check if node belongs to category."""
        node_category = node_info.get("category", "").lower()
        category_lower = category.lower()

        # Exact match or starts with (for subcategories like "image/upscaling")
        return node_category == category_lower or node_category.startswith(category_lower + "/")

    def _find_similar_node_types(
        self, query: str, node_library: dict[str, Any], max_suggestions: int = 5
    ) -> list[str]:
        """Find similar node type names for suggestions."""
        query_lower = query.lower()
        similar = []

        for node_type in sorted(node_library, key=lambda value: (value.casefold(), value)):
            # Simple similarity: contains query or query contains node type
            if query_lower in node_type.lower() or node_type.lower() in query_lower:
                similar.append(node_type)
                if len(similar) >= max_suggestions:
                    break

        return similar

    def _find_downstream_compatible(
        self,
        source_node_info: dict[str, Any],
        all_nodes: dict[str, Any],
        output_slot: str | None = None,
        max_results: int = 30,
    ) -> list[CompatibleNode]:
        """Find node types that can accept outputs from source node."""
        source_outputs = source_node_info.get("output", [])
        if not source_outputs:
            return []

        # Filter to specific output slot if requested
        if output_slot is not None:
            output_names = source_node_info.get("output_name", [])
            if output_slot not in output_names:
                return []
            idx = output_names.index(output_slot)
            if idx >= len(source_outputs):
                return []
            source_outputs = [source_outputs[idx]]

        compatible = []

        for node_type in sorted(all_nodes, key=lambda value: (value.casefold(), value)):
            node_info = all_nodes[node_type]
            inputs = node_info.get("input", {}).get("required", {})

            for input_name, input_spec in inputs.items():
                if isinstance(input_spec, list) and len(input_spec) > 0:
                    input_type = input_spec[0]

                    if input_type in source_outputs:
                        compatible.append(
                            CompatibleNode(
                                node_type=node_type,
                                display_name=node_info.get("display_name", node_type),
                                category=node_info.get("category", ""),
                                direction="downstream",
                                connection={
                                    "source_output": input_type,
                                    "target_input": input_name,
                                    "data_type": input_type,
                                },
                                description=node_info.get("description", ""),
                            )
                        )
                        break

            if len(compatible) >= max_results:
                break

        return compatible

    def _find_upstream_compatible(
        self,
        target_node_info: dict[str, Any],
        all_nodes: dict[str, Any],
        input_slot: str | None = None,
        max_results: int = 30,
    ) -> list[CompatibleNode]:
        """Find node types that can provide inputs to target node."""
        target_inputs = target_node_info.get("input", {}).get("required", {})
        if not target_inputs:
            return []

        # Filter to specific input slot if requested
        if input_slot is not None:
            if input_slot in target_inputs:
                target_inputs = {input_slot: target_inputs[input_slot]}
            else:
                return []

        # Collect required input types
        required_types = set()
        for input_spec in target_inputs.values():
            if isinstance(input_spec, list) and len(input_spec) > 0:
                required_types.add(input_spec[0])

        if not required_types:
            return []

        compatible = []

        for node_type in sorted(all_nodes, key=lambda value: (value.casefold(), value)):
            node_info = all_nodes[node_type]
            outputs = node_info.get("output", [])

            for output_type in outputs:
                if output_type in required_types:
                    # Find which input this satisfies
                    for input_name, input_spec in target_inputs.items():
                        if isinstance(input_spec, list) and input_spec[0] == output_type:
                            compatible.append(
                                CompatibleNode(
                                    node_type=node_type,
                                    display_name=node_info.get("display_name", node_type),
                                    category=node_info.get("category", ""),
                                    direction="upstream",
                                    connection={
                                        "source_output": output_type,
                                        "target_input": input_name,
                                        "data_type": output_type,
                                    },
                                    description=node_info.get("description", ""),
                                )
                            )
                            break
                    break

            if len(compatible) >= max_results:
                break

        return compatible


# ============================================================================
# Global Instance
# ============================================================================

_node_library_clients: dict[tuple[str, int], NodeLibraryClient] = {}


def get_node_library_client(
    server_url: str = "http://127.0.0.1:8188", timeout: int = 10
) -> NodeLibraryClient:
    """Get or create the global NodeLibraryClient instance."""
    normalized_url = server_url.rstrip("/")
    key = (normalized_url, timeout)
    if key not in _node_library_clients:
        _node_library_clients[key] = NodeLibraryClient(normalized_url, timeout)
    return _node_library_clients[key]
