import json

import server_runner


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def request(self, _method, _path):
        return None

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_existing_backend_requires_fl_mcp_health_shape(monkeypatch):
    response = FakeResponse({"message": "another API"})
    connection = FakeConnection(response)
    monkeypatch.setattr(
        server_runner.http.client,
        "HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )
    runner = server_runner.ServerRunner(".", auto_start=False)

    assert runner.is_fl_mcp_backend() is False
    assert connection.closed is True


def test_existing_fl_mcp_backend_is_reusable(monkeypatch):
    response = FakeResponse({"status": "healthy", "active_connections": 0})
    connection = FakeConnection(response)
    monkeypatch.setattr(
        server_runner.http.client,
        "HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )
    runner = server_runner.ServerRunner(".", auto_start=False)

    assert runner.is_fl_mcp_backend() is True
