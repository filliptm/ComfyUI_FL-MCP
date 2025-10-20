# ComfyUI Node Library Tools - Implementation Plan (2nd Draft)

**Date:** 2025-10-18  
**Status:** Design Phase - Refactored for Clean Architecture

---

## Architecture Overview

**Separation of Concerns:**

```
backend/
├── node_library.py          # NEW: Core node library logic (like comfy_tools.py)
│   ├── NodeLibraryClient    # HTTP client + caching
│   ├── Search/filter helpers
│   └── Compatibility logic
│
├── mcp_server.py            # MODIFIED: Clean tool wrappers only
│   ├── Import from node_library
│   ├── Request models
│   └── @mcp.tool() wrappers
│
└── config.py                # MODIFIED: Add ComfyUI server config
```

**Pattern Matching Existing Code:**
- `comfy_tools.py` → `node_library.py` (logic layer)
- `mcp_server.py` → Thin wrappers calling `node_library` functions
- Same pattern as `comfy_list_folders()`, `comfy_read_file()`, etc.

---

## File 1: `backend/node_library.py` (NEW)

**Purpose:** Core node library discovery logic (analogous to `comfy_tools.py`)

### Complete File Contents:

```python
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
        if query_lower in node_type.lower():
            return True
        
        # Search in display name
        if query_lower in node_info.get('display_name', '').lower():
            return True
        
        # Search in description
        if query_lower in node_info.get('description', '').lower():
            return True
        
        # Search in category
        if query_lower in node_info.get('category', '').lower():
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
```

---

## File 2: `backend/config.py` (MODIFIED)

**Changes:** Add ComfyUI server configuration

### Additions to Settings Class:

```python
class Settings(BaseSettings):
    # ... existing settings ...
    
    # ComfyUI Server Configuration
    comfyui_server_url: str = "http://127.0.0.1:8188"
    comfyui_api_timeout: int = 10  # seconds
```

**Location:** Add after the `tool_timeout` and `max_tool_retries` section

---

## File 3: `backend/mcp_server.py` (MODIFIED)

**Changes:** Add imports, request models, and clean tool wrappers

### Section 1: Add Imports (after existing imports)

```python
# Add to imports section (around line 15)
from node_library import (
    get_node_library_client,
    NodeLibraryError,
    NodeLibraryConnectionError,
    NodeTypeNotFoundError
)
```

### Section 2: Add Request Models (after existing request models, before tools)

```python
# ============================================================================
# NODE LIBRARY REQUEST MODELS
# ============================================================================

class NodeLibrarySearchRequest(BaseModel):
    """Search for ComfyUI node types by various criteria."""
    query: Optional[str] = Field(
        None,
        description="Text search in node type names and descriptions (case-insensitive)"
    )
    category: Optional[str] = Field(
        None,
        description="Filter by node category (e.g., 'sampling', 'loaders', 'image')"
    )
    input_type: Optional[str] = Field(
        None,
        description="Find node types accepting this input type (e.g., 'LATENT', 'IMAGE')"
    )
    output_type: Optional[str] = Field(
        None,
        description="Find node types producing this output type (e.g., 'IMAGE', 'LATENT')"
    )
    max_results: int = Field(
        20,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50)"
    )


class NodeLibraryGetDetailsRequest(BaseModel):
    """Get detailed information about a specific node type."""
    node_type: str = Field(
        ...,
        description="Exact node type name (e.g., 'KSampler', 'CheckpointLoaderSimple')"
    )


class NodeLibraryFindCompatibleRequest(BaseModel):
    """Find node types compatible with a given node type."""
    node_type: str = Field(
        ...,
        description="Source node type name (e.g., 'KSampler')"
    )
    direction: Literal["downstream", "upstream", "both"] = Field(
        "downstream",
        description="downstream=connects AFTER, upstream=connects BEFORE, both=both directions"
    )
    output_slot: Optional[str] = Field(
        None,
        description="Specific output slot name to match (downstream only)"
    )
    input_slot: Optional[str] = Field(
        None,
        description="Specific input slot name to match (upstream only)"
    )
    max_results: int = Field(
        30,
        ge=1,
        le=100,
        description="Maximum results per direction (1-100)"
    )
```

### Section 3: Add Tool Wrappers (after comfy_search_resources, before ERROR FEEDBACK section)

```python
# ============================================================================
# COMFYUI NODE LIBRARY DISCOVERY TOOLS
# ============================================================================

@mcp.tool()
async def node_library_search(request: NodeLibrarySearchRequest, ctx: Context) -> Dict[str, Any]:
    """Search for available ComfyUI node types (not workflow nodes).
    
    This tool searches the library of installed node types that can be created
    in workflows. Use this to discover what node types are available before
    creating them with create_nodes().
    
    DISTINCTION FROM find_node():
    - find_node() searches nodes already IN your workflow
    - node_library_search() searches node TYPES available to create
    
    USE CASES:
    - "What node types handle upscaling?" → output_type="IMAGE", query="upscale"
    - "Show samplers" → category="sampling"
    - "What accepts LATENT?" → input_type="LATENT"
    - "Find LoRA loaders" → query="lora"
    
    RETURNS:
    Array of matching node type definitions with inputs, outputs, categories.
    Use node_library_get_details() for comprehensive info on a specific type.
    """
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        results = await client.search_nodes(
            query=request.query,
            category=request.category,
            input_type=request.input_type,
            output_type=request.output_type,
            max_results=request.max_results
        )
        
        # Format results
        formatted_results = [
            {
                "node_type": r.node_type,
                "display_name": r.display_name,
                "category": r.category,
                "description": r.description,
                "inputs": r.inputs,
                "outputs": r.outputs,
                "match_reason": r.match_reason
            }
            for r in results
        ]
        
        return {
            "query": request.model_dump(exclude_none=True),
            "results": formatted_results,
            "total_results": len(formatted_results),
            "truncated": len(formatted_results) >= request.max_results
        }
        
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library search failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_search: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_get_details(request: NodeLibraryGetDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get comprehensive details about a specific node type.
    
    This tool provides everything needed to understand and use a node type
    before creating it in the workflow with create_nodes().
    
    DISTINCTION FROM get_node_values():
    - get_node_values() gets parameter VALUES from a workflow node instance
    - node_library_get_details() gets parameter DEFINITIONS for a node type
    
    USE CASES:
    - Before creating a node: understand what parameters it needs
    - When planning workflow: verify input/output compatibility
    - When debugging: check valid parameter ranges and types
    - Learning: understand what a node type does
    
    RETURNS:
    Complete node type specification including:
    - All input parameters with types, defaults, constraints (min/max/options)
    - All output types and names
    - Category and display information
    - Parameter order (for UI layout)
    """
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        node_info = await client.get_node_details(request.node_type)
        
        return {
            "node_type": request.node_type,
            "display_name": node_info.get('display_name', request.node_type),
            "category": node_info.get('category', ''),
            "description": node_info.get('description', ''),
            "inputs": node_info.get('input', {}),
            "outputs": node_info.get('output', []),
            "output_names": node_info.get('output_name', []),
            "input_order": node_info.get('input_order', [])
        }
        
    except NodeTypeNotFoundError as e:
        raise RuntimeError(str(e))
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library lookup failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_get_details: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_find_compatible(request: NodeLibraryFindCompatibleRequest, ctx: Context) -> Dict[str, Any]:
    """Find node types that can connect to/from a given node type.
    
    This tool helps discover what node types are compatible based on input/output
    type matching. Use this when building workflows to find what comes next.
    
    DISTINCTION FROM connect_nodes():
    - connect_nodes() connects EXISTING workflow nodes together
    - node_library_find_compatible() finds compatible node TYPES to create
    
    USE CASES:
    - Building workflow: "What can I connect after KSampler?" → downstream
    - Understanding flow: "What feeds into VAEDecode?" → upstream
    - Planning chain: "Build checkpoint → sampler → decode → save" → iterate downstream
    - Type checking: "Can I connect this type to that?" → verify compatibility
    
    RETURNS:
    Array of compatible node types with connection details:
    - Which output/input slots are compatible
    - What data types match
    - Suggested connection patterns
    """
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        compatible = await client.find_compatible_nodes(
            node_type=request.node_type,
            direction=request.direction,
            output_slot=request.output_slot,
            input_slot=request.input_slot,
            max_results=request.max_results
        )
        
        # Format results
        formatted_compatible = [
            {
                "node_type": c.node_type,
                "display_name": c.display_name,
                "category": c.category,
                "connection": c.connection,
                "description": c.description
            }
            for c in compatible
        ]
        
        return {
            "source_node_type": request.node_type,
            "direction": request.direction,
            "compatible_nodes": formatted_compatible,
            "total_compatible": len(formatted_compatible),
            "truncated": len(formatted_compatible) >= request.max_results
        }
        
    except NodeTypeNotFoundError as e:
        raise RuntimeError(str(e))
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library compatibility search failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_find_compatible: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")
```

---

## File 4: `.env` (OPTIONAL - User Override)

**Changes:** Add optional ComfyUI server configuration

```bash
# ComfyUI Server Configuration (optional - defaults to http://127.0.0.1:8188)
COMFYUI_SERVER_URL=http://127.0.0.1:8188
COMFYUI_API_TIMEOUT=10
```

---

## Dependencies

**Add to `requirements.txt` or `pyproject.toml`:**

```txt
httpx>=0.27.0  # Async HTTP client for node library API
```

**Install:**
```bash
pip install httpx
```

---

## Implementation Checklist

### Phase 1: Create Core Module (45 min)

- [ ] **Create `backend/node_library.py`**
  - [ ] Copy complete file contents from above
  - [ ] Verify imports
  - [ ] Test basic import: `python -c "from backend.node_library import get_node_library_client"`

### Phase 2: Update Configuration (5 min)

- [ ] **Modify `backend/config.py`**
  - [ ] Add `comfyui_server_url` setting
  - [ ] Add `comfyui_api_timeout` setting
  - [ ] Verify settings load correctly

### Phase 3: Add Tool Wrappers (30 min)

- [ ] **Modify `backend/mcp_server.py`**
  - [ ] Add imports from `node_library`
  - [ ] Add request models (3 classes)
  - [ ] Add tool wrappers (3 functions)
  - [ ] Verify no syntax errors

### Phase 4: Install Dependencies (5 min)

- [ ] **Install httpx**
  ```bash
  pip install httpx
  ```

### Phase 5: Testing (30 min)

- [ ] **Start ComfyUI** (ensure running at http://127.0.0.1:8188)

- [ ] **Test node_library_search()**
  - [ ] Search by query: `{"query": "sampler"}`
  - [ ] Search by category: `{"category": "sampling"}`
  - [ ] Search by output_type: `{"output_type": "IMAGE"}`
  - [ ] Combined search: `{"query": "upscale", "output_type": "IMAGE"}`

- [ ] **Test node_library_get_details()**
  - [ ] Valid node: `{"node_type": "KSampler"}`
  - [ ] Invalid node: `{"node_type": "NonExistent"}` (should error with suggestions)

- [ ] **Test node_library_find_compatible()**
  - [ ] Downstream: `{"node_type": "KSampler", "direction": "downstream"}`
  - [ ] Upstream: `{"node_type": "KSampler", "direction": "upstream"}`
  - [ ] Both: `{"node_type": "VAEDecode", "direction": "both"}`

- [ ] **Test error handling**
  - [ ] Stop ComfyUI, call any tool (should get connection error)
  - [ ] Verify error messages are helpful

- [ ] **Test caching**
  - [ ] First call: check logs for "Fetching from..."
  - [ ] Second call (within 5 min): check logs for "Cache hit"

### Total Estimated Time: ~2 hours

---

## Code Organization Summary

**What Goes Where:**

| Component | Location | Purpose |
|-----------|----------|----------|
| Core logic | `backend/node_library.py` | HTTP client, search, caching |
| Request models | `backend/mcp_server.py` | Pydantic models for tool inputs |
| Tool wrappers | `backend/mcp_server.py` | @mcp.tool() decorated functions |
| Configuration | `backend/config.py` | Server URL and timeout settings |
| User overrides | `.env` | Optional custom ComfyUI server URL |

**Pattern Match:**
```python
# Existing pattern:
comfy_tools.py       → Core logic (ComfyUITools class)
mcp_server.py        → Tool wrapper (comfy_list_folders)

# New pattern:
node_library.py      → Core logic (NodeLibraryClient class)
mcp_server.py        → Tool wrapper (node_library_search)
```

---

## Exact Line Numbers for mcp_server.py

**Import Section (add after line ~15):**
```python
from node_library import (
    get_node_library_client,
    NodeLibraryError,
    NodeLibraryConnectionError,
    NodeTypeNotFoundError
)
```

**Request Models (add after existing request models, before first @mcp.tool()):**
- Look for the last request model definition
- Add the 3 NodeLibrary request models

**Tool Wrappers (add after `comfy_search_resources()`, before ERROR FEEDBACK section):**
- Find the comment: `# ============================================================================`
- Find: `# ERROR FEEDBACK & QUEUE STATUS TOOLS`
- Insert new section before it:
```python
# ============================================================================
# COMFYUI NODE LIBRARY DISCOVERY TOOLS
# ============================================================================

@mcp.tool()
async def node_library_search(...):
    ...

@mcp.tool()
async def node_library_get_details(...):
    ...

@mcp.tool()
async def node_library_find_compatible(...):
    ...
```

---

## Success Criteria

**Implementation Complete When:**

1. ✅ `backend/node_library.py` created and imports successfully
2. ✅ `backend/config.py` updated with ComfyUI settings
3. ✅ `backend/mcp_server.py` has 3 new tools registered
4. ✅ `httpx` installed
5. ✅ All test cases pass
6. ✅ Cache working (logs show hits/misses)
7. ✅ Error messages helpful and actionable
8. ✅ No token bloat (focused results only)

---

## Notes

- **Clean Architecture:** Logic separated from tool wrappers (like comfy_tools.py pattern)
- **Minimal Changes to mcp_server.py:** Only imports, models, and thin wrappers
- **Reusable Core:** `NodeLibraryClient` can be used elsewhere if needed
- **Consistent Patterns:** Follows existing code style and error handling
- **Token Efficient:** No full library dumps, only targeted results

---

**Ready for implementation!** 🚀

All code is complete and ready to copy-paste. Just follow the checklist and test each phase.
