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
    matched_nodes: Optional[List[str]] = None  # Node class names that matched filter

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
        node_filter: Optional[str] = None,
        installed_only: bool = False,
        updates_available: bool = False,
        mode: Literal["local", "remote", "cache"] = "cache",
        max_results: int = 20
    ) -> List[NodePackInfo]:
        """Search for node packs by various criteria.
        
        Args:
            query: Text search in name/description/author
            category: Filter by category
            node_filter: Regex pattern to match node class names within packs
            installed_only: Only show installed packs
            updates_available: Only show packs with updates
            mode: Data source mode
            max_results: Maximum results to return
            
        Returns:
            List of matching NodePackInfo (with matched_nodes if node_filter used)
        """
        await self._ensure_installed()
        
        # Check cache for node packs
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
                    files=pack.get("files", []),
                    matched_nodes=None
                )
            
            # Cache results
            await self.cache.set(cache_key, all_packs)
            logger.info(f"[Manager] Fetched {len(all_packs)} node packs (mode={mode})")
        else:
            all_packs = cached
        
        # If node_filter is specified, fetch node mappings
        node_to_pack_map = None
        if node_filter:
            try:
                import re
                node_pattern = re.compile(node_filter, re.IGNORECASE)
                
                # Get node mappings (node_type -> pack_id)
                mappings = await self.get_node_mappings(mode="local")
                
                # Invert to pack_id -> [node_types]
                pack_to_nodes = {}
                for node_type, mapping in mappings.items():
                    pack_id = mapping.node_pack_id
                    if pack_id not in pack_to_nodes:
                        pack_to_nodes[pack_id] = []
                    pack_to_nodes[pack_id].append(node_type)
                
                # Filter nodes by pattern
                node_to_pack_map = {}  # pack_id -> [matched_node_types]
                for pack_id, node_types in pack_to_nodes.items():
                    matched = [nt for nt in node_types if node_pattern.search(nt)]
                    if matched:
                        node_to_pack_map[pack_id] = matched
                
                logger.info(f"[Manager] Node filter matched {len(node_to_pack_map)} packs")
                
            except re.error as e:
                logger.error(f"[Manager] Invalid regex pattern '{node_filter}': {e}")
                # Continue without node filtering
                node_to_pack_map = None
        
        # Apply filters
        results = []
        for pack in all_packs.values():
            # Node filter (must match first if specified)
            if node_to_pack_map is not None:
                if pack.id not in node_to_pack_map:
                    continue
                # Add matched nodes to pack info
                pack.matched_nodes = node_to_pack_map[pack.id]
            
            # Text query filter
            if query:
                query_lower = query.lower()
                if not (
                    query_lower in pack.name.lower() or
                    query_lower in pack.description.lower() or
                    query_lower in pack.author.lower()
                ):
                    continue
            
            # Category filter
            if category and pack.category.lower() != category.lower():
                continue
            
            # Installation filter
            if installed_only and pack.installed == "False":
                continue
            
            # Update filter
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
        
        API returns data in format:
        {
            "extension-id": [
                ["NodeClass1", "NodeClass2", ...],  # List of node types
                {"author": "...", "description": "..."}  # Metadata
            ],
            ...
        }
        
        Args:
            mode: Mapping source mode
            
        Returns:
            Dictionary mapping node type to NodeMapping
        """
        await self._ensure_installed()
        
        data = await self._get("/customnode/getmappings", params={"mode": mode})
        
        mappings = {}
        # Iterate over extension-id (pack_id) -> pack_data
        for pack_id, pack_data in data.items():
            # pack_data is [node_list, metadata_dict]
            if isinstance(pack_data, list) and len(pack_data) > 0:
                node_list = pack_data[0]  # First element is list of node types
                
                # Ensure node_list is actually a list
                if isinstance(node_list, list):
                    # Map each node type to this pack
                    for node_type in node_list:
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