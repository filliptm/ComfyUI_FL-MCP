import asyncio
import json

import mcp_server
import pytest

_CLOSE = object()


class FakeClientSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.close_calls = 0

    async def recv(self):
        return json.dumps({"type": "handshake_ack", "status": "ready"})

    async def send(self, message):
        data = json.loads(message)
        self.sent.append(data)
        if data["type"] == "tool_request":
            await self.incoming.put(json.dumps({
                "type": "tool_result",
                "request_id": data["request_id"],
                "success": True,
                "data": {"tool": data["tool_name"]},
            }))

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is _CLOSE:
            raise StopAsyncIteration
        return message

    async def close(self):
        self.close_calls += 1
        await self.incoming.put(_CLOSE)


@pytest.mark.asyncio
async def test_next_tool_reconnects_after_connection_closes(monkeypatch):
    sockets = [FakeClientSocket(), FakeClientSocket()]
    connect_calls = 0

    async def connect(_url):
        nonlocal connect_calls
        socket = sockets[connect_calls]
        connect_calls += 1
        return socket

    monkeypatch.setattr(mcp_server.websockets, "connect", connect)
    client = mcp_server.MCPWebSocketClient("session", "ws://bridge/ws")

    await client.connect()
    await sockets[0].close()
    await client._receive_task

    result = await client.execute_tool("generate_seed", {})

    assert result == {"tool": "generate_seed"}
    assert connect_calls == 2
    await client.disconnect()


@pytest.mark.asyncio
async def test_concurrent_tools_share_one_reconnect(monkeypatch):
    socket = FakeClientSocket()
    connect_calls = 0

    async def connect(_url):
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(0)
        return socket

    monkeypatch.setattr(mcp_server.websockets, "connect", connect)
    client = mcp_server.MCPWebSocketClient("session", "ws://bridge/ws")

    first, second = await asyncio.gather(
        client.execute_tool("first", {}),
        client.execute_tool("second", {}),
    )

    assert first == {"tool": "first"}
    assert second == {"tool": "second"}
    assert connect_calls == 1
    await client.disconnect()


@pytest.mark.asyncio
async def test_old_receive_loop_cannot_disconnect_new_generation(monkeypatch):
    old_socket = FakeClientSocket()
    new_socket = FakeClientSocket()
    sockets = iter([old_socket, new_socket])

    async def connect(_url):
        return next(sockets)

    monkeypatch.setattr(mcp_server.websockets, "connect", connect)
    client = mcp_server.MCPWebSocketClient("session", "ws://bridge/ws")

    await client.connect()
    client.connected = False
    await client.connect()
    await asyncio.sleep(0)

    assert client.connected is True
    assert client.ws is new_socket
    await client.disconnect()


@pytest.mark.asyncio
async def test_handshake_includes_explicit_client_identity(monkeypatch):
    socket = FakeClientSocket()

    async def connect(_url):
        return socket

    monkeypatch.setattr(mcp_server.websockets, "connect", connect)
    client = mcp_server.MCPWebSocketClient(
        "session",
        "ws://bridge/ws",
        client_id="embedded-chat-test",
    )
    await client.connect()

    handshake = socket.sent[0]
    assert handshake["connection_type"] == "mcp"
    assert handshake["client_id"] == "embedded-chat-test"
    await client.disconnect()


@pytest.mark.asyncio
async def test_embedded_tool_filter_removes_unselected_tools(monkeypatch):
    class Tool:
        def __init__(self, name):
            self.name = name

    class FakeMcp:
        def __init__(self):
            self.removed = []

        async def get_tools(self):
            return {
                "workflow_overview": Tool("workflow_overview"),
                "queue_workflow": Tool("queue_workflow"),
            }

        def remove_tool(self, name):
            self.removed.append(name)

    fake = FakeMcp()
    monkeypatch.setattr(mcp_server, "mcp", fake)
    monkeypatch.setenv("FL_MCP_ALLOWED_TOOLS", "workflow_overview")

    await mcp_server._restrict_tools_from_environment()

    assert fake.removed == ["queue_workflow"]


@pytest.mark.asyncio
async def test_embedded_tool_filter_supports_current_fastmcp_api(monkeypatch):
    class Tool:
        def __init__(self, name):
            self.name = name

    class FakeMcp:
        def __init__(self):
            self.removed = []

        async def list_tools(self, *, run_middleware):
            assert run_middleware is False
            return [Tool("workflow_overview"), Tool("queue_workflow")]

        def remove_tool(self, name):
            self.removed.append(name)

    fake = FakeMcp()
    monkeypatch.setattr(mcp_server, "mcp", fake)
    monkeypatch.setenv("FL_MCP_ALLOWED_TOOLS", "workflow_overview")

    await mcp_server._restrict_tools_from_environment()

    assert fake.removed == ["queue_workflow"]
