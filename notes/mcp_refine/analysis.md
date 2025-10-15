# MCP Server Refactoring Analysis

**Date:** 2025-10-15  
**Issue:** Incorrect FastMCP patterns - need to use Context and Request models  
**Status:** Understanding requirements before implementation

---

## Problems Identified

### 1. ❌ Missing Context Parameter

**Current (Wrong):**
```python
async def _execute_tool(tool_name: str, parameters: Dict[str, Any], ...) -> Dict[str, Any]:
    if _ws_client is None:
        raise RuntimeError("WebSocket client not initialized")
    return await _ws_client.execute_tool(...)
```

**What's Wrong:**
- Using global `_ws_client` variable
- No access to FastMCP Context
- Cannot get lifespan context properly

**Correct Pattern (from updated code):**
```python
async def _execute_tool(ctx: Context, tool_name: str, parameters: Dict[str, Any], ...) -> Dict[str, Any]:
    _ws_client = ctx.request_context.lifespan_context.client
    if _ws_client is None:
        raise RuntimeError("WebSocket client not initialized")
    return await _ws_client.execute_tool(...)
```

**Key Understanding:**
- `ctx: Context` gives access to request context
- `ctx.request_context.lifespan_context` accesses data yielded from `mcp_lifespan()`
- `.client` is the key from the dict we yielded: `yield {"client": _ws_client}`
- This is the **correct** way to access shared state in FastMCP

---

### 2. ❌ Tool Functions Use Direct Parameters Instead of Request Models

**Current (Wrong):**
```python
@mcp.tool()
async def remove_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")
) -> Dict[str, Any]:
    return await _execute_tool("remove_nodes", {"node_ids": node_ids})
```

**What's Wrong:**
- Function signature has direct parameters with Field() annotations
- No Context parameter
- Parameters manually assembled into dict for `_execute_tool()`
- Not following FastMCP best practices

**Correct Pattern:**
```python
from pydantic import BaseModel

class RemoveNodesRequest(BaseModel):
    """Request to remove nodes from workflow."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")

@mcp.tool()
async def remove_nodes(request: RemoveNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Remove one or more nodes from the workflow."""
    return await _execute_tool(ctx, "remove_nodes", request.model_dump())
```

**Key Understanding:**
- **Every tool** should have exactly **2 parameters**: `request: SomeRequestModel, ctx: Context`
- Request models are Pydantic V2 BaseModel subclasses
- Field descriptions go in the request model, not function signature
- Request models should be defined **before** the tool function
- Use `request.model_dump()` to convert to dict for `_execute_tool()`

---

### 3. ❌ Verbose Example Code in Docstrings

**Current (Wrong):**
```python
@mcp.tool()
async def remove_nodes(...):
    """Remove one or more nodes from the workflow.
    
    Args:
        node_ids: List of node IDs or titles to remove
    
    Returns:
        Dictionary with 'removed_count' (int) key
    
    Example:
        >>> result = await remove_nodes(node_ids=[5, 7, 9])
        >>> print(f"Removed {result['removed_count']} nodes")
    """
```

**What's Wrong:**
- Verbose examples are unnecessary - agent knows how to use tools
- Examples are now outdated since signature will change
- Takes up context window space

**Correct Pattern:**
```python
@mcp.tool()
async def remove_nodes(request: RemoveNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Remove one or more nodes from the workflow."""
    return await _execute_tool(ctx, "remove_nodes", request.model_dump())
```

**Key Understanding:**
- Keep docstrings minimal - just the description
- Remove all Args, Returns, Examples sections
- The tool description is what the agent sees
- Request model field descriptions are sufficient

---

## What Needs to Change

### Step 1: Update `_execute_tool()` signature
```python
# OLD
async def _execute_tool(tool_name: str, parameters: Dict[str, Any], ...) -> Dict[str, Any]:
    _ws_client = ctx.request_context.lifespan_context.client  # This was already fixed

# NEW
async def _execute_tool(ctx: Context, tool_name: str, parameters: Dict[str, Any], ...) -> Dict[str, Any]:
    _ws_client = ctx.request_context.lifespan_context.client
```

### Step 2: Create Request Models for ALL tools

For each tool, create a Pydantic BaseModel **before** the tool function:

```python
class RemoveNodesRequest(BaseModel):
    """Request to remove nodes from workflow."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")

class BypassNodesRequest(BaseModel):
    """Request to bypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to bypass")

class CreateNodeRequest(BaseModel):
    """Request to create a new node."""
    node_type: str = Field(..., description="ComfyUI node class name (e.g., 'CheckpointLoaderSimple')")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Node parameter values as key-value pairs")
    position: Optional[Dict[str, float]] = Field(None, description="Node position {x, y}")

# ... etc for ALL tools
```

### Step 3: Update ALL tool functions

**Pattern:**
```python
@mcp.tool()
async def tool_name(request: ToolNameRequest, ctx: Context) -> Dict[str, Any]:
    """Brief description of what the tool does."""
    return await _execute_tool(ctx, "tool_name", request.model_dump())
```

### Step 4: Special Cases

**Tools with no parameters:**
```python
class WorkflowOverviewRequest(BaseModel):
    """Request for workflow overview."""
    pass  # Empty request model

@mcp.tool()
async def workflow_overview(request: WorkflowOverviewRequest, ctx: Context) -> Dict[str, Any]:
    """Get a comprehensive overview of the current workflow."""
    return await _execute_tool(ctx, "workflow_overview", {})
```

**Tools with only optional parameters:**
```python
class WorkflowDiagramRequest(BaseModel):
    """Request to generate workflow diagram."""
    node_ids: Optional[List[int]] = Field(None, description="Optional list of node IDs to include (null for all nodes)")

@mcp.tool()
async def workflow_diagram(request: WorkflowDiagramRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a Mermaid diagram of the workflow or subset of nodes."""
    return await _execute_tool(ctx, "workflow_diagram", request.model_dump())
```

---

## Example from Updated Code

I can see you've already updated `query_workflow` as an example:

```python
# WorkflowQuery is already a Pydantic model imported from backend.models

@mcp.tool()
async def query_workflow(query: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation.
    
    Supports filtering nodes by type, parameters, connections, etc.
    Can traverse graph connections (upstream/downstream).
    Can aggregate results (count, sum, avg, etc.).
    Multiple result formats: full, summary, ids, scalar, diagram.
    """
    return await _execute_tool(ctx, "query_workflow", query.model_dump())
```

**Note:** Even this could be simplified to just:
```python
@mcp.tool()
async def query_workflow(query: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation."""
    return await _execute_tool(ctx, "query_workflow", query.model_dump())
```

---

## Summary of Changes Needed

1. ✅ **Lifespan function** - Already fixed to yield `{"client": _ws_client}`
2. ✅ **_execute_tool signature** - Already updated to accept `ctx: Context` first
3. ⏳ **Create ~40 Request models** - One for each tool (except query_workflow which uses WorkflowQuery)
4. ⏳ **Update all tool functions** - Change signature to `(request: XxxRequest, ctx: Context)`
5. ⏳ **Simplify docstrings** - Remove verbose examples, Args, Returns sections
6. ⏳ **Update all _execute_tool calls** - Pass `ctx` as first argument

---

## Tools That Need Request Models

### Query & Analysis (2 tools)
- ✅ `query_workflow` - Already uses WorkflowQuery
- ❌ `workflow_overview` - Needs WorkflowOverviewRequest (empty)
- ❌ `workflow_diagram` - Needs WorkflowDiagramRequest

### Node Management (8 tools)
- ❌ `find_node` - Needs FindNodeRequest
- ❌ `create_node` - Needs CreateNodeRequest
- ❌ `remove_nodes` - Needs RemoveNodesRequest
- ❌ `bypass_nodes` - Needs BypassNodesRequest
- ❌ `unbypass_nodes` - Needs UnbypassNodesRequest
- ❌ `pin_nodes` - Needs PinNodesRequest
- ❌ `unpin_nodes` - Needs UnpinNodesRequest
- ❌ `select_nodes` - Needs SelectNodesRequest

### Node Manipulation (3 tools)
- ❌ `get_node_values` - Needs GetNodeValuesRequest
- ❌ `set_node_values` - Needs SetNodeValuesRequest
- ❌ `connect_nodes` - Needs ConnectNodesRequest

### Layout Management (9 tools)
- ❌ `get_node_rect` - Needs GetNodeRectRequest
- ❌ `set_node_rect` - Needs SetNodeRectRequest
- ❌ `position_node_left` - Needs PositionNodeLeftRequest
- ❌ `position_node_right` - Needs PositionNodeRightRequest
- ❌ `position_node_top` - Needs PositionNodeTopRequest
- ❌ `position_node_bottom` - Needs PositionNodeBottomRequest
- ❌ `move_node_right` - Needs MoveNodeRightRequest
- ❌ `move_node_bottom` - Needs MoveNodeBottomRequest

### Workflow Control (6 tools)
- ❌ `queue_workflow` - Needs QueueWorkflowRequest
- ❌ `cancel_workflow` - Needs CancelWorkflowRequest (empty)
- ❌ `enable_auto_queue` - Needs EnableAutoQueueRequest (empty)
- ❌ `disable_auto_queue` - Needs DisableAutoQueueRequest (empty)
- ❌ `set_batch_count` - Needs SetBatchCountRequest
- ❌ `get_queue_status` - Needs GetQueueStatusRequest (empty)

### System Control (5 tools)
- ❌ `disable_sleep` - Needs DisableSleepRequest (empty)
- ❌ `enable_sleep` - Needs EnableSleepRequest (empty)
- ❌ `disable_screensaver` - Needs DisableScreensaverRequest (empty)
- ❌ `enable_screensaver` - Needs EnableScreensaverRequest (empty)
- ❌ `send_images` - Needs SendImagesRequest

### Utility (4 tools)
- ❌ `generate_seed` - Needs GenerateSeedRequest (empty)
- ❌ `generate_float` - Needs GenerateFloatRequest
- ❌ `generate_int` - Needs GenerateIntRequest
- ❌ `random_choice` - Needs RandomChoiceRequest

**Total: 37 Request models needed** (1 already exists as WorkflowQuery)

---

## Confidence Check

Before implementing, I need to confirm I understand:

1. ✅ Every `@mcp.tool()` function gets exactly 2 params: `request: SomeRequest, ctx: Context`
2. ✅ Request models are Pydantic V2 BaseModel with Field descriptions
3. ✅ Request models defined BEFORE the tool function they're used in
4. ✅ Context is used to access lifespan data: `ctx.request_context.lifespan_context.client`
5. ✅ All `_execute_tool()` calls must pass `ctx` as first argument
6. ✅ Docstrings should be minimal - just the description, no examples/args/returns
7. ✅ Empty request models (with `pass`) for tools with no parameters
8. ✅ Use `request.model_dump()` to convert request to dict for _execute_tool

**Am I understanding this correctly?**
