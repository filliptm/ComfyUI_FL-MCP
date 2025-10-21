# ComfyUI Manager Integration - Final Implementation

**Date:** 2025-10-20  
**Project:** fl_js  
**Status:** READY FOR IMPLEMENTATION  
**Parent Documents:** [research.md](./research.md), [investigation.md](./investigation.md)

---

## Changes from investigation.md

### Tools Removed

1. **`manager_check_installed`** - REMOVED
   - **Reason:** Manager detection happens in lifespan, accessible via `ctx`
   - **Alternative:** Manager client available in context, no tool needed

2. **`manager_list_installed`** - REMOVED
   - **Reason:** Already covered by `comfy_list_folders` with `folder_type="custom_nodes"`
   - **Reason:** Also covered by `manager_search_nodes` with `installed_only=True` filter
   - **Alternative:** Use existing tools that provide same functionality

3. **`manager_get_pack_details`** - REMOVED
   - **Reason:** Redundant with `manager_search_nodes` which returns full details
   - **Alternative:** Search for specific pack by name to get details

### Tools Kept (3 Total)

1. **`manager_search_nodes`** - KEPT
   - Primary discovery tool
   - Includes `installed_only` filter (replaces manager_list_installed)
   - Returns full pack details (replaces manager_get_pack_details)

2. **`manager_get_node_mappings`** - KEPT
   - Unique capability: node type → pack lookup
   - No overlap with existing tools

3. **`manager_check_updates`** - KEPT
   - Useful agent-accessible functionality
   - No overlap with existing tools

### Architectural Changes

1. **Manager Client in Lifespan**
   - Initialize in `mcp_lifespan()` like node_library_client
   - Store in context for tool access
   - Global singleton pattern via `get_comfy_manager_client()`

2. **Docstring Style**
   - Agent-focused: "Use this when...", "This tool helps..."
   - No code examples
   - Focus on when/why to use the tool
   - Match existing tool docstring style in mcp_server.py

---

## Implementation

### Step 1: Create backend/comfy_manager.py

**File:** `backend/comfy_manager.py` (NEW, ~450 lines)

```python
"""ComfyUI Manager client for node pack discovery and management.

Provides access to ComfyUI Manager's REST API for:
- Node pack search and discovery
- Installation status checking
- Node-to-pack mappings
- Update checking
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Literal
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Exceptions
# ============================================================================

class ManagerError(Exception):
    """Base exception for ComfyUI Manager errors."""
    pass


class ManagerNotInstalledError(ManagerError):
    """Raised when ComfyUI Manager is not installed."""
    pass


class ManagerConnectionError(ManagerError):
    """Raised when ComfyUI server is unreachable."""
    pass


class ManagerAPIError(ManagerError):
    """Raised when Manager API returns an error."""
    pass


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class NodePackInfo:
    """Information about a custom node pack."""
    id: str
    name: str
    description: str
    author: str
    repository: str
    installed: str  # "True", "False", or "Update"
    updatable: bool
    stars: int
    last_update: str
    category: str
    files: List[str]


@dataclass
class ManagerVersion:
    """ComfyUI Manager version info."""
    version: str
    installed: bool


@dataclass
class NodeMapping:
    """Mapping of node type to node pack."""
    node_type: str
    node_pack_id: str
    node_pack_name: str


# ============================================================================
# Cache
# ============================================================================

class ManagerCache:
    """Cache for Manager API responses."""
    
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Any] = {}
        self._cache_times: Dict[str, float] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
    
    async def get(self, key: str) -> Optional[Any]:
        """Get cached data if valid."""
        async with self._lock:
            if key not in self._cache:
                return None
            
            age = time.time() - self._cache_times[key]
            if age > self._ttl:
                logger.debug(f"[Manager] Cache expired for {key} (age: {age:.1f}s)")
                return None
            
            logger.debug(f"[Manager] Cache hit for {key} (age: {age:.1f}s)")
            return self._cache[key]
    
    async def set(self, key: str, data: Any):
        """Set cache data."""
        async with self._lock:
            self._cache[key] = data
            self._cache_times[key] = time.time()
            logger.debug(f"[Manager] Cache updated for {key}")
    
    async def invalidate(self, key: Optional[str] = None):
        """Clear cache (specific key or all)."""
        async with self._lock:
            if key:
                self._cache.pop(key, None)
                self._cache_times.pop(key, None)
                logger.debug(f"[Manager] Cache invalidated for {key}")
            else:
                self._cache.clear()
                self._cache_times.clear()
                logger.debug("[Manager] Cache cleared")


# ============================================================================
# Core Client
# ============================================================================

class ComfyManagerClient:
    """Client for ComfyUI Manager REST API."""
    
    def __init__(self, server_url: str = "http://127.0.0.1:8188", timeout: int = 10):
        self.server_url = server_url.rstrip('/')
        self.timeout = timeout
        self.cache = ManagerCache(ttl_seconds=300)
        self._manager_installed: Optional[bool] = None
        self._manager_version: Optional[str] = None
    
    async def check_installed(self) -> ManagerVersion:
        """Check if ComfyUI Manager is installed and get version.
        
        Uses /manager/version endpoint probe (Method 2 from research).
        
        Returns:
            ManagerVersion with installation status and version
            
        Raises:
            ManagerConnectionError: If ComfyUI server is unreachable
        """
        # Check cache first
        if self._manager_installed is not None:
            return ManagerVersion(
                version=self._manager_version or "unknown",
                installed=self._manager_installed
            )
        
        url = f"{self.server_url}/manager/version"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                logger.info(f"[Manager] Checking installation at {url}")
                response = await client.get(url)
                
                if response.status_code == 200:
                    version = response.text.strip()
                    self._manager_installed = True
                    self._manager_version = version
                    logger.info(f"[Manager] Installed, version: {version}")
                    return ManagerVersion(version=version, installed=True)
                elif response.status_code == 404:
                    # Manager not installed
                    self._manager_installed = False
                    logger.warning("[Manager] Not installed (404 on /manager/version)")
                    return ManagerVersion(version="", installed=False)
                else:
                    raise ManagerAPIError(
                        f"Unexpected response from /manager/version: {response.status_code}"
                    )
                    
        except httpx.TimeoutException:
            raise ManagerConnectionError(
                f"ComfyUI server timeout. Is ComfyUI running at {self.server_url}?"
            )
        except httpx.RequestError as e:
            raise ManagerConnectionError(
                f"Failed to connect to ComfyUI at {self.server_url}: {e}"
            )
        except Exception as e:
            logger.error(f"[Manager] Unexpected error checking installation: {e}")
            raise ManagerConnectionError(f"Failed to check Manager installation: {e}")
    
    async def _ensure_installed(self):
        """Ensure Manager is installed before making API calls.
        
        Raises:
            ManagerNotInstalledError: If Manager is not installed
        """
        version_info = await self.check_installed()
        if not version_info.installed:
            raise ManagerNotInstalledError(
                "ComfyUI Manager is not installed. "
                "Please install it from: https://github.com/ltdrdata/ComfyUI-Manager"
            )
    
    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Make GET request to Manager API.
        
        Args:
            endpoint: API endpoint (e.g., "/customnode/getlist")
            params: Query parameters
            
        Returns:
            JSON response data
            
        Raises:
            ManagerAPIError: If API returns error
            ManagerConnectionError: If connection fails
        """
        url = f"{self.server_url}{endpoint}"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                logger.debug(f"[Manager] GET {url} params={params}")
                response = await client.get(url, params=params)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    raise ManagerAPIError(f"Endpoint not found: {endpoint}")
                elif response.status_code == 403:
                    raise ManagerAPIError(
                        f"Access forbidden (security level). Endpoint: {endpoint}"
                    )
                else:
                    raise ManagerAPIError(
                        f"Manager API error: {response.status_code} for {endpoint}"
                    )
                    
        except httpx.TimeoutException:
            raise ManagerConnectionError(
                f"Timeout accessing Manager API: {endpoint}"
            )
        except httpx.RequestError as e:
            raise ManagerConnectionError(
                f"Failed to connect to Manager API: {e}"
            )
        except ManagerAPIError:
            raise
        except Exception as e:
            logger.error(f"[Manager] Unexpected error on {endpoint}: {e}")
            raise ManagerAPIError(f"Failed to access {endpoint}: {e}")
    
    async def search_node_packs(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        installed_only: bool = False,
        updates_available: bool = False,
        mode: Literal["local", "remote", "cache"] = "cache",
        max_results: int = 20
    ) -> List[NodePackInfo]:
        """Search for node packs by various criteria.
        
        Args:
            query: Text search in name/description/author
            category: Filter by category
            installed_only: Only show installed packs
            updates_available: Only show packs with updates
            mode: Data source mode
            max_results: Maximum results to return
            
        Returns:
            List of matching NodePackInfo
        """
        await self._ensure_installed()
        
        # Check cache
        cache_key = f"node_packs_{mode}"
        cached = await self.cache.get(cache_key)
        
        if cached is None:
            # Fetch from API
            data = await self._get("/customnode/getlist", params={"mode": mode})
            
            # Parse response
            all_packs = {}
            raw_packs = data.get("custom_nodes", [])
            
            for pack in raw_packs:
                pack_id = pack.get("title", "").replace(" ", "_")
                all_packs[pack_id] = NodePackInfo(
                    id=pack_id,
                    name=pack.get("title", ""),
                    description=pack.get("description", ""),
                    author=pack.get("author", ""),
                    repository=pack.get("reference", ""),
                    installed=pack.get("installed", "False"),
                    updatable=pack.get("installed") == "Update",
                    stars=pack.get("stars", 0),
                    last_update=pack.get("last_update", ""),
                    category=pack.get("category", ""),
                    files=pack.get("files", [])
                )
            
            # Cache results
            await self.cache.set(cache_key, all_packs)
            logger.info(f"[Manager] Fetched {len(all_packs)} node packs (mode={mode})")
        else:
            all_packs = cached
        
        # Apply filters
        results = []
        for pack in all_packs.values():
            if query:
                query_lower = query.lower()
                if not (
                    query_lower in pack.name.lower() or
                    query_lower in pack.description.lower() or
                    query_lower in pack.author.lower()
                ):
                    continue
            
            if category and pack.category.lower() != category.lower():
                continue
            
            if installed_only and pack.installed == "False":
                continue
            
            if updates_available and not pack.updatable:
                continue
            
            results.append(pack)
            
            if len(results) >= max_results:
                break
        
        logger.info(f"[Manager] Search found {len(results)} packs")
        return results
    
    async def get_node_mappings(
        self,
        mode: Literal["local", "remote", "nickname"] = "local"
    ) -> Dict[str, NodeMapping]:
        """Get node type to node pack mappings.
        
        Args:
            mode: Mapping source mode
            
        Returns:
            Dictionary mapping node type to NodeMapping
        """
        await self._ensure_installed()
        
        data = await self._get("/customnode/getmappings", params={"mode": mode})
        
        mappings = {}
        for node_type, pack_info in data.items():
            if isinstance(pack_info, list) and len(pack_info) > 0:
                pack_id = pack_info[0]
                mappings[node_type] = NodeMapping(
                    node_type=node_type,
                    node_pack_id=pack_id,
                    node_pack_name=pack_id  # Could enhance with actual name lookup
                )
        
        logger.info(f"[Manager] Fetched {len(mappings)} node mappings")
        return mappings
    
    async def check_updates(
        self,
        mode: Literal["local", "remote"] = "remote"
    ) -> Dict[str, Any]:
        """Check for available updates.
        
        Args:
            mode: Check mode (local or remote)
            
        Returns:
            Update status information
        """
        await self._ensure_installed()
        
        try:
            data = await self._get("/customnode/fetch_updates", params={"mode": mode})
            return {
                "updates_available": True,
                "details": data
            }
        except ManagerAPIError:
            # 200 = no updates, 201 = updates available
            return {
                "updates_available": False,
                "message": "No updates available"
            }


# ============================================================================
# Global Instance
# ============================================================================

_comfy_manager_client: Optional[ComfyManagerClient] = None


def get_comfy_manager_client(
    server_url: str = "http://127.0.0.1:8188",
    timeout: int = 10
) -> ComfyManagerClient:
    """Get or create the global ComfyManagerClient instance."""
    global _comfy_manager_client
    if _comfy_manager_client is None:
        _comfy_manager_client = ComfyManagerClient(server_url, timeout)
    return _comfy_manager_client
```

---

### Step 2: Modify backend/mcp_server.py - Imports

**File:** `backend/mcp_server.py`  
**Location:** After line ~30 (after `from node_library import ...`)

```python
from comfy_manager import (
    get_comfy_manager_client,
    ManagerError,
    ManagerNotInstalledError,
    ManagerConnectionError,
    ManagerAPIError
)
```

---

### Step 3: Modify backend/mcp_server.py - Lifespan

**File:** `backend/mcp_server.py`  
**Location:** In `mcp_lifespan` function, after subprocess/standalone logic, before final `yield`

**Add:**

```python
    # Check if ComfyUI Manager is installed and initialize client
    manager_client = None
    manager_available = False
    
    try:
        from config import settings
        manager_client = get_comfy_manager_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        version_info = await manager_client.check_installed()
        
        if version_info.installed:
            logger.info(f"[MCP] ComfyUI Manager detected (v{version_info.version})")
            manager_available = True
        else:
            logger.warning("[MCP] ComfyUI Manager not installed - manager tools will return errors")
    except Exception as e:
        logger.warning(f"[MCP] Could not check Manager status: {e}")
```

**Modify the yield statement to include manager client:**

```python
    # Before (subprocess mode):
    yield {"client": _WS_CLIENT}
    
    # After (subprocess mode):
    yield {
        "client": _WS_CLIENT,
        "manager_client": manager_client,
        "manager_available": manager_available
    }
    
    # Before (standalone mode):
    yield {"client": None}
    
    # After (standalone mode):
    yield {
        "client": None,
        "manager_client": manager_client,
        "manager_available": manager_available
    }
```

---

### Step 4: Modify backend/mcp_server.py - Request Models

**File:** `backend/mcp_server.py`  
**Location:** After line ~300 (after NODE LIBRARY REQUEST MODELS section)

```python
# ============================================================================
# MANAGER REQUEST MODELS
# ============================================================================

class ManagerSearchNodesRequest(BaseModel):
    """Search for custom node packs in ComfyUI Manager."""
    query: Optional[str] = Field(None, description="Search query for node pack name/description/author")
    category: Optional[str] = Field(None, description="Filter by category")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates available")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(20, ge=1, le=100, description="Maximum results to return")


class ManagerGetNodeMappingsRequest(BaseModel):
    """Get node type to pack mappings from ComfyUI Manager."""
    node_type: Optional[str] = Field(None, description="Specific node type to look up (empty for all)")
    mode: Literal["local", "remote", "nickname"] = Field("local", description="Mapping source")


class ManagerCheckUpdatesRequest(BaseModel):
    """Check for available updates to installed node packs."""
    mode: Literal["local", "remote"] = Field("remote", description="Check mode")
```

---

### Step 5: Modify backend/mcp_server.py - Tool Definitions

**File:** `backend/mcp_server.py`  
**Location:** After the NODE LIBRARY DISCOVERY TOOLS section (after `node_library_find_compatible`)

```python
# ============================================================================
# COMFYUI MANAGER TOOLS
# ============================================================================

@mcp.tool()
async def manager_search_nodes(
    request: ManagerSearchNodesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search for custom node packs available through ComfyUI Manager.
    
    Use this tool to discover node packs by name, category, or functionality.
    Helps find and understand what node packs are available to install or
    what's already installed in the ComfyUI environment.
    
    WHEN TO USE:
    - "What node packs handle image upscaling?" → query="upscale"
    - "Show me animation node packs" → category="animation"
    - "What's installed?" → installed_only=True
    - "What can I update?" → updates_available=True
    - "Find packs by author" → query="author_name"
    
    FILTERS:
    - query: Text search across name, description, author
    - category: Filter by pack category
    - installed_only: Only show installed packs (alternative to comfy_list_folders)
    - updates_available: Only show packs with updates
    - mode: "cache" (fast), "remote" (fresh), "local" (filesystem)
    
    RETURNS:
    Array of node pack objects with full details including:
    - name, description, author, repository
    - installation status ("True", "False", "Update")
    - stars, last_update, category
    - files (download URLs)
    
    NOTE: If Manager not installed, returns error with installation instructions.
    """
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "results": [],
                "count": 0
            }
        
        results = await manager_client.search_node_packs(
            query=request.query,
            category=request.category,
            installed_only=request.installed_only,
            updates_available=request.updates_available,
            mode=request.mode,
            max_results=request.max_results
        )
        
        # Convert dataclass to dict
        results_dict = [
            {
                "id": pack.id,
                "name": pack.name,
                "description": pack.description,
                "author": pack.author,
                "repository": pack.repository,
                "installed": pack.installed,
                "updatable": pack.updatable,
                "stars": pack.stars,
                "last_update": pack.last_update,
                "category": pack.category,
                "files": pack.files
            }
            for pack in results
        ]
        
        return {
            "results": results_dict,
            "count": len(results_dict),
            "truncated": len(results_dict) >= request.max_results
        }
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerConnectionError as e:
        logger.error(f"[Manager] Connection error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "results": [], "count": 0}


@mcp.tool()
async def manager_get_node_mappings(
    request: ManagerGetNodeMappingsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Find which node pack provides a specific node type.
    
    Use this tool to discover the source node pack for any node type in ComfyUI.
    Helps understand dependencies and find where to get missing nodes.
    
    WHEN TO USE:
    - "What pack has the KSampler node?" → node_type="KSampler"
    - "Where does FL_ImageCaptionSaver come from?" → node_type="FL_ImageCaptionSaver"
    - "Show all node-to-pack mappings" → node_type=None (returns all)
    - Debugging missing nodes → lookup node type to find pack
    
    RETURNS:
    If node_type specified:
    - Single mapping: {node_type, pack_id, pack_name, found: true/false}
    
    If node_type empty:
    - All mappings: {mappings: {node_type: {pack_id, pack_name}, ...}, count}
    
    NOTE: This is different from node_library tools which search node TYPE definitions.
    This tool maps node types to their SOURCE PACK.
    """
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "mappings": {},
                "count": 0
            }
        
        mappings = await manager_client.get_node_mappings(mode=request.mode)
        
        if request.node_type:
            # Return specific mapping
            if request.node_type in mappings:
                mapping = mappings[request.node_type]
                return {
                    "node_type": mapping.node_type,
                    "pack_id": mapping.node_pack_id,
                    "pack_name": mapping.node_pack_name,
                    "found": True
                }
            else:
                return {
                    "node_type": request.node_type,
                    "found": False,
                    "error": f"Node type '{request.node_type}' not found in mappings"
                }
        else:
            # Return all mappings
            mappings_dict = {
                node_type: {
                    "pack_id": mapping.node_pack_id,
                    "pack_name": mapping.node_pack_name
                }
                for node_type, mapping in mappings.items()
            }
            return {
                "mappings": mappings_dict,
                "count": len(mappings_dict)
            }
            
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}


@mcp.tool()
async def manager_check_updates(
    request: ManagerCheckUpdatesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Check if any installed node packs have available updates.
    
    Use this tool to discover if the ComfyUI installation has outdated node packs
    that could benefit from updates.
    
    WHEN TO USE:
    - Maintenance: "Are there any updates available?"
    - Before troubleshooting: Check if updating might fix issues
    - After installing ComfyUI: See what's outdated
    - Regular checks: Keep environment up to date
    
    MODES:
    - "remote": Check against remote repositories (fresh, slower)
    - "local": Check against local cache (fast, may be stale)
    
    RETURNS:
    {
        "updates_available": bool,
        "details": {...} or "message": "No updates available"
    }
    
    NOTE: This is read-only. To actually update, user must use ComfyUI Manager UI.
    """
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "updates_available": False
            }
        
        result = await manager_client.check_updates(mode=request.mode)
        return result
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "updates_available": False}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "updates_available": False}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "updates_available": False}
```

---

## Summary of Changes

### Files Created

1. **`backend/comfy_manager.py`** (~450 lines)
   - ComfyManagerClient class
   - Exception hierarchy
   - Data classes (NodePackInfo, ManagerVersion, NodeMapping)
   - Cache system
   - 3 core methods: search_node_packs, get_node_mappings, check_updates
   - Global singleton getter

### Files Modified

1. **`backend/mcp_server.py`** (~200 lines added)
   - **Imports:** Add comfy_manager imports (~5 lines)
   - **Lifespan:** Initialize manager client (~15 lines)
   - **Request Models:** Add 3 request classes (~25 lines)
   - **Tools:** Add 3 tool definitions (~155 lines)

### Total Implementation

- **New Code:** ~450 lines
- **Modified Code:** ~200 lines
- **Total:** ~650 lines
- **Tools Added:** 3
- **Dependencies:** 0 (all existing)

---

## Testing Checklist

### 1. Manager Detection

- [ ] Start MCP with Manager installed → logs "Manager detected (vX.X)"
- [ ] Start MCP without Manager → logs warning about Manager not installed
- [ ] Call manager tool without Manager → returns error dict, doesn't crash

### 2. manager_search_nodes

- [ ] Search with query → returns filtered results
- [ ] Search with category → returns category-filtered results
- [ ] Search with installed_only=True → returns only installed packs
- [ ] Search with updates_available=True → returns only updatable packs
- [ ] Search with max_results=5 → returns max 5 results
- [ ] Search with no results → returns empty array

### 3. manager_get_node_mappings

- [ ] Lookup known node type → returns pack info
- [ ] Lookup unknown node type → returns found=False
- [ ] Lookup with node_type=None → returns all mappings

### 4. manager_check_updates

- [ ] Check with updates available → returns updates_available=True
- [ ] Check with no updates → returns updates_available=False
- [ ] Check with mode="remote" → fetches fresh data
- [ ] Check with mode="local" → uses cache

### 5. Error Handling

- [ ] ComfyUI not running → returns connection error
- [ ] Manager not installed → returns not_installed error
- [ ] Network timeout → returns timeout error
- [ ] All errors logged, none crash MCP

---

## Rollback Procedure

If issues arise:

```bash
# Remove new file
rm backend/comfy_manager.py

# Revert mcp_server.py
git checkout backend/mcp_server.py
```

Or manually remove:
1. Import section additions (line ~30)
2. Lifespan additions (line ~180)
3. Request models section (line ~300)
4. Tools section (after node_library tools)

---

## Future Enhancements (Out of Scope)

### Phase 2: Write Operations

- `manager_install_pack()` - Install node pack
- `manager_update_pack()` - Update node pack  
- `manager_uninstall_pack()` - Uninstall node pack

**Note:** Requires security level handling and more complex error management.

### Phase 3: Advanced Features

- Model management tools
- Version pinning
- Dependency resolution
- Bulk operations

---

## References

- **Research:** [research.md](./research.md)
- **Investigation:** [investigation.md](./investigation.md)
- **Pattern:** `backend/node_library.py`
- **Integration:** `backend/mcp_server.py`
