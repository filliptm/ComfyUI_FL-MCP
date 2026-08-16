import json
from pathlib import Path

import pytest
import server_runner

BACKEND_DIR = Path(server_runner.__file__).resolve().parent


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeConnection:
    def __init__(self, responses, requests):
        self.responses = responses
        self.requests = requests
        self.path = None
        self.closed = False

    def request(self, method, path, **_kwargs):
        self.path = path
        self.requests.append((method, path))

    def getresponse(self):
        return self.responses[self.path]

    def close(self):
        self.closed = True


def install_http(monkeypatch, responses):
    requests = []
    connections = []

    def factory(*_args, **_kwargs):
        connection = FakeConnection(responses, requests)
        connections.append(connection)
        return connection

    monkeypatch.setattr(server_runner.http.client, "HTTPConnection", factory)
    return requests, connections


def matching_runtime(runner, **changes):
    return runner.runtime_identity | {
        "pid": 1234,
        "mode": "daemon",
        "started_at": "2026-08-06T12:00:00Z",
    } | changes


def test_existing_backend_requires_matching_runtime_identity(monkeypatch):
    runner = server_runner.ServerRunner(BACKEND_DIR, auto_start=False)
    responses = {
        "/health": FakeResponse({"status": "healthy", "active_connections": 0}),
        "/api/mcp/status": FakeResponse({"healthy": True, "port": runner.port}),
    }
    _requests, connections = install_http(monkeypatch, responses)

    assert runner.is_fl_mcp_backend() is False
    assert all(connection.closed for connection in connections)


def test_existing_backend_is_reusable_only_for_current_build(monkeypatch):
    runner = server_runner.ServerRunner(BACKEND_DIR, auto_start=False)
    runtime = matching_runtime(runner)
    responses = {
        "/health": FakeResponse({
            "status": "healthy",
            "active_connections": 0,
            "runtime": runtime,
        }),
        "/api/mcp/status": FakeResponse({
            "healthy": True,
            "port": runner.port,
            "runtime": runtime,
        }),
    }
    install_http(monkeypatch, responses)

    assert runner.is_fl_mcp_backend() is True


@pytest.mark.parametrize(
    ("changes", "same_project"),
    [
        ({"build_id": "0" * 64}, True),
        ({"project_id": "different-checkout"}, False),
    ],
)
def test_probe_distinguishes_stale_build_from_other_checkout(
    monkeypatch,
    changes,
    same_project,
):
    runner = server_runner.ServerRunner(BACKEND_DIR, auto_start=False)
    runtime = matching_runtime(runner, **changes)
    responses = {
        "/health": FakeResponse({
            "status": "healthy",
            "active_connections": 0,
            "runtime": runtime,
        }),
        "/api/mcp/status": FakeResponse({
            "healthy": True,
            "port": runner.port,
            "runtime": runtime,
        }),
    }
    install_http(monkeypatch, responses)

    probe = runner._probe_backend()

    assert probe["same_project"] is same_project
    assert probe["identity_matches"] is False
    assert probe["reusable"] is False


@pytest.mark.parametrize("launch_mode", ["auto", "subprocess"])
def test_stale_same_project_daemon_is_shutdown_before_subprocess_launch(
    monkeypatch,
    launch_mode,
):
    runner = server_runner.ServerRunner(
        BACKEND_DIR,
        launch_mode=launch_mode,
        auto_start=False,
    )
    events = []
    monkeypatch.setattr(runner, "is_port_in_use", lambda: True)
    monkeypatch.setattr(
        runner,
        "_probe_backend",
        lambda: {
            "reusable": False,
            "is_fl_mcp": True,
            "same_project": True,
            "mode": "daemon",
        },
    )
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda method, path: events.append((method, path)) or {"success": True},
    )
    monkeypatch.setattr(
        runner,
        "_wait_for_port_to_close",
        lambda: events.append(("wait", "port-close")) or True,
    )
    monkeypatch.setattr(
        runner,
        "_remove_daemon_pid_file",
        lambda: events.append(("remove", "daemon-pid")),
    )
    monkeypatch.setattr(
        runner,
        "_launch_as_subprocess",
        lambda: events.append(("launch", "subprocess")) or True,
    )

    assert runner.start() is True
    assert events == [
        ("POST", "/api/mcp/shutdown"),
        ("wait", "port-close"),
        ("remove", "daemon-pid"),
        ("launch", "subprocess"),
    ]


@pytest.mark.parametrize("launch_mode", ["auto", "subprocess"])
def test_stale_same_project_embedded_backend_is_shutdown_before_subprocess_launch(
    monkeypatch,
    launch_mode,
):
    # "embedded" is what a subprocess-launched backend reports when
    # FL_MCP_MODE is unset - e.g. one left running by a ComfyUI restart that
    # didn't actually terminate its child process. It must be replaced the
    # same way a stale daemon is, not silently left serving old code.
    runner = server_runner.ServerRunner(
        BACKEND_DIR,
        launch_mode=launch_mode,
        auto_start=False,
    )
    events = []
    monkeypatch.setattr(runner, "is_port_in_use", lambda: True)
    monkeypatch.setattr(
        runner,
        "_probe_backend",
        lambda: {
            "reusable": False,
            "is_fl_mcp": True,
            "same_project": True,
            "mode": "embedded",
        },
    )
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda method, path: events.append((method, path)) or {"success": True},
    )
    monkeypatch.setattr(
        runner,
        "_wait_for_port_to_close",
        lambda: events.append(("wait", "port-close")) or True,
    )
    monkeypatch.setattr(
        runner,
        "_remove_daemon_pid_file",
        lambda: events.append(("remove", "daemon-pid")),
    )
    monkeypatch.setattr(
        runner,
        "_launch_as_subprocess",
        lambda: events.append(("launch", "subprocess")) or True,
    )

    assert runner.start() is True
    assert events == [
        ("POST", "/api/mcp/shutdown"),
        ("wait", "port-close"),
        ("remove", "daemon-pid"),
        ("launch", "subprocess"),
    ]


def test_legacy_daemon_is_owned_only_when_local_pid_matches(tmp_path, monkeypatch):
    backend_dir = tmp_path / "project" / "backend"
    backend_dir.mkdir(parents=True)
    state_dir = backend_dir.parent / ".fl_mcp"
    state_dir.mkdir()
    (state_dir / "daemon.pid").write_text("4321", encoding="utf-8")
    runner = server_runner.ServerRunner(backend_dir, auto_start=False)
    responses = {
        "/health": FakeResponse({"status": "healthy", "active_connections": 0}),
        "/api/mcp/status": FakeResponse({
            "healthy": True,
            "port": runner.port,
            "mode": "daemon",
            "pid": 4321,
        }),
    }
    install_http(monkeypatch, responses)

    probe = runner._probe_backend()

    assert probe["same_project"] is True
    assert probe["reusable"] is False


@pytest.mark.parametrize(
    ("probe", "error_text"),
    [
        (
            {
                "reusable": False,
                "is_fl_mcp": False,
                "same_project": False,
                "mode": "",
            },
            "occupied by another service",
        ),
        (
            {
                "reusable": False,
                "is_fl_mcp": True,
                "same_project": False,
                "mode": "daemon",
            },
            "another checkout",
        ),
    ],
)
def test_unknown_and_other_project_backends_are_never_shutdown(
    monkeypatch,
    probe,
    error_text,
):
    runner = server_runner.ServerRunner(BACKEND_DIR, auto_start=False)
    monkeypatch.setattr(runner, "is_port_in_use", lambda: True)
    monkeypatch.setattr(runner, "_probe_backend", lambda: probe)
    monkeypatch.setattr(
        runner,
        "_shutdown_stale_daemon",
        lambda: pytest.fail("must not shut down an unowned backend"),
    )
    monkeypatch.setattr(
        runner,
        "_launch_as_subprocess",
        lambda: pytest.fail("must not launch while the port remains occupied"),
    )

    assert runner.start() is False
    assert error_text in runner.last_error


def test_successful_stale_daemon_shutdown_removes_pid_file(tmp_path, monkeypatch):
    backend_dir = tmp_path / "project" / "backend"
    backend_dir.mkdir(parents=True)
    pid_path = backend_dir.parent / ".fl_mcp" / "daemon.pid"
    pid_path.parent.mkdir()
    pid_path.write_text("4321", encoding="utf-8")
    runner = server_runner.ServerRunner(backend_dir, auto_start=False)
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda method, path: {"success": True},
    )
    monkeypatch.setattr(runner, "_wait_for_port_to_close", lambda: True)

    assert runner._shutdown_stale_daemon() is True
    assert pid_path.exists() is False


def test_terminal_mode_does_not_replace_stale_daemon(monkeypatch):
    runner = server_runner.ServerRunner(
        BACKEND_DIR,
        launch_mode="terminal",
        auto_start=False,
    )
    monkeypatch.setattr(runner, "is_port_in_use", lambda: True)
    monkeypatch.setattr(
        runner,
        "_probe_backend",
        lambda: {
            "reusable": False,
            "is_fl_mcp": True,
            "same_project": True,
            "mode": "daemon",
        },
    )
    monkeypatch.setattr(
        runner,
        "_shutdown_stale_daemon",
        lambda: pytest.fail("terminal mode must not replace a daemon"),
    )

    assert runner.start() is False
    assert "stale FL-MCP backend" in runner.last_error


def test_startup_failure_is_retained_for_frontend_diagnostics(tmp_path, monkeypatch):
    runner = server_runner.ServerRunner(tmp_path, auto_start=False)
    runner.active_mode = "subprocess"
    monkeypatch.setattr(runner, "is_port_in_use", lambda: False)

    class FailedProcess:
        @staticmethod
        def poll():
            return 1

    runner.process = FailedProcess()

    assert runner.wait_for_server(timeout=0.1) is False
    assert "exited during startup with code 1" in runner.last_error
    assert "fl_mcp_server.log" in runner.last_error
    runner.process = None
    runner.cleanup()
