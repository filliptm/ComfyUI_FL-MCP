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
