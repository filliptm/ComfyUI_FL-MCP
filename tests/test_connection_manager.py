import pytest
from manager import ConnectionManager


class FakeWebSocket:
    def __init__(self):
        self.close_calls = []
        self.messages = []

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))

    async def send_json(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_new_connection_replaces_old_owner():
    manager = ConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()

    await manager.connect(old_socket, "session", "mcp")
    await manager.connect(new_socket, "session", "mcp")

    assert manager.active_connections["session"]["mcp"]["legacy-mcp"] is new_socket
    assert old_socket.close_calls == [(4000, "replaced by a newer connection")]


@pytest.mark.asyncio
async def test_stale_disconnect_cannot_remove_new_owner():
    manager = ConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()

    await manager.connect(old_socket, "session", "mcp")
    await manager.connect(new_socket, "session", "mcp")
    manager.disconnect("session", old_socket, "mcp")

    assert manager.active_connections["session"]["mcp"]["legacy-mcp"] is new_socket

    manager.disconnect("session", new_socket, "mcp")
    assert "session" not in manager.active_connections


@pytest.mark.asyncio
async def test_distinct_mcp_clients_coexist_and_results_are_targeted():
    manager = ConnectionManager()
    first = FakeWebSocket()
    second = FakeWebSocket()

    await manager.connect(first, "session", "mcp", "first")
    await manager.connect(second, "session", "mcp", "second")

    assert manager.has_connection("session", "mcp", "first")
    assert manager.has_connection("session", "mcp", "second")
    assert first.close_calls == []
    assert manager.register_tool_request("session", "request-1", "first")
    assert manager.resolve_tool_request("session", "request-1") == "first"

    await manager.send_message(
        "session",
        {"type": "tool_result", "request_id": "request-1"},
        target="mcp",
        client_id="first",
    )
    assert len(first.messages) == 1
    assert second.messages == []


@pytest.mark.asyncio
async def test_duplicate_active_request_id_is_rejected():
    manager = ConnectionManager()
    assert manager.register_tool_request("session", "same", "first")
    assert not manager.register_tool_request("session", "same", "second")
