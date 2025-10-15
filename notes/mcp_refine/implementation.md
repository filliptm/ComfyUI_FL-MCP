# MCP Server Refactoring Implementation Plan

**Date:** 2025-10-15  
**Task:** Refactor all MCP tools to use Request models and Context  
**Reference:** notes/mcp_refine/analysis.md

---

## Implementation Strategy

### Phase 1: Request Model Definitions
Define all Pydantic V2 BaseModel request classes at the top of the file, organized by category.

### Phase 2: Update Tool Functions
Update each `@mcp.tool()` function to use the new signature pattern.

### Phase 3: Cleanup
Remove verbose docstrings, examples, and outdated comments.

---

## Code Changes

### Location: `backend/mcp_server.py`

---

### CHANGE 1: Add Request Model Imports

**After the existing imports, before MCPWebSocketClient class:**

```python
from pydantic import BaseModel, Field
```

*(Already imported, just confirming)*

---

### CHANGE 2: Define All Request Models

**Insert after `_execute_tool()` function, before the first `@mcp.tool()`:**

```python
# ============================================================================
# REQUEST MODELS
# ============================================================================

# Query & Analysis
class WorkflowOverviewRequest(BaseModel):
    """Request for workflow overview."""
    pass

class WorkflowDiagramRequest(BaseModel):
    """Request to generate workflow diagram."""
    node_ids: Optional[List[int]] = Field(None, description="Optional list of node IDs to include (null for all nodes)")

# Node Management
class FindNodeRequest(BaseModel):
    """Request to find a node."""
    node_id: Optional[int] = Field(None, description="Node ID to find")
    node_type: Optional[str] = Field(None, description="Node type/class to find (e.g., 'KSampler')")
    title: Optional[str] = Field(None, description="Node title to find")
    find_last: bool = Field(False, description="If true, search from end of array")

class CreateNodeRequest(BaseModel):
    """Request to create a new node."""
    node_type: str = Field(..., description="ComfyUI node class name (e.g., 'CheckpointLoaderSimple')")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Node parameter values as key-value pairs")
    position: Optional[Dict[str, float]] = Field(None, description="Node position {x, y}")

class RemoveNodesRequest(BaseModel):
    """Request to remove nodes from workflow."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")

class BypassNodesRequest(BaseModel):
    """Request to bypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to bypass")

class UnbypassNodesRequest(BaseModel):
    """Request to unbypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unbypass")

class PinNodesRequest(BaseModel):
    """Request to pin nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to pin")

class UnpinNodesRequest(BaseModel):
    """Request to unpin nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unpin")

class SelectNodesRequest(BaseModel):
    """Request to select nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to select")

# Node Manipulation
class GetNodeValuesRequest(BaseModel):
    """Request to get node parameter values."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class SetNodeValuesRequest(BaseModel):
    """Request to set node parameter values."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    values: Dict[str, Any] = Field(..., description="Parameter values to set as key-value pairs")

class ConnectNodesRequest(BaseModel):
    """Request to connect two nodes."""
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title")
    source_slot: Union[str, int] = Field(..., description="Source output slot name or index")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title")
    target_slot: Optional[Union[str, int]] = Field(None, description="Target input slot name or index (defaults to source_slot)")

# Layout Management
class GetNodeRectRequest(BaseModel):
    """Request to get node position and size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class SetNodeRectRequest(BaseModel):
    """Request to set node position and/or size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    x: Optional[float] = Field(None, description="X position (null to keep current)")
    y: Optional[float] = Field(None, description="Y position (null to keep current)")
    width: Optional[float] = Field(None, description="Width (null to keep current)")
    height: Optional[float] = Field(None, description="Height (null to keep current)")

class PositionNodeLeftRequest(BaseModel):
    """Request to position node to the left of another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(32, description="Margin between nodes in pixels")

class PositionNodeRightRequest(BaseModel):
    """Request to position node to the right of another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(32, description="Margin between nodes in pixels")

class PositionNodeTopRequest(BaseModel):
    """Request to position node above another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(64, description="Margin between nodes in pixels")

class PositionNodeBottomRequest(BaseModel):
    """Request to position node below another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(64, description="Margin between nodes in pixels")

class MoveNodeRightRequest(BaseModel):
    """Request to move node to the right, avoiding collisions."""
    node_id: Union[int, str] = Field(..., description="Node to move")
    margin: int = Field(32, description="Margin to maintain when avoiding collisions")

class MoveNodeBottomRequest(BaseModel):
    """Request to move node downward, avoiding collisions."""
    node_id: Union[int, str] = Field(..., description="Node to move")
    margin: int = Field(64, description="Margin to maintain when avoiding collisions")

# Workflow Control
class QueueWorkflowRequest(BaseModel):
    """Request to queue workflow for execution."""
    batch_count: Optional[int] = Field(None, description="Number of times to execute (default: current batch count)")

class CancelWorkflowRequest(BaseModel):
    """Request to cancel workflow execution."""
    pass

class EnableAutoQueueRequest(BaseModel):
    """Request to enable auto-queue mode."""
    pass

class DisableAutoQueueRequest(BaseModel):
    """Request to disable auto-queue mode."""
    pass

class SetBatchCountRequest(BaseModel):
    """Request to set workflow batch count."""
    count: int = Field(..., description="Batch count (number of times to execute workflow)")

class GetQueueStatusRequest(BaseModel):
    """Request to get queue status."""
    pass

# System Control
class DisableSleepRequest(BaseModel):
    """Request to disable system sleep."""
    pass

class EnableSleepRequest(BaseModel):
    """Request to enable system sleep."""
    pass

class DisableScreensaverRequest(BaseModel):
    """Request to disable screensaver."""
    pass

class EnableScreensaverRequest(BaseModel):
    """Request to enable screensaver."""
    pass

class SendImagesRequest(BaseModel):
    """Request to send images to external URL."""
    url: str = Field(..., description="Target URL to send images to")
    field: str = Field(..., description="Form field name for images")
    file_paths: List[Union[str, Dict[str, Any]]] = Field(..., description="List of file paths or PreviewImage node objects")

# Utility
class GenerateSeedRequest(BaseModel):
    """Request to generate random seed."""
    pass

class GenerateFloatRequest(BaseModel):
    """Request to generate random float."""
    min: float = Field(..., description="Minimum value")
    max: float = Field(..., description="Maximum value")

class GenerateIntRequest(BaseModel):
    """Request to generate random integer."""
    min: int = Field(..., description="Minimum value")
    max: int = Field(..., description="Maximum value")

class RandomChoiceRequest(BaseModel):
    """Request to pick random item from list."""
    items: List[Any] = Field(..., description="List of items to choose from")
```

---

### CHANGE 3: Update All Tool Functions

**Pattern for each tool:**

#### workflow_overview
```python
@mcp.tool()
async def workflow_overview(request: WorkflowOverviewRequest, ctx: Context) -> Dict[str, Any]:
    """Get a comprehensive overview of the current workflow."""
    return await _execute_tool(ctx, "workflow_overview", {})
```

#### workflow_diagram
```python
@mcp.tool()
async def workflow_diagram(request: WorkflowDiagramRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a Mermaid diagram of the workflow or subset of nodes."""
    return await _execute_tool(ctx, "workflow_diagram", request.model_dump())
```

#### find_node
```python
@mcp.tool()
async def find_node(request: FindNodeRequest, ctx: Context) -> Dict[str, Any]:
    """Find a node by ID, type, or title."""
    return await _execute_tool(ctx, "find_node", request.model_dump())
```

#### create_node
```python
@mcp.tool()
async def create_node(request: CreateNodeRequest, ctx: Context) -> Dict[str, Any]:
    """Create a new node in the workflow."""
    return await _execute_tool(ctx, "create_node", request.model_dump())
```

#### remove_nodes
```python
@mcp.tool()
async def remove_nodes(request: RemoveNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Remove one or more nodes from the workflow."""
    return await _execute_tool(ctx, "remove_nodes", request.model_dump())
```

#### bypass_nodes
```python
@mcp.tool()
async def bypass_nodes(request: BypassNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Bypass (mute) one or more nodes."""
    return await _execute_tool(ctx, "bypass_nodes", request.model_dump())
```

#### unbypass_nodes
```python
@mcp.tool()
async def unbypass_nodes(request: UnbypassNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Unbypass (unmute) one or more nodes."""
    return await _execute_tool(ctx, "unbypass_nodes", request.model_dump())
```

#### pin_nodes
```python
@mcp.tool()
async def pin_nodes(request: PinNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Pin one or more nodes to prevent movement."""
    return await _execute_tool(ctx, "pin_nodes", request.model_dump())
```

#### unpin_nodes
```python
@mcp.tool()
async def unpin_nodes(request: UnpinNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Unpin one or more nodes to allow movement."""
    return await _execute_tool(ctx, "unpin_nodes", request.model_dump())
```

#### select_nodes
```python
@mcp.tool()
async def select_nodes(request: SelectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Select one or more nodes in the UI."""
    return await _execute_tool(ctx, "select_nodes", request.model_dump())
```

#### get_node_values
```python
@mcp.tool()
async def get_node_values(request: GetNodeValuesRequest, ctx: Context) -> Dict[str, Any]:
    """Get all parameter values from a node."""
    return await _execute_tool(ctx, "get_node_values", request.model_dump())
```

#### set_node_values
```python
@mcp.tool()
async def set_node_values(request: SetNodeValuesRequest, ctx: Context) -> Dict[str, Any]:
    """Set parameter values on a node."""
    return await _execute_tool(ctx, "set_node_values", request.model_dump())
```

#### connect_nodes
```python
@mcp.tool()
async def connect_nodes(request: ConnectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Connect two nodes together."""
    return await _execute_tool(ctx, "connect_nodes", request.model_dump())
```

#### get_node_rect
```python
@mcp.tool()
async def get_node_rect(request: GetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
    """Get node position and size."""
    return await _execute_tool(ctx, "get_node_rect", request.model_dump())
```

#### set_node_rect
```python
@mcp.tool()
async def set_node_rect(request: SetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
    """Set node position and/or size."""
    return await _execute_tool(ctx, "set_node_rect", request.model_dump())
```

#### position_node_left
```python
@mcp.tool()
async def position_node_left(request: PositionNodeLeftRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node to the left of another node."""
    return await _execute_tool(ctx, "position_node_left", request.model_dump())
```

#### position_node_right
```python
@mcp.tool()
async def position_node_right(request: PositionNodeRightRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node to the right of another node."""
    return await _execute_tool(ctx, "position_node_right", request.model_dump())
```

#### position_node_top
```python
@mcp.tool()
async def position_node_top(request: PositionNodeTopRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node above another node."""
    return await _execute_tool(ctx, "position_node_top", request.model_dump())
```

#### position_node_bottom
```python
@mcp.tool()
async def position_node_bottom(request: PositionNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node below another node."""
    return await _execute_tool(ctx, "position_node_bottom", request.model_dump())
```

#### move_node_right
```python
@mcp.tool()
async def move_node_right(request: MoveNodeRightRequest, ctx: Context) -> Dict[str, Any]:
    """Move a node to the right, avoiding collisions."""
    return await _execute_tool(ctx, "move_node_right", request.model_dump())
```

#### move_node_bottom
```python
@mcp.tool()
async def move_node_bottom(request: MoveNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
    """Move a node downward, avoiding collisions."""
    return await _execute_tool(ctx, "move_node_bottom", request.model_dump())
```

#### queue_workflow
```python
@mcp.tool()
async def queue_workflow(request: QueueWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Queue the workflow for execution."""
    return await _execute_tool(ctx, "queue_workflow", request.model_dump())
```

#### cancel_workflow
```python
@mcp.tool()
async def cancel_workflow(request: CancelWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Cancel the currently executing workflow."""
    return await _execute_tool(ctx, "cancel_workflow", {})
```

#### enable_auto_queue
```python
@mcp.tool()
async def enable_auto_queue(request: EnableAutoQueueRequest, ctx: Context) -> Dict[str, Any]:
    """Enable auto-queue mode."""
    return await _execute_tool(ctx, "enable_auto_queue", {})
```

#### disable_auto_queue
```python
@mcp.tool()
async def disable_auto_queue(request: DisableAutoQueueRequest, ctx: Context) -> Dict[str, Any]:
    """Disable auto-queue mode."""
    return await _execute_tool(ctx, "disable_auto_queue", {})
```

#### set_batch_count
```python
@mcp.tool()
async def set_batch_count(request: SetBatchCountRequest, ctx: Context) -> Dict[str, Any]:
    """Set the workflow batch count."""
    return await _execute_tool(ctx, "set_batch_count", request.model_dump())
```

#### get_queue_status
```python
@mcp.tool()
async def get_queue_status(request: GetQueueStatusRequest, ctx: Context) -> Dict[str, Any]:
    """Get current queue status and settings."""
    return await _execute_tool(ctx, "get_queue_status", {})
```

#### disable_sleep
```python
@mcp.tool()
async def disable_sleep(request: DisableSleepRequest, ctx: Context) -> Dict[str, Any]:
    """Disable system sleep/suspend."""
    return await _execute_tool(ctx, "disable_sleep", {})
```

#### enable_sleep
```python
@mcp.tool()
async def enable_sleep(request: EnableSleepRequest, ctx: Context) -> Dict[str, Any]:
    """Enable system sleep/suspend."""
    return await _execute_tool(ctx, "enable_sleep", {})
```

#### disable_screensaver
```python
@mcp.tool()
async def disable_screensaver(request: DisableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
    """Disable screensaver."""
    return await _execute_tool(ctx, "disable_screensaver", {})
```

#### enable_screensaver
```python
@mcp.tool()
async def enable_screensaver(request: EnableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
    """Enable screensaver."""
    return await _execute_tool(ctx, "enable_screensaver", {})
```

#### send_images
```python
@mcp.tool()
async def send_images(request: SendImagesRequest, ctx: Context) -> Dict[str, Any]:
    """Send images to an external URL."""
    return await _execute_tool(ctx, "send_images", request.model_dump())
```

#### generate_seed
```python
@mcp.tool()
async def generate_seed(request: GenerateSeedRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random seed value."""
    return await _execute_tool(ctx, "generate_seed", {})
```

#### generate_float
```python
@mcp.tool()
async def generate_float(request: GenerateFloatRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random float value."""
    return await _execute_tool(ctx, "generate_float", request.model_dump())
```

#### generate_int
```python
@mcp.tool()
async def generate_int(request: GenerateIntRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random integer value."""
    return await _execute_tool(ctx, "generate_int", request.model_dump())
```

#### random_choice
```python
@mcp.tool()
async def random_choice(request: RandomChoiceRequest, ctx: Context) -> Dict[str, Any]:
    """Pick a random item from a list."""
    return await _execute_tool(ctx, "random_choice", request.model_dump())
```

---

### CHANGE 4: Simplify query_workflow docstring

**Current:**
```python
@mcp.tool()
async def query_workflow(query: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation.
    
    Supports filtering nodes by type, parameters, connections, etc.
    Can traverse graph connections (upstream/downstream).
    Can aggregate results (count, sum, avg, etc.).
    Multiple result formats: full, summary, ids, scalar, diagram.
    
    Args:
        query: WorkflowQuery object with filters, traversal, aggregation, etc.
    
    Returns:
        Query results in requested format
    
    Example - Find all KSampler nodes:
        >>> result = await query_workflow(WorkflowQuery(
        ...     filters=FilterGroup(
        ...         operator="and",
        ...         filters=[Filter(field="type", operator="equals", value="KSampler")]
        ...     )
        ... ))
    
    Example - Count nodes:
        >>> result = await query_workflow(WorkflowQuery(
        ...     aggregation=Aggregation(type="count"),
        ...     result_format="scalar"
        ... ))
    
    Example - Get downstream nodes:
        >>> result = await query_workflow(WorkflowQuery(
        ...     filters=FilterGroup(
        ...         operator="and",
        ...         filters=[Filter(field="id", operator="equals", value=5)]
        ...     ),
        ...     traversal=Traversal(direction="downstream")
        ... ))
    """
    return await _execute_tool(ctx, "query_workflow", query.model_dump())
```

**New:**
```python
@mcp.tool()
async def query_workflow(query: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation."""
    return await _execute_tool(ctx, "query_workflow", query.model_dump())
```

---

## Summary of Changes

### Files Modified: 1
- `backend/mcp_server.py`

### Lines Changed: ~1500 lines
- Add 37 Request model definitions (~200 lines)
- Update 38 tool function signatures (~40 lines)
- Remove ~1260 lines of verbose docstrings, Args, Returns, Examples

### Testing Impact
- **No functional changes** - all tools still do the same thing
- **API changes** - tool signatures change but MCP handles this transparently
- **Agent behavior** - should be identical, just cleaner tool descriptions

---

## Verification Checklist

After implementation, verify:

- [ ] All 38 tools have Request models defined
- [ ] All tools use `(request: XxxRequest, ctx: Context)` signature
- [ ] All `_execute_tool()` calls pass `ctx` as first argument
- [ ] Empty request models use `pass`
- [ ] All verbose examples removed from docstrings
- [ ] File still runs without syntax errors
- [ ] MCP server starts successfully
- [ ] Tools can be called by agent

---

## Understanding Confirmation

I understand that:

1. ✅ **Every tool needs exactly 2 parameters**: `request: SomeRequest, ctx: Context`
2. ✅ **Request models are Pydantic V2 BaseModel** with Field() descriptions
3. ✅ **Request models come BEFORE the tool** they're used in
4. ✅ **Context accesses lifespan data** via `ctx.request_context.lifespan_context.client`
5. ✅ **All _execute_tool calls** must pass `ctx` first: `_execute_tool(ctx, "tool_name", ...)`
6. ✅ **Docstrings are minimal** - just description, no Args/Returns/Examples
7. ✅ **Empty requests use `pass`** for tools with no parameters
8. ✅ **Use `request.model_dump()`** to convert to dict, or `{}` for empty requests

Ready to implement upon your approval.
