# ComfyUI Manager Integration Research

**Date:** 2025-10-20  
**Project:** fl_js  
**Goal:** Add ComfyUI Manager capabilities to backend/mcp_server.py

---

## Executive Summary

This research document explores integrating ComfyUI Manager's REST API capabilities into our MCP server toolset. The goal is to expose manager search, installation status checking, and other manager-related operations as MCP tools.

---

## 1. ComfyUI Manager Overview

### What is ComfyUI Manager?

ComfyUI Manager is a comprehensive extension management system for ComfyUI that provides:
- **Custom node discovery and installation**
- **Model management**
- **Update checking and version control**
- **REST API endpoints** for programmatic access
- **Node pack metadata and search**

### Location
- **Repository:** Likely at `custom_nodes/ComfyUI-Manager/` in ComfyUI installation
- **Main Server File:** `notes/manager/custom_nodes/ComfyUI-Manager/glob/manager_server.py`
- **Core Logic:** `manager_core.py` (referenced but not in our notes)

---

## 2. ComfyUI Manager REST API

### API Architecture

The Manager extends ComfyUI's PromptServer with additional routes:

```python
from server import PromptServer
routes = PromptServer.instance.routes

@routes.get("/endpoint")
async def handler(request):
    # handler logic
```

This means the Manager API is **accessible through the same server as ComfyUI** (typically `http://localhost:8188`).

### Key API Endpoints Discovered

#### Node Pack Discovery

1. **`GET /customnode/getlist`**
   - Returns unified custom node list
   - Query params: `mode` (local/remote/cache)
   - Response: JSON with node pack metadata, versions, installation status
   - Includes: name, description, files, repository, installed status, updates available

2. **`GET /customnode/getmappings`**
   - Provides (node → node pack) mapping
   - Query params: `mode` (local/remote/nickname)
   - Useful for: "What pack contains this node type?"

3. **`GET /customnode/installed`**
   - Returns list of currently installed node packs
   - Query params: `mode` (default/imported)
   - Response: JSON array of installed packs

4. **`GET /customnode/alternatives`**
   - Get alternative node packs for similar functionality
   - Query params: `mode`

#### Version Management

5. **`GET /customnode/versions/{node_name}`**
   - Get all available versions for a specific node pack
   - Path param: `node_name` (CNR node ID)

6. **`GET /customnode/disabled_versions/{node_name}`**
   - Get disabled versions of a node pack

#### Model Management

7. **`GET /externalmodel/getlist`**
   - Returns model list with installation status
   - Query params: `mode`

#### Installation/Update Operations

8. **`POST /manager/queue/install`**
   - Queue custom node for installation
   - Requires security level: 'middle'

9. **`POST /manager/queue/update`**
   - Queue custom node for update

10. **`POST /manager/queue/uninstall`**
    - Queue custom node for uninstallation

11. **`GET /manager/queue/status`**
    - Get current queue status (total, done, in-progress)

#### Search & Discovery

12. **`GET /customnode/fetch_updates`**
    - Fetch updates for all installed nodes
    - Returns 201 if updates available, 200 if none

---

## 3. Detection: Is ComfyUI Manager Installed?

### Method 1: Check Custom Nodes Folder (Current Approach)

From `backend/mcp_server.py` → `comfy_list_folders` function:

```python
@mcp.tool()
async def comfy_list_folders(request: ComfyListFoldersRequest, ctx: Context) -> Dict[str, Any]:
    tools = get_comfy_tools()
    items = tools.list_folders(request.folder_type)
```

We can use `folder_type="custom_nodes"` to list installed node packs and check if `"ComfyUI-Manager"` is in the results.

### Method 2: API Endpoint Probe

Attempt to access a Manager-specific endpoint like `/manager/version`:

```python
@routes.get("/manager/version")
async def get_version(request):
    return web.Response(text=core.version_str, status=200)
```

If this endpoint returns 200, Manager is installed and active.

### Method 3: FL System Check Pattern

From `notes/manager/custom_nodes/ComfyUI_Fill-Nodes/nodes/utility/FL_SystemCheck.py`:

```python
import importlib

def check_library_version(library):
    try:
        module = importlib.import_module(library)
        return module.__version__
    except ImportError:
        return "Not installed"
```

This pattern could be adapted to check for Manager's presence by attempting to import its modules.

**Recommended Approach:** Combination of Method 1 (filesystem check via existing tool) + Method 2 (API probe for version).

---

## 4. FL System Check Node Analysis

### What is FL_SystemCheck?

A ComfyUI node from Fill's node pack that gathers system information and exposes it via REST API.

**File:** `notes/manager/custom_nodes/ComfyUI_Fill-Nodes/nodes/utility/FL_SystemCheck.py`

### Key Features

```python
class FL_SystemCheck:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {}}
    
    RETURN_TYPES = ()
    FUNCTION = "run_check"
    OUTPUT_NODE = True
    CATEGORY = "🏵️Fill Nodes/Utility"

def gather_system_info():
    # Collects:
    # - Python version
    # - OS info
    # - CPU/RAM/GPU
    # - Library versions (torch, xformers, numpy, etc.)
    # - Environment variables
    return info

@PromptServer.instance.routes.get("/fl_system_info")
async def system_info(request):
    return web.json_response(gather_system_info())
```

### Lessons Learned

1. **REST API Pattern:** Nodes can register custom endpoints using `@PromptServer.instance.routes`
2. **System Introspection:** Use `importlib.import_module()` to check library availability
3. **Environment Data:** Access via `os.environ.get()`
4. **GPU Detection:** Via `torch.cuda.is_available()` and `torch.cuda.get_device_name()`

### Application to Our Use Case

We can create similar capability checks for:
- ComfyUI Manager presence
- Specific node pack availability
- Model file existence
- System resource status

---

## 5. Proposed MCP Tools

### Tool Set 1: Discovery & Search

#### `manager_search_nodes`
**Purpose:** Search for custom node packs by name, description, or functionality  
**Request Model:**
```python
class ManagerSearchNodesRequest(BaseModel):
    query: Optional[str] = Field(None, description="Search query for node pack name/description")
    category: Optional[str] = Field(None, description="Filter by category")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(20, ge=1, le=100, description="Max results to return")
```

**Implementation:** Calls `/customnode/getlist` and filters results

#### `manager_check_installed`
**Purpose:** Check if ComfyUI Manager is installed and get version  
**Request Model:**
```python
class ManagerCheckInstalledRequest(BaseModel):
    pass  # No parameters needed
```

**Implementation:**
1. Use `comfy_list_folders` with `folder_type="custom_nodes"`
2. Check if `"ComfyUI-Manager"` in results
3. Optionally probe `/manager/version` endpoint

#### `manager_list_installed`
**Purpose:** Get list of all installed custom node packs  
**Request Model:**
```python
class ManagerListInstalledRequest(BaseModel):
    mode: Literal["default", "imported"] = Field("default", description="List mode")
```

**Implementation:** Calls `/customnode/installed`

### Tool Set 2: Node Pack Information

#### `manager_get_node_pack_details`
**Purpose:** Get detailed info about a specific node pack  
**Request Model:**
```python
class ManagerGetNodePackDetailsRequest(BaseModel):
    node_pack_id: str = Field(..., description="Node pack identifier")
    mode: Literal["local", "remote", "cache"] = Field("cache")
```

**Implementation:** Calls `/customnode/getlist` and filters for specific pack

#### `manager_get_node_mappings`
**Purpose:** Find which node pack provides a specific node type  
**Request Model:**
```python
class ManagerGetNodeMappingsRequest(BaseModel):
    node_type: Optional[str] = Field(None, description="Specific node type to look up")
    mode: Literal["local", "remote", "nickname"] = Field("local")
```

**Implementation:** Calls `/customnode/getmappings`

### Tool Set 3: Installation Status

#### `manager_check_updates`
**Purpose:** Check for available updates across all installed packs  
**Request Model:**
```python
class ManagerCheckUpdatesRequest(BaseModel):
    mode: Literal["local", "remote"] = Field("remote")
```

**Implementation:** Calls `/customnode/fetch_updates`

#### `manager_get_versions`
**Purpose:** Get available versions for a node pack  
**Request Model:**
```python
class ManagerGetVersionsRequest(BaseModel):
    node_pack_id: str = Field(..., description="Node pack identifier")
```

**Implementation:** Calls `/customnode/versions/{node_pack_id}`

---

## 6. Implementation Architecture

### Approach 1: Direct HTTP Calls (Recommended)

**Pros:**
- Manager already provides REST API
- No need to import Manager's Python modules
- Works regardless of Manager's internal changes
- Clean separation of concerns

**Cons:**
- Requires ComfyUI server to be running
- Network overhead (minimal, localhost)

**Implementation Pattern:**
```python
import aiohttp
from config import settings

async def _call_manager_api(endpoint: str, method: str = "GET", **kwargs):
    """Call ComfyUI Manager API endpoint."""
    url = f"{settings.comfyui_server_url}{endpoint}"
    
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, **kwargs) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise RuntimeError(f"Manager API call failed: {response.status}")
```

### Approach 2: Python Module Import

**Pros:**
- Direct access to Manager's functionality
- No HTTP overhead

**Cons:**
- Tight coupling to Manager's internal structure
- Requires Manager to be importable
- May break with Manager updates

**Not Recommended** due to coupling concerns.

---

## 7. Integration Points in mcp_server.py

### Location for New Tools

After line ~1000 in `backend/mcp_server.py`, in the section:
```python
# ============================================================================
# COMFYUI EXTENDED TOOLS
# ============================================================================
```

Add new section:
```python
# ============================================================================
# COMFYUI MANAGER TOOLS
# ============================================================================
```

### Dependencies Needed

1. **HTTP Client:** Already have `aiohttp` (used in manager_server.py)
2. **Config:** Already have `settings.comfyui_server_url` from `config.py`
3. **Error Handling:** Create `ManagerError`, `ManagerNotInstalledError` exceptions

### Request Models Location

Add after line ~300 in `backend/mcp_server.py`, after existing request models:
```python
# ============================================================================
# MANAGER REQUEST MODELS
# ============================================================================
```

---

## 8. Error Handling Strategy

### Error Types

1. **ManagerNotInstalledError:** Manager not found in custom_nodes
2. **ManagerAPIError:** Manager API returned error status
3. **ManagerConnectionError:** Cannot connect to ComfyUI server
4. **ManagerSecurityError:** Operation not allowed at current security level

### Detection Flow

```
1. Tool called
   ↓
2. Check if Manager installed (cache result for session)
   ↓
3. If not installed → ManagerNotInstalledError
   ↓
4. If installed → Make API call
   ↓
5. Handle response:
   - 200: Success
   - 403: Security error
   - 404: Not found
   - 400/500: API error
```

---

## 9. Search Functionality Deep Dive

### Manager's Search Capabilities

From `/customnode/getlist` endpoint analysis:

**Response Structure:**
```json
{
  "channel": "default",
  "node_packs": {
    "node_pack_id": {
      "name": "Node Pack Name",
      "description": "Description with markdown",
      "author": "Author name",
      "repository": "https://github.com/...",
      "files": ["https://github.com/.../archive/main.zip"],
      "installed": "True"/"False"/"Update",
      "updatable": true/false,
      "stars": 123,
      "last_update": "2024-01-01",
      "category": "category_name"
    }
  }
}
```

### Search Implementation Strategy

**Client-side filtering** of `/customnode/getlist` results:

1. Fetch full list (cached by Manager)
2. Filter by:
   - Text search in name/description
   - Category match
   - Installation status
   - Update availability
3. Sort by relevance/stars/date
4. Limit results

---

## 10. Security Considerations

### Manager Security Levels

From `manager_server.py`:

```python
def is_allowed_security_level(level):
    # 'block': Never allowed
    # 'high': Requires local IP + 'normal-' or 'weak' security
    # 'middle': Requires 'weak', 'normal', or 'normal-' security
    # default: Always allowed
```

### Read-Only Operations (Safe)

- `/customnode/getlist` ✓
- `/customnode/installed` ✓
- `/customnode/getmappings` ✓
- `/customnode/versions/{node_name}` ✓
- `/manager/version` ✓
- `/externalmodel/getlist` ✓

### Write Operations (Require Security Level)

- `/manager/queue/install` (middle)
- `/manager/queue/update` (middle)
- `/manager/queue/uninstall` (middle)

**For Phase 1:** Only implement read-only operations.

---

## 11. Testing Strategy

### Prerequisites

1. ComfyUI running with Manager installed
2. At least 2-3 custom node packs installed
3. Manager in various states (updates available, no updates)

### Test Cases

1. **Detection:**
   - Manager installed → Returns version
   - Manager not installed → Clear error message

2. **Search:**
   - Empty query → Returns all (limited)
   - Specific query → Filters correctly
   - Category filter → Returns only matching category
   - Installed filter → Returns only installed

3. **Mappings:**
   - Known node type → Returns correct pack
   - Unknown node type → Returns empty/null

4. **Versions:**
   - Valid pack ID → Returns version list
   - Invalid pack ID → Returns error

---

## 12. Next Steps

See [investigation.md](./investigation.md) for:
- Detailed code structure analysis
- Exact implementation locations
- Request/response model definitions
- Error handling implementation
- Integration testing plan

---

## References

- **ComfyUI Manager Server:** `notes/manager/custom_nodes/ComfyUI-Manager/glob/manager_server.py`
- **FL System Check:** `notes/manager/custom_nodes/ComfyUI_Fill-Nodes/nodes/utility/FL_SystemCheck.py`
- **Current MCP Server:** `backend/mcp_server.py`
- **ComfyUI Tools:** `backend/comfy_tools.py` (for filesystem operations)
- **Node Library Client:** `backend/node_library.py` (pattern reference)

---

## Appendix: Manager API Quick Reference

| Endpoint | Method | Purpose | Security |
|----------|--------|---------|----------|
| `/manager/version` | GET | Get Manager version | None |
| `/customnode/getlist` | GET | List all node packs | None |
| `/customnode/installed` | GET | List installed packs | None |
| `/customnode/getmappings` | GET | Node→Pack mappings | None |
| `/customnode/versions/{id}` | GET | Get pack versions | None |
| `/customnode/fetch_updates` | GET | Check for updates | None |
| `/externalmodel/getlist` | GET | List models | None |
| `/manager/queue/status` | GET | Queue status | None |
| `/manager/queue/install` | POST | Install pack | Middle |
| `/manager/queue/update` | POST | Update pack | Middle |
| `/manager/queue/uninstall` | POST | Uninstall pack | Middle |
