# Error Feedback Debug Session 1 - Investigation

**Date**: 2025-10-18  
**Related**: [analysis.md](./analysis.md)  
**Mode**: Debugging

---

## 🔍 Code Investigation

### Current Implementation Status

#### ✅ Backend Implementation (Confirmed)

**File**: `backend/manager.py`
- ✅ `ErrorBuffer` class exists
- ✅ `ExecutionTracker` class exists  
- ✅ `handle_comfy_error()` method exists
- ✅ `handle_queue_status()` method exists
- ✅ `handle_execution_event()` method exists

**File**: `backend/server.py`
- ✅ WebSocket message routing for error events exists
- ✅ Routes: `comfy_error`, `queue_status`, `execution_event`

**File**: `backend/mcp_server.py`
- ✅ `get_recent_errors()` tool exists
- ✅ `get_errors_for_run()` tool exists
- ✅ `get_queue_status()` tool exists
- ✅ Other error tools exist

#### ✅ Frontend Implementation (Confirmed)

**File**: `web/js/ws_client.js`
- ✅ `setupComfyListeners(comfyApi)` method exists (line 300)
- ✅ Listens to `execution_error` event (line 305)
- ✅ Listens to `execution_interrupted` event
- ✅ Listens to `status` event (queue updates)
- ✅ Listens to `execution_start`, `executing`, `execution_cached`, `execution_success` events
- ✅ Forwards all events to backend via `this.send()`

**File**: `web/js/extension.js`
- ✅ Calls `wsClient.setupComfyListeners(window.app.api)` after handshake (line 69)
- ✅ Has retry logic if API not available (line 72-77)

**File**: `web/js/fl_api.js`
- ✅ `queueWorkflow()` method exists (line 1064)
- ✅ Calls `app.queuePrompt()` (line 1069)
- ❌ **Does NOT capture errors from queuePrompt**
- ❌ **Does NOT send validation errors to backend**

---

## 🔎 Root Cause Confirmed

### The Missing Link: Validation Error Capture

From the logs:
```javascript
// Frontend log shows:
XHRPOST http://127.0.0.1:8188/api/prompt
[HTTP/1.1 400 Bad Request 2ms]

Error: Prompt execution failed
    PromptExecutionError api.ts:222
```

This error is thrown by **ComfyUI's own code** (in `api.ts`), NOT by our code. The error happens in:

1. `app.queuePrompt()` is called (ComfyUI core function)
2. ComfyUI POSTs to `/api/prompt` endpoint
3. Server returns HTTP 400 (validation error)
4. ComfyUI throws `PromptExecutionError`
5. **Our code doesn't catch this error**

### Why ComfyUI Events Don't Fire

ComfyUI's event architecture:

```
app.queuePrompt() 
  ↓
  POST /api/prompt
  ↓
  ┌─ HTTP 400 (Validation Error)
  │   ↓
  │   Throw PromptExecutionError
  │   ↓
  │   NO EVENTS FIRED ← We are here
  │
  └─ HTTP 200 (Validation Success)
      ↓
      Workflow starts executing
      ↓
      WebSocket events fire:
      - execution_start
      - executing (per node)
      - execution_error (if node fails) ← This WOULD work
      - execution_success
```

**Conclusion**: The current implementation ONLY captures **execution errors** (errors that happen during workflow execution), NOT **validation errors** (errors that happen before execution starts).

---

## 💡 The Real Problem

### Two Types of Errors in ComfyUI

#### 1. Validation Errors (Pre-Execution)

**When**: Before workflow starts executing  
**How Exposed**: HTTP 400 response from `/api/prompt`  
**Error Source**: JavaScript exception in `app.queuePrompt()`  
**Captured By**: ❌ NOT captured by our implementation  

**Examples**:
- Missing node connections
- Invalid node parameters
- Malformed workflow JSON
- Unknown node types

**Error Structure** (from HTTP response body):
```javascript
{
  "error": {
    "type": "prompt_error",
    "message": "Validation failed",
    "details": "..."
  },
  "node_errors": {
    "5": {
      "errors": [
        {
          "type": "required_input_missing",
          "message": "Required input 'model' is not connected"
        }
      ],
      "class_type": "KSampler"
    }
  }
}
```

#### 2. Execution Errors (During Execution)

**When**: During workflow execution  
**How Exposed**: ComfyUI WebSocket event `execution_error`  
**Error Source**: ComfyUI's execution engine  
**Captured By**: ✅ Captured by our implementation  

**Examples**:
- Node crashes during processing
- Out of memory errors
- File not found errors
- Model loading failures

**Error Structure** (from WebSocket event):
```javascript
{
  "prompt_id": "abc123",
  "node_id": 5,
  "node_type": "KSampler",
  "exception_type": "RuntimeError",
  "exception_message": "CUDA out of memory",
  "traceback": [...],
  "executed": [1, 2, 3, 4],
  "current_inputs": {...},
  "current_outputs": [...]
}
```

---

## 🔧 Where to Capture Validation Errors

### Option 1: Wrap `app.queuePrompt()` in `fl_api.js`

**Pros**:
- Centralized error handling
- Can extract full error details from HTTP response
- Can send to backend immediately

**Cons**:
- Need to access the underlying Promise/error from `app.queuePrompt()`
- ComfyUI's `queuePrompt()` may not return the error details

**Code Location**: `web/js/fl_api.js` line 1064-1076

```javascript
queueWorkflow(batchCount = null) {
    try {
        if (batchCount !== null) {
            app.ui.batchCount.value = batchCount;
        }
        app.queuePrompt();  // ← This throws, but we don't catch
        console.log(`[FL_API] Queued workflow (batch: ${app.ui.batchCount.value})`);
        return { queued: true, batch_count: parseInt(app.ui.batchCount.value) };
    } catch (error) {
        console.error("[FL_API] queueWorkflow error:", error);
        throw error;  // ← Re-throws, but doesn't send to backend
    }
}
```

### Option 2: Hook into ComfyUI's API Error Handler

**Pros**:
- Captures ALL errors from ComfyUI API
- No need to modify every tool
- Centralized at the API level

**Cons**:
- Need to understand ComfyUI's internal API structure
- May require monkey-patching ComfyUI code

**Code Location**: Hook into `window.app.api` after it's initialized

### Option 3: Add Error Listener to ComfyUI API

**Pros**:
- Clean, event-driven approach
- Doesn't modify existing code

**Cons**:
- Need to verify ComfyUI API has error events
- May not exist for validation errors

---

## 🔍 Investigation: ComfyUI API Structure

### What We Know

From `web/js/extension.js` and `web/js/ws_client.js`:

```javascript
// ComfyUI API is available at:
window.app.api

// It has addEventListener for:
- "execution_error"
- "execution_interrupted" 
- "status"
- "execution_start"
- "executing"
- "execution_cached"
- "execution_success"
```

### What We Need to Find

1. **Does ComfyUI API expose validation errors as events?**
   - Likely: NO (based on logs showing JavaScript exception)
   
2. **Can we hook into the fetch/XHR layer?**
   - Likely: YES (can intercept HTTP responses)
   
3. **Does `app.queuePrompt()` return a Promise?**
   - Need to check: If yes, we can catch errors
   - If no, we need to wrap it

---

## 📝 Proposed Solutions

### Solution 1: Wrap `app.queuePrompt()` with Error Handling

**Implementation**: Modify `web/js/fl_api.js`

```javascript
async queueWorkflow(batchCount = null) {
    try {
        if (batchCount !== null) {
            app.ui.batchCount.value = batchCount;
        }
        
        // Call queuePrompt and wait for result
        const result = await app.queuePrompt();
        
        console.log(`[FL_API] Queued workflow (batch: ${app.ui.batchCount.value})`);
        return { 
            queued: true, 
            batch_count: parseInt(app.ui.batchCount.value),
            prompt_id: result?.prompt_id
        };
    } catch (error) {
        console.error("[FL_API] queueWorkflow error:", error);
        
        // Extract error details
        const errorData = {
            error_type: "validation_error",
            message: error.message || "Unknown error",
            timestamp: Date.now(),
            // Try to extract more details if available
            details: error.response?.data || error.data || null
        };
        
        // Send to backend via WebSocket
        if (window.FL_JS?.wsClient) {
            window.FL_JS.wsClient.send({
                type: "comfy_error",
                data: errorData
            });
        }
        
        // Re-throw so tool executor can handle it
        throw error;
    }
}
```

### Solution 2: Intercept ComfyUI API Calls

**Implementation**: Add to `web/js/ws_client.js` in `setupComfyListeners()`

```javascript
setupComfyListeners(comfyApi) {
    this.comfyApi = comfyApi;
    console.log('[WSClient] Setting up ComfyUI event listeners');
    
    // Wrap the queuePrompt function to capture validation errors
    const originalQueuePrompt = window.app.queuePrompt.bind(window.app);
    window.app.queuePrompt = async (...args) => {
        try {
            return await originalQueuePrompt(...args);
        } catch (error) {
            console.error('[WSClient] Queue validation error:', error);
            
            // Send validation error to backend
            this.send({
                type: "comfy_error",
                data: {
                    error_type: "validation_error",
                    message: error.message,
                    details: error.response?.data || null,
                    timestamp: Date.now()
                }
            });
            
            // Re-throw so normal error handling continues
            throw error;
        }
    };
    
    // ... existing event listeners ...
}
```

### Solution 3: Add Error Buffer to Frontend

**Implementation**: Create `web/js/error_buffer.js`

```javascript
export class ErrorBuffer {
    constructor(maxSize = 50) {
        this.errors = [];
        this.maxSize = maxSize;
    }
    
    addError(error) {
        this.errors.push({
            ...error,
            timestamp: error.timestamp || Date.now()
        });
        
        // Keep only last N errors
        if (this.errors.length > this.maxSize) {
            this.errors.shift();
        }
        
        // Also send to backend
        if (window.FL_JS?.wsClient) {
            window.FL_JS.wsClient.send({
                type: "comfy_error",
                data: error
            });
        }
    }
    
    getRecent(limit = 10) {
        return this.errors.slice(-limit);
    }
    
    clear() {
        this.errors = [];
    }
}
```

Then use it in both validation and execution error paths.

---

## 🤔 Questions to Answer

### Q1: Does `app.queuePrompt()` return a Promise?

**Answer**: Need to test in browser console:
```javascript
const result = window.app.queuePrompt();
console.log(result);
console.log(result instanceof Promise);
```

If it returns a Promise, we can use `async/await` and `try/catch`.

### Q2: Where does the error actually get thrown?

**Answer**: From the log:
```
Error: Prompt execution failed
    PromptExecutionError api.ts:222
```

The error is thrown in ComfyUI's `api.ts` file. This is inside ComfyUI's core code, not our extension.

### Q3: Can we access the HTTP response body?

**Answer**: The error object might contain:
- `error.response` (if using axios/fetch)
- `error.data`
- `error.message`

Need to inspect the actual error object in the catch block.

### Q4: Should we buffer errors in frontend or backend?

**Answer**: **Both**, for different reasons:

- **Frontend Buffer**: 
  - Immediate access for UI display
  - Works even if backend is down
  - Can show errors in ComfyUI interface
  
- **Backend Buffer**:
  - Persistent across page refreshes
  - Accessible via MCP tools for agent
  - Can correlate with execution tracking
  - **This is what we implemented**

**Current Implementation**: Backend only (which is correct for MCP agent access)

---

## ✅ Recommended Approach

### Phase 1: Capture Validation Errors (Immediate)

1. **Wrap `app.queuePrompt()` in `ws_client.js`**
   - Intercept at the API level
   - Capture validation errors
   - Send to backend via WebSocket
   - Let existing backend buffer store them

2. **Test with broken workflow**
   - Verify validation errors are captured
   - Check backend buffer receives them
   - Verify MCP tools can retrieve them

### Phase 2: Test Execution Errors (Verification)

1. **Create workflow that passes validation but fails execution**
   - Example: Load non-existent model file
   - Example: Use invalid image dimensions
   
2. **Verify execution errors are captured**
   - Should already work with current implementation
   - Confirm WebSocket events fire correctly

### Phase 3: Add Frontend Error Display (Enhancement)

1. **Show errors in ComfyUI UI**
   - Add error notification in chat interface
   - Show recent errors in sidebar
   - Highlight nodes with errors

---

## 📝 Implementation Plan

### Step 1: Add Validation Error Capture

**File**: `web/js/ws_client.js`

**Location**: In `setupComfyListeners()` method, BEFORE the existing event listeners

**Code**:
```javascript
setupComfyListeners(comfyApi) {
    this.comfyApi = comfyApi;
    console.log('[WSClient] Setting up ComfyUI event listeners');
    
    // === NEW: Wrap queuePrompt to capture validation errors ===
    if (window.app?.queuePrompt && !window.app._queuePromptWrapped) {
        const originalQueuePrompt = window.app.queuePrompt.bind(window.app);
        
        window.app.queuePrompt = async function(...args) {
            try {
                console.log('[WSClient] Calling queuePrompt...');
                const result = await originalQueuePrompt(...args);
                console.log('[WSClient] queuePrompt success:', result);
                return result;
            } catch (error) {
                console.error('[WSClient] queuePrompt validation error:', error);
                
                // Extract error details
                const errorData = {
                    error_type: "validation_error",
                    message: error.message || "Unknown validation error",
                    timestamp: Date.now(),
                    // Try to extract structured error data
                    node_errors: error.response?.data?.node_errors || null,
                    error_details: error.response?.data?.error || null,
                };
                
                // Send to backend
                if (window.FL_JS?.wsClient?.connected) {
                    console.log('[WSClient] Sending validation error to backend:', errorData);
                    window.FL_JS.wsClient.send({
                        type: "comfy_error",
                        data: errorData
                    });
                }
                
                // Re-throw so normal error handling continues
                throw error;
            }
        };
        
        // Mark as wrapped to prevent double-wrapping
        window.app._queuePromptWrapped = true;
        console.log('[WSClient] Wrapped app.queuePrompt to capture validation errors');
    }
    
    // === EXISTING: Event listeners for execution errors ===
    this.comfyApi.addEventListener("execution_error", (event) => {
        // ... existing code ...
    });
    // ... rest of existing listeners ...
}
```

### Step 2: Update Backend Error Handler

**File**: `backend/manager.py`

**Update**: Modify `handle_comfy_error()` to handle validation errors

```python
async def handle_comfy_error(self, data: Dict[str, Any]) -> None:
    """Handle error from ComfyUI frontend.
    
    Args:
        data: Error data from ComfyUI
    """
    error_type = data.get("error_type")
    
    if error_type == "validation_error":
        # Validation error (before execution starts)
        self.error_buffer.add_error(data)
        logger.error(
            f"ComfyUI validation error: {data.get('message')}"
        )
    elif error_type == "execution_error":
        # Execution error (during workflow run)
        self.error_buffer.add_error(data)
        self.execution_tracker.handle_execution_error(data)
        logger.error(
            f"ComfyUI execution error in node {data.get('node_id')} "
            f"({data.get('node_type')}): {data.get('exception_message')}"
        )
    elif error_type == "execution_interrupted":
        self.error_buffer.add_error(data)
        logger.warning(
            f"ComfyUI execution interrupted at node {data.get('node_id')}"
        )
```

### Step 3: Test

1. **Start backend**: `cd backend && python server.py`
2. **Reload ComfyUI frontend**
3. **Create broken workflow** (missing connections)
4. **Try to queue it**
5. **Check logs**:
   - Frontend: Should see `[WSClient] queuePrompt validation error`
   - Backend: Should see `ComfyUI validation error`
6. **Query errors via MCP**:
   ```python
   errors = await get_recent_errors(limit=5)
   print(errors)
   ```

---

## 📈 Success Criteria

- [ ] Validation errors are captured and logged
- [ ] Validation errors appear in backend error buffer
- [ ] `get_recent_errors()` returns validation errors
- [ ] Execution errors still work (regression test)
- [ ] Error details include node information
- [ ] Errors are timestamped correctly

---

## 🔍 Next Steps

1. **Implement validation error capture** (Solution 2 above)
2. **Test with broken workflow**
3. **Verify MCP tools work**
4. **Test execution errors** (create workflow that fails during execution)
5. **Document findings**

---

**Status**: Ready for implementation  
**Confidence**: High (95%) that this will solve the issue  
**Estimated Time**: 30 minutes to implement and test
