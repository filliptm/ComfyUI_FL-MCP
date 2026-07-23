import uuid

import server
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _handshake(websocket, session_id, client_version):
    websocket.send_json({
        "type": "handshake",
        "session_id": session_id,
        "client_version": client_version,
    })
    message = websocket.receive_json()
    assert message["type"] == "handshake_ack"


def test_browser_and_mcp_tool_round_trip():
    session_id = f"integration-{uuid.uuid4()}"
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as frontend:
            _handshake(frontend, session_id, "1.0.0-frontend")
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
