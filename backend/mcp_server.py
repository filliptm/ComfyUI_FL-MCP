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
from pydantic import BaseModel, Field

from models import WorkflowQuery

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
    _ws_client = ctx.request_context.lifespan_context.client
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
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title")
    source_slot: Union[str, int] = Field(..., description="Source output slot name or index")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title")
    target_slot: Optional[Union[str, int]] = Field(None, description="Target input slot name or index (defaults to source_slot)")

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


# ============================================================================
# QUERY & ANALYSIS TOOLS
# ============================================================================

@mcp.tool()
async def query_workflow(query: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation."""
    return await _execute_tool(ctx, "query_workflow", query.model_dump())


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

@mcp.tool()
async def find_node(request: FindNodeRequest, ctx: Context) -> Dict[str, Any]:
    """Find a node by ID, type, or title."""
    return await _execute_tool(ctx, "find_node", request.model_dump())


@mcp.tool()
async def create_node(request: CreateNodeRequest, ctx: Context) -> Dict[str, Any]:
    """Create a new node in the workflow."""
    return await _execute_tool(ctx, "create_node", request.model_dump())


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
    """Connect two nodes together."""
    return await _execute_tool(ctx, "connect_nodes", request.model_dump())


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
    """Queue the workflow for execution."""
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


def main():
    """Run the MCP server as a standalone application."""
    mcp.run()
    
if __name__ == "__main__":
    main()
