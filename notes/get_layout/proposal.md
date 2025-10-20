# get_layout() Tool Proposal

## Overview
Proposal to replace `get_node_rect()` with `get_layout()` - a batch operation that retrieves position and size information for all nodes (or a subset) in the workflow.

## Current State

### Existing Tools
1. **`get_node_rect()`** - `backend/mcp_server.py` (~line 818)
   - Gets rect for a single node
   - Single WebSocket call per node
   
2. **`modify_layout()`** - `backend/mcp_server.py` (~line 826)
   - Batch operation for setting multiple node rects
   - Accepts `BatchLayoutRequest` with list of `SetNodeRectRequest`
   - Loops through and calls `set_node_rect` for each

3. **`getRect()`** - `web/js/fl_api.js` (~line 798)
   - Frontend function to get rect from single node
   - Accesses `node.pos[]` and `node.size[]` directly

### Frontend Data Access
- `app.graph._nodes` - Array of all workflow nodes
- Each node has:
  - `id` - Node identifier
  - `title` - Node title
  - `type` / `comfyClass` - Node type
  - `pos[0]`, `pos[1]` - X, Y position
  - `size[0]`, `size[1]` - Width, Height

## Proposed Solution

### Design Decision: Frontend Batch Collection (Option A)

**Why this approach:**
1. **Frontend is authoritative** - Canvas state lives in frontend
2. **Single WebSocket round-trip** - All data in one response
3. **Matches existing pattern** - Similar to `modify_layout()` batch structure
4. **Direct data access** - No need to loop through individual calls
5. **Efficient** - Leverages `app.graph._nodes` array that's already in memory

**Alternative rejected (Option B - Backend Loop):**
- Would require N WebSocket calls for N nodes
- Backend would need to first get list of all node IDs
- Adds unnecessary complexity and latency
- Doesn't leverage frontend's direct graph access

## Implementation Plan

### 1. Backend: `backend/mcp_server.py`

```python
class GetLayoutRequest(BaseModel):
    """Request to get layout for all nodes or specific nodes."""
    node_ids: Optional[List[Union[int, str]]] = Field(
        None, 
        description="Optional list of node IDs to get rects for (null for all nodes)"
    )

@mcp.tool()
async def get_layout(request: GetLayoutRequest, ctx: Context) -> Dict[str, Any]:
    """Get position and size for all nodes (or specified nodes) in the workflow.
    
    Returns layout information for the entire workflow at once, useful for:
    - Understanding overall workflow spatial organization
    - Calculating new layouts before applying them
    - Detecting overlaps or spacing issues
    - Exporting workflow layout data
    
    Returns:
        {
            "nodes": [
                {
                    "node_id": int,
                    "title": str,
                    "type": str,
                    "rect": {"x": float, "y": float, "width": float, "height": float}
                },
                ...
            ],
            "count": int
        }
    """
    return await _execute_tool(ctx, "get_layout", request.model_dump())
```

### 2. Frontend API: `web/js/fl_api.js`

```javascript
/**
 * Get layout (rects) for all nodes or specific nodes
 * @param {Array<number|string>|null} nodeIds - Optional array of node IDs (null for all)
 * @returns {object} {nodes: Array<object>, count: number}
 */
getLayout(nodeIds = null) {
    try {
        const nodes = nodeIds 
            ? nodeIds.map(id => this._findNode(id)).filter(n => n !== null)
            : app.graph._nodes;
        
        const layout = nodes.map(node => ({
            node_id: node.id,
            title: node.title,
            type: node.comfyClass || node.type,
            rect: {
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1]
            }
        }));
        
        console.log(`[FL_API] Got layout for ${layout.length} node(s)`);
        return { nodes: layout, count: layout.length };
    } catch (error) {
        console.error("[FL_API] getLayout error:", error);
        throw error;
    }
}
```

### 3. Tool Executor: `web/js/tool_executor.js`

```javascript
// In constructor handlers object:
"get_layout": this._handleGetLayout.bind(this),

// Add handler method:
async _handleGetLayout(params) {
    const { node_ids } = params;
    return this.flApi.getLayout(node_ids);
}
```

## Use Cases

1. **Workflow Analysis**
   - Get spatial organization of entire workflow
   - Calculate bounding box of workflow
   - Detect overlapping nodes

2. **Layout Optimization**
   - Retrieve current layout before calculating new positions
   - Find spacing gaps or clusters
   - Prepare data for auto-layout algorithms

3. **Export/Documentation**
   - Export workflow layout data
   - Generate layout diagrams
   - Save layout presets

4. **Debugging**
   - Inspect node positions during development
   - Verify layout changes
   - Compare layouts before/after operations

## Migration from get_node_rect()

### Deprecation Strategy
1. Implement `get_layout()` alongside `get_node_rect()`
2. Update documentation to recommend `get_layout()` for batch operations
3. Eventually deprecate `get_node_rect()` or keep for single-node convenience

### Backward Compatibility
- `get_node_rect()` can remain for single-node queries
- Or it can be reimplemented as: `get_layout({node_ids: [id]}).nodes[0]`

## Pattern Consistency

This follows the established pattern in the codebase:

| Operation | Single | Batch |
|-----------|--------|-------|
| **Get** | `get_node_rect()` | `get_layout()` |
| **Set** | `set_node_rect()` | `modify_layout()` |

Both batch operations:
- Accept optional node ID lists
- Handle collection/iteration on appropriate side (frontend for data, backend for orchestration)
- Return structured results with metadata

## Next Steps

1. ✅ Document proposal
2. 🔄 Deep investigation of code patterns
3. ⏳ Implement backend changes
4. ⏳ Implement frontend changes
5. ⏳ Test with various workflows
6. ⏳ Update documentation
