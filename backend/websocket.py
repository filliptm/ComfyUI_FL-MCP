"""WebSocket connection manager with multi-client session support."""

from fastapi import WebSocket
from typing import Dict, Optional
from datetime import datetime, timedelta
import logging

from models import (
    HandshakeAck,
    Pong,
    ErrorMessage,
    SessionContext,
)

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections with session-based routing."""

    def __init__(
        self,
        session_timeout_seconds: int = 300,  # 5 minutes
    ):
        # Map session_id -> WebSocket connection
        self.active_connections: Dict[str, WebSocket] = {}
        # Map session_id -> SessionContext
        self.session_contexts: Dict[str, SessionContext] = {}
        # Map session_id -> Agent instance (populated by agent.py)
        self.session_agents: Dict[str, Any] = {}  # type: ignore
        # Session timeout
        self.session_timeout = timedelta(seconds=session_timeout_seconds)

    async def connect(
        self, websocket: WebSocket, session_id: str
    ) -> SessionContext:
        """Register a new WebSocket connection.

        Args:
            websocket: WebSocket connection
            session_id: Session ID from client

        Returns:
            SessionContext for this session
        """
        await websocket.accept()
        self.active_connections[session_id] = websocket

        # Get or create session context
        if session_id in self.session_contexts:
            context = self.session_contexts[session_id]
            context.last_activity = datetime.now()
            logger.info(f"Session {session_id} reconnected")
        else:
            context = SessionContext(session_id=session_id)
            self.session_contexts[session_id] = context
            logger.info(f"New session {session_id} created")

        return context

    def disconnect(self, session_id: str) -> None:
        """Disconnect a WebSocket connection.

        Note: Session context is kept alive for reconnection window.

        Args:
            session_id: Session ID to disconnect
        """
        if session_id in self.active_connections:
            del self.active_connections[session_id]
            logger.info(f"Session {session_id} disconnected")

    async def send_message(self, session_id: str, message: Dict) -> bool:
        """Send a message to a specific session.

        Args:
            session_id: Target session ID
            message: Message dict to send

        Returns:
            True if message was sent, False if session not connected
        """
        if session_id not in self.active_connections:
            logger.warning(f"Cannot send message: session {session_id} not connected")
            return False

        try:
            websocket = self.active_connections[session_id]
            await websocket.send_json(message)
            
            # Update last activity
            if session_id in self.session_contexts:
                self.session_contexts[session_id].last_activity = datetime.now()
            
            return True
        except Exception as e:
            logger.error(f"Error sending message to {session_id}: {e}")
            return False

    async def send_handshake_ack(
        self, session_id: str, is_reconnect: bool
    ) -> None:
        """Send handshake acknowledgment.

        Args:
            session_id: Session ID
            is_reconnect: Whether this is a reconnection
        """
        message = HandshakeAck(
            session_id=session_id,
            status="reconnected" if is_reconnect else "ready",
            agent_context=None,  # TODO: Add context if needed
        )
        await self.send_message(session_id, message.model_dump())

    async def send_pong(self, session_id: str) -> None:
        """Send pong response to ping.

        Args:
            session_id: Session ID
        """
        message = Pong(
            session_id=session_id,
            timestamp=datetime.now().isoformat(),
        )
        await self.send_message(session_id, message.model_dump())

    async def send_error(
        self,
        session_id: str,
        error_code: str,
        error_message: str,
        details: Optional[Dict] = None,
    ) -> None:
        """Send error message to client.

        Args:
            session_id: Session ID
            error_code: Error code
            error_message: Error message
            details: Additional error details
        """
        message = ErrorMessage(
            session_id=session_id,
            error_code=error_code,
            message=error_message,
            details=details,
        )
        await self.send_message(session_id, message.model_dump())

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
        """Get number of active connections.

        Returns:
            Number of active connections
        """
        return len(self.active_connections)

    def get_total_session_count(self) -> int:
        """Get total number of sessions (active + inactive).

        Returns:
            Total number of sessions
        """
        return len(self.session_contexts)


# Global connection manager instance
manager = ConnectionManager()
