# Create Nodes Investigation - Detailed Code Flow

**Date:** 2025-10-16  
**Issue:** MCP subprocess crash on tool error  
**Investigation:** Trace complete error flow through codebase  

---

## 🔍 Full Error Flow Trace

### **From Log Analysis:**

The error follows this exact path:

```
1. Agent calls create_nodes tool
   ↓
2. create_nodes loops through nodes, calls _execute_tool
   ↓
3. _execute_tool calls ws_client.execute_tool
   ↓
4. execute_tool sends WebSocket message to backend
   ↓
5. Backend routes tool_request to frontend
   ↓
6. Frontend executes create_node, fails with "Node type not found"
   ↓
7. Frontend sends tool_result with success=false
   ↓
8. Backend routes tool_result back to MCP subprocess
   ↓
9. MCP subprocess _handle_message receives result
   ↓
10. _handle_message calls future.set_exception(RuntimeError(...))
   ↓
11. execute_tool's await asyncio.wait_for(future) raises RuntimeError
   ↓
12. Exception propagates to _execute_tool
   ↓
13. Exception propagates to create_nodes
   ↓
14. FastMCP tool_manager.py catches exception
   ↓
15. FastMCP logs error, returns to pydantic-ai
   ↓
16. pydantic-ai retries (ModelRetry exception)
   ↓
17. Retry fails, raises UnexpectedModelBehavior
   ↓
18. Backend server.py catches error, sends error message to frontend
   ↓
19. MCP subprocess connection closes (code 1006)
```

---

## 📄 Log Timeline Analysis

### **Timestamp: 23:41:57.654-57.656**

**First Request (ID: 8587043a-3b69-43c6-83bb-6c96b925e4f6):**
```
23:41:57.654 - Tool result received from frontend (success)
23:41:57.654 - Tool result routed to MCP subprocess
23:41:57.654 - WARNING: Received tool result for unknown request_id: 8587043a...
```

**Analysis:**
- Backend says "success" but there's a warning about unknown request_id
- This is **confusing** - why is success logged but request_id unknown?
- Possible explanation: The log says "(success)" based on parsing, but the actual result was failure

**Second Request (ID: b9bae3dc-6473-45ff-9fb1-d41f8406698a):**
```
23:41:57.655 - Tool request sent to frontend (create_node)
23:41:57.656 - Tool result received from frontend (failed)
23:41:57.656 - Tool result routed to MCP subprocess
23:41:57.656 - WARNING: Received tool result for unknown request_id: b9bae3dc...
```

**Analysis:**
- This is the actual failing request
- Frontend correctly returns failure
- Same warning about unknown request_id

---

## ⚠️ Key Discovery: Unknown Request ID Warning

### **From `backend/callback_router.py`:**

```python
# This warning appears for BOTH requests
WARNING - Received tool result for unknown request_id: {request_id}
```

**Investigation Questions:**
1. Why is the request_id "unknown" to callback_router?
2. Is callback_router still being used in subprocess mode?
3. Is this a race condition or architectural issue?

**Hypothesis:**
- The system has **TWO routing mechanisms**:
  1. **Legacy:** `callback_router` (used in standalone mode)
  2. **New:** Direct WebSocket routing (used in subprocess mode)
- Both are running simultaneously, causing confusion

**Evidence from `backend/mcp_server.py`:**

```python
# Legacy callback router support (kept for backwards compatibility)
_callback_router = None

def set_callback_router(router):
    """Set the callback router instance (legacy).
    
    This is kept for backwards compatibility but is no longer used
    when running in subprocess mode.
    """
    global _callback_router
    _callback_router = router
    logger.info("Callback router set for MCP server (legacy mode)")
```

**Conclusion:**
- The warning is **harmless** - it's the legacy system complaining
- The actual routing works correctly via WebSocket
- But this creates **log noise** and confusion

---

## 🔥 The Real Crash Point

### **From Traceback:**

```python
# FastMCP catches the error
File ".../fastmcp/tools/tool_manager.py:131 in call_tool
    return await tool.run(arguments)

# Tool execution fails
File ".../backend/mcp_server.py:476 in create_nodes
    o.append(await _execute_tool(ctx, "create_node", node.model_dump()))

# WebSocket client raises RuntimeError
File ".../backend/mcp_server.py:137 in execute_tool
    result = await asyncio.wait_for(future, timeout=timeout_seconds)

# Exception: RuntimeError: Node type not found: ImageComposite
```

**Then:**

```python
# pydantic-ai catches the error and retries
File ".../pydantic_ai/mcp.py:158 in direct_call_tool
    raise exceptions.ModelRetry(text)

# After max retries, raises UnexpectedModelBehavior
File ".../pydantic_ai/_tool_manager.py:101 in _call_tool
    raise UnexpectedModelBehavior(f'Tool {name!r} exceeded max retries count of {max_retries}')
```

**Finally:**

```python
# Backend catches the error
File ".../backend/server.py:565 in handle_user_message
    response = await agent.run(message.message, ...)

# Logs error and sends to frontend
2025-10-16 23:41:57,996 - backend.server - ERROR - Error handling user message: Tool 'create_nodes' exceeded max retries count of 1
```

**Then MCP subprocess closes:**

```
DEBUG:    ! failing connection with code 1006
DEBUG:    = connection is CLOSED
2025-10-16 23:41:57,826 - Session b115cf0e... - mcp disconnected
```

---

## 🧩 Architecture Analysis

### **Component Interaction:**

```
┌────────────────────────┐
│   Frontend (Browser)    │
│   - ComfyUI Interface   │
│   - Tool Executor       │
└─────────┬──────────────┘
         │ WebSocket
         │
┌────────┴───────────────────────────────┐
│   Backend Server (backend/server.py)    │
│   - WebSocket Router                    │
│   - Session Manager                     │
│   - Callback Router (LEGACY)            │ ← Causes warnings!
│   - Agent Runner (pydantic-ai)          │
└────────┬───────────────────────────────┘
         │ Subprocess + WebSocket
         │
┌────────┴───────────────────────────────┐
│   MCP Subprocess (mcp_server.py)        │
│   - FastMCP Server                      │
│   - Tool Definitions                    │
│   - WebSocket Client                    │ ← Crashes here!
│   - Lifespan Manager                    │
└────────────────────────────────────────┘
```

### **Error Propagation:**

```
Frontend Error ("Node not found")
  ↓ WebSocket tool_result {success: false}
Backend Router
  ↓ Routes to MCP subprocess
  ↓ ALSO routes to callback_router (legacy) ← Causes warning
MCP WebSocket Client
  ↓ future.set_exception(RuntimeError(...))
MCP Tool (create_nodes)
  ↓ await _execute_tool() raises
FastMCP Framework
  ↓ Catches exception, logs error
pydantic-ai
  ↓ Catches exception, retries (ModelRetry)
  ↓ Max retries exceeded
  ↓ Raises UnexpectedModelBehavior
Backend Agent Runner
  ↓ Catches exception, sends error to frontend
MCP Subprocess
  ↓ Connection closes (code 1006)
  ↓ Process terminates
```

---

## 🔍 Code Investigation: Why Does It Crash?

### **Question: Does FastMCP crash on tool errors?**

**From traceback:**

```python
File ".../fastmcp/tools/tool_manager.py:131 in call_tool
    return await tool.run(arguments)
```

FastMCP catches the exception and logs it:

```
ERROR    Error calling tool 'create_nodes': Node type not found: ImageComposite
```

Then returns the error to pydantic-ai. **FastMCP does NOT crash.**

### **Question: Does pydantic-ai crash on tool errors?**

**From traceback:**

```python
File ".../pydantic_ai/mcp.py:158 in direct_call_tool
    raise exceptions.ModelRetry(text)
```

pydantic-ai catches the error and retries:

```python
File ".../pydantic_ai/_tool_manager.py:95 in _call_tool
    output = await self.toolset.call_tool(name, args_dict, ctx, tool)
```

After max retries (1), raises `UnexpectedModelBehavior`. **pydantic-ai does NOT crash, it returns error to caller.**

### **Question: Why does MCP subprocess close?**

**From logs:**

```
DEBUG:    ! failing connection with code 1006
DEBUG:    = connection is CLOSED
```

**Code 1006:** Abnormal closure (no close frame received)

**Analysis:**
- The MCP subprocess **doesn't crash** from the exception
- The WebSocket connection **closes abnormally**
- This suggests the subprocess **terminates** for another reason

**Hypothesis:**
- pydantic-ai raises `UnexpectedModelBehavior`
- Backend catches this in `server.py:handle_user_message`
- Backend sends error to frontend
- **But what happens to the MCP subprocess?**

---

## 🔎 Deeper Investigation Needed

### **Missing Information:**

1. **How does backend/server.py handle UnexpectedModelBehavior?**
   - Does it close the MCP subprocess connection?
   - Does it restart the subprocess?

2. **What is the MCP subprocess lifecycle?**
   - Is it per-session or global?
   - Does it restart automatically?

3. **Why code 1006 (abnormal closure)?**
   - Who initiates the close?
   - Is it intentional or a crash?

### **Files to Investigate:**

1. `backend/server.py` - Agent runner and error handling
2. `backend/manager.py` - Session and subprocess management
3. `backend/subprocess_manager.py` (if exists) - MCP subprocess lifecycle

---

## 💡 Preliminary Conclusions

### **What We Know:**

1. ✅ **Error propagates correctly** through all layers
2. ✅ **FastMCP handles errors gracefully** (doesn't crash)
3. ✅ **pydantic-ai retries and returns error** (doesn't crash)
4. ✅ **Backend catches error and sends to frontend**
5. ❌ **MCP subprocess closes abnormally** (code 1006)
6. ⚠️ **Legacy callback_router causes log warnings** (harmless but noisy)

### **What We Don't Know:**

1. ❓ **Why does MCP subprocess close?**
   - Is it intentional (error handling policy)?
   - Is it a bug (unexpected termination)?

2. ❓ **Who initiates the close?**
   - Backend server?
   - MCP subprocess itself?
   - pydantic-ai?

3. ❓ **Is this the intended behavior?**
   - Should MCP subprocess close on tool errors?
   - Or should it stay alive and continue?

### **Next Steps:**

1. 🔍 **Investigate `backend/server.py`:**
   - Search for "UnexpectedModelBehavior" handling
   - Check if it closes MCP connection

2. 🔍 **Investigate session/subprocess management:**
   - Find where MCP subprocess is created
   - Check lifecycle and restart policies

3. 🔍 **Check pydantic-ai integration:**
   - How does backend handle agent errors?
   - Does it restart MCP on errors?

4. 🧪 **Test hypothesis:**
   - Create minimal test case
   - Trigger tool error
   - Observe MCP subprocess behavior

---

## 📝 Summary

### **Error Flow (Confirmed):**

```
create_nodes → _execute_tool → WebSocket → Frontend → Error
  ↓
RuntimeError raised
  ↓
FastMCP catches, logs, returns to pydantic-ai
  ↓
pydantic-ai retries (ModelRetry)
  ↓
Max retries exceeded (UnexpectedModelBehavior)
  ↓
Backend catches, sends error to frontend
  ↓
MCP subprocess closes (code 1006) ← WHY?
```

### **Key Questions:**

1. **Why does MCP subprocess close?** ← **CRITICAL**
2. Is this intentional or a bug?
3. Should we prevent this closure?
4. Can we make tools more resilient?

### **Confidence Level:**

- 🟢 **High** - Error flow is clear
- 🟡 **Medium** - Root cause of closure unclear
- 🔴 **Low** - Solution approach uncertain

**Next:** Investigate `backend/server.py` and session management to understand MCP subprocess lifecycle.
