"""FastAPI bridge server for ComfyUI FL-MCP."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))
# Also add this file's own directory so flat sibling imports (comfy_supervisor,
# config, manager, ...) resolve. The embedded ComfyUI Python uses a ._pth file
# and does NOT auto-prepend the script directory to sys.path.
sys.path.insert(0, str(Path(__file__).parent))

from comfy_supervisor import comfy_supervisor
from config import DATA_DIR, settings
from manager import manager
from models import (
    Handshake,
    REQUIRED_FRONTEND_TOOL_CONTRACT_REVISIONS,
    ScreenshotMessage,
    ToolResult,
)
from node_catalog_store import NodeCatalogStore
from node_library import get_node_library_client
from process_utils import pid_is_running
from version import __version__, runtime_metadata

LOG_LEVEL = getattr(logging, settings.log_level, logging.INFO)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_JS_DIR = PROJECT_ROOT / "web" / "js"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "fl_mcp_server.log", mode="a", encoding="utf-8"),
    ],
)

logger = logging.getLogger("fl_mcp_server")

NODE_CATALOG_DB_NAME = "node_catalog.sqlite3"
NODE_CATALOG_STARTUP_ATTEMPTS = 8
NODE_CATALOG_INITIAL_RETRY_DELAY_SECONDS = 0.5
NODE_CATALOG_MAX_RETRY_DELAY_SECONDS = 8.0


async def cleanup_task() -> None:
    """Clean up stale disconnected sessions."""
    while True:
        await asyncio.sleep(60)
        cleaned = manager.cleanup_stale_sessions()
        if cleaned:
            logger.info("Cleaned up %s stale sessions", cleaned)


async def parent_watchdog_task(parent_pid: int) -> None:
    """Exit this managed backend if the ComfyUI parent process disappears."""
    logger.info("Parent watchdog enabled for PID %s", parent_pid)
    while True:
        await asyncio.sleep(2)
        try:
            parent_alive = pid_is_running(parent_pid)
        except Exception as exc:
            logger.warning("Parent watchdog check failed: %s", exc)
            continue
        if not parent_alive:
            logger.warning("ComfyUI parent process exited; stopping managed backend")
            os._exit(0)


async def reconcile_node_catalog_on_startup(
    client: Any,
    *,
    max_attempts: int = NODE_CATALOG_STARTUP_ATTEMPTS,
    initial_retry_delay: float = NODE_CATALOG_INITIAL_RETRY_DELAY_SECONDS,
    max_retry_delay: float = NODE_CATALOG_MAX_RETRY_DELAY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Warm the durable node catalog without delaying bridge availability.

    ComfyUI and the bridge start independently, so ``/object_info`` may be
    unavailable for a short period. The bound ``NodeCatalogStore`` records each
    failed refresh while retaining its last valid generation. Retries are
    deliberately bounded; later normal node-library refreshes continue healing
    the same persistent store.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if initial_retry_delay < 0 or max_retry_delay < 0:
        raise ValueError("catalog retry delays must not be negative")

    retry_delay = min(initial_retry_delay, max_retry_delay)
    for attempt in range(1, max_attempts + 1):
        try:
            await client.fetch_node_library(force_refresh=True)
            status = await client.persisted_catalog_status()
            if not status or status.get("state") != "fresh":
                state = status.get("state") if status else "unavailable"
                detail = None
                if status:
                    detail = status.get("last_error") or status.get("error")
                raise RuntimeError(
                    f"persistent node catalog remained {state}"
                    + (f": {detail}" if detail else "")
                )
            logger.info(
                "Persistent node catalog reconciled on startup "
                "(attempt=%s, generation=%s, nodes=%s)",
                attempt,
                status.get("generation") if status else None,
                status.get("node_count") if status else None,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= max_attempts:
                logger.warning(
                    "Persistent node catalog startup reconciliation exhausted "
                    "%s attempts; retaining the last valid generation: %s",
                    max_attempts,
                    exc,
                )
                return False
            logger.info(
                "ComfyUI node catalog is not ready (attempt %s/%s): %s; "
                "retrying in %.1fs",
                attempt,
                max_attempts,
                exc,
                retry_delay,
            )
            await sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)

    return False


@asynccontextmanager
async def node_catalog_persistence_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the bridge process's durable node-catalog binding and warm-up task."""

    catalog_store: NodeCatalogStore | None = None
    catalog_client: Any | None = None
    reconciliation_handle: asyncio.Task[bool] | None = None
    persistence_bound = False

    try:
        catalog_store = NodeCatalogStore(DATA_DIR / NODE_CATALOG_DB_NAME)
        catalog_client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        catalog_client.bind_persistence(catalog_store)
        persistence_bound = True
        reconciliation_handle = asyncio.create_task(
            reconcile_node_catalog_on_startup(catalog_client),
            name="fl-mcp-node-catalog-reconcile",
        )
        app.state.node_catalog_store = catalog_store
        app.state.node_catalog_reconciliation_task = reconciliation_handle
    except Exception as exc:
        # Persistent discovery enriches Ren but must never prevent the bridge or
        # canvas connection from becoming available.
        logger.warning("Persistent node catalog is unavailable: %s", exc, exc_info=True)
        app.state.node_catalog_store = None
        app.state.node_catalog_reconciliation_task = None

    try:
        yield
    finally:
        if reconciliation_handle is not None:
            reconciliation_handle.cancel()
            try:
                await reconciliation_handle
            except asyncio.CancelledError:
                pass
        if persistence_bound and catalog_client is not None:
            try:
                catalog_client.unbind_persistence(catalog_store)
            except Exception as exc:
                logger.warning("Node catalog persistence unbind failed: %s", exc)
        if catalog_store is not None:
            try:
                await asyncio.to_thread(catalog_store.close)
            except Exception as exc:
                logger.warning("Node catalog persistence close failed: %s", exc)
        app.state.node_catalog_store = None
        app.state.node_catalog_reconciliation_task = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting ComfyUI FL-MCP bridge server")
    cleanup_handle = asyncio.create_task(cleanup_task())
    watchdog_handle: Optional[asyncio.Task[None]] = None

    parent_pid_raw = os.getenv("FL_MCP_PARENT_PID")
    mcp_mode = os.getenv("FL_MCP_MODE", "embedded").lower()
    if parent_pid_raw and mcp_mode != "daemon":
        try:
            parent_pid = int(parent_pid_raw)
            if parent_pid > 0:
                watchdog_handle = asyncio.create_task(parent_watchdog_task(parent_pid))
        except ValueError:
            logger.warning("Ignoring invalid FL_MCP_PARENT_PID=%r", parent_pid_raw)

    async with node_catalog_persistence_lifespan(app):
        try:
            yield
        finally:
            logger.info("Shutting down ComfyUI FL-MCP bridge server")
            try:
                from chat_runtime import chat_runtime

                await chat_runtime.shutdown()
            except Exception as exc:
                logger.warning("Embedded chat shutdown cleanup failed: %s", exc)
            try:
                from chat_routes import web_image_previews

                await web_image_previews.aclose()
            except Exception as exc:
                logger.warning("Web image preview cleanup failed: %s", exc)
            cleanup_handle.cancel()
            if watchdog_handle:
                watchdog_handle.cancel()
            for handle in (cleanup_handle, watchdog_handle):
                if not handle:
                    continue
                try:
                    await handle
                except asyncio.CancelledError:
                    pass


app = FastAPI(
    title="ComfyUI FL-MCP",
    description="MCP bridge and tooling server for ComfyUI",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-FL-MCP-Run-Id",
        "X-FL-MCP-Conversation-Id",
    ],
)

app.mount("/js", StaticFiles(directory=str(WEB_JS_DIR)), name="shared_js")

try:
    from chat_routes import router as chat_router

    app.include_router(chat_router)
except Exception as exc:
    logger.error("Embedded chat routes are unavailable: %s", exc, exc_info=True)


@app.get("/")
async def root() -> Dict[str, str]:
    return {"name": "ComfyUI FL-MCP", "version": __version__, "status": "running"}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "runtime": runtime_metadata(),
        "active_connections": manager.get_active_session_count(),
        "total_sessions": manager.get_total_session_count(),
        "sessions": [
            {
                "session_id": session_id,
                "connections": manager.get_connection_info(session_id),
                "last_activity": context.last_activity.isoformat(),
            }
            for session_id, context in manager.session_contexts.items()
        ],
    }


@app.get("/api/mcp/status")
async def mcp_status() -> Dict[str, Any]:
    return {
        "mode": os.getenv("FL_MCP_MODE", "embedded"),
        "pid": os.getpid(),
        "port": settings.ws_port,
        "healthy": True,
        "runtime": runtime_metadata(),
        "activeConnections": manager.get_active_session_count(),
        "totalSessions": manager.get_total_session_count(),
    }


@app.post("/api/mcp/shutdown")
async def mcp_shutdown() -> JSONResponse:
    mode = os.getenv("FL_MCP_MODE", "embedded")
    if mode != "daemon":
        return JSONResponse(
            {"success": False, "error": "Shutdown is only available in daemon mode.", "mode": mode},
            status_code=409,
        )

    async def shutdown_later() -> None:
        await asyncio.sleep(0.25)
        os._exit(0)

    asyncio.create_task(shutdown_later())
    return JSONResponse({"success": True, "mode": mode})


@app.get("/api/config")
async def get_client_config() -> Dict[str, Any]:
    public_ws_source = settings.public_url if settings.public_url else ""
    if public_ws_source and public_ws_source not in {"http://127.0.0.1:8000", ""}:
        ws_url = public_ws_source.replace("https://", "wss://").replace("http://", "ws://")
        if not ws_url.endswith("/ws"):
            ws_url = f"{ws_url}/ws"
    else:
        ws_url = f"ws://{settings.ws_host}:{settings.ws_port}/ws"
    return {"ws_url": ws_url, "version": __version__, "public_url": settings.public_url}


@app.get("/api/sessions")
async def list_sessions() -> Dict[str, Any]:
    sessions = []
    for session_id, context in manager.session_contexts.items():
        sessions.append({
            "session_id": session_id,
            "connections": manager.get_connection_info(session_id),
            "last_activity": context.last_activity.isoformat(),
            "has_frontend": manager.has_connection(session_id, "frontend"),
            "has_mcp": manager.has_connection(session_id, "mcp"),
        })
    return {"sessions": sessions, "total": len(sessions)}


@app.get("/api/comfy/status")
async def comfy_status() -> Dict[str, Any]:
    return comfy_supervisor.status()


@app.post("/api/comfy/start")
async def comfy_start() -> Dict[str, Any]:
    if not settings.enable_comfy_process_control:
        return {
            "success": False,
            "error": (
                "disabled_by_config: enable Process control under "
                "Ren > Settings > Bridge & safety, then restart ComfyUI."
            ),
            "disabled_by_config": True,
            "required_setting": "enable_comfy_process_control",
        }
    return comfy_supervisor.start()


@app.post("/api/comfy/stop")
async def comfy_stop() -> Dict[str, Any]:
    if not settings.enable_comfy_process_control:
        return {
            "success": False,
            "error": (
                "disabled_by_config: enable Process control under "
                "Ren > Settings > Bridge & safety, then restart ComfyUI."
            ),
            "disabled_by_config": True,
            "required_setting": "enable_comfy_process_control",
        }
    return comfy_supervisor.stop()


@app.post("/api/comfy/restart")
async def comfy_restart() -> Dict[str, Any]:
    if not settings.enable_comfy_process_control:
        return {
            "success": False,
            "error": (
                "disabled_by_config: enable Process control under "
                "Ren > Settings > Bridge & safety, then restart ComfyUI."
            ),
            "disabled_by_config": True,
            "required_setting": "enable_comfy_process_control",
        }
    return comfy_supervisor.restart()


@app.get("/api/comfy/logs")
async def comfy_logs(limit: int = 300) -> Dict[str, Any]:
    return comfy_supervisor.logs(limit=limit)


@app.patch("/api/comfy/config")
async def comfy_config(request: Request) -> JSONResponse:
    data = await request.json()
    return JSONResponse({"config": comfy_supervisor.save_config(data)})


@app.get("/api/view")
async def view_image(
    filename: str,
    subfolder: str = "",
    type: str = "output",
    rand: float = 0.0,
) -> FileResponse:
    del rand
    from comfy_tools import get_comfy_tools

    if type not in {"output", "input", "temp"}:
        raise HTTPException(status_code=400, detail=f"Invalid type: {type}")

    comfy_tools = get_comfy_tools()
    base_paths = {
        "output": comfy_tools.comfyui_root / "output",
        "input": comfy_tools.comfyui_root / "input",
        "temp": comfy_tools.comfyui_root / "temp",
    }
    base_path = base_paths[type]
    file_path = (base_path / subfolder / filename if subfolder else base_path / filename).resolve()
    base_path_resolved = base_path.resolve()
    try:
        file_path.relative_to(base_path_resolved)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(file_path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    session_id: str | None = None
    connection_type = "frontend"
    client_id = "browser"

    try:
        await websocket.accept()
        handshake_data = await websocket.receive_json()
        if handshake_data.get("type") != "handshake":
            await websocket.send_json({
                "type": "error",
                "error_code": "INVALID_HANDSHAKE",
                "message": "First message must be handshake",
            })
            await websocket.close()
            return

        try:
            handshake = Handshake(**handshake_data)
        except Exception as exc:
            await websocket.send_json({
                "type": "error",
                "error_code": "INVALID_HANDSHAKE_DATA",
                "message": f"Invalid handshake data: {exc}",
            })
            await websocket.close()
            return

        session_id = handshake.session_id
        version = (handshake.client_version or "").lower()
        connection_type = handshake.connection_type or (
            "mcp" if "mcp" in version else "frontend"
        )
        if connection_type != "frontend" and (
            handshake.supported_tools is not None
            or handshake.tool_manifest_hash is not None
            or handshake.tool_contract_revisions is not None
            or handshake.tool_contract_manifest_hash is not None
        ):
            await websocket.send_json({
                "type": "error",
                "error_code": "INVALID_HANDSHAKE_DATA",
                "message": "Only frontend clients may advertise tool manifests.",
            })
            await websocket.close()
            return
        required_tool = "apply_workflow_graph_patch"
        required_revision = REQUIRED_FRONTEND_TOOL_CONTRACT_REVISIONS[required_tool]
        advertised_revision = (
            handshake.tool_contract_revisions.get(required_tool)
            if handshake.tool_contract_revisions is not None
            else None
        )
        frontend_outdated_reason = None
        if connection_type == "frontend":
            if handshake.supported_tools is None:
                frontend_outdated_reason = "capability_manifest_missing"
            elif required_tool not in handshake.supported_tools:
                frontend_outdated_reason = "required_capability_missing"
            elif handshake.tool_contract_revisions is None:
                frontend_outdated_reason = "contract_revision_manifest_missing"
            elif advertised_revision is None:
                frontend_outdated_reason = "required_contract_revision_missing"
            elif advertised_revision < required_revision:
                frontend_outdated_reason = "tool_contract_revision_outdated"
            elif handshake.tool_contract_manifest_hash is None:
                frontend_outdated_reason = "contract_revision_hash_missing"
        if frontend_outdated_reason is not None:
            await websocket.send_json({
                "type": "error",
                "error_code": "frontend_bridge_outdated",
                "message": (
                    "The connected ComfyUI browser bridge is outdated. Reload the "
                    "ComfyUI frontend before using Ren workflow tools."
                ),
                "error_details": {
                    "bridge_state": "frontend_bridge_outdated",
                    "capability_code": "frontend_capability_missing",
                    "reason": frontend_outdated_reason,
                    "requested_tool": required_tool,
                    "supported_tool_count": (
                        len(handshake.supported_tools)
                        if handshake.supported_tools is not None
                        else None
                    ),
                    "tool_manifest_hash": handshake.tool_manifest_hash,
                    "required_contract_revision": required_revision,
                    "advertised_contract_revision": advertised_revision,
                    "tool_contract_manifest_hash": (
                        handshake.tool_contract_manifest_hash
                    ),
                },
            })
            # A clean close is terminal even for legacy browser clients, so an
            # outdated tab cannot reconnect-loop or contend with a capable tab.
            await websocket.close(code=1000, reason="frontend_bridge_outdated")
            return
        client_id = handshake.client_id or (
            "legacy-mcp" if connection_type == "mcp" else "browser"
        )
        is_reconnect = manager.has_connection(session_id, connection_type, client_id)
        await manager.connect(
            websocket,
            session_id,
            connection_type,
            client_id,
            supported_tools=(
                handshake.supported_tools if connection_type == "frontend" else None
            ),
            tool_manifest_hash=(
                handshake.tool_manifest_hash if connection_type == "frontend" else None
            ),
            tool_contract_revisions=(
                handshake.tool_contract_revisions
                if connection_type == "frontend"
                else None
            ),
            tool_contract_manifest_hash=(
                handshake.tool_contract_manifest_hash
                if connection_type == "frontend"
                else None
            ),
        )
        await manager.send_handshake_ack(
            session_id,
            is_reconnect,
            connection_type,
            client_id,
        )
        logger.info(
            "Session %s connected as %s/%s",
            session_id,
            connection_type,
            client_id,
        )

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            msg_session_id = data.get("session_id")
            if msg_session_id != session_id:
                await manager.send_error(
                    session_id,
                    "SESSION_MISMATCH",
                    f"Message session_id '{msg_session_id}' does not match connection session_id '{session_id}'",
                    target=connection_type,
                    client_id=client_id,
                )
                continue

            if msg_type == "tool_result":
                await handle_tool_result(session_id, data)
            elif msg_type == "tool_request":
                if connection_type != "mcp":
                    await manager.send_error(
                        session_id,
                        "INVALID_CLIENT_ROLE",
                        "Only MCP connections may send tool requests.",
                        target=connection_type,
                        client_id=client_id,
                    )
                    continue
                await route_tool_request_to_frontend(session_id, data, client_id)
            elif msg_type == "tool_report":
                await route_tool_report_to_frontend(session_id, data)
            elif msg_type == "screenshot":
                await handle_screenshot(session_id, data)
            elif msg_type == "comfy_error":
                await manager.handle_comfy_error(data.get("data") or {})
            elif msg_type == "queue_status":
                await manager.handle_queue_status(data.get("data") or {})
            elif msg_type == "execution_event":
                await manager.handle_execution_event(data.get("event"), data.get("data") or {})
            else:
                await manager.send_error(
                    session_id,
                    "UNKNOWN_MESSAGE_TYPE",
                    f"Unknown message type: {msg_type}",
                    target=connection_type,
                    client_id=client_id,
                )

    except WebSocketDisconnect:
        if session_id:
            manager.disconnect(session_id, websocket, connection_type, client_id)
            logger.info("Session %s disconnected from %s", session_id, connection_type)
    except Exception as exc:
        logger.error("Error in WebSocket connection: %s", exc, exc_info=True)
        if session_id:
            manager.disconnect(session_id, websocket, connection_type, client_id)
        try:
            await websocket.close()
        except Exception:
            pass


async def handle_tool_result(session_id: str, data: Dict[str, Any]) -> None:
    try:
        result = ToolResult(**data)
    except Exception as exc:
        logger.error("Invalid tool result: %s", exc, exc_info=True)
        await manager.send_error(session_id, "TOOL_RESULT_ERROR", str(exc), target="frontend")
        return

    owner_client_id = manager.resolve_tool_request(session_id, result.request_id)
    if owner_client_id and manager.has_connection(session_id, "mcp", owner_client_id):
        await manager.send_message(
            session_id,
            data,
            target="mcp",
            client_id=owner_client_id,
        )
        logger.info("Tool result routed to MCP: request_id=%s", result.request_id)
    else:
        logger.warning(
            "No MCP request owner for tool result: request_id=%s",
            result.request_id,
        )


async def handle_screenshot(session_id: str, data: Dict[str, Any]) -> None:
    try:
        screenshot_msg = ScreenshotMessage(**data)
        from comfy_tools import get_comfy_tools

        screenshot_dir = get_comfy_tools().comfyui_root / "output" / "screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        base64_str = screenshot_msg.base64_data.split(";base64,", 1)[-1]
        ext = "jpg" if screenshot_msg.format == "jpeg" else "png"
        filename = f"{screenshot_msg.screenshot_id}.{ext}"
        file_path = screenshot_dir / filename
        file_path.write_bytes(base64.b64decode(base64_str))
        await manager.send_message(session_id, {
            "type": "screenshot_saved",
            "session_id": session_id,
            "screenshot_id": screenshot_msg.screenshot_id,
            "filename": filename,
            "path": str(file_path),
        }, target="frontend")
    except Exception as exc:
        logger.error("Error handling screenshot: %s", exc, exc_info=True)
        await manager.send_error(session_id, "SCREENSHOT_ERROR", str(exc), target="frontend")


async def route_tool_request_to_frontend(
    session_id: str,
    data: Dict[str, Any],
    client_id: str,
) -> None:
    request_id = str(data.get("request_id") or "")
    if not request_id:
        await manager.send_error(
            session_id,
            "MISSING_REQUEST_ID",
            "Tool requests must include request_id.",
            target="mcp",
            client_id=client_id,
        )
        return
    if not manager.register_tool_request(session_id, request_id, client_id):
        await manager.send_message(session_id, {
            "type": "tool_result",
            "session_id": session_id,
            "request_id": request_id,
            "success": False,
            "error": "duplicate_request_id: this request ID is already active",
            "execution_time_ms": 0,
        }, target="mcp", client_id=client_id)
        return
    if not manager.has_connection(session_id, "frontend"):
        manager.resolve_tool_request(session_id, request_id)
        error_msg = (
            "requires_browser_bridge: no ComfyUI browser bridge is connected for this "
            "session. Open ComfyUI in a browser and keep the FL-MCP bridge panel connected."
        )
        logger.warning(error_msg)
        await manager.send_message(session_id, {
            "type": "tool_result",
            "session_id": session_id,
            "request_id": data.get("request_id"),
            "success": False,
            "error": error_msg,
            "execution_time_ms": 0,
        }, target="mcp", client_id=client_id)
        return

    tool_name = str(data.get("tool_name") or "")
    capability_failure = manager.frontend_tool_capability_failure(
        session_id,
        tool_name,
    )
    if capability_failure is not None:
        manager.resolve_tool_request(session_id, request_id)
        logger.warning(
            "Frontend capability guard rejected tool %s for session %s: %s",
            tool_name,
            session_id,
            capability_failure["error_code"],
        )
        await manager.send_message(session_id, {
            "type": "tool_result",
            "session_id": session_id,
            "request_id": request_id,
            "success": False,
            **capability_failure,
            "execution_time_ms": 0,
        }, target="mcp", client_id=client_id)
        return

    await manager.send_message(session_id, data, target="frontend")


async def route_tool_report_to_frontend(session_id: str, data: Dict[str, Any]) -> None:
    if manager.has_connection(session_id, "frontend"):
        await manager.send_message(session_id, data, target="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.server:app",
        host=settings.ws_host,
        port=settings.ws_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
