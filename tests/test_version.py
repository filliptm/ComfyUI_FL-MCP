import tomllib
from pathlib import Path

import pytest
import server
from version import (
    RUNTIME_PRODUCT,
    __version__,
    runtime_build_identity,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_package_version():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        package = tomllib.load(handle)

    assert package["project"]["version"] == __version__


@pytest.mark.asyncio
async def test_runtime_endpoints_share_one_version():
    assert server.app.version == __version__
    assert (await server.root())["version"] == __version__
    assert (await server.get_client_config())["version"] == __version__

    health_runtime = (await server.health())["runtime"]
    status_runtime = (await server.mcp_status())["runtime"]
    assert health_runtime == status_runtime
    assert health_runtime["product"] == RUNTIME_PRODUCT
    assert health_runtime["version"] == __version__
    assert len(health_runtime["project_id"]) == 64
    assert len(health_runtime["build_id"]) == 64
    assert health_runtime["started_at"].endswith("Z")
    assert health_runtime["started_at_unix"] > 0
    assert health_runtime["pid"] > 0


def test_runtime_build_identity_is_content_deterministic(tmp_path):
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    source = backend_dir / "example.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    first = runtime_build_identity(tmp_path)
    assert runtime_build_identity(tmp_path) == first

    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert runtime_build_identity(tmp_path) != first
