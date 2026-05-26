"""ComfyUI process supervisor used by Ren daemon mode."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional


REN_ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = Path(__file__).resolve().parents[3]
REN_DIR = REN_ROOT / ".ren"
CONFIG_PATH = REN_DIR / "config.json"


class ComfySupervisor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._logs: Deque[str] = deque(maxlen=2000)
        self._reader_threads: List[threading.Thread] = []
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        defaults = {
            "comfy_root": str(COMFY_ROOT),
            "python": str(COMFY_ROOT / "venv" / "bin" / "python"),
            "args": ["main.py", "--cpu", "--disable-auto-launch"],
            "host": "127.0.0.1",
            "port": 8188,
        }
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    defaults.update(loaded)
            except Exception as exc:
                self._append_log(f"Failed to read Ren daemon config: {exc}")
        return defaults

    def save_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.config.update({k: v for k, v in config.items() if v is not None})
            REN_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
            return dict(self.config)

    def _append_log(self, line: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._logs.append(f"{timestamp} {line.rstrip()}")

    def _reader(self, pipe: Any, label: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break
                self._append_log(f"[{label}] {line}")
        except Exception as exc:
            self._append_log(f"[{label}] log reader failed: {exc}")

    def _health_url(self) -> str:
        return f"http://{self.config.get('host', '127.0.0.1')}:{int(self.config.get('port', 8188))}/"

    def external_health(self) -> Dict[str, Any]:
        url = self._health_url()
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return {
                    "reachable": 200 <= response.status < 500,
                    "statusCode": response.status,
                    "url": url,
                }
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            return {"reachable": False, "error": str(exc), "url": url}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            process = self._process
            return_code = process.poll() if process else None
            managed_running = bool(process and return_code is None)
            health = self.external_health()
            mode = os.getenv("FL_REN_MODE", "embedded")
            return {
                "mode": mode,
                "canManageProcess": mode == "daemon" or self._process is not None,
                "managed": process is not None,
                "managedRunning": managed_running,
                "pid": process.pid if process else None,
                "returnCode": return_code,
                "reachable": health.get("reachable", False),
                "health": health,
                "config": dict(self.config),
            }

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if os.getenv("FL_REN_MODE", "embedded") != "daemon" and self._process is None:
                status = self.status()
                status.update({
                    "success": False,
                    "error": "Starting ComfyUI requires Ren daemon mode.",
                })
                return status
            if self._process and self._process.poll() is None:
                return self.status()

            comfy_root = Path(str(self.config["comfy_root"])).expanduser()
            python = Path(str(self.config["python"])).expanduser()
            if not python.exists():
                python = Path(sys.executable)
            args = [str(python), *[str(arg) for arg in self.config.get("args", [])]]
            env = os.environ.copy()
            env["FL_REN_MODE"] = "daemon-child"

            self._append_log(f"Starting ComfyUI: {' '.join(args)}")
            self._process = subprocess.Popen(
                args,
                cwd=str(comfy_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            self._reader_threads = []
            if self._process.stdout:
                thread = threading.Thread(
                    target=self._reader,
                    args=(self._process.stdout, "stdout"),
                    daemon=True,
                )
                thread.start()
                self._reader_threads.append(thread)
            if self._process.stderr:
                thread = threading.Thread(
                    target=self._reader,
                    args=(self._process.stderr, "stderr"),
                    daemon=True,
                )
                thread.start()
                self._reader_threads.append(thread)
            return self.status()

    def stop(self, timeout: float = 20.0) -> Dict[str, Any]:
        with self._lock:
            process = self._process
            if os.getenv("FL_REN_MODE", "embedded") != "daemon" and process is None:
                status = self.status()
                status.update({
                    "success": False,
                    "error": "Stopping ComfyUI requires Ren daemon mode.",
                })
                return status
            if not process or process.poll() is not None:
                return self.status()

            self._append_log(f"Stopping ComfyUI PID {process.pid}")
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._append_log(f"Force killing ComfyUI PID {process.pid}")
                process.kill()
                process.wait(timeout=5)
            return self.status()

    def restart(self) -> Dict[str, Any]:
        if os.getenv("FL_REN_MODE", "embedded") != "daemon" and self._process is None:
            status = self.status()
            status.update({
                "success": False,
                "error": "Restarting ComfyUI requires Ren daemon mode.",
            })
            return status
        self.stop()
        return self.start()

    def logs(self, limit: int = 300) -> Dict[str, Any]:
        with self._lock:
            safe_limit = max(1, min(int(limit), 2000))
            return {"lines": list(self._logs)[-safe_limit:]}


comfy_supervisor = ComfySupervisor()
