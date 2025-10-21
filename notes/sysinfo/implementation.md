# System Info Tool - Implementation Guide

## Overview
This document contains the complete implementation for the system info MCP tool.

## Files to Create/Modify

### 1. Create: `backend/sysinfo.py`

This is a new file containing all system detection logic.

```python
"""System information utilities for FL_JS agent system.

Provides OS, Python environment, and virtual environment detection
to help agents provide platform-specific installation instructions.
"""

import sys
import os
import platform
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


def detect_venv() -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Detect if running in a virtual environment and identify its type.
    
    Returns:
        Tuple of (in_venv, venv_type, venv_path, venv_name)
        - in_venv: True if in any virtual environment
        - venv_type: "venv", "conda", "virtualenv", or None
        - venv_path: Absolute path to virtual environment root
        - venv_name: Name of the virtual environment
    """
    # Check if in any virtual environment
    in_venv = (
        hasattr(sys, 'real_prefix') or 
        (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
    )
    
    if not in_venv:
        return False, None, None, None
    
    # Determine venv type and path
    venv_type = None
    venv_path = None
    venv_name = None
    
    # Check for Conda
    if os.environ.get('CONDA_DEFAULT_ENV'):
        venv_type = "conda"
        venv_name = os.environ.get('CONDA_DEFAULT_ENV')
        venv_path = os.environ.get('CONDA_PREFIX')
    
    # Check for old-style virtualenv (has real_prefix)
    elif hasattr(sys, 'real_prefix'):
        venv_type = "virtualenv"
        venv_path = sys.prefix
        venv_name = Path(sys.prefix).name
    
    # Modern venv (Python 3.3+)
    else:
        venv_type = "venv"
        venv_path = sys.prefix
        venv_name = Path(sys.prefix).name
    
    return in_venv, venv_type, venv_path, venv_name


def get_os_info() -> Dict[str, Any]:
    """
    Get operating system information.
    
    Returns:
        Dictionary with OS details:
        - platform: "Windows", "Linux", or "Darwin"
        - platform_details: Full platform string
        - architecture: System architecture (e.g., "AMD64", "x86_64")
        - version: OS version string
    """
    return {
        "platform": platform.system(),
        "platform_details": platform.platform(),
        "architecture": platform.machine(),
        "version": platform.release()
    }


def get_python_info() -> Dict[str, Any]:
    """
    Get Python interpreter and environment information.
    
    Returns:
        Dictionary with Python details:
        - version: Version string (e.g., "3.11.5")
        - version_info: Version tuple [major, minor, micro]
        - executable: Path to Python executable
        - in_venv: True if in virtual environment
        - venv_type: Type of venv ("venv", "conda", "virtualenv", or null)
        - venv_path: Path to venv root (or null)
        - venv_name: Name of venv (or null)
    """
    in_venv, venv_type, venv_path, venv_name = detect_venv()
    
    return {
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "executable": sys.executable,
        "in_venv": in_venv,
        "venv_type": venv_type,
        "venv_path": venv_path,
        "venv_name": venv_name
    }


def get_installation_helpers(python_info: Dict[str, Any], os_info: Dict[str, Any]) -> Dict[str, str]:
    """
    Generate platform-specific installation command helpers.
    
    Args:
        python_info: Python environment info from get_python_info()
        os_info: OS info from get_os_info()
    
    Returns:
        Dictionary with installation command templates:
        - pip_command: Command to run pip
        - python_command: Command to run python
        - example_install: Example pip install command
    """
    is_windows = os_info["platform"] == "Windows"
    in_venv = python_info["in_venv"]
    venv_name = python_info.get("venv_name")
    
    # Determine command prefixes
    if in_venv and venv_name:
        if is_windows:
            pip_cmd = f"{venv_name}\\Scripts\\pip"
            python_cmd = f"{venv_name}\\Scripts\\python"
        else:
            pip_cmd = f"{venv_name}/bin/pip"
            python_cmd = f"{venv_name}/bin/python"
    else:
        # Not in venv or can't determine venv name
        if is_windows:
            pip_cmd = "pip"
            python_cmd = "python"
        else:
            pip_cmd = "pip3"
            python_cmd = "python3"
    
    return {
        "pip_command": pip_cmd,
        "python_command": python_cmd,
        "example_install": f"{pip_cmd} install package-name"
    }


def get_system_info(comfy_tools=None) -> Dict[str, Any]:
    """
    Get comprehensive system information for installation guidance.
    
    Args:
        comfy_tools: Optional ComfyUITools instance to include ComfyUI paths
    
    Returns:
        Dictionary with complete system information:
        - os: Operating system details
        - python: Python environment details
        - comfyui: ComfyUI installation paths (if comfy_tools provided)
        - installation_helpers: Platform-specific command templates
    """
    os_info = get_os_info()
    python_info = get_python_info()
    installation_helpers = get_installation_helpers(python_info, os_info)
    
    result = {
        "os": os_info,
        "python": python_info,
        "installation_helpers": installation_helpers
    }
    
    # Add ComfyUI paths if available
    if comfy_tools:
        result["comfyui"] = {
            "root": str(comfy_tools.comfyui_root),
            "custom_nodes_path": str(comfy_tools.comfyui_root / "custom_nodes"),
            "models_path": str(comfy_tools.comfyui_root / "models")
        }
    
    return result
```

---

### 2. Modify: `backend/mcp_server.py`

#### Add Import (near top of file, around line 40)

Add this import with the other module imports:

```python
from sysinfo import get_system_info as _get_system_info
```

#### Add Request Model (around line 310, in REQUEST MODELS section)

Add this after the other request models:

```python
class GetSystemInfoRequest(BaseModel):
    """Request for system information."""
    pass  # No parameters needed
```

#### Add Tool Function (around line 1250, after comfy_search_files)

Add this new tool at the end of the ComfyUI tools section:

```python
@mcp.tool()
async def get_system_info(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Get system and environment information for installation guidance.
    
    This tool provides OS, Python, and virtual environment details to help
    provide platform-specific installation instructions for ComfyUI components.
    
    USE CASES:
    - Installation Guidance: Determine correct pip/python commands for user's platform
    - Environment Detection: Check if running in venv/conda for dependency installation
    - Platform-Specific Help: Provide Windows vs Linux vs macOS specific instructions
    - ControlNet Setup: Guide users through manual model installation with correct paths
    - Dependency Installation: Show correct command syntax for user's environment
    
    RETURNED INFORMATION:
    - OS platform (Windows/Linux/Darwin) and architecture
    - Python version and executable path
    - Virtual environment status and type (venv/conda/virtualenv)
    - ComfyUI installation paths
    - Platform-specific installation command templates
    
    EXAMPLE USAGE:
    Agent: "Let me check your system to provide the right installation commands..."
    Result: "You're on Windows with a venv, use: venv\\Scripts\\pip install ..."
    
    SECURITY: Read-only system information, no modifications.
    """
    await _report_tool_activity(ctx, "get_system_info")
    
    try:
        # Get ComfyUI tools to include installation paths
        tools = get_comfy_tools()
        
        # Get comprehensive system info
        info = _get_system_info(comfy_tools=tools)
        
        return info
        
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        # Still return basic info even if ComfyUI tools fail
        try:
            info = _get_system_info(comfy_tools=None)
            info["warning"] = "ComfyUI paths unavailable"
            return info
        except Exception as e2:
            logger.error(f"Fatal error in get_system_info: {e2}")
            raise RuntimeError(f"Failed to get system information: {e2}")
```

---

## Implementation Steps

1. **Create `backend/sysinfo.py`**
   - Copy the entire code block from section 1 above
   - Save as new file `backend/sysinfo.py`

2. **Modify `backend/mcp_server.py`**
   - Add the import statement near the top
   - Add the `GetSystemInfoRequest` model in the REQUEST MODELS section
   - Add the `get_system_info` tool function after the other ComfyUI tools

3. **Test the implementation**
   - Restart the MCP server
   - Call `get_system_info` tool from agent
   - Verify all fields are populated correctly

---

## Expected Output Example

### Windows with venv:
```json
{
  "os": {
    "platform": "Windows",
    "platform_details": "Windows-10-10.0.19045-SP0",
    "architecture": "AMD64",
    "version": "10"
  },
  "python": {
    "version": "3.11.5",
    "version_info": [3, 11, 5],
    "executable": "C:\\ComfyUI\\venv\\Scripts\\python.exe",
    "in_venv": true,
    "venv_type": "venv",
    "venv_path": "C:\\ComfyUI\\venv",
    "venv_name": "venv"
  },
  "comfyui": {
    "root": "C:\\ComfyUI",
    "custom_nodes_path": "C:\\ComfyUI\\custom_nodes",
    "models_path": "C:\\ComfyUI\\models"
  },
  "installation_helpers": {
    "pip_command": "venv\\Scripts\\pip",
    "python_command": "venv\\Scripts\\python",
    "example_install": "venv\\Scripts\\pip install package-name"
  }
}
```

### Linux without venv:
```json
{
  "os": {
    "platform": "Linux",
    "platform_details": "Linux-5.15.0-91-generic-x86_64-with-glibc2.35",
    "architecture": "x86_64",
    "version": "5.15.0-91-generic"
  },
  "python": {
    "version": "3.10.12",
    "version_info": [3, 10, 12],
    "executable": "/usr/bin/python3",
    "in_venv": false,
    "venv_type": null,
    "venv_path": null,
    "venv_name": null
  },
  "comfyui": {
    "root": "/home/user/ComfyUI",
    "custom_nodes_path": "/home/user/ComfyUI/custom_nodes",
    "models_path": "/home/user/ComfyUI/models"
  },
  "installation_helpers": {
    "pip_command": "pip3",
    "python_command": "python3",
    "example_install": "pip3 install package-name"
  }
}
```

---

## Testing Checklist

- [ ] Tool appears in MCP tool list
- [ ] Tool executes without errors
- [ ] OS information is correct
- [ ] Python version is correct
- [ ] Virtual environment detection works
- [ ] ComfyUI paths are included
- [ ] Installation helpers show correct commands
- [ ] Works on Windows
- [ ] Works on Linux
- [ ] Works on macOS
- [ ] Works with venv
- [ ] Works with conda
- [ ] Works without virtual environment
- [ ] Error handling works if ComfyUI not found

---

## Future Enhancements

Potential additions for future versions:

1. **GPU Detection**
   ```python
   def get_gpu_info() -> Dict[str, Any]:
       # Detect CUDA, ROCm, MPS (Apple Silicon)
       # Return GPU model, VRAM, driver version
   ```

2. **Disk Space**
   ```python
   def get_disk_space(path: str) -> Dict[str, int]:
       # Return total, used, available space
   ```

3. **RAM Information**
   ```python
   def get_memory_info() -> Dict[str, int]:
       # Return total and available RAM
   ```

4. **Dependency Checking**
   ```python
   def check_package_installed(package: str) -> Dict[str, Any]:
       # Check if package is installed and return version
   ```

---

## Notes

- This is a **read-only** tool - it only queries system information
- No security concerns as it doesn't modify anything
- Uses standard library modules only (no external dependencies)
- Gracefully handles missing ComfyUI installation
- Works across all major platforms (Windows, Linux, macOS)
- Supports all common virtual environment types
