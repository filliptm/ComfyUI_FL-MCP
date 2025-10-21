# ComfyUI Manager - Node Filter Refinement

**Date:** 2025-10-20  
**Project:** fl_js  
**Parent:** [final_implementation.md](./final_implementation.md)  
**Issue:** Search results can be overwhelming when packs contain hundreds of nodes  
**Solution:** Add `node_filter` parameter to filter packs by specific node names

---

## Problem Statement

When searching for node packs:
- Some packs contain **hundreds of nodes** (e.g., ComfyUI core nodes)
- Agent needs to find "which pack has node X?"
- Current search only filters by pack metadata (name, description, author, category)
- No way to filter by **node class names** within packs

## Solution Design

### Add `node_filter` Parameter

**Purpose:** Filter node packs by node class names they contain

**Approach:**
1. Use the existing `/customnode/getmappings` endpoint data
2. The mappings tell us: `{"NodeClassName": ["pack_id", ...], ...}`
3. Apply regex pattern matching on node class names
4. Only return packs that contain matching nodes
5. Add `matched_nodes` field to results showing which nodes matched

**Benefits:**
- Reduces result size dramatically
- Answers "which pack has KSampler?" type questions
- Works with regex for flexible matching
- No additional API calls (mappings already cached)

---

## Implementation

### Step 1: Update NodePackInfo Data Class

**File:** `backend/comfy_manager.py`  
**Location:** Line ~50 (in NodePackInfo dataclass)

**Before:**
```python
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
```

**After:**
```python
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
```

---

### Step 2: Update search_node_packs Method

**File:** `backend/comfy_manager.py`  
**Location:** Line ~290 (search_node_packs method)

**Replace the entire method with:**

```python
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
```

---

### Step 3: Update ManagerSearchNodesRequest Model

**File:** `backend/mcp_server.py`  
**Location:** Line ~300 (ManagerSearchNodesRequest class)

**Before:**
```python
class ManagerSearchNodesRequest(BaseModel):
    """Search for custom node packs in ComfyUI Manager."""
    query: Optional[str] = Field(None, description="Search query for node pack name/description/author")
    category: Optional[str] = Field(None, description="Filter by category")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates available")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(20, ge=1, le=100, description="Maximum results to return")
```

**After:**
```python
class ManagerSearchNodesRequest(BaseModel):
    """Search for custom node packs in ComfyUI Manager."""
    query: Optional[str] = Field(None, description="Search query for node pack name/description/author")
    category: Optional[str] = Field(None, description="Filter by category")
    node_filter: Optional[str] = Field(None, description="Regex pattern to filter by node class names (e.g., 'KSampler', 'FL_.*', 'Image.*Saver')")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates available")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(20, ge=1, le=100, description="Maximum results to return")
```

---

### Step 4: Update manager_search_nodes Tool

**File:** `backend/mcp_server.py`  
**Location:** Line ~500 (manager_search_nodes tool function)

**Modify the tool call and docstring:**

**Replace the entire function with:**

```python
@mcp.tool()
async def manager_search_nodes(
    request: ManagerSearchNodesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search for custom node packs available through ComfyUI Manager.
    
    Use this tool to discover node packs by name, category, functionality, or specific nodes.
    Helps find and understand what node packs are available to install or already installed.
    
    WHEN TO USE:
    - "What node packs handle image upscaling?" → query="upscale"
    - "Show me animation node packs" → category="animation"
    - "Which pack has KSampler?" → node_filter="KSampler"
    - "Find FL nodes" → node_filter="FL_.*"
    - "What's installed?" → installed_only=True
    - "What can I update?" → updates_available=True
    - "Find packs by author" → query="author_name"
    
    FILTERS:
    - query: Text search across name, description, author
    - category: Filter by pack category
    - node_filter: Regex pattern to match node class names (RECOMMENDED for specific nodes)
    - installed_only: Only show installed packs
    - updates_available: Only show packs with updates
    - mode: "cache" (fast), "remote" (fresh), "local" (filesystem)
    
    NODE FILTER EXAMPLES:
    - "KSampler" → exact match
    - "FL_.*" → all FL nodes
    - "Image.*Saver" → ImageSaver, ImageBatchSaver, etc.
    - "(Load|Save)Image" → LoadImage or SaveImage
    
    RETURNS:
    Array of node pack objects with:
    - name, description, author, repository
    - installation status ("True", "False", "Update")
    - stars, last_update, category
    - files (download URLs)
    - matched_nodes (if node_filter used) - list of node class names that matched
    
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
            node_filter=request.node_filter,
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
                "files": pack.files,
                "matched_nodes": pack.matched_nodes  # Will be None if no node_filter
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
```

---

## Usage Examples

### Example 1: Find Pack for Specific Node

**Query:** "Which pack has the KSampler node?"

**Tool Call:**
```python
manager_search_nodes(
    node_filter="^KSampler$",  # Exact match
    max_results=5
)
```

**Result:**
```json
{
  "results": [
    {
      "id": "ComfyUI",
      "name": "ComfyUI Core",
      "matched_nodes": ["KSampler", "KSamplerAdvanced"],
      ...
    }
  ],
  "count": 1
}
```

### Example 2: Find All FL Nodes

**Query:** "Show me all Fill node packs"

**Tool Call:**
```python
manager_search_nodes(
    node_filter="FL_.*",  # All nodes starting with FL_
    max_results=20
)
```

**Result:**
```json
{
  "results": [
    {
      "id": "ComfyUI_Fill-Nodes",
      "name": "Fill Nodes",
      "matched_nodes": [
        "FL_ImageCaptionSaver",
        "FL_KsamplerBasic",
        "FL_SystemCheck",
        ...
      ],
      ...
    }
  ],
  "count": 1
}
```

### Example 3: Find Image Saver Nodes

**Query:** "Which packs have image saver nodes?"

**Tool Call:**
```python
manager_search_nodes(
    node_filter=".*Image.*Saver",  # Any node with Image and Saver
    max_results=10
)
```

**Result:**
```json
{
  "results": [
    {
      "id": "ComfyUI_Fill-Nodes",
      "name": "Fill Nodes",
      "matched_nodes": ["FL_ImageCaptionSaver", "FL_API_ImageSaver"],
      ...
    },
    {
      "id": "ComfyUI-Custom-Scripts",
      "name": "Custom Scripts",
      "matched_nodes": ["SaveImageWithMetadata"],
      ...
    }
  ],
  "count": 2
}
```

### Example 4: Combine Filters

**Query:** "Show installed packs with Load nodes"

**Tool Call:**
```python
manager_search_nodes(
    node_filter="Load.*",
    installed_only=True,
    max_results=20
)
```

---

## Benefits

### Before (Without node_filter)
- Search "image" → 50+ packs with "image" in description
- No way to find specific node types
- Agent must read through all descriptions
- Results often too broad

### After (With node_filter)
- Search `node_filter="KSampler"` → 1-2 packs, exact matches shown
- Directly answers "which pack has X node?"
- `matched_nodes` field shows exactly what matched
- Results focused and actionable

---

## Implementation Notes

### Performance
- Node mappings are already cached (TTL: 300s)
- Regex matching is fast (happens in memory)
- No additional API calls
- Filtering happens after cache lookup

### Error Handling
- Invalid regex → logged, continues without node filtering
- Missing mappings → returns empty results
- All existing error handling preserved

### Backwards Compatibility
- `node_filter` is optional (None by default)
- Existing tool calls work unchanged
- `matched_nodes` is None when no filter used
- No breaking changes

---

## Testing Checklist

- [ ] Search with `node_filter="KSampler"` → returns packs with KSampler
- [ ] Search with `node_filter="FL_.*"` → returns Fill Nodes pack
- [ ] Search with invalid regex → logs error, returns results without filter
- [ ] Search with no node_filter → works as before (matched_nodes=None)
- [ ] Combine node_filter + query → both filters apply
- [ ] Combine node_filter + installed_only → both filters apply
- [ ] matched_nodes field populated correctly
- [ ] max_results respected with node_filter

---

## Summary of Changes

### Files Modified

1. **`backend/comfy_manager.py`**
   - Add `matched_nodes` field to `NodePackInfo` dataclass (1 line)
   - Update `search_node_packs` method signature (1 line)
   - Add node filtering logic (~40 lines)
   - Total: ~42 lines changed

2. **`backend/mcp_server.py`**
   - Add `node_filter` to `ManagerSearchNodesRequest` (1 line)
   - Update `manager_search_nodes` docstring (~15 lines)
   - Pass `node_filter` to client call (1 line)
   - Add `matched_nodes` to result dict (1 line)
   - Total: ~18 lines changed

### Total Changes
- **Lines Modified:** ~60 lines
- **New Dependencies:** 0 (uses existing `re` module)
- **Breaking Changes:** 0 (fully backwards compatible)

---

## Categories Discovery (Bonus)

To discover available categories, agent can now:

```python
# Get all packs
results = manager_search_nodes(max_results=100)

# Extract unique categories
categories = set(pack["category"] for pack in results["results"] if pack["category"])
```

No need for dedicated tool - this pattern works fine.
