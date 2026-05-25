"""Backend server subprocess manager for FL_JS.

Handles automatic startup, monitoring, and cleanup of the FastAPI backend server.
Supports multiple launch modes: terminal window, subprocess, or manual.
"""

import sys
import os
import subprocess
import atexit
import socket
import time
import threading
import logging
from typing import Optional, Literal
from pathlib import Path


class ServerRunner:
    """Manages FL_JS FastAPI backend server.
    
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
        
        # Track which mode was actually used
        self.active_mode: Optional[str] = None
        
        # Subprocess tracking (only used in subprocess mode)
        self.process: Optional[subprocess.Popen] = None
        self.log_file_handle: Optional[object] = None
        self._cleaned_up = False
        self._should_monitor = False
        self._monitor_thread: Optional[threading.Thread] = None
        
        # Setup logging
        self.logger = logging.getLogger("FL_JS.ServerRunner")
        
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
            
            log_file = log_dir / "server.log"
            
            # Open in append mode with line buffering
            handle = open(log_file, "a", buffering=1, encoding="utf-8")
            
            # Write startup marker
            handle.write(f"\n{'='*80}\n")
            handle.write(f"FL_JS Backend Server Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            handle.write(f"{'='*80}\n\n")
            handle.flush()
            
            print(f"[FL_JS] Server logs: {log_file}")
            return handle
        
        except Exception as e:
            print(f"[FL_JS] Warning: Could not setup log file: {e}")
            return None
    
    def start(self) -> bool:
        """Start the FastAPI backend server.
        
        Returns:
            True if started successfully, False otherwise
        """
        # Check if already running (subprocess mode)
        if self.process is not None:
            print("[FL_JS] Backend already running (subprocess)")
            return True
        
        # Check port availability
        if self.is_port_in_use():
            print(f"[FL_JS] Port {self.port} already in use.")
            print(f"[FL_JS] If you're running the backend manually, this is OK.")
            print(f"[FL_JS] Otherwise, change WS_PORT in .env or stop the conflicting service.")
            return False
        
        # Determine launch method
        if self.launch_mode == "manual":
            print("[FL_JS] Manual launch mode - not starting backend")
            print("[FL_JS] To start manually: cd backend && python server.py")
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
            print(f"[FL_JS] Unknown launch mode: {self.launch_mode}")
            return False
    
    def _launch_in_terminal(self, fallback: bool = True) -> bool:
        """Launch backend in separate terminal window.
        
        Args:
            fallback: If True, fallback to subprocess on failure
        
        Returns:
            True if started successfully, False otherwise
        """
        print(f"[FL_JS] Attempting to launch backend in terminal window...")
        
        # Import here to avoid import errors if module doesn't exist
        try:
            from backend.terminal_launcher import TerminalLauncher
        except ImportError as e:
            print(f"[FL_JS] Terminal launcher not available: {e}")
            if fallback:
                print("[FL_JS] Falling back to subprocess mode...")
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
            print(f"[FL_JS] {message}")
            print(f"[FL_JS] Backend starting on port {self.port}...")
            print(f"[FL_JS] Check the terminal window for logs")
            print(f"[FL_JS] Close the terminal window to stop the backend")
            
            self.active_mode = "terminal"
            
            # Wait for server to be ready
            if self.wait_for_server(timeout=15):
                print(f"[FL_JS] Backend server started successfully!")
                return True
            else:
                print("[FL_JS] Backend server failed to start (timeout)")
                print("[FL_JS] Check the terminal window for errors")
                return False
        
        else:
            print(f"[FL_JS] Terminal launch failed: {message}")
            
            if fallback:
                print("[FL_JS] Falling back to subprocess mode...")
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
                print(f"[FL_JS] Error: server.py not found at {server_script}")
                print(f"[FL_JS] Backend directory: {self.backend_dir}")
                return False
            
            print(f"[FL_JS] Starting backend server (subprocess mode) on port {self.port}...")
            
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
            child_env["FL_JS_PARENT_PID"] = str(os.getpid())
            child_env["FL_JS_MANAGED_BACKEND"] = "1"

            self.process = subprocess.Popen(
                [python_exe, "-u", str(server_script)],  # -u for unbuffered output
                cwd=str(self.backend_dir),
                env=child_env,
                stdout=stdout_dest,
                stderr=stderr_dest,
                bufsize=1,  # Line buffered
                universal_newlines=True,
            )
            
            self.active_mode = "subprocess"
            
            # If logging to file, start output capture thread
            if self.log_file_handle and self.process.stdout:
                self._start_output_capture()
            
            # Wait for server to be ready
            if self.wait_for_server(timeout=15):
                print(f"[FL_JS] Backend server started successfully! (PID: {self.process.pid})")
                
                # Start monitoring thread if auto-restart enabled
                if self.auto_restart:
                    self._start_monitoring()
                
                return True
            else:
                print("[FL_JS] Backend server failed to start (timeout)")
                print("[FL_JS] Check backend/logs/server.log for errors")
                self.cleanup()
                return False
        
        except Exception as e:
            print(f"[FL_JS] Failed to start backend server: {e}")
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
                    print(f"[FL_JS Backend] {line.rstrip()}")
            
            except Exception as e:
                print(f"[FL_JS] Output capture error: {e}")
        
        output_thread = threading.Thread(target=capture_output, daemon=True)
        output_thread.start()
    
    def _start_monitoring(self):
        """Start monitoring thread for auto-restart."""
        self._should_monitor = True
        self._monitor_thread = threading.Thread(target=self._monitor_process, daemon=True)
        self._monitor_thread.start()
        print("[FL_JS] Auto-restart monitoring enabled")
    
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
                print(f"[FL_JS] Backend process terminated unexpectedly (exit code: {return_code})")
                
                # Check restart rate limiting
                current_time = time.time()
                restart_times = [t for t in restart_times if current_time - t < restart_window]
                
                if len(restart_times) >= max_restarts:
                    print(f"[FL_JS] Too many restarts ({max_restarts} in {restart_window}s). Giving up.")
                    print("[FL_JS] Please check backend/logs/server.log for errors.")
                    print("[FL_JS] Restart ComfyUI to try again.")
                    self._should_monitor = False
                    break
                
                # Attempt restart
                restart_times.append(current_time)
                restart_count += 1
                
                print(f"[FL_JS] Attempting restart ({restart_count})...")
                
                # Reset process reference
                self.process = None
                
                # Wait a bit before restarting
                time.sleep(2)
                
                # Restart
                if not self._launch_as_subprocess():
                    print("[FL_JS] Restart failed. Will retry on next check.")
                else:
                    print("[FL_JS] Backend restarted successfully")
    
    def wait_for_server(self, timeout: int = 15) -> bool:
        """Wait for server to become available.
        
        Args:
            timeout: Maximum seconds to wait
        
        Returns:
            True if server is ready, False if timeout
        """
        start_time = time.time()
        
        print("[FL_JS] Waiting for backend to be ready...", end="", flush=True)
        
        while time.time() - start_time < timeout:
            if self.is_port_in_use():
                # Port is open, give it a moment to fully initialize
                time.sleep(0.5)
                print(" Ready!")
                return True
            
            # Check if process crashed during startup (subprocess mode only)
            if self.active_mode == "subprocess" and self.process and self.process.poll() is not None:
                print(" Failed!")
                print(f"[FL_JS] Process terminated during startup (exit code: {self.process.poll()})")
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
                print(f"[FL_JS] Terminating backend server (PID: {self.process.pid})...")
                
                # Try graceful termination first
                self.process.terminate()
                
                # Wait up to 5 seconds for graceful shutdown
                try:
                    self.process.wait(timeout=5)
                    print("[FL_JS] Backend server terminated gracefully")
                except subprocess.TimeoutExpired:
                    print("[FL_JS] Backend server did not terminate, killing...")
                    self.process.kill()
                    self.process.wait()
                    print("[FL_JS] Backend server killed")
            
            except Exception as e:
                print(f"[FL_JS] Error during cleanup: {e}")
            
            finally:
                self.process = None
        
        elif self.active_mode == "terminal":
            # No cleanup needed for terminal mode
            print("[FL_JS] Terminal mode - no cleanup needed")
            print("[FL_JS] Close the terminal window to stop the backend")
        
        # Close log file (subprocess mode only)
        if self.log_file_handle:
            try:
                self.log_file_handle.write(f"\n{'='*80}\n")
                self.log_file_handle.write(f"FL_JS Backend Server Stopped: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                self.log_file_handle.write(f"{'='*80}\n\n")
                self.log_file_handle.close()
            except Exception:
                pass
            finally:
                self.log_file_handle = None
    
    def __del__(self):
        """Destructor: ensure cleanup on garbage collection."""
        self.cleanup()
