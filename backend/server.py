"""FastAPI server with WebSocket endpoint for FL_JS Agentic System."""

import asyncio
import base64
import copy
import logging
from contextlib import asynccontextmanager
import traceback
from typing import Any, Dict, List, Optional, Set
import sys
import os
from pathlib import Path

# Add parent directory to path to allow 'backend' imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic_ai import Agent, UnexpectedModelBehavior

from config import settings
from manager import manager
from callback_router import CallbackRouter, current_session_id
from agent import agent_manager
from models import (
    Handshake,
    UserMessage,
    ToolResult,
)

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
        logging.FileHandler(log_file, mode="a", encoding="utf-8")  # File output
    ],
)

logger = logging.getLogger("ren_server")
logger.info(f"Logger initialized with level: {log_level_name}")


# Global callback router instance
callback_router: CallbackRouter | None = None

# Background tasks
async def cleanup_task() -> None:
    """Background task to cleanup stale sessions."""
    while True:
        await asyncio.sleep(60)  # Every minute
        cleaned = manager.cleanup_stale_sessions()
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale sessions")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore
    """Application lifespan manager."""
    global callback_router
    
    # Startup
    logger.info("Starting FL_JS backend server")
    logger.info(f"LLM Provider: {settings.llm_provider}")
    logger.info(f"LLM Model: {settings.resolved_model}")
    logger.info(f"Provider Tuning: {settings.provider_tuning}")
    
    # Initialize callback router
    callback_router = CallbackRouter(manager, default_timeout=30.0)
    logger.info("Callback router initialized")
    
    # Initialize MCP server with callback router
    from mcp_server import set_callback_router
    set_callback_router(callback_router)
    logger.info("MCP server configured with callback router")
    
    # Start background tasks
    cleanup_task_handle = asyncio.create_task(cleanup_task())
    
    yield
    
    # Shutdown
    logger.info("Shutting down FL_JS backend server")
    cleanup_task_handle.cancel()
    try:
        await cleanup_task_handle
    except asyncio.CancelledError:
        pass


# Create FastAPI app
app = FastAPI(
    title="FL_JS Agentic System",
    description="AI-powered ComfyUI workflow assistant",
    version="0.3.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get project root and directories
PROJECT_ROOT = Path(__file__).parent.parent
PWA_DIR = PROJECT_ROOT / "web" / "pwa"
WEB_JS_DIR = PROJECT_ROOT / "web" / "js"

# Serve PWA static files
app.mount("/pwa/static", StaticFiles(directory=str(PWA_DIR)), name="pwa_static")

# Serve shared JavaScript modules
app.mount("/js", StaticFiles(directory=str(WEB_JS_DIR)), name="shared_js")


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "FL_JS Agentic System",
        "version": "0.3.0",
        "status": "running",
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "active_connections": manager.get_active_session_count(),
        "total_sessions": manager.get_total_session_count(),
        "pending_callbacks": callback_router.get_pending_count() if callback_router else 0,
        "active_agents": agent_manager.get_agent_count(),
    }


@app.get("/api/config")
async def get_client_config() -> dict[str, Any]:
    """Return client configuration including WebSocket URL.
    
    This endpoint allows the frontend to dynamically discover the WebSocket URL
    based on the actual port the backend is running on, enabling users to set
    custom ports via WS_PORT in .env.
    
    Returns:
        Dict with ws_url and other client configuration
    """
    # Build WebSocket URL based on current server configuration
    # Use settings.ngrok_url if available (production mode)
    if settings.ngrok_url:
        ws_url = settings.ngrok_url.replace("https://", "wss://").replace("http://", "ws://")
        if not ws_url.endswith("/ws"):
            ws_url = f"{ws_url}/ws"
    else:
        # Local mode - use configured host and port
        ws_url = f"ws://{settings.ws_host}:{settings.ws_port}/ws"
    
    return {
        "ws_url": ws_url,
        "version": "0.3.0",
        "ngrok_mode": bool(settings.ngrok_url),
    }


@app.get("/pwa")
@app.get("/pwa/")
async def serve_pwa() -> FileResponse:
    """Serve PWA application."""
    return FileResponse(str(PWA_DIR / "index.html"))


@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    """List active sessions for PWA session picker.
    
    Returns:
        Dict with sessions list containing session_id, connections, and last_activity
    """
    sessions = []
    for session_id, context in manager.session_contexts.items():
        sessions.append({
            "session_id": session_id,
            "connections": manager.get_connection_info(session_id),
            "last_activity": context.last_activity.isoformat(),
            "has_frontend": manager.has_connection(session_id, 'frontend'),
            "has_pwa": manager.has_connection(session_id, 'pwa'),
        })
    
    return {
        "sessions": sessions,
        "total": len(sessions),
    }


@app.get("/api/view")
async def view_image(
    filename: str,
    subfolder: str = "",
    type: str = "output",
    rand: float = 0.0  # Cache busting
) -> FileResponse:
    """Serve ComfyUI images to all clients (frontend and PWA).
    
    This endpoint proxies ComfyUI's image serving, making it accessible
    to both embedded frontend and standalone PWA clients.
    
    Args:
        filename: Image filename
        subfolder: Optional subfolder path
        type: Image type ('output', 'input', 'temp')
        rand: Cache busting parameter
        
    Returns:
        FileResponse with appropriate media type
        
    Raises:
        HTTPException: 400 for invalid type, 403 for security violation, 404 for not found
    """
    try:
        # Import ComfyUI tools
        from comfy_tools import get_comfy_tools
        comfy_tools = get_comfy_tools()
        
        # Validate type
        if type not in ["output", "input", "temp"]:
            raise HTTPException(status_code=400, detail=f"Invalid type: {type}")
        
        # Build path based on type
        if type == "output":
            base_path = comfy_tools.comfyui_root / "output"
        elif type == "input":
            base_path = comfy_tools.comfyui_root / "input"
        elif type == "temp":
            base_path = comfy_tools.comfyui_root / "temp"
        
        # Handle subfolder
        if subfolder:
            file_path = base_path / subfolder / filename
        else:
            file_path = base_path / filename
        
        # Validate path is within allowed directory (security)
        file_path = file_path.resolve()
        base_path_resolved = base_path.resolve()
        if not str(file_path).startswith(str(base_path_resolved)):
            logger.warning(f"Path traversal attempt blocked: {file_path}")
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Check file exists
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {filename}")
        
        # Determine media type
        media_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        media_type = media_types.get(file_path.suffix.lower(), "application/octet-stream")
        
        # Return file
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            headers={
                "Cache-Control": "no-cache",  # Respect rand parameter
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for client connections.

    Protocol:
        1. Client connects
        2. Client sends handshake with session_id
        3. Server registers session and sends handshake_ack
        4. Bidirectional message exchange
        5. Client disconnects (session kept alive for reconnection)

    Args:
        websocket: WebSocket connection
    """
    session_id: str | None = None
    connection_type: str = 'frontend'  # Default to frontend
    agent: Agent | None = None
    
    try:
        # Accept connection
        await websocket.accept()
        logger.info("WebSocket connection accepted, waiting for handshake")
        
        # Wait for handshake
        handshake_data = await websocket.receive_json()
        
        # Validate handshake
        if handshake_data.get("type") != "handshake":
            await websocket.send_json({
                "type": "error",
                "error_code": "INVALID_HANDSHAKE",
                "message": "First message must be handshake",
            })
            await websocket.close()
            return
        
        # Parse handshake
        try:
            handshake = Handshake(**handshake_data)
            session_id = handshake.session_id
            
            # Detect connection type from client_version
            if handshake.client_version:
                version_lower = handshake.client_version.lower()
                if 'mcp' in version_lower:
                    connection_type = 'mcp'
                elif 'pwa' in version_lower:
                    connection_type = 'pwa'
                else:
                    connection_type = 'frontend'
            else:
                connection_type = 'frontend'
            
            logger.info(f"Detected connection type: {connection_type}")
            
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "error_code": "INVALID_HANDSHAKE_DATA",
                "message": f"Invalid handshake data: {str(e)}",
            })
            await websocket.close()
            return
        
        if not session_id:
            await websocket.send_json({
                "type": "error",
                "error_code": "MISSING_SESSION_ID",
                "message": "session_id is required in handshake",
            })
            await websocket.close()
            return
        
        # Register connection with type
        is_reconnect = manager.has_connection(session_id, connection_type)
        context = await manager.connect(websocket, session_id, connection_type)
        
        # Send handshake acknowledgment
        await manager.send_handshake_ack(session_id, is_reconnect, connection_type)
        
        logger.info(
            f"Session {session_id} - {connection_type} "
            f"{'reconnected' if is_reconnect else 'connected'}"
        )
        
        # Get or create agent for frontend connections
        # if connection_type == 'frontend':
        agent = agent_manager.get_agent(session_id)
        logger.info(f"Agent created/retrieved for session {session_id}")
        
        # Start MCP servers for frontend connections and wrap message loop
        async with agent.run_mcp_servers(): # if agent else asynccontextmanager(lambda: (yield))():
            if agent:
                logger.info(f"MCP servers started for session {session_id}")
            
            # Message loop
            while True:
                # 🔍 TRACE: Log before receive
                logger.info(f"[TRACE] Waiting for message on session {session_id} ({connection_type})")
                
                data = await websocket.receive_json()
                
                # 🔍 TRACE: Log what we received
                logger.info(f"[TRACE] Received message on session {session_id} ({connection_type}): type={data.get('type')}")
                
                # Get message type and session_id for logging
                msg_type = data.get("type")
                msg_session_id = data.get("session_id")
                
                # Log all incoming messages with session_id info (changed from DEBUG to INFO)
                logger.info(
                    f"[VALIDATION] Received {msg_type} | "
                    f"msg_session_id={msg_session_id} | "
                    f"connection_session_id={session_id} | "
                    f"connection_type={connection_type}"
                )
                
                # Validate session_id in message
                if msg_session_id != session_id:
                    logger.warning(
                        f"[VALIDATION] Session mismatch! "
                        f"msg_session_id={msg_session_id} != connection_session_id={session_id} | "
                        f"msg_type={msg_type}"
                    )
                    await manager.send_error(
                        session_id,
                        "SESSION_MISMATCH",
                        f"Message session_id '{msg_session_id}' does not match connection session_id '{session_id}'",
                        target=connection_type
                    )
                    continue
                
                # Route message based on type
                if msg_type == "user_message":
                    if agent:
                        asyncio.create_task(
                            handle_user_message(
                                session_id, 
                                data, 
                                message_history=context.conversation_history,
                                agent=agent
                            )
                        )
                    else:
                        logger.warning(f"Received user_message on MCP connection for session {session_id}")
                
                elif msg_type == "tool_result":
                    await handle_tool_result(session_id, data)
                
                elif msg_type == "tool_request":
                    # Tool request from MCP subprocess - route to display clients
                    await route_tool_request_to_frontend(session_id, data)
                                
                elif msg_type == "tool_report":
                    # Tool activity report from MCP subprocess - route to display clients
                    await route_tool_report_to_frontend(session_id, data)
                
                elif msg_type == "screenshot":
                    # Screenshot data from frontend
                    await handle_screenshot(session_id, data)

                elif msg_type == "comfy_error":
                    await manager.handle_comfy_error(data.get("data", {}))
                
                elif msg_type == "queue_status":
                    await manager.handle_queue_status(data.get("data", {}))
                
                elif msg_type == "execution_event":
                    event = data.get("event")
                    logger.debug(f"**execution_event**: {data}")
                    event_data = data.get("data", {})
                    await manager.handle_execution_event(event, event_data)
                
                else:
                    await manager.send_error(
                        session_id,
                        "UNKNOWN_MESSAGE_TYPE",
                        f"Unknown message type: {msg_type}",
                        target=connection_type
                    )
    
    except WebSocketDisconnect:
        if session_id:
            manager.disconnect(session_id, connection_type)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            # Cleanup agent for frontend connections
            if connection_type == 'frontend':
                agent_manager.remove_agent(session_id)
                logger.info(f"Agent removed for session {session_id}")
            logger.info(f"Session {session_id} - {connection_type} disconnected")
    
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e}", exc_info=True)
        if session_id:
            manager.disconnect(session_id, connection_type)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            # Cleanup agent for frontend connections
            if connection_type == 'frontend':
                agent_manager.remove_agent(session_id)
                logger.info(f"Agent removed for session {session_id} (error cleanup)")
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


# Function to filter message history
from pydantic_ai.messages import ModelMessage, SystemPromptPart, UserPromptPart, TextPart, ToolCallPart, ToolReturnPart, RetryPromptPart
from pydantic_ai.agent import AgentRunResult

# Helper function to truncate text with marker
def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to max_chars length with a truncation marker."""
    if not text or len(text) <= max_chars:
        return text
    
    truncation_marker = "...<truncated>"
    return text[:max_chars - len(truncation_marker)] + truncation_marker

# Helper function to recursively truncate strings in a nested structure
def truncate_nested_strings(obj: Any, max_chars: int) -> Any:
    """Recursively truncate all string values in a nested structure."""
    if isinstance(obj, str):
        return truncate_text(obj, max_chars)
    elif isinstance(obj, dict):
        return {k: truncate_nested_strings(v, max_chars) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [truncate_nested_strings(item, max_chars) for item in obj]
    else:
        return obj

def filtered_message_history(
    messages: List[ModelMessage], 
    limit: Optional[int] = None, 
    include_tool_messages: bool = False,
    max_chars: Optional[int] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Filter and limit the message history from an AgentRunResult.
    
    Args:
        messages: The message history
        limit: Optional int, if provided returns only system message + last N messages.
               If None, uses provider-specific default from settings.
        include_tool_messages: Whether to include tool messages in the history
        max_chars: Maximum number of characters for string values in tool calls and returns.
                  If None, uses provider-specific default from settings.
        
    Returns:
        Filtered list of messages in the format expected by the agent
    """
    # Use provider-specific defaults if not specified
    if limit is None:
        limit = settings.provider_tuning["history_limit"]
    if max_chars is None:
        max_chars = settings.provider_tuning["max_chars"]
    
    # Extract system message (always the first one with role="system")
    system_message = next((msg for msg in messages if len(msg.parts) > 0 and type(msg.parts[0]) == SystemPromptPart), None)
    
    # Filter non-system messages
    non_system_messages = [msg for msg in messages if len(msg.parts) > 0 and  type(msg.parts[0]) != SystemPromptPart]
    
    # Apply tool message filtering if requested
    if not include_tool_messages:
        non_system_messages = [msg for msg in non_system_messages if not any(isinstance(part, ToolCallPart) or isinstance(part, ToolReturnPart) for part in msg.parts)]
    
    # Find the most recent UserPromptPart before applying limit
    latest_user_prompt_part = None
    latest_user_prompt_index = -1
    for i, msg in enumerate(non_system_messages):
        for part in msg.parts:
            if isinstance(part, UserPromptPart):
                latest_user_prompt_part = part
                latest_user_prompt_index = i
    
    # Apply limit if specified, but ensure paired tool calls and returns stay together
    if limit is not None and limit > 0:
        # Build comprehensive mapping of tool calls and returns
        tool_call_to_msg = {}  # tool_call_id -> message_index
        tool_return_to_msg = {}  # tool_call_id -> message_index
        
        for i, msg in enumerate(non_system_messages):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    tool_call_to_msg[part.tool_call_id] = i
                elif isinstance(part, ToolReturnPart):
                    tool_return_to_msg[part.tool_call_id] = i
                elif isinstance(part, RetryPromptPart):
                    # RetryPromptPart acts like a ToolReturnPart for mapping purposes
                    tool_return_to_msg[part.tool_call_id] = i
        
        # Take the last 'limit' messages but ensure we include paired messages
        if len(non_system_messages) > limit:
            # Start with the basic window
            included_indices = set(range(len(non_system_messages) - limit, len(non_system_messages)))
            
            # BIDIRECTIONAL pairing: ensure both calls and returns are included together
            # We iterate until no new indices are added to guarantee completeness
            changes_made = True
            iteration_count = 0
            max_iterations = len(non_system_messages)  # Safety limit
            
            while changes_made and iteration_count < max_iterations:
                changes_made = False
                original_size = len(included_indices)
                iteration_count += 1
                
                # Create a copy to iterate over (avoid modifying set during iteration)
                current_indices = set(included_indices)
                
                for msg_idx in current_indices:
                    if msg_idx >= len(non_system_messages):  # Safety check
                        continue
                        
                    msg = non_system_messages[msg_idx]
                    for part in msg.parts:
                        if isinstance(part, ToolCallPart):
                            # If we have a tool call, ensure its return is included
                            if part.tool_call_id in tool_return_to_msg:
                                return_msg_idx = tool_return_to_msg[part.tool_call_id]
                                if return_msg_idx not in included_indices:
                                    included_indices.add(return_msg_idx)
                                    
                        elif isinstance(part, ToolReturnPart):
                            # If we have a tool return, ensure its call is included
                            if part.tool_call_id in tool_call_to_msg:
                                call_msg_idx = tool_call_to_msg[part.tool_call_id]
                                if call_msg_idx not in included_indices:
                                    included_indices.add(call_msg_idx)
                        elif isinstance(part, RetryPromptPart):
                            # If we have a retry prompt, ensure its call is included
                            if part.tool_call_id in tool_call_to_msg:
                                call_msg_idx = tool_call_to_msg[part.tool_call_id]
                                if call_msg_idx not in included_indices:
                                    included_indices.add(call_msg_idx)
                
                # Check if we added any new indices
                if len(included_indices) > original_size:
                    changes_made = True
            
            # Safety check: if we hit max iterations, log a warning
            if iteration_count >= max_iterations:
                logger.warning(f"Tool pairing reached max iterations ({max_iterations}). Some tool calls/returns may be unpaired.")
            
            # VALIDATION STEP: After the bidirectional pairing loop, validate completeness
            # Remove any orphaned tool calls or returns from the included messages
            validated_indices: Set[int] = set()
            orphaned_calls: Set[str] = set()
            orphaned_returns: Set[str] = set()
            
            for msg_idx in sorted(included_indices):
                if msg_idx >= len(non_system_messages):  # Safety check
                    continue
                    
                msg = non_system_messages[msg_idx]
                has_orphaned_parts = False
                
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        # Check if return exists in included messages
                        if part.tool_call_id in tool_return_to_msg:
                            return_idx = tool_return_to_msg[part.tool_call_id]
                            if return_idx not in included_indices:
                                has_orphaned_parts = True
                                orphaned_calls.add(part.tool_call_id)
                        else:
                            # If we don't have a return at all for this call
                            has_orphaned_parts = True
                            orphaned_calls.add(part.tool_call_id)
                            
                    elif isinstance(part, ToolReturnPart) or isinstance(part, RetryPromptPart):
                        # Check if call exists in included messages
                        if part.tool_call_id in tool_call_to_msg:
                            call_idx = tool_call_to_msg[part.tool_call_id]
                            if call_idx not in included_indices:
                                has_orphaned_parts = True
                                orphaned_returns.add(part.tool_call_id)
                        else:
                            # If we don't have a call at all for this return
                            has_orphaned_parts = True
                            orphaned_returns.add(part.tool_call_id)
                
                # Only include message if no orphaned parts
                if not has_orphaned_parts:
                    validated_indices.add(msg_idx)
            
            # Log any orphaned tool calls for debugging
            if orphaned_calls or orphaned_returns:
                logger.warning(f"Excluded messages with orphaned tool calls: {orphaned_calls}, returns: {orphaned_returns}")
            
            # Use validated indices instead of included_indices
            # IMPORTANT: Sort indices to maintain chronological order
            sorted_indices = sorted(validated_indices)
            non_system_messages = [non_system_messages[i] for i in sorted_indices]
            
            # Handle the latest user prompt preservation logic
            if (latest_user_prompt_index >= 0 and 
                latest_user_prompt_index not in validated_indices and 
                latest_user_prompt_part is not None and 
                system_message is not None):
                # Find if system_message already has a UserPromptPart
                user_prompt_index = next((i for i, part in enumerate(system_message.parts) 
                                       if isinstance(part, UserPromptPart)), None)
                
                if user_prompt_index is not None:
                    # Replace existing UserPromptPart
                    system_message.parts[user_prompt_index] = latest_user_prompt_part
                else:
                    # Add new UserPromptPart to system message
                    system_message.parts.append(latest_user_prompt_part)
    
    # Apply string truncation to tool calls and returns
    if max_chars > 0:
        # Create deep copies to avoid modifying original messages
        truncated_system_message = copy.deepcopy(system_message) if system_message else None
        truncated_non_system_messages = [copy.deepcopy(msg) for msg in non_system_messages]
        
        # Process system message if it exists
        if truncated_system_message:
            for part in truncated_system_message.parts:
                # We don't truncate system or user prompt parts to preserve core instructions
                if isinstance(part, ToolCallPart):
                    # Truncate parameters in tool calls
                    if hasattr(part, 'parameters') and part.parameters:
                        part.parameters = truncate_nested_strings(part.parameters, max_chars)
                elif isinstance(part, ToolReturnPart):
                    # Truncate content in tool returns
                    if hasattr(part, 'content') and part.content:
                        part.content = truncate_nested_strings(part.content, max_chars)
        
        # Process non-system messages
        for msg in truncated_non_system_messages:
            for part in msg.parts:
                if isinstance(part, TextPart):
                    # Truncate text content (usually assistant or user regular messages)
                    if hasattr(part, 'text') and part.text:
                        part.text = truncate_text(part.text, max_chars)
                elif isinstance(part, ToolCallPart):
                    # Truncate parameters in tool calls
                    if hasattr(part, 'parameters') and part.parameters:
                        part.parameters = truncate_nested_strings(part.parameters, max_chars)
                elif isinstance(part, ToolReturnPart):
                    # Truncate content in tool returns
                    if hasattr(part, 'content') and part.content:
                        part.content = truncate_nested_strings(part.content, max_chars)
                elif isinstance(part, RetryPromptPart):
                    # Also truncate retry prompts content
                    if hasattr(part, 'content') and part.content:
                        part.content = truncate_nested_strings(part.content, max_chars)
        
        # Use the truncated messages for result
        system_message = truncated_system_message
        non_system_messages = truncated_non_system_messages
    
    # Combine system message with other messages
    result_messages = []
    if system_message:
        result_messages.append(system_message)
    result_messages.extend(non_system_messages)
        
    return result_messages


# Message handlers

async def handle_user_message(
    session_id: str, 
    data: dict[str, Any], 
    message_history: List[ModelMessage],
    agent: Agent
) -> None:
    """Handle user message.

    Args:
        session_id: Session ID
        data: Message data
        message_history: Conversation history for this session
        agent: Agent instance for this session
    """
    try:
        message = UserMessage(**data)
        logger.info(f"User message from {session_id}: {message.message[:50]}...")
        
        # Set session context for tool callbacks
        current_session_id.set(session_id)
        
        # Send typing indicator
        await manager.send_message(session_id, {
            "type": "typing_indicator",
            "session_id": session_id,
            "is_typing": True,
        })
        
        # Process message with agent with retry logic for flaky providers
        max_retries = 2
        last_error = None
        response = None
        
        for attempt in range(max_retries + 1):
            try:
                response = await agent.run(
                    message.message, 
                    message_history=filtered_message_history(
                        message_history,
                        include_tool_messages=True
                        # limit and max_chars will use provider-specific defaults
                    )
                )
                break  # Success - exit retry loop
                
            except UnexpectedModelBehavior as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning(
                        f"Model generation failed (attempt {attempt + 1}/{max_retries + 1}): "
                        f"{str(e)[:100]}... Retrying..."
                    )
                    continue
                else:
                    # Final attempt failed
                    logger.error(f"Model generation failed after {max_retries + 1} attempts")
                    raise last_error
        
        if response is not None:
            # Determine which connections should receive the response
            # PWA and frontend should both see agent responses
            response_targets = []
            if manager.has_connection(session_id, 'pwa'):
                response_targets.append('pwa')
            if manager.has_connection(session_id, 'frontend'):
                response_targets.append('frontend')
            
            # Set History (Mutable)
            message_history.clear()
            message_history.extend(response.all_messages())
            
            # Send response to all connected clients
            for target in response_targets:
                await manager.send_message(session_id, {
                    "type": "agent_response",
                    "session_id": session_id,
                    "message": response.output,
                    "tool_calls": [], # TODO set this to use the proper message parts
                    "is_final": True,
                }, target=target)
            
            logger.info(f"Agent response sent to {session_id} (targets: {response_targets})")
            
    except UnexpectedModelBehavior as ue:
        # Happens mainly when a model can't get a tool call right after so many tries

        # Extract root cause error
        root_cause = ue.__cause__ if ue.__cause__ else ue
        root_cause_msg = str(root_cause)
        
        trback = traceback.format_exception(type(root_cause), root_cause, root_cause.__traceback__)

        logger.critical(f"Critical Tool Error: {root_cause_msg}\n\nTraceback:\n{trback}")
    
    except Exception as e:
        logger.error(f"Error handling user message: {e}", exc_info=True)
        await manager.send_error(
            session_id,
            "MESSAGE_PROCESSING_ERROR",
            "Failed to process message",
            {"error": str(e)},
        )
    finally:
        # Clear session context
        current_session_id.set(None)


async def handle_tool_result(session_id: str, data: dict[str, Any]) -> None:
    """Handle tool execution result from frontend.
    
    This result needs to be routed back to the MCP subprocess that requested it.

    Args:
        session_id: Session ID
        data: Tool result data
    """
    try:
        result = ToolResult(**data)
        logger.info(
            f"Tool result from {session_id}: request_id={result.request_id} "
            f"({'success' if result.success else 'failed'})"
        )
        
        # Route result back to MCP subprocess
        if manager.has_connection(session_id, 'mcp'):
            await manager.send_message(session_id, data, target='mcp')
            logger.info(f"Tool result routed to MCP subprocess for session {session_id}")
        else:
            logger.warning(
                f"No MCP connection for session {session_id}, cannot route tool result"
            )
        
        # Also route to callback router (legacy support)
        if callback_router:
            await callback_router.handle_tool_result(
                request_id=result.request_id,
                success=result.success,
                data=result.data,
                error=result.error,
                execution_time_ms=result.execution_time_ms
            )
    
    except Exception as e:
        logger.error(f"Error handling tool result: {e}", exc_info=True)
        await manager.send_error(
            session_id,
            "TOOL_RESULT_ERROR",
            "Failed to process tool result",
            {"error": str(e)},
        )


async def handle_screenshot(session_id: str, data: dict) -> None:
    """Handle screenshot data from frontend.
    
    Receives base64-encoded screenshot, decodes it, and saves to
    output/screenshots/ directory.
    
    Args:
        session_id: Session ID
        data: Screenshot message data
    """
    try:
        from models import ScreenshotMessage
        from comfy_tools import get_comfy_tools
        
        screenshot_msg = ScreenshotMessage(**data)
        logger.info(
            f"Screenshot from {session_id}: {screenshot_msg.screenshot_id} "
            f"({screenshot_msg.format}, {screenshot_msg.size_bytes} bytes)"
        )
        
        # Get ComfyUI output directory
        comfy_tools = get_comfy_tools()
        screenshots_dir = comfy_tools.comfyui_root / "output" / "screenshots"
        
        # Create screenshots directory if it doesn't exist
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        
        # Decode base64 data
        # Format: "data:image/jpeg;base64,/9j/4AAQ..."
        if ";base64," in screenshot_msg.base64_data:
            base64_str = screenshot_msg.base64_data.split(";base64,")[1]
        else:
            base64_str = screenshot_msg.base64_data
        
        image_data = base64.b64decode(base64_str)
        
        # Determine file extension
        ext = "jpg" if screenshot_msg.format == "jpeg" else "png"
        filename = f"{screenshot_msg.screenshot_id}.{ext}"
        file_path = screenshots_dir / filename
        
        # Save to file
        with open(file_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"Screenshot saved: {file_path}")
        
        # Send confirmation back to frontend (optional)
        await manager.send_message(session_id, {
            "type": "screenshot_saved",
            "session_id": session_id,
            "screenshot_id": screenshot_msg.screenshot_id,
            "filename": filename,
            "path": str(file_path),
        }, target='frontend')
        
    except Exception as e:
        logger.error(f"Error handling screenshot: {e}", exc_info=True)
        await manager.send_error(
            session_id,
            "SCREENSHOT_ERROR",
            "Failed to save screenshot",
            {"error": str(e)},
        )


async def route_tool_request_to_frontend(session_id: str, data: dict) -> None:
    """Route tool request from MCP subprocess to all display clients.
    
    Broadcasts to both frontend (which can execute) and PWA (which displays only).
    Only frontend is expected to send back tool_result.
    
    Args:
        session_id: Session ID
        data: Tool request data
    """
    try:
        logger.info(
            f"Routing tool request to display clients: session={session_id}, "
            f"tool={data.get('tool_name')}, request_id={data.get('request_id')}"
        )
        
        # Check if ANY display client is connected
        has_frontend = manager.has_connection(session_id, 'frontend')
        has_pwa = manager.has_connection(session_id, 'pwa')
        
        if not has_frontend and not has_pwa:
            error_msg = f"No display clients connected for session {session_id}.\n\n**What to do**\n- Ask the user to make sure they have ComfyUI open in their browser\- If they do not see your reply in the ComfyUI Ren side drawer it means they are connected to the wrong session.\n- Tell them if they are on the Ren Go app they can refresh the page to choose a session"
            logger.error(error_msg)
            
            # Send error back to MCP subprocess
            await manager.send_message(session_id, {
                'type': 'tool_result',
                'session_id': session_id,
                'request_id': data.get('request_id'),
                'success': False,
                'error': error_msg,
                'execution_time_ms': 0,
            }, target='mcp')
            return
        
        # Broadcast to all connected display clients
        if has_frontend:
            result = await manager.send_message(session_id, data, target='frontend')
            if result:
                logger.info(f"Tool request forwarded to frontend for session {session_id}")
            else:
                logger.error(f"Failed to forward tool request to frontend for session {session_id}")
        
        if has_pwa:
            result = await manager.send_message(session_id, data, target='pwa')
            if result:
                logger.info(f"Tool request forwarded to PWA for session {session_id}")
            else:
                logger.error(f"Failed to forward tool request to PWA for session {session_id}")
        
    except Exception as e:
        logger.error(f"Error routing tool request: {e}", exc_info=True)
        
        # Send error back to MCP subprocess
        try:
            await manager.send_message(session_id, {
                'type': 'tool_result',
                'session_id': session_id,
                'request_id': data.get('request_id'),
                'success': False,
                'error': str(e),
                'execution_time_ms': 0,
            }, target='mcp')
        except Exception as send_error:
            logger.error(f"Failed to send error response: {send_error}")


async def route_tool_report_to_frontend(session_id: str, data: dict) -> None:
    """Route tool activity report from MCP subprocess to all display clients.
    
    Tool reports are lightweight notifications that a Python-executed tool
    is running. They are broadcast to all display clients for visual feedback.
    
    Args:
        session_id: Session ID
        data: Tool report data containing tool_name and timestamp
    """
    try:
        logger.debug(
            f"Routing tool report to display clients: session={session_id}, "
            f"tool={data.get('tool_name')}"
        )
        
        # Check if ANY display client is connected
        has_frontend = manager.has_connection(session_id, 'frontend')
        has_pwa = manager.has_connection(session_id, 'pwa')
        
        if not has_frontend and not has_pwa:
            logger.debug(f"No display clients for session {session_id}, skipping tool report")
            return
        
        # Broadcast to all connected display clients
        if has_frontend:
            result = await manager.send_message(session_id, data, target='frontend')
            if result:
                logger.debug(f"Tool report forwarded to frontend for session {session_id}")
            else:
                logger.warning(f"Failed to forward tool report to frontend for session {session_id}")
        
        if has_pwa:
            result = await manager.send_message(session_id, data, target='pwa')
            if result:
                logger.debug(f"Tool report forwarded to PWA for session {session_id}")
            else:
                logger.warning(f"Failed to forward tool report to PWA for session {session_id}")
        
    except Exception as e:
        logger.error(f"Error routing tool report: {e}", exc_info=True)
        
if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "backend.server:app",
        host=settings.ws_host,
        port=settings.ws_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
