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
from typing import Any, AsyncIterator, Dict, List, Optional, Union, Literal

import websockets
from fastmcp import FastMCP, Context
from pydantic import BaseModel, Field

from models import WorkflowQuery
from comfy_models import (
    ComfyListFoldersRequest, ComfyListFoldersResponse,
    ComfyReadFileRequest, ComfyReadFileResponse,
    ComfySearchFilesRequest, ComfySearchFilesResponse
)
from comfy_tools import get_comfy_tools, ComfyUIError, ComfyUINotFoundError
from manager import manager

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


@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[Any]:
    """Manage MCP server lifespan and WebSocket connection."""
    
    _ws_client = None
    
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


# Legacy callback router support (kept for backwards compatibility)
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
        ctx: FastMCP Context
        tool_name: Name of the tool to execute
        parameters: Tool parameters
        timeout_ms: Optional timeout in milliseconds
        
    Returns:
        Tool execution result
        
    Raises:
        RuntimeError: If WebSocket client not initialized
    """
    _ws_client = ctx.request_context.lifespan_context['client']
    if _ws_client is None:
        raise RuntimeError("WebSocket client not initialized. MCP server not running in subprocess mode.")
    
    return await _ws_client.execute_tool(
        tool_name=tool_name,
        parameters=parameters,
        timeout_ms=timeout_ms or 30000
    )


# ============================================================================
# REQUEST MODELS
# ============================================================================

# Query & Analysis
class WorkflowOverviewRequest(BaseModel):
    """Request for workflow overview."""
    pass

class WorkflowDiagramRequest(BaseModel):
    """Request to generate workflow diagram."""
    node_ids: Optional[List[int]] = Field(None, description="Optional list of node IDs to include (null for all nodes)")

# Node Management
class FindNodeRequest(BaseModel):
    """Request to find a node."""
    node_id: Optional[int] = Field(None, description="Node ID to find")
    node_type: Optional[str] = Field(None, description="Node type/class to find (e.g., 'KSampler')")
    title: Optional[str] = Field(None, description="Node title to find")
    find_last: bool = Field(False, description="If true, search from end of array")

class CreateNodeRequest(BaseModel):
    """Request to create a new node."""
    node_type: str = Field(..., description="ComfyUI node class name (e.g., 'CheckpointLoaderSimple')")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Node parameter values as key-value pairs")
    position: Optional[Dict[str, float]] = Field(None, description="Node position {x, y}")
    
class CreateNodesRequest(BaseModel):
    nodes: List[CreateNodeRequest] = Field(..., description="List of nodes to create each their own parameters")

class RemoveNodesRequest(BaseModel):
    """Request to remove nodes from workflow."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")

class BypassNodesRequest(BaseModel):
    """Request to bypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to bypass")

class UnbypassNodesRequest(BaseModel):
    """Request to unbypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unbypass")

class PinNodesRequest(BaseModel):
    """Request to pin nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to pin")

class UnpinNodesRequest(BaseModel):
    """Request to unpin nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unpin")

class SelectNodesRequest(BaseModel):
    """Request to select nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to select")

class GetSelectedNodesRequest(BaseModel):
    """Request to get currently selected nodes."""
    pass

# Node Manipulation
class GetNodeValuesRequest(BaseModel):
    """Request to get node parameter values."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class SetNodeValuesRequest(BaseModel):
    """Request to set node parameter values."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    values: Dict[str, Any] = Field(..., description="Parameter values to set as key-value pairs")

class ConnectNodesRequest(BaseModel):
    """Request to connect two nodes."""
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title, must be a number: 1-9999")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title, must be a number: 1-9999")
    source_slot: Optional[Union[str, int]] = Field(None, description="Source output slot name or index (auto-match if not provided)")
    target_slot: Optional[Union[str, int]] = Field(None, description="Target input slot name or index (auto-match if not provided)")
    auto_match: bool = Field(True, description="Enable auto-matching by type if slot names not found")
    match_strategy: Literal["first", "type", "name"] = Field(
        "type",
        description="Auto-match strategy: 'first'=use first available, 'type'=match by data type, 'name'=match by similar names"
    )

class GetNodeSlotsRequest(BaseModel):
    """Request to get node slot information."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class ConnectionSpec(BaseModel):
    """Single connection specification for batch operations."""
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title")
    source_slot: Optional[Union[str, int]] = Field(None, description="Source output slot name or index (optional for auto-match)")
    target_slot: Optional[Union[str, int]] = Field(None, description="Target input slot name or index (optional for auto-match)")

class ConnectNodesBatchRequest(BaseModel):
    """Request to connect multiple node pairs in batch."""
    connections: List[ConnectionSpec] = Field(..., description="List of connection specifications")
    auto_match: bool = Field(True, description="Enable auto-matching by type if slot names not found")
    stop_on_error: bool = Field(False, description="Stop on first error (false = continue and report all)")

class AutoConnectWorkflowRequest(BaseModel):
    """Request to auto-connect nodes in sequence."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs to connect in order")
    strategy: Literal["sequential", "type_match"] = Field(
        "sequential",
        description="Connection strategy: 'sequential' connects in order, 'type_match' finds all compatible pairs"
    )

# Layout Management
class GetNodeRectRequest(BaseModel):
    """Request to get node position and size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class SetNodeRectRequest(BaseModel):
    """Request to set node position and/or size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    x: Optional[float] = Field(None, description="X position (null to keep current)")
    y: Optional[float] = Field(None, description="Y position (null to keep current)")
    width: Optional[float] = Field(None, description="Width (null to keep current)")
    height: Optional[float] = Field(None, description="Height (null to keep current)")
    
class BatchLayoutRequest(BaseModel):
    node_rects: List[SetNodeRectRequest] = Field(..., description="A List of nodes with their new rectangle settings for full or partial quick layout changes")

class PositionNodeLeftRequest(BaseModel):
    """Request to position node to the left of another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(32, description="Margin between nodes in pixels")

class PositionNodeRightRequest(BaseModel):
    """Request to position node to the right of another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(32, description="Margin between nodes in pixels")

class PositionNodeTopRequest(BaseModel):
    """Request to position node above another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(64, description="Margin between nodes in pixels")

class PositionNodeBottomRequest(BaseModel):
    """Request to position node below another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(64, description="Margin between nodes in pixels")

class MoveNodeRightRequest(BaseModel):
    """Request to move node to the right, avoiding collisions."""
    node_id: Union[int, str] = Field(..., description="Node to move")
    margin: int = Field(32, description="Margin to maintain when avoiding collisions")

class MoveNodeBottomRequest(BaseModel):
    """Request to move node downward, avoiding collisions."""
    node_id: Union[int, str] = Field(..., description="Node to move")
    margin: int = Field(64, description="Margin to maintain when avoiding collisions")

# Workflow Control
class QueueWorkflowRequest(BaseModel):
    """Request to queue workflow for execution."""
    batch_count: Optional[int] = Field(None, description="Number of times to execute (default: current batch count)")

class CancelWorkflowRequest(BaseModel):
    """Request to cancel workflow execution."""
    pass

class EnableAutoQueueRequest(BaseModel):
    """Request to enable auto-queue mode."""
    pass

class DisableAutoQueueRequest(BaseModel):
    """Request to disable auto-queue mode."""
    pass

class SetBatchCountRequest(BaseModel):
    """Request to set workflow batch count."""
    count: int = Field(..., description="Batch count (number of times to execute workflow)")

class GetQueueStatusRequest(BaseModel):
    """Request to get queue status."""
    pass

# System Control
class DisableSleepRequest(BaseModel):
    """Request to disable system sleep."""
    pass

class EnableSleepRequest(BaseModel):
    """Request to enable system sleep."""
    pass

class DisableScreensaverRequest(BaseModel):
    """Request to disable screensaver."""
    pass

class EnableScreensaverRequest(BaseModel):
    """Request to enable screensaver."""
    pass

class SendImagesRequest(BaseModel):
    """Request to send images to external URL."""
    url: str = Field(..., description="Target URL to send images to")
    field: str = Field(..., description="Form field name for images")
    file_paths: List[Union[str, Dict[str, Any]]] = Field(..., description="List of file paths or PreviewImage node objects")

# Utility
class GenerateSeedRequest(BaseModel):
    """Request to generate random seed."""
    pass

class GenerateFloatRequest(BaseModel):
    """Request to generate random float."""
    min: float = Field(..., description="Minimum value")
    max: float = Field(..., description="Maximum value")

class GenerateIntRequest(BaseModel):
    """Request to generate random integer."""
    min: int = Field(..., description="Minimum value")
    max: int = Field(..., description="Maximum value")

class RandomChoiceRequest(BaseModel):
    """Request to pick random item from list."""
    items: List[Any] = Field(..., description="List of items to choose from")

# Error Feedback
class GetRecentErrorsRequest(BaseModel):
    """Request to get recent execution errors."""
    limit: int = Field(10, description="Number of recent errors to retrieve (default: 10, max: 100)")

class GetErrorsForRunRequest(BaseModel):
    """Request to get errors for a specific workflow run."""
    prompt_id: str = Field(..., description="The prompt/run ID to get errors for")

class GetQueueStatusDetailsRequest(BaseModel):
    """Request to get detailed queue status and active executions."""
    pass

class GetExecutionDetailsRequest(BaseModel):
    """Request to get execution details for a specific run."""
    prompt_id: str = Field(..., description="The prompt/run ID to get details for")

class ClearErrorBufferRequest(BaseModel):
    """Request to clear the error buffer."""
    pass

class WaitRequest(BaseModel):
    delay: float = Field(..., description="Brief period of time to wait (keep between 5 and 20 seconds). Great for waiting a bit after the workflow is queued to show some result")

@mcp.tool()
async def wait(request: WaitRequest) -> Dict[str, Any]:
    await asyncio.sleep(float(request.delay))
    return {"waited_for": request.delay}

# ============================================================================
# QUERY & ANALYSIS TOOLS
# ============================================================================

@mcp.tool()
async def query_workflow(request: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation."""
    return await _execute_tool(ctx, "query_workflow", request.model_dump())


@mcp.tool()
async def workflow_overview(request: WorkflowOverviewRequest, ctx: Context) -> Dict[str, Any]:
    """Get a comprehensive overview of the current workflow."""
    return await _execute_tool(ctx, "workflow_overview", {})


@mcp.tool()
async def workflow_diagram(request: WorkflowDiagramRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a Mermaid diagram of the workflow or subset of nodes."""
    return await _execute_tool(ctx, "workflow_diagram", request.model_dump())


# ============================================================================
# NODE MANAGEMENT TOOLS
# ============================================================================

# TODO: Add tools to see what nodes are installed in the comfy sort of python environment (checking custom_nodes folder?) (needs web research)
#       kinda also needs a way to see what's like in each node pack somehow (is there an easy comfy lib way for this?)

@mcp.tool()
async def find_node(request: FindNodeRequest, ctx: Context) -> Dict[str, Any]:
    """Find a node by ID, type, or title."""
    return await _execute_tool(ctx, "find_node", request.model_dump())


@mcp.tool()
async def create_nodes(request: CreateNodesRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Create one or more new node in the workflow."""
    o = []
    for node in request.nodes:
        o.append(await _execute_tool(ctx, "create_node", node.model_dump()))
    return o


@mcp.tool()
async def remove_nodes(request: RemoveNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Remove one or more nodes from the workflow."""
    return await _execute_tool(ctx, "remove_nodes", request.model_dump())


@mcp.tool()
async def bypass_nodes(request: BypassNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Bypass (mute) one or more nodes."""
    return await _execute_tool(ctx, "bypass_nodes", request.model_dump())


@mcp.tool()
async def unbypass_nodes(request: UnbypassNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Unbypass (unmute) one or more nodes."""
    return await _execute_tool(ctx, "unbypass_nodes", request.model_dump())


@mcp.tool()
async def pin_nodes(request: PinNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Pin one or more nodes to prevent movement."""
    return await _execute_tool(ctx, "pin_nodes", request.model_dump())


@mcp.tool()
async def unpin_nodes(request: UnpinNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Unpin one or more nodes to allow movement."""
    return await _execute_tool(ctx, "unpin_nodes", request.model_dump())


@mcp.tool()
async def select_nodes(request: SelectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Select one or more nodes in the UI."""
    return await _execute_tool(ctx, "select_nodes", request.model_dump())


@mcp.tool()
async def get_current_user_focus(request: GetSelectedNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Get currently selected nodes in ComfyUI to understand user's current focus.
    
    This tool provides context-aware assistance by returning detailed information
    about the nodes the user currently has selected in the workflow canvas.
    
    USE CASES:
    - User asks "what does this node do?" - Check selected nodes for context
    - User says "change the seed" - Find seed parameter in selected nodes
    - User requests modifications - Know which nodes they're referring to
    - Debugging assistance - Analyze parameters of nodes user is examining
    
    RETURNS:
    Dictionary with 'nodes' key containing array of selected node objects.
    Each node includes:
    - id: Node ID (integer)
    - title: Node title (string)
    - type: Node type/class (string, e.g., "KSampler")
    - position: {x: float, y: float}
    - size: {width: float, height: float}
    - mode: Node mode (0=normal, 2=muted, 4=bypassed)
    - parameters: Dictionary of parameter name -> value
    - inputs: Array of {name, type, link} objects
    - outputs: Array of {name, type, links} objects
    
    If no nodes are selected, returns empty array: {"nodes": []}    
    """
    return await _execute_tool(ctx, "get_selected_nodes", {})


# ============================================================================
# NODE MANIPULATION TOOLS
# ============================================================================

@mcp.tool()
async def get_node_values(request: GetNodeValuesRequest, ctx: Context) -> Dict[str, Any]:
    """Get all parameter values from a node."""
    return await _execute_tool(ctx, "get_node_values", request.model_dump())


@mcp.tool()
async def set_node_values(request: SetNodeValuesRequest, ctx: Context) -> Dict[str, Any]:
    """Set parameter values on a node."""
    return await _execute_tool(ctx, "set_node_values", request.model_dump())


@mcp.tool()
async def connect_nodes(request: ConnectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Connect two nodes with optional auto-matching.
    
    BASIC USAGE (with slot names):
    Provide exact slot names for reliable connections.
    
    SMART USAGE (auto-match by type):
    Omit slot names to automatically find compatible connections by type.
    
    PARAMETERS:
    - source_node_id: Source node ID or title (required)
    - target_node_id: Target node ID or title (required)
    - source_slot: Output slot name/index (optional, auto-matches if not provided)
    - target_slot: Input slot name/index (optional, auto-matches if not provided)
    - auto_match: Enable auto-matching (default: true)
    - match_strategy: How to auto-match (default: "type")
      - "first": Use first available output/input
      - "type": Match by compatible types
      - "name": Match by similar slot names
    
    RETURNS:
    Dictionary with connection details including source/target nodes, slots, and data type.
    
    ERROR HANDLING:
    If connection fails, error message includes available slots on both nodes
    and suggestion to use get_node_slots() for discovery.
    """
    return await _execute_tool(ctx, "connect_nodes", request.model_dump())


@mcp.tool()
async def get_node_slots(request: GetNodeSlotsRequest, ctx: Context) -> Dict[str, Any]:
    """Get detailed input and output slot information for a node.
    
    This tool enables agents to discover exact slot names, types, and connection status
    before attempting to connect nodes, eliminating guesswork and connection failures.
    
    USE CASES:
    - Pre-connection discovery: Determine available slots before connecting
    - Type matching: Find compatible slots by data type
    - Connection debugging: Understand why connections fail
    - Workflow planning: Verify connection compatibility
    
    RETURNS:
    Dictionary containing:
    - node_id: Node ID (integer)
    - type: Node type/class (string)
    - title: Node title (string)
    - inputs: Array of input slot objects with name, type, index, connection status
    - outputs: Array of output slot objects with name, type, index, connection status
    
    Each slot object includes:
    - name: Exact slot name (case-sensitive string)
    - type: Data type (e.g., "LATENT", "IMAGE", "MODEL")
    - index: Slot index for direct connection (integer)
    - connected: Whether slot is currently connected (boolean)
    - connected_from/connected_to: Connection details if connected
    """
    return await _execute_tool(ctx, "get_node_slots", request.model_dump())


@mcp.tool()
async def connect_nodes_batch(request: ConnectNodesBatchRequest, ctx: Context) -> Dict[str, Any]:
    """Connect multiple node pairs in a single batch operation.
    
    This tool enables efficient batch connection of nodes, reducing the number of
    tool calls needed to build complex workflows from N calls to 1 call.
    
    PARAMETERS:
    - connections: List of connection specifications (source, target, optional slots)
    - auto_match: Enable auto-matching by type (default: true)
    - stop_on_error: Stop on first error vs continue (default: false = continue)
    
    RETURNS:
    Dictionary with:
    - total: Total number of connection attempts
    - successful: Number of successful connections
    - failed: Number of failed connections
    - results: Array of result objects for each connection
    
    Each result object contains:
    - success: Whether connection succeeded (boolean)
    - connection: Connection details if successful
    - error: Error message if failed
    - attempted: Original connection spec if failed
    """
    return await _execute_tool(ctx, "connect_nodes_batch", request.model_dump())


@mcp.tool()
async def auto_connect_workflow(request: AutoConnectWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Automatically connect nodes based on type compatibility.
    
    This tool simplifies workflow creation by automatically connecting nodes in sequence
    or by finding all compatible type matches.
    
    STRATEGIES:
    - "sequential": Connect nodes in order A→B→C→D (left to right workflow)
    - "type_match": Find and connect all compatible type pairs in the workflow
    
    PARAMETERS:
    - node_ids: List of node IDs to connect
    - strategy: Connection strategy (default: "sequential")
    
    RETURNS:
    Dictionary with:
    - connections_made: Number of successful connections
    - connections: Array of connection details
    - failed: Array of failed connection attempts with reasons
    """
    return await _execute_tool(ctx, "auto_connect_workflow", request.model_dump())


# ============================================================================
# LAYOUT MANAGEMENT TOOLS
# ============================================================================

@mcp.tool()
async def get_node_rect(request: GetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
    """Get node position and size."""
    return await _execute_tool(ctx, "get_node_rect", request.model_dump())


@mcp.tool()
async def set_node_rect(request: SetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
    """Set node position and/or size."""
    return await _execute_tool(ctx, "set_node_rect", request.model_dump())

@mcp.tool()
async def modify_layout(request: BatchLayoutRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Modify the layout of multiple nodes by setting their bounding boxes. Use this when moving many nodes at a time. Attempt to avoid overlaps."""
    o = []
    for rect in request.node_rects:
        o.append(await _execute_tool(ctx, "set_node_rect", rect.model_dump()))
    return o

@mcp.tool()
async def position_node_left(request: PositionNodeLeftRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node to the left of another node."""
    return await _execute_tool(ctx, "position_node_left", request.model_dump())


@mcp.tool()
async def position_node_right(request: PositionNodeRightRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node to the right of another node."""
    return await _execute_tool(ctx, "position_node_right", request.model_dump())


@mcp.tool()
async def position_node_top(request: PositionNodeTopRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node above another node."""
    return await _execute_tool(ctx, "position_node_top", request.model_dump())


@mcp.tool()
async def position_node_bottom(request: PositionNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
    """Position a node below another node."""
    return await _execute_tool(ctx, "position_node_bottom", request.model_dump())


@mcp.tool()
async def move_node_right(request: MoveNodeRightRequest, ctx: Context) -> Dict[str, Any]:
    """Move a node to the right, avoiding collisions."""
    return await _execute_tool(ctx, "move_node_right", request.model_dump())


@mcp.tool()
async def move_node_bottom(request: MoveNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
    """Move a node downward, avoiding collisions."""
    return await _execute_tool(ctx, "move_node_bottom", request.model_dump())


# ============================================================================
# WORKFLOW CONTROL TOOLS
# ============================================================================

@mcp.tool()
async def queue_workflow(request: QueueWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Queue the workflow for execution. User might say 'run' the workflow."""
    return await _execute_tool(ctx, "queue_workflow", request.model_dump())


@mcp.tool()
async def cancel_workflow(request: CancelWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Cancel the currently executing workflow."""
    return await _execute_tool(ctx, "cancel_workflow", {})


@mcp.tool()
async def enable_auto_queue(request: EnableAutoQueueRequest, ctx: Context) -> Dict[str, Any]:
    """Enable auto-queue mode."""
    return await _execute_tool(ctx, "enable_auto_queue", {})


@mcp.tool()
async def disable_auto_queue(request: DisableAutoQueueRequest, ctx: Context) -> Dict[str, Any]:
    """Disable auto-queue mode."""
    return await _execute_tool(ctx, "disable_auto_queue", {})


@mcp.tool()
async def set_batch_count(request: SetBatchCountRequest, ctx: Context) -> Dict[str, Any]:
    """Set the workflow batch count."""
    return await _execute_tool(ctx, "set_batch_count", request.model_dump())


@mcp.tool()
async def get_queue_status(request: GetQueueStatusRequest, ctx: Context) -> Dict[str, Any]:
    """Get current queue status and settings."""
    return await _execute_tool(ctx, "get_queue_status", {})


# ============================================================================
# SYSTEM CONTROL TOOLS
# ============================================================================

@mcp.tool()
async def disable_sleep(request: DisableSleepRequest, ctx: Context) -> Dict[str, Any]:
    """Disable system sleep/suspend."""
    return await _execute_tool(ctx, "disable_sleep", {})


@mcp.tool()
async def enable_sleep(request: EnableSleepRequest, ctx: Context) -> Dict[str, Any]:
    """Enable system sleep/suspend."""
    return await _execute_tool(ctx, "enable_sleep", {})


@mcp.tool()
async def disable_screensaver(request: DisableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
    """Disable screensaver."""
    return await _execute_tool(ctx, "disable_screensaver", {})


@mcp.tool()
async def enable_screensaver(request: EnableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
    """Enable screensaver."""
    return await _execute_tool(ctx, "enable_screensaver", {})


@mcp.tool()
async def send_images(request: SendImagesRequest, ctx: Context) -> Dict[str, Any]:
    """Send images to an external URL."""
    return await _execute_tool(ctx, "send_images", request.model_dump())


# ============================================================================
# UTILITY TOOLS
# ============================================================================
# TODO: These all can use python instead of the frontend bridge, lol!

@mcp.tool()
async def generate_seed(request: GenerateSeedRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random seed value."""
    return await _execute_tool(ctx, "generate_seed", {})


@mcp.tool()
async def generate_float(request: GenerateFloatRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random float value."""
    return await _execute_tool(ctx, "generate_float", request.model_dump())


@mcp.tool()
async def generate_int(request: GenerateIntRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random integer value."""
    return await _execute_tool(ctx, "generate_int", request.model_dump())


@mcp.tool()
async def random_choice(request: RandomChoiceRequest, ctx: Context) -> Dict[str, Any]:
    """Pick a random item from a list."""
    return await _execute_tool(ctx, "random_choice", request.model_dump())

# ============================================================================
# COMFYUI EXTENDED TOOLS
# ============================================================================

@mcp.tool()
async def comfy_list_folders(request: ComfyListFoldersRequest, ctx: Context) -> Dict[str, Any]:
    """List contents of ComfyUI custom nodes, checkpoints, input, output and more with type-aware organization.
    
    This tool provides agents with deterministic access to ComfyUI directory structure.
    
    USE CASES:
    - Custom Node Discovery: folder_type="custom_nodes" → List all installed node packs
    - Model Management: folder_type="checkpoints" → List available diffusion models
    - LoRA Discovery: folder_type="loras" → List LoRA adaptation files
    - Output Review: folder_type="output" → List recently generated images
    - Input Files: folder_type="input" → List available input files
    
    SECURITY: All paths are validated and sandboxed to ComfyUI installation.
    """
    try:
        tools = get_comfy_tools()
        items = tools.list_folders(request.folder_type)
        
        return {
            "folder_type": request.folder_type.value,
            "folder_path": tools.folder_mappings[request.folder_type],
            "items": items,
            "total_items": len(items),
            "comfyui_root": str(tools.comfyui_root)
        }
        
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"ComfyUI operation failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in comfy_list_folders: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def comfy_read_file(request: ComfyReadFileRequest, ctx: Context) -> Dict[str, Any]:
    """Read files within ComfyUI for analysis and understanding.
    
    This tool enables agents to examine ComfyUI files to understand capabilities or debug node settings.
    
    USE CASES:
    - Node Discovery: Read "custom_nodes/{pack}/__init__.py" → Extract NODE_CLASS_MAPPINGS
    - Implementation Analysis: Read node .py files → Understand functionality and inputs
    - Documentation: Read "custom_nodes/{pack}/README.md" → Get usage info
    - Dependencies: Read "requirements.txt" → Check compatibility
    - Configuration: Read config files → Understand settings
    
    COMMON FILE PATTERNS:
    - "custom_nodes/{pack}/__init__.py" → Node registration and mappings
    - "custom_nodes/{pack}/nodes.py" → Node implementations
    - "custom_nodes/{pack}/README.md" → Documentation and examples
    - "custom_nodes/{pack}/requirements.txt" → Python dependencies
    
    SECURITY: Files are sandboxed to ComfyUI directory, size limits enforced.
    """
    try:
        tools = get_comfy_tools()
        content = tools.read_file(request.path, request.max_size)
        
        # Get file info
        full_path = tools._validate_path(request.path)
        stat = full_path.stat()
        
        return {
            "path": request.path,
            "content": content,
            "size": stat.st_size,
            "encoding": "utf-8",
            "extension": full_path.suffix,
            "comfyui_root": str(tools.comfyui_root)
        }
        
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"ComfyUI file operation failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in comfy_read_file: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def comfy_search_resources(request: ComfySearchFilesRequest, ctx: Context) -> Dict[str, Any]:
    """Search for patterns in ComfyUI files to discover functionality.
    
    This tool enables agents to efficiently discover specific functionality.
    - Find installed nodes
    - Find Node Packs
    - Find installed models, LoRAs, etc.
    - Find any resource within comfy with the right search patterns
    
    USE CASES:
    - Node Discovery: pattern="NODE_CLASS_MAPPINGS" → Find all node registrations
    - Class Search: pattern="class.*Upscale" → Find upscaling node implementations
    - Function Search: pattern="def.*encode" → Find encoding functions
    - Capability Search: pattern="upscale|enhance|resize" → Find image enhancement
    - Documentation: pattern="example|tutorial" → Find usage examples
    - Dependencies: pattern="requirements" → Find dependency files
    
    PERFORMANCE: Results limited by max_results, provides context for understanding.
    """
    try:
        tools = get_comfy_tools()
        results = tools.search_files(
            pattern=request.pattern,
            folder_type=request.folder_type,
            file_pattern=request.file_pattern,
            max_results=request.max_results,
            context_lines=request.context_lines
        )
        
        return {
            "pattern": request.pattern,
            "folder_type": request.folder_type.value,
            "results": results,
            "total_matches": len(results),
            "files_searched": 0,  # Could track this if needed
            "truncated": len(results) >= request.max_results,
            "comfyui_root": str(tools.comfyui_root)
        }
        
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"ComfyUI search operation failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in comfy_search_files: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


# ============================================================================
# ERROR FEEDBACK & QUEUE STATUS TOOLS
# ============================================================================

@mcp.tool()
async def get_recent_errors(request: GetRecentErrorsRequest, ctx: Context) -> Dict[str, Any]:
    """Get recent execution errors from ComfyUI.
    
    Retrieves the N most recent errors that occurred during workflow execution.
    Useful for debugging failed workflows and understanding error patterns.
    """
    limit = min(request.limit, 100)  # Cap at buffer size
    errors = manager.error_buffer.get_recent_errors(limit)
    return {
        "errors": errors,
        "count": len(errors),
        "total_in_buffer": manager.error_buffer.get_count()
    }

@mcp.tool()
async def get_errors_for_run(request: GetErrorsForRunRequest, ctx: Context) -> Dict[str, Any]:
    """Get all errors for a specific workflow run.
    
    Retrieves all errors that occurred during a specific workflow execution,
    identified by its prompt_id. Use this to debug why a particular run failed.
    """
    errors = manager.error_buffer.get_errors_for_prompt(request.prompt_id)
    return {
        "prompt_id": request.prompt_id,
        "errors": errors,
        "count": len(errors)
    }

@mcp.tool()
async def get_queue_status_details(request: GetQueueStatusDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get current ComfyUI queue status and active executions.
    
    Returns information about currently running and queued workflows,
    including execution progress and node tracking.
    """
    queue_status = manager.execution_tracker.get_queue_status()
    active_executions = manager.execution_tracker.get_all_executions()
    
    return {
        "queue": queue_status,
        "active_executions": active_executions,
        "execution_count": len(active_executions)
    }

@mcp.tool()
async def get_execution_details(request: GetExecutionDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get detailed execution state for a specific workflow run.
    
    Provides comprehensive information about a workflow execution including
    current node, executed nodes, cached nodes, and status.
    """
    execution = manager.execution_tracker.get_execution_state(request.prompt_id)
    return {
        "prompt_id": request.prompt_id,
        "found": execution is not None,
        "execution": execution
    }

@mcp.tool()
async def clear_error_buffer(request: ClearErrorBufferRequest, ctx: Context) -> Dict[str, Any]:
    """Clear the error buffer.
    
    Removes all stored errors from the buffer. Use this to start fresh
    after fixing issues or when the buffer gets too cluttered.
    """
    previous_count = manager.error_buffer.get_count()
    manager.error_buffer.clear()
    return {
        "cleared": True,
        "previous_count": previous_count
    }

    
# Other Ideas
#   (**DONE**) Meta-Awareness: Awareness of the full environment including installed plugins (this is through python I'm assuming!)
#   Workspace awareness: what tabs do you have? can we switch workflow tabs? etc. (from frontend then executed tools through here?)
#   Workflow awareness: list workflows, find workflows? or like rather, be pointed at a folder or workflow to load? loading, etc. stuff that's in the file menu?
#   (**DONE**) Node Search and Node Finding: What is already possible through comfy lib? It'd be nice to have tools for find_installed_node that lets us search over all nodes names, descriptions, etc.

def main():
    mcp.run()
    
if __name__ == "__main__":
    main()
