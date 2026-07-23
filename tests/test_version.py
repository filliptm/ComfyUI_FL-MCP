import pytest
import server
from version import __version__


@pytest.mark.asyncio
async def test_runtime_endpoints_share_one_version():
    assert server.app.version == __version__
    assert (await server.root())["version"] == __version__
    assert (await server.get_client_config())["version"] == __version__
