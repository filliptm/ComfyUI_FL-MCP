import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from backend.comfy_manager import (
    ComfyManagerClient,
    ManagerAPIError,
    ManagerNotInstalledError,
    ManagerVersion,
)


class FakeAsyncClient:
    def __init__(self, routes, requests, *args, **kwargs):
        self.routes = routes
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, params=None, json=None):
        key = (method, url)
        self.requests.append({"method": method, "url": url, "params": params, "json": json})
        response = self.routes.get(key)
        if response is None:
            return httpx.Response(404, text="not found")
        if callable(response):
            response = response(params=params, json=json)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)


@pytest.fixture
def fake_http(monkeypatch):
    routes = {}
    requests = []

    def factory(*args, **kwargs):
        return FakeAsyncClient(routes, requests, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return routes, requests


def test_check_installed_uses_features_manager_v4(fake_http):
    routes, _ = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True, "supports_csrf_post": True}}
    }

    async def run():
        client = ComfyManagerClient("http://comfy")
        return await client.check_installed()

    status = asyncio.run(run())

    assert status == ManagerVersion(version="v4", installed=True, supports_v4=True)


def test_installed_packs_and_node_mappings_use_v2_routes(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/v2/customnode/installed")] = {
        "ComfyUI_FL-Ren-Agent": {
            "ver": "abc123",
            "cnr_id": "",
            "aux_id": "filliptm/ComfyUI_FL-Ren-Agent",
            "enabled": True,
        }
    }
    routes[("GET", "http://comfy/v2/customnode/getmappings")] = {
        "comfy-core": [["KSampler", "SaveImage"], {"title_aux": "Comfy Core"}]
    }

    async def run():
        client = ComfyManagerClient("http://comfy")
        return await client.list_installed_packs(), await client.get_node_mappings(mode="local")

    installed, mappings = asyncio.run(run())

    assert "ComfyUI_FL-Ren-Agent" in installed
    assert mappings["KSampler"].node_pack_id == "comfy-core"
    assert mappings["KSampler"].node_pack_name == "Comfy Core"
    assert requests[-1]["params"] == {"mode": "local"}
    assert not any(
        request["url"].startswith("http://comfy/customnode/")
        for request in requests
    )


def test_installed_packs_fall_back_to_legacy_route_and_remember_it(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/customnode/installed")] = {
        "Example Pack": {
            "ver": "abc123",
            "cnr_id": "example-pack",
            "aux_id": "example/pack",
            "enabled": True,
        }
    }
    routes[("GET", "http://comfy/v2/customnode/getmappings")] = {
        "example-pack": [["ExampleNode"], {"title_aux": "Example Pack"}]
    }

    async def run():
        client = ComfyManagerClient("http://comfy")
        first = await client.list_installed_packs()
        second = await client.list_installed_packs()
        mappings = await client.get_node_mappings()
        return first, second, mappings

    first, second, mappings = asyncio.run(run())

    assert first == second
    assert "Example Pack" in first
    assert mappings["ExampleNode"].node_pack_id == "example-pack"
    urls = [request["url"] for request in requests]
    assert urls.count("http://comfy/v2/customnode/installed") == 1
    assert urls.count("http://comfy/customnode/installed") == 2
    assert urls.count("http://comfy/v2/customnode/getmappings") == 1
    assert "http://comfy/customnode/getmappings" not in urls


def test_node_mappings_fall_back_to_legacy_route_and_remember_it(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/customnode/getmappings")] = {
        "legacy-pack": [["LegacyNode"], {"title_aux": "Legacy Pack"}]
    }

    async def run():
        client = ComfyManagerClient("http://comfy")
        first = await client.get_node_mappings(mode="local")
        second = await client.get_node_mappings(mode="local")
        return first, second

    first, second = asyncio.run(run())

    assert first == second
    assert first["LegacyNode"].node_pack_name == "Legacy Pack"
    urls = [request["url"] for request in requests]
    assert urls.count("http://comfy/v2/customnode/getmappings") == 1
    assert urls.count("http://comfy/customnode/getmappings") == 2
    mapping_requests = [
        request for request in requests if request["url"].endswith("getmappings")
    ]
    assert all(request["params"] == {"mode": "local"} for request in mapping_requests)


def test_customnode_read_does_not_fall_back_on_non_404(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/v2/customnode/installed")] = httpx.Response(
        500,
        text="boom",
    )
    routes[("GET", "http://comfy/customnode/installed")] = {"unexpected": {}}

    async def run():
        client = ComfyManagerClient("http://comfy")
        await client.list_installed_packs()

    with pytest.raises(ManagerAPIError) as exc:
        asyncio.run(run())

    assert exc.value.status_code == 500
    assert "500" in str(exc.value)
    assert not any(
        request["url"] == "http://comfy/customnode/installed"
        for request in requests
    )


def test_legacy_manager_allows_reads_but_keeps_v4_queue_blocked(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"version": "legacy"}}
    }
    routes[("GET", "http://comfy/customnode/installed")] = {
        "Legacy Pack": {"aux_id": "legacy/pack", "enabled": True}
    }
    routes[("GET", "http://comfy/customnode/getmappings")] = {
        "legacy-pack": [["LegacyNode"], {"title_aux": "Legacy Pack"}]
    }

    async def run():
        client = ComfyManagerClient("http://comfy")
        installed = await client.list_installed_packs()
        mappings = await client.get_node_mappings()
        with pytest.raises(ManagerNotInstalledError):
            await client.queue_status()
        return installed, mappings

    installed, mappings = asyncio.run(run())

    assert "Legacy Pack" in installed
    assert mappings["LegacyNode"].node_pack_name == "Legacy Pack"
    assert not any("/v2/" in request["url"] for request in requests)


def test_queue_action_posts_manager_v4_task_and_starts_queue(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("POST", "http://comfy/v2/manager/queue/task")] = {"ok": True}
    routes[("GET", "http://comfy/v2/manager/queue/start")] = {"started": True}

    async def run():
        client = ComfyManagerClient("http://comfy")
        return await client.queue_action(
            "disable",
            {"node_name": "example-pack", "is_unknown": False},
            client_id="test-client",
            ui_id="test-ui",
        )

    result = asyncio.run(run())

    task_request = requests[-2]
    assert task_request["url"] == "http://comfy/v2/manager/queue/task"
    assert task_request["json"] == {
        "ui_id": "test-ui",
        "client_id": "test-client",
        "kind": "disable",
        "params": {"node_name": "example-pack", "is_unknown": False},
    }
    assert result["queued"] is True
    assert result["requires_restart"] is True


def test_queue_start_and_reset_use_current_manager_get_routes(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/v2/manager/queue/start")] = {"started": True}
    routes[("GET", "http://comfy/v2/manager/queue/reset")] = {"reset": True}

    async def run():
        client = ComfyManagerClient("http://comfy")
        return await client.queue_start(), await client.queue_reset()

    started, reset = asyncio.run(run())

    assert started == {"started": True}
    assert reset == {"reset": True}
    queue_requests = [
        request for request in requests if "/v2/manager/queue/" in request["url"]
    ]
    assert [request["method"] for request in queue_requests] == ["GET", "GET"]
    assert all(request["json"] is None for request in queue_requests)


@pytest.mark.parametrize("status_code", [404, 405])
def test_queue_start_falls_back_to_post_and_remembers_older_manager_method(
    fake_http,
    status_code,
):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/v2/manager/queue/start")] = httpx.Response(
        status_code,
        text="method unavailable",
    )
    routes[("POST", "http://comfy/v2/manager/queue/start")] = {"started": True}

    async def run():
        client = ComfyManagerClient("http://comfy")
        first = await client.queue_start()
        second = await client.queue_start()
        return first, second

    first, second = asyncio.run(run())

    assert first == second == {"started": True}
    queue_requests = [
        request for request in requests if request["url"].endswith("/queue/start")
    ]
    assert [request["method"] for request in queue_requests] == ["GET", "POST", "POST"]
    assert queue_requests[0]["json"] is None
    assert queue_requests[1]["json"] == {}
    assert queue_requests[2]["json"] == {}


def test_queue_method_fallback_does_not_hide_server_errors(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/v2/manager/queue/reset")] = httpx.Response(
        500,
        text="boom",
    )
    routes[("POST", "http://comfy/v2/manager/queue/reset")] = {"unexpected": True}

    async def run():
        client = ComfyManagerClient("http://comfy")
        await client.queue_reset()

    with pytest.raises(ManagerAPIError) as exc:
        asyncio.run(run())

    assert exc.value.status_code == 500
    reset_requests = [
        request for request in requests if request["url"].endswith("/queue/reset")
    ]
    assert [request["method"] for request in reset_requests] == ["GET"]


def test_queue_action_update_all_uses_query_params(fake_http):
    routes, requests = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("POST", "http://comfy/v2/manager/queue/update_all")] = {"ok": True}

    async def run():
        client = ComfyManagerClient("http://comfy")
        return await client.queue_action(
            "update-all",
            {"mode": "cache"},
            client_id="test-client",
            ui_id="update-all",
            start_queue=False,
        )

    result = asyncio.run(run())

    update_request = requests[-1]
    assert update_request["params"] == {
        "client_id": "test-client",
        "ui_id": "update-all",
        "mode": "cache",
    }
    assert result["queue_start"] is None


def test_v2_api_errors_are_reported(fake_http):
    routes, _ = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    routes[("GET", "http://comfy/v2/manager/queue/status")] = httpx.Response(
        500,
        text="boom",
    )

    async def run():
        client = ComfyManagerClient("http://comfy")
        await client.queue_status()

    with pytest.raises(ManagerAPIError) as exc:
        asyncio.run(run())

    assert "500" in str(exc.value)
    assert "boom" in str(exc.value)


def test_external_models_fall_back_to_packaged_model_db(fake_http, monkeypatch, tmp_path):
    routes, _ = fake_http
    routes[("GET", "http://comfy/features")] = {
        "extension": {"manager": {"supports_v4": True}}
    }
    package_dir = tmp_path / "comfyui_manager"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "model-list.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "TAEF1 Decoder",
                        "filename": "taef1_decoder.pth",
                        "type": "TAESD",
                        "base": "FLUX.1",
                        "description": "FLUX preview decoder",
                        "reference": "https://example.test/taesd",
                        "save_path": "vae_approx",
                        "size": "4.71MB",
                        "url": "https://example.test/taef1_decoder.pth",
                    },
                    {
                        "name": "Other Model",
                        "filename": "other.safetensors",
                        "type": "checkpoint",
                        "base": "SDXL",
                        "description": "",
                        "reference": "",
                        "save_path": "checkpoints",
                        "size": "1GB",
                        "url": "https://example.test/other.safetensors",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(
        sys.modules,
        "comfyui_manager",
        SimpleNamespace(__file__=str(package_dir / "__init__.py")),
    )

    async def run():
        client = ComfyManagerClient("http://comfy")
        return await client.search_external_models(query="flux", max_results=5)

    results = asyncio.run(run())

    assert len(results) == 1
    assert results[0].name == "TAEF1 Decoder"
    assert results[0].installed is False
