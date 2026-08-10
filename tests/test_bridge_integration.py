import uuid

import server
from fastapi.testclient import TestClient
from models import (
    canonical_tool_contract_manifest_hash,
    canonical_tool_manifest_hash,
)
from starlette.websockets import WebSocketDisconnect


def _handshake(
    websocket,
    session_id,
    client_version,
    client_id=None,
    supported_tools=None,
):
    payload = {
        "type": "handshake",
        "session_id": session_id,
        "client_version": client_version,
    }
    if client_id:
        payload["connection_type"] = "mcp"
        payload["client_id"] = client_id
    if supported_tools is not None:
        payload["supported_tools"] = supported_tools
        payload["tool_manifest_hash"] = canonical_tool_manifest_hash(supported_tools)
        revisions = {
            tool: 3 if tool == "apply_workflow_graph_patch" else 1
            for tool in supported_tools
        }
        payload["tool_contract_revisions"] = revisions
        payload["tool_contract_manifest_hash"] = (
            canonical_tool_contract_manifest_hash(revisions)
        )
    websocket.send_json(payload)
    message = websocket.receive_json()
    assert message["type"] == "handshake_ack"


def test_browser_and_mcp_tool_round_trip():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as frontend:
            _handshake(
                frontend,
                session_id,
                "1.0.0-frontend",
                supported_tools=["apply_workflow_graph_patch", "generate_seed"],
            )
            with client.websocket_connect("/ws") as mcp:
                _handshake(mcp, session_id, "1.0.0-mcp")

                mcp.send_json({
                    "type": "tool_request",
                    "session_id": session_id,
                    "request_id": "request-1",
                    "tool_name": "generate_seed",
                    "parameters": {},
                })
                request = frontend.receive_json()
                assert request["type"] == "tool_request"
                assert request["tool_name"] == "generate_seed"

                frontend.send_json({
                    "type": "tool_result",
                    "session_id": session_id,
                    "request_id": "request-1",
                    "success": True,
                    "data": {"seed": 42},
                    "execution_time_ms": 1,
                })
                result = mcp.receive_json()
                assert result["type"] == "tool_result"
                assert result["data"] == {"seed": 42}


def test_replaced_mcp_disconnect_does_not_remove_new_connection():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        first_context = client.websocket_connect("/ws")
        first = first_context.__enter__()
        _handshake(first, session_id, "1.0.0-mcp")

        with client.websocket_connect("/ws") as second:
            _handshake(second, session_id, "1.0.0-mcp")

            try:
                first.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 4000
            else:
                raise AssertionError("The replaced MCP connection remained open")
            finally:
                first_context.__exit__(None, None, None)

            status = client.get("/api/sessions").json()
            session = next(item for item in status["sessions"] if item["session_id"] == session_id)
            assert session["has_mcp"] is True


def test_multiple_mcp_clients_receive_only_their_results():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as frontend:
            _handshake(
                frontend,
                session_id,
                "1.0.0-frontend",
                supported_tools=[
                    "apply_workflow_graph_patch",
                    "generate_seed",
                    "workflow_overview",
                ],
            )
            with client.websocket_connect("/ws") as first:
                _handshake(first, session_id, "1.0.0-mcp", "first")
                with client.websocket_connect("/ws") as second:
                    _handshake(second, session_id, "1.0.0-mcp", "second")

                    first.send_json({
                        "type": "tool_request",
                        "session_id": session_id,
                        "request_id": "first-request",
                        "tool_name": "generate_seed",
                        "parameters": {},
                    })
                    second.send_json({
                        "type": "tool_request",
                        "session_id": session_id,
                        "request_id": "second-request",
                        "tool_name": "workflow_overview",
                        "parameters": {},
                    })

                    requests = [frontend.receive_json(), frontend.receive_json()]
                    assert {item["request_id"] for item in requests} == {
                        "first-request",
                        "second-request",
                    }

                    frontend.send_json({
                        "type": "tool_result",
                        "session_id": session_id,
                        "request_id": "second-request",
                        "success": True,
                        "data": {"owner": "second"},
                        "execution_time_ms": 1,
                    })
                    frontend.send_json({
                        "type": "tool_result",
                        "session_id": session_id,
                        "request_id": "first-request",
                        "success": True,
                        "data": {"owner": "first"},
                        "execution_time_ms": 1,
                    })

                    assert second.receive_json()["data"] == {"owner": "second"}
                    assert first.receive_json()["data"] == {"owner": "first"}

                    session = next(
                        item
                        for item in client.get("/api/sessions").json()["sessions"]
                        if item["session_id"] == session_id
                    )
                    assert session["connections"]["mcp_client_count"] == 2


def test_stale_frontend_is_rejected_before_tool_request_is_forwarded():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as frontend:
            _handshake(
                frontend,
                session_id,
                "1.0.0-frontend",
                supported_tools=["apply_workflow_graph_patch", "generate_seed"],
            )
            with client.websocket_connect("/ws") as mcp:
                _handshake(mcp, session_id, "1.0.0-mcp")

                mcp.send_json({
                    "type": "tool_request",
                    "session_id": session_id,
                    "request_id": "missing-capability",
                    "tool_name": "workflow_overview",
                    "parameters": {"would_mutate": True},
                })
                failure = mcp.receive_json()
                assert failure["success"] is False
                assert failure["error_code"] == "frontend_capability_missing"
                assert failure["error_details"]["requested_tool"] == (
                    "workflow_overview"
                )
                assert failure["error_details"]["bridge_state"] == (
                    "frontend_bridge_outdated"
                )

                # A supported request is the first message the browser receives,
                # proving the mutating request never crossed the bridge boundary.
                mcp.send_json({
                    "type": "tool_request",
                    "session_id": session_id,
                    "request_id": "supported-capability",
                    "tool_name": "generate_seed",
                    "parameters": {},
                })
                forwarded = frontend.receive_json()
                assert forwarded["request_id"] == "supported-capability"
                assert forwarded["tool_name"] == "generate_seed"


def test_outdated_frontend_handshakes_cannot_replace_capable_active_bridge():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as frontend:
            _handshake(
                frontend,
                session_id,
                "1.0.0-frontend",
                supported_tools=["apply_workflow_graph_patch", "generate_seed"],
            )

            for supported_tools, expected_reason in (
                (None, "capability_manifest_missing"),
                (["generate_seed"], "required_capability_missing"),
            ):
                with client.websocket_connect("/ws") as stale_frontend:
                    payload = {
                        "type": "handshake",
                        "session_id": session_id,
                        "client_version": "0.9.0-frontend",
                    }
                    if supported_tools is not None:
                        payload["supported_tools"] = supported_tools
                        payload["tool_manifest_hash"] = canonical_tool_manifest_hash(
                            supported_tools
                        )
                    stale_frontend.send_json(payload)
                    failure = stale_frontend.receive_json()
                    assert failure["type"] == "error"
                    assert failure["error_code"] == "frontend_bridge_outdated"
                    assert failure["error_details"]["reason"] == expected_reason
                    assert failure["error_details"]["requested_tool"] == (
                        "apply_workflow_graph_patch"
                    )
                    try:
                        stale_frontend.receive_json()
                    except WebSocketDisconnect as exc:
                        assert exc.code == 1000
                        assert exc.reason == "frontend_bridge_outdated"
                    else:
                        raise AssertionError("The outdated frontend remained connected")

            with client.websocket_connect("/ws") as stale_frontend:
                stale_tools = ["apply_workflow_graph_patch", "generate_seed"]
                stale_revisions = {
                    "apply_workflow_graph_patch": 2,
                    "generate_seed": 1,
                }
                stale_frontend.send_json({
                    "type": "handshake",
                    "session_id": session_id,
                    "client_version": "0.9.0-frontend",
                    "supported_tools": stale_tools,
                    "tool_manifest_hash": canonical_tool_manifest_hash(stale_tools),
                    "tool_contract_revisions": stale_revisions,
                    "tool_contract_manifest_hash": (
                        canonical_tool_contract_manifest_hash(stale_revisions)
                    ),
                })
                failure = stale_frontend.receive_json()
                assert failure["type"] == "error"
                assert failure["error_code"] == "frontend_bridge_outdated"
                assert failure["error_details"]["reason"] == (
                    "tool_contract_revision_outdated"
                )
                assert failure["error_details"]["required_contract_revision"] == 3
                assert failure["error_details"]["advertised_contract_revision"] == 2
                try:
                    stale_frontend.receive_json()
                except WebSocketDisconnect as exc:
                    assert exc.code == 1000
                    assert exc.reason == "frontend_bridge_outdated"
                else:
                    raise AssertionError("The outdated frontend remained connected")

            with client.websocket_connect("/ws") as mcp:
                _handshake(mcp, session_id, "1.0.0-mcp")
                mcp.send_json({
                    "type": "tool_request",
                    "session_id": session_id,
                    "request_id": "active-bridge-request",
                    "tool_name": "generate_seed",
                    "parameters": {},
                })
                forwarded = frontend.receive_json()
                assert forwarded["request_id"] == "active-bridge-request"
                assert forwarded["tool_name"] == "generate_seed"


def test_frontend_handshake_rejects_a_mismatched_manifest_hash():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as frontend:
            frontend.send_json({
                "type": "handshake",
                "session_id": session_id,
                "client_version": "1.0.0-frontend",
                "supported_tools": ["generate_seed"],
                "tool_manifest_hash": "0" * 64,
            })

            failure = frontend.receive_json()
            assert failure["type"] == "error"
            assert failure["error_code"] == "INVALID_HANDSHAKE_DATA"
            assert "tool_manifest_hash does not match" in failure["message"]
