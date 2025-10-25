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
from node_library import (
    get_node_library_client,
    NodeLibraryError,
    NodeLibraryConnectionError,
    NodeTypeNotFoundError
)

from comfy_manager import (
    get_comfy_manager_client,
    ManagerError,
    ManagerNotInstalledError,
    ManagerConnectionError,
    ManagerAPIError
)
from sysinfo import get_system_info as _get_system_info

from manager import manager # This is the Connection Manager, not comfy manager :D
from calc import acalc_batch, CalcBatchParams

# LOGGING

log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)

# Ensure log directory exists (optional)
os.makedirs("logs", exist_ok=True)
log_file = "logs/ren_server.log"

# Configure logging to both console and file
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),              # Console output
        logging.FileHandler(log_file, mode="w", encoding="utf-8")  # File output
    ],
)

logger = logging.getLogger("ren_server")
logger.info(f"Logger initialized with level: {log_level_name}")


# ============================================================================
# WebSocket Client for MCP Subprocess (persist across tool calls)
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
        """Connect to backend WebSocket server (one-time, persistent)."""
        if self.connected and self.ws and not self.ws.closed:
            return

        logger.info(f"[MCP-WS] Connecting to {self.ws_url} with session {self.session_id}")
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
        
        if data.get('type') != 'handshake_ack':
            raise RuntimeError(f"Unexpected handshake response: {data}")

        self.connected = True
        logger.info(f"[MCP-WS] Connected and handshake complete")

        # Start a single persistent receive loop
        if not self._receive_task or self._receive_task.done():
            self._receive_task = asyncio.create_task(self._receive_loop())
    
    async def _receive_loop(self):
        """Receive and process messages from backend."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                await self._handle_message(data)
        except websockets.exceptions.ConnectionClosed:  # includes OK(1000)
            logger.warning(f"[MCP-WS] Connection closed")
            self.connected = False
            await self._fail_all_pending(RuntimeError("WebSocket closed"))
        except Exception as e:
            logger.error(f"[MCP-WS] Receive loop error: {e}")
            self.connected = False
            await self._fail_all_pending(RuntimeError(f"WebSocket error: {e!r}"))
    
    async def _handle_message(self, data: dict):
        """Handle incoming message from backend."""
        msg_type = data.get('type')
        
        if msg_type == 'tool_result':
            request_id = data.get('request_id')
            future = self.pending_requests.get(request_id)
            if future and not future.done():
                if data.get('success'):
                    future.set_result(data.get('data'))
                else:
                    future.set_exception(RuntimeError(data.get('error', 'Tool execution failed')))
                self.pending_requests.pop(request_id, None)
        else:
            logger.warning(f"[MCP-WS] Unexpected message type: {msg_type}")

    async def _fail_all_pending(self, exc: Exception):
        # Fail and clear all outstanding tool calls so they don't hang to timeout
        for rid, fut in list(self.pending_requests.items()):
            if not fut.done():
                fut.set_exception(exc)
            self.pending_requests.pop(rid, None)

    async def execute_tool(self, tool_name: str, parameters: dict, timeout_ms: int = 30000) -> dict:
        """Execute a tool via WebSocket callback."""
        if not self.connected or not self.ws:
            raise RuntimeError("WebSocket not connected")
        
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[request_id] = future
        
        logger.info(f"[MCP-WS] Executing tool: {tool_name} (request_id: {request_id})")
        
        try:
            await self.ws.send(json.dumps({
                'type': 'tool_request',
                'session_id': self.session_id,
                'request_id': request_id,
                'tool_name': tool_name,
                'parameters': parameters,
                'timeout_ms': timeout_ms,
            }))
            
            timeout_seconds = timeout_ms / 1000.0
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
            logger.info(f"[MCP-WS] Tool execution complete: {request_id}")
            return result

        except Exception as e:
            logger.error(f"[MCP-WS] Tool execution error: {e}")
            # ensure future is cleaned up on any error path
            self.pending_requests.pop(request_id, None)
            raise
    
    async def disconnect(self):
        """Optional explicit shutdown (not used by lifespan)."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self.ws and not self.ws.closed:
            await self.ws.close()
        self.connected = False
        logger.info("[MCP-WS] Disconnected")


# ============================================================================
# FastMCP lifespan: reuse a single persistent client (no teardown)
# ============================================================================

_WS_CLIENT = None  # module-level singleton

@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[Any]:
    """Manage MCP server lifespan and persistent WebSocket connection."""
    global _WS_CLIENT

    # Check if ComfyUI Manager is installed and initialize client
    manager_client = None
    manager_available = False
    
    logger.info(f"FL_MCP_MODE: {os.getenv('FL_MCP_MODE')}")
    
    try:
        from config import settings
        manager_client = get_comfy_manager_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        version_info = await manager_client.check_installed()
        
        if version_info.installed:
            logger.info(f"[MCP] ComfyUI Manager detected (v{version_info.version})")
            manager_available = True
        else:
            logger.warning("[MCP] ComfyUI Manager not installed - manager tools will return errors")
    except Exception as e:
        logger.warning(f"[MCP] Could not check Manager status: {e}")

    if os.getenv('FL_MCP_MODE') == 'subprocess':
        session_id = os.getenv('FL_SESSION_ID')
        ws_url = os.getenv('FL_WS_URL')
        if not session_id or not ws_url:
            logger.error("Missing FL_SESSION_ID or FL_WS_URL environment variables")
            raise RuntimeError("MCP subprocess not properly configured")
        
        logger.info(f"[MCP] Starting in subprocess mode for session: {session_id}")

        try:
            # Create once and keep alive across tool calls
            if _WS_CLIENT is None:
                _WS_CLIENT = MCPWebSocketClient(session_id, ws_url)
                await _WS_CLIENT.connect()
                logger.info("[MCP] WebSocket client connected (persistent)")
            elif not _WS_CLIENT.connected or (_WS_CLIENT.ws and _WS_CLIENT.ws.closed):
                # Session exists but not connected (e.g., prior close). No auto-reconnect logic here;
                # just try to connect once.
                await _WS_CLIENT.connect()
                logger.info("[MCP] WebSocket client reconnected (persistent)")
            
            yield {
                "client": _WS_CLIENT,
                "manager_client": manager_client,
                "manager_available": manager_available
            }
        except Exception as e:
            logger.error(f"MCP Initialization Failed: {str(e)}")

        # NOTE: no disconnect/teardown here; keep WS open for the process lifetime.
        return

    # Standalone (no WebSocket bridge)
    logger.info("[MCP] Running in standalone mode (no WebSocket)")
    yield {
        "client": None,
        "manager_client": manager_client,
        "manager_available": manager_available
    }

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

import time
async def _report_tool_activity(ctx: Context, tool_name: str) -> None:
    """Report tool activity to frontend for Python-only tools."""
    _ws_client = ctx.request_context.lifespan_context.get('client')
    if _ws_client and _ws_client.connected:
        try:
            await _ws_client.ws.send(json.dumps({
                'type': 'tool_report',
                'session_id': _ws_client.session_id,
                'tool_name': tool_name,
                'timestamp': time.time()
            }))
        except Exception as e:
            logger.debug(f"Could not report tool activity: {e}")


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
    """Request to create a new node.

    Simplified schema for better LLM JSON generation reliability.
    Position flattened to x/y fields. Parameters removed - set them separately with set_node_values.
    """
    node_type: str = Field(..., description="ComfyUI node class name (e.g., 'CheckpointLoaderSimple')")
    x: Optional[float] = Field(None, description="X position (pixels from left)")
    y: Optional[float] = Field(None, description="Y position (pixels from top)")
    
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

class FocusOnNodesRequest(BaseModel):
    """Request to fit canvas view to specific nodes."""
    node_ids: Optional[List[int]] = Field(
        None,
        description="Node IDs to focus on (null=selected nodes, empty=all nodes)"
    )

class TakeScreenshotRequest(BaseModel):
    """Request to take a screenshot of the canvas."""
    format: Literal["jpeg", "png"] = Field(
        "jpeg",
        description="Image format (jpeg recommended for smaller size)"
    )
    quality: float = Field(
        0.9,
        ge=0.0,
        le=1.0,
        description="JPEG quality (0.0-1.0, only applies to jpeg format)"
    )


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
    """Single connection specification for batch operations.

    Simplified schema for better LLM JSON generation - removed Union types.
    Use node IDs (integers) for reliability. Slot names as strings only.
    """
    source_node_id: int = Field(..., description="Source node ID")
    target_node_id: int = Field(..., description="Target node ID")
    source_slot_name: Optional[str] = Field(None, description="Source output slot name (optional for auto-match)")
    target_slot_name: Optional[str] = Field(None, description="Target input slot name (optional for auto-match)")

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

class GetLayoutRequest(BaseModel):
    """Request to get layout for all nodes or specific nodes."""
    node_ids: Optional[List[Union[int, str]]] = Field(
        None, 
        description="Optional list of node IDs or titles to get rects for (omit for all nodes)"
    )

class SetNodeRectRequest(BaseModel):
    """Request to set node position and/or size."""
    node_id: int = Field(..., description="Node id of the node who's layout rectangle to set.")
    x: Optional[float] = Field(None, description="X position (null to keep current)")
    y: Optional[float] = Field(None, description="Y position (null to keep current)")
    width: Optional[float] = Field(None, description="Width (null to keep current)")
    height: Optional[float] = Field(None, description="Height (null to keep current)")

class NodeRect(BaseModel):
    """Single node layout specification.

    Flattened schema for better LLM JSON generation - node_id included directly.
    """
    node_id: int = Field(..., description="Node ID to modify")
    x: Optional[float] = Field(None, description="X position (omit to keep current)")
    y: Optional[float] = Field(None, description="Y position (omit to keep current)")
    width: Optional[float] = Field(None, description="Width (omit to keep current)")
    height: Optional[float] = Field(None, description="Height (omit to keep current)")

class BatchLayoutRequest(BaseModel):
    """Modify layout of multiple nodes.

    Simplified schema - changed from Dict[int, NodeRect] to List[NodeRect].
    """
    node_rects: List[NodeRect] = Field(..., description="List of node rectangles to update")

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
    # batch_count: Optional[int] = Field(None, description="Number of times to execute (default: current batch count)")
    pass

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

class GetSystemInfoRequest(BaseModel):
    """Request for system information."""
    pass  # No parameters needed

# # Error Feedback
# class GetRecentErrorsRequest(BaseModel):
#     """Request to get recent execution errors."""
#     limit: int = Field(10, description="Number of recent errors to retrieve (default: 10, max: 100)")

# class GetErrorsForRunRequest(BaseModel):
#     """Request to get errors for a specific workflow run."""
#     prompt_id: str = Field(..., description="The prompt/run ID to get errors for")

class GetWorkflowHistoryRequest(BaseModel):
    """Request for workflow history."""
    prompt_id: Optional[str] = Field(
        default=None,
        description="Specific prompt ID to get history for. If None, returns recent history."
    )
    max_items: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of history items to return (1-100)"
    )

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

# ============================================================================
# NODE LIBRARY REQUEST MODELS
# ============================================================================

class NodeLibrarySearchRequest(BaseModel):
    """Search for ComfyUI node types by various criteria."""
    query: Optional[str] = Field(
        None,
        description="Text search in node type names and descriptions (case-insensitive)"
    )
    category: Optional[str] = Field(
        None,
        description="Filter by node category (e.g., 'sampling', 'loaders', 'image')"
    )
    input_type: Optional[str] = Field(
        None,
        description="Find node types accepting this input type (e.g., 'LATENT', 'IMAGE')"
    )
    output_type: Optional[str] = Field(
        None,
        description="Find node types producing this output type (e.g., 'IMAGE', 'LATENT')"
    )
    max_results: int = Field(
        20,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50)"
    )


class NodeLibraryGetDetailsRequest(BaseModel):
    """Get detailed information about a specific node type."""
    node_type: str = Field(
        ...,
        description="Exact node type name (e.g., 'KSampler', 'CheckpointLoaderSimple')"
    )


class NodeLibraryFindCompatibleRequest(BaseModel):
    """Find node types compatible with a given node type."""
    node_type: str = Field(
        ...,
        description="Source node type name (e.g., 'KSampler')"
    )
    direction: Literal["downstream", "upstream", "both"] = Field(
        "downstream",
        description="downstream=connects AFTER, upstream=connects BEFORE, both=both directions"
    )
    output_slot: Optional[str] = Field(
        None,
        description="Specific output slot name to match (downstream only)"
    )
    input_slot: Optional[str] = Field(
        None,
        description="Specific input slot name to match (upstream only)"
    )
    max_results: int = Field(
        30,
        ge=1,
        le=100,
        description="Maximum results per direction (1-100)"
    )

# ============================================================================
# MANAGER REQUEST MODELS
# ============================================================================

class ManagerSearchNodesRequest(BaseModel):
    """Search for custom node packs in ComfyUI Manager."""
    query: Optional[str] = Field(None, description="Search query for node pack name/description/author")
    node_filter: Optional[str] = Field(None, description="Regex pattern to filter by node class names (e.g., 'KSampler', 'FL_.*', 'Image.*Saver')")
    category: Optional[str] = Field(None, description="Filter by category")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates available")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(16, ge=1, le=100, description="Maximum results to return")


class ManagerGetNodeMappingsRequest(BaseModel):
    """Get node type to pack mappings from ComfyUI Manager."""
    node_type: Optional[str] = Field(None, description="Specific node type to look up (empty for all)")
    mode: Literal["local", "remote", "nickname"] = Field("local", description="Mapping source")


class ManagerCheckUpdatesRequest(BaseModel):
    """Check for available updates to installed node packs."""
    mode: Literal["local", "remote"] = Field("remote", description="Check mode")


class ManagerSearchExternalModelsRequest(BaseModel):
    """Search for uninstalled models in ComfyUI Manager registry."""
    query: Optional[str] = Field(
        None, 
        description="Regex search across name, description, filename"
    )
    base_filter: Optional[str] = Field(
        None, 
        description="Regex filter for base (e.g., 'FLUX', 'SDXL', 'SD1')"
    )
    type_filter: Optional[str] = Field(
        None, 
        description="Regex filter for type (e.g., 'checkpoint', 'lora', 'upscale', 'TAESD')"
    )
    name_filter: Optional[str] = Field(
        None, 
        description="Regex filter for model name"
    )
    description_filter: Optional[str] = Field(
        None, 
        description="Regex filter for description text"
    )
    reference_filter: Optional[str] = Field(
        None, 
        description="Regex filter for reference URL"
    )
    uninstalled_only: bool = Field(
        True, 
        description="Only show uninstalled models (default: True)"
    )
    installed_only: bool = Field(
        False, 
        description="Only show installed models (default: False)"
    )
    max_results: int = Field(
        10, 
        ge=1, 
        le=100, 
        description="Maximum results to return (1-100)"
    )
    mode: Literal["cache", "remote"] = Field(
        "cache", 
        description="Data source mode"
    )
    
# PNG Workflow Extraction
class ExtractWorkflowFromImageRequest(BaseModel):
    """Request to extract workflow from PNG metadata."""
    image_path: str = Field(
        ...,
        description="Path to PNG file relative to ComfyUI root (e.g., 'output/ComfyUI_00042_.png')"
    )


# ===========================================================================
# GENERAL UTILITIES
# ===========================================================================

@mcp.tool()
async def calculate_expressions(request: CalcBatchParams, ctx: Context) -> Dict[str, Any]:
    """
    Evaluate a *batch* of math AST expressions return their results. Great for calculating simple math expressions for calculating bounding boxes for layout modification. Don't include comments.

    Features:
      • Supports + - * / // % **, parentheses, unary +/-
      • Variables & **simple assignments** (`x = 2+3`) that persist across lines
      • Math funcs: sin, cos, tan, asin, acos, atan, atan2, sinh, cosh, tanh,
        exp, log, log10, log2, sqrt, floor, ceil, hypot, radians, degrees
      • Builtins: abs, round, min, max, pow
      • Constants: pi, e, tau
      • Random (seeded via `params.seed`): `rand()` / `random()`, `uniform(a,b)`, `randint(a,b)`
      • No `eval` or attributes; AST is strictly whitelisted
      • If `params.variables` is given, it is **updated** with numeric names

    Returns
    -------
    list[float] : one numeric result per input expression (assignment returns assigned value)
    """
    await _report_tool_activity(ctx, "calculate_expressions")
    
    try:
        response = await acalc_batch(request)
        return {"results": response}
    except Exception as e:
        ctx.error(str(e))
        raise

@mcp.tool()
async def wait(request: WaitRequest, ctx: Context) -> Dict[str, Any]:
    """Use this to wait for some short period of time, perhaps after generating an image"""
    await _report_tool_activity(ctx, "wait")
    
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
    """Create one or more new nodes in the workflow. BEFORE CALLING THIS TOOL: check to see that the node exists by searching using node_library_search tool.

    This is a TRUE BATCH operation - all nodes are created in a single frontend execution without round-trips per node.
    """
    node_count = len(request.nodes)
    logger.info(f"[BATCH] Creating {node_count} nodes in single batch operation")

    # Send all nodes at once to frontend for batch creation
    result = await _execute_tool(ctx, "create_nodes_batch", request.model_dump())

    logger.info(f"[BATCH] Batch create complete: {node_count} nodes")
    return result


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
async def focus_on_nodes(request: FocusOnNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Fit canvas view to show specific nodes or current selection. If you don't know what nodes to focus on, use `workflow_overview` to get some node ids based on your task
    
    This tool adjusts the canvas viewport to center and fit the specified nodes,
    making them clearly visible. Useful for:
    - Focusing on a workflow section before taking a screenshot
    - Navigating to specific nodes in large workflows
    - Preparing visual context for user
    
    PARAMETERS:
    - node_ids: Optional list of node IDs
      - null (default): Fit to currently selected nodes
      - [] (empty list): Fit to all nodes in workflow
      - [1, 2, 3]: Fit to specific nodes
    
    WORKFLOW:
    1. select_nodes([1, 2, 3]) - Select nodes
    2. focus_on_nodes() - Zoom to selected nodes (no params needed)
    3. take_screenshot() - Capture the focused view
    
    RETURNS:
    - fitted_count: Number of nodes fitted in view
    """
    return await _execute_tool(ctx, "focus_on_nodes", request.model_dump())

@mcp.tool()
async def take_screenshot(request: TakeScreenshotRequest, ctx: Context) -> Dict[str, Any]:
    """Capture the current ComfyUI canvas as an image. Make sure to use focus_on_nodes before this always.
    
    This tool takes a screenshot of the workflow canvas and saves it to
    output/screenshots/. The screenshot can then be displayed to the user
    or analyzed by the agent.
    
    USE CASES:
    - Visual documentation: "Show me the workflow"
    - Section capture: Focus on nodes, then screenshot
    - Debugging: Capture problematic workflow sections for the user to see
    - Sharing: Create shareable workflow images
    
    The URL can be embedded directly in agent responses:
    ![Screenshot](api/view?filename={filename}&type=output&subfolder=screenshots&rand=0.123)
    """
    result = await _execute_tool(ctx, "take_screenshot", request.model_dump())
    
    # Add URL for easy markdown embedding with public_url support
    if result.get('success') and result.get('filename'):
        import random
        from config import settings
        
        # Use public_url from settings (supports both localhost and ngrok)
        base_url = settings.public_url.rstrip('/')
        result['url'] = (
            f"{base_url}/api/view?filename={result['filename']}"
            f"&type=output&subfolder=screenshots&rand={random.random()}"
        )
    
    return result


@mcp.tool()
async def get_current_node_selection(request: GetSelectedNodesRequest, ctx: Context) -> Dict[str, Any]:
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

# @mcp.tool()
# async def get_node_rect(request: GetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
#     """Get node position and size. Only use this to """
#     return await _execute_tool(ctx, "get_node_rect", request.model_dump())


@mcp.tool()
async def get_layout(request: GetLayoutRequest, ctx: Context) -> Dict[str, Any]:
    """Get position and size for all nodes (or specified nodes) in the workflow. Use it to understand node spatial organization or understanding visual workflow structure.
    
    This tool retrieves layout information for the entire workflow at once. Use it before calling `modify_layout`.
    
    Returns:
        {
            "nodes": [
                {
                    "node_id": int,
                    "title": str,
                    "type": str,
                    "rect": {"x": float, "y": float, "width": float, "height": float}
                },
                ...
            ],
            "count": int
        }
        
    """
    return await _execute_tool(ctx, "get_layout", request.model_dump())


# @mcp.tool()
# async def set_node_rect(request: SetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
#     """Set node position and/or size."""
#     return await _execute_tool(ctx, "set_node_rect", request.model_dump())

@mcp.tool()
async def modify_layout(request: BatchLayoutRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Modify the layout of multiple nodes by setting their bounding boxes. Use this to rearrange many nodes at a time. Attempt to avoid overlaps. Before calling this tool call `get_layout` to get the current workflow layout or for some set of nodes.
    
    When defining bounding boxes make sure to account for vertical and horizontal spacing between elements that are supposed to be close.
    """
    return await _execute_tool(ctx, "modify_layout", request.model_dump())
    # o = []
    # for rect in request.node_rects:
    #     o.append(await _execute_tool(ctx, "set_node_rect", rect.model_dump()))
    # return o

# @mcp.tool()
# async def position_node_left(request: PositionNodeLeftRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node to the left of another node."""
#     return await _execute_tool(ctx, "position_node_left", request.model_dump())


# @mcp.tool()
# async def position_node_right(request: PositionNodeRightRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node to the right of another node."""
#     return await _execute_tool(ctx, "position_node_right", request.model_dump())


# @mcp.tool()
# async def position_node_top(request: PositionNodeTopRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node above another node."""
#     return await _execute_tool(ctx, "position_node_top", request.model_dump())


# @mcp.tool()
# async def position_node_bottom(request: PositionNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node below another node."""
#     return await _execute_tool(ctx, "position_node_bottom", request.model_dump())


# @mcp.tool()
# async def move_node_right(request: MoveNodeRightRequest, ctx: Context) -> Dict[str, Any]:
#     """Move a node to the right, avoiding collisions."""
#     return await _execute_tool(ctx, "move_node_right", request.model_dump())


# @mcp.tool()
# async def move_node_bottom(request: MoveNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
#     """Move a node downward, avoiding collisions."""
#     return await _execute_tool(ctx, "move_node_bottom", request.model_dump())


# ============================================================================
# WORKFLOW CONTROL TOOLS
# ============================================================================

@mcp.tool()
async def queue_workflow(request: QueueWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Queue the workflow for execution.
    
    Before calling this tool, call `workflow_overview` to double check for 
    disconnected nodes and any missing slot connections.
    
    This tool verifies that the workflow actually made it into ComfyUI's queue
    and provides detailed feedback on any validation errors or queue failures.
    
    Returns:
        Success case:
        {
            "success": True,
            "prompt_id": str,
            "queue_number": int,
            "batch_count": int,
            "status": "queued" | "running",
            "message": str
        }
        
        Validation error case:
        {
            "success": False,
            "error": "Workflow validation failed",
            "node_errors": {...},
            "suggestion": str
        }
        
        Queue failure case:
        {
            "success": False,
            "error": str,
            "prompt_id": str,
            "suggestion": str
        }
    """
    # Queue the workflow via frontend
    r = await _execute_tool(ctx, "queue_workflow", request.model_dump())
    logger.debug(f"Queue result: {r}")
    
    # Extract queue information from frontend response
    prompt_id = r.get('prompt_id')
    node_errors = r.get('node_errors', {})
    queue_number = r.get('queue_number')
    batch_count = r.get('batch_count')
    
    # Check for node validation errors first
    if node_errors:
        logger.warning(f"Workflow validation failed: {node_errors}")
        return {
            "success": False,
            "error": "Workflow validation failed",
            "node_errors": node_errors,
            "suggestion": (
                "The workflow has node configuration errors. "
                "Use workflow_overview to identify disconnected nodes or missing inputs. "
                "Fix the errors and try queueing again."
            )
        }
    
    # Verify the workflow actually made it into the queue
    if prompt_id:
        try:
            # Check if prompt appears in history (it should appear immediately when queued)
            history_result = await get_execution_history(
                GetWorkflowHistoryRequest(prompt_id=prompt_id),
                ctx
            )
            
            # If status is 'unknown', the prompt never made it to the queue
            if history_result.get('status') == 'unknown':
                logger.error(f"Prompt {prompt_id} not found in queue or history")
                return {
                    "success": False,
                    "error": "Workflow failed to queue",
                    "prompt_id": prompt_id,
                    "suggestion": (
                        "ComfyUI did not accept the workflow. This can happen when:\n"
                        "1. No parameter in the workflow has changed and ComfyUI is returning cached results (example: you're running on a fixed seed in all ksamplers and trying to queue without changing prompts or setting any other values in any node)\n"
                        "2. ComfyUI rejected the workflow for internal reasons\n\n"
                        "Give the user ren links with options to modify different parameters (like seed, steps, or strength) and queue again or try some other next thing they might do."
                        "Use get_execution_history to check for further errors using the prompt_id."
                    )
                }
            
            # Success - workflow is queued or already running
            status = history_result.get('status', 'queued')
            logger.info(f"Workflow queued successfully: {prompt_id} (position {queue_number}, status: {status})")
            
            return {
                "success": True,
                "prompt_id": prompt_id,
                "queue_number": queue_number,
                "batch_count": batch_count,
                "status": status,
                "message": f"Workflow queued successfully at position {queue_number} (status: {status})"
            }
            
        except Exception as e:
            # History check failed - log but don't fail the queue operation
            logger.warning(f"Could not verify queue status: {e}")
            return {
                "success": True,
                "prompt_id": prompt_id,
                "queue_number": queue_number,
                "batch_count": batch_count,
                "status": "queued",
                "message": f"Workflow queued at position {queue_number} (verification skipped)",
                "warning": "Could not verify queue status"
            }
    else:
        # No prompt_id returned - unexpected error
        logger.error(f"No prompt_id in queue result: {r}")
        return {
            "success": False,
            "error": "No prompt_id returned from queue operation",
            "raw_result": r,
            "suggestion": "This is unexpected. Check that ComfyUI is running and the frontend is connected."
        }


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

# THIS IS COMMENTED OUT BECAUSE: Vanilla claude code is a lying piece of shit and it didn't implement shit, plus this is fucking backend functionality... vibe coded slop... removed.

# @mcp.tool()
# async def disable_sleep(request: DisableSleepRequest, ctx: Context) -> Dict[str, Any]:
#     """Disable system sleep/suspend."""
#     return await _execute_tool(ctx, "disable_sleep", {})


# @mcp.tool()
# async def enable_sleep(request: EnableSleepRequest, ctx: Context) -> Dict[str, Any]:
#     """Enable system sleep/suspend."""
#     return await _execute_tool(ctx, "enable_sleep", {})


# @mcp.tool()
# async def disable_screensaver(request: DisableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
#     """Disable screensaver."""
#     return await _execute_tool(ctx, "disable_screensaver", {})


# @mcp.tool()
# async def enable_screensaver(request: EnableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
#     """Enable screensaver."""
#     return await _execute_tool(ctx, "enable_screensaver", {})


# @mcp.tool()
# async def send_images(request: SendImagesRequest, ctx: Context) -> Dict[str, Any]:
#     """Send images to an external URL."""
#     return await _execute_tool(ctx, "send_images", request.model_dump())


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

@mcp.tool()
async def get_system_info(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Get system and environment information for installation guidance.
    
    This tool provides OS, Python, and virtual environment details to help
    provide platform-specific installation instructions for ComfyUI components.
    
    USE CASES:
    - Installation Guidance: Determine correct pip/python commands for user's platform
    - Environment Detection: Check if running in venv/conda for dependency installation
    - Platform-Specific Help: Provide Windows vs Linux vs macOS specific instructions
    - ControlNet Setup: Guide users through manual model installation with correct paths
    - Dependency Installation: Show correct command syntax for user's environment
    
    RETURNED INFORMATION:
    - OS platform (Windows/Linux/Darwin) and architecture
    - Python version and executable path
    - Virtual environment status and type (venv/conda/virtualenv)
    - ComfyUI installation paths
    - Platform-specific installation command templates
        
    SECURITY: Read-only system information, no modifications.
    """
    await _report_tool_activity(ctx, "get_system_info")
    
    try:
        # Get ComfyUI tools to include installation paths
        tools = get_comfy_tools()
        
        # Get comprehensive system info
        info = _get_system_info(comfy_tools=tools)
        
        return info
        
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        # Still return basic info even if ComfyUI tools fail
        try:
            info = _get_system_info(comfy_tools=None)
            info["warning"] = "ComfyUI paths unavailable"
            return info
        except Exception as e2:
            logger.error(f"Fatal error in get_system_info: {e2}")
            raise RuntimeError(f"Failed to get system information: {e2}")


# ============================================================================
# COMFYUI EXTENDED TOOLS
# ============================================================================

@mcp.tool()
async def comfy_list_folders(request: ComfyListFoldersRequest, ctx: Context) -> Dict[str, Any]:
    """List contents of ComfyUI custom nodes, checkpoints, input, output, workflows folders and more with filtering, sorting, and limiting.
    
    Supports regex pattern filtering on full paths, flexible sorting by multiple
    dimensions (name, size, modified_time, type), sort order control (asc/desc),
    and result limiting for efficient agent-based file discovery.
    
    USE CASES:
    - Custom Node Discovery: folder_type="custom_nodes" → List all installed node packs
    - Model Management: folder_type="checkpoints" → List available diffusion models
    - LoRA Discovery: folder_type="loras" → List LoRA adaptation files
    - Output Review: folder_type="output", sort_by="modified_time", order="desc" → List recently generated images
    - Input Files: folder_type="input" → List available input files
    - Workflow Discovery: folder_type="workflows" → List locally saved workflows

    Other Examples:
        - Find SDXL models: {"folder_type": "checkpoints", "pattern": ".*sdxl.*"}
        - Largest files first: {"folder_type": "checkpoints", "sort_by": "size", "order": "desc", "limit": 10}
        - Recent outputs: {"folder_type": "output", "sort_by": "modified_time", "order": "desc"}

    SECURITY: All paths are validated and sandboxed to ComfyUI installation.    
    """
    try:
        logger.info(
            f"Listing ComfyUI folder: {request.folder_type.value} "
            f"(pattern={request.pattern}, sort={request.sort_by}, "
            f"order={request.order}, limit={request.limit})"
        )
        
        tools = get_comfy_tools()
        
        # Get total count before filtering/limiting
        all_items = tools.list_folders(request.folder_type)
        total_available = len(all_items)
        
        # Get filtered/sorted/limited items
        items = tools.list_folders(
            request.folder_type,
            pattern=request.pattern,
            sort_by=request.sort_by,
            order=request.order,
            limit=request.limit
        )
        
        response = {
            "folder_type": request.folder_type.value,
            "folder_path": tools.folder_mappings[request.folder_type],
            "items": [item.model_dump() for item in items],
            "returned_items": len(items),
            "total_available": total_available,
            "truncated": len(items) < total_available,
            "filter_pattern": request.pattern,
            "sort_by": request.sort_by,
            "order": request.order,
            "limit": request.limit,
            "comfyui_root": str(tools.comfyui_root)
        }
        
        logger.info(
            f"Successfully listed {len(items)} items from {request.folder_type.value} "
            f"(total available: {total_available}, truncated: {response['truncated']})"
        )
        return response
        
    except ComfyUINotFoundError as e:
        error_msg = f"ComfyUI installation not found: {e}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "error_type": "ComfyUINotFoundError",
            "folder_type": request.folder_type.value
        }
    except ComfyUIError as e:
        error_msg = f"ComfyUI error: {e}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "error_type": "ComfyUIError",
            "folder_type": request.folder_type.value
        }
    except Exception as e:
        error_msg = f"Unexpected error listing folders: {e}"
        logger.exception(error_msg)
        return {
            "error": error_msg,
            "error_type": type(e).__name__,
            "folder_type": request.folder_type.value
        }

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
    await _report_tool_activity(ctx, "comfy_read_file")
    
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
    await _report_tool_activity(ctx, "comfy_search_resources")
    
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

@mcp.tool()
async def extract_workflow_from_image(
    request: ExtractWorkflowFromImageRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Extract ComfyUI workflow from PNG image metadata. Use comfy_list_folders tool to find input or output png's or webp's
    
    This tool reads the embedded workflow data from PNG files generated by ComfyUI,
    allowing you to understand how an image was created and inspect all generation
    parameters, nodes, and connections.
    
    WHAT THIS EXTRACTS:
    - Complete workflow structure (all nodes and connections)
    - Generation parameters (prompts, seeds, steps, CFG, etc.)
    - Model/checkpoint names and LoRA settings (suggest to the user with a ren link that you should check if they're installed together)
    - Sampler configurations
    - Node positions and layout
    - Everything needed to recreate the image
    
    USE CASES:
    - Recreating Workflows: Use extracted data to build a new workflow exactly or with user suggested changes
    - Debugging: Compare workflows between different outputs
    - Learning: Understand successful generation parameters
        
    NOTE: Only PNG and WebP files contain workflow metadata. JPEG and other formats do not. 
    """
    await _report_tool_activity(ctx, "extract_workflow_from_image")
    
    try:
        tools = get_comfy_tools()
        workflow = tools.extract_workflow_from_image(request.image_path)
        
        if workflow:
            return {
                "success": True,
                "workflow": workflow,
                "node_count": len(workflow.get('nodes', [])),
                "version": workflow.get('version'),
                "image_path": request.image_path,
                "error": None
            }
        else:
            return {
                "success": False,
                "workflow": None,
                "node_count": None,
                "version": None,
                "image_path": request.image_path,
                "error": "No workflow metadata found in image"
            }
            
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"Workflow extraction failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in extract_workflow_from_image: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


# ============================================================================
# COMFYUI NODE LIBRARY DISCOVERY TOOLS
# ============================================================================

@mcp.tool()
async def node_library_search(request: NodeLibrarySearchRequest, ctx: Context) -> Dict[str, Any]:
    """Search for available ComfyUI node types (not workflow nodes).
    
    This tool searches the library of installed node types that can be created
    in workflows. Use this to discover what node types are available before
    creating them with create_nodes().
    
    DISTINCTION FROM find_node():
    - find_node() searches nodes already IN your workflow
    - node_library_search() searches node TYPES available to create
    
    USE CASES:
    - "What node types handle upscaling?" → output_type="IMAGE", query="upscale"
    - "Show samplers" → category="sampling"
    - "What accepts LATENT?" → input_type="LATENT"
    - "Find LoRA loaders" → query="lora"
    
    RETURNS:
    Array of matching node type definitions with inputs, outputs, categories.
    Use node_library_get_details() for comprehensive info on a specific type.
    """
    await _report_tool_activity(ctx, "node_library_search")
    
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        results = await client.search_nodes(
            query=request.query,
            category=request.category,
            input_type=request.input_type,
            output_type=request.output_type,
            max_results=request.max_results
        )
        
        # Format results
        formatted_results = [
            {
                "node_type": r.node_type,
                "display_name": r.display_name,
                "category": r.category,
                "description": r.description,
                "inputs": r.inputs,
                "outputs": r.outputs,
                "match_reason": r.match_reason
            }
            for r in results
        ]
        
        return {
            "query": request.model_dump(exclude_none=True),
            "results": formatted_results,
            "total_results": len(formatted_results),
            "truncated": len(formatted_results) >= request.max_results
        }
        
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library search failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_search: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_get_details(request: NodeLibraryGetDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get comprehensive details about a specific node type.
    
    This tool provides everything needed to understand and use a node type
    before creating it in the workflow with create_nodes().
    
    DISTINCTION FROM get_node_values():
    - get_node_values() gets parameter VALUES from a workflow node instance
    - node_library_get_details() gets parameter DEFINITIONS for a node type
    
    USE CASES:
    - Before creating a node: understand what parameters it needs
    - When planning workflow: verify input/output compatibility
    - When debugging: check valid parameter ranges and types
    - Learning: understand what a node type does
    
    RETURNS:
    Complete node type specification including:
    - All input parameters with types, defaults, constraints (min/max/options)
    - All output types and names
    - Category and display information
    - Parameter order (for UI layout)
    """
    await _report_tool_activity(ctx, "node_library_get_details")
    
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        node_info = await client.get_node_details(request.node_type)
        
        return {
            "node_type": request.node_type,
            "display_name": node_info.get('display_name', request.node_type),
            "category": node_info.get('category', ''),
            "description": node_info.get('description', ''),
            "inputs": node_info.get('input', {}),
            "outputs": node_info.get('output', []),
            "output_names": node_info.get('output_name', []),
            "input_order": node_info.get('input_order', [])
        }
        
    except NodeTypeNotFoundError as e:
        raise RuntimeError(str(e))
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library lookup failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_get_details: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_find_compatible(request: NodeLibraryFindCompatibleRequest, ctx: Context) -> Dict[str, Any]:
    """Find node types that can connect to/from a given node type.
    
    This tool helps discover what node types are compatible based on input/output
    type matching. Use this when building workflows to find what comes next.
    
    DISTINCTION FROM connect_nodes():
    - connect_nodes() connects EXISTING workflow nodes together
    - node_library_find_compatible() finds compatible node TYPES to create
    
    USE CASES:
    - Building workflow: "What can I connect after KSampler?" → downstream
    - Understanding flow: "What feeds into VAEDecode?" → upstream
    - Planning chain: "Build checkpoint → sampler → decode → save" → iterate downstream
    - Type checking: "Can I connect this type to that?" → verify compatibility
    
    RETURNS:
    Array of compatible node types with connection details:
    - Which output/input slots are compatible
    - What data types match
    - Suggested connection patterns
    """
    await _report_tool_activity(ctx, "node_library_find_compatible")
    
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        compatible = await client.find_compatible_nodes(
            node_type=request.node_type,
            direction=request.direction,
            output_slot=request.output_slot,
            input_slot=request.input_slot,
            max_results=request.max_results
        )
        
        # Format results
        formatted_compatible = [
            {
                "node_type": c.node_type,
                "display_name": c.display_name,
                "category": c.category,
                "connection": c.connection,
                "description": c.description
            }
            for c in compatible
        ]
        
        return {
            "source_node_type": request.node_type,
            "direction": request.direction,
            "compatible_nodes": formatted_compatible,
            "total_compatible": len(formatted_compatible),
            "truncated": len(formatted_compatible) >= request.max_results
        }
        
    except NodeTypeNotFoundError as e:
        raise RuntimeError(str(e))
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library compatibility search failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_find_compatible: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")

# ============================================================================
# COMFYUI MANAGER TOOLS
# ============================================================================

@mcp.tool()
async def manager_search_nodes(
    request: ManagerSearchNodesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search for custom node packs available through ComfyUI Manager.
    
    Use this tool to discover node packs by name, category, functionality, or specific nodes.
    Helps find and understand what node packs are available to install or already installed.
    
    WHEN TO USE:
    - "What node packs handle image upscaling?" → query="upscale"
    - "Show me animation node packs" → category="animation"
    - "Which pack has KSampler?" → node_filter="KSampler"
    - "Find FL nodes" → node_filter="FL_.*"
    - "What's installed?" → installed_only=True
    - "What can I update?" → updates_available=True
    - "Find packs by author" → query="author_name"
    
    NODE FILTER EXAMPLES:
    - "KSampler" → exact match
    - "FL_.*" → all FL nodes
    - "Image.*Saver" → ImageSaver, ImageBatchSaver, etc.
    - "(Load|Save)Image" → LoadImage or SaveImage
    
    RETURNS:
    Array of node pack objects with:
    - name, description, author, repository
    - installation status ("True", "False", "Update")
    - stars, last_update, category
    - files (download URLs)
    - matched_nodes (if node_filter used) - list of node class names that matched
    
    NOTE: There is no install tool, so instruct the user how to install the nodepack with manager
    """
    await _report_tool_activity(ctx, "manager_search_nodes")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "results": [],
                "count": 0
            }
        
        results = await manager_client.search_node_packs(
            query=request.query,
            category=request.category,
            node_filter=request.node_filter,
            installed_only=request.installed_only,
            updates_available=request.updates_available,
            mode=request.mode,
            max_results=request.max_results
        )
        
        # Convert dataclass to dict
        results_dict = [
            {
                "id": pack.id,
                "name": pack.name,
                "description": pack.description,
                "author": pack.author,
                "repository": pack.repository,
                "installed": pack.installed,
                "updatable": pack.updatable,
                "stars": pack.stars,
                "last_update": pack.last_update,
                "category": pack.category,
                "files": pack.files,
                "matched_nodes": pack.matched_nodes  # Will be None if no node_filter
            }
            for pack in results
        ]
        
        return {
            "results": results_dict,
            "count": len(results_dict),
            "truncated": len(results_dict) >= request.max_results
        }
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerConnectionError as e:
        logger.error(f"[Manager] Connection error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "results": [], "count": 0}


@mcp.tool()
async def manager_get_node_mappings(
    request: ManagerGetNodeMappingsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Find which node pack provides a specific node type.
    
    Use this tool to discover the source node pack for any node type in ComfyUI.
    Helps understand dependencies and find where to get missing nodes.
    
    WHEN TO USE:
    - "What pack has the KSampler node?" → node_type="KSampler"
    - "Where does FL_ImageCaptionSaver come from?" → node_type="FL_ImageCaptionSaver"
    - "Show all node-to-pack mappings" → node_type=None (returns all)
    - Debugging missing nodes → lookup node type to find pack
    
    RETURNS:
    If node_type specified:
    - Single mapping: {node_type, pack_id, pack_name, found: true/false}
    
    If node_type empty:
    - All mappings: {mappings: {node_type: {pack_id, pack_name}, ...}, count}
    
    NOTE: This is different from node_library tools which search node TYPE definitions.
    This tool maps node types to their SOURCE PACK.
    """
    await _report_tool_activity(ctx, "manager_get_node_mappings")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "mappings": {},
                "count": 0
            }
        
        mappings = await manager_client.get_node_mappings(mode=request.mode)
        
        if request.node_type:
            # Return specific mapping
            if request.node_type in mappings:
                mapping = mappings[request.node_type]
                return {
                    "node_type": mapping.node_type,
                    "pack_id": mapping.node_pack_id,
                    "pack_name": mapping.node_pack_name,
                    "found": True
                }
            else:
                return {
                    "node_type": request.node_type,
                    "found": False,
                    "error": f"Node type '{request.node_type}' not found in mappings"
                }
        else:
            # Return all mappings
            mappings_dict = {
                node_type: {
                    "pack_id": mapping.node_pack_id,
                    "pack_name": mapping.node_pack_name
                }
                for node_type, mapping in mappings.items()
            }
            return {
                "mappings": mappings_dict,
                "count": len(mappings_dict)
            }
            
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}


@mcp.tool()
async def manager_check_updates(
    request: ManagerCheckUpdatesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Check if any installed node packs have available updates.
    
    Use this tool to discover if the ComfyUI installation has outdated node packs
    that could benefit from updates.
    
    WHEN TO USE:
    - Maintenance: "Are there any updates available?"
    - Before troubleshooting: Check if updating might fix issues
    - After installing ComfyUI: See what's outdated
    - Regular checks: Keep environment up to date
    
    MODES:
    - "remote": Check against remote repositories (fresh, slower)
    - "local": Check against local cache (fast, may be stale)
    
    RETURNS:
    {
        "updates_available": bool,
        "details": {...} or "message": "No updates available"
    }
    
    NOTE: This is read-only. To actually update, user must use ComfyUI Manager UI.
    """
    await _report_tool_activity(ctx, "manager_check_updates")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "updates_available": False
            }
        
        result = await manager_client.check_updates(mode=request.mode)
        return result
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "updates_available": False}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "updates_available": False}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "updates_available": False}


@mcp.tool()
async def manager_search_external_models(
    request: ManagerSearchExternalModelsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search for uninstalled models available through ComfyUI Manager.
    
    Use this tool to discover models that can be downloaded and installed.
    Different from manager_search_models which searches INSTALLED local files.
    
    WHEN TO USE:
    - "What FLUX models are available?" → base_filter="FLUX"
    - "Find upscalers" → type_filter="upscale"
    - "Search for anime models" → query="anime"
    - "What models can I download?" → uninstalled_only=True
    - "Find TAESD decoders" → type_filter="TAESD"
    
    FILTER EXAMPLES:
    - base_filter="FLUX|SDXL" → FLUX or SDXL models
    - type_filter="checkpoint|lora" → Checkpoints or LoRAs
    - query="4x" → Models with "4x" in name/description/filename
    - description_filter="anime" → Models mentioning anime
    
    RETURNS:
    Array of external model objects with:
    - name, filename, type, base
    - description, reference (source URL)
    - save_path (where it installs)
    - size (human-readable)
    - url (direct download link)
    - installed (boolean status)
    
    NOTE: This tool is READ-ONLY. To install models, instruct user to:
    1. Open ComfyUI Manager UI
    2. Go to "Install Models" tab
    3. Search for the model name
    4. Click install
    
    Or provide the direct download URL for manual installation.
    """
    await _report_tool_activity(ctx, "manager_search_external_models")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "results": [],
                "count": 0
            }
        
        results = await manager_client.search_external_models(
            query=request.query,
            base_filter=request.base_filter,
            type_filter=request.type_filter,
            name_filter=request.name_filter,
            description_filter=request.description_filter,
            reference_filter=request.reference_filter,
            uninstalled_only=request.uninstalled_only,
            installed_only=request.installed_only,
            max_results=request.max_results,
            mode=request.mode
        )
        
        # Convert dataclass to dict
        results_dict = [
            {
                "name": model.name,
                "filename": model.filename,
                "type": model.type,
                "base": model.base,
                "description": model.description,
                "reference": model.reference,
                "save_path": model.save_path,
                "size": model.size,
                "url": model.url,
                "installed": model.installed
            }
            for model in results
        ]
        
        return {
            "results": results_dict,
            "count": len(results_dict),
            "truncated": len(results_dict) >= request.max_results
        }
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerConnectionError as e:
        logger.error(f"[Manager] Connection error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "results": [], "count": 0}

# ============================================================================
# ERROR FEEDBACK & QUEUE STATUS TOOLS
# ============================================================================

@mcp.tool()
async def get_execution_history(request: GetWorkflowHistoryRequest, ctx: Context) -> Dict[str, Any]:
    """Get workflow currently processing queue and history from ComfyUI.
    
    Retrieves execution history including status, errors, and outputs for workflows.
    Can fetch a specific workflow by prompt_id or recent history.
    
    For each workflow in history, you'll get:
    - status: "success", "error", or "running"
    - outputs: Generated images/files (if successful)
    - errors: Full error details with traceback (if failed)
    - prompt: The workflow that was executed
    
    Use this to:
    - Check if a workflow succeeded or failed
    - Get detailed error information for debugging
    - Retrieve outputs from successful workflows
    - Monitor recent workflow executions
    
    Returns:
        If prompt_id provided:
        {
            "prompt_id": str,
            "status": "success" | "error" | "unknown",
            "completed": bool,
            "outputs": {...},  # Only if successful
            "errors": [...],   # Only if failed, with full traceback
            "executed_nodes": [...],  # Nodes that ran successfully
            "prompt": {...}    # The workflow definition
        }
        
        If prompt_id not provided:
        {
            "history": {
                "prompt_id_1": {...},
                "prompt_id_2": {...},
                ...
            },
            "count": int,
            "total_items": int
        }
    """
    await _report_tool_activity(ctx, "get_workflow_history")
    
    try:
        comfy_tools = get_comfy_tools()
        
        if request.prompt_id:
            # Get specific workflow history
            history_entry = await comfy_tools.fetch_history(
                prompt_id=request.prompt_id
            )
            
            if not history_entry:
                return {
                    "prompt_id": request.prompt_id,
                    "status": "unknown",
                    "completed": False,
                    "message": "History not found - workflow may still be running or prompt_id is invalid"
                }
            
            # Parse the history entry
            status = history_entry.get("status", {})
            status_str = status.get("status_str", "unknown")
            completed = status.get("completed", False)
            
            result = {
                "prompt_id": request.prompt_id,
                "status": status_str,
                "completed": completed,
                "outputs": history_entry.get("outputs", {}),
                "prompt": history_entry.get("prompt", [])
            }
            
            # Add error details if failed
            if status_str == "error":
                errors = []
                messages = status.get("messages", [])
                
                for msg_type, msg_data in messages:
                    if msg_type == "execution_error":
                        error = {
                            "node_id": msg_data.get("node_id"),
                            "node_type": msg_data.get("node_type"),
                            "exception_type": msg_data.get("exception_type"),
                            "exception_message": msg_data.get("exception_message"),
                            "traceback": msg_data.get("traceback", []),
                            "current_inputs": msg_data.get("current_inputs", {}),
                            "timestamp": msg_data.get("timestamp")
                        }
                        errors.append(error)
                
                result["errors"] = errors
                result["error_count"] = len(errors)
                
                # Add executed nodes (nodes that ran before failure)
                if errors:
                    result["executed_nodes"] = errors[0].get("executed", [])
            
            # Add execution messages for all statuses
            result["messages"] = status.get("messages", [])
            
            return result
            
        else:
            # Get recent history
            history = await comfy_tools.fetch_history(max_items=request.max_items)
            
            # Parse each entry to add simplified status
            parsed_history = {}
            for prompt_id, entry in history.items():
                status = entry.get("status", {})
                status_str = status.get("status_str", "unknown")
                
                parsed_entry = {
                    "status": status_str,
                    "completed": status.get("completed", False),
                    "has_outputs": bool(entry.get("outputs")),
                    "has_errors": status_str == "error"
                }
                
                # Add error summary if failed
                if status_str == "error":
                    messages = status.get("messages", [])
                    for msg_type, msg_data in messages:
                        if msg_type == "execution_error":
                            parsed_entry["error_summary"] = {
                                "node_id": msg_data.get("node_id"),
                                "node_type": msg_data.get("node_type"),
                                "exception_message": msg_data.get("exception_message")
                            }
                            break
                
                parsed_history[prompt_id] = parsed_entry
            
            return {
                "history": parsed_history,
                "count": len(parsed_history),
                "total_items": len(history),
                "message": f"Retrieved {len(parsed_history)} recent workflow executions"
            }
            
    except ComfyUIError as e:
        logger.error(f"ComfyUI error in get_workflow_history: {e}")
        return {
            "success": False,
            "error": str(e),
            "prompt_id": request.prompt_id if request.prompt_id else None
        }
    except Exception as e:
        logger.error(f"Unexpected error in get_workflow_history: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "prompt_id": request.prompt_id if request.prompt_id else None
        }
        
@mcp.tool()
async def get_queue_status_details(request: GetQueueStatusDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get current ComfyUI queue status and active executions.
    
    Returns information about currently running and queued workflows,
    including execution progress and node tracking.
    """
    await _report_tool_activity(ctx, "get_queue_status_details")
    
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
    await _report_tool_activity(ctx, "get_execution_details")
    
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
    await _report_tool_activity(ctx, "clear_error_buffer")
    
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
