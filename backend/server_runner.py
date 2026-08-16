"""Backend server subprocess manager for ComfyUI FL-MCP.

Handles automatic startup, monitoring, and cleanup of the FastAPI backend server.
Supports multiple launch modes: terminal window, subprocess, or manual.
"""

import atexit
import http.client
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal, Optional

from process_utils import managed_process_kwargs
from version import (
    RUNTIME_PRODUCT,
    __version__,
    runtime_build_identity,
    runtime_project_identity,
)


class ServerRunner:
    """Manages the ComfyUI FL-MCP FastAPI bridge server.
    
    Features:
    - Multiple launch modes (terminal/subprocess/manual)
    - Automatic startup when ComfyUI loads
    - Port conflict detection
    - Health check with timeout
    - Auto-restart on crash (subprocess mode)
    - Graceful shutdown on exit
    - Dual logging (file + stdout)
    """
    
    def __init__(
        self,
        backend_dir: str,
        port: int = 8000,
        launch_mode: Literal["auto", "terminal", "subprocess", "manual"] = "auto",
        auto_start: bool = True,
        auto_restart: bool = True,
        log_to_file: bool = True,
    ):
        """Initialize server runner.
        
        Args:
            backend_dir: Path to backend directory containing server.py
            port: Port to run server on
            launch_mode: How to launch backend (auto/terminal/subprocess/manual)
            auto_start: Whether to start server immediately
            auto_restart: Whether to restart server if it crashes (subprocess only)
            log_to_file: Whether to log server output to file (subprocess only)
        """
        self.backend_dir = Path(backend_dir).resolve()
        self.port = port
        self.launch_mode = launch_mode
        self.auto_restart = auto_restart
        self.log_to_file = log_to_file
        project_root = self.backend_dir.parent
        self.runtime_identity = {
            "product": RUNTIME_PRODUCT,
            "project_id": runtime_project_identity(project_root),
            "build_id": runtime_build_identity(project_root),
            "version": __version__,
        }
        
        # Track which mode was actually used
        self.active_mode: Optional[str] = None
        
        # Subprocess tracking (only used in subprocess mode)
        self.process: Optional[subprocess.Popen] = None
        self.log_file_handle: Optional[object] = None
        self._cleaned_up = False
        self._should_monitor = False
        self._monitor_thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        
        # Setup logging
        self.logger = logging.getLogger("FL-MCP.ServerRunner")
        
        # Register cleanup handlers (only needed for subprocess mode)
        atexit.register(self.cleanup)
        
        if auto_start and launch_mode != "manual":
            self.start()
    
    def is_port_in_use(self) -> bool:
        """Check if the port is already in use.
        
        Returns:
            True if port is in use, False otherwise
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('127.0.0.1', self.port)) == 0

    def _request_json(self, method: str, path: str) -> Optional[dict]:
        connection = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
            if method == "POST":
                connection.request(
                    method,
                    path,
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
            else:
                connection.request(method, path)
            response = connection.getresponse()
            if response.status != 200:
                response.read()
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
            return None
        finally:
            if connection is not None:
                connection.close()

    def _local_daemon_pid(self) -> Optional[int]:
        try:
            return int(
                self._daemon_pid_path().read_text(encoding="utf-8").strip()
            )
        except (OSError, ValueError):
            return None

    def _daemon_pid_path(self) -> Path:
        return self.backend_dir.parent / ".fl_mcp" / "daemon.pid"

    def _remove_daemon_pid_file(self) -> None:
        try:
            self._daemon_pid_path().unlink(missing_ok=True)
        except OSError as exc:
            print(f"[FL-MCP] Warning: could not remove stale daemon PID file: {exc}")

    def _probe_backend(self) -> dict:
        health = self._request_json("GET", "/health") or {}
        status = self._request_json("GET", "/api/mcp/status") or {}
        runtime = status.get("runtime") or health.get("runtime") or {}
        if not isinstance(runtime, dict):
            runtime = {}

        health_matches = (
            health.get("status") == "healthy"
            and "active_connections" in health
        )
        status_matches = (
            status.get("healthy") is True
            and status.get("port") == self.port
        )
        product_matches = runtime.get("product") == RUNTIME_PRODUCT
        mode = str(runtime.get("mode") or status.get("mode") or "").lower()
        remote_pid = runtime.get("pid") or status.get("pid")
        legacy_same_project = bool(
            not runtime
            and health_matches
            and status_matches
            and mode == "daemon"
            and isinstance(remote_pid, int)
            and remote_pid == self._local_daemon_pid()
        )
        same_project = bool(
            product_matches
            and runtime.get("project_id") == self.runtime_identity["project_id"]
        ) or legacy_same_project
        identity_matches = bool(
            product_matches
            and same_project
            and runtime.get("build_id") == self.runtime_identity["build_id"]
            and runtime.get("version") == self.runtime_identity["version"]
        )

        return {
            "health": health,
            "status": status,
            "runtime": runtime,
            "is_fl_mcp": product_matches or (health_matches and status_matches),
            "same_project": same_project,
            "identity_matches": identity_matches,
            "reusable": bool((health_matches or status_matches) and identity_matches),
            "mode": mode,
        }

    def is_fl_mcp_backend(self) -> bool:
        """Return whether the listening backend is safe to reuse."""
        return bool(self._probe_backend()["reusable"])

    def _wait_for_port_to_close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_port_in_use():
                return True
            time.sleep(0.1)
        return not self.is_port_in_use()

    def _shutdown_stale_daemon(self) -> bool:
        response = self._request_json("POST", "/api/mcp/shutdown")
        if response is not None and response.get("success") is not True:
            self.last_error = "The stale FL-MCP daemon refused the shutdown request."
            return False
        if self._wait_for_port_to_close():
            self._remove_daemon_pid_file()
            return True
        self.last_error = (
            f"The stale FL-MCP daemon did not release port {self.port} after shutdown."
        )
        return False
    
    def _setup_log_file(self) -> Optional[object]:
        """Setup log file for server output.
        
        Returns:
            File handle or None if logging disabled
        """
        if not self.log_to_file:
            return None
        
        try:
            log_dir = self.backend_dir / "logs"
            log_dir.mkdir(exist_ok=True)
            
            log_file = log_dir / "fl_mcp_server.log"
            
            # Open in append mode with line buffering
            handle = open(log_file, "a", buffering=1, encoding="utf-8")
            
            # Write startup marker
            handle.write(f"\n{'='*80}\n")
            handle.write(f"FL-MCP Backend Server Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            handle.write(f"{'='*80}\n\n")
            handle.flush()
            
            print(f"[FL-MCP] Server logs: {log_file}")
            return handle
        
        except Exception as e:
            print(f"[FL-MCP] Warning: Could not setup log file: {e}")
            return None
    
    def start(self) -> bool:
        """Start the FastAPI backend server.
        
        Returns:
            True if started successfully, False otherwise
        """
        self.last_error = None

        # Check if already running (subprocess mode)
        if self.process is not None:
            print("[FL-MCP] Backend already running (subprocess)")
            return True
        
        # Check port availability
        if self.is_port_in_use():
            probe = self._probe_backend()
            if probe["reusable"]:
                print(f"[FL-MCP] Reusing existing backend on port {self.port}.")
                self.active_mode = "external"
                return True
            if (
                probe["is_fl_mcp"]
                and probe["same_project"]
                and probe["mode"] in {"daemon", "embedded"}
                and self.launch_mode in {"auto", "subprocess"}
            ):
                # "embedded" is what a subprocess-launched backend reports when
                # FL_MCP_MODE is unset (the common case). Without this, a stale
                # embedded-mode backend left running by a ComfyUI restart that
                # didn't actually kill its child process is never replaced -
                # start() just logs an error and gives up, silently serving
                # stale code indefinitely.
                print(f"[FL-MCP] Replacing stale {probe['mode']} backend on port {self.port}.")
                if not self._shutdown_stale_daemon():
                    print(f"[FL-MCP] {self.last_error}")
                    return False
            else:
                if probe["is_fl_mcp"] and probe["same_project"]:
                    self.last_error = (
                        f"A stale FL-MCP backend is using port {self.port}, but launch mode "
                        f"'{self.launch_mode}' cannot replace it safely. Stop that backend or "
                        "use auto/subprocess launch mode."
                    )
                elif probe["is_fl_mcp"]:
                    self.last_error = (
                        f"Port {self.port} is occupied by an FL-MCP backend from another "
                        "checkout. It was left untouched; choose another bridge port."
                    )
                else:
                    self.last_error = (
                        f"Port {self.port} is occupied by another service. "
                        "Choose another bridge port and restart ComfyUI."
                    )
                print(f"[FL-MCP] {self.last_error}")
                return False
        
        # Determine launch method
        if self.launch_mode == "manual":
            print("[FL-MCP] Manual launch mode - not starting backend")
            print("[FL-MCP] To start manually: cd backend && python server.py")
            return False
        
        elif self.launch_mode == "terminal":
            return self._launch_in_terminal(fallback=False)
        
        elif self.launch_mode == "subprocess":
            return self._launch_as_subprocess()
        
        elif self.launch_mode == "auto":
            # Default to hidden subprocess mode. Terminal popups are now an
            # explicit opt-in via BACKEND_LAUNCH_MODE=terminal.
            return self._launch_as_subprocess()
        
        else:
            print(f"[FL-MCP] Unknown launch mode: {self.launch_mode}")
            self.last_error = f"Unknown backend launch mode: {self.launch_mode}."
            return False
    
    def _launch_in_terminal(self, fallback: bool = True) -> bool:
        """Launch backend in separate terminal window.
        
        Args:
            fallback: If True, fallback to subprocess on failure
        
        Returns:
            True if started successfully, False otherwise
        """
        print("[FL-MCP] Attempting to launch backend in terminal window...")
        
        # Import here to avoid import errors if module doesn't exist
        try:
            from backend.terminal_launcher import TerminalLauncher
        except ImportError as e:
            print(f"[FL-MCP] Terminal launcher not available: {e}")
            if fallback:
                print("[FL-MCP] Falling back to subprocess mode...")
                return self._launch_as_subprocess()
            return False
        
        # Create launcher
        launcher = TerminalLauncher(
            backend_dir=self.backend_dir,
            python_exe=sys.executable,
            port=self.port,
        )
        
        # Try to launch
        success, message = launcher.launch()
        
        if success:
            print(f"[FL-MCP] {message}")
            print(f"[FL-MCP] Backend starting on port {self.port}...")
            print("[FL-MCP] Check the terminal window for logs")
            print("[FL-MCP] Close the terminal window to stop the backend")
            
            self.active_mode = "terminal"
            
            # Wait for server to be ready
            if self.wait_for_server(timeout=15):
                print("[FL-MCP] Backend server started successfully!")
                return True
            else:
                print("[FL-MCP] Backend server failed to start (timeout)")
                print("[FL-MCP] Check the terminal window for errors")
                return False
        
        else:
            print(f"[FL-MCP] Terminal launch failed: {message}")
            
            if fallback:
                print("[FL-MCP] Falling back to subprocess mode...")
                return self._launch_as_subprocess()
            else:
                return False
    
    def _launch_as_subprocess(self) -> bool:
        """Launch backend as managed subprocess.
        
        Returns:
            True if started successfully, False otherwise
        """
        try:
            # Use same Python as ComfyUI
            python_exe = sys.executable
            server_script = self.backend_dir / "server.py"
            
            if not server_script.exists():
                print(f"[FL-MCP] Error: server.py not found at {server_script}")
                print(f"[FL-MCP] Backend directory: {self.backend_dir}")
                self.last_error = f"Backend entry point was not found: {server_script}"
                return False
            
            print(f"[FL-MCP] Starting backend server (subprocess mode) on port {self.port}...")
            
            # Setup log file
            self.log_file_handle = self._setup_log_file()
            
            # Determine stdout/stderr
            if self.log_file_handle:
                # Dual output: file + inherited stdout
                stdout_dest = subprocess.PIPE
                stderr_dest = subprocess.STDOUT
            else:
                # Just inherit stdout/stderr
                stdout_dest = None
                stderr_dest = None
            
            # Start subprocess
            child_env = os.environ.copy()
            child_env["FL_MCP_PARENT_PID"] = str(os.getpid())
            child_env["FL_MCP_MANAGED_BACKEND"] = "1"

            self.process = subprocess.Popen(
                [python_exe, "-u", str(server_script)],  # -u for unbuffered output
                cwd=str(self.backend_dir),
                env=child_env,
                stdout=stdout_dest,
                stderr=stderr_dest,
                bufsize=1,  # Line buffered
                universal_newlines=True,
                **managed_process_kwargs(),
            )
            
            self.active_mode = "subprocess"
            
            # If logging to file, start output capture thread
            if self.log_file_handle and self.process.stdout:
                self._start_output_capture()
            
            # Wait for server to be ready
            if self.wait_for_server(timeout=15):
                print(f"[FL-MCP] Backend server started successfully! (PID: {self.process.pid})")
                
                # Start monitoring thread if auto-restart enabled
                if self.auto_restart:
                    self._start_monitoring()
                
                return True
            else:
                print("[FL-MCP] Backend server failed to start (timeout)")
                print("[FL-MCP] Check backend/logs/fl_mcp_server.log for errors")
                if not self.last_error:
                    self.last_error = (
                        "Backend did not become ready before the startup timeout. "
                        f"Check {self.backend_dir / 'logs' / 'fl_mcp_server.log'}."
                    )
                self.cleanup()
                return False
        
        except Exception as e:
            print(f"[FL-MCP] Failed to start backend server: {e}")
            self.last_error = f"Backend failed to start: {e}"
            self.cleanup()
            return False
    
    def _start_output_capture(self):
        """Start thread to capture and duplicate subprocess output."""
        def capture_output():
            """Capture subprocess output and write to both file and stdout."""
            try:
                if not self.process or not self.process.stdout:
                    return
                
                for line in iter(self.process.stdout.readline, ''):
                    if not line:
                        break
                    
                    # Write to log file
                    if self.log_file_handle:
                        try:
                            self.log_file_handle.write(line)
                            self.log_file_handle.flush()
                        except Exception:
                            pass
                    
                    # Write to stdout (ComfyUI console)
                    print(f"[FL-MCP Backend] {line.rstrip()}")
            
            except Exception as e:
                print(f"[FL-MCP] Output capture error: {e}")
        
        output_thread = threading.Thread(target=capture_output, daemon=True)
        output_thread.start()
    
    def _start_monitoring(self):
        """Start monitoring thread for auto-restart."""
        self._should_monitor = True
        self._monitor_thread = threading.Thread(target=self._monitor_process, daemon=True)
        self._monitor_thread.start()
        print("[FL-MCP] Auto-restart monitoring enabled")
    
    def _monitor_process(self):
        """Monitor process and restart if it crashes."""
        restart_count = 0
        max_restarts = 5
        restart_window = 60  # seconds
        restart_times = []
        
        while self._should_monitor and not self._cleaned_up:
            time.sleep(2)  # Check every 2 seconds
            
            if self.process is None:
                continue
            
            # Check if process has terminated
            return_code = self.process.poll()
            
            if return_code is not None:
                # Process has terminated
                print(f"[FL-MCP] Backend process terminated unexpectedly (exit code: {return_code})")
                
                # Check restart rate limiting
                current_time = time.time()
                restart_times = [t for t in restart_times if current_time - t < restart_window]
                
                if len(restart_times) >= max_restarts:
                    print(f"[FL-MCP] Too many restarts ({max_restarts} in {restart_window}s). Giving up.")
                    print("[FL-MCP] Please check backend/logs/fl_mcp_server.log for errors.")
                    print("[FL-MCP] Restart ComfyUI to try again.")
                    self._should_monitor = False
                    break
                
                # Attempt restart
                restart_times.append(current_time)
                restart_count += 1
                
                print(f"[FL-MCP] Attempting restart ({restart_count})...")
                
                # Reset process reference
                self.process = None
                
                # Wait a bit before restarting
                time.sleep(2)
                
                # Restart
                if not self._launch_as_subprocess():
                    print("[FL-MCP] Restart failed. Will retry on next check.")
                else:
                    print("[FL-MCP] Backend restarted successfully")
    
    def wait_for_server(self, timeout: int = 15) -> bool:
        """Wait for server to become available.
        
        Args:
            timeout: Maximum seconds to wait
        
        Returns:
            True if server is ready, False if timeout
        """
        start_time = time.time()
        
        print("[FL-MCP] Waiting for backend to be ready...", end="", flush=True)
        
        while time.time() - start_time < timeout:
            if self.is_port_in_use():
                # Port is open, give it a moment to fully initialize
                time.sleep(0.5)
                print(" Ready!")
                return True
            
            # Check if process crashed during startup (subprocess mode only)
            if self.active_mode == "subprocess" and self.process and self.process.poll() is not None:
                print(" Failed!")
                return_code = self.process.poll()
                print(f"[FL-MCP] Process terminated during startup (exit code: {return_code})")
                self.last_error = (
                    f"Backend exited during startup with code {return_code}. "
                    f"Check {self.backend_dir / 'logs' / 'fl_mcp_server.log'}."
                )
                return False
            
            time.sleep(0.5)
            print(".", end="", flush=True)
        
        print(" Timeout!")
        return False
    
    def cleanup(self):
        """Terminate the backend server process (subprocess mode only)."""
        if self._cleaned_up:
            return
        
        self._cleaned_up = True
        
        # Stop monitoring
        self._should_monitor = False
        
        # Only cleanup subprocess if we launched in subprocess mode
        if self.active_mode == "subprocess" and self.process is not None:
            try:
                print(f"[FL-MCP] Terminating backend server (PID: {self.process.pid})...")
                
                # Try graceful termination first
                self.process.terminate()
                
                # Wait up to 5 seconds for graceful shutdown
                try:
                    self.process.wait(timeout=5)
                    print("[FL-MCP] Backend server terminated gracefully")
                except subprocess.TimeoutExpired:
                    print("[FL-MCP] Backend server did not terminate, killing...")
                    self.process.kill()
                    self.process.wait()
                    print("[FL-MCP] Backend server killed")
            
            except Exception as e:
                print(f"[FL-MCP] Error during cleanup: {e}")
            
            finally:
                self.process = None
        
        elif self.active_mode == "terminal":
            # No cleanup needed for terminal mode
            print("[FL-MCP] Terminal mode - no cleanup needed")
            print("[FL-MCP] Close the terminal window to stop the backend")
        
        # Close log file (subprocess mode only)
        if self.log_file_handle:
            try:
                self.log_file_handle.write(f"\n{'='*80}\n")
                self.log_file_handle.write(f"FL-MCP Backend Server Stopped: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                self.log_file_handle.write(f"{'='*80}\n\n")
                self.log_file_handle.close()
            except Exception:
                pass
            finally:
                self.log_file_handle = None
    
    def __del__(self):
        """Destructor: ensure cleanup on garbage collection."""
        self.cleanup()
