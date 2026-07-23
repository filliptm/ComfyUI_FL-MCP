"""WebSocket connection manager with MCP and browser bridge support."""

from fastapi import WebSocket
from typing import Any, Dict, Optional, List
from datetime import datetime, timedelta
import logging
from collections import deque

import json

from models import (
    HandshakeAck,
    ErrorMessage,
    SessionContext,
)

logger = logging.getLogger(__name__)


class ErrorBuffer:
    """Circular buffer for storing ComfyUI execution errors."""
    
    def __init__(self, max_size: int = 100):
        self.errors = deque(maxlen=max_size)
        self.errors_by_prompt: Dict[str, List[Dict[str, Any]]] = {}
        self.max_size = max_size
        
    def add_error(self, error_data: Dict[str, Any]) -> None:
        """Add error to buffer with timestamp and indexing."""
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
        
        self.errors.append(error_entry)
        
        prompt_id = error_entry["prompt_id"]
        if prompt_id:
            if prompt_id not in self.errors_by_prompt:
                self.errors_by_prompt[prompt_id] = []
            self.errors_by_prompt[prompt_id].append(error_entry)
            
            if len(self.errors_by_prompt[prompt_id]) > self.max_size:
                self.errors_by_prompt[prompt_id].pop(0)
        
        logger.debug(f"Error added to buffer: {error_entry['error_type']} for prompt {prompt_id}")
        
    def get_recent_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get N most recent errors."""
        return list(self.errors)[-limit:]
        
    def get_errors_for_prompt(self, prompt_id: str) -> List[Dict[str, Any]]:
        """Get all errors for a specific prompt/run."""
        return self.errors_by_prompt.get(prompt_id, [])
        
    def get_all_errors(self) -> List[Dict[str, Any]]:
        """Get all errors in buffer."""
        return list(self.errors)
        
    def clear(self) -> None:
        """Clear all errors from buffer."""
        self.errors.clear()
        self.errors_by_prompt.clear()
        logger.info("Error buffer cleared")
        
    def get_count(self) -> int:
        """Get total number of errors in buffer."""
        return len(self.errors)


class ExecutionTracker:
    """Tracks active workflow executions and queue status."""
    
    def __init__(self):
        self.active_executions: Dict[str, Dict[str, Any]] = {}
        self.queue_status = {
            "running": [],
            "pending": [],
            "queue_remaining": 0
        }
        
    def handle_execution_start(self, data: Dict[str, Any]) -> None:
        """Handle execution_start event."""
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
        
        if prompt_id not in self.queue_status["running"]:
            self.queue_status["running"].append(prompt_id)
            
        logger.debug(f"Execution started: {prompt_id}")
        
    def handle_executing(self, data: Dict[str, Any]) -> None:
        """Handle executing event (node execution tracking)."""
        try:
            prompt_id = data.get("prompt_id")
            node_id = data.get("node")
        except Exception as e:
            print(f"Error extracting message ids: {e}, Data: type({type(data)}) > {data}")
            if isinstance(data, str):
                data = json.loads(data)
                prompt_id = data.get("prompt_id")
                node_id = data.get("node")
        
        if not prompt_id or prompt_id not in self.active_executions:
            return
            
        execution = self.active_executions[prompt_id]
        
        if node_id is None:
            execution["current_node"] = None
            execution["status"] = "completing"
        else:
            execution["current_node"] = node_id
            if node_id not in execution["executed_nodes"]:
                execution["executed_nodes"].append(node_id)
                
    def handle_execution_cached(self, data: Dict[str, Any]) -> None:
        """Handle execution_cached event."""
        prompt_id = data.get("prompt_id")
        if prompt_id and prompt_id in self.active_executions:
            self.active_executions[prompt_id]["cached_nodes"] = data.get("nodes", [])
            
    def handle_execution_success(self, data: Dict[str, Any]) -> None:
        """Handle execution_success event."""
        prompt_id = data.get("prompt_id")
        if prompt_id and prompt_id in self.active_executions:
            self.active_executions[prompt_id]["status"] = "success"
            self.active_executions[prompt_id]["end_time"] = datetime.now().isoformat()
            
            if prompt_id in self.queue_status["running"]:
                self.queue_status["running"].remove(prompt_id)
                
            logger.debug(f"Execution succeeded: {prompt_id}")
            
    def handle_execution_error(self, data: Dict[str, Any]) -> None:
        """Handle execution_error event."""
        prompt_id = data.get("prompt_id")
        if prompt_id and prompt_id in self.active_executions:
            self.active_executions[prompt_id]["status"] = "error"
            self.active_executions[prompt_id]["end_time"] = datetime.now().isoformat()
            self.active_executions[prompt_id]["error"] = {
                "node_id": data.get("node_id"),
                "exception_type": data.get("exception_type"),
                "message": data.get("exception_message"),
            }
            
            if prompt_id in self.queue_status["running"]:
                self.queue_status["running"].remove(prompt_id)
                
            logger.debug(f"Execution failed: {prompt_id}")
            
    def handle_status(self, data: Dict[str, Any]) -> None:
        """Handle status event (queue updates)."""
        exec_info = data.get("exec_info", {})
        self.queue_status["queue_remaining"] = exec_info.get("queue_remaining", 0)
        
    def get_execution_state(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        """Get execution state for a specific prompt."""
        return self.active_executions.get(prompt_id)
        
    def get_all_executions(self) -> Dict[str, Dict[str, Any]]:
        """Get all active executions."""
        return self.active_executions.copy()
        
    def get_queue_status(self) -> Dict[str, Any]:
        """Get current queue status."""
        return self.queue_status.copy()
        
    def cleanup_old_executions(self, max_age_hours: int = 24) -> int:
        """Clean up old completed executions."""
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        to_remove = []
        
        for prompt_id, execution in self.active_executions.items():
            if execution["status"] in ["success", "error"]:
                end_time_str = execution.get("end_time")
                if end_time_str:
                    end_time = datetime.fromisoformat(end_time_str)
                    if end_time < cutoff:
                        to_remove.append(prompt_id)
                    
        for prompt_id in to_remove:
            del self.active_executions[prompt_id]
            
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old executions")
            
        return len(to_remove)


class ConnectionManager:
    """Manages WebSocket connections with session-based routing."""

    def __init__(
        self,
        session_timeout_seconds: int = 300,  # 5 minutes
    ):
        # Map session_id -> dict of connection types -> WebSocket.
        # e.g. {"session123": {"frontend": WebSocket, "mcp": WebSocket}}
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        # Map session_id -> SessionContext
        self.session_contexts: Dict[str, SessionContext] = {}
        # Session timeout
        self.session_timeout = timedelta(seconds=session_timeout_seconds)
        
        # Error tracking
        self.error_buffer = ErrorBuffer(max_size=100)
        self.execution_tracker = ExecutionTracker()
        
        logger.info("ConnectionManager initialized with error tracking")

    async def connect(
        self, websocket: WebSocket, session_id: str, connection_type: str = 'frontend'
    ) -> SessionContext:
        """Register a new WebSocket connection.

        Args:
            websocket: WebSocket connection
            session_id: Session ID from client
            connection_type: Type of connection ('frontend' or 'mcp')

        Returns:
            SessionContext for this session
        """
        # Initialize session connections dict if needed
        if session_id not in self.active_connections:
            self.active_connections[session_id] = {}

        previous = self.active_connections[session_id].get(connection_type)
        self.active_connections[session_id][connection_type] = websocket
        if previous is not None and previous is not websocket:
            logger.warning(
                "Session %s - %s connection replaced by a newer client",
                session_id,
                connection_type,
            )
            try:
                await previous.close(code=4000, reason="replaced by a newer connection")
            except Exception as exc:
                logger.debug("Could not close replaced %s connection: %s", connection_type, exc)

        # Get or create session context
        if session_id in self.session_contexts:
            context = self.session_contexts[session_id]
            context.last_activity = datetime.now()
            logger.info(f"Session {session_id} - {connection_type} connected")
        else:
            context = SessionContext(session_id=session_id)
            self.session_contexts[session_id] = context
            logger.info(f"New session {session_id} created with {connection_type} connection")

        return context

    def disconnect(
        self,
        session_id: str,
        websocket: WebSocket,
        connection_type: str = 'frontend',
    ) -> None:
        """Disconnect a WebSocket connection.

        Note: Session context is kept alive for reconnection window.

        Args:
            session_id: Session ID to disconnect
            websocket: WebSocket requesting removal
            connection_type: Type of connection to disconnect ('frontend' or 'mcp')
        """
        if session_id in self.active_connections:
            if connection_type in self.active_connections[session_id]:
                current = self.active_connections[session_id][connection_type]
                if current is not websocket:
                    logger.info(
                        "Session %s - ignored stale %s disconnect",
                        session_id,
                        connection_type,
                    )
                    return
                del self.active_connections[session_id][connection_type]
                logger.info(f"Session {session_id} - {connection_type} disconnected")
            
            # Clean up session entry if no more connections
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]
                logger.info(f"Session {session_id} - all connections closed")

    async def send_message(
        self, session_id: str, message: Dict, target: str = 'frontend'
    ) -> bool:
        """Send a message to a specific session connection.

        Args:
            session_id: Target session ID
            message: Message dict to send
            target: Target connection type ('frontend', 'mcp', or 'all')

        Returns:
            True if message was sent to at least one connection, False otherwise
        """
        if session_id not in self.active_connections:
            logger.warning(f"Cannot send message: session {session_id} not connected")
            return False

        connections = self.active_connections[session_id]
        
        # Log what connections are available and what we're targeting
        logger.debug(f"[SEND] {message.get('type', 'unknown')} -> {target} | available: {list(connections.keys())} | session: {session_id[:8]}...")
        
        # Determine which connections to send to
        if target == 'all':
            targets = list(connections.values())
        else:
            targets = [connections.get(target)] if target in connections else []
        
        if not targets:
            logger.warning(f"Cannot send message: no {target} connection for session {session_id}")
            return False
        
        sent = False
        for websocket in targets:
            if websocket:
                try:
                    await websocket.send_json(message)
                    logger.debug(f"[SEND] Sent {message.get('type', 'unknown')} to {target}")
                    sent = True
                except Exception as e:
                    logger.error(f"[SEND] Error sending to {target}: {e}")
            else:
                logger.warning(f"[SEND]   WebSocket is None for {target} in session {session_id[:8]}...")
        
        # Update last activity if any message was sent
        if sent and session_id in self.session_contexts:
            self.session_contexts[session_id].last_activity = datetime.now()
        
        return sent
    
    async def send_handshake_ack(
        self, session_id: str, is_reconnect: bool, connection_type: str = 'frontend'
    ) -> None:
        """Send handshake acknowledgment.

        Args:
            session_id: Session ID
            is_reconnect: Whether this is a reconnection
            connection_type: Type of connection to send to
        """
        message = HandshakeAck(
            session_id=session_id,
            status="reconnected" if is_reconnect else "ready",
            bridge_context=None,
        )
        await self.send_message(session_id, message.model_dump(), target=connection_type)

    async def send_error(
        self,
        session_id: str,
        error_code: str,
        error_message: str,
        details: Optional[Dict] = None,
        target: str = 'frontend'
    ) -> None:
        """Send error message to client.

        Args:
            session_id: Session ID
            error_code: Error code
            error_message: Error message
            details: Additional error details
            target: Target connection type
        """
        message = ErrorMessage(
            session_id=session_id,
            error_code=error_code,
            message=error_message,
            details=details,
        )
        await self.send_message(session_id, message.model_dump(), target=target)

    def has_connection(self, session_id: str, connection_type: str) -> bool:
        """Check if a specific connection type exists for a session.
        
        Args:
            session_id: Session ID
            connection_type: Connection type to check
            
        Returns:
            True if connection exists, False otherwise
        """
        return (
            session_id in self.active_connections
            and connection_type in self.active_connections[session_id]
        )

    def cleanup_stale_sessions(self) -> int:
        """Clean up sessions that have been inactive and disconnected.

        Returns:
            Number of sessions cleaned up
        """
        now = datetime.now()
        stale_sessions = []

        for session_id, context in self.session_contexts.items():
            # Only cleanup if disconnected AND past timeout
            if (
                session_id not in self.active_connections
                and now - context.last_activity > self.session_timeout
            ):
                stale_sessions.append(session_id)

        for session_id in stale_sessions:
            # Clean up session context
            if session_id in self.session_contexts:
                del self.session_contexts[session_id]
            logger.info(f"Cleaned up stale session {session_id}")

        return len(stale_sessions)

    def get_active_session_count(self) -> int:
        """Get number of sessions with active connections.

        Returns:
            Number of sessions with at least one active connection
        """
        return len(self.active_connections)

    def get_total_session_count(self) -> int:
        """Get total number of sessions (active + inactive).

        Returns:
            Total number of sessions
        """
        return len(self.session_contexts)
    
    def get_connection_info(self, session_id: str) -> Dict[str, bool]:
        """Get connection status for a session.
        
        Args:
            session_id: Session ID
            
        Returns:
            Dict with connection types as keys and boolean status as values
        """
        if session_id not in self.active_connections:
            return {}
        return {
            conn_type: True
            for conn_type in self.active_connections[session_id].keys()
        }
    
    def _get_session_id_for_prompt(self, prompt_id: str) -> Optional[str]:
        """Find session ID for a given prompt ID.
        
        This searches through active sessions to find which one is executing
        the given prompt.
        
        Args:
            prompt_id: Prompt ID to search for
            
        Returns:
            Session ID if found, None otherwise
        """
        # For now, we assume single session per prompt
        # In a multi-session scenario, we'd need to track prompt -> session mapping
        # Return the first active session (simple heuristic)
        if self.active_connections:
            return list(self.active_connections.keys())[0]
        return None
    
    async def handle_comfy_error(self, data: Dict[str, Any]) -> None:
        """Handle error from ComfyUI frontend and broadcast to MCP clients."""
        error_type = data.get("error_type")
        
        if error_type == "execution_error":
            self.error_buffer.add_error(data)
            self.execution_tracker.handle_execution_error(data)
            logger.error(
                f"ComfyUI execution error in node {data.get('node_id')} "
                f"({data.get('node_type')}): {data.get('exception_message')}"
            )
            
            # Broadcast execution errors to the browser bridge session if present.
            prompt_id = data.get("prompt_id")
            session_id = self._get_session_id_for_prompt(prompt_id)
            if session_id and self.has_connection(session_id, "frontend"):
                await self.send_message(session_id, {
                    "type": "execution_error",
                    "prompt_id": prompt_id,
                    "node_id": data.get("node_id"),
                    "node_type": data.get("node_type"),
                    "exception_type": data.get("exception_type"),
                    "exception_message": data.get("exception_message"),
                }, target="frontend")
                
        elif error_type == "execution_interrupted":
            self.error_buffer.add_error(data)
            logger.warning(
                f"ComfyUI execution interrupted at node {data.get('node_id')}"
            )
            
    async def handle_queue_status(self, data: Dict[str, Any]) -> None:
        """Handle queue status update from ComfyUI."""
        self.execution_tracker.handle_status(data)
        logger.debug(f"Queue status: {data.get('exec_info', {}).get('queue_remaining', 0)} remaining")
        
    async def handle_execution_event(self, event: str, data: Dict[str, Any]) -> None:
        """Handle execution lifecycle events from ComfyUI and broadcast to MCP clients."""
        if event == "start":
            self.execution_tracker.handle_execution_start(data)
        elif event == "executing":
            self.execution_tracker.handle_executing(data)
        elif event == "cached":
            self.execution_tracker.handle_execution_cached(data)
        elif event == "success":
            self.execution_tracker.handle_execution_success(data)
            
            # Broadcast success to the browser bridge session if present.
            prompt_id = data.get("prompt_id")
            session_id = self._get_session_id_for_prompt(prompt_id)
            if session_id and self.has_connection(session_id, "frontend"):
                execution_data = self.execution_tracker.get_execution_state(prompt_id)
                await self.send_message(session_id, {
                    "type": "execution_success",
                    "prompt_id": prompt_id,
                    "execution": execution_data,
                }, target="frontend")


# Global connection manager instance
manager = ConnectionManager()
