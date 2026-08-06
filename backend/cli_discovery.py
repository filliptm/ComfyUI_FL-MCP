"""Desktop-safe discovery for CLIs installed in a user's login shell."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    import pwd
except ImportError:  # pragma: no cover - Windows has no pwd module.
    pwd = None

CLI_DISCOVERY_TIMEOUT_SECONDS = 5
_CLI_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")
_PATH_MARKER = "__FL_MCP_LOGIN_PATH__="


@dataclass(frozen=True, slots=True)
class CliDiscovery:
    executable: str | None
    search_path: str
    source: str


def _login_shell() -> str | None:
    candidates = [os.getenv("SHELL", "")]
    if pwd is not None:
        try:
            candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
        except (KeyError, OSError):
            pass
    candidates.extend(("/bin/zsh", "/bin/bash", "/bin/sh"))
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


@lru_cache(maxsize=1)
def login_shell_path() -> str | None:
    """Read PATH from an interactive login shell, as Finder apps cannot."""

    if os.name == "nt":
        return None
    shell = _login_shell()
    if not shell:
        return None
    command = f'printf "\\n{_PATH_MARKER}%s\\n" "$PATH"'
    try:
        result = subprocess.run(
            [shell, "-l", "-i", "-c", command],
            capture_output=True,
            check=False,
            text=True,
            timeout=CLI_DISCOVERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(_PATH_MARKER):
            value = line.removeprefix(_PATH_MARKER).strip()
            return value or None
    return None


@lru_cache(maxsize=16)
def discover_cli(name: str) -> CliDiscovery:
    """Resolve a CLI from inherited PATH, then the user's login-shell PATH."""

    if not _CLI_NAME.fullmatch(name):
        raise ValueError("CLI name contains unsupported characters.")
    inherited_path = os.getenv("PATH", "")
    executable = shutil.which(name, path=inherited_path)
    if executable:
        return CliDiscovery(str(Path(executable).resolve()), inherited_path, "process_path")
    resolved_path = login_shell_path()
    if resolved_path:
        executable = shutil.which(name, path=resolved_path)
        if executable:
            return CliDiscovery(
                str(Path(executable).resolve()),
                resolved_path,
                "login_shell",
            )
    return CliDiscovery(None, resolved_path or inherited_path, "missing")


def cli_environment(name: str) -> dict[str, str]:
    """Return an environment that can execute a shebang-based discovered CLI."""

    discovery = discover_cli(name)
    environment = os.environ.copy()
    if discovery.search_path:
        environment["PATH"] = discovery.search_path
    return environment


def clear_cli_discovery_cache() -> None:
    """Refresh discovery after installing a CLI or completing login."""

    discover_cli.cache_clear()
    login_shell_path.cache_clear()
