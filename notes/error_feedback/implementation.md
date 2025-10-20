# Error Feedback & Queue Status Implementation Guide

**Date**: 2025-10-18  
**Related**: [research.md](./research.md)  
**Status**: Ready for Implementation

---

## 📋 Overview

This guide provides step-by-step instructions to implement error feedback and queue status tracking in the ComfyUI MCP integration. The implementation captures execution errors from ComfyUI's frontend, stores them in the backend, and exposes them via MCP tools.

---

## 🎯 Goals

1. **Capture all execution errors** from ComfyUI with full context
2. **Track queue status** and execution progress in real-time
3. **Store error history** with prompt_id indexing for debugging
4. **Expose error/queue data** via MCP tools for agent access
5. **Handle connection validation errors** separately from execution errors

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     ComfyUI Frontend                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  ComfyUI API Events                                   │  │
│  │  • execution_error                                    │  │
│  │  • execution_interrupted                              │  │
│  │  • execution_start/success                            │  │
│  │  • executing (node tracking)                          │  │
│  │  • status (queue updates)                             │  │
│  └──────────────────────────────────────────────────────┘  │
│                            ↓                                 │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  web/js/ws_client.js                                  │  │
│  │  • Listen to ComfyUI events                           │  │
│  │  • Forward to backend via WebSocket                   │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓ WebSocket
┌─────────────────────────────────────────────────────────────┐
│                     Backend (FastAPI)                       │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  backend/manager.py (ConnectionManager)               │  │
│  │  • ErrorBuffer (circular buffer, 100 errors)          │  │
│  │  • ExecutionTracker (active execution states)         │  │
│  │  • Message routing                                     │  │
│  └──────────────────────────────────────────────────────┘  │
│                            ↓                                 │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  backend/mcp_server.py                                │  │
│  │  • get_recent_errors()                                │  │
│  │  • get_errors_for_run()                               │  │
│  │  • get_queue_status()                                 │  │
│  │  • clear_error_buffer()                               │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓ MCP Protocol
┌─────────────────────────────────────────────────────────────┐
│                    Agent (Claude/MCP)                       │
│  Can query errors and queue status for debugging           │
└─────────────────────────────────────────────────────────────┘
```

---

## 📦 Implementation Steps

### Step 1: Add Error Buffer to Backend

**File**: `backend/manager.py`

#### 1.1 Add ErrorBuffer Class

Add this class **before** the `ConnectionManager` class:

```python
from collections import deque
from typing import List

class ErrorBuffer:
    """Circular buffer for storing ComfyUI execution errors.
    
    Stores up to max_size errors with prompt_id indexing for quick lookup.
    """
    
    def __init__(self, max_size: int = 100):
        """Initialize error buffer.
        
        Args:
            max_size: Maximum number of errors to store
        """
        self.errors = deque(maxlen=max_size)
        self.errors_by_prompt: Dict[str, List[Dict[str, Any]]] = {}
        self.max_size = max_size
        
    def add_error(self, error_data: Dict[str, Any]) -> None:
        """Add error to buffer with timestamp and indexing.
        
        Args:
            error_data: Error data from ComfyUI execution_error event
        """
        # Create error entry with metadata
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "error_type": error_data.get("error_type", "execution_error"),
            "prompt_id": error_data.get("prompt_id"),
            "node_id": error_data.get("node_id"),
            "node_type": error_data.get("node_type"),
            "exception_type": error_data.get("exception_type"),
            "exception_message": error_data.get("exception_message"),
            "traceback": error_data.get("traceback", []),
            "executed_nodes": error_data.get("executed", []),
            "current_inputs": error_data.get("current_inputs", {}),
            "current_outputs": error_data.get("current_outputs", []),
        }
        
        # Add to circular buffer
        self.errors.append(error_entry)
        
        # Index by prompt_id for quick lookup
        prompt_id = error_entry["prompt_id"]
        if prompt_id:
            if prompt_id not in self.errors_by_prompt:
                self.errors_by_prompt[prompt_id] = []
            self.errors_by_prompt[prompt_id].append(error_entry)
            
            # Cleanup old prompt_id entries if they exceed buffer
            if len(self.errors_by_prompt[prompt_id]) > self.max_size:
                self.errors_by_prompt[prompt_id].pop(0)
        
        logger.debug(f"Error added to buffer: {error_entry['error_type']} for prompt {prompt_id}")
        
    def get_recent_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get N most recent errors.
        
        Args:
            limit: Maximum number of errors to return
            
        Returns:
            List of error entries (most recent last)
        """
        return list(self.errors)[-limit:]
        
    def get_errors_for_prompt(self, prompt_id: str) -> List[Dict[str, Any]]:
        """Get all errors for a specific prompt/run.
        
        Args:
            prompt_id: Prompt ID to get errors for
            
        Returns:
            List of error entries for that prompt
        """
        return self.errors_by_prompt.get(prompt_id, [])
        
    def get_all_errors(self) -> List[Dict[str, Any]]:
        """Get all errors in buffer.
        
        Returns:
            List of all error entries
        """
        return list(self.errors)
        
    def clear(self) -> None:
        """Clear all errors from buffer."""
        self.errors.clear()
        self.errors_by_prompt.clear()
        logger.info("Error buffer cleared")
        
    def get_count(self) -> int:
        """Get total number of errors in buffer.
        
        Returns:
            Number of errors stored
        """
        return len(self.errors)
```

#### 1.2 Add ExecutionTracker Class

Add this class after `ErrorBuffer`:

```python
class ExecutionTracker:
    """Tracks active workflow executions and queue status.
    
    Monitors execution lifecycle events from ComfyUI to maintain
    current state of running and queued workflows.
    """
    
    def __init__(self):
        """Initialize execution tracker."""
        self.active_executions: Dict[str, Dict[str, Any]] = {}
        self.queue_status = {
            "running": [],
            "pending": [],
            "queue_remaining": 0
        }
        
    def handle_execution_start(self, data: Dict[str, Any]) -> None:
        """Handle execution_start event.
        
        Args:
            data: Event data with prompt_id
        """
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return
            
        self.active_executions[prompt_id] = {
            "status": "running",
            "start_time": datetime.now().isoformat(),
            "current_node": None,
            "executed_nodes": [],
            "cached_nodes": [],
        }
        
        # Update queue status
        if prompt_id not in self.queue_status["running"]:
            self.queue_status["running"].append(prompt_id)
            
        logger.debug(f"Execution started: {prompt_id}")
        
    def handle_executing(self, data: Dict[str, Any]) -> None:
        """Handle executing event (node execution tracking).
        
        Args:
            data: Event data with prompt_id and node
        """
        prompt_id = data.get("prompt_id")
        node_id = data.get("node")
        
        if not prompt_id or prompt_id not in self.active_executions:
            return
            
        execution = self.active_executions[prompt_id]
        
        if node_id is None:
            # Execution complete (node is null)
            execution["current_node"] = None
            execution["status"] = "completing"
        else:
            # Node started executing
            execution["current_node"] = node_id
            if node_id not in execution["executed_nodes"]:
                execution["executed_nodes"].append(node_id)
                
    def handle_execution_cached(self, data: Dict[str, Any]) -> None:
        """Handle execution_cached event.
        
        Args:
            data: Event data with prompt_id and cached nodes
        """
        prompt_id = data.get("prompt_id")
        if prompt_id and prompt_id in self.active_executions:
            self.active_executions[prompt_id]["cached_nodes"] = data.get("nodes", [])
            
    def handle_execution_success(self, data: Dict[str, Any]) -> None:
        """Handle execution_success event.
        
        Args:
            data: Event data with prompt_id
        """
        prompt_id = data.get("prompt_id")
        if prompt_id and prompt_id in self.active_executions:
            self.active_executions[prompt_id]["status"] = "success"
            self.active_executions[prompt_id]["end_time"] = datetime.now().isoformat()
            
            # Remove from running queue
            if prompt_id in self.queue_status["running"]:
                self.queue_status["running"].remove(prompt_id)
                
            logger.debug(f"Execution succeeded: {prompt_id}")
            
    def handle_execution_error(self, data: Dict[str, Any]) -> None:
        """Handle execution_error event.
        
        Args:
            data: Event data with prompt_id and error details
        """
        prompt_id = data.get("prompt_id")
        if prompt_id and prompt_id in self.active_executions:
            self.active_executions[prompt_id]["status"] = "error"
            self.active_executions[prompt_id]["end_time"] = datetime.now().isoformat()
            self.active_executions[prompt_id]["error"] = {
                "node_id": data.get("node_id"),
                "exception_type": data.get("exception_type"),
                "message": data.get("exception_message"),
            }
            
            # Remove from running queue
            if prompt_id in self.queue_status["running"]:
                self.queue_status["running"].remove(prompt_id)
                
            logger.debug(f"Execution failed: {prompt_id}")
            
    def handle_status(self, data: Dict[str, Any]) -> None:
        """Handle status event (queue updates).
        
        Args:
            data: Event data with exec_info
        """
        exec_info = data.get("exec_info", {})
        self.queue_status["queue_remaining"] = exec_info.get("queue_remaining", 0)
        
    def get_execution_state(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        """Get execution state for a specific prompt.
        
        Args:
            prompt_id: Prompt ID to get state for
            
        Returns:
            Execution state dict or None if not found
        """
        return self.active_executions.get(prompt_id)
        
    def get_all_executions(self) -> Dict[str, Dict[str, Any]]:
        """Get all active executions.
        
        Returns:
            Dict of prompt_id -> execution state
        """
        return self.active_executions.copy()
        
    def get_queue_status(self) -> Dict[str, Any]:
        """Get current queue status.
        
        Returns:
            Queue status dict
        """
        return self.queue_status.copy()
        
    def cleanup_old_executions(self, max_age_hours: int = 24) -> int:
        """Clean up old completed executions.
        
        Args:
            max_age_hours: Maximum age in hours for completed executions
            
        Returns:
            Number of executions cleaned up
        """
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        to_remove = []
        
        for prompt_id, execution in self.active_executions.items():
            if execution["status"] in ["success", "error"]:
                end_time = datetime.fromisoformat(execution.get("end_time", ""))
                if end_time < cutoff:
                    to_remove.append(prompt_id)
                    
        for prompt_id in to_remove:
            del self.active_executions[prompt_id]
            
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old executions")
            
        return len(to_remove)
```

#### 1.3 Update ConnectionManager.__init__

Add error buffer and execution tracker to `ConnectionManager.__init__`:

```python
def __init__(
    self,
    session_timeout_seconds: int = 300,  # 5 minutes
):
    # Existing code...
    self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
    self.session_contexts: Dict[str, SessionContext] = {}
    self.session_agents: Dict[str, Any] = {}  # type: ignore
    self.session_timeout = timedelta(seconds=session_timeout_seconds)
    
    # NEW: Add error tracking
    self.error_buffer = ErrorBuffer(max_size=100)
    self.execution_tracker = ExecutionTracker()
    
    logger.info("ConnectionManager initialized with error tracking")
```

#### 1.4 Add Message Handlers

Add these methods to `ConnectionManager`:

```python
async def handle_comfy_error(self, data: Dict[str, Any]) -> None:
    """Handle error from ComfyUI frontend.
    
    Args:
        data: Error data from ComfyUI
    """
    error_type = data.get("error_type")
    
    if error_type == "execution_error":
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
        
async def handle_queue_status(self, data: Dict[str, Any]) -> None:
    """Handle queue status update from ComfyUI.
    
    Args:
        data: Queue status data
    """
    self.execution_tracker.handle_status(data)
    logger.debug(f"Queue status: {data.get('exec_info', {}).get('queue_remaining', 0)} remaining")
    
async def handle_execution_event(self, event: str, data: Dict[str, Any]) -> None:
    """Handle execution lifecycle events from ComfyUI.
    
    Args:
        event: Event type (start, executing, cached, success)
        data: Event data
    """
    if event == "start":
        self.execution_tracker.handle_execution_start(data)
    elif event == "executing":
        self.execution_tracker.handle_executing(data)
    elif event == "cached":
        self.execution_tracker.handle_execution_cached(data)
    elif event == "success":
        self.execution_tracker.handle_execution_success(data)
```

---

### Step 2: Update WebSocket Message Routing

**File**: `backend/server.py`

Update the WebSocket message handler to route error/queue messages:

```python
# In the websocket_endpoint function, add these message types:

if message_type == "comfy_error":
    await manager.handle_comfy_error(message.get("data", {}))
    
elif message_type == "queue_status":
    await manager.handle_queue_status(message.get("data", {}))
    
elif message_type == "execution_event":
    event = message.get("event")
    data = message.get("data", {})
    await manager.handle_execution_event(event, data)
```

---

### Step 3: Add Frontend Error Listeners

**File**: `web/js/ws_client.js`

#### 3.1 Add ComfyUI API Reference

Add a property to store ComfyUI's API instance:

```javascript
class WSClient extends EventEmitter {
    constructor(sessionId, config = {}) {
        super();
        this.sessionId = sessionId;
        this.ws = null;
        
        // NEW: Store ComfyUI API reference
        this.comfyApi = null;
        
        // ... rest of existing constructor
    }
```

#### 3.2 Add setupComfyListeners Method

Add this method to `WSClient` class:

```javascript
/**
 * Setup listeners for ComfyUI API events
 * Call this after ComfyUI's API is initialized
 */
setupComfyListeners(comfyApi) {
    this.comfyApi = comfyApi;
    console.log('[WSClient] Setting up ComfyUI event listeners');
    
    // Error events
    this.comfyApi.addEventListener("execution_error", (event) => {
        console.error('[WSClient] ComfyUI execution error:', event.detail);
        this.send({
            type: "comfy_error",
            data: {
                error_type: "execution_error",
                ...event.detail,
                timestamp: Date.now()
            }
        });
    });
    
    this.comfyApi.addEventListener("execution_interrupted", (event) => {
        console.warn('[WSClient] ComfyUI execution interrupted:', event.detail);
        this.send({
            type: "comfy_error",
            data: {
                error_type: "execution_interrupted",
                ...event.detail,
                timestamp: Date.now()
            }
        });
    });
    
    // Queue status
    this.comfyApi.addEventListener("status", (event) => {
        this.send({
            type: "queue_status",
            data: event.detail
        });
    });
    
    // Execution tracking
    this.comfyApi.addEventListener("execution_start", (event) => {
        console.log('[WSClient] Execution started:', event.detail.prompt_id);
        this.send({
            type: "execution_event",
            event: "start",
            data: event.detail
        });
    });
    
    this.comfyApi.addEventListener("executing", (event) => {
        this.send({
            type: "execution_event",
            event: "executing",
            data: event.detail
        });
    });
    
    this.comfyApi.addEventListener("execution_cached", (event) => {
        console.log('[WSClient] Execution cached:', event.detail);
        this.send({
            type: "execution_event",
            event: "cached",
            data: event.detail
        });
    });
    
    this.comfyApi.addEventListener("execution_success", (event) => {
        console.log('[WSClient] Execution succeeded:', event.detail.prompt_id);
        this.send({
            type: "execution_event",
            event: "success",
            data: event.detail
        });
    });
    
    console.log('[WSClient] ComfyUI event listeners registered');
}
```

#### 3.3 Update Extension to Call setupComfyListeners

**File**: `web/js/extension.js`

Find where the WSClient is initialized and add:

```javascript
// After wsClient is created and connected:
const wsClient = new WSClient(sessionId);
wsClient.connect();

// NEW: Setup ComfyUI listeners after API is available
wsClient.on('handshake_ack', () => {
    // ComfyUI's API should be available at this point
    if (window.app && window.app.api) {
        wsClient.setupComfyListeners(window.app.api);
    } else {
        console.warn('[Extension] ComfyUI API not available yet');
        // Try again after a delay
        setTimeout(() => {
            if (window.app && window.app.api) {
                wsClient.setupComfyListeners(window.app.api);
            }
        }, 1000);
    }
});
```

---

### Step 4: Add MCP Tools

**File**: `backend/mcp_server.py`

Add these new tools at the end of the file:

```python
from backend.manager import manager

@mcp.tool()
async def get_recent_errors(
    limit: int = 10
) -> Dict[str, Any]:
    """Get recent execution errors from ComfyUI.
    
    Retrieves the N most recent errors that occurred during workflow execution.
    Useful for debugging failed workflows and understanding error patterns.
    
    Args:
        limit: Number of recent errors to retrieve (default: 10, max: 100)
        
    Returns:
        Dict with:
        - errors: List of error objects with full context
        - count: Number of errors returned
        
    """
    limit = min(limit, 100)  # Cap at buffer size
    errors = manager.error_buffer.get_recent_errors(limit)
    return {
        "errors": errors,
        "count": len(errors),
        "total_in_buffer": manager.error_buffer.get_count()
    }

@mcp.tool()
async def get_errors_for_run(
    prompt_id: str
) -> Dict[str, Any]:
    """Get all errors for a specific workflow run.
    
    Retrieves all errors that occurred during a specific workflow execution,
    identified by its prompt_id. Use this to debug why a particular run failed.
    
    Args:
        prompt_id: The prompt/run ID to get errors for (returned from queue_workflow)
        
    Returns:
        Dict with:
        - prompt_id: The requested prompt ID
        - errors: List of errors for that run
        - count: Number of errors found
        
    """
    errors = manager.error_buffer.get_errors_for_prompt(prompt_id)
    return {
        "prompt_id": prompt_id,
        "errors": errors,
        "count": len(errors)
    }

@mcp.tool()
async def get_queue_status() -> Dict[str, Any]:
    """Get current ComfyUI queue status and active executions.
    
    Returns information about currently running and queued workflows,
    including execution progress and node tracking.
    
    Returns:
        Dict with:
        - queue: Queue status (running, pending, queue_remaining)
        - active_executions: Dict of prompt_id -> execution state
        - execution_count: Number of active executions
        
    """
    queue_status = manager.execution_tracker.get_queue_status()
    active_executions = manager.execution_tracker.get_all_executions()
    
    return {
        "queue": queue_status,
        "active_executions": active_executions,
        "execution_count": len(active_executions)
    }

@mcp.tool()
async def get_execution_details(
    prompt_id: str
) -> Dict[str, Any]:
    """Get detailed execution state for a specific workflow run.
    
    Provides comprehensive information about a workflow execution including
    current node, executed nodes, cached nodes, and status.
    
    Args:
        prompt_id: The prompt/run ID to get details for
        
    Returns:
        Dict with execution details or None if not found
        
    """
    execution = manager.execution_tracker.get_execution_state(prompt_id)
    return {
        "prompt_id": prompt_id,
        "found": execution is not None,
        "execution": execution
    }

@mcp.tool()
async def clear_error_buffer() -> Dict[str, Any]:
    """Clear the error buffer.
    
    Removes all stored errors from the buffer. Use this to start fresh
    after fixing issues or when the buffer gets too cluttered.
    
    Returns:
        Confirmation of cleared buffer
        
    """
    previous_count = manager.error_buffer.get_count()
    manager.error_buffer.clear()
    return {
        "cleared": True,
        "previous_count": previous_count
    }
```

---

## ✅ Testing Plan

### Test 1: Error Capture

1. **Create a broken workflow**:
   - Use `create_workflow` with invalid parameters
   - Or manually break a node connection

2. **Queue the workflow**:
   ```python
   result = await queue_workflow(workflow_id="broken_test")
   prompt_id = result["prompt_id"]
   ```

3. **Check for errors**:
   ```python
   # Wait a moment for execution
   await asyncio.sleep(2)
   
   # Get errors for this run
   errors = await get_errors_for_run(prompt_id=prompt_id)
   print(f"Errors: {errors['count']}")
   for error in errors['errors']:
       print(f"  Node: {error['node_type']}")
       print(f"  Error: {error['exception_message']}")
   ```

### Test 2: Queue Tracking

1. **Queue multiple workflows**:
   ```python
   prompt_ids = []
   for i in range(3):
       result = await queue_workflow(workflow_id=f"test_{i}")
       prompt_ids.append(result["prompt_id"])
   ```

2. **Check queue status**:
   ```python
   status = await get_queue_status()
   print(f"Queue remaining: {status['queue']['queue_remaining']}")
   print(f"Active executions: {status['execution_count']}")
   ```

3. **Track execution progress**:
   ```python
   for prompt_id in prompt_ids:
       details = await get_execution_details(prompt_id=prompt_id)
       if details['found']:
           exec_state = details['execution']
           print(f"{prompt_id}: {exec_state['status']}")
           print(f"  Current node: {exec_state['current_node']}")
           print(f"  Executed: {len(exec_state['executed_nodes'])} nodes")
   ```

### Test 3: Connection Validation Errors

1. **Create workflow with missing connection**:
   ```python
   workflow = await create_workflow(workflow_id="connection_test")
   # Add nodes but don't connect them properly
   await add_node(workflow_id="connection_test", node_type="KSampler")
   # Missing required connections
   ```

2. **Try to queue**:
   ```python
   result = await queue_workflow(workflow_id="connection_test")
   # Should get validation error in response
   if not result.get("success"):
       print(f"Validation error: {result.get('error')}")
   ```

### Test 4: Error Buffer Management

1. **Generate multiple errors**:
   ```python
   for i in range(15):
       # Create and queue broken workflows
       pass
   ```

2. **Check recent errors**:
   ```python
   errors = await get_recent_errors(limit=10)
   print(f"Recent errors: {errors['count']}")
   print(f"Total in buffer: {errors['total_in_buffer']}")
   ```

3. **Clear buffer**:
   ```python
   result = await clear_error_buffer()
   print(f"Cleared {result['previous_count']} errors")
   ```

---

## 🔍 Debugging Tips

### Check Frontend Logs

```javascript
// In browser console
console.log(wsClient.getState());
// Should show connected: true, handshakeComplete: true
```

### Check Backend Logs

```bash
# Look for error tracking messages
tail -f backend/logs/server.log | grep -E "(error|Error|ERROR)"
```

### Test ComfyUI Event Flow

```javascript
// In browser console, manually trigger error
window.app.api.dispatchEvent(new CustomEvent("execution_error", {
    detail: {
        prompt_id: "test123",
        node_id: 1,
        node_type: "TestNode",
        exception_message: "Test error",
        exception_type: "TestException",
        traceback: ["line 1", "line 2"],
        executed: []
    }
}));
```

---

## 📝 Implementation Checklist

- [ ] **Backend (manager.py)**
  - [ ] Add `ErrorBuffer` class
  - [ ] Add `ExecutionTracker` class
  - [ ] Update `ConnectionManager.__init__`
  - [ ] Add `handle_comfy_error` method
  - [ ] Add `handle_queue_status` method
  - [ ] Add `handle_execution_event` method

- [ ] **Backend (server.py)**
  - [ ] Add message routing for `comfy_error`
  - [ ] Add message routing for `queue_status`
  - [ ] Add message routing for `execution_event`

- [ ] **Backend (mcp_server.py)**
  - [ ] Add `get_recent_errors` tool
  - [ ] Add `get_errors_for_run` tool
  - [ ] Add `get_queue_status` tool
  - [ ] Add `get_execution_details` tool
  - [ ] Add `clear_error_buffer` tool

- [ ] **Frontend (ws_client.js)**
  - [ ] Add `comfyApi` property
  - [ ] Add `setupComfyListeners` method
  - [ ] Add event listeners for all error types
  - [ ] Add event listeners for execution tracking
  - [ ] Add event listeners for queue status

- [ ] **Frontend (extension.js)**
  - [ ] Call `setupComfyListeners` after handshake
  - [ ] Add retry logic if API not immediately available

- [ ] **Testing**
  - [ ] Test error capture with broken workflow
  - [ ] Test queue tracking with multiple workflows
  - [ ] Test connection validation errors
  - [ ] Test error buffer management
  - [ ] Test MCP tools from agent

---

## 🚀 Next Steps After Implementation

1. **Monitor error patterns** - Use `get_recent_errors` to identify common failures
2. **Improve error messages** - Add more context to help users debug
3. **Add error recovery** - Implement automatic retry for transient errors
4. **Performance monitoring** - Track execution times and bottlenecks
5. **Error analytics** - Aggregate error data for insights

---

## 📚 Related Documentation

- [research.md](./research.md) - Detailed research on ComfyUI error handling
- `backend/manager.py` - Connection manager implementation
- `web/js/ws_client.js` - WebSocket client implementation
- `backend/mcp_server.py` - MCP tool definitions

---

**Status**: Ready for implementation  
**Estimated Time**: 2-3 hours  
**Complexity**: Medium
