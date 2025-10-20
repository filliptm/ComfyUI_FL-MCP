# ComfyUI Node Library Tools - Implementation Plan

**Target File:** `backend/mcp_server.py`  
**Date:** 2025-10-18  
**Status:** Design Phase - Ready for Implementation

---

## Executive Summary

Implement **3 new MCP tools** that query ComfyUI's `/object_info` HTTP API to enable intelligent node discovery and search within the agent workflow. These tools will be prefixed with `node_library_` to clearly distinguish them from workflow manipulation tools.

**Why Not `get_node_library()`?**  
As discussed, fetching the entire node library (potentially hundreds of nodes) would consume excessive tokens and increase costs. Instead, we focus on **targeted search and lookup** operations.

---

## Tool Naming Convention

### Pattern: `node_library_{action}`

**Rationale:**
- Clearly scoped to **node discovery** (not workflow manipulation)
- Distinguishes from existing tools like `find_node()` (finds nodes **in** workflow)
- Prefix makes autocomplete/search easier for agents
- Consistent with existing patterns like `comfy_list_folders()`

**Comparison with Existing Tools:**

| Existing Tool | Scope | New Tool | Scope |
|--------------|-------|----------|-------|
| `find_node()` | Find node **in current workflow** | `node_library_search()` | Search **available node types** |
| `create_nodes()` | Create node **instances** | `node_library_get_details()` | Get node **type definition** |
| `get_node_slots()` | Get slots of **workflow node** | `node_library_find_compatible()` | Find **type-compatible node types** |
| `comfy_list_folders()` | List **filesystem resources** | `node_library_search()` | Search **installed node types** |

---

## Tools to Implement

### 1. `node_library_search()` - Search Available Node Types

**Purpose:** Search ComfyUI's installed node library by name, category, input/output types, or capabilities.

**Use Cases (Precise for Agent):**
- "What node types can upscale images?" → search by output_type="IMAGE", query="upscale"
- "Show me all sampling node types" → search by category="sampling"
- "What node types accept LATENT input?" → search by input_type="LATENT"
- "Find checkpoint loader node types" → search by query="checkpoint"

**Request Model:**
```python
class NodeLibrarySearchRequest(BaseModel):
    """Search for ComfyUI node types by various criteria."""
    query: Optional[str] = Field(
        None, 
        description="Text search in node type names and descriptions (case-insensitive)"
    )
    category: Optional[str] = Field(
        None,
        description="Filter by node category (e.g., 'sampling', 'loaders', 'image', 'latent')"
    )
    input_type: Optional[str] = Field(
        None,
        description="Find node types that accept this input type (e.g., 'LATENT', 'IMAGE', 'MODEL')"
    )
    output_type: Optional[str] = Field(
        None,
        description="Find node types that produce this output type (e.g., 'IMAGE', 'LATENT', 'CONDITIONING')"
    )
    max_results: int = Field(
        20,
        description="Maximum number of results to return (default: 20, max: 50)"
    )
```

**Response Structure:**
```python
{
    "query": {"query": "upscale", "output_type": "IMAGE"},
    "results": [
        {
            "node_type": "ImageUpscaleWithModel",
            "display_name": "Upscale Image (using Model)",
            "category": "image/upscaling",
            "description": "Upscale image using upscale model",
            "inputs": {
                "required": {"image": "IMAGE", "upscale_model": "UPSCALE_MODEL"},
                "optional": {}
            },
            "outputs": ["IMAGE"],
            "match_reason": "Matches output_type='IMAGE' and query contains 'upscale'"
        },
        # ... more results
    ],
    "total_results": 5,
    "truncated": false
}
```

**Docstring (Agent-Focused):**
```python
"""Search for available ComfyUI node types (not workflow nodes).

This tool searches the library of installed node types that can be created
in workflows. Use this to discover what node types are available before
creating them with create_nodes().

DISTINCTION FROM find_node():
- find_node() searches nodes already IN your workflow
- node_library_search() searches node TYPES available to create

USE CASES:
- "What node types handle upscaling?" → search output_type="IMAGE", query="upscale"
- "Show samplers" → search category="sampling"
- "What accepts LATENT?" → search input_type="LATENT"
- "Find LoRA loaders" → search query="lora"

RETURNS:
Array of matching node type definitions with inputs, outputs, categories.
Use node_library_get_details() for comprehensive info on a specific type.
"""
```

---

### 2. `node_library_get_details()` - Get Specific Node Type Info

**Purpose:** Get comprehensive details about a specific node type before creating it.

**Use Cases (Precise for Agent):**
- "What parameters does KSampler take?" → get_details("KSampler")
- "How do I use CheckpointLoaderSimple?" → get_details("CheckpointLoaderSimple")
- "What are the inputs for VAEDecode?" → get_details("VAEDecode")

**Request Model:**
```python
class NodeLibraryGetDetailsRequest(BaseModel):
    """Get detailed information about a specific node type."""
    node_type: str = Field(
        ...,
        description="Exact node type name (e.g., 'KSampler', 'CheckpointLoaderSimple')"
    )
```

**Response Structure:**
```python
{
    "node_type": "KSampler",
    "display_name": "KSampler",
    "category": "sampling",
    "description": "Basic sampler for latent diffusion",
    "inputs": {
        "required": {
            "model": ["MODEL"],
            "positive": ["CONDITIONING"],
            "negative": ["CONDITIONING"],
            "latent_image": ["LATENT"],
            "seed": ["INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}],
            "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
            "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}],
            "sampler_name": [["euler", "euler_ancestral", "heun", ...]],
            "scheduler": [["normal", "karras", "exponential", ...]],
            "denoise": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}]
        },
        "optional": {}
    },
    "outputs": ["LATENT"],
    "output_names": ["latent"],
    "input_order": ["model", "positive", "negative", "latent_image", "seed", ...]
}
```

**Docstring (Agent-Focused):**
```python
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
```

---

### 3. `node_library_find_compatible()` - Find Type-Compatible Node Types

**Purpose:** Discover node types that can connect to/from a given node type based on type compatibility.

**Use Cases (Precise for Agent):**
- "What node types can I connect after KSampler?" → find_compatible("KSampler", direction="downstream")
- "What node types feed into VAEDecode?" → find_compatible("VAEDecode", direction="upstream")
- "Build a chain from checkpoint to image" → iteratively find compatible types

**Request Model:**
```python
class NodeLibraryFindCompatibleRequest(BaseModel):
    """Find node types compatible with a given node type."""
    node_type: str = Field(
        ...,
        description="Source node type name (e.g., 'KSampler')"
    )
    direction: Literal["downstream", "upstream", "both"] = Field(
        "downstream",
        description="downstream=what can connect AFTER, upstream=what can connect BEFORE"
    )
    output_slot: Optional[str] = Field(
        None,
        description="Specific output slot to match (downstream only, null=all outputs)"
    )
    input_slot: Optional[str] = Field(
        None,
        description="Specific input slot to match (upstream only, null=all inputs)"
    )
    max_results: int = Field(
        30,
        description="Maximum results per direction (default: 30)"
    )
```

**Response Structure:**
```python
{
    "source_node_type": "KSampler",
    "direction": "downstream",
    "compatible_nodes": [
        {
            "node_type": "VAEDecode",
            "display_name": "VAE Decode",
            "category": "latent",
            "connection": {
                "source_output": "LATENT",
                "target_input": "samples",
                "data_type": "LATENT"
            },
            "description": "Decode latent to image"
        },
        {
            "node_type": "LatentUpscale",
            "display_name": "Upscale Latent",
            "category": "latent",
            "connection": {
                "source_output": "LATENT",
                "target_input": "samples",
                "data_type": "LATENT"
            },
            "description": "Upscale latent representation"
        }
    ],
    "total_compatible": 12,
    "truncated": false
}
```

**Docstring (Agent-Focused):**
```python
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
```

---

## Technical Implementation Details

### HTTP Client Setup

**Library:** Use `httpx` for async HTTP requests (install if not already present)

**Why httpx:**
- ✅ Async/await support (matches FastMCP async architecture)
- ✅ Modern API similar to `requests`
- ✅ Connection pooling and timeout handling
- ✅ Type hints and excellent error handling

**Installation:**
```bash
pip install httpx
```

**Add to imports in `mcp_server.py`:**
```python
import httpx
from typing import Optional, Dict, Any, List, Literal
```

---

### ComfyUI Server URL Configuration

**Add to `backend/config.py`:**
```python
class Settings(BaseSettings):
    # ... existing settings ...
    
    # ComfyUI Server
    comfyui_server_url: str = "http://127.0.0.1:8188"
    comfyui_api_timeout: int = 10  # seconds
```

**Add to `.env` (optional override):**
```bash
COMFYUI_SERVER_URL=http://127.0.0.1:8188
COMFYUI_API_TIMEOUT=10
```

---

### Node Library Cache Implementation

**Why Cache:**
- Node library doesn't change frequently (only on ComfyUI restart)
- Reduces redundant HTTP calls
- Improves response time
- Reduces ComfyUI server load

**Cache Strategy:**
```python
class NodeLibraryCache:
    """Cache for ComfyUI node library data."""
    
    def __init__(self, ttl_seconds: int = 300):  # 5 minute default TTL
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: Optional[float] = None
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
    
    async def get(self) -> Optional[Dict[str, Any]]:
        """Get cached data if valid."""
        async with self._lock:
            if self._cache is None:
                return None
            
            import time
            age = time.time() - self._cache_time
            if age > self._ttl:
                logger.debug(f"[NodeLibrary] Cache expired (age: {age:.1f}s)")
                return None
            
            logger.debug(f"[NodeLibrary] Cache hit (age: {age:.1f}s)")
            return self._cache
    
    async def set(self, data: Dict[str, Any]):
        """Set cache data."""
        async with self._lock:
            import time
            self._cache = data
            self._cache_time = time.time()
            logger.debug(f"[NodeLibrary] Cache updated ({len(data)} nodes)")
    
    async def invalidate(self):
        """Clear cache."""
        async with self._lock:
            self._cache = None
            self._cache_time = None
            logger.debug("[NodeLibrary] Cache invalidated")

# Global cache instance
_node_library_cache = NodeLibraryCache(ttl_seconds=300)
```

---

### Core Helper Function

**Purpose:** Fetch node library from ComfyUI with caching

```python
async def _fetch_node_library() -> Dict[str, Any]:
    """Fetch node library from ComfyUI /object_info endpoint.
    
    Returns:
        Dictionary mapping node type names to node metadata
        
    Raises:
        RuntimeError: If ComfyUI server is unreachable or returns error
    """
    # Check cache first
    cached = await _node_library_cache.get()
    if cached is not None:
        return cached
    
    # Fetch from ComfyUI
    from config import settings
    url = f"{settings.comfyui_server_url}/object_info"
    
    try:
        async with httpx.AsyncClient(timeout=settings.comfyui_api_timeout) as client:
            logger.info(f"[NodeLibrary] Fetching from {url}")
            response = await client.get(url)
            response.raise_for_status()
            
            data = response.json()
            
            # Validate response structure
            if not isinstance(data, dict):
                raise RuntimeError(f"Invalid response from ComfyUI: expected dict, got {type(data)}")
            
            logger.info(f"[NodeLibrary] Fetched {len(data)} node types")
            
            # Cache the result
            await _node_library_cache.set(data)
            
            return data
            
    except httpx.TimeoutException:
        raise RuntimeError(
            f"ComfyUI server timeout. Is ComfyUI running at {settings.comfyui_server_url}?"
        )
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"ComfyUI server error: {e.response.status_code} {e.response.reason_phrase}"
        )
    except httpx.RequestError as e:
        raise RuntimeError(
            f"Failed to connect to ComfyUI server at {settings.comfyui_server_url}: {e}"
        )
    except Exception as e:
        logger.error(f"[NodeLibrary] Unexpected error: {e}")
        raise RuntimeError(f"Failed to fetch node library: {e}")
```

---

### Search Implementation Logic

**Text Search Strategy:**
```python
def _matches_text_query(node_type: str, node_info: Dict[str, Any], query: str) -> bool:
    """Check if node matches text query."""
    query_lower = query.lower()
    
    # Search in node type name
    if query_lower in node_type.lower():
        return True
    
    # Search in display name
    display_name = node_info.get('display_name', '')
    if query_lower in display_name.lower():
        return True
    
    # Search in description (if available)
    description = node_info.get('description', '')
    if query_lower in description.lower():
        return True
    
    # Search in category
    category = node_info.get('category', '')
    if query_lower in category.lower():
        return True
    
    return False
```

**Type Matching Strategy:**
```python
def _has_input_type(node_info: Dict[str, Any], input_type: str) -> bool:
    """Check if node has input of specified type."""
    inputs = node_info.get('input', {})
    required = inputs.get('required', {})
    optional = inputs.get('optional', {})
    
    # Check all input parameters
    for param_name, param_spec in {**required, **optional}.items():
        if isinstance(param_spec, list) and len(param_spec) > 0:
            param_type = param_spec[0]
            if param_type == input_type:
                return True
    
    return False

def _has_output_type(node_info: Dict[str, Any], output_type: str) -> bool:
    """Check if node has output of specified type."""
    outputs = node_info.get('output', [])
    return output_type in outputs
```

**Category Matching:**
```python
def _matches_category(node_info: Dict[str, Any], category: str) -> bool:
    """Check if node belongs to category (supports partial match)."""
    node_category = node_info.get('category', '').lower()
    category_lower = category.lower()
    
    # Exact match or starts with (for subcategories like "image/upscaling")
    return node_category == category_lower or node_category.startswith(category_lower + '/')
```

---

### Compatibility Finding Logic

**Downstream (What Can Connect After):**
```python
def _find_downstream_compatible(
    source_node_info: Dict[str, Any],
    all_nodes: Dict[str, Any],
    output_slot: Optional[str] = None,
    max_results: int = 30
) -> List[Dict[str, Any]]:
    """Find node types that can accept outputs from source node."""
    
    # Get source outputs
    source_outputs = source_node_info.get('output', [])
    if not source_outputs:
        return []
    
    # If specific output slot requested, filter to that type
    if output_slot is not None:
        output_names = source_node_info.get('output_name', [])
        if output_slot in output_names:
            idx = output_names.index(output_slot)
            if idx < len(source_outputs):
                source_outputs = [source_outputs[idx]]
        else:
            # Try as index
            try:
                idx = int(output_slot)
                if 0 <= idx < len(source_outputs):
                    source_outputs = [source_outputs[idx]]
            except (ValueError, IndexError):
                pass
    
    compatible = []
    
    # Search all nodes for compatible inputs
    for node_type, node_info in all_nodes.items():
        inputs = node_info.get('input', {}).get('required', {})
        
        for input_name, input_spec in inputs.items():
            if isinstance(input_spec, list) and len(input_spec) > 0:
                input_type = input_spec[0]
                
                # Check if any source output matches this input type
                if input_type in source_outputs:
                    compatible.append({
                        'node_type': node_type,
                        'display_name': node_info.get('display_name', node_type),
                        'category': node_info.get('category', ''),
                        'connection': {
                            'source_output': input_type,
                            'target_input': input_name,
                            'data_type': input_type
                        },
                        'description': node_info.get('description', '')
                    })
                    break  # Only add once per node type
        
        if len(compatible) >= max_results:
            break
    
    return compatible
```

**Upstream (What Can Connect Before):**
```python
def _find_upstream_compatible(
    target_node_info: Dict[str, Any],
    all_nodes: Dict[str, Any],
    input_slot: Optional[str] = None,
    max_results: int = 30
) -> List[Dict[str, Any]]:
    """Find node types that can provide inputs to target node."""
    
    # Get target inputs
    target_inputs = target_node_info.get('input', {}).get('required', {})
    if not target_inputs:
        return []
    
    # If specific input slot requested, filter to that
    if input_slot is not None:
        if input_slot in target_inputs:
            target_inputs = {input_slot: target_inputs[input_slot]}
        else:
            return []  # Requested slot doesn't exist
    
    # Collect required input types
    required_types = set()
    for input_name, input_spec in target_inputs.items():
        if isinstance(input_spec, list) and len(input_spec) > 0:
            required_types.add(input_spec[0])
    
    if not required_types:
        return []
    
    compatible = []
    
    # Search all nodes for compatible outputs
    for node_type, node_info in all_nodes.items():
        outputs = node_info.get('output', [])
        
        # Check if any output matches required input types
        for output_type in outputs:
            if output_type in required_types:
                # Find which input this satisfies
                for input_name, input_spec in target_inputs.items():
                    if isinstance(input_spec, list) and input_spec[0] == output_type:
                        compatible.append({
                            'node_type': node_type,
                            'display_name': node_info.get('display_name', node_type),
                            'category': node_info.get('category', ''),
                            'connection': {
                                'source_output': output_type,
                                'target_input': input_name,
                                'data_type': output_type
                            },
                            'description': node_info.get('description', '')
                        })
                        break
                break
        
        if len(compatible) >= max_results:
            break
    
    return compatible
```

---

## Error Handling Strategy

### Common Error Scenarios

**1. ComfyUI Server Not Running**
```python
# Error message:
"ComfyUI server not reachable at http://127.0.0.1:8188. 
 Is ComfyUI running? Check COMFYUI_SERVER_URL in config."
```

**2. Invalid Node Type**
```python
# For node_library_get_details() and node_library_find_compatible()
if node_type not in node_library:
    # Suggest similar node types
    similar = _find_similar_node_types(node_type, node_library, max_suggestions=5)
    raise RuntimeError(
        f"Node type '{node_type}' not found in ComfyUI installation.\n"
        f"Did you mean one of these? {', '.join(similar)}"
    )
```

**3. Empty Search Results**
```python
# Return helpful message in response
{
    "query": {...},
    "results": [],
    "total_results": 0,
    "message": "No node types match your search criteria. Try:
                - Broader query terms
                - Different category
                - Check spelling of type names"
}
```

**4. Network Timeout**
```python
# Caught in _fetch_node_library()
"ComfyUI server timeout after 10s. Server may be overloaded or unreachable."
```

---

## Testing Strategy

### Manual Testing Checklist

**Prerequisites:**
- ComfyUI running at default URL (http://127.0.0.1:8188)
- At least some custom nodes installed for variety

**Test Cases:**

1. **`node_library_search()` - Text Query**
   ```python
   # Search for "sampler"
   {"query": "sampler"}
   # Expected: KSampler, KSamplerAdvanced, SamplerCustom, etc.
   ```

2. **`node_library_search()` - Category Filter**
   ```python
   # Search sampling category
   {"category": "sampling"}
   # Expected: All sampling nodes
   ```

3. **`node_library_search()` - Type Filter**
   ```python
   # Find nodes that output IMAGE
   {"output_type": "IMAGE"}
   # Expected: VAEDecode, ImageUpscale, SaveImage, etc.
   ```

4. **`node_library_search()` - Combined Filters**
   ```python
   # Find image upscaling nodes
   {"query": "upscale", "output_type": "IMAGE"}
   # Expected: ImageUpscaleWithModel, LatentUpscale (after decode), etc.
   ```

5. **`node_library_get_details()` - Valid Node**
   ```python
   {"node_type": "KSampler"}
   # Expected: Full parameter specs, inputs, outputs
   ```

6. **`node_library_get_details()` - Invalid Node**
   ```python
   {"node_type": "NonExistentNode"}
   # Expected: Error with suggestions
   ```

7. **`node_library_find_compatible()` - Downstream**
   ```python
   {"node_type": "KSampler", "direction": "downstream"}
   # Expected: VAEDecode, LatentUpscale, etc. (accepts LATENT)
   ```

8. **`node_library_find_compatible()` - Upstream**
   ```python
   {"node_type": "KSampler", "direction": "upstream"}
   # Expected: CheckpointLoader, LoraLoader, CLIPTextEncode, etc.
   ```

9. **Cache Behavior**
   ```python
   # First call: should fetch from ComfyUI (check logs)
   # Second call (within 5 min): should use cache (check logs)
   # After 5 min: should re-fetch
   ```

10. **ComfyUI Unreachable**
    ```python
    # Stop ComfyUI, then call any tool
    # Expected: Clear error message about server unreachable
    ```

---

## Implementation Checklist

### Phase 1: Foundation (30-45 min)

- [ ] **Install httpx dependency**
  ```bash
  pip install httpx
  ```

- [ ] **Update `backend/config.py`**
  - [ ] Add `comfyui_server_url` setting
  - [ ] Add `comfyui_api_timeout` setting

- [ ] **Add to `backend/mcp_server.py`**
  - [ ] Import httpx
  - [ ] Import time (for cache)
  - [ ] Add NodeLibraryCache class
  - [ ] Create global cache instance
  - [ ] Implement `_fetch_node_library()` helper

### Phase 2: Request Models (15-20 min)

- [ ] **Add request models to `backend/mcp_server.py`**
  - [ ] `NodeLibrarySearchRequest`
  - [ ] `NodeLibraryGetDetailsRequest`
  - [ ] `NodeLibraryFindCompatibleRequest`

### Phase 3: Helper Functions (30-45 min)

- [ ] **Add search/filter helpers**
  - [ ] `_matches_text_query()`
  - [ ] `_has_input_type()`
  - [ ] `_has_output_type()`
  - [ ] `_matches_category()`
  - [ ] `_find_similar_node_types()` (for error suggestions)

- [ ] **Add compatibility helpers**
  - [ ] `_find_downstream_compatible()`
  - [ ] `_find_upstream_compatible()`

### Phase 4: Tool Implementation (45-60 min)

- [ ] **Implement `node_library_search()`**
  - [ ] Fetch node library
  - [ ] Apply filters
  - [ ] Format results
  - [ ] Add match reasons
  - [ ] Handle empty results

- [ ] **Implement `node_library_get_details()`**
  - [ ] Fetch node library
  - [ ] Lookup node type
  - [ ] Format response
  - [ ] Handle not found with suggestions

- [ ] **Implement `node_library_find_compatible()`**
  - [ ] Fetch node library
  - [ ] Lookup source node
  - [ ] Find compatible based on direction
  - [ ] Format results
  - [ ] Handle invalid node type

### Phase 5: Testing & Refinement (30-45 min)

- [ ] **Test all tools with ComfyUI running**
  - [ ] Test search with various filters
  - [ ] Test get_details with valid/invalid nodes
  - [ ] Test find_compatible in both directions
  - [ ] Verify cache behavior
  - [ ] Test error handling (stop ComfyUI)

- [ ] **Refine error messages**
  - [ ] Clear, actionable error messages
  - [ ] Helpful suggestions for common mistakes

- [ ] **Update documentation**
  - [ ] Verify docstrings are agent-focused
  - [ ] Add examples to docstrings if needed

### Total Estimated Time: 2.5 - 3.5 hours

---

## Code Location in `backend/mcp_server.py`

**Where to Add:**

Insert after the existing ComfyUI tools section (after `comfy_search_resources()`) and before the Error Feedback section:

```python
# ============================================================================
# COMFYUI EXTENDED TOOLS
# ============================================================================

@mcp.tool()
async def comfy_list_folders(...):
    ...

@mcp.tool()
async def comfy_read_file(...):
    ...

@mcp.tool()
async def comfy_search_resources(...):
    ...

# ============================================================================
# COMFYUI NODE LIBRARY DISCOVERY TOOLS
# ============================================================================

# Cache for node library data
_node_library_cache = NodeLibraryCache(ttl_seconds=300)

async def _fetch_node_library() -> Dict[str, Any]:
    """Fetch node library from ComfyUI /object_info endpoint."""
    ...

# Helper functions for search/filtering
def _matches_text_query(...):
    ...

# Tool implementations
@mcp.tool()
async def node_library_search(...):
    ...

@mcp.tool()
async def node_library_get_details(...):
    ...

@mcp.tool()
async def node_library_find_compatible(...):
    ...

# ============================================================================
# ERROR FEEDBACK & QUEUE STATUS TOOLS
# ============================================================================
```

---

## Future Enhancements (Post-MVP)

### Phase 2 Features (Not in Initial Implementation)

1. **Semantic Search**
   - "Find nodes for image upscaling" → NLP matching
   - Build capability index from descriptions
   - Fuzzy matching for typos

2. **Cross-Reference with Filesystem**
   - Link node types to their source packages
   - Extract documentation from README files
   - Show example workflows using specific nodes

3. **Node Usage Statistics**
   - Track which nodes are commonly used together
   - Suggest popular workflow patterns
   - Rank results by popularity

4. **Manual Cache Refresh Tool**
   ```python
   @mcp.tool()
   async def node_library_refresh_cache():
       """Force refresh of node library cache."""
       await _node_library_cache.invalidate()
       await _fetch_node_library()
   ```

5. **Node Library Statistics**
   ```python
   @mcp.tool()
   async def node_library_stats():
       """Get statistics about installed node library."""
       # Total nodes, categories, custom vs core, etc.
   ```

---

## Dependencies

**New Dependencies:**
```txt
httpx>=0.27.0  # Async HTTP client
```

**Existing Dependencies (Already in Project):**
- `fastmcp` - MCP server framework
- `pydantic` - Request/response models
- `asyncio` - Async support

---

## Configuration Summary

**`.env` additions (optional):**
```bash
# ComfyUI Server Configuration
COMFYUI_SERVER_URL=http://127.0.0.1:8188
COMFYUI_API_TIMEOUT=10
```

**`backend/config.py` additions:**
```python
class Settings(BaseSettings):
    # ... existing settings ...
    
    # ComfyUI Server
    comfyui_server_url: str = "http://127.0.0.1:8188"
    comfyui_api_timeout: int = 10  # seconds
```

---

## Success Criteria

**Implementation Complete When:**

1. ✅ All 3 tools implemented and callable
2. ✅ Cache working correctly (logs show cache hits/misses)
3. ✅ All test cases pass
4. ✅ Error handling graceful and informative
5. ✅ Docstrings clear and agent-focused
6. ✅ No token bloat (responses focused, not entire library)
7. ✅ Performance acceptable (<2s for cached, <5s for uncached)

**Agent Can Successfully:**
- Search for node types by various criteria
- Get detailed specs before creating nodes
- Discover compatible node types for workflow building
- Understand errors and recover gracefully

---

## Notes

- **Token Efficiency:** By avoiding `get_node_library()` and focusing on targeted search/lookup, we keep responses small and costs low
- **Naming Clarity:** The `node_library_` prefix clearly distinguishes these from workflow manipulation tools
- **Cache Strategy:** 5-minute TTL balances freshness with performance (nodes rarely change)
- **Error UX:** Focus on helpful error messages that guide agents to correct usage
- **Async All The Way:** httpx + async/await matches FastMCP's async architecture

---

## Ready for Implementation?

This plan provides:
- ✅ Clear tool specifications with precise use cases
- ✅ Complete implementation details
- ✅ Error handling strategy
- ✅ Testing checklist
- ✅ Time estimates
- ✅ Code organization guidance

**Next Step:** Transition to **Implementation Mode** and start coding! 🚀
