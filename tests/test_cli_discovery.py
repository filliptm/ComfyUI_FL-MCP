from types import SimpleNamespace

import cli_discovery


def test_cli_discovery_uses_login_shell_path_for_gui_apps(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "claude"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(cli_discovery, "_login_shell", lambda: "/bin/zsh")
    monkeypatch.setattr(
        cli_discovery.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=f"profile output\n__FL_MCP_LOGIN_PATH__={bin_dir}:/usr/bin\n",
        ),
    )
    cli_discovery.clear_cli_discovery_cache()

    discovery = cli_discovery.discover_cli("claude")
    environment = cli_discovery.cli_environment("claude")

    assert discovery.executable == str(executable.resolve())
    assert discovery.source == "login_shell"
    assert environment["PATH"] == f"{bin_dir}:/usr/bin"
    cli_discovery.clear_cli_discovery_cache()


def test_cli_discovery_rejects_shell_syntax():
    try:
        cli_discovery.discover_cli("claude; touch /tmp/nope")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("Unsafe CLI name was accepted")
