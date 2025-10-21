# ComfyUI Manager Integration - Technical Investigation

**Date:** 2025-10-20  
**Project:** fl_js  
**Parent Document:** [research.md](./research.md)  
**Purpose:** Deep dive into implementation details and code structure

---

## 1. Implementation Pattern: Following node_library.py

### Reference Architecture

**File:** `backend/node_library.py`

This module provides the blueprint for our ComfyUI Manager client:

```python
# Structure:
1. Docstring
2. Imports (httpx, logging, typing, dataclasses)
3. Exceptions (NodeLibraryError, NodeLibraryConnectionError, NodeTypeNotFoundError)
4. Data Classes (@dataclass for results)
5. Cache Class (optional, with TTL)
6. Core Client Class (with httpx async client)
7. Global Instance (singleton pattern)
```

### Key Patterns to Follow

#### Exception Hierarchy
```python
class NodeLibraryError(Exception):
    """Base exception for node library errors."""
    pass

class NodeLibraryConnectionError(NodeLibraryError):
    """Raised when ComfyUI server is unreachable."""
    pass
```

#### HTTP Client Pattern
```python
async with httpx.AsyncClient(timeout=self.timeout) as client:
    response = await client.get(url)
    response.raise_for_status()
    return response.json()
```

#### Error Handling Pattern
```python
try:
    # Make request
except httpx.TimeoutException:
    raise NodeLibraryConnectionError(f"ComfyUI server timeout...")
except httpx.HTTPStatusError as e:
    raise NodeLibraryConnectionError(f"ComfyUI server error: {e.response.status_code}")
except httpx.RequestError as e:
    raise NodeLibraryConnectionError(f"Failed to connect...")
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    raise NodeLibraryConnectionError(f"Failed...")
```

#### Global Instance Pattern
```python
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

## 2. New Module: backend/comfy_manager.py

### File Structure

```python
"""ComfyUI Manager client for node pack discovery and management.

Provides access to ComfyUI Manager's REST API for:
- Node pack search and discovery
- Installation status checking
- Version management
- Node-to-pack mappings
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
# Cache (Optional - can add later if needed)
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
        
        Uses Method 2 from research: probe /manager/version endpoint.
        
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
    
    async def get_node_packs(
        self,
        mode: Literal["local", "remote", "cache"] = "cache"
    ) -> Dict[str, NodePackInfo]:
        """Get list of all node packs.
        
        Args:
            mode: Data source (local=filesystem, remote=fetch, cache=use cached)
            
        Returns:
            Dictionary mapping node pack ID to NodePackInfo
        """
        await self._ensure_installed()
        
        # Check cache
        cache_key = f"node_packs_{mode}"
        cached = await self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        # Fetch from API
        data = await self._get("/customnode/getlist", params={"mode": mode})
        
        # Parse response
        node_packs = {}
        raw_packs = data.get("custom_nodes", [])
        
        for pack in raw_packs:
            pack_id = pack.get("title", "").replace(" ", "_")
            node_packs[pack_id] = NodePackInfo(
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
        await self.cache.set(cache_key, node_packs)
        
        logger.info(f"[Manager] Fetched {len(node_packs)} node packs (mode={mode})")
        return node_packs
    
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
            query: Text search in name/description
            category: Filter by category
            installed_only: Only show installed packs
            updates_available: Only show packs with updates
            mode: Data source mode
            max_results: Maximum results to return
            
        Returns:
            List of matching NodePackInfo
        """
        all_packs = await self.get_node_packs(mode=mode)
        results = []
        
        for pack in all_packs.values():
            # Apply filters
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
    
    async def get_installed_packs(
        self,
        mode: Literal["default", "imported"] = "default"
    ) -> List[NodePackInfo]:
        """Get list of installed node packs.
        
        Args:
            mode: List mode (default or imported)
            
        Returns:
            List of installed NodePackInfo
        """
        await self._ensure_installed()
        
        data = await self._get("/customnode/installed", params={"mode": mode})
        
        installed = []
        if isinstance(data, dict):
            custom_nodes = data.get("custom_nodes", [])
        else:
            custom_nodes = data
        
        for pack in custom_nodes:
            pack_id = pack.get("title", "").replace(" ", "_")
            installed.append(NodePackInfo(
                id=pack_id,
                name=pack.get("title", ""),
                description=pack.get("description", ""),
                author=pack.get("author", ""),
                repository=pack.get("reference", ""),
                installed="True",
                updatable=pack.get("installed") == "Update",
                stars=pack.get("stars", 0),
                last_update=pack.get("last_update", ""),
                category=pack.get("category", ""),
                files=pack.get("files", [])
            ))
        
        logger.info(f"[Manager] Found {len(installed)} installed packs")
        return installed
    
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
        except ManagerAPIError as e:
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

## 3. Integration into mcp_server.py

### Import Section (after line ~30)

```python
from comfy_manager import (
    get_comfy_manager_client,
    ManagerError,
    ManagerNotInstalledError,
    ManagerConnectionError,
    ManagerAPIError,
    NodePackInfo,
    ManagerVersion,
    NodeMapping
)
```

### Conditional Tool Registration Pattern

FastMCP supports dynamic tool registration. We can check Manager availability during lifespan and conditionally register tools.

**Location:** In `mcp_lifespan` function (around line 172-210)

```python
@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[Any]:
    """Manage MCP server lifespan and persistent WebSocket connection."""
    global _WS_CLIENT
    
    # ... existing subprocess/standalone logic ...
    
    # Check if ComfyUI Manager is installed
    try:
        manager_client = get_comfy_manager_client()
        version_info = await manager_client.check_installed()
        
        if version_info.installed:
            logger.info(f"[MCP] ComfyUI Manager detected (v{version_info.version})")
            # Manager tools will be available
            _MANAGER_AVAILABLE = True
        else:
            logger.warning("[MCP] ComfyUI Manager not installed - manager tools disabled")
            _MANAGER_AVAILABLE = False
    except Exception as e:
        logger.warning(f"[MCP] Could not check Manager status: {e}")
        _MANAGER_AVAILABLE = False
    
    yield {
        "client": _WS_CLIENT,
        "manager_available": _MANAGER_AVAILABLE
    }
```

### Request Models (after line ~300)

```python
# ============================================================================
# MANAGER REQUEST MODELS
# ============================================================================

class ManagerCheckInstalledRequest(BaseModel):
    """Request to check if ComfyUI Manager is installed."""
    pass


class ManagerSearchNodesRequest(BaseModel):
    """Request to search for custom node packs."""
    query: Optional[str] = Field(None, description="Search query for node pack name/description/author")
    category: Optional[str] = Field(None, description="Filter by category")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates available")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(20, ge=1, le=100, description="Maximum results to return")


class ManagerListInstalledRequest(BaseModel):
    """Request to list installed node packs."""
    mode: Literal["default", "imported"] = Field("default", description="List mode")


class ManagerGetNodeMappingsRequest(BaseModel):
    """Request to get node type to pack mappings."""
    node_type: Optional[str] = Field(None, description="Specific node type to look up (empty for all)")
    mode: Literal["local", "remote", "nickname"] = Field("local", description="Mapping source")


class ManagerCheckUpdatesRequest(BaseModel):
    """Request to check for available updates."""
    mode: Literal["local", "remote"] = Field("remote", description="Check mode")


class ManagerGetPackDetailsRequest(BaseModel):
    """Request to get detailed info about a specific node pack."""
    pack_id: str = Field(..., description="Node pack identifier")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
```

### Tool Definitions (after line ~1000, new section)

```python
# ============================================================================
# COMFYUI MANAGER TOOLS
# ============================================================================

@mcp.tool()
async def manager_check_installed(request: ManagerCheckInstalledRequest, ctx: Context) -> Dict[str, Any]:
    """Check if ComfyUI Manager is installed and get version.
    
    Returns:
        Dictionary with 'installed' (bool) and 'version' (str) keys
        
    Example:
        >>> manager_check_installed({})
        {"installed": true, "version": "2.50"}
    """
    try:
        client = get_comfy_manager_client()
        version_info = await client.check_installed()
        
        return {
            "installed": version_info.installed,
            "version": version_info.version,
            "status": "ready" if version_info.installed else "not_installed"
        }
    except ManagerConnectionError as e:
        logger.error(f"[Manager] Connection error: {e}")
        return {
            "installed": False,
            "error": str(e),
            "status": "connection_error"
        }
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {
            "installed": False,
            "error": str(e),
            "status": "error"
        }


@mcp.tool()
async def manager_search_nodes(
    request: ManagerSearchNodesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search for custom node packs by name, description, or category.
    
    Args:
        request: Search criteria (query, category, filters, etc.)
        
    Returns:
        Dictionary with 'results' list and 'count' int
        
    Example:
        >>> manager_search_nodes({"query": "image", "max_results": 5})
        {"results": [{...}], "count": 5}
    """
    try:
        client = get_comfy_manager_client()
        results = await client.search_node_packs(
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
                "category": pack.category
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
async def manager_list_installed(
    request: ManagerListInstalledRequest,
    ctx: Context
) -> Dict[str, Any]:
    """List all installed custom node packs.
    
    Args:
        request: List mode (default or imported)
        
    Returns:
        Dictionary with 'installed' list and 'count' int
        
    Example:
        >>> manager_list_installed({"mode": "default"})
        {"installed": [{...}], "count": 15}
    """
    try:
        client = get_comfy_manager_client()
        installed = await client.get_installed_packs(mode=request.mode)
        
        installed_dict = [
            {
                "id": pack.id,
                "name": pack.name,
                "description": pack.description,
                "author": pack.author,
                "repository": pack.repository,
                "updatable": pack.updatable,
                "category": pack.category
            }
            for pack in installed
        ]
        
        return {
            "installed": installed_dict,
            "count": len(installed_dict)
        }
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "installed": [], "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "installed": [], "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "installed": [], "count": 0}


@mcp.tool()
async def manager_get_node_mappings(
    request: ManagerGetNodeMappingsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Get node type to node pack mappings.
    
    Find which node pack provides a specific node type, or get all mappings.
    
    Args:
        request: Node type to lookup (optional) and mode
        
    Returns:
        Dictionary with 'mappings' dict or single 'mapping' object
        
    Example:
        >>> manager_get_node_mappings({"node_type": "KSampler"})
        {"node_type": "KSampler", "pack_id": "...", "pack_name": "..."}
    """
    try:
        client = get_comfy_manager_client()
        mappings = await client.get_node_mappings(mode=request.mode)
        
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
    """Check for available updates to installed node packs.
    
    Args:
        request: Check mode (local or remote)
        
    Returns:
        Dictionary with 'updates_available' bool and update details
        
    Example:
        >>> manager_check_updates({"mode": "remote"})
        {"updates_available": true, "details": {...}}
    """
    try:
        client = get_comfy_manager_client()
        result = await client.check_updates(mode=request.mode)
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


@mcp.tool()
async def manager_get_pack_details(
    request: ManagerGetPackDetailsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Get detailed information about a specific node pack.
    
    Args:
        request: Pack ID and mode
        
    Returns:
        Dictionary with detailed pack information
        
    Example:
        >>> manager_get_pack_details({"pack_id": "ComfyUI-Manager"})
        {"id": "...", "name": "...", "description": "...", ...}
    """
    try:
        client = get_comfy_manager_client()
        all_packs = await client.get_node_packs(mode=request.mode)
        
        if request.pack_id in all_packs:
            pack = all_packs[request.pack_id]
            return {
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
                "files": pack.files,
                "found": True
            }
        else:
            return {
                "pack_id": request.pack_id,
                "found": False,
                "error": f"Node pack '{request.pack_id}' not found"
            }
            
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "found": False}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "found": False}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "found": False}
```

---

## 4. Error Handling Flow

### Detection Flow (from research.md)

```
1. Tool called
   ↓
2. get_comfy_manager_client() - creates/returns singleton
   ↓
3. client.method() - calls _ensure_installed() internally
   ↓
4. _ensure_installed() checks cache or probes /manager/version
   ↓
5a. If installed → Proceed with API call
5b. If not installed → Raise ManagerNotInstalledError
   ↓
6. Tool catches exception and returns error dict (logged, not raised)
```

### Exception Handling in Tools

All tools follow this pattern:

```python
try:
    # Business logic
    result = await client.some_method()
    return result
except ManagerNotInstalledError as e:
    logger.warning(f"[Manager] Not installed: {e}")
    return {"error": str(e), ...defaults...}
except ManagerAPIError as e:
    logger.error(f"[Manager] API error: {e}")
    return {"error": str(e), ...defaults...}
except ManagerConnectionError as e:
    logger.error(f"[Manager] Connection error: {e}")
    return {"error": str(e), ...defaults...}
except Exception as e:
    logger.error(f"[Manager] Unexpected error: {e}")
    return {"error": str(e), ...defaults...}
```

**Key Points:**
- Never raise exceptions to MCP client
- Always return dict with "error" key
- Log all errors with appropriate level
- Provide sensible defaults (empty lists, False, etc.)

---

## 5. Testing Checklist

### Unit Tests (future)

- [ ] `ComfyManagerClient.check_installed()` with mock responses
- [ ] `ComfyManagerClient._get()` error handling
- [ ] `ComfyManagerClient.search_node_packs()` filtering logic
- [ ] Cache TTL expiration
- [ ] Exception hierarchy

### Integration Tests

- [ ] Manager installed, version detected
- [ ] Manager not installed, graceful error
- [ ] Search with various filters
- [ ] Get mappings for known node type
- [ ] Get mappings for unknown node type
- [ ] List installed packs
- [ ] Check updates (with/without updates available)
- [ ] Connection timeout handling
- [ ] ComfyUI server not running

### Manual Testing

1. Start ComfyUI with Manager installed
2. Run MCP server
3. Call `manager_check_installed` - should return version
4. Call `manager_search_nodes` with query - should return results
5. Call `manager_list_installed` - should list installed packs
6. Stop ComfyUI server
7. Call any manager tool - should return connection error
8. Uninstall Manager (or point to instance without it)
9. Call any manager tool - should return not_installed error

---

## 6. File Modification Summary

### New File

- **`backend/comfy_manager.py`** (new file, ~600 lines)
  - Complete client implementation
  - Follows `node_library.py` pattern
  - Exception classes
  - Data classes
  - Cache class
  - Client class
  - Global instance getter

### Modified Files

- **`backend/mcp_server.py`**
  - Line ~30: Add imports
  - Line ~172-210: Add Manager detection in lifespan
  - Line ~300: Add request models (6 classes)
  - Line ~1000: Add tool definitions (6 tools)
  - Total additions: ~400 lines

---

## 7. Dependencies

### Already Available

- `httpx` - Used by `node_library.py`
- `asyncio` - Standard library
- `logging` - Standard library
- `typing` - Standard library
- `dataclasses` - Standard library
- `pydantic` - Used throughout

### No New Dependencies Required ✓

---

## 8. Configuration

### Environment Variables (optional)

```bash
# Override ComfyUI server URL (defaults to http://127.0.0.1:8188)
COMFYUI_SERVER_URL=http://localhost:8188

# Override timeout (defaults to 10 seconds)
COMFYUI_TIMEOUT=15
```

### Usage in Code

```python
import os
from comfy_manager import get_comfy_manager_client

server_url = os.getenv("COMFYUI_SERVER_URL", "http://127.0.0.1:8188")
timeout = int(os.getenv("COMFYUI_TIMEOUT", "10"))

client = get_comfy_manager_client(server_url, timeout)
```

---

## 9. Future Enhancements (Out of Scope for Phase 1)

### Write Operations

- `manager_install_pack()` - Install node pack
- `manager_update_pack()` - Update node pack
- `manager_uninstall_pack()` - Uninstall node pack
- `manager_queue_status()` - Get installation queue status

**Note:** Requires security level checks and more complex error handling.

### Advanced Features

- Model management tools (`/externalmodel/*` endpoints)
- Version pinning/rollback
- Bulk operations
- Dependency resolution

---

## 10. Next Steps

See [implementation.md](./implementation.md) for:
- Complete file contents for `backend/comfy_manager.py`
- Complete modifications for `backend/mcp_server.py`
- Line-by-line implementation guide
- Copy-paste ready code blocks

---

## References

- **Parent Research:** [research.md](./research.md)
- **Pattern Reference:** `backend/node_library.py`
- **Integration Point:** `backend/mcp_server.py`
- **Manager API Source:** `notes/manager/custom_nodes/ComfyUI-Manager/glob/manager_server.py`
