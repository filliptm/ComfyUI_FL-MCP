"""FL-MCP runtime version and build identity."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__version__ = "0.8.0"

RUNTIME_PRODUCT = "comfyui-fl-mcp"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def runtime_build_identity(project_root: str | Path = PROJECT_ROOT) -> str:
    """Hash the Python sources loaded by the bridge runtime."""
    root = Path(project_root).resolve()
    paths = sorted((root / "backend").rglob("*.py"))
    daemon_entrypoint = root / "mcp_daemon.py"
    if daemon_entrypoint.is_file():
        paths.append(daemon_entrypoint)

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_project_identity(project_root: str | Path = PROJECT_ROOT) -> str:
    """Return a non-reversible identity for this checkout location."""
    root = str(Path(project_root).resolve()).encode("utf-8")
    return hashlib.sha256(root).hexdigest()


RUNTIME_BUILD_ID = runtime_build_identity()
RUNTIME_PROJECT_ID = runtime_project_identity()
RUNTIME_STARTED_AT_UNIX = time.time()
RUNTIME_STARTED_AT = datetime.fromtimestamp(
    RUNTIME_STARTED_AT_UNIX,
    UTC,
).isoformat().replace("+00:00", "Z")


def runtime_metadata() -> dict[str, Any]:
    return {
        "product": RUNTIME_PRODUCT,
        "project_id": RUNTIME_PROJECT_ID,
        "build_id": RUNTIME_BUILD_ID,
        "version": __version__,
        "started_at": RUNTIME_STARTED_AT,
        "started_at_unix": RUNTIME_STARTED_AT_UNIX,
        "pid": os.getpid(),
        "mode": os.getenv("FL_MCP_MODE", "embedded").lower(),
    }
