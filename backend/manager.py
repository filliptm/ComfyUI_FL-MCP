"""WebSocket connection manager with multi-client session support."""

from fastapi import WebSocket
from typing import Any, Dict, Optional
from datetime import datetime, timedelta
import logging

from models import (
    HandshakeAck,
    ErrorMessage,
    SessionContext,
)

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections with session-based routing.
    
    Supports multiple connection types per session (frontend and mcp).
    """

    def __init__(
        self,
        session_timeout_seconds: int = 300,  # 5 minutes
    ):
        # Map session_id -> dict of connection types -> WebSocket
        # e.g., {"session123": {"frontend": WebSocket, "mcp": WebSocket}}
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        # Map session_id -> SessionContext
        self.session_contexts: Dict[str, SessionContext] = {}
        # Map session_id -> Agent instance (populated by agent.py)
        self.session_agents: Dict[str, Any] = {}  # type: ignore
        # Session timeout
        self.session_timeout = timedelta(seconds=session_timeout_seconds)

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
        
        # Store connection by type
        self.active_connections[session_id][connection_type] = websocket

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

    def disconnect(self, session_id: str, connection_type: str = 'frontend') -> None:
        """Disconnect a WebSocket connection.

        Note: Session context is kept alive for reconnection window.

        Args:
            session_id: Session ID to disconnect
            connection_type: Type of connection to disconnect ('frontend' or 'mcp')
        """
        if session_id in self.active_connections:
            if connection_type in self.active_connections[session_id]:
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
                    sent = True
                except Exception as e:
                    logger.error(f"Error sending message to {session_id}: {e}")
        
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
            agent_context=None,  # TODO: Add context if needed
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
            # Clean up agent instance
            if session_id in self.session_agents:
                del self.session_agents[session_id]
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


# Global connection manager instance
manager = ConnectionManager()
