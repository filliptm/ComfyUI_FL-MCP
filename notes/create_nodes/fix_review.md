# User's Fix Review - UnexpectedModelBehavior Handler

**Date:** 2025-10-16  
**Location:** `backend/server.py:584-593`  
**Change:** Added exception handler for `UnexpectedModelBehavior`  

---

## 🔧 The Fix

### **Code Added:**

```python
except UnexpectedModelBehavior as ue:
    # Happens mainly when a model can't get a tool call right after so many tries

    # Extract root cause error
    root_cause = ue.__cause__ if ue.__cause__ else ue
    root_cause_msg = str(root_cause)
    
    trback = traceback.format_exception(type(root_cause), root_cause, root_cause.__traceback__)

    logger.critical(f"Critical Tool Error: {root_cause_msg}\n\nTraceback:\n{trback}")
```

**Location in flow:**
```python
async def handle_user_message(session_id: str, data: dict[str, Any], message_history:List[ModelMessage]) -> None:
    try:
        # ... agent.run() ...
        response = await agent.run(message.message, message_history=...)
        
        if response is not None:
            # Send response to frontend
            await manager.send_message(session_id, {...})
    
    except UnexpectedModelBehavior as ue:  # ← NEW HANDLER
        # Log critical error with traceback
        logger.critical(...)
    
    except Exception as e:
        # Generic error handler
        logger.error(...)
        await manager.send_error(session_id, ...)
```

---

## ✅ What This Fixes

### **Before:**
- `UnexpectedModelBehavior` would fall through to generic `except Exception`
- Generic handler sends error message to frontend
- **But doesn't crash the WebSocket handler** (good!)
- MCP subprocess might still close for other reasons

### **After:**
- `UnexpectedModelBehavior` is caught specifically
- Logs critical error with full traceback
- **Does NOT send error to frontend** (intentional?)
- **Does NOT crash** (good!)

---

## 🔍 Analysis

### **1. Does this prevent the MCP subprocess crash?**

**Answer: Maybe, but not directly.**

**Reasoning:**
- The crash happens in the **MCP subprocess** (code 1006)
- This fix is in the **backend server** (different process)
- The exception is caught in `handle_user_message` which runs in the backend
- **But:** If the exception was propagating to the WebSocket handler, this would prevent that

**Key Question:** Was the exception actually propagating to `websocket_endpoint()`?

---

### **2. Could the exception have been propagating?**

**From the code:**

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    try:
        # ...
        while True:
            data = await websocket.receive_json()
            
            if msg_type == "user_message":
                # Fire-and-forget task
                asyncio.create_task(handle_user_message(session_id, data, ...))
                # ↑ This is NON-BLOCKING!
            # ...
    
    except WebSocketDisconnect:
        # ...
    except Exception as e:
        # ...
```

**Analysis:**
- `handle_user_message` is launched as a **background task** via `asyncio.create_task()`
- This means exceptions in `handle_user_message` **do NOT propagate** to `websocket_endpoint()`
- They would be logged as "unhandled task exceptions" instead

**Conclusion:** The exception was **NOT propagating** to the WebSocket handler.

---

### **3. So why did the user's other implementation crash?**

**User said:**
> "in my other implementation of this same stack it was because I hade a 'raise' on the except in the try wrapping agent.run and that caused the outer process inside the websocket handler to error out"

**Translation:**
```python
# User's other implementation (BAD)
try:
    response = await agent.run(...)
except UnexpectedModelBehavior as ue:
    logger.error(...)
    raise  # ← This re-raises the exception!
```

**In that case:**
- The `raise` would re-raise the exception
- If `handle_user_message` was **NOT** a background task (i.e., `await handle_user_message(...)`)
- The exception would propagate to `websocket_endpoint()`
- The WebSocket handler's generic `except Exception` would catch it
- The handler would close the connection

**But in THIS implementation:**
- No `raise` in the exception handler
- `handle_user_message` is a background task
- **So the exception cannot propagate**

---

### **4. Then why is the MCP subprocess still crashing?**

**Hypothesis:**

The MCP subprocess crash is **NOT caused by UnexpectedModelBehavior propagating**.

It's caused by something else:

**Option A: pydantic-ai's retry mechanism**
- pydantic-ai retries the tool call
- Each retry sends a new tool request to the MCP subprocess
- The MCP subprocess tries to execute the tool again
- Fails again
- After max retries, pydantic-ai gives up
- **But why would this close the MCP subprocess?**

**Option B: MCP subprocess timeout or error**
- The MCP subprocess is waiting for a tool result
- The result never comes (because of retry failures)
- The subprocess times out or errors out
- Closes connection

**Option C: Backend intentionally closes MCP on error**
- There might be error handling code that closes the MCP subprocess
- Need to search for "close" or "disconnect" in error handling

**Option D: MCP subprocess crashes internally**
- The exception in the MCP subprocess itself causes a crash
- Even though FastMCP catches it, something else fails
- Process terminates

---

## 🔎 What We Still Don't Know

### **Critical Questions:**

1. **Does the fix actually prevent the crash?**
   - Need to test with the same error scenario
   - See if MCP subprocess still closes

2. **Why does the MCP subprocess close?**
   - Is it intentional (error handling policy)?
   - Is it a bug (unhandled error)?
   - Is it external (timeout, resource limit)?

3. **What triggers code 1006 (abnormal closure)?**
   - Backend closes the connection?
   - MCP subprocess closes it?
   - Network issue?

4. **Is there error handling code that closes MCP?**
   - Search for "disconnect" or "close" in error paths
   - Check manager.py for session cleanup on errors

---

## 🔬 Next Investigation Steps

### **1. Search for MCP disconnect logic**

Search for:
- `manager.disconnect(session_id, 'mcp')`
- `websocket.close()`
- Error handling that might close connections

### **2. Check manager.py**

Look for:
- Session cleanup logic
- Error handling that disconnects clients
- Timeout mechanisms

### **3. Check MCP subprocess lifecycle**

Look for:
- How is the subprocess started?
- When does it restart?
- What triggers termination?

### **4. Test the fix**

Create test case:
- Trigger `UnexpectedModelBehavior` (e.g., invalid node type)
- Check if MCP subprocess still crashes
- Compare logs before and after fix

---

## 📝 User's Question

> "I am still not sure why not catching it failed the outer process though..."

**Answer:**

In **this implementation**, not catching it would **NOT** fail the outer process because:

1. `handle_user_message` is a **background task** (`asyncio.create_task()`)
2. Exceptions in background tasks **don't propagate** to the caller
3. They're logged as "unhandled task exceptions" instead

**However:**

- The generic `except Exception` handler **would** catch it
- It would send an error message to the frontend
- **But it wouldn't close the WebSocket connection**

**So the question remains:** Why does the MCP subprocess close?

**Hypothesis:** The MCP subprocess closes for a **different reason** than the `UnexpectedModelBehavior` exception.

---

## 🤔 User's Concern

> "I'm not sure that is happening here... check my addition anyway and let's keep investigating."

**Review of the fix:**

✅ **Good:**
- Catches `UnexpectedModelBehavior` specifically
- Logs critical error with full traceback
- Doesn't crash the handler
- Extracts root cause for better debugging

⚠️ **Potential Issues:**
- **Doesn't send error to frontend** - User gets no feedback
- **Doesn't clear typing indicator** - UI might show "typing..." forever
- **Doesn't reset agent state** - Agent might be in bad state

**Suggested Enhancement:**

```python
except UnexpectedModelBehavior as ue:
    # Happens mainly when a model can't get a tool call right after so many tries
    
    # Extract root cause error
    root_cause = ue.__cause__ if ue.__cause__ else ue
    root_cause_msg = str(root_cause)
    
    trback = traceback.format_exception(type(root_cause), root_cause, root_cause.__traceback__)
    
    logger.critical(f"Critical Tool Error: {root_cause_msg}\n\nTraceback:\n{trback}")
    
    # ✅ Send error to frontend so user knows what happened
    await manager.send_error(
        session_id,
        "TOOL_EXECUTION_FAILED",
        f"Tool execution failed after retries: {root_cause_msg}",
        {"error": str(root_cause), "traceback": trback},
    )
    
    # ✅ Clear typing indicator
    await manager.send_message(session_id, {
        "type": "typing_indicator",
        "session_id": session_id,
        "is_typing": False,
    })
```

---

## 🎯 Summary

### **The Fix:**
- ✅ Catches `UnexpectedModelBehavior`
- ✅ Logs critical error with traceback
- ✅ Prevents exception from propagating (though it wouldn't anyway)
- ❌ **Doesn't explain why MCP subprocess closes**

### **The Mystery:**
- ❓ **Why does MCP subprocess close with code 1006?**
- ❓ Is it related to `UnexpectedModelBehavior` at all?
- ❓ Or is it a separate issue?

### **Next Steps:**
1. Search for MCP disconnect/close logic in error handling
2. Check `manager.py` for session cleanup on errors
3. Test if the fix actually prevents the crash
4. Investigate MCP subprocess lifecycle

**Confidence Level:** 🟡 **Medium**

The fix is good for logging and error handling, but we still don't know **why the MCP subprocess closes**. Need to investigate further.
