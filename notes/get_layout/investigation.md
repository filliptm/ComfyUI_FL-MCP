# get_layout() Implementation Investigation

## Code Pattern Analysis

This document provides a deep dive into the existing codebase patterns to ensure `get_layout()` implementation is solid and consistent.

---

## 1. Existing Layout Management Tools

### Backend: `backend/mcp_server.py`

#### Single Operations
```python
# Lines ~360-361
class GetNodeRectRequest(BaseModel):
    """Request to get node position and size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

# Lines ~363-368
class SetNodeRectRequest(BaseModel):
    """Request to set node position and/or size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    x: Optional[float] = Field(None, description="X position (null to keep current)")
    y: Optional[float] = Field(None, description="Y position (null to keep current)")
    width: Optional[float] = Field(None, description="Width (null to keep current)")
    height: Optional[float] = Field(None, description="Height (null to keep current)")
```

#### Batch Operations
```python
# Lines ~372-373
class BatchLayoutRequest(BaseModel):
    node_rects: List[SetNodeRectRequest] = Field(..., description="A List of nodes with their new rectangle settings for full or partial quick layout changes")
```

#### Tool Implementations
```python
# Lines ~815-818
@mcp.tool()
async def get_node_rect(request: GetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
    """Get node position and size."""
    return await _execute_tool(ctx, "get_node_rect", request.model_dump())

# Lines ~821-824
@mcp.tool()
async def set_node_rect(request: SetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
    """Set node position and/or size."""
    return await _execute_tool(ctx, "set_node_rect", request.model_dump())

# Lines ~826-832
@mcp.tool()
async def modify_layout(request: BatchLayoutRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Modify the layout of multiple nodes by setting their bounding boxes. Use this when moving many nodes at a time. Attempt to avoid overlaps."""
    o = []
    for rect in request.node_rects:
        o.append(await _execute_tool(ctx, "set_node_rect", rect.model_dump()))
    return o
```

**Key Pattern**: `modify_layout()` is a **backend orchestration** pattern:
- Accepts batch request
- Loops through items **on backend**
- Calls single operation tool for each item
- Aggregates results

---

## 2. Frontend Data Access Patterns

### Direct Graph Access: `web/js/fl_api.js`

#### Node Finding Pattern
```javascript
// Lines ~1272-1289
_findNode(query) {
    if (typeof query === "object" && query.id !== undefined) {
        return query;  // Already a node object
    }

    if (typeof query === "number") {
        // Find by ID
        return app.graph._nodes.find(n => n.id === query) || null;
    }

    if (typeof query === "string") {
        // Try as title first, then type
        return app.graph._nodes.find(n => n.title === query) ||
               app.graph._nodes.find(n => n.type === query || n.comfyClass === query) ||
               null;
    }

    return null;
}
```

**Key Pattern**: Uses `app.graph._nodes` array directly

#### Single Node Rect Access
```javascript
// Lines ~798-818
getRect(nodeId) {
    try {
        const node = this._findNode(nodeId);
        if (!node) {
            throw new Error(`Node not found: ${nodeId}`);
        }

        const rect = {
            x: node.pos[0],
            y: node.pos[1],
            width: node.size[0],
            height: node.size[1]
        };

        console.log(`[FL_API] Got rect for node ${node.id}`);
        return rect;
    } catch (error) {
        console.error("[FL_API] getRect error:", error);
        throw error;
    }
}
```

**Key Pattern**: Direct property access from node object:
- `node.pos[0]` → x position
- `node.pos[1]` → y position
- `node.size[0]` → width
- `node.size[1]` → height

### Batch Collection Pattern: `web/js/query_executor.js`

```javascript
// Lines ~70-83
getAllNodes() {
    const nodes = [];
    const graph = this.app.graph;
    
    if (!graph || !graph._nodes) {
        return nodes;
    }
    
    for (const node of graph._nodes) {
        nodes.push(this.serializeNode(node));
    }
    
    return nodes;
}
```

**Key Pattern**: Iterate `app.graph._nodes` and transform/serialize each node

---

## 3. Tool Executor Registration Pattern

### Handler Registration: `web/js/tool_executor.js`

```javascript
// Lines ~31-91 (partial)
_registerHandlers() {
    return {
        // Query & Analysis
        "query_workflow": this._handleQueryWorkflow.bind(this),
        "workflow_overview": this._handleWorkflowOverview.bind(this),
        "workflow_diagram": this._handleWorkflowDiagram.bind(this),
        
        // Layout Management
        "get_node_rect": this._handleGetNodeRect.bind(this),
        "set_node_rect": this._handleSetNodeRect.bind(this),
        "position_node_left": this._handlePositionNodeLeft.bind(this),
        // ... more handlers
    };
}
```

### Handler Implementation Pattern
```javascript
// Lines ~380-384
async _handleGetNodeRect(params) {
    const { node_id } = params;
    const rect = this.flApi.getRect(node_id);
    return { node_id, rect };
}
```

**Key Pattern**: 
1. Extract params
2. Call FL_API method
3. Return structured result

---

## 4. Similar Batch Operations Analysis

### Workflow Diagram (Get Operation)
```javascript
// Lines ~219-231
async _handleWorkflowDiagram(params) {
    const { node_ids } = params;
    
    if (node_ids) {
        // Get specific nodes
        const nodes = node_ids.map(id => this.queryExecutor.getNodeById(id)).filter(n => n !== null);
        return { diagram: this.queryExecutor.generateDiagram(nodes) };
    } else {
        // Get all nodes
        const nodes = this.queryExecutor.getAllNodes();
        return { diagram: this.queryExecutor.generateDiagram(nodes) };
    }
}
```

**Key Pattern**: 
- Optional `node_ids` parameter
- If provided: map through specific IDs
- If null: get all nodes
- Process and return in single response

### Remove Nodes (Set Operation)
```javascript
// Lines ~93-106 (fl_api.js)
remove(nodeIds) {
    try {
        let removed = 0;
        for (const id of nodeIds) {
            const node = this._findNode(id);
            if (node) {
                app.graph.remove(node);
                removed++;
            }
        }
        console.log(`[FL_API] Removed ${removed} node(s)`);
        return removed;
    } catch (error) {
        console.error("[FL_API] remove error:", error);
        throw error;
    }
}
```

**Key Pattern**: Frontend loops through operations and returns count

---

## 5. Proposed Implementation Pattern

### Why Frontend Collection is Optimal

**Evidence from codebase:**

1. **`modify_layout()` is backend orchestration** (lines 826-832)
   - Loops on backend
   - Makes N WebSocket calls
   - This is for **SET operations** where each call might fail independently

2. **`workflow_diagram()` is frontend collection** (lines 219-231)
   - Collects all data on frontend
   - Single WebSocket response
   - This is for **GET operations** where data is already in memory

3. **`getAllNodes()` pattern exists** (query_executor.js lines 70-83)
   - Already iterates `app.graph._nodes`
   - Already serializes node data
   - Proven pattern for batch collection

4. **Frontend has direct access** (fl_api.js lines 1279+)
   - `app.graph._nodes` is the source of truth
   - No need for intermediate calls
   - Position/size data is immediately available

### Decision Matrix

| Aspect | Backend Loop | Frontend Collection |
|--------|--------------|---------------------|
| **Round-trips** | N calls | 1 call |
| **Latency** | High (N × RTT) | Low (1 × RTT) |
| **Complexity** | Backend needs node list first | Direct access |
| **Error handling** | Per-node failures | All-or-nothing |
| **Matches pattern** | `modify_layout()` (SET) | `workflow_diagram()` (GET) |
| **Data source** | Via WebSocket | Direct memory |

**Conclusion**: Frontend collection is the correct pattern for batch GET operations.

---

## 6. Implementation Specification

### Backend Model
```python
class GetLayoutRequest(BaseModel):
    """Request to get layout for all nodes or specific nodes."""
    node_ids: Optional[List[Union[int, str]]] = Field(
        None, 
        description="Optional list of node IDs to get rects for (null for all nodes)"
    )
```

**Rationale**:
- Matches `WorkflowDiagramRequest` pattern (line 263-265)
- `Optional[List[...]]` allows null for "all nodes"
- `Union[int, str]` allows ID or title lookup (consistent with `GetNodeRectRequest`)

### Backend Tool
```python
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

**Rationale**:
- Simple passthrough like `get_node_rect` (line 815-818)
- No backend looping (unlike `modify_layout`)
- Rich docstring explains use cases
- Structured return type documented

### Frontend API Method
```javascript
/**
 * Get layout (rects) for all nodes or specific nodes
 * @param {Array<number|string>|null} nodeIds - Optional array of node IDs (null for all)
 * @returns {object} {nodes: Array<object>, count: number}
 */
getLayout(nodeIds = null) {
    try {
        // Get nodes to process
        const nodes = nodeIds 
            ? nodeIds.map(id => this._findNode(id)).filter(n => n !== null)
            : app.graph._nodes;
        
        // Collect layout data
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

**Rationale**:
- Follows `getRect()` pattern (lines 798-818)
- Uses `_findNode()` helper for consistency
- Filters out null nodes (handles missing IDs gracefully)
- Returns structured object with metadata
- Includes node metadata (title, type) for context
- Logging matches existing pattern

### Tool Executor Handler
```javascript
// In _registerHandlers() object:
"get_layout": this._handleGetLayout.bind(this),

// Handler method:
async _handleGetLayout(params) {
    const { node_ids } = params;
    return this.flApi.getLayout(node_ids);
}
```

**Rationale**:
- Matches `_handleGetNodeRect` pattern (lines 380-384)
- Simple parameter extraction and delegation
- No transformation needed (FL_API returns correct structure)

---

## 7. Edge Cases & Error Handling

### Case 1: Empty Workflow
```javascript
app.graph._nodes = [];
getLayout(null); // Returns {nodes: [], count: 0}
```
**Handled**: Empty array is valid return

### Case 2: Invalid Node IDs
```javascript
getLayout([1, 999, 3]); // Node 999 doesn't exist
// Returns layout for nodes 1 and 3 only (filtered)
```
**Handled**: `.filter(n => n !== null)` removes missing nodes

### Case 3: Null vs Empty Array
```javascript
getLayout(null);  // All nodes
getLayout([]);    // No nodes (empty result)
```
**Handled**: Ternary operator distinguishes null from empty array

### Case 4: Graph Not Ready
```javascript
if (!app.graph || !app.graph._nodes) {
    return { nodes: [], count: 0 };
}
```
**Should Add**: Safety check like `getAllNodes()` (query_executor.js line 74)

---

## 8. Consistency Verification

### Naming Consistency
| Operation | Single | Batch |
|-----------|--------|-------|
| **Get** | `get_node_rect` | `get_layout` ✅ |
| **Set** | `set_node_rect` | `modify_layout` ✅ |

### Parameter Consistency
| Tool | Parameter Name | Type |
|------|----------------|------|
| `get_node_rect` | `node_id` | `Union[int, str]` |
| `get_layout` | `node_ids` | `Optional[List[Union[int, str]]]` ✅ |
| `workflow_diagram` | `node_ids` | `Optional[List[int]]` |

**Note**: Should use `Union[int, str]` for consistency with single operation

### Return Structure Consistency
| Tool | Returns |
|------|----------|
| `get_node_rect` | `{node_id, rect}` |
| `get_layout` | `{nodes: [{node_id, title, type, rect}, ...], count}` ✅ |
| `workflow_diagram` | `{diagram}` |

**Rationale**: Batch operations return richer metadata

---

## 9. Testing Considerations

### Unit Test Scenarios
1. **All nodes**: `get_layout(null)` with 5 nodes
2. **Specific nodes**: `get_layout([1, 3, 5])` 
3. **Empty workflow**: `get_layout(null)` with 0 nodes
4. **Invalid IDs**: `get_layout([999])` (non-existent)
5. **Mixed valid/invalid**: `get_layout([1, 999, 3])`
6. **By title**: `get_layout(["Load Checkpoint", "KSampler"])`
7. **Empty array**: `get_layout([])`

### Integration Test Scenarios
1. Get layout → modify_layout → verify changes
2. Get layout → calculate bounds → position new node
3. Get layout → detect overlaps → resolve
4. Large workflow (100+ nodes) performance test

---

## 10. Performance Analysis

### Current: N calls to get_node_rect
```
For 50 nodes:
- 50 WebSocket calls
- 50 × (serialize + deserialize + network RTT)
- Estimated: 50 × 5ms = 250ms minimum
```

### Proposed: 1 call to get_layout
```
For 50 nodes:
- 1 WebSocket call
- 1 × (serialize + deserialize + network RTT)
- Estimated: 1 × 5ms = 5ms
```

**Speedup**: 50× faster for 50 nodes (scales linearly)

### Memory Footprint
```javascript
Per node: ~100 bytes (node_id, title, type, rect)
50 nodes: ~5KB
100 nodes: ~10KB
```

**Acceptable**: Well within WebSocket message limits

---

## 11. Migration Path

### Option A: Keep Both Tools
```python
@mcp.tool()
async def get_node_rect(...):
    """Get single node rect. For batch operations, use get_layout()."""
    # Keep existing implementation

@mcp.tool()
async def get_layout(...):
    """Get multiple node rects efficiently."""
    # New implementation
```

**Pros**: No breaking changes
**Cons**: API surface grows

### Option B: Deprecate get_node_rect
```python
@mcp.tool()
async def get_node_rect(request: GetNodeRectRequest, ctx: Context):
    """[DEPRECATED] Use get_layout() instead."""
    result = await get_layout(
        GetLayoutRequest(node_ids=[request.node_id]), 
        ctx
    )
    return result['nodes'][0] if result['nodes'] else None
```

**Pros**: Cleaner API
**Cons**: Breaking change

### Recommendation: Option A
- Maintain backward compatibility
- Single-node queries are still common
- Direct call is slightly more ergonomic for single nodes

---

## 12. Final Implementation Checklist

### Backend (`backend/mcp_server.py`)
- [ ] Add `GetLayoutRequest` model after `GetNodeRectRequest` (~line 362)
- [ ] Add `get_layout()` tool after `get_node_rect()` (~line 818)
- [ ] Update docstring with use cases and return structure

### Frontend API (`web/js/fl_api.js`)
- [ ] Add `getLayout()` method in Layout Management section (~line 820)
- [ ] Add null check for `app.graph._nodes`
- [ ] Use `_findNode()` helper for consistency
- [ ] Return structured object with metadata

### Tool Executor (`web/js/tool_executor.js`)
- [ ] Add `"get_layout"` to handlers registration (~line 61)
- [ ] Add `_handleGetLayout()` method in Layout section (~line 385)

### Testing
- [ ] Test with null (all nodes)
- [ ] Test with specific node IDs
- [ ] Test with empty workflow
- [ ] Test with invalid IDs
- [ ] Test with large workflows (50+ nodes)

### Documentation
- [ ] Update API documentation
- [ ] Add usage examples
- [ ] Document return structure
- [ ] Note performance benefits

---

## Conclusion

The investigation confirms that **frontend batch collection** is the correct pattern for `get_layout()` implementation:

1. **Matches existing GET patterns** (workflow_diagram, getAllNodes)
2. **Leverages direct data access** (app.graph._nodes)
3. **Single WebSocket round-trip** (50× faster for 50 nodes)
4. **Consistent with codebase conventions** (naming, structure, error handling)
5. **Simple implementation** (no complex orchestration needed)

The implementation is ready to proceed with high confidence.
