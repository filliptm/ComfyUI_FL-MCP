# MCP Tools Implementation Plan

## Tool Categories Overview

All tools follow the same pattern:
1. Defined in `backend/mcp_server.py` using FastMCP
2. Callback to frontend via WebSocket
3. Frontend executes FL_JS function
4. Result returned to agent

## Tool Implementation Pattern

### Backend Tool Definition
```python
# backend/mcp_server.py

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import uuid

mcp = FastMCP("FL_JS Workflow Tools")

# Example tool
@mcp.tool()
async def create_node(
    node_type: str = Field(..., description="Type of node to create (e.g., 'CheckpointLoaderSimple')"),
    parameters: Optional[Dict[str, Any]] = Field(None, description="Node parameter values"),
    position: Optional[Dict[str, float]] = Field(None, description="Node position {x, y}")
) -> Dict[str, Any]:
    """
    Create a new node in the workflow.
    
    Returns the created node's ID and details.
    """
    from backend.callback_router import execute_tool_callback
    
    request_id = str(uuid.uuid4())
    
    result = await execute_tool_callback(
        request_id=request_id,
        tool_name="create_node",
        parameters={
            "node_type": node_type,
            "parameters": parameters or {},
            "position": position
        },
        timeout_ms=5000
    )
    
    return result
```

### Frontend Tool Handler
```javascript
// frontend/tool_executor.js

class ToolExecutor {
    constructor(flApi) {
        this.flApi = flApi;
        this.toolHandlers = this.registerHandlers();
    }
    
    registerHandlers() {
        return {
            'create_node': this.handleCreateNode.bind(this),
            'remove_nodes': this.handleRemoveNodes.bind(this),
            // ... other handlers
        };
    }
    
    async handleCreateNode(params) {
        try {
            const { node_type, parameters, position } = params;
            
            // Call FL_JS API
            const node = await this.flApi.create(node_type, parameters, position);
            
            return {
                success: true,
                data: {
                    id: node.id,
                    type: node.type,
                    title: node.title,
                    position: { x: node.pos[0], y: node.pos[1] }
                }
            };
        } catch (error) {
            return {
                success: false,
                error: error.message
            };
        }
    }
}
```

## Tool Categories

### 1. Node Management Tools (11 tools)

#### find_node
```python
@mcp.tool()
async def find_node(
    node_id: Optional[int] = None,
    node_type: Optional[str] = None,
    title: Optional[str] = None,
    find_last: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Find a node by ID, type, or title.
    
    Args:
        node_id: Specific node ID to find
        node_type: Type of node to find
        title: Title or partial title to match
        find_last: If True, find the last matching node instead of first
    
    Returns:
        Node details or None if not found
    """
    # Implementation via callback
```

#### create_node
```python
@mcp.tool()
async def create_node(
    node_type: str,
    parameters: Optional[Dict[str, Any]] = None,
    position: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    """
    Create a new node in the workflow.
    
    Args:
        node_type: Type of node (e.g., 'CheckpointLoaderSimple')
        parameters: Node parameter values
        position: Position {x, y}
    
    Returns:
        Created node details
    """
```

#### remove_nodes
```python
@mcp.tool()
async def remove_nodes(
    node_ids: List[int]
) -> Dict[str, Any]:
    """
    Remove one or more nodes from the workflow.
    
    Args:
        node_ids: List of node IDs to remove
    
    Returns:
        Number of nodes removed
    """
```

#### bypass_nodes
```python
@mcp.tool()
async def bypass_nodes(
    node_ids: List[int]
) -> Dict[str, Any]:
    """
    Bypass (mute) one or more nodes.
    
    Args:
        node_ids: List of node IDs to bypass
    
    Returns:
        Number of nodes bypassed
    """
```

#### unbypass_nodes
```python
@mcp.tool()
async def unbypass_nodes(
    node_ids: List[int]
) -> Dict[str, Any]:
    """
    Unbypass (unmute) one or more nodes.
    
    Args:
        node_ids: List of node IDs to unbypass
    
    Returns:
        Number of nodes unbypassed
    """
```

#### pin_nodes
```python
@mcp.tool()
async def pin_nodes(
    node_ids: List[int]
) -> Dict[str, Any]:
    """
    Pin one or more nodes (prevent movement).
    
    Args:
        node_ids: List of node IDs to pin
    
    Returns:
        Number of nodes pinned
    """
```

#### unpin_nodes
```python
@mcp.tool()
async def unpin_nodes(
    node_ids: List[int]
) -> Dict[str, Any]:
    """
    Unpin one or more nodes (allow movement).
    
    Args:
        node_ids: List of node IDs to unpin
    
    Returns:
        Number of nodes unpinned
    """
```

#### select_nodes
```python
@mcp.tool()
async def select_nodes(
    node_ids: List[int],
    clear_selection: bool = True
) -> Dict[str, Any]:
    """
    Select one or more nodes in the UI.
    
    Args:
        node_ids: List of node IDs to select
        clear_selection: Whether to clear existing selection first
    
    Returns:
        Number of nodes selected
    """
```

### 2. Node Manipulation Tools (3 tools)

#### get_node_values
```python
@mcp.tool()
async def get_node_values(
    node_id: int,
    parameter_names: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Get parameter values from a node.
    
    Args:
        node_id: Node ID to get values from
        parameter_names: Specific parameters to get (None = all)
    
    Returns:
        Dictionary of parameter names and values
    """
```

#### set_node_values
```python
@mcp.tool()
async def set_node_values(
    node_id: int,
    values: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Set parameter values on a node.
    
    Args:
        node_id: Node ID to set values on
        values: Dictionary of parameter names and new values
    
    Returns:
        Updated parameter values
    """
```

#### connect_nodes
```python
@mcp.tool()
async def connect_nodes(
    source_node_id: int,
    source_slot: str,
    target_node_id: int,
    target_slot: str
) -> Dict[str, Any]:
    """
    Connect an output slot to an input slot.
    
    Args:
        source_node_id: Source node ID
        source_slot: Output slot name (e.g., 'MODEL')
        target_node_id: Target node ID
        target_slot: Input slot name (e.g., 'model')
    
    Returns:
        Connection details
    """
```

### 3. Layout Management Tools (8 tools)

#### position_node_left
```python
@mcp.tool()
async def position_node_left(
    node_id: int,
    target_node_id: int,
    spacing: int = 50
) -> Dict[str, Any]:
    """
    Position a node to the left of another node.
    
    Args:
        node_id: Node to position
        target_node_id: Reference node
        spacing: Horizontal spacing in pixels
    
    Returns:
        New position of the node
    """
```

#### position_node_right
```python
@mcp.tool()
async def position_node_right(
    node_id: int,
    target_node_id: int,
    spacing: int = 50
) -> Dict[str, Any]:
    """Position a node to the right of another node."""
```

#### position_node_top
```python
@mcp.tool()
async def position_node_top(
    node_id: int,
    target_node_id: int,
    spacing: int = 50
) -> Dict[str, Any]:
    """Position a node above another node."""
```

#### position_node_bottom
```python
@mcp.tool()
async def position_node_bottom(
    node_id: int,
    target_node_id: int,
    spacing: int = 50
) -> Dict[str, Any]:
    """Position a node below another node."""
```

#### move_node_right
```python
@mcp.tool()
async def move_node_right(
    node_id: int
) -> Dict[str, Any]:
    """
    Move a node to the rightmost position in the workflow.
    
    Args:
        node_id: Node to move
    
    Returns:
        New position of the node
    """
```

#### move_node_bottom
```python
@mcp.tool()
async def move_node_bottom(
    node_id: int
) -> Dict[str, Any]:
    """Move a node to the bottom position in the workflow."""
```

#### get_node_rect
```python
@mcp.tool()
async def get_node_rect(
    node_id: int
) -> Dict[str, Any]:
    """
    Get the bounding rectangle of a node.
    
    Args:
        node_id: Node ID
    
    Returns:
        {x, y, width, height}
    """
```

#### set_node_rect
```python
@mcp.tool()
async def set_node_rect(
    node_id: int,
    x: float,
    y: float,
    width: Optional[float] = None,
    height: Optional[float] = None
) -> Dict[str, Any]:
    """
    Set the position and optionally size of a node.
    
    Args:
        node_id: Node ID
        x: X position
        y: Y position
        width: Width (optional)
        height: Height (optional)
    
    Returns:
        Updated rectangle
    """
```

### 4. Workflow Control Tools (6 tools)

#### queue_workflow
```python
@mcp.tool()
async def queue_workflow(
    batch_count: Optional[int] = None
) -> Dict[str, Any]:
    """
    Queue the workflow for execution.
    
    Args:
        batch_count: Number of batches to queue (uses current if None)
    
    Returns:
        Queue details (prompt_id, number)
    """
```

#### cancel_workflow
```python
@mcp.tool()
async def cancel_workflow() -> Dict[str, Any]:
    """
    Cancel the current workflow execution.
    
    Returns:
        Cancellation status
    """
```

#### enable_auto_queue
```python
@mcp.tool()
async def enable_auto_queue() -> Dict[str, Any]:
    """
    Enable auto-queue mode.
    
    Returns:
        Auto-queue status
    """
```

#### disable_auto_queue
```python
@mcp.tool()
async def disable_auto_queue() -> Dict[str, Any]:
    """Disable auto-queue mode."""
```

#### set_batch_count
```python
@mcp.tool()
async def set_batch_count(
    count: int
) -> Dict[str, Any]:
    """
    Set the batch count for workflow execution.
    
    Args:
        count: Number of batches
    
    Returns:
        New batch count
    """
```

#### get_queue_status
```python
@mcp.tool()
async def get_queue_status() -> Dict[str, Any]:
    """
    Get the current queue status.
    
    Returns:
        Queue details (running, pending)
    """
```

### 5. System Control Tools (5 tools)

#### disable_sleep
```python
@mcp.tool()
async def disable_sleep() -> Dict[str, Any]:
    """Disable system sleep mode."""
```

#### enable_sleep
```python
@mcp.tool()
async def enable_sleep() -> Dict[str, Any]:
    """Enable system sleep mode."""
```

#### disable_screensaver
```python
@mcp.tool()
async def disable_screensaver() -> Dict[str, Any]:
    """Disable screensaver."""
```

#### enable_screensaver
```python
@mcp.tool()
async def enable_screensaver() -> Dict[str, Any]:
    """Enable screensaver."""
```

#### send_images
```python
@mcp.tool()
async def send_images(
    url: str,
    method: str = "POST"
) -> Dict[str, Any]:
    """
    Configure image sending to a URL.
    
    Args:
        url: URL to send images to
        method: HTTP method (POST, PUT, etc.)
    
    Returns:
        Configuration status
    """
```

### 6. Utility Tools (4 tools)

#### generate_seed
```python
@mcp.tool()
async def generate_seed() -> int:
    """
    Generate a random seed value.
    
    Returns:
        Random integer seed
    """
```

#### generate_float
```python
@mcp.tool()
async def generate_float(
    min_value: float = 0.0,
    max_value: float = 1.0
) -> float:
    """
    Generate a random float value.
    
    Args:
        min_value: Minimum value
        max_value: Maximum value
    
    Returns:
        Random float
    """
```

#### generate_int
```python
@mcp.tool()
async def generate_int(
    min_value: int = 0,
    max_value: int = 100
) -> int:
    """
    Generate a random integer value.
    
    Args:
        min_value: Minimum value
        max_value: Maximum value
    
    Returns:
        Random integer
    """
```

#### random_choice
```python
@mcp.tool()
async def random_choice(
    choices: List[Any]
) -> Any:
    """
    Choose a random item from a list.
    
    Args:
        choices: List of items to choose from
    
    Returns:
        Random item from the list
    """
```

### 7. Query & Visualization Tools (3 tools)

#### query_workflow
```python
@mcp.tool()
async def query_workflow(
    query: WorkflowQuery
) -> Union[List[Dict], Dict[str, Any], str]:
    """
    Query the workflow graph using structured query language.
    
    Args:
        query: WorkflowQuery object with filters, traversal, etc.
    
    Returns:
        Query results in specified format
    """
```

#### workflow_overview
```python
@mcp.tool()
async def workflow_overview(
    node_ids: Optional[List[int]] = None,
    include_params: bool = False,
    max_nodes: Optional[int] = None
) -> str:
    """
    Generate a Mermaid diagram of the workflow.
    
    Args:
        node_ids: Specific nodes to include (None = all)
        include_params: Whether to show parameter values
        max_nodes: Maximum nodes to include
    
    Returns:
        Mermaid diagram string
    """
```

#### get_workflow_stats
```python
@mcp.tool()
async def get_workflow_stats() -> Dict[str, Any]:
    """
    Get statistics about the current workflow.
    
    Returns:
        Statistics including node counts, connection counts, etc.
    """
```

## Callback Router Implementation

```python
# backend/callback_router.py

import asyncio
import uuid
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

class CallbackRouter:
    def __init__(self, connection_manager):
        self.connection_manager = connection_manager
        self.pending_callbacks: Dict[str, asyncio.Future] = {}
    
    async def execute_tool_callback(
        self,
        session_id: str,
        request_id: str,
        tool_name: str,
        parameters: Dict[str, Any],
        timeout_ms: int = 30000
    ) -> Dict[str, Any]:
        """
        Execute a tool by sending callback request to frontend.
        
        Args:
            session_id: Session ID for routing
            request_id: Unique request ID
            tool_name: Name of the tool to execute
            parameters: Tool parameters
            timeout_ms: Timeout in milliseconds
        
        Returns:
            Tool execution result
        
        Raises:
            TimeoutError: If tool execution times out
            RuntimeError: If tool execution fails
        """
        # Create future for result
        future = asyncio.Future()
        self.pending_callbacks[request_id] = future
        
        # Send tool request to client
        await self.connection_manager.send_message(session_id, {
            "type": "tool_request",
            "session_id": session_id,
            "request_id": request_id,
            "tool_name": tool_name,
            "parameters": parameters,
            "timeout_ms": timeout_ms
        })
        
        # Wait for result with timeout
        try:
            result = await asyncio.wait_for(
                future,
                timeout=timeout_ms / 1000
            )
            return result
        except asyncio.TimeoutError:
            del self.pending_callbacks[request_id]
            raise TimeoutError(f"Tool '{tool_name}' execution timed out after {timeout_ms}ms")
        finally:
            # Clean up
            if request_id in self.pending_callbacks:
                del self.pending_callbacks[request_id]
    
    async def handle_tool_result(
        self,
        request_id: str,
        result: Dict[str, Any]
    ):
        """
        Handle tool result from frontend.
        
        Args:
            request_id: Request ID
            result: Tool execution result
        """
        if request_id in self.pending_callbacks:
            future = self.pending_callbacks[request_id]
            
            if result.get("success"):
                future.set_result(result.get("data"))
            else:
                future.set_exception(
                    RuntimeError(result.get("error", "Unknown error"))
                )

# Global instance
callback_router = None

def init_callback_router(connection_manager):
    global callback_router
    callback_router = CallbackRouter(connection_manager)
    return callback_router

async def execute_tool_callback(
    request_id: str,
    tool_name: str,
    parameters: Dict[str, Any],
    timeout_ms: int = 30000
) -> Dict[str, Any]:
    """Convenience function for tool execution."""
    # Get session_id from context (set by WebSocket handler)
    from contextvars import ContextVar
    session_id_var: ContextVar[str] = ContextVar('session_id')
    session_id = session_id_var.get()
    
    return await callback_router.execute_tool_callback(
        session_id=session_id,
        request_id=request_id,
        tool_name=tool_name,
        parameters=parameters,
        timeout_ms=timeout_ms
    )
```

## Frontend Tool Executor (Complete)

```javascript
// frontend/tool_executor.js

class ToolExecutor {
    constructor(flApi, queryExecutor, diagramGenerator) {
        this.flApi = flApi;
        this.queryExecutor = queryExecutor;
        this.diagramGenerator = diagramGenerator;
        this.executionLog = [];
    }
    
    async execute(toolRequest) {
        const { request_id, tool_name, parameters } = toolRequest;
        const startTime = performance.now();
        
        try {
            // Route to appropriate handler
            const handler = this.getHandler(tool_name);
            if (!handler) {
                throw new Error(`Unknown tool: ${tool_name}`);
            }
            
            // Execute tool
            const result = await handler(parameters);
            
            const executionTime = performance.now() - startTime;
            
            // Log execution
            this.logExecution({
                request_id,
                tool_name,
                parameters,
                result,
                execution_time_ms: executionTime,
                success: true
            });
            
            return {
                success: true,
                data: result,
                execution_time_ms: executionTime
            };
        } catch (error) {
            const executionTime = performance.now() - startTime;
            
            // Log error
            this.logExecution({
                request_id,
                tool_name,
                parameters,
                error: error.message,
                execution_time_ms: executionTime,
                success: false
            });
            
            return {
                success: false,
                error: error.message,
                execution_time_ms: executionTime
            };
        }
    }
    
    getHandler(toolName) {
        const handlers = {
            // Node Management
            'find_node': (p) => this.flApi.find(p.node_id, p.node_type, p.title, p.find_last),
            'create_node': (p) => this.flApi.create(p.node_type, p.parameters, p.position),
            'remove_nodes': (p) => this.flApi.remove(p.node_ids),
            'bypass_nodes': (p) => this.flApi.bypass(p.node_ids),
            'unbypass_nodes': (p) => this.flApi.unbypass(p.node_ids),
            'pin_nodes': (p) => this.flApi.pin(p.node_ids),
            'unpin_nodes': (p) => this.flApi.unpin(p.node_ids),
            'select_nodes': (p) => this.flApi.select(p.node_ids, p.clear_selection),
            
            // Node Manipulation
            'get_node_values': (p) => this.flApi.getValues(p.node_id, p.parameter_names),
            'set_node_values': (p) => this.flApi.setValues(p.node_id, p.values),
            'connect_nodes': (p) => this.flApi.connect(
                p.source_node_id, p.source_slot,
                p.target_node_id, p.target_slot
            ),
            
            // Layout Management
            'position_node_left': (p) => this.flApi.putOnLeft(p.node_id, p.target_node_id, p.spacing),
            'position_node_right': (p) => this.flApi.putOnRight(p.node_id, p.target_node_id, p.spacing),
            'position_node_top': (p) => this.flApi.putOnTop(p.node_id, p.target_node_id, p.spacing),
            'position_node_bottom': (p) => this.flApi.putOnBottom(p.node_id, p.target_node_id, p.spacing),
            'move_node_right': (p) => this.flApi.moveToRight(p.node_id),
            'move_node_bottom': (p) => this.flApi.moveToBottom(p.node_id),
            'get_node_rect': (p) => this.flApi.getRect(p.node_id),
            'set_node_rect': (p) => this.flApi.setRect(p.node_id, p.x, p.y, p.width, p.height),
            
            // Workflow Control
            'queue_workflow': (p) => this.flApi.generate(p.batch_count),
            'cancel_workflow': () => this.flApi.cancel(),
            'enable_auto_queue': () => this.flApi.enableAutoQueue(),
            'disable_auto_queue': () => this.flApi.disableAutoQueue(),
            'set_batch_count': (p) => this.flApi.setBatchCount(p.count),
            'get_queue_status': () => this.flApi.getQueueStatus(),
            
            // System Control
            'disable_sleep': () => this.flApi.disableSleep(),
            'enable_sleep': () => this.flApi.enableSleep(),
            'disable_screensaver': () => this.flApi.disableScreenSaver(),
            'enable_screensaver': () => this.flApi.enableScreenSaver(),
            'send_images': (p) => this.flApi.sendImages(p.url, p.method),
            
            // Utilities
            'generate_seed': () => this.flApi.generateSeed(),
            'generate_float': (p) => this.flApi.generateFloat(p.min_value, p.max_value),
            'generate_int': (p) => this.flApi.generateInt(p.min_value, p.max_value),
            'random_choice': (p) => this.flApi.random(p.choices),
            
            // Query & Visualization
            'query_workflow': (p) => this.queryExecutor.execute(p.query),
            'workflow_overview': (p) => this.diagramGenerator.generate(p.node_ids, p.include_params, p.max_nodes),
            'get_workflow_stats': () => this.flApi.getWorkflowStats()
        };
        
        return handlers[toolName];
    }
    
    logExecution(logEntry) {
        this.executionLog.push({
            ...logEntry,
            timestamp: new Date().toISOString()
        });
        
        // Keep only last 100 entries
        if (this.executionLog.length > 100) {
            this.executionLog.shift();
        }
        
        // Console log for debugging
        if (logEntry.success) {
            console.log(`[Tool] ${logEntry.tool_name} completed in ${logEntry.execution_time_ms.toFixed(2)}ms`);
        } else {
            console.error(`[Tool] ${logEntry.tool_name} failed:`, logEntry.error);
        }
    }
    
    getExecutionLog() {
        return this.executionLog;
    }
}
```

## Tool Registration with Agent

```python
# backend/agent.py

from pydantic_ai import Agent
from backend.mcp_server import mcp

def create_agent(session_id: str) -> Agent:
    """
    Create a PydanticAI agent with all MCP tools registered.
    
    Args:
        session_id: Session ID for this agent instance
    
    Returns:
        Configured Agent instance
    """
    agent = Agent(
        model="openai:gpt-4-turbo-preview",
        system_prompt=get_system_prompt(),
        tools=mcp.get_tools()  # Register all MCP tools
    )
    
    return agent
```

## Summary

✅ **37 total tools** covering all FL_JS functionality
✅ **Consistent pattern** for all tools (backend definition + frontend handler)
✅ **Type-safe** with Pydantic models
✅ **Async callback** mechanism via WebSocket
✅ **Error handling** and logging
✅ **Timeout support** for all tools
✅ **Execution tracking** for debugging
