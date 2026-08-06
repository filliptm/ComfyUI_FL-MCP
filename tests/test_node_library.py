import asyncio

import httpx
import pytest

from backend import node_library
from backend.node_library import (
    NodeLibraryClient,
    NodeLibraryConnectionError,
    canonical_schema_hash,
    classify_node_origin,
    get_node_library_client,
    node_schema_hash,
)


def node_info(
    *,
    display_name="Example",
    python_module="nodes",
    category="test",
    description="",
    search_aliases=None,
):
    info = {
        "display_name": display_name,
        "python_module": python_module,
        "category": category,
        "description": description,
        "input": {"required": {"image": ["IMAGE", {}]}},
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
    }
    if search_aliases is not None:
        info["search_aliases"] = search_aliases
    return info


class FakeAsyncClient:
    def __init__(self, responses, requests, *args, **kwargs):
        self.responses = responses
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        self.requests.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status_code, data = response
        request = httpx.Request("GET", url)
        if isinstance(data, dict):
            return httpx.Response(status_code, json=data, request=request)
        return httpx.Response(status_code, text=str(data), request=request)


def install_fake_http(monkeypatch, responses):
    requests = []

    def factory(*args, **kwargs):
        return FakeAsyncClient(responses, requests, *args, **kwargs)

    monkeypatch.setattr(node_library.httpx, "AsyncClient", factory)
    return requests


def test_catalog_hash_ignores_object_info_key_order():
    first = {"NodeB": {"output": ["IMAGE"]}, "NodeA": {"input": {"required": {}}}}
    second = {"NodeA": {"input": {"required": {}}}, "NodeB": {"output": ["IMAGE"]}}

    assert canonical_schema_hash(first) == canonical_schema_hash(second)


def test_schema_hash_changes_only_for_modified_node():
    first = node_info(display_name="First")
    reordered = dict(reversed(list(first.items())))
    changed = {**first, "display_name": "Changed"}

    assert node_schema_hash("ExampleNode", first) == node_schema_hash("ExampleNode", reordered)
    assert node_schema_hash("ExampleNode", first) != node_schema_hash("ExampleNode", changed)


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (node_info(python_module="nodes"), "native"),
        (node_info(python_module="comfy_extras.nodes_upscale_model"), "native"),
        (node_info(python_module="comfy_api_nodes.nodes_openai", category="partner/image"), "partner"),
        ({**node_info(python_module="nodes"), "api_node": True}, "partner"),
        (node_info(python_module="custom_nodes.ComfyUI_KJNodes.nodes"), "custom"),
        (node_info(python_module="third_party.unknown"), "unknown"),
    ],
)
def test_origin_classifies_loaded_node_provenance(info, expected):
    assert classify_node_origin(info) == expected


def test_exact_query_match_wins_regardless_of_catalog_order():
    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.cache.set(
            {
                "KSamplerAdvanced": node_info(display_name="KSampler Advanced"),
                "CustomKSampler": node_info(display_name="Custom KSampler"),
                "KSampler": node_info(display_name="KSampler"),
            },
            client.source,
        )
        return await client.search_nodes(query="KSampler", max_results=1)

    results = asyncio.run(run())

    assert [result.node_type for result in results] == ["KSampler"]
    assert results[0].score == 100
    assert results[0].match_reason == "exact node type match"


def test_search_ties_have_stable_lexical_order_and_provenance():
    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.cache.set(
            {
                "Zulu": node_info(python_module="custom_nodes.example"),
                "alpha": node_info(python_module="nodes"),
                "Beta": node_info(python_module="comfy_api_nodes.example", category="partner"),
            },
            client.source,
        )
        return await client.search_nodes(max_results=3)

    results = asyncio.run(run())

    assert [result.node_type for result in results] == ["alpha", "Beta", "Zulu"]
    assert [result.origin for result in results] == ["native", "partner", "custom"]
    assert all(len(result.schema_hash) == 64 for result in results)


def test_search_uses_best_match_across_class_display_name_and_aliases():
    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.cache.set(
            {
                "HelperWithBar": node_info(
                    display_name="Bar",
                    search_aliases=["Something Else"],
                ),
            },
            client.source,
        )
        return await client.search_nodes(query="bar")

    results = asyncio.run(run())

    assert results[0].score == 90
    assert results[0].match_reason == "exact display name match"


def test_cache_reports_empty_fresh_and_stale():
    now = [100.0]

    async def run():
        client = NodeLibraryClient("http://comfy", cache_ttl=10, clock=lambda: now[0])
        empty = await client.cache.status(client.source)
        await client.cache.set({"KSampler": node_info()}, client.source)
        fresh = await client.catalog_status()
        now[0] = 111.0
        stale = await client.catalog_status()
        return empty, fresh, stale

    empty, fresh, stale = asyncio.run(run())

    assert empty["state"] == "empty"
    assert fresh["state"] == "fresh"
    assert fresh["node_count"] == 1
    assert stale["state"] == "stale"
    assert stale["catalog_hash"] == fresh["catalog_hash"]


def test_cold_catalog_status_loads_object_info(monkeypatch):
    requests = install_fake_http(
        monkeypatch,
        [(200, {"KSampler": node_info()})],
    )

    async def run():
        client = NodeLibraryClient("http://comfy")
        return await client.catalog_status()

    status = asyncio.run(run())

    assert status["state"] == "fresh"
    assert status["node_count"] == 1
    assert requests == ["http://comfy/object_info"]


def test_cached_fetch_and_force_refresh_replace_catalog_generation(monkeypatch):
    first = {"KSampler": node_info(display_name="First")}
    second = {"KSampler": node_info(display_name="Second")}
    requests = install_fake_http(monkeypatch, [(200, first), (200, second)])

    async def run():
        client = NodeLibraryClient("http://comfy")
        initial = await client.fetch_node_library()
        cached = await client.fetch_node_library()
        first_status = await client.catalog_status()
        refreshed = await client.fetch_node_library(force_refresh=True)
        second_status = await client.catalog_status()
        return initial, cached, refreshed, first_status, second_status

    initial, cached, refreshed, first_status, second_status = asyncio.run(run())

    assert initial == cached == first
    assert refreshed == second
    assert requests == ["http://comfy/object_info", "http://comfy/object_info"]
    assert first_status["catalog_hash"] != second_status["catalog_hash"]


def test_failed_refresh_keeps_last_good_catalog_snapshot(monkeypatch):
    catalog = {"KSampler": node_info()}
    requests = install_fake_http(monkeypatch, [(200, catalog), (500, "boom")])

    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.fetch_node_library()
        before = await client.catalog_status()
        with pytest.raises(NodeLibraryConnectionError):
            await client.fetch_node_library(force_refresh=True)
        after = await client.catalog_status()
        return before, after

    before, after = asyncio.run(run())

    assert len(requests) == 2
    assert after == before


def test_node_details_include_catalog_and_schema_identity():
    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.cache.set(
            {"CustomNode": node_info(python_module="custom_nodes.example")},
            client.source,
        )
        return await client.get_node_details("CustomNode")

    details = asyncio.run(run())

    assert details["origin"] == "custom"
    assert details["source"] == "http://comfy/object_info"
    assert len(details["schema_hash"]) == 64
    assert len(details["catalog_hash"]) == 64


def test_compatible_search_rejects_unknown_output_slot():
    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.cache.set(
            {
                "Source": node_info(),
                "Target": node_info(),
            },
            client.source,
        )
        return await client.find_compatible_nodes(
            "Source",
            direction="downstream",
            output_slot="DOES_NOT_EXIST",
        )

    assert asyncio.run(run()) == []


def test_node_library_clients_are_isolated_by_server_and_timeout(monkeypatch):
    monkeypatch.setattr(node_library, "_node_library_clients", {})

    first = get_node_library_client("http://first/", timeout=5)
    same = get_node_library_client("http://first", timeout=5)
    other_server = get_node_library_client("http://second", timeout=5)
    other_timeout = get_node_library_client("http://first", timeout=10)

    assert first is same
    assert first is not other_server
    assert first is not other_timeout
