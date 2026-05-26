"""FL_JS Agentic System for ComfyUI

AI-powered workflow assistant with natural language interface.

This extension can start a hidden Ren backend from the ComfyUI frontend.
The backend handles AI agent interactions and WebSocket communication.

Configuration:
    Edit .env file to configure backend settings:
    - BACKEND_LAUNCH_MODE: How to launch backend (auto/terminal/subprocess/manual)
    - AUTO_START_BACKEND: Enable/disable embedded auto-start (default: false)
    - AUTO_RESTART_BACKEND: Auto-restart on crash (default: true, subprocess only)
    - WS_PORT: Backend server port (default: 8000)

Manual Backend Start:
    If you prefer to start the backend manually:
    1. Set BACKEND_LAUNCH_MODE=manual in .env
    2. Run: cd backend && python server.py

For more information, see README.md
"""

import os
import sys
import json
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# Determine backend directory
REN_ROOT = Path(__file__).parent
BACKEND_DIR = REN_ROOT / "backend"
REN_DIR = REN_ROOT / ".ren"
PID_FILE = REN_DIR / "daemon.pid"
LAUNCH_LOG = REN_ROOT / "logs" / "ren_daemon_launcher.log"

# Add backend to Python path for imports
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).parent))

# Import configuration
try:
    from backend.config import settings
    AUTO_START = settings.auto_start_backend
    AUTO_RESTART = settings.auto_restart_backend
    LOG_TO_FILE = settings.log_backend_to_file
    LAUNCH_MODE = settings.backend_launch_mode
    PORT = settings.ws_port
except ImportError as e:
    print(f"[FL_JS] Warning: Could not load config ({e}), using defaults")
    AUTO_START = True
    AUTO_RESTART = True
    LOG_TO_FILE = True
    LAUNCH_MODE = "auto"
    PORT = 8000
except Exception as e:
    print(f"[FL_JS] Warning: Error loading config ({e}), using defaults")
    AUTO_START = True
    AUTO_RESTART = True
    LOG_TO_FILE = True
    LAUNCH_MODE = "auto"
    PORT = 8000


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _request_json(url: str, timeout: float = 1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def _read_daemon_pid():
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if _pid_is_running(pid) else None


def _launcher_status():
    backend_url = f"http://127.0.0.1:{PORT}"
    ren_status = _request_json(f"{backend_url}/api/ren/status")
    health = _request_json(f"{backend_url}/health")
    pid = _read_daemon_pid()
    backend_reachable = bool(health or ren_status or _is_port_open(PORT))
    mode = (ren_status or {}).get("mode")
    return {
        "backendUrl": backend_url,
        "wsUrl": f"ws://127.0.0.1:{PORT}/ws",
        "backendReachable": backend_reachable,
        "health": health,
        "renStatus": ren_status,
        "mode": mode or ("unknown" if backend_reachable else "stopped"),
        "daemonPid": pid,
        "canStart": not backend_reachable,
        "canStop": bool(backend_reachable and (mode == "daemon" or pid)),
        "port": PORT,
    }


def _start_daemon():
    status = _launcher_status()
    if status["backendReachable"]:
        return status | {"started": False, "message": "Ren backend is already running."}

    REN_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["FL_REN_MODE"] = "daemon"
    env.pop("FL_JS_PARENT_PID", None)
    env.pop("FL_JS_MANAGED_BACKEND", None)

    with open(LAUNCH_LOG, "a", buffering=1, encoding="utf-8") as log:
        log.write(f"\n{'=' * 80}\n")
        log.write(f"Ren daemon launcher start: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"{'=' * 80}\n")
        proc = subprocess.Popen(
            [sys.executable, str(REN_ROOT / "ren_daemon.py")],
            cwd=str(REN_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
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
        "error": "Ren daemon did not become healthy before timeout.",
    }


def _stop_daemon():
    status = _launcher_status()
    backend_url = status["backendUrl"]
    ren_status = status.get("renStatus") or {}

    if status["backendReachable"] and ren_status.get("mode") == "daemon":
        try:
            request = urllib.request.Request(
                f"{backend_url}/api/ren/shutdown",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(request, timeout=1)
        except Exception:
            pass

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

    return _launcher_status() | {"stopped": False, "error": "Ren daemon did not stop before timeout."}


try:
    comfy_server_module = sys.modules.get("server")
    if comfy_server_module is None or not hasattr(comfy_server_module, "PromptServer"):
        raise RuntimeError("ComfyUI PromptServer module is not available")
    PromptServer = comfy_server_module.PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/fl_ren/launcher/status")
    async def fl_ren_launcher_status(request):
        return web.json_response(_launcher_status())

    @PromptServer.instance.routes.post("/fl_ren/launcher/start")
    async def fl_ren_launcher_start(request):
        return web.json_response(_start_daemon())

    @PromptServer.instance.routes.post("/fl_ren/launcher/stop")
    async def fl_ren_launcher_stop(request):
        return web.json_response(_stop_daemon())

    print("[FL_JS] Registered Ren launcher routes")
except Exception as e:
    print(f"[FL_JS] Warning: Could not register Ren launcher routes: {e}")

# Start backend server if enabled
if AUTO_START:
    try:
        from backend.server_runner import ServerRunner
        
        print("="*80)
        print("[FL_JS] Initializing FL_JS Agentic System...")
        print("="*80)
        
        server_runner = ServerRunner(
            backend_dir=str(BACKEND_DIR),
            port=PORT,
            launch_mode=LAUNCH_MODE,
            auto_start=True,
            auto_restart=AUTO_RESTART,
            log_to_file=LOG_TO_FILE,
        )
        
        # Keep reference to prevent garbage collection
        _FL_JS_SERVER = server_runner
        
        print("="*80)
        print("[FL_JS] Initialization complete!")
        print("="*80)
        
    except Exception as e:
        print("="*80)
        print(f"[FL_JS] Failed to start backend server: {e}")
        print("[FL_JS] You can start it manually:")
        print(f"[FL_JS]   cd {BACKEND_DIR}")
        print("[FL_JS]   python server.py")
        print("="*80)
else:
    print("="*80)
    print("[FL_JS] Backend auto-start disabled (AUTO_START_BACKEND=false)")
    print("[FL_JS] Start manually:")
    print(f"[FL_JS]   cd {BACKEND_DIR}")
    print("[FL_JS]   python server.py")
    print("="*80)

# ComfyUI Custom Node Registration
# FL_JS is an extension-only custom node (no processing nodes)

# No nodes to register (extension-only)
NODE_CLASS_MAPPINGS = {}

# Optional: Empty display names
NODE_DISPLAY_NAME_MAPPINGS = {}

# Point to JavaScript extensions
WEB_DIRECTORY = "./web/js"

# Export for ComfyUI
__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']
