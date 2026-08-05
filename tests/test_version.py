import tomllib
from pathlib import Path

import pytest
import server
from version import __version__

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
