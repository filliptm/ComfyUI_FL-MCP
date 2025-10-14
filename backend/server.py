"""FastAPI server with WebSocket endpoint for FL_JS Agentic System."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from .websocket import manager
from callback_router import CallbackRouter, current_session_id
from .agent import agent_manager
from models import (
    Handshake,
    UserMessage,
    ToolResult,
    Ping,
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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
    logger.info(f"LLM Model: {settings.llm_model}")
    
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
        
        # Register connection
        is_reconnect = session_id in manager.session_contexts
        context = await manager.connect(websocket, session_id)
        
        # Send handshake acknowledgment
        await manager.send_handshake_ack(session_id, is_reconnect)
        
        logger.info(
            f"Session {session_id} {'reconnected' if is_reconnect else 'connected'}"
        )
        
        # Message loop
        while True:
            data = await websocket.receive_json()
            
            # Validate session_id in message
            msg_session_id = data.get("session_id")
            if msg_session_id != session_id:
                await manager.send_error(
                    session_id,
                    "SESSION_MISMATCH",
                    f"Message session_id '{msg_session_id}' does not match connection session_id '{session_id}'",
                )
                continue
            
            # Route message based on type
            msg_type = data.get("type")
            
            if msg_type == "ping":
                await handle_ping(session_id)
            
            elif msg_type == "user_message":
                await handle_user_message(session_id, data)
            
            elif msg_type == "tool_result":
                await handle_tool_result(session_id, data)
            
            else:
                await manager.send_error(
                    session_id,
                    "UNKNOWN_MESSAGE_TYPE",
                    f"Unknown message type: {msg_type}",
                )
    
    except WebSocketDisconnect:
        if session_id:
            manager.disconnect(session_id)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            logger.info(f"Session {session_id} disconnected")
    
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e}", exc_info=True)
        if session_id:
            manager.disconnect(session_id)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            await manager.send_error(
                session_id,
                "INTERNAL_ERROR",
                "An internal error occurred",
                {"error": str(e)},
            )
        try:
            await websocket.close()
        except Exception:
            pass


# Message handlers

async def handle_ping(session_id: str) -> None:
    """Handle ping message.

    Args:
        session_id: Session ID
    """
    await manager.send_pong(session_id)


async def handle_user_message(session_id: str, data: dict[str, Any]) -> None:
    """Handle user message.

    Args:
        session_id: Session ID
        data: Message data
    """
    try:
        message = UserMessage(**data)
        logger.info(f"User message from {session_id}: {message.content[:50]}...")
        
        # Set session context for tool callbacks
        current_session_id.set(session_id)
        
        # Send typing indicator
        await manager.send_message(session_id, {
            "type": "typing_indicator",
            "session_id": session_id,
            "is_typing": True,
        })
        
        # Get or create agent for this session
        agent = agent_manager.get_agent(session_id)
        
        # Process message with agent
        response = await agent.run(message.content)
        
        # Send response
        await manager.send_message(session_id, {
            "type": "agent_response",
            "session_id": session_id,
            "message": response.message,
            "tool_calls": response.tool_calls,
            "is_final": True,
        })
        
        logger.info(f"Agent response sent to {session_id}")
    
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
    """Handle tool execution result.

    Args:
        session_id: Session ID
        data: Tool result data
    """
    try:
        result = ToolResult(**data)
        logger.debug(
            f"Tool result from {session_id}: request_id={result.request_id} "
            f"({'success' if result.success else 'failed'})"
        )
        
        # Route to callback router
        if callback_router:
            await callback_router.handle_tool_result(
                request_id=result.request_id,
                success=result.success,
                data=result.data,
                error=result.error,
                execution_time_ms=result.execution_time_ms
            )
        else:
            logger.error("Callback router not initialized, cannot handle tool result")
    
    except Exception as e:
        logger.error(f"Error handling tool result: {e}", exc_info=True)
        await manager.send_error(
            session_id,
            "TOOL_RESULT_ERROR",
            "Failed to process tool result",
            {"error": str(e)},
        )


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "backend.server:app",
        host=settings.ws_host,
        port=settings.ws_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
