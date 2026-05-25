"""SSE chat stream broadcaster for Ren.

This decouples an agent run from the browser response. A client can start a
run, detach, and reattach to the same conversation while events are replayed.
The frontend tool callback WebSocket can remain separate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


ChatEvent = Dict[str, Any]


@dataclass
class StreamState:
    conversation_id: str
    session_id: str
    started_at: datetime = field(default_factory=datetime.now)
    buffer: List[ChatEvent] = field(default_factory=list)
    subscribers: List[asyncio.Queue[Optional[ChatEvent]]] = field(default_factory=list)
    task: Optional[asyncio.Task] = None
    closed: bool = False


class StreamHandle:
    def __init__(self, broadcaster: "ChatBroadcaster", state: StreamState):
        self._broadcaster = broadcaster
        self._state = state

    @property
    def conversation_id(self) -> str:
        return self._state.conversation_id

    @property
    def session_id(self) -> str:
        return self._state.session_id

    def set_task(self, task: asyncio.Task) -> None:
        self._state.task = task

    async def publish(self, event: ChatEvent) -> None:
        await self._broadcaster.publish(self._state.conversation_id, event)

    async def end(self) -> None:
        await self._broadcaster.end(self._state.conversation_id)


class SubscribeResult:
    def __init__(
        self,
        broadcaster: "ChatBroadcaster",
        conversation_id: str,
        queue: asyncio.Queue[Optional[ChatEvent]],
        replay: List[ChatEvent],
    ):
        self._broadcaster = broadcaster
        self.conversation_id = conversation_id
        self.queue = queue
        self.replay = replay

    def unsubscribe(self) -> None:
        self._broadcaster.unsubscribe(self.conversation_id, self.queue)


class ChatBroadcaster:
    """Reference-counted in-memory event hub for active chat runs."""

    MAX_BUFFER = 10_000

    def __init__(self) -> None:
        self._streams: Dict[str, StreamState] = {}
        self._lock = asyncio.Lock()

    async def start(self, conversation_id: str, session_id: str) -> StreamHandle:
        async with self._lock:
            existing = self._streams.get(conversation_id)
            if existing and not existing.closed:
                await self._end_locked(conversation_id)

            state = StreamState(conversation_id=conversation_id, session_id=session_id)
            self._streams[conversation_id] = state
            return StreamHandle(self, state)

    async def publish(self, conversation_id: str, event: ChatEvent) -> None:
        async with self._lock:
            state = self._streams.get(conversation_id)
            if not state or state.closed:
                return

            if len(state.buffer) < self.MAX_BUFFER:
                state.buffer.append(event)

            for queue in list(state.subscribers):
                queue.put_nowait(event)

    async def end(self, conversation_id: str) -> None:
        async with self._lock:
            await self._end_locked(conversation_id)

    async def _end_locked(self, conversation_id: str) -> None:
        state = self._streams.get(conversation_id)
        if not state or state.closed:
            return

        state.closed = True
        for queue in list(state.subscribers):
            queue.put_nowait(None)
        self._streams.pop(conversation_id, None)

    async def subscribe(self, conversation_id: str) -> Optional[SubscribeResult]:
        async with self._lock:
            state = self._streams.get(conversation_id)
            if not state or state.closed:
                return None

            queue: asyncio.Queue[Optional[ChatEvent]] = asyncio.Queue()
            state.subscribers.append(queue)
            return SubscribeResult(self, conversation_id, queue, list(state.buffer))

    def unsubscribe(
        self,
        conversation_id: str,
        queue: asyncio.Queue[Optional[ChatEvent]],
    ) -> None:
        state = self._streams.get(conversation_id)
        if not state:
            return
        try:
            state.subscribers.remove(queue)
        except ValueError:
            pass

    def is_active(self, conversation_id: str) -> bool:
        state = self._streams.get(conversation_id)
        return bool(state and not state.closed)

    def cancel(self, conversation_id: str) -> bool:
        state = self._streams.get(conversation_id)
        if not state or state.closed:
            return False
        if state.task and not state.task.done():
            state.task.cancel()
            return True
        return False

    def list(self) -> List[Dict[str, Any]]:
        return [
            {
                "conversationId": state.conversation_id,
                "sessionId": state.session_id,
                "startedAt": state.started_at.isoformat(),
            }
            for state in self._streams.values()
            if not state.closed
        ]
