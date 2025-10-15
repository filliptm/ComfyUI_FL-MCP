"""MCP Server implementation using FastMCP.

This module defines all tools available to the AI agent for controlling
ComfyUI workflows through WebSocket-based tool execution.
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import websockets
from fastmcp import FastMCP, Context
from pydantic import Field

from backend.models import WorkflowQuery

logger = logging.getLogger(__name__)


# ============================================================================
# WebSocket Client for MCP Subprocess
# ============================================================================

class MCPWebSocketClient:
    """WebSocket client for MCP subprocess to communicate with backend."""
    
    def __init__(self, session_id: str, ws_url: str):
        self.session_id = session_id
        self.ws_url = ws_url
        self.ws = None
        self.pending_requests = {}  # request_id -> asyncio.Future
        self.connected = False
        self._receive_task = None
        
    async def connect(self):
        """Connect to backend WebSocket server."""
        logger.info(f"[MCP-WS] Connecting to {self.ws_url} with session {self.session_id}")
        
        try:
            self.ws = await websockets.connect(self.ws_url)
            
            # Send handshake
            await self.ws.send(json.dumps({
                'type': 'handshake',
                'session_id': self.session_id,
                'client_version': '1.0.0-mcp',
            }))
            
            # Wait for handshake ack
            response = await self.ws.recv()
            data = json.loads(response)
            
            if data.get('type') == 'handshake_ack':
                self.connected = True
                logger.info(f"[MCP-WS] Connected and handshake complete")
                
                # Start receive loop
                self._receive_task = asyncio.create_task(self._receive_loop())
            else:
                raise RuntimeError(f"Unexpected handshake response: {data}")
                
        except Exception as e:
            logger.error(f"[MCP-WS] Connection failed: {e}")
            raise
    
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
    
    async def _handle_message(self, data: dict):
        """Handle incoming message from backend."""
        msg_type = data.get('type')
        
        if msg_type == 'tool_result':
            # Tool execution result from frontend
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
    
    async def execute_tool(self, tool_name: str, parameters: dict, timeout_ms: int = 30000) -> dict:
        """Execute a tool via WebSocket callback."""
        if not self.connected:
            raise RuntimeError("WebSocket not connected")
        
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        
        # Create future for this request
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[request_id] = future
        
        logger.info(f"[MCP-WS] Executing tool: {tool_name} (request_id: {request_id})")
        
        try:
            # Send tool request
            await self.ws.send(json.dumps({
                'type': 'tool_request',
                'session_id': self.session_id,
                'request_id': request_id,
                'tool_name': tool_name,
                'parameters': parameters,
                'timeout_ms': timeout_ms,
            }))
            
            # Wait for result with timeout
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
    
    async def disconnect(self):
        """Disconnect from WebSocket server."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        self.connected = False
        logger.info("[MCP-WS] Disconnected")


# Global WebSocket client for this MCP subprocess
_ws_client: Optional[MCPWebSocketClient] = None


@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[Any]:
    """Manage MCP server lifespan and WebSocket connection."""
    
    # Check if running in subprocess mode
    if os.getenv('FL_MCP_MODE') == 'subprocess':
        session_id = os.getenv('FL_SESSION_ID')
        ws_url = os.getenv('FL_WS_URL')
        
        if not session_id or not ws_url:
            logger.error("Missing FL_SESSION_ID or FL_WS_URL environment variables")
            raise RuntimeError("MCP subprocess not properly configured")
        
        logger.info(f"[MCP] Starting in subprocess mode for session: {session_id}")
        
        # Create and connect WebSocket client
        _ws_client = MCPWebSocketClient(session_id, ws_url)
        await _ws_client.connect()
        
        logger.info("[MCP] WebSocket client connected")
        
    else:
        logger.info("[MCP] Running in standalone mode (no WebSocket)")
    
    yield {"client": _ws_client}
    
    # # Cleanup
    # if _ws_client:
    #     await _ws_client.disconnect()
    #     logger.info("[MCP] WebSocket client disconnected")


# Initialize FastMCP server with lifespan
mcp = FastMCP("FL_Agent Workflow Tools", lifespan=mcp_lifespan)


# Import callback router functions (legacy, kept for backwards compatibility)
_callback_router = None


def set_callback_router(router):
    """Set the callback router instance (legacy).
    
    This is kept for backwards compatibility but is no longer used
    when running in subprocess mode.
    
    Args:
        router: CallbackRouter instance
    """
    global _callback_router
    _callback_router = router
    logger.info("Callback router set for MCP server (legacy mode)")


async def _execute_tool(ctx: Context, tool_name: str, parameters: Dict[str, Any], timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """Execute a tool via WebSocket callback.
    
    Args:
        tool_name: Name of the tool to execute
        parameters: Tool parameters
        timeout_ms: Optional timeout in milliseconds
        
    Returns:
        Tool execution result
        
    Raises:
        RuntimeError: If WebSocket client not initialized
    """
    _ws_client = ctx.request_context.lifespan_context.client
    if _ws_client is None:
        raise RuntimeError("WebSocket client not initialized. MCP server not running in subprocess mode.")
    
    return await _ws_client.execute_tool(
        tool_name=tool_name,
        parameters=parameters,
        timeout_ms=timeout_ms or 30000
    )


# ============================================================================
# QUERY & ANALYSIS TOOLS
# ============================================================================

@mcp.tool()
async def query_workflow(query: WorkflowQuery) -> Dict[str, Any]:
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
    return await _execute_tool("query_workflow", query.model_dump())


@mcp.tool()
async def workflow_overview() -> Dict[str, Any]:
    """Get a comprehensive overview of the current workflow.
    
    Returns workflow statistics, node type counts, disconnected nodes,
    and a Mermaid diagram of the entire workflow.
    
    Returns:
        Dictionary with:
        - total_nodes: Total number of nodes
        - node_types: Count of each node type
        - disconnected_nodes: List of nodes with no connections
        - diagram: Mermaid diagram of workflow
    
    Example:
        >>> result = await workflow_overview()
        >>> print(f"Total nodes: {result['total_nodes']}")
        >>> print(f"Disconnected: {len(result['disconnected_nodes'])}")
        >>> print(result['diagram'])
    """
    return await _execute_tool("workflow_overview", {})


@mcp.tool()
async def workflow_diagram(
    node_ids: Optional[List[int]] = Field(None, description="Optional list of node IDs to include (null for all nodes)")
) -> Dict[str, Any]:
    """Generate a Mermaid diagram of the workflow or subset of nodes.
    
    Creates a visual representation of the workflow graph showing nodes
    and their connections.
    
    Args:
        node_ids: Optional list of node IDs to include. If None, includes all nodes.
    
    Returns:
        Dictionary with 'diagram' (Mermaid string) key
    
    Example:
        >>> # Diagram of entire workflow
        >>> result = await workflow_diagram()
        >>> print(result['diagram'])
        >>> 
        >>> # Diagram of specific nodes
        >>> result = await workflow_diagram(node_ids=[5, 7, 9, 12])
    """
    return await _execute_tool("workflow_diagram", {
        "node_ids": node_ids
    })


# ============================================================================
# NODE MANAGEMENT TOOLS
# ============================================================================

@mcp.tool()
async def find_node(
    node_id: Optional[int] = Field(None, description="Node ID to find"),
    node_type: Optional[str] = Field(None, description="Node type/class to find (e.g., 'KSampler')"),
    title: Optional[str] = Field(None, description="Node title to find"),
    find_last: bool = Field(False, description="If true, search from end of array")
) -> Dict[str, Any]:
    """Find a node by ID, type, or title.
    
    Returns the first matching node, or the last if find_last=True.
    At least one of node_id, node_type, or title must be provided.
    
    Returns:
        Dictionary with 'found' (bool) and 'node' (object or null) keys.
        If found, node contains: id, type, title, position, size, mode.
    
    Example:
        >>> result = await find_node(node_type="KSampler")
        >>> if result["found"]:
        ...     print(f"Found node: {result['node']['id']}")
    """
    return await _execute_tool("find_node", {
        "node_id": node_id,
        "node_type": node_type,
        "title": title,
        "find_last": find_last
    })


@mcp.tool()
async def create_node(
    node_type: str = Field(..., description="ComfyUI node class name (e.g., 'CheckpointLoaderSimple')"),
    parameters: Optional[Dict[str, Any]] = Field(None, description="Node parameter values as key-value pairs"),
    position: Optional[Dict[str, float]] = Field(None, description="Node position {x, y}")
) -> Dict[str, Any]:
    """Create a new node in the workflow.
    
    Creates a node of the specified type, optionally setting initial parameter
    values and position.
    
    Args:
        node_type: ComfyUI node class name (e.g., 'KSampler', 'CheckpointLoaderSimple')
        parameters: Optional dict of parameter values (e.g., {"seed": 42, "steps": 20})
        position: Optional position dict with x and y coordinates
    
    Returns:
        Dictionary with: id, type, title, position, size
    
    Example:
        >>> result = await create_node(
        ...     node_type="CheckpointLoaderSimple",
        ...     parameters={"ckpt_name": "sd_xl_base_1.0.safetensors"},
        ...     position={"x": 100, "y": 100}
        ... )
        >>> node_id = result["id"]
    """
    return await _execute_tool("create_node", {
        "node_type": node_type,
        "parameters": parameters or {},
        "position": position
    })


@mcp.tool()
async def remove_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")
) -> Dict[str, Any]:
    """Remove one or more nodes from the workflow.
    
    Args:
        node_ids: List of node IDs (integers) or titles (strings) to remove
    
    Returns:
        Dictionary with 'removed_count' (int) key
    
    Example:
        >>> result = await remove_nodes(node_ids=[5, 7, 9])
        >>> print(f"Removed {result['removed_count']} nodes")
    """
    return await _execute_tool("remove_nodes", {
        "node_ids": node_ids
    })


@mcp.tool()
async def bypass_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to bypass")
) -> Dict[str, Any]:
    """Bypass (mute) one or more nodes.
    
    Bypassed nodes are skipped during workflow execution but remain in the graph.
    
    Args:
        node_ids: List of node IDs or titles to bypass
    
    Returns:
        Dictionary with 'bypassed_count' (int) key
    
    Example:
        >>> result = await bypass_nodes(node_ids=[5, 7])
    """
    return await _execute_tool("bypass_nodes", {
        "node_ids": node_ids
    })


@mcp.tool()
async def unbypass_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unbypass")
) -> Dict[str, Any]:
    """Unbypass (unmute) one or more nodes.
    
    Restores bypassed nodes to normal execution mode.
    
    Args:
        node_ids: List of node IDs or titles to unbypass
    
    Returns:
        Dictionary with 'unbypassed_count' (int) key
    """
    return await _execute_tool("unbypass_nodes", {
        "node_ids": node_ids
    })


@mcp.tool()
async def pin_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to pin")
) -> Dict[str, Any]:
    """Pin one or more nodes to prevent movement.
    
    Pinned nodes cannot be moved in the UI.
    
    Args:
        node_ids: List of node IDs or titles to pin
    
    Returns:
        Dictionary with 'pinned_count' (int) key
    """
    return await _execute_tool("pin_nodes", {
        "node_ids": node_ids
    })


@mcp.tool()
async def unpin_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unpin")
) -> Dict[str, Any]:
    """Unpin one or more nodes to allow movement.
    
    Args:
        node_ids: List of node IDs or titles to unpin
    
    Returns:
        Dictionary with 'unpinned_count' (int) key
    """
    return await _execute_tool("unpin_nodes", {
        "node_ids": node_ids
    })


@mcp.tool()
async def select_nodes(
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to select")
) -> Dict[str, Any]:
    """Select one or more nodes in the UI.
    
    Deselects all other nodes and selects the specified nodes.
    
    Args:
        node_ids: List of node IDs or titles to select
    
    Returns:
        Dictionary with 'selected_count' (int) key
    """
    return await _execute_tool("select_nodes", {
        "node_ids": node_ids
    })


# ============================================================================
# NODE MANIPULATION TOOLS
# ============================================================================

@mcp.tool()
async def get_node_values(
    node_id: Union[int, str] = Field(..., description="Node ID or title")
) -> Dict[str, Any]:
    """Get all parameter values from a node.
    
    Returns all widget values (parameters) from the specified node.
    
    Args:
        node_id: Node ID (int) or title (str)
    
    Returns:
        Dictionary with 'node_id' and 'values' (dict of parameter values)
    
    Example:
        >>> result = await get_node_values(node_id=5)
        >>> values = result["values"]
        >>> print(f"Seed: {values['seed']}, Steps: {values['steps']}")
    """
    return await _execute_tool("get_node_values", {
        "node_id": node_id
    })


@mcp.tool()
async def set_node_values(
    node_id: Union[int, str] = Field(..., description="Node ID or title"),
    values: Dict[str, Any] = Field(..., description="Parameter values to set as key-value pairs")
) -> Dict[str, Any]:
    """Set parameter values on a node.
    
    Updates widget values (parameters) on the specified node.
    
    Args:
        node_id: Node ID (int) or title (str)
        values: Dictionary of parameter values to set (e.g., {"seed": 42, "steps": 20})
    
    Returns:
        Dictionary with 'node_id' and 'values' (updated parameter values)
    
    Example:
        >>> result = await set_node_values(
        ...     node_id=5,
        ...     values={"seed": 42, "steps": 30, "cfg": 7.5}
        ... )
    """
    return await _execute_tool("set_node_values", {
        "node_id": node_id,
        "values": values
    })


@mcp.tool()
async def connect_nodes(
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title"),
    source_slot: Union[str, int] = Field(..., description="Source output slot name or index"),
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title"),
    target_slot: Optional[Union[str, int]] = Field(None, description="Target input slot name or index (defaults to source_slot)")
) -> Dict[str, Any]:
    """Connect two nodes together.
    
    Creates a connection from a source node's output to a target node's input.
    Slots can be specified by name (string) or index (integer).
    
    Args:
        source_node_id: Source node ID or title
        source_slot: Source output slot name (e.g., "IMAGE", "LATENT") or index (0, 1, 2...)
        target_node_id: Target node ID or title
        target_slot: Target input slot name or index (if None, uses source_slot name)
    
    Returns:
        Dictionary with 'connected' (bool) key
    
    Example:
        >>> # Connect by slot name
        >>> result = await connect_nodes(
        ...     source_node_id=5,
        ...     source_slot="IMAGE",
        ...     target_node_id=7,
        ...     target_slot="image"
        ... )
        >>> 
        >>> # Connect by index
        >>> result = await connect_nodes(
        ...     source_node_id=5,
        ...     source_slot=0,
        ...     target_node_id=7,
        ...     target_slot=0
        ... )
    """
    return await _execute_tool("connect_nodes", {
        "source_node_id": source_node_id,
        "source_slot": source_slot,
        "target_node_id": target_node_id,
        "target_slot": target_slot
    })


# ============================================================================
# LAYOUT MANAGEMENT TOOLS
# ============================================================================

@mcp.tool()
async def get_node_rect(
    node_id: Union[int, str] = Field(..., description="Node ID or title")
) -> Dict[str, Any]:
    """Get node position and size.
    
    Args:
        node_id: Node ID or title
    
    Returns:
        Dictionary with 'node_id' and 'rect' {x, y, width, height}
    
    Example:
        >>> result = await get_node_rect(node_id=5)
        >>> rect = result["rect"]
        >>> print(f"Position: ({rect['x']}, {rect['y']}), Size: {rect['width']}x{rect['height']}")
    """
    return await _execute_tool("get_node_rect", {
        "node_id": node_id
    })


@mcp.tool()
async def set_node_rect(
    node_id: Union[int, str] = Field(..., description="Node ID or title"),
    x: Optional[float] = Field(None, description="X position (null to keep current)"),
    y: Optional[float] = Field(None, description="Y position (null to keep current)"),
    width: Optional[float] = Field(None, description="Width (null to keep current)"),
    height: Optional[float] = Field(None, description="Height (null to keep current)")
) -> Dict[str, Any]:
    """Set node position and/or size.
    
    Updates the node's position and/or size. Pass null for any dimension to keep current value.
    
    Args:
        node_id: Node ID or title
        x: X position (or None to keep current)
        y: Y position (or None to keep current)
        width: Width (or None to keep current)
        height: Height (or None to keep current)
    
    Returns:
        Dictionary with 'node_id' and 'rect' (updated rectangle)
    
    Example:
        >>> # Move to new position
        >>> result = await set_node_rect(node_id=5, x=200, y=300)
        >>> 
        >>> # Resize
        >>> result = await set_node_rect(node_id=5, width=400, height=200)
    """
    return await _execute_tool("set_node_rect", {
        "node_id": node_id,
        "x": x,
        "y": y,
        "width": width,
        "height": height
    })


@mcp.tool()
async def position_node_left(
    target_node_id: Union[int, str] = Field(..., description="Node to position"),
    anchor_node_id: Union[int, str] = Field(..., description="Reference node"),
    margin: int = Field(32, description="Margin between nodes in pixels")
) -> Dict[str, Any]:
    """Position a node to the left of another node.
    
    Places the target node to the left of the anchor node with the specified margin.
    
    Args:
        target_node_id: Node to move
        anchor_node_id: Reference node
        margin: Space between nodes (default: 32)
    
    Returns:
        Dictionary with 'positioned' (bool) key
    
    Example:
        >>> await position_node_left(target_node_id=7, anchor_node_id=5, margin=50)
    """
    return await _execute_tool("position_node_left", {
        "target_node_id": target_node_id,
        "anchor_node_id": anchor_node_id,
        "margin": margin
    })


@mcp.tool()
async def position_node_right(
    target_node_id: Union[int, str] = Field(..., description="Node to position"),
    anchor_node_id: Union[int, str] = Field(..., description="Reference node"),
    margin: int = Field(32, description="Margin between nodes in pixels")
) -> Dict[str, Any]:
    """Position a node to the right of another node.
    
    Places the target node to the right of the anchor node with the specified margin.
    
    Args:
        target_node_id: Node to move
        anchor_node_id: Reference node
        margin: Space between nodes (default: 32)
    
    Returns:
        Dictionary with 'positioned' (bool) key
    """
    return await _execute_tool("position_node_right", {
        "target_node_id": target_node_id,
        "anchor_node_id": anchor_node_id,
        "margin": margin
    })


@mcp.tool()
async def position_node_top(
    target_node_id: Union[int, str] = Field(..., description="Node to position"),
    anchor_node_id: Union[int, str] = Field(..., description="Reference node"),
    margin: int = Field(64, description="Margin between nodes in pixels")
) -> Dict[str, Any]:
    """Position a node above another node.
    
    Places the target node above the anchor node with the specified margin.
    
    Args:
        target_node_id: Node to move
        anchor_node_id: Reference node
        margin: Space between nodes (default: 64)
    
    Returns:
        Dictionary with 'positioned' (bool) key
    """
    return await _execute_tool("position_node_top", {
        "target_node_id": target_node_id,
        "anchor_node_id": anchor_node_id,
        "margin": margin
    })


@mcp.tool()
async def position_node_bottom(
    target_node_id: Union[int, str] = Field(..., description="Node to position"),
    anchor_node_id: Union[int, str] = Field(..., description="Reference node"),
    margin: int = Field(64, description="Margin between nodes in pixels")
) -> Dict[str, Any]:
    """Position a node below another node.
    
    Places the target node below the anchor node with the specified margin.
    
    Args:
        target_node_id: Node to move
        anchor_node_id: Reference node
        margin: Space between nodes (default: 64)
    
    Returns:
        Dictionary with 'positioned' (bool) key
    """
    return await _execute_tool("position_node_bottom", {
        "target_node_id": target_node_id,
        "anchor_node_id": anchor_node_id,
        "margin": margin
    })


@mcp.tool()
async def move_node_right(
    node_id: Union[int, str] = Field(..., description="Node to move"),
    margin: int = Field(32, description="Margin to maintain when avoiding collisions")
) -> Dict[str, Any]:
    """Move a node to the right, avoiding collisions.
    
    Moves the node to the right until it no longer collides with other nodes.
    
    Args:
        node_id: Node to move
        margin: Minimum space to maintain between nodes (default: 32)
    
    Returns:
        Dictionary with 'moved' (bool) key
    
    Example:
        >>> await move_node_right(node_id=5, margin=50)
    """
    return await _execute_tool("move_node_right", {
        "node_id": node_id,
        "margin": margin
    })


@mcp.tool()
async def move_node_bottom(
    node_id: Union[int, str] = Field(..., description="Node to move"),
    margin: int = Field(64, description="Margin to maintain when avoiding collisions")
) -> Dict[str, Any]:
    """Move a node downward, avoiding collisions.
    
    Moves the node down until it no longer collides with other nodes.
    
    Args:
        node_id: Node to move
        margin: Minimum space to maintain between nodes (default: 64)
    
    Returns:
        Dictionary with 'moved' (bool) key
    """
    return await _execute_tool("move_node_bottom", {
        "node_id": node_id,
        "margin": margin
    })


# ============================================================================
# WORKFLOW CONTROL TOOLS
# ============================================================================

@mcp.tool()
async def queue_workflow(
    batch_count: Optional[int] = Field(None, description="Number of times to execute (default: current batch count)")
) -> Dict[str, Any]:
    """Queue the workflow for execution.
    
    Queues the current workflow to run on the ComfyUI backend.
    
    Args:
        batch_count: Number of times to execute (or None to use current setting)
    
    Returns:
        Dictionary with 'queued' (bool) and 'batchCount' (int) keys
    
    Example:
        >>> result = await queue_workflow(batch_count=5)
        >>> print(f"Queued workflow with batch count: {result['batchCount']}")
    """
    return await _execute_tool("queue_workflow", {
        "batch_count": batch_count
    })


@mcp.tool()
async def cancel_workflow() -> Dict[str, Any]:
    """Cancel the currently executing workflow.
    
    Interrupts the current workflow execution.
    
    Returns:
        Dictionary with 'cancelled' (bool) key
    
    Example:
        >>> result = await cancel_workflow()
    """
    return await _execute_tool("cancel_workflow", {})


@mcp.tool()
async def enable_auto_queue() -> Dict[str, Any]:
    """Enable auto-queue mode.
    
    When enabled, the workflow will automatically execute when changes are made.
    
    Returns:
        Dictionary with 'autoQueue' (bool) and 'mode' (str) keys
    
    Example:
        >>> result = await enable_auto_queue()
    """
    return await _execute_tool("enable_auto_queue", {})


@mcp.tool()
async def disable_auto_queue() -> Dict[str, Any]:
    """Disable auto-queue mode.
    
    Prevents automatic workflow execution on changes.
    
    Returns:
        Dictionary with 'autoQueue' (bool) and 'mode' (str) keys
    """
    return await _execute_tool("disable_auto_queue", {})


@mcp.tool()
async def set_batch_count(
    count: int = Field(..., description="Batch count (number of times to execute workflow)")
) -> Dict[str, Any]:
    """Set the workflow batch count.
    
    Sets how many times the workflow will execute when queued.
    
    Args:
        count: Batch count (must be >= 1)
    
    Returns:
        Dictionary with 'batchCount' (int) key
    
    Example:
        >>> result = await set_batch_count(count=10)
    """
    return await _execute_tool("set_batch_count", {
        "count": count
    })


@mcp.tool()
async def get_queue_status() -> Dict[str, Any]:
    """Get current queue status and settings.
    
    Returns information about the queue mode and batch count.
    
    Returns:
        Dictionary with 'mode' (str), 'autoQueue' (bool), and 'batchCount' (int) keys
    
    Example:
        >>> result = await get_queue_status()
        >>> print(f"Auto-queue: {result['autoQueue']}, Batch: {result['batchCount']}")
    """
    return await _execute_tool("get_queue_status", {})


# ============================================================================
# SYSTEM CONTROL TOOLS
# ============================================================================

@mcp.tool()
async def disable_sleep() -> Dict[str, Any]:
    """Disable system sleep/suspend.
    
    Prevents the system from going to sleep during long workflow executions.
    Requires the event-handler custom node to be installed.
    
    Returns:
        Dictionary with 'sleepDisabled' (bool) key
    """
    return await _execute_tool("disable_sleep", {})


@mcp.tool()
async def enable_sleep() -> Dict[str, Any]:
    """Enable system sleep/suspend.
    
    Allows the system to sleep normally.
    Requires the event-handler custom node to be installed.
    
    Returns:
        Dictionary with 'sleepEnabled' (bool) key
    """
    return await _execute_tool("enable_sleep", {})


@mcp.tool()
async def disable_screensaver() -> Dict[str, Any]:
    """Disable screensaver.
    
    Prevents the screensaver from activating during workflow execution.
    Requires the event-handler custom node to be installed.
    
    Returns:
        Dictionary with 'screensaverDisabled' (bool) key
    """
    return await _execute_tool("disable_screensaver", {})


@mcp.tool()
async def enable_screensaver() -> Dict[str, Any]:
    """Enable screensaver.
    
    Allows the screensaver to activate normally.
    Requires the event-handler custom node to be installed.
    
    Returns:
        Dictionary with 'screensaverEnabled' (bool) key
    """
    return await _execute_tool("enable_screensaver", {})


@mcp.tool()
async def send_images(
    url: str = Field(..., description="Target URL to send images to"),
    field: str = Field(..., description="Form field name for images"),
    file_paths: List[Union[str, Dict[str, Any]]] = Field(..., description="List of file paths or PreviewImage node objects")
) -> Dict[str, Any]:
    """Send images to an external URL.
    
    Sends generated images to an external service via HTTP POST.
    Requires the event-handler custom node to be installed.
    
    Args:
        url: Target URL
        field: Form field name for the images
        file_paths: List of file paths (strings) or PreviewImage node objects
    
    Returns:
        Dictionary with 'sent' (bool) and 'count' (int) keys
    
    Example:
        >>> result = await send_images(
        ...     url="http://example.com/upload",
        ...     field="images",
        ...     file_paths=["/path/to/image1.png", "/path/to/image2.png"]
        ... )
    """
    return await _execute_tool("send_images", {
        "url": url,
        "field": field,
        "file_paths": file_paths
    })


# ============================================================================
# UTILITY TOOLS
# ============================================================================

@mcp.tool()
async def generate_seed() -> Dict[str, Any]:
    """Generate a random seed value.
    
    Generates a random seed suitable for use with ComfyUI nodes.
    
    Returns:
        Dictionary with 'seed' (int) key
    
    Example:
        >>> result = await generate_seed()
        >>> seed = result["seed"]
        >>> await set_node_values(node_id=5, values={"seed": seed})
    """
    return await _execute_tool("generate_seed", {})


@mcp.tool()
async def generate_float(
    min: float = Field(..., description="Minimum value"),
    max: float = Field(..., description="Maximum value")
) -> Dict[str, Any]:
    """Generate a random float value.
    
    Generates a random floating-point number in the specified range.
    
    Args:
        min: Minimum value (inclusive)
        max: Maximum value (exclusive)
    
    Returns:
        Dictionary with 'value' (float) key
    
    Example:
        >>> result = await generate_float(min=0.5, max=1.5)
        >>> value = result["value"]
    """
    return await _execute_tool("generate_float", {
        "min": min,
        "max": max
    })


@mcp.tool()
async def generate_int(
    min: int = Field(..., description="Minimum value"),
    max: int = Field(..., description="Maximum value")
) -> Dict[str, Any]:
    """Generate a random integer value.
    
    Generates a random integer in the specified range.
    
    Args:
        min: Minimum value (inclusive)
        max: Maximum value (exclusive)
    
    Returns:
        Dictionary with 'value' (int) key
    
    Example:
        >>> result = await generate_int(min=10, max=50)
        >>> value = result["value"]
    """
    return await _execute_tool("generate_int", {
        "min": min,
        "max": max
    })


@mcp.tool()
async def random_choice(
    items: List[Any] = Field(..., description="List of items to choose from")
) -> Dict[str, Any]:
    """Pick a random item from a list.
    
    Selects one random item from the provided list.
    
    Args:
        items: List of items (must be non-empty)
    
    Returns:
        Dictionary with 'choice' (any type) key
    
    Example:
        >>> result = await random_choice(items=["euler", "euler_a", "dpmpp_2m"])
        >>> sampler = result["choice"]
        >>> await set_node_values(node_id=5, values={"sampler_name": sampler})
    """
    return await _execute_tool("random_choice", {
        "items": items
    })


def main():
    """Run the MCP server as a standalone application."""
    mcp.run()
    
if __name__ == "__main__":
    main()
