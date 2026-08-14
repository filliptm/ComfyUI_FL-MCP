import hashlib

import pytest
from manager import ConnectionManager
from models import (
    Handshake,
    canonical_tool_contract_manifest_hash,
    canonical_tool_manifest_hash,
)
from pydantic import ValidationError


class FakeWebSocket:
    def __init__(self):
        self.close_calls = []
        self.messages = []

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))

    async def send_json(self, message):
        self.messages.append(message)


def test_frontend_tool_manifest_uses_exact_json_hash_and_strict_ordering():
    tools = ["apply_workflow_graph_patch", "generate_seed"]
    expected_hash = hashlib.sha256(
        b'["apply_workflow_graph_patch","generate_seed"]'
    ).hexdigest()

    handshake = Handshake(
        session_id="session",
        connection_type="frontend",
        supported_tools=tools,
        tool_manifest_hash=expected_hash,
    )
    assert handshake.supported_tools == tools
    assert canonical_tool_manifest_hash(tools) == expected_hash

    for invalid_tools in (
        ["generate_seed", "apply_workflow_graph_patch"],
        ["generate_seed", "generate_seed"],
        ["not a tool"],
        [1],
    ):
        with pytest.raises(ValidationError):
            Handshake(
                session_id="session",
                connection_type="frontend",
                supported_tools=invalid_tools,
            )


def test_frontend_tool_contract_manifest_matches_browser_canonical_hash():
    tools = [
        "apply_workflow_graph_patch",
        "find_node",
        "workflow_get_current_json",
    ]
    revisions = {
        "apply_workflow_graph_patch": 3,
        "find_node": 1,
        "workflow_get_current_json": 1,
    }
    expected_hash = "a50992e03368df832d12a5d17cad5ecfe7672223efe5a1c6a79b5dc16a6d1c49"
    handshake = Handshake(
        session_id="session",
        connection_type="frontend",
        supported_tools=tools,
        tool_manifest_hash=canonical_tool_manifest_hash(tools),
        tool_contract_revisions=revisions,
        tool_contract_manifest_hash=expected_hash,
    )

    assert handshake.tool_contract_revisions == revisions
    assert canonical_tool_contract_manifest_hash(revisions) == expected_hash

    invalid_manifests = (
        {"find_node": 1, "apply_workflow_graph_patch": 3},
        {"apply_workflow_graph_patch": 1},
        {"apply_workflow_graph_patch": True, "find_node": 1, "workflow_get_current_json": 1},
    )
    for invalid in invalid_manifests:
        with pytest.raises(ValidationError):
            Handshake(
                session_id="session",
                connection_type="frontend",
                supported_tools=tools,
                tool_contract_revisions=invalid,
            )


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


@pytest.mark.asyncio
async def test_frontend_replacement_atomically_replaces_capability_manifest():
    manager = ConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    old_tools = ["apply_workflow_graph_patch"]
    new_tools = ["generate_seed"]

    await manager.connect(
        old_socket,
        "session",
        "frontend",
        supported_tools=old_tools,
        tool_manifest_hash=canonical_tool_manifest_hash(old_tools),
        tool_contract_revisions={"apply_workflow_graph_patch": 3},
        tool_contract_manifest_hash=canonical_tool_contract_manifest_hash(
            {"apply_workflow_graph_patch": 3}
        ),
    )
    await manager.connect(
        new_socket,
        "session",
        "frontend",
        supported_tools=new_tools,
        tool_manifest_hash=canonical_tool_manifest_hash(new_tools),
        tool_contract_revisions={"generate_seed": 1},
        tool_contract_manifest_hash=canonical_tool_contract_manifest_hash(
            {"generate_seed": 1}
        ),
    )

    capabilities = manager.get_frontend_capabilities("session")
    assert capabilities is not None
    assert capabilities.websocket is new_socket
    assert capabilities.supported_tools == ("generate_seed",)
    assert old_socket.close_calls == [(4000, "replaced by a newer connection")]
    assert manager.frontend_tool_capability_failure("session", "generate_seed") is None
    failure = manager.frontend_tool_capability_failure(
        "session",
        "apply_workflow_graph_patch",
    )
    assert failure is not None
    assert failure["error_code"] == "frontend_capability_missing"

    manager.disconnect("session", old_socket, "frontend")
    assert manager.get_frontend_capabilities("session") is capabilities


@pytest.mark.asyncio
async def test_legacy_frontend_connects_but_cannot_authorize_tools():
    manager = ConnectionManager()
    socket = FakeWebSocket()

    await manager.connect(socket, "session", "frontend")

    assert manager.has_connection("session", "frontend")
    failure = manager.frontend_tool_capability_failure("session", "generate_seed")
    assert failure is not None
    assert failure["error_code"] == "frontend_bridge_outdated"
    assert failure["error_details"] == {
        "bridge_state": "frontend_bridge_outdated",
        "capability_code": "frontend_capability_missing",
        "reason": "capability_manifest_missing",
        "requested_tool": "generate_seed",
        "supported_tool_count": None,
        "tool_manifest_hash": None,
    }


@pytest.mark.asyncio
async def test_graph_patch_contract_revision_guard_fails_before_forwarding():
    manager = ConnectionManager()
    socket = FakeWebSocket()
    tools = ["apply_workflow_graph_patch"]
    revisions = {"apply_workflow_graph_patch": 2}
    await manager.connect(
        socket,
        "session",
        "frontend",
        supported_tools=tools,
        tool_manifest_hash=canonical_tool_manifest_hash(tools),
        tool_contract_revisions=revisions,
        tool_contract_manifest_hash=canonical_tool_contract_manifest_hash(revisions),
    )

    failure = manager.frontend_tool_capability_failure(
        "session",
        "apply_workflow_graph_patch",
    )
    assert failure is not None
    assert failure["error_code"] == "frontend_bridge_outdated"
    assert failure["error_details"] == {
        "bridge_state": "frontend_bridge_outdated",
        "capability_code": "frontend_contract_revision_outdated",
        "reason": "tool_contract_revision_outdated",
        "requested_tool": "apply_workflow_graph_patch",
        "required_contract_revision": 3,
        "advertised_contract_revision": 2,
        "tool_contract_manifest_hash": canonical_tool_contract_manifest_hash(revisions),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "edit_node_mask",
        "confirm_mask_review",
        "get_node_image_ref",
        "get_selected_nodes",
        "queue_workflow",
        "set_node_values_exact",
    ],
)
async def test_exact_edit_contract_revision_is_required_before_forwarding(tool_name):
    manager = ConnectionManager()
    socket = FakeWebSocket()
    tools = [tool_name]
    revisions = {tool_name: 1}
    await manager.connect(
        socket,
        "session",
        "frontend",
        supported_tools=tools,
        tool_manifest_hash=canonical_tool_manifest_hash(tools),
        tool_contract_revisions=revisions,
        tool_contract_manifest_hash=canonical_tool_contract_manifest_hash(revisions),
    )

    failure = manager.frontend_tool_capability_failure("session", tool_name)
    assert failure is not None
    assert failure["error_code"] == "frontend_bridge_outdated"
    expected_revision = {
        "edit_node_mask": 5,
        "confirm_mask_review": 3,
        "set_node_values_exact": 4,
            "queue_workflow": 3,
    }.get(tool_name, 2)
    assert failure["error_details"]["required_contract_revision"] == expected_revision
    assert failure["error_details"]["advertised_contract_revision"] == 1
