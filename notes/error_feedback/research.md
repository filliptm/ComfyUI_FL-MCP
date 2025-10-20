# ComfyUI Error Feedback & Queue Status Research

**Research Date**: 2025-10-18  
**Purpose**: Understand how to capture execution errors, queue status, and provide feedback via WebSocket to backend

---

## 📋 Table of Contents

1. [WebSocket Message Types](#websocket-message-types)
2. [Error Message Structure](#error-message-structure)
3. [Queue Status Tracking](#queue-status-tracking)
4. [Implementation Strategy](#implementation-strategy)
5. [Code Examples](#code-examples)

---

## 🔌 WebSocket Message Types

### Built-in ComfyUI Message Types

ComfyUI sends these message types via WebSocket during workflow execution:

| Message Type | When Sent | Data Structure |
|--------------|-----------|----------------|
| `execution_start` | When a prompt is about to run | `{prompt_id}` |
| `execution_error` | When an error occurs during execution | See [Error Structure](#error-message-structure) |
| `execution_interrupted` | When stopped by InterruptProcessingException | `{prompt_id, node_id, node_type, executed[]}` |
| `execution_cached` | At the start of execution | `{prompt_id, nodes[]}` |
| `execution_success` | When all nodes executed successfully | `{prompt_id, timestamp}` |
| `executing` | When a new node is about to execute | `{node, prompt_id}` |
| `executed` | When a node returns a UI element | `{node, prompt_id, output}` |
| `progress` | During execution of nodes with progress hooks | `{node, prompt_id, value, max}` |
| `status` | When the queue state changes | `{exec_info: {queue_remaining}}` |

### Message Flow Pattern

```
execution_start
  ↓
execution_cached (nodes already cached)
  ↓
status (queue updates)
  ↓
executing (node_id) → progress → executed
  ↓
executing (node_id) → progress → executed
  ↓
...
  ↓
executing (null) [completion signal]
  ↓
execution_success OR execution_error
```

---

## ❌ Error Message Structure

### `execution_error` Message Format

From `execution.py` lines 647-661:

```python
mes = {
    "prompt_id": prompt_id,
    "node_id": node_id,           # Real node ID (not display ID)
    "node_type": class_type,       # Node class name
    "executed": list(executed),    # List of successfully executed node IDs
    "exception_message": error["exception_message"],  # Error message + tips
    "exception_type": exception_type,  # Full qualified type name
    "traceback": error["traceback"],   # List of traceback strings
    "current_inputs": error["current_inputs"],  # Formatted input values
    "current_outputs": list(current_outputs),   # List of output node IDs
}
```

### Error Details from `execute()` Function

From `execution.py` lines 609-626:

```python
error_details = {
    "node_id": real_node_id,
    "exception_message": "{}\n{}".format(ex, tips),
    "exception_type": exception_type,  # e.g., "builtins.ValueError"
    "traceback": traceback.format_tb(tb),  # List of formatted traceback lines
    "current_inputs": input_data_formatted  # {name: [formatted_values]}
}
```

### Input Formatting

From `execution.py` lines 602-606:

```python
input_data_formatted = {}
for name, inputs in input_data_all.items():
    input_data_formatted[name] = [format_value(x) for x in inputs]

# format_value converts to None, int, float, bool, str, or str(x)
```

### Special Error Types

#### 1. Execution Blocked

From `execution.py` lines 556-567:

```python
mes = {
    "prompt_id": prompt_id,
    "node_id": unique_id,
    "node_type": class_type,
    "executed": list(executed),
    "exception_message": f"Execution Blocked: {block.message}",
    "exception_type": "ExecutionBlocked",
    "traceback": [],
    "current_inputs": [],
    "current_outputs": [],
}
```

#### 2. Interrupt Processing

From `execution.py` lines 644-650:

```python
mes = {
    "prompt_id": prompt_id,
    "node_id": node_id,
    "node_type": class_type,
    "executed": list(executed),
}
# Message type: "execution_interrupted" instead of "execution_error"
```

#### 3. Out of Memory (OOM)

From `execution.py` lines 613-615:

```python
if isinstance(ex, comfy.model_management.OOM_EXCEPTION):
    tips = "This error means you ran out of memory on your GPU.\n\nTIPS: If the workflow worked before you might have accidentally set the batch_size to a large number."
    # Appended to exception_message
```

---

## 📊 Queue Status Tracking

### Queue State Messages

From official docs:

```javascript
api.addEventListener("status", statusHandler);

function statusHandler(event) {
    const queueRemaining = event.detail.exec_info.queue_remaining;
    // Number of entries still in execution queue
}
```

### Execution State Tracking

```javascript
// Track which node is currently executing
api.addEventListener("executing", executingHandler);

function executingHandler(event) {
    const nodeId = event.detail.node;  // null when complete
    const promptId = event.detail.prompt_id;
    
    if (nodeId === null) {
        // Workflow execution complete
    }
}
```

### Queue Information via HTTP API

```python
# Get current queue status
GET http://{server_address}/queue

# Response structure (inferred):
{
    "Running": [...],  # Currently executing prompts
    "Pending": [...]   # Queued prompts
}
```

### History Retrieval

```python
# Get execution history for a specific prompt
GET http://{server_address}/history/{prompt_id}

# Returns full execution results including errors
```

---

## 🎯 Implementation Strategy

### Phase 1: Frontend Error Capture

**Goal**: Capture all execution errors and queue status in the frontend

**Location**: `web/js/ws_client.js`

```javascript
class WSClient {
    constructor() {
        this.errorBuffer = [];  // Circular buffer
        this.maxErrorBufferSize = 50;
        this.currentExecutions = new Map();  // prompt_id -> execution state
        
        // Register error listeners
        this.api.addEventListener("execution_error", this.handleExecutionError.bind(this));
        this.api.addEventListener("execution_interrupted", this.handleExecutionInterrupted.bind(this));
        this.api.addEventListener("execution_start", this.handleExecutionStart.bind(this));
        this.api.addEventListener("execution_success", this.handleExecutionSuccess.bind(this));
        this.api.addEventListener("executing", this.handleExecuting.bind(this));
        this.api.addEventListener("status", this.handleStatus.bind(this));
    }
    
    handleExecutionError(event) {
        const errorData = {
            type: "execution_error",
            timestamp: Date.now(),
            ...event.detail  // Contains all error fields
        };
        
        this.addToErrorBuffer(errorData);
        this.sendToBackend("error_event", errorData);
    }
    
    addToErrorBuffer(error) {
        this.errorBuffer.push(error);
        if (this.errorBuffer.length > this.maxErrorBufferSize) {
            this.errorBuffer.shift();  // Remove oldest
        }
    }
}
```

### Phase 2: Backend Error Buffer

**Goal**: Store error history on backend with run context

**Location**: `backend/ws_manager.py`

```python
from collections import deque
from datetime import datetime
from typing import Dict, List, Any

class ErrorBuffer:
    def __init__(self, max_size: int = 100):
        self.errors = deque(maxlen=max_size)
        self.errors_by_prompt = {}  # prompt_id -> [errors]
        
    def add_error(self, error_data: Dict[str, Any]):
        """Add error to buffer with timestamp and context"""
        error_entry = {
            "timestamp": datetime.now().isoformat(),
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
        
        self.errors.append(error_entry)
        
        # Index by prompt_id for quick lookup
        prompt_id = error_entry["prompt_id"]
        if prompt_id not in self.errors_by_prompt:
            self.errors_by_prompt[prompt_id] = []
        self.errors_by_prompt[prompt_id].append(error_entry)
        
    def get_recent_errors(self, limit: int = 10) -> List[Dict]:
        """Get N most recent errors"""
        return list(self.errors)[-limit:]
        
    def get_errors_for_prompt(self, prompt_id: str) -> List[Dict]:
        """Get all errors for a specific prompt/run"""
        return self.errors_by_prompt.get(prompt_id, [])
        
    def clear(self):
        """Clear all errors"""
        self.errors.clear()
        self.errors_by_prompt.clear()
```

### Phase 3: Queue Status Tracking

**Goal**: Track queue state and execution progress

```python
class ExecutionTracker:
    def __init__(self):
        self.active_executions = {}  # prompt_id -> execution_state
        self.queue_status = {
            "running": [],
            "pending": [],
            "queue_remaining": 0
        }
        
    def handle_execution_start(self, data: Dict):
        prompt_id = data["prompt_id"]
        self.active_executions[prompt_id] = {
            "status": "running",
            "start_time": datetime.now().isoformat(),
            "current_node": None,
            "executed_nodes": [],
            "cached_nodes": [],
        }
        
    def handle_executing(self, data: Dict):
        prompt_id = data["prompt_id"]
        node_id = data["node"]
        
        if prompt_id in self.active_executions:
            if node_id is None:
                # Execution complete
                self.active_executions[prompt_id]["current_node"] = None
            else:
                self.active_executions[prompt_id]["current_node"] = node_id
                self.active_executions[prompt_id]["executed_nodes"].append(node_id)
                
    def handle_execution_cached(self, data: Dict):
        prompt_id = data["prompt_id"]
        if prompt_id in self.active_executions:
            self.active_executions[prompt_id]["cached_nodes"] = data.get("nodes", [])
            
    def handle_status(self, data: Dict):
        exec_info = data.get("exec_info", {})
        self.queue_status["queue_remaining"] = exec_info.get("queue_remaining", 0)
        
    def get_execution_state(self, prompt_id: str) -> Dict:
        return self.active_executions.get(prompt_id, {})
```

### Phase 4: MCP Tool Integration

**New MCP Tools**:

```python
# In mcp_server.py

@mcp.tool()
async def get_recent_errors(
    limit: int = 10
) -> Dict[str, Any]:
    """Get recent execution errors from ComfyUI
    
    Args:
        limit: Number of recent errors to retrieve (default: 10)
        
    Returns:
        List of error objects with full context
    """
    errors = ws_manager.error_buffer.get_recent_errors(limit)
    return {
        "errors": errors,
        "count": len(errors)
    }

@mcp.tool()
async def get_errors_for_run(
    prompt_id: str
) -> Dict[str, Any]:
    """Get all errors for a specific workflow run
    
    Args:
        prompt_id: The prompt/run ID to get errors for
        
    Returns:
        List of errors that occurred during that run
    """
    errors = ws_manager.error_buffer.get_errors_for_prompt(prompt_id)
    return {
        "prompt_id": prompt_id,
        "errors": errors,
        "count": len(errors)
    }

@mcp.tool()
async def get_queue_status() -> Dict[str, Any]:
    """Get current queue status and active executions
    
    Returns:
        Queue status including running/pending prompts and execution states
    """
    return {
        "queue": ws_manager.execution_tracker.queue_status,
        "active_executions": ws_manager.execution_tracker.active_executions
    }

@mcp.tool()
async def clear_error_buffer() -> Dict[str, Any]:
    """Clear the error buffer
    
    Returns:
        Confirmation of cleared buffer
    """
    ws_manager.error_buffer.clear()
    return {"cleared": True}
```

---

## 💻 Code Examples

### Frontend: Registering Error Listeners

```javascript
// In web/js/ws_client.js

class WSClient {
    setupComfyListeners() {
        // Error events
        this.api.addEventListener("execution_error", (event) => {
            console.error("[ComfyUI] Execution error:", event.detail);
            this.send({
                type: "comfy_error",
                data: {
                    error_type: "execution_error",
                    ...event.detail,
                    timestamp: Date.now()
                }
            });
        });
        
        this.api.addEventListener("execution_interrupted", (event) => {
            console.warn("[ComfyUI] Execution interrupted:", event.detail);
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
        this.api.addEventListener("status", (event) => {
            this.send({
                type: "queue_status",
                data: event.detail
            });
        });
        
        // Execution tracking
        this.api.addEventListener("execution_start", (event) => {
            this.send({
                type: "execution_event",
                event: "start",
                data: event.detail
            });
        });
        
        this.api.addEventListener("executing", (event) => {
            this.send({
                type: "execution_event",
                event: "executing",
                data: event.detail
            });
        });
        
        this.api.addEventListener("execution_success", (event) => {
            this.send({
                type: "execution_event",
                event: "success",
                data: event.detail
            });
        });
    }
}
```

### Backend: Handling Error Events

```python
# In backend/ws_manager.py

class WSManager:
    def __init__(self):
        self.error_buffer = ErrorBuffer(max_size=100)
        self.execution_tracker = ExecutionTracker()
        self.message_handlers = {
            "comfy_error": self.handle_comfy_error,
            "queue_status": self.handle_queue_status,
            "execution_event": self.handle_execution_event,
        }
        
    async def handle_comfy_error(self, data: Dict):
        """Handle error from ComfyUI frontend"""
        error_type = data.get("error_type")
        
        if error_type == "execution_error":
            self.error_buffer.add_error(data)
            logger.error(
                f"ComfyUI execution error in node {data.get('node_id')} "
                f"({data.get('node_type')}): {data.get('exception_message')}"
            )
        elif error_type == "execution_interrupted":
            logger.warning(
                f"ComfyUI execution interrupted at node {data.get('node_id')}"
            )
            
    async def handle_queue_status(self, data: Dict):
        """Handle queue status update"""
        self.execution_tracker.handle_status(data)
        
    async def handle_execution_event(self, message: Dict):
        """Handle execution lifecycle events"""
        event = message.get("event")
        data = message.get("data")
        
        if event == "start":
            self.execution_tracker.handle_execution_start(data)
        elif event == "executing":
            self.execution_tracker.handle_executing(data)
        elif event == "success":
            prompt_id = data.get("prompt_id")
            if prompt_id in self.execution_tracker.active_executions:
                self.execution_tracker.active_executions[prompt_id]["status"] = "success"
                self.execution_tracker.active_executions[prompt_id]["end_time"] = datetime.now().isoformat()
```

---

## 🚨 Important Notes

### Connection Missing Errors

**Problem**: When `connect_nodes` fails, ComfyUI doesn't send an `execution_error` - it fails during validation.

**Solution**: These are caught during `queue_workflow` and returned in the HTTP response:

```python
# When queueing fails validation
response = requests.post(f"{server}/prompt", json=prompt_data)
if response.status_code != 200:
    # Validation error
    error_data = response.json()
    # error_data contains node_errors with validation failures
```

**Detection**: Look for `node_errors` in the queue response, not WebSocket messages.

### Error Buffer Size

- Frontend: 50 errors (memory constrained)
- Backend: 100 errors (more capacity)
- Indexed by `prompt_id` for quick lookup

### WebSocket Reliability

From GitHub issue #3128:
- WebSocket can hang during intensive operations
- Implement client-side timeouts
- Consider HTTP polling as fallback for critical status

---

## 📚 References

- [ComfyUI Official Docs - Messages](https://docs.comfy.org/development/comfyui-server/comms_messages)
- [ComfyUI execution.py source](https://github.com/comfyanonymous/ComfyUI/blob/master/execution.py)
- [WebSockets & ComfyUI Tutorial](https://dev.to/worldlinetech/websockets-comfyui-building-interactive-ai-applications-1j1g)
- [GitHub Issue #3128 - WebSocket Stability](https://github.com/comfyanonymous/ComfyUI/issues/3128)

---

## ✅ Next Steps

1. **Implement ErrorBuffer class** in `backend/ws_manager.py`
2. **Implement ExecutionTracker class** in `backend/ws_manager.py`
3. **Add error listeners** in `web/js/ws_client.js`
4. **Add MCP tools** for error/queue retrieval
5. **Test error capture** with intentionally broken workflows
6. **Test queue tracking** with multiple queued workflows
