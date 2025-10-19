"""ComfyUI node library discovery via HTTP API.

Provides intelligent search and discovery of installed ComfyUI node types
through the /object_info API endpoint.
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

class NodeLibraryError(Exception):
    """Base exception for node library errors."""
    pass


class NodeLibraryConnectionError(NodeLibraryError):
    """Raised when ComfyUI server is unreachable."""
    pass


class NodeTypeNotFoundError(NodeLibraryError):
    """Raised when a node type doesn't exist."""
    pass


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class NodeSearchResult:
    """Result from node library search."""
    node_type: str
    display_name: str
    category: str
    description: str
    inputs: Dict[str, Any]
    outputs: List[str]
    match_reason: str


@dataclass
class CompatibleNode:
    """Compatible node type for connection."""
    node_type: str
    display_name: str
    category: str
    connection: Dict[str, str]
    description: str


# ============================================================================
# Cache
# ============================================================================

class NodeLibraryCache:
    """Cache for ComfyUI node library data."""
    
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: Optional[float] = None
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
    
    async def get(self) -> Optional[Dict[str, Any]]:
        """Get cached data if valid."""
        async with self._lock:
            if self._cache is None:
                return None
            
            age = time.time() - self._cache_time
            if age > self._ttl:
                logger.debug(f"[NodeLibrary] Cache expired (age: {age:.1f}s)")
                return None
            
            logger.debug(f"[NodeLibrary] Cache hit (age: {age:.1f}s)")
            return self._cache
    
    async def set(self, data: Dict[str, Any]):
        """Set cache data."""
        async with self._lock:
            self._cache = data
            self._cache_time = time.time()
            logger.debug(f"[NodeLibrary] Cache updated ({len(data)} nodes)")
    
    async def invalidate(self):
        """Clear cache."""
        async with self._lock:
            self._cache = None
            self._cache_time = None
            logger.debug("[NodeLibrary] Cache invalidated")


# ============================================================================
# Core Client
# ============================================================================

class NodeLibraryClient:
    """Client for ComfyUI node library discovery."""
    
    def __init__(self, server_url: str = "http://127.0.0.1:8188", timeout: int = 10):
        self.server_url = server_url.rstrip('/')
        self.timeout = timeout
        self.cache = NodeLibraryCache(ttl_seconds=300)
    
    async def fetch_node_library(self) -> Dict[str, Any]:
        """Fetch node library from ComfyUI /object_info endpoint.
        
        Returns:
            Dictionary mapping node type names to node metadata
            
        Raises:
            NodeLibraryConnectionError: If ComfyUI server is unreachable
        """
        # Check cache first
        cached = await self.cache.get()
        if cached is not None:
            return cached
        
        # Fetch from ComfyUI
        url = f"{self.server_url}/object_info"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                logger.info(f"[NodeLibrary] Fetching from {url}")
                response = await client.get(url)
                response.raise_for_status()
                
                data = response.json()
                
                # Validate response structure
                if not isinstance(data, dict):
                    raise NodeLibraryConnectionError(
                        f"Invalid response from ComfyUI: expected dict, got {type(data)}"
                    )
                
                logger.info(f"[NodeLibrary] Fetched {len(data)} node types")
                
                # Cache the result
                await self.cache.set(data)
                
                return data
                
        except httpx.TimeoutException:
            raise NodeLibraryConnectionError(
                f"ComfyUI server timeout. Is ComfyUI running at {self.server_url}?"
            )
        except httpx.HTTPStatusError as e:
            raise NodeLibraryConnectionError(
                f"ComfyUI server error: {e.response.status_code}"
            )
        except httpx.RequestError as e:
            raise NodeLibraryConnectionError(
                f"Failed to connect to ComfyUI at {self.server_url}: {e}"
            )
        except Exception as e:
            logger.error(f"[NodeLibrary] Unexpected error: {e}")
            raise NodeLibraryConnectionError(f"Failed to fetch node library: {e}")
    
    async def search_nodes(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        input_type: Optional[str] = None,
        output_type: Optional[str] = None,
        max_results: int = 20
    ) -> List[NodeSearchResult]:
        """Search for node types by various criteria.
        
        Args:
            query: Text search in node names/descriptions
            category: Filter by category
            input_type: Find nodes accepting this input type
            output_type: Find nodes producing this output type
            max_results: Maximum results to return
            
        Returns:
            List of matching node search results
        """
        node_library = await self.fetch_node_library()
        results = []
        
        for node_type, node_info in node_library.items():
            # Apply filters
            match_reasons = []
            
            # Text query
            if query and not self._matches_text_query(node_type, node_info, query):
                continue
            if query:
                match_reasons.append(f"matches query '{query}'")
            
            # Category filter
            if category and not self._matches_category(node_info, category):
                continue
            if category:
                match_reasons.append(f"category='{category}'")
            
            # Input type filter
            if input_type and not self._has_input_type(node_info, input_type):
                continue
            if input_type:
                match_reasons.append(f"accepts input type '{input_type}'")
            
            # Output type filter
            if output_type and not self._has_output_type(node_info, output_type):
                continue
            if output_type:
                match_reasons.append(f"outputs type '{output_type}'")
            
            # Build result
            results.append(NodeSearchResult(
                node_type=node_type,
                display_name=node_info.get('display_name', node_type),
                category=node_info.get('category', ''),
                description=node_info.get('description', ''),
                inputs=node_info.get('input', {}),
                outputs=node_info.get('output', []),
                match_reason=', '.join(match_reasons) if match_reasons else 'all nodes'
            ))
            
            if len(results) >= max_results:
                break
        
        logger.info(f"[NodeLibrary] Search found {len(results)} results")
        return results
    
    async def get_node_details(self, node_type: str) -> Dict[str, Any]:
        """Get detailed information about a specific node type.
        
        Args:
            node_type: Exact node type name
            
        Returns:
            Complete node metadata
            
        Raises:
            NodeTypeNotFoundError: If node type doesn't exist
        """
        node_library = await self.fetch_node_library()
        
        if node_type not in node_library:
            # Find similar nodes for suggestion
            similar = self._find_similar_node_types(node_type, node_library, max_suggestions=5)
            raise NodeTypeNotFoundError(
                f"Node type '{node_type}' not found.\n" +
                (f"Did you mean: {', '.join(similar)}?" if similar else "")
            )
        
        return node_library[node_type]
    
    async def find_compatible_nodes(
        self,
        node_type: str,
        direction: Literal["downstream", "upstream", "both"] = "downstream",
        output_slot: Optional[str] = None,
        input_slot: Optional[str] = None,
        max_results: int = 30
    ) -> List[CompatibleNode]:
        """Find node types compatible with a given node type.
        
        Args:
            node_type: Source node type name
            direction: Search direction (downstream/upstream/both)
            output_slot: Specific output slot to match (downstream only)
            input_slot: Specific input slot to match (upstream only)
            max_results: Maximum results per direction
            
        Returns:
            List of compatible node types
            
        Raises:
            NodeTypeNotFoundError: If source node type doesn't exist
        """
        node_library = await self.fetch_node_library()
        
        # Validate source node exists
        if node_type not in node_library:
            similar = self._find_similar_node_types(node_type, node_library, max_suggestions=5)
            raise NodeTypeNotFoundError(
                f"Node type '{node_type}' not found.\n" +
                (f"Did you mean: {', '.join(similar)}?" if similar else "")
            )
        
        source_node_info = node_library[node_type]
        compatible = []
        
        # Find downstream compatible (what can connect after)
        if direction in ["downstream", "both"]:
            downstream = self._find_downstream_compatible(
                source_node_info, node_library, output_slot, max_results
            )
            compatible.extend(downstream)
        
        # Find upstream compatible (what can connect before)
        if direction in ["upstream", "both"]:
            upstream = self._find_upstream_compatible(
                source_node_info, node_library, input_slot, max_results
            )
            compatible.extend(upstream)
        
        logger.info(f"[NodeLibrary] Found {len(compatible)} compatible nodes")
        return compatible
    
    # ========================================================================
    # Helper Methods
    # ========================================================================
    
    def _matches_text_query(self, node_type: str, node_info: Dict[str, Any], query: str) -> bool:
        """Check if node matches text query."""
        query_lower = query.lower()
        
        # Search in node type name
        if node_type is not None:
            if query_lower in node_type.lower():
                return True
        
        if node_info is not None:
            # Search in display name
            dn = node_info.get('display_name', '')
            if dn and query_lower in str(dn).lower():
                return True
            
            # Search in description
            dsc = node_info.get('description', '')
            if dsc and query_lower in str(dsc).lower():
                return True
            
            # Search in category
            cat = node_info.get('category', '')
            if query_lower in str(cat).lower():
                return True
        
        return False
    
    def _has_input_type(self, node_info: Dict[str, Any], input_type: str) -> bool:
        """Check if node has input of specified type."""
        inputs = node_info.get('input', {})
        required = inputs.get('required', {})
        optional = inputs.get('optional', {})
        
        for param_spec in {**required, **optional}.values():
            if isinstance(param_spec, list) and len(param_spec) > 0:
                if param_spec[0] == input_type:
                    return True
        
        return False
    
    def _has_output_type(self, node_info: Dict[str, Any], output_type: str) -> bool:
        """Check if node has output of specified type."""
        outputs = node_info.get('output', [])
        return output_type in outputs
    
    def _matches_category(self, node_info: Dict[str, Any], category: str) -> bool:
        """Check if node belongs to category."""
        node_category = node_info.get('category', '').lower()
        category_lower = category.lower()
        
        # Exact match or starts with (for subcategories like "image/upscaling")
        return node_category == category_lower or node_category.startswith(category_lower + '/')
    
    def _find_similar_node_types(
        self, 
        query: str, 
        node_library: Dict[str, Any], 
        max_suggestions: int = 5
    ) -> List[str]:
        """Find similar node type names for suggestions."""
        query_lower = query.lower()
        similar = []
        
        for node_type in node_library.keys():
            # Simple similarity: contains query or query contains node type
            if query_lower in node_type.lower() or node_type.lower() in query_lower:
                similar.append(node_type)
                if len(similar) >= max_suggestions:
                    break
        
        return similar
    
    def _find_downstream_compatible(
        self,
        source_node_info: Dict[str, Any],
        all_nodes: Dict[str, Any],
        output_slot: Optional[str] = None,
        max_results: int = 30
    ) -> List[CompatibleNode]:
        """Find node types that can accept outputs from source node."""
        source_outputs = source_node_info.get('output', [])
        if not source_outputs:
            return []
        
        # Filter to specific output slot if requested
        if output_slot is not None:
            output_names = source_node_info.get('output_name', [])
            if output_slot in output_names:
                idx = output_names.index(output_slot)
                if idx < len(source_outputs):
                    source_outputs = [source_outputs[idx]]
        
        compatible = []
        
        for node_type, node_info in all_nodes.items():
            inputs = node_info.get('input', {}).get('required', {})
            
            for input_name, input_spec in inputs.items():
                if isinstance(input_spec, list) and len(input_spec) > 0:
                    input_type = input_spec[0]
                    
                    if input_type in source_outputs:
                        compatible.append(CompatibleNode(
                            node_type=node_type,
                            display_name=node_info.get('display_name', node_type),
                            category=node_info.get('category', ''),
                            connection={
                                'source_output': input_type,
                                'target_input': input_name,
                                'data_type': input_type
                            },
                            description=node_info.get('description', '')
                        ))
                        break
            
            if len(compatible) >= max_results:
                break
        
        return compatible
    
    def _find_upstream_compatible(
        self,
        target_node_info: Dict[str, Any],
        all_nodes: Dict[str, Any],
        input_slot: Optional[str] = None,
        max_results: int = 30
    ) -> List[CompatibleNode]:
        """Find node types that can provide inputs to target node."""
        target_inputs = target_node_info.get('input', {}).get('required', {})
        if not target_inputs:
            return []
        
        # Filter to specific input slot if requested
        if input_slot is not None:
            if input_slot in target_inputs:
                target_inputs = {input_slot: target_inputs[input_slot]}
            else:
                return []
        
        # Collect required input types
        required_types = set()
        for input_spec in target_inputs.values():
            if isinstance(input_spec, list) and len(input_spec) > 0:
                required_types.add(input_spec[0])
        
        if not required_types:
            return []
        
        compatible = []
        
        for node_type, node_info in all_nodes.items():
            outputs = node_info.get('output', [])
            
            for output_type in outputs:
                if output_type in required_types:
                    # Find which input this satisfies
                    for input_name, input_spec in target_inputs.items():
                        if isinstance(input_spec, list) and input_spec[0] == output_type:
                            compatible.append(CompatibleNode(
                                node_type=node_type,
                                display_name=node_info.get('display_name', node_type),
                                category=node_info.get('category', ''),
                                connection={
                                    'source_output': output_type,
                                    'target_input': input_name,
                                    'data_type': output_type
                                },
                                description=node_info.get('description', '')
                            ))
                            break
                    break
            
            if len(compatible) >= max_results:
                break
        
        return compatible


# ============================================================================
# Global Instance
# ============================================================================

_node_library_client: Optional[NodeLibraryClient] = None


def get_node_library_client(
    server_url: str = "http://127.0.0.1:8188",
    timeout: int = 10
) -> NodeLibraryClient:
    """Get or create the global NodeLibraryClient instance."""
    global _node_library_client
    if _node_library_client is None:
        _node_library_client = NodeLibraryClient(server_url, timeout)
    return _node_library_client
