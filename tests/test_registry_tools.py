from types import SimpleNamespace

import mcp_server
import pytest


def fake_context(registry_client, manager_client=None):
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={
            "client": None,
            "registry_client": registry_client,
            "manager_client": manager_client,
        })
    )


class FakeManager:
    async def list_installed_packs(self):
        return {
            "folder-name": {"cnr_id": "installed.pack", "enabled": True},
        }


class BrokenManager:
    async def list_installed_packs(self):
        raise RuntimeError("Manager is offline")


class AuxOnlyManager:
    async def list_installed_packs(self):
        return {
            "folder-only": {"aux_id": "Owner/Repository", "enabled": True},
        }


class CompleteIdentityManager:
    async def list_installed_packs(self):
        return {
            "folder-a": {"cnr_id": "official.pack", "enabled": True},
            "folder-b": {"aux_id": "Other/Repo", "enabled": True},
        }


class UnresolvedIdentityManager:
    async def list_installed_packs(self):
        return {
            "mystery-folder": {"aux_id": "not-a-repository", "enabled": True},
        }


class FakeRegistryClient:
    def __init__(self):
        self.search_kwargs = None
        self.get_kwargs = None

    async def search_packages(self, query, **kwargs):
        self.search_kwargs = {"query": query, **kwargs}
        return {
            "ok": True,
            "query": query,
            "registry_query": query,
            "results": [{
                "package_id": "example.pack",
                "name": "Example Pack",
                "registry_url": "https://registry.comfy.org/publishers/example/nodes/pack",
                "github_url": "https://github.com/example/pack",
            }],
            "skipped_results": [],
        }

    async def get_package(self, package_id, **kwargs):
        self.get_kwargs = {"package_id": package_id, **kwargs}
        return {
            "ok": True,
            "package": {
                "package_id": package_id,
                "name": "Example Pack",
                "registry_url": "https://registry.comfy.org/publishers/example/nodes/pack",
                "github_url": "https://github.com/example/pack",
                "comfy_nodes": ["ExampleNode"],
                "class_count": 1,
                "class_metadata_state": "available",
            },
        }


@pytest.mark.asyncio
async def test_registry_search_passes_precise_filters_and_returns_both_links():
    client = FakeRegistryClient()
    result = await mcp_server.registry_search_packages.fn(
        mcp_server.RegistrySearchPackagesRequest(
            query="background removal",
            comfy_node_search="RemoveBackground",
            supported_os="macos",
            supported_accelerator="metal",
            max_results=7,
            refresh=True,
        ),
        fake_context(client, FakeManager()),
    )

    assert client.search_kwargs == {
        "query": "background removal",
        "comfy_node_search": "RemoveBackground",
        "supported_os": "macos",
        "supported_accelerator": "metal",
        "include_installed": False,
        "max_results": 7,
        "refresh": True,
        "installed_pack_ids": {
            "package_ids": ["folder-name", "installed.pack"],
            "repository_urls": [],
            "identity_complete": True,
        },
    }
    assert result["results"][0]["registry_url"].startswith("https://registry.comfy.org/")
    assert result["results"][0]["github_url"] == "https://github.com/example/pack"
    assert result["local_install_state"] == {
        "state": "known",
        "source": "comfyui_manager",
        "installed_pack_count": 1,
        "package_identity_count": 2,
        "repository_identity_count": 0,
        "identity_complete": True,
    }


def test_registry_search_defaults_to_uninstalled_new_node_discovery():
    request = mcp_server.RegistrySearchPackagesRequest(query="new nodes")
    assert request.include_installed is False


def test_registry_tool_bounds_match_client_bounds():
    with pytest.raises(ValueError):
        mcp_server.RegistrySearchPackagesRequest(query="nodes", max_results=21)
    with pytest.raises(ValueError):
        mcp_server.RegistryGetPackageRequest(package_id="example.pack", max_classes=201)


@pytest.mark.asyncio
async def test_manager_aux_id_only_produces_verified_repository_identity():
    identity, state = await mcp_server._registry_installed_pack_ids(
        fake_context(None, AuxOnlyManager())
    )

    assert identity == {
        "package_ids": ["folder-only"],
        "repository_urls": ["https://github.com/Owner/Repository"],
        "identity_complete": True,
    }
    assert state["state"] == "known"
    assert state["identity_complete"] is True
    assert state["repository_identity_count"] == 1


@pytest.mark.asyncio
async def test_manager_identity_envelope_is_complete_when_every_pack_is_resolved():
    identity, state = await mcp_server._registry_installed_pack_ids(
        fake_context(None, CompleteIdentityManager())
    )

    assert identity == {
        "package_ids": ["folder-a", "folder-b", "official.pack"],
        "repository_urls": ["https://github.com/Other/Repo"],
        "identity_complete": True,
    }
    assert state == {
        "state": "known",
        "source": "comfyui_manager",
        "installed_pack_count": 2,
        "package_identity_count": 3,
        "repository_identity_count": 1,
        "identity_complete": True,
    }


@pytest.mark.asyncio
async def test_unresolved_manager_pack_keeps_possible_id_but_marks_identity_incomplete():
    identity, state = await mcp_server._registry_installed_pack_ids(
        fake_context(None, UnresolvedIdentityManager())
    )

    assert identity == {
        "package_ids": ["mystery-folder"],
        "repository_urls": [],
        "identity_complete": False,
    }
    assert state["state"] == "known"
    assert state["identity_complete"] is False
    assert "lack both a Registry ID" in state["reason"]


@pytest.mark.asyncio
async def test_registry_detail_survives_unknown_manager_install_state():
    client = FakeRegistryClient()
    result = await mcp_server.registry_get_package.fn(
        mcp_server.RegistryGetPackageRequest(
            package_id="example.pack",
            max_classes=50,
        ),
        fake_context(client, BrokenManager()),
    )

    assert client.get_kwargs == {
        "package_id": "example.pack",
        "refresh": False,
        "installed_pack_ids": None,
        "max_classes": 50,
    }
    assert result["package"]["registry_url"].startswith("https://registry.comfy.org/")
    assert result["package"]["github_url"] == "https://github.com/example/pack"
    assert result["local_install_state"]["state"] == "unknown"
    assert "Manager is offline" in result["local_install_state"]["reason"]
