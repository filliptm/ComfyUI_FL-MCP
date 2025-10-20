# ComfyUI Node Library Research

**Research Date:** 2025-10-18  
**Research Goal:** Understand how to query ComfyUI's node library via Python API to enable agent-based node search/discovery  
**Target Implementation:** `backend/mcp_server.py` - add tools for node discovery and search

---

## Executive Summary

ComfyUI exposes its entire node library through the **`/object_info` HTTP API endpoint**, which returns comprehensive metadata about all installed nodes (both core and custom). This endpoint can be queried programmatically to:

1. **Discover all available nodes** in the ComfyUI installation
2. **Get detailed node specifications** including inputs, outputs, types, and defaults
3. **Enable agent-based node search** by node name, category, or capabilities
4. **Support workflow generation** by understanding node interfaces

**Key Finding:** We don't need to parse Python `NODE_CLASS_MAPPINGS` directly - ComfyUI already provides a REST API that aggregates this information!

---

## ComfyUI Node Discovery Architecture

### How ComfyUI Loads Custom Nodes

1. **Directory Scanning**: On startup, ComfyUI scans the `custom_nodes/` directory
2. **Module Discovery**: Identifies Python modules (directories with `__init__.py`)
3. **Import & Registration**: Imports each module and looks for `NODE_CLASS_MAPPINGS`
4. **Aggregation**: Merges custom nodes with core nodes into a unified registry

### NODE_CLASS_MAPPINGS Structure

```python
# In custom_nodes/YourNodePack/__init__.py
from .nodes import CustomNodeClass

NODE_CLASS_MAPPINGS = {
    "CustomNode": CustomNodeClass,        # Key = node type name
    "Another Node": AnotherNodeClass      # Value = Python class
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CustomNode": "Custom Node Display Name"
}
```

**Key Properties:**
- Must be a Python `dict`
- Keys are node type strings (must be unique across entire ComfyUI installation)
- Values are Python classes implementing the node
- Exposed at module level in `__init__.py`
- Requires ComfyUI restart to recognize changes

---

## The `/object_info` API Endpoint

### Overview

ComfyUI's HTTP server exposes node metadata through a RESTful API:

**Endpoint:** `GET /object_info`  
**Default URL:** `http://127.0.0.1:8188/object_info`  
**Purpose:** Returns comprehensive information about all available nodes

### API Usage

#### Get All Nodes
```bash
curl http://127.0.0.1:8188/object_info
```

#### Get Specific Node
```bash
curl http://127.0.0.1:8188/object_info/KSampler
```

### Response Structure

The endpoint returns a JSON object where:
- **Keys** = Node type names (e.g., `"KSampler"`, `"CheckpointLoaderSimple"`)
- **Values** = Node metadata objects

#### Node Metadata Schema

Each node object includes:

```json
{
  "NodeTypeName": {
    "input": {
      "required": {
        "param_name": ["TYPE", {"default": value}],
        "model": ["MODEL"],
        "seed": ["INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}]
      },
      "optional": {
        "optional_param": ["TYPE", {"default": value}]
      }
    },
    "output": ["LATENT", "IMAGE"],
    "output_name": ["latent", "image"],
    "category": "sampling",
    "display_name": "KSampler",
    "description": "Node description if available"
  }
}
```

**Field Breakdown:**
- `input.required`: Required input parameters with type and constraints
- `input.optional`: Optional input parameters
- `output`: Array of output types (e.g., `["LATENT", "IMAGE"]`)
- `output_name`: Human-readable names for outputs
- `category`: UI menu category (e.g., `"sampling"`, `"loaders"`)
- `display_name`: UI display name
- `description`: Documentation (if provided by node author)

### Integration with FL_JS

We can query this endpoint from our MCP server using standard HTTP requests:

```python
import requests

def get_all_nodes(comfyui_url="http://127.0.0.1:8188"):
    """Fetch all node definitions from ComfyUI."""
    response = requests.get(f"{comfyui_url}/object_info")
    return response.json()

def search_nodes_by_category(category: str):
    """Search for nodes in a specific category."""
    all_nodes = get_all_nodes()
    return {
        name: info 
        for name, info in all_nodes.items() 
        if info.get('category') == category
    }

def search_nodes_by_output_type(output_type: str):
    """Find nodes that produce a specific output type."""
    all_nodes = get_all_nodes()
    return {
        name: info 
        for name, info in all_nodes.items()
        if output_type in info.get('output', [])
    }
```

---

## Current FL_JS Capabilities

### Existing ComfyUI Tools (from `backend/comfy_tools.py`)

We already have filesystem-based discovery tools:

1. **`comfy_list_folders()`** - List custom node packages, models, etc.
2. **`comfy_read_file()`** - Read node source code and documentation
3. **`comfy_search_resources()`** - Grep-style search through ComfyUI files

**Current Limitation:** These tools work at the filesystem level but don't provide structured node metadata.

### What's Missing

Agent-friendly node discovery tools that provide:
- ✅ **Structured node metadata** (inputs, outputs, types)
- ✅ **Searchable by capability** ("find nodes that upscale images")
- ✅ **Type compatibility checking** ("what nodes accept LATENT input?")
- ✅ **Category browsing** ("show all sampling nodes")

---

## Proposed Implementation

### New MCP Tools for `backend/mcp_server.py`

#### 1. `get_node_library()` - Get All Node Definitions

```python
class GetNodeLibraryRequest(BaseModel):
    """Request to get all available node definitions."""
    include_core: bool = Field(True, description="Include core ComfyUI nodes")
    include_custom: bool = Field(True, description="Include custom nodes")
    categories: Optional[List[str]] = Field(None, description="Filter by categories")

@mcp.tool()
async def get_node_library(request: GetNodeLibraryRequest, ctx: Context) -> Dict[str, Any]:
    """Get comprehensive library of all available ComfyUI nodes.
    
    Returns structured metadata for all installed nodes including:
    - Input parameters (required/optional) with types and constraints
    - Output types and names
    - Categories and display names
    - Documentation (if available)
    
    USE CASES:
    - Discover what nodes are available in this ComfyUI installation
    - Understand node interfaces before creating workflows
    - Find nodes by category (e.g., "sampling", "loaders", "image")
    - Check compatibility between nodes
    
    RETURNS:
    Dictionary mapping node type names to their metadata objects.
    Each node includes input specs, output types, category, and description.
    """
    # Query ComfyUI /object_info endpoint
    # Filter by categories if specified
    # Return structured node library
    pass
```

#### 2. `search_nodes()` - Intelligent Node Search

```python
class SearchNodesRequest(BaseModel):
    """Request to search for nodes by various criteria."""
    query: Optional[str] = Field(None, description="Text search in node names/descriptions")
    category: Optional[str] = Field(None, description="Filter by category")
    input_type: Optional[str] = Field(None, description="Nodes that accept this input type")
    output_type: Optional[str] = Field(None, description="Nodes that produce this output type")
    has_parameter: Optional[str] = Field(None, description="Nodes with specific parameter")
    max_results: int = Field(20, description="Maximum results to return")

@mcp.tool()
async def search_nodes(request: SearchNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Search for nodes by name, category, input/output types, or capabilities.
    
    This tool enables agents to discover nodes based on what they need to accomplish:
    - "Find nodes that can upscale images" → search by output_type="IMAGE"
    - "What nodes accept LATENT?" → search by input_type="LATENT"
    - "Show me all sampling nodes" → search by category="sampling"
    - "Find checkpoint loaders" → search by query="checkpoint"
    
    SEARCH STRATEGIES:
    - Text search: Matches node names, display names, and descriptions
    - Type matching: Finds nodes compatible with specific data types
    - Category filtering: Browses nodes by UI category
    - Parameter search: Finds nodes with specific parameters (e.g., "seed")
    
    RETURNS:
    Array of matching nodes with full metadata, ranked by relevance.
    Includes match reasons to help agents understand why nodes were returned.
    """
    pass
```

#### 3. `get_node_details()` - Get Specific Node Info

```python
class GetNodeDetailsRequest(BaseModel):
    """Request detailed information about a specific node."""
    node_type: str = Field(..., description="Node type name (e.g., 'KSampler')")
    include_examples: bool = Field(False, description="Include usage examples if available")

@mcp.tool()
async def get_node_details(request: GetNodeDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get comprehensive details about a specific node type.
    
    Returns everything an agent needs to know to use a node:
    - All input parameters with types, defaults, and constraints
    - All outputs with types and names  
    - Category and display information
    - Documentation and descriptions
    - Usage examples (if available)
    
    USE CASES:
    - Before creating a node: understand its interface
    - When debugging: verify parameter names and types
    - When connecting nodes: check type compatibility
    - Learning: understand what a node does
    """
    pass
```

#### 4. `find_compatible_nodes()` - Type-Based Discovery

```python
class FindCompatibleNodesRequest(BaseModel):
    """Request to find nodes compatible with a given node."""
    source_node_type: str = Field(..., description="Source node type")
    source_output_slot: Optional[str] = Field(None, description="Specific output slot")
    direction: Literal["downstream", "upstream", "both"] = Field(
        "downstream",
        description="Find nodes that can connect after (downstream) or before (upstream)"
    )

@mcp.tool()
async def find_compatible_nodes(request: FindCompatibleNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Find nodes that can connect to a given node based on type compatibility.
    
    This tool helps agents build workflows by discovering connection possibilities:
    - "What can I connect after a KSampler?" → Find nodes accepting LATENT
    - "What feeds into a VAEDecode?" → Find nodes outputting LATENT
    - "Build a chain from checkpoint to image" → Find compatible paths
    
    RETURNS:
    Categorized list of compatible nodes with connection details:
    - Which output connects to which input
    - Data types that match
    - Suggested connection patterns
    """
    pass
```

---

## Implementation Strategy

### Phase 1: Core Node Library Access

1. **Add HTTP client to MCP server**
   - Detect ComfyUI server URL (default: `http://127.0.0.1:8188`)
   - Add configuration for custom URLs
   - Handle connection errors gracefully

2. **Implement `get_node_library()` tool**
   - Query `/object_info` endpoint
   - Cache results (with TTL or manual refresh)
   - Parse and structure response

3. **Add basic search with `search_nodes()`**
   - Text search in node names/descriptions
   - Category filtering
   - Type-based filtering

### Phase 2: Advanced Discovery

4. **Implement `get_node_details()`**
   - Query specific node: `/object_info/{node_type}`
   - Enrich with filesystem data (README, examples)
   - Cross-reference with installed node packs

5. **Implement `find_compatible_nodes()`**
   - Build type compatibility graph
   - Suggest connection patterns
   - Rank by common usage patterns

### Phase 3: Enhanced Intelligence

6. **Semantic search capabilities**
   - "Find nodes for image upscaling" → Match categories + descriptions
   - "What nodes work with ControlNet?" → Type + name matching
   - Build node capability index

7. **Cross-reference with filesystem**
   - Combine `/object_info` with `comfy_search_resources()`
   - Link nodes to their source packages
   - Extract documentation from README files

---

## Technical Considerations

### ComfyUI Server Detection

The MCP server needs to know the ComfyUI server URL:

```python
# Options for detection:
1. Environment variable: COMFYUI_SERVER_URL
2. Configuration file: backend/config.py
3. Auto-detect from manager.py (if available)
4. Default fallback: http://127.0.0.1:8188
```

### Caching Strategy

Node library doesn't change frequently, so we should cache:

```python
class NodeLibraryCache:
    def __init__(self, ttl_seconds=300):  # 5 minute TTL
        self._cache = None
        self._cache_time = None
        self._ttl = ttl_seconds
    
    def get(self):
        if self._cache and (time.time() - self._cache_time < self._ttl):
            return self._cache
        return None
    
    def set(self, data):
        self._cache = data
        self._cache_time = time.time()
    
    def invalidate(self):
        self._cache = None
```

### Error Handling

```python
# Handle common failure scenarios:
1. ComfyUI server not running → Graceful error message
2. Network timeout → Retry with backoff
3. Invalid node type → Suggest similar nodes
4. Empty results → Provide search tips
```

### Type System

ComfyUI uses string-based types (e.g., `"IMAGE"`, `"LATENT"`, `"MODEL"`):

```python
# Common types to understand:
CORE_TYPES = {
    "IMAGE": "Image tensor (B,H,W,C)",
    "LATENT": "Latent representation",
    "MODEL": "Diffusion model",
    "CONDITIONING": "Text conditioning",
    "VAE": "VAE model",
    "CLIP": "CLIP model",
    "MASK": "Image mask",
    "INT": "Integer value",
    "FLOAT": "Float value",
    "STRING": "Text string"
}
```

---

## Example Use Cases

### Use Case 1: Agent Wants to Upscale an Image

```python
# Agent flow:
1. search_nodes(output_type="IMAGE", query="upscale")
   → Returns: ["ImageUpscaleWithModel", "UpscaleModelLoader", ...]

2. get_node_details(node_type="ImageUpscaleWithModel")
   → Returns: inputs={"image": IMAGE, "upscale_model": UPSCALE_MODEL}

3. find_compatible_nodes(source_node_type="VAEDecode", direction="downstream")
   → Confirms: VAEDecode outputs IMAGE, compatible with ImageUpscaleWithModel

4. create_nodes([{"node_type": "ImageUpscaleWithModel", ...}])
   → Creates the upscale node in workflow
```

### Use Case 2: Agent Explores Available Samplers

```python
1. search_nodes(category="sampling")
   → Returns: ["KSampler", "KSamplerAdvanced", "SamplerCustom", ...]

2. For each sampler:
   get_node_details(node_type=sampler)
   → Compare parameters, capabilities, complexity

3. Agent selects appropriate sampler based on requirements
```

### Use Case 3: Agent Builds Workflow from Scratch

```python
1. search_nodes(query="checkpoint", output_type="MODEL")
   → Start: CheckpointLoaderSimple

2. find_compatible_nodes(source_node_type="CheckpointLoaderSimple")
   → Next options: KSampler, LoraLoader, etc.

3. Continue building chain until reaching desired output (IMAGE)

4. Validate complete workflow has no type mismatches
```

---

## Alternative Approaches Considered

### ❌ Direct Python Import of NODE_CLASS_MAPPINGS

**Approach:** Import custom node modules directly and access `NODE_CLASS_MAPPINGS`

**Problems:**
- Requires running in same Python environment as ComfyUI
- Import side effects could break custom nodes
- Dependency conflicts
- Doesn't work if MCP server runs separately

**Verdict:** Not recommended - use HTTP API instead

### ❌ Filesystem-Only Discovery

**Approach:** Parse `__init__.py` files to extract `NODE_CLASS_MAPPINGS`

**Problems:**
- Fragile regex/AST parsing
- Doesn't capture runtime-registered nodes
- Misses core nodes
- No type information without executing code

**Verdict:** Useful as supplement, not primary method

### ✅ HTTP API (Chosen Approach)

**Approach:** Query `/object_info` endpoint

**Benefits:**
- ✅ Works regardless of MCP server location
- ✅ Gets complete, runtime-accurate node library
- ✅ Includes core + custom nodes
- ✅ Provides full type information
- ✅ Standard HTTP - easy to implement
- ✅ Can be cached for performance

**Verdict:** Best approach - use as primary method

---

## Next Steps

### Immediate Actions

1. **Add HTTP client to `backend/mcp_server.py`**
   - Install `aiohttp` or use `requests` for async HTTP
   - Add ComfyUI server URL configuration
   - Implement connection testing

2. **Implement `get_node_library()` tool**
   - Query `/object_info` endpoint
   - Add response caching
   - Write comprehensive docstring

3. **Test with real ComfyUI instance**
   - Verify endpoint response structure
   - Test with various custom nodes installed
   - Validate type information accuracy

### Future Enhancements

4. **Build search index**
   - Create inverted index for fast text search
   - Build type compatibility graph
   - Add fuzzy matching for node names

5. **Add semantic understanding**
   - Map common tasks to node patterns
   - Suggest workflow templates
   - Learn from user's workflow patterns

6. **Integration with existing tools**
   - Cross-reference `/object_info` with filesystem discovery
   - Link nodes to their source packages
   - Extract examples from node pack READMEs

---

## References

### Documentation
- [ComfyUI Custom Nodes Lifecycle](https://docs.comfy.org/custom-nodes/backend/lifecycle)
- [ComfyUI Custom Nodes Walkthrough](https://docs.comfy.org/custom-nodes/walkthrough)
- [ComfyUI Development Core Concepts](https://docs.comfy.org/development/core-concepts/custom-nodes)
- [ComfyUI API Routes](https://docs.comfy.org/development/comfyui-server/comms_routes)

### Articles & Tutorials
- [DevLog: ComfyUI API](https://dev.to/methodox/devlog-20250710-comfyui-api-1mi0)
- [ComfyUI WebSockets API Part 2](https://medium.com/@yushantripleseven/comfyui-websockets-api-part-2-0ab988acfd97)

### GitHub Issues
- [ComfyUI Issue #2110 - API Documentation](https://github.com/comfyanonymous/ComfyUI/issues/2110)
- [ComfyUI Issue #1037 - Need API Document](https://github.com/comfyanonymous/ComfyUI/issues/1037)

### Related Files in FL_JS
- `backend/mcp_server.py` - MCP server implementation (target for new tools)
- `backend/comfy_tools.py` - Existing filesystem-based ComfyUI utilities
- `backend/comfy_models.py` - Pydantic models for ComfyUI tools
- `backend/manager.py` - Backend manager (may have ComfyUI connection info)

---

## Conclusion

ComfyUI's `/object_info` HTTP API provides everything needed for comprehensive node discovery and search. By implementing MCP tools that query this endpoint, we can enable the FL_JS agent to:

1. **Discover available nodes** without filesystem parsing
2. **Search intelligently** by type, category, or capability
3. **Understand node interfaces** before creating them
4. **Build workflows programmatically** with type safety

This approach is robust, maintainable, and aligns with ComfyUI's API-first architecture. The implementation can start simple (basic library access) and evolve into sophisticated semantic search and workflow generation capabilities.

**Recommendation:** Proceed with implementing `get_node_library()` and `search_nodes()` tools in `backend/mcp_server.py` as the foundation for agent-driven node discovery.
