import pytest
from manager import ConnectionManager


class FakeWebSocket:
    def __init__(self):
        self.close_calls = []

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))


@pytest.mark.asyncio
async def test_new_connection_replaces_old_owner():
    manager = ConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()

    await manager.connect(old_socket, "session", "mcp")
    await manager.connect(new_socket, "session", "mcp")

    assert manager.active_connections["session"]["mcp"] is new_socket
    assert old_socket.close_calls == [(4000, "replaced by a newer connection")]


@pytest.mark.asyncio
async def test_stale_disconnect_cannot_remove_new_owner():
    manager = ConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()

    await manager.connect(old_socket, "session", "mcp")
    await manager.connect(new_socket, "session", "mcp")
    manager.disconnect("session", old_socket, "mcp")

    assert manager.active_connections["session"]["mcp"] is new_socket

    manager.disconnect("session", new_socket, "mcp")
    assert "session" not in manager.active_connections
