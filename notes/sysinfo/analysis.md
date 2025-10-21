# System Info Tool - Analysis

## Overview
Create a new MCP tool that provides OS and Python environment information to help ComfyUI users get platform-specific installation instructions, especially for ControlNet models and other manual installations.

## Use Case
ComfyUI users are generally knowledgeable about their systems, but the agent needs to:
- Detect the operating system (Windows, Linux, macOS)
- Identify if running in a virtual environment
- Provide correct installation commands based on the environment

## Design Goals

### Information to Provide

1. **Operating System**
   - Platform name (Windows, Linux, Darwin/macOS)
   - Platform details (version info)
   - Architecture (x86_64, arm64, etc.)

2. **Python Environment**
   - Python version
   - Python executable path
   - Virtual environment status
   - Virtual environment type (venv, conda, virtualenv)
   - Virtual environment path

3. **ComfyUI Context**
   - ComfyUI root directory
   - Custom nodes path
   - Models directory paths

4. **Installation Helpers**
   - Suggested pip command prefix based on environment
   - Suggested installation path for models

## Technical Approach

### Module Structure
- **New file**: `backend/sysinfo.py` - Core system detection logic
- **Modified file**: `backend/mcp_server.py` - MCP tool registration

### Python Modules Needed
```python
import sys
import os
import platform
from pathlib import Path
```

### Virtual Environment Detection Strategy

```python
# Check if in venv
in_venv = hasattr(sys, 'real_prefix') or (
    hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
)

# Detect venv type
venv_type = None
if in_venv:
    if os.environ.get('CONDA_DEFAULT_ENV'):
        venv_type = "conda"
    elif hasattr(sys, 'real_prefix'):
        venv_type = "virtualenv"  # Old virtualenv
    else:
        venv_type = "venv"  # Python 3.3+ venv
```

### Response Schema

```json
{
    "os": {
        "platform": "Windows" | "Linux" | "Darwin",
        "platform_details": "Windows-10-10.0.19045-SP0",
        "architecture": "AMD64",
        "version": "10.0.19045"
    },
    "python": {
        "version": "3.11.5",
        "version_info": [3, 11, 5],
        "executable": "C:\\ComfyUI\\venv\\Scripts\\python.exe",
        "in_venv": true,
        "venv_path": "C:\\ComfyUI\\venv",
        "venv_type": "venv",
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

## Implementation Plan

### Phase 1: Create `backend/sysinfo.py`
- Implement OS detection functions
- Implement venv detection functions
- Create main `get_system_info()` function
- Return structured dictionary

### Phase 2: Register MCP Tool in `backend/mcp_server.py`
- Add Pydantic request model (empty - no parameters needed)
- Add Pydantic response model
- Register tool with `@mcp.tool()`
- Use `_report_tool_activity()` for logging (Python-only, no WebSocket)

### Phase 3: Testing Considerations
- Test on Windows with venv
- Test on Linux with venv
- Test on macOS
- Test with conda environments
- Test without virtual environment

## Additional Features (Future)

1. **GPU Detection**
   - CUDA availability and version
   - ROCm availability
   - GPU model information

2. **Disk Space**
   - Available space in ComfyUI directory
   - Available space in models directory

3. **RAM Information**
   - Total system RAM
   - Available RAM

4. **Dependency Checking**
   - Check if specific packages are installed
   - Version checking for critical dependencies

## Security Considerations
- Only read system information, never modify
- Don't expose sensitive environment variables
- Don't expose full PATH or other security-sensitive data
- Sanitize paths in output

## Integration with Existing Code

### Leveraging Existing Patterns
- Use similar structure to `comfy_tools.py` for organization
- Follow same error handling patterns
- Use same logging approach
- Integrate with existing `ComfyUITools` instance for ComfyUI paths

### Tool Registration Pattern
```python
# Similar to other tools in mcp_server.py
@mcp.tool()
async def get_system_info(
    ctx: Context
) -> Dict[str, Any]:
    """Get system and environment information for installation guidance."""
    await _report_tool_activity("get_system_info", {})
    
    from sysinfo import get_system_info as _get_sys_info
    return _get_sys_info(comfy_tools)
```

## Next Steps
1. Investigate existing codebase patterns more deeply
2. Create detailed implementation document with full code
3. Implement and test
