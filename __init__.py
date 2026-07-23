"""ComfyUI FL-MCP custom node extension.

This extension registers the browser bridge JavaScript and optional backend
launcher routes used by the MCP server.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).parent
BACKEND_DIR = ROOT / "backend"
STATE_DIR = ROOT / ".fl_mcp"
PID_FILE = STATE_DIR / "daemon.pid"
LAUNCH_LOG = ROOT / "logs" / "fl_mcp_launcher.log"

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(ROOT))

from backend.process_utils import daemon_process_kwargs, pid_is_running

try:
    from backend.config import settings

    AUTO_START = settings.auto_start_backend
    AUTO_RESTART = settings.auto_restart_backend
    LOG_TO_FILE = settings.log_backend_to_file
    LAUNCH_MODE = settings.backend_launch_mode
    PORT = settings.ws_port
except Exception as exc:
    print(f"[FL-MCP] Warning: could not load config ({exc}), using defaults")
    AUTO_START = True
    AUTO_RESTART = True
    LOG_TO_FILE = True
    LAUNCH_MODE = "auto"
    PORT = 8000


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _request_json(url: str, timeout: float = 1.0, method: str = "GET"):
    parsed = urlsplit(url)
    connection = None
    try:
        connection = http.client.HTTPConnection(
            parsed.hostname or "127.0.0.1",
            parsed.port or 80,
            timeout=timeout,
        )
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        body = b"{}" if method == "POST" else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _read_daemon_pid():
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if pid_is_running(pid) else None


def _launcher_status():
    backend_url = f"http://127.0.0.1:{PORT}"
    mcp_status = _request_json(f"{backend_url}/api/mcp/status")
    health = _request_json(f"{backend_url}/health")
    pid = _read_daemon_pid()
    backend_reachable = bool(health or mcp_status or _is_port_open(PORT))
    mode = (mcp_status or {}).get("mode")
    return {
        "backendUrl": backend_url,
        "wsUrl": f"ws://127.0.0.1:{PORT}/ws",
        "backendReachable": backend_reachable,
        "health": health,
        "mcpStatus": mcp_status,
        "mode": mode or ("unknown" if backend_reachable else "stopped"),
        "daemonPid": pid,
        "canStart": not backend_reachable,
        "canStop": bool(backend_reachable and (mode == "daemon" or pid)),
        "port": PORT,
    }


def _start_daemon():
    status = _launcher_status()
    if status["backendReachable"]:
        return status | {"started": False, "message": "FL-MCP backend is already running."}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["FL_MCP_MODE"] = "daemon"
    env.pop("FL_MCP_PARENT_PID", None)
    env.pop("FL_MCP_MANAGED_BACKEND", None)

    with open(LAUNCH_LOG, "a", buffering=1, encoding="utf-8") as log:
        log.write(f"\n{'=' * 80}\n")
        log.write(f"FL-MCP daemon launcher start: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"{'=' * 80}\n")
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_daemon.py")],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **daemon_process_kwargs(),
        )

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    for _ in range(40):
        time.sleep(0.25)
        status = _launcher_status()
        if status["backendReachable"]:
            return status | {"started": True, "pid": proc.pid}
    return _launcher_status() | {
        "started": False,
        "pid": proc.pid,
        "error": "FL-MCP daemon did not become healthy before timeout.",
    }


def _stop_daemon():
    status = _launcher_status()
    backend_url = status["backendUrl"]
    mcp_status = status.get("mcpStatus") or {}

    if status["backendReachable"] and mcp_status.get("mode") == "daemon":
        _request_json(f"{backend_url}/api/mcp/shutdown", timeout=1, method="POST")

    pid = _read_daemon_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    for _ in range(20):
        time.sleep(0.25)
        if not _is_port_open(PORT):
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass
            return _launcher_status() | {"stopped": True}

    return _launcher_status() | {"stopped": False, "error": "FL-MCP daemon did not stop before timeout."}


try:
    comfy_server_module = sys.modules.get("server")
    if comfy_server_module is None or not hasattr(comfy_server_module, "PromptServer"):
        raise RuntimeError("ComfyUI PromptServer module is not available")
    PromptServer = comfy_server_module.PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/fl_mcp/launcher/status")
    async def fl_mcp_launcher_status(request):
        return web.json_response(_launcher_status())

    @PromptServer.instance.routes.post("/fl_mcp/launcher/start")
    async def fl_mcp_launcher_start(request):
        return web.json_response(await asyncio.to_thread(_start_daemon))

    @PromptServer.instance.routes.post("/fl_mcp/launcher/stop")
    async def fl_mcp_launcher_stop(request):
        return web.json_response(await asyncio.to_thread(_stop_daemon))

    print("[FL-MCP] Registered launcher routes")
except Exception as exc:
    print(f"[FL-MCP] Warning: could not register launcher routes: {exc}")


if AUTO_START:
    try:
        from backend.server_runner import ServerRunner

        print("=" * 80)
        print("[FL-MCP] Initializing ComfyUI FL-MCP bridge...")
        print("=" * 80)

        server_runner = ServerRunner(
            backend_dir=str(BACKEND_DIR),
            port=PORT,
            launch_mode=LAUNCH_MODE,
            auto_start=True,
            auto_restart=AUTO_RESTART,
            log_to_file=LOG_TO_FILE,
        )
        _FL_MCP_SERVER = server_runner

        print("=" * 80)
        print("[FL-MCP] Initialization complete")
        print("=" * 80)
    except Exception as exc:
        print("=" * 80)
        print(f"[FL-MCP] Failed to start backend server: {exc}")
        print(f"[FL-MCP] Start manually: cd {BACKEND_DIR} && python server.py")
        print("=" * 80)
else:
    print("=" * 80)
    print("[FL-MCP] Backend auto-start disabled")
    print(f"[FL-MCP] Start manually: cd {BACKEND_DIR} && python server.py")
    print("=" * 80)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
