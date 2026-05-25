"""FL_JS Agentic System for ComfyUI

AI-powered workflow assistant with natural language interface.

This extension automatically starts a FastAPI backend server when ComfyUI loads.
The backend handles AI agent interactions and WebSocket communication.

Configuration:
    Edit .env file to configure backend settings:
    - BACKEND_LAUNCH_MODE: How to launch backend (auto/terminal/subprocess/manual)
    - AUTO_START_BACKEND: Enable/disable auto-start (default: true)
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
from pathlib import Path

# Determine backend directory
BACKEND_DIR = Path(__file__).parent / "backend"

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
