"""FastAPI bridge server for ComfyUI FL-MCP."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

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
from config import settings
from manager import manager
from models import Handshake, ScreenshotMessage, ToolResult


LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", settings.log_level).upper(), logging.INFO)
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
            os.kill(parent_pid, 0)
        except PermissionError:
            continue
        except ProcessLookupError:
            logger.warning("ComfyUI parent process exited; stopping managed backend")
            os._exit(0)
        except Exception as exc:
            logger.warning("Parent watchdog check failed: %s", exc)


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

    try:
        yield
    finally:
        logger.info("Shutting down ComfyUI FL-MCP bridge server")
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
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/js", StaticFiles(directory=str(WEB_JS_DIR)), name="shared_js")


@app.get("/")
async def root() -> Dict[str, str]:
    return {"name": "ComfyUI FL-MCP", "version": "0.4.0", "status": "running"}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
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
    return {"ws_url": ws_url, "version": "0.4.0", "public_url": settings.public_url}


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
            "error": "disabled_by_config: set FL_MCP_ENABLE_COMFY_PROCESS_CONTROL=true to enable this endpoint.",
            "disabled_by_config": True,
        }
    return comfy_supervisor.start()


@app.post("/api/comfy/stop")
async def comfy_stop() -> Dict[str, Any]:
    if not settings.enable_comfy_process_control:
        return {
            "success": False,
            "error": "disabled_by_config: set FL_MCP_ENABLE_COMFY_PROCESS_CONTROL=true to enable this endpoint.",
            "disabled_by_config": True,
        }
    return comfy_supervisor.stop()


@app.post("/api/comfy/restart")
async def comfy_restart() -> Dict[str, Any]:
    if not settings.enable_comfy_process_control:
        return {
            "success": False,
            "error": "disabled_by_config: set FL_MCP_ENABLE_COMFY_PROCESS_CONTROL=true to enable this endpoint.",
            "disabled_by_config": True,
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
        connection_type = "mcp" if "mcp" in version else "frontend"
        is_reconnect = manager.has_connection(session_id, connection_type)
        await manager.connect(websocket, session_id, connection_type)
        await manager.send_handshake_ack(session_id, is_reconnect, connection_type)
        logger.info("Session %s connected as %s", session_id, connection_type)

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
                )
                continue

            if msg_type == "tool_result":
                await handle_tool_result(session_id, data)
            elif msg_type == "tool_request":
                await route_tool_request_to_frontend(session_id, data)
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
                )

    except WebSocketDisconnect:
        if session_id:
            manager.disconnect(session_id, connection_type)
            logger.info("Session %s disconnected from %s", session_id, connection_type)
    except Exception as exc:
        logger.error("Error in WebSocket connection: %s", exc, exc_info=True)
        if session_id:
            manager.disconnect(session_id, connection_type)
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

    if manager.has_connection(session_id, "mcp"):
        await manager.send_message(session_id, data, target="mcp")
        logger.info("Tool result routed to MCP: request_id=%s", result.request_id)
    else:
        logger.warning("No MCP connection for tool result: request_id=%s", result.request_id)


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


async def route_tool_request_to_frontend(session_id: str, data: Dict[str, Any]) -> None:
    if not manager.has_connection(session_id, "frontend"):
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
        }, target="mcp")
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
