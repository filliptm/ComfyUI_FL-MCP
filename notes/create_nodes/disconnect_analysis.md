# MCP Subprocess Disconnect Analysis

**Date:** 2025-10-16  
**Question:** Why does MCP subprocess close with code 1006 on tool errors?  
**Status:** Investigation in progress  

---

## 🔍 Key Finding: Background Task Isolation

### **Critical Discovery:**

```python
# In backend/server.py:websocket_endpoint()
if msg_type == "user_message":
    # Fire-and-forget background task
    asyncio.create_task(handle_user_message(session_id, data, message_history=context.conversation_history))
    # ↑ This is NON-BLOCKING and ISOLATED
```

**What this means:**
- `handle_user_message` runs as a **background task**
- Exceptions in background tasks **do NOT propagate** to the caller
- The WebSocket handler (`websocket_endpoint`) **never sees** exceptions from `handle_user_message`
- **Therefore:** The user's fix (catching `UnexpectedModelBehavior`) prevents logging issues but **doesn't affect WebSocket stability**

---

## 🔎 Disconnect Logic Analysis

### **1. Normal Disconnect Flow**

```python
# backend/server.py:254-260
except WebSocketDisconnect:
    if session_id:
        manager.disconnect(session_id, connection_type)
        # Cancel any pending callbacks for this session
        if callback_router:
            callback_router.cancel_pending_callbacks(session_id)
        logger.info(f"Session {session_id} - {connection_type} disconnected")
```

**Triggers:**
- Client closes connection
- Network issue
- WebSocket timeout

**Actions:**
- Remove connection from manager
- Cancel pending callbacks
- Log disconnect
- **Session context is preserved** for reconnection

---

### **2. Error-Triggered Disconnect**

```python
# backend/server.py:262-277
except Exception as e:
    logger.error(f"Error in WebSocket connection: {e}", exc_info=True)
    if session_id:
        manager.disconnect(session_id, connection_type)
        # Cancel any pending callbacks
        if callback_router:
            callback_router.cancel_pending_callbacks(session_id)
        await manager.send_error(
            session_id,
            "INTERNAL_ERROR",
            "An internal error occurred",
            {"error": str(e)},
            target=connection_type
        )
    try:
        await websocket.close()
    except Exception:
        pass
```

**Triggers:**
- Exception in WebSocket message loop
- Exception in message routing
- **NOT exceptions in background tasks** (like `handle_user_message`)

**Actions:**
- Log error
- Disconnect session
- Cancel pending callbacks
- Send error message
- Close WebSocket connection

---

### **3. Manager Disconnect Logic**

```python
# backend/manager.py:70-86
def disconnect(self, session_id: str, connection_type: str = 'frontend') -> None:
    """Disconnect a WebSocket connection.
    
    Note: Session context is kept alive for reconnection window.
    """
    if session_id in self.active_connections:
        if connection_type in self.active_connections[session_id]:
            del self.active_connections[session_id][connection_type]
            logger.info(f"Session {session_id} - {connection_type} disconnected")
        
        # Clean up session entry if no more connections
        if not self.active_connections[session_id]:
            del self.active_connections[session_id]
            logger.info(f"Session {session_id} - all connections closed")
```

**Important:**
- Only removes connection from active connections dict
- **Does NOT close the WebSocket** (caller's responsibility)
- **Does NOT terminate MCP subprocess**
- Session context remains for reconnection

---

## 🔍 MCP Subprocess Connection Handling

### **From `backend/mcp_server.py`:**

```python
class MCPWebSocketClient:
    async def _receive_loop(self):
        """Receive and process messages from backend."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                await self._handle_message(data)
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"[MCP-WS] Connection closed")
            self.connected = False
        except Exception as e:
            logger.error(f"[MCP-WS] Receive loop error: {e}")
            self.connected = False
```

**What happens on connection close:**
- `ConnectionClosed` exception is caught
- Sets `self.connected = False`
- **Does NOT crash the subprocess**
- Receive loop exits gracefully

**Question:** What happens when `self.connected = False`?
- Pending tool requests will fail with "WebSocket not connected"
- But the subprocess itself should stay alive

---

## ❓ The Mystery: Code 1006

### **What is code 1006?**

From WebSocket spec:
- **1006 = Abnormal Closure**
- Indicates connection closed **without close frame**
- Usually means:
  - Process terminated
  - Network failure
  - Unexpected crash

### **From the logs:**

```
DEBUG:    ! failing connection with code 1006
DEBUG:    = connection is CLOSED
2025-10-16 23:41:57,826 - Session b115cf0e... - mcp disconnected
```

**Analysis:**
- The "failing connection" message suggests the WebSocket library detected an abnormal closure
- This is logged by the websockets library, not our code
- The MCP connection is then marked as disconnected

**Hypothesis:** The MCP subprocess **itself** is terminating, causing the WebSocket to close abnormally.

---

## 🔎 Investigating MCP Subprocess Lifecycle

### **Question: How is the MCP subprocess started?**

**Need to find:**
- Where is the MCP subprocess created?
- How is it managed?
- What causes it to restart?
- What causes it to terminate?

### **Likely locations:**
- `backend/agent.py` - Agent manager
- `backend/subprocess_manager.py` (if exists)
- `backend/server.py` - Lifespan manager

### **From logs:**

```
2025-10-16 23:41:57,996 - backend.server - ERROR - Error handling user message: Tool 'create_nodes' exceeded max retries count of 1
```

**Timeline:**
- Tool error at 23:41:57.654
- Error logged at 23:41:57.996 (342ms later)
- MCP disconnect at 23:41:57.826 (172ms after tool error)

**Observation:** MCP disconnected **before** the error was logged in `handle_user_message`.

This suggests the MCP subprocess crashed **during** the retry attempts, not after.

---

## 💡 New Hypothesis

### **The MCP subprocess crashes during pydantic-ai retry**

**Sequence:**

1. Agent calls `create_nodes` tool
2. Tool fails with "Node type not found"
3. pydantic-ai catches error, raises `ModelRetry`
4. pydantic-ai attempts retry
5. **During retry, MCP subprocess crashes** ← **WHY?**
6. WebSocket closes with code 1006
7. Backend logs error after retries exhausted

**Why would retry cause crash?**

**Option A: Retry sends malformed request**
- pydantic-ai retry mechanism sends a modified request
- MCP subprocess can't handle it
- Crashes

**Option B: MCP subprocess state is corrupted**
- First error leaves subprocess in bad state
- Retry attempt triggers crash
- Process terminates

**Option C: Unhandled exception in retry path**
- MCP subprocess has a bug in error handling
- Retry triggers unhandled exception
- Process terminates

**Option D: Resource exhaustion**
- Multiple retries create resource leak
- Subprocess runs out of memory/handles
- Process terminates

---

## 🔬 Next Investigation Steps

### **1. Find MCP subprocess management code**

```bash
# Search for subprocess creation
grep -r "subprocess" backend/
grep -r "Popen" backend/
grep -r "create_subprocess" backend/
```

### **2. Check agent.py for MCP lifecycle**

```bash
# Look for MCP server startup
grep -r "mcp_server" backend/agent.py
grep -r "run_mcp_servers" backend/
```

### **3. Enable MCP subprocess logging**

- Check if MCP subprocess has its own log file
- Look for crash dumps or error logs
- Check if subprocess stderr is captured

### **4. Test minimal reproduction**

Create test case:
```python
# Trigger tool error with retry
# Monitor MCP subprocess
# Check when/why it terminates
```

### **5. Check pydantic-ai retry behavior**

- How does pydantic-ai retry tool calls?
- Does it restart the MCP connection?
- Does it send different requests?

---

## 📝 Summary

### **What We Know:**

1. ✅ **Background task isolation** - Exceptions in `handle_user_message` don't crash WebSocket
2. ✅ **Manager disconnect is passive** - Doesn't close WebSocket or terminate subprocess
3. ✅ **MCP client handles disconnects** - Catches `ConnectionClosed` gracefully
4. ✅ **Code 1006 = abnormal closure** - Suggests process termination
5. ✅ **MCP disconnects DURING retry** - Not after error is logged

### **What We Don't Know:**

1. ❓ **How is MCP subprocess started/managed?**
2. ❓ **Why does it terminate on tool errors?**
3. ❓ **What happens during pydantic-ai retry?**
4. ❓ **Is there error handling that terminates subprocess?**
5. ❓ **Are there logs from the subprocess itself?**

### **Leading Hypothesis:**

**The MCP subprocess crashes during pydantic-ai's retry attempt**, possibly due to:
- Unhandled exception in retry path
- Corrupted state from first error
- Bug in error handling
- Resource exhaustion

**This would explain:**
- Code 1006 (abnormal closure)
- Timing (crashes during retry, not after)
- Why catching `UnexpectedModelBehavior` doesn't help (crash happens before that)

### **Next Priority:**

Find and analyze:
1. MCP subprocess management code
2. MCP subprocess logs (if any)
3. pydantic-ai retry behavior
4. Agent lifecycle in `backend/agent.py`

**Confidence Level:** 🟡 **Medium**

We've ruled out several possibilities but still need to find the subprocess management code to understand the crash.
