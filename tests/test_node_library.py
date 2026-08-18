import asyncio

import httpx
import pytest

from backend import node_library
from backend.node_library import (
    CATALOG_HASH_SCHEMA,
    NODE_SCHEMA_HASH_SCHEMA,
    NodeLibraryClient,
    NodeLibraryConnectionError,
    canonical_schema_hash,
    catalog_contract_hash,
    catalog_origin_counts,
    classify_node_origin,
    get_node_library_client,
    node_schema_hash,
    normalize_node_schema_contract,
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


class FakeCatalogPersistence:
    def __init__(self, *, reconcile_error=None):
        self.reconcile_error = reconcile_error
        self.reconciliations = []
        self.failures = []
        self.status_payload = {"state": "empty", "node_count": 0}

    def reconcile(self, catalog, **metadata):
        if self.reconcile_error is not None:
            raise self.reconcile_error
        self.reconciliations.append((catalog, metadata))
        self.status_payload = {
            "state": "fresh",
            "node_count": len(catalog),
            "catalog_hash": metadata["catalog_hash"],
        }

    def record_refresh_failure(self, error):
        self.failures.append(error)
        self.status_payload = {
            **self.status_payload,
            "state": "stale",
            "last_error": error,
        }

    def status(self, *, max_age_seconds=None):
        return {**self.status_payload, "max_age_seconds": max_age_seconds}


def test_catalog_hash_ignores_object_info_key_order():
    first = {"NodeB": {"output": ["IMAGE"]}, "NodeA": {"input": {"required": {}}}}
    second = {"NodeA": {"input": {"required": {}}}, "NodeB": {"output": ["IMAGE"]}}

    assert canonical_schema_hash(first) == canonical_schema_hash(second)
    assert catalog_contract_hash(first) == catalog_contract_hash(second)


def test_contract_hash_normalizes_only_widget_default_values():
    first = {
        "Cache Node": node_info(),
    }
    first["Cache Node"]["input"]["required"]["image"][1].update(
        {
            "default": "19327504_cache",
            "min": 1,
        }
    )
    second = {
        "Cache Node": node_info(),
    }
    second["Cache Node"]["input"]["required"]["image"][1].update(
        {
            "default": "83356673_cache",
            "min": 1,
        }
    )

    assert canonical_schema_hash(first) != canonical_schema_hash(second)
    assert catalog_contract_hash(first) == catalog_contract_hash(second)

    normalized = normalize_node_schema_contract(first["Cache Node"])
    assert normalized["input"]["required"]["image"][1]["default"] == {
        "$contract": "widget-default",
        "json_type": "string",
    }
    assert first["Cache Node"]["input"]["required"]["image"][1]["default"] == (
        "19327504_cache"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda schema: schema["input"]["required"]["image"][1].update({"min": 2}),
        lambda schema: schema["input"]["required"]["image"].__setitem__(0, "LATENT"),
        lambda schema: schema["output"].__setitem__(0, "LATENT"),
        lambda schema: schema.update({"python_module": "custom_nodes.changed"}),
        lambda schema: schema["input"]["required"]["image"][1].update({"default": 1}),
        lambda schema: schema["input"]["required"]["image"][1].pop("default"),
    ],
)
def test_contract_hash_keeps_meaningful_schema_identity(change):
    base = node_info()
    base["input"]["required"]["image"][1].update(
        {
            "default": "first.png",
            "min": 1,
        }
    )
    changed = node_info()
    changed["input"]["required"]["image"][1].update(
        {
            "default": "second.png",
            "min": 1,
        }
    )
    change(changed)

    assert node_schema_hash("ExampleNode", base) != node_schema_hash(
        "ExampleNode", changed
    )


def test_contract_hash_preserves_enum_choices():
    first = node_info()
    first["input"]["required"]["mode"] = [
        ["fast", "quality"],
        {"default": "fast"},
    ]
    second = node_info()
    second["input"]["required"]["mode"] = [
        ["fast", "quality", "ultra"],
        {"default": "quality"},
    ]

    assert node_schema_hash("ExampleNode", first) != node_schema_hash(
        "ExampleNode", second
    )


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


def test_catalog_origin_counts_include_every_loaded_class():
    catalog = {
        "Native": node_info(python_module="nodes"),
        "Partner": node_info(
            python_module="comfy_api_nodes.nodes_gemini",
            category="partner/image/Gemini",
        ),
        "Custom": node_info(python_module="custom_nodes.comfyui-kjnodes"),
        "Unknown": node_info(python_module="vendor.nodes"),
        "Malformed": None,
    }

    assert catalog_origin_counts(catalog) == {
        "native": 1,
        "partner": 1,
        "custom": 1,
        "unknown": 2,
    }


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
    assert empty["origin_counts"] == catalog_origin_counts({})
    assert empty["catalog_hash_schema"] == CATALOG_HASH_SCHEMA
    assert empty["observed_catalog_hash"] is None
    assert fresh["state"] == "fresh"
    assert fresh["node_count"] == 1
    assert fresh["origin_counts"] == {
        "native": 1,
        "partner": 0,
        "custom": 0,
        "unknown": 0,
    }
    assert fresh["catalog_hash_schema"] == CATALOG_HASH_SCHEMA
    assert len(fresh["observed_catalog_hash"]) == 64
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


def test_max_age_seconds_serves_the_cache_within_its_own_short_horizon(monkeypatch):
    first = {"KSampler": node_info(display_name="First")}
    second = {"KSampler": node_info(display_name="Second")}
    requests = install_fake_http(monkeypatch, [(200, first), (200, second)])
    now = [100.0]

    async def run():
        client = NodeLibraryClient("http://comfy", cache_ttl=300, clock=lambda: now[0])
        initial = await client.fetch_node_library(max_age_seconds=3.0)
        now[0] = 102.0
        within_horizon = await client.fetch_node_library(max_age_seconds=3.0)
        return initial, within_horizon

    initial, within_horizon = asyncio.run(run())

    assert initial == within_horizon == first
    assert requests == ["http://comfy/object_info"]


def test_max_age_seconds_refetches_once_its_own_horizon_elapses(monkeypatch):
    first = {"KSampler": node_info(display_name="First")}
    second = {"KSampler": node_info(display_name="Second")}
    requests = install_fake_http(monkeypatch, [(200, first), (200, second)])
    now = [100.0]

    async def run():
        client = NodeLibraryClient("http://comfy", cache_ttl=300, clock=lambda: now[0])
        initial = await client.fetch_node_library(max_age_seconds=3.0)
        now[0] = 104.0
        past_horizon = await client.fetch_node_library(max_age_seconds=3.0)
        return initial, past_horizon

    initial, past_horizon = asyncio.run(run())

    assert initial == first
    assert past_horizon == second
    assert requests == ["http://comfy/object_info", "http://comfy/object_info"]


def test_bound_persistence_reconciles_only_fresh_http_generations(monkeypatch):
    first = {"KSampler": node_info(display_name="First")}
    second = {"KSampler": node_info(display_name="Second")}
    install_fake_http(monkeypatch, [(200, first), (200, second)])
    persistence = FakeCatalogPersistence()

    async def run():
        client = NodeLibraryClient("http://comfy")
        client.bind_persistence(persistence)
        await client.fetch_node_library()
        await client.fetch_node_library()
        await client.fetch_node_library(force_refresh=True)
        return await client.catalog_status(), await client.persisted_catalog_status()

    live_status, persisted_status = asyncio.run(run())

    assert len(persistence.reconciliations) == 2
    for catalog, metadata in persistence.reconciliations:
        assert metadata["source"] == "http://comfy/object_info"
        assert metadata["catalog_hash"] == catalog_contract_hash(catalog)
        assert metadata["observed_catalog_hash"] == canonical_schema_hash(catalog)
        assert metadata["node_schema_hashes"] == {
            node_type: node_schema_hash(node_type, info) for node_type, info in catalog.items()
        }
    assert "persistence" not in live_status
    assert persisted_status["state"] == "fresh"
    assert persisted_status["catalog_hash"] == catalog_contract_hash(second)


def test_bind_and_conditional_unbind_are_explicit():
    client = NodeLibraryClient("http://comfy")
    first = FakeCatalogPersistence()
    replacement = FakeCatalogPersistence()

    client.bind_persistence(first)
    assert client.unbind_persistence(replacement) is False
    assert asyncio.run(client.persisted_catalog_status()) is not None
    assert client.unbind_persistence(first) is True
    assert asyncio.run(client.persisted_catalog_status()) is None
    assert client.unbind_persistence() is False

    with pytest.raises(TypeError, match="must provide"):
        client.bind_persistence(object())


def test_persistence_reconcile_failure_never_invalidates_fresh_live_catalog(monkeypatch):
    catalog = {"KSampler": node_info()}
    install_fake_http(monkeypatch, [(200, catalog)])
    persistence = FakeCatalogPersistence(reconcile_error=RuntimeError("disk full"))

    async def run():
        client = NodeLibraryClient("http://comfy")
        client.bind_persistence(persistence)
        fetched = await client.fetch_node_library()
        cached = await client.fetch_node_library()
        snapshot = await client.catalog_snapshot()
        return fetched, cached, snapshot

    fetched, cached, snapshot = asyncio.run(run())

    assert fetched == cached == snapshot.data == catalog
    assert snapshot.catalog_hash == catalog_contract_hash(catalog)
    assert persistence.failures == ["Persistent catalog reconciliation failed: disk full"]


def test_failed_live_refresh_records_stale_sidecar_but_never_reads_it(monkeypatch):
    install_fake_http(monkeypatch, [(500, "boom")])
    persistence = FakeCatalogPersistence()
    persistence.status_payload = {
        "state": "fresh",
        "node_count": 1,
        "snapshot": {"PersistedOnly": node_info()},
    }

    async def run():
        client = NodeLibraryClient("http://comfy")
        client.bind_persistence(persistence)
        with pytest.raises(NodeLibraryConnectionError):
            await client.fetch_node_library()
        return await client.cache.snapshot(), await client.persisted_catalog_status()

    live_snapshot, persisted_status = asyncio.run(run())

    assert live_snapshot is None
    assert persistence.reconciliations == []
    assert persistence.failures == ["ComfyUI server error: 500"]
    assert persisted_status["state"] == "stale"
    assert "PersistedOnly" in persisted_status["snapshot"]


def test_catalog_snapshot_binds_data_source_and_hash_to_one_generation(monkeypatch):
    catalog = {"KSampler": node_info(display_name="Pinned")}
    install_fake_http(monkeypatch, [(200, catalog)])

    async def run():
        client = NodeLibraryClient("http://comfy")
        return await client.catalog_snapshot()

    snapshot = asyncio.run(run())

    assert snapshot.data == catalog
    assert snapshot.source == "http://comfy/object_info"
    assert snapshot.catalog_hash == catalog_contract_hash(snapshot.data)


def test_refresh_keeps_contract_hash_stable_for_runtime_widget_defaults(monkeypatch):
    first = node_info()
    first["input"]["required"]["image"][1]["default"] = "19327504_cache"
    second = node_info()
    second["input"]["required"]["image"][1]["default"] = "83356673_cache"
    install_fake_http(
        monkeypatch,
        [(200, {"Cache Node": first}), (200, {"Cache Node": second})],
    )

    async def run():
        client = NodeLibraryClient("http://comfy")
        await client.fetch_node_library()
        initial = await client.catalog_status()
        await client.fetch_node_library(force_refresh=True)
        refreshed = await client.catalog_status()
        return initial, refreshed

    initial, refreshed = asyncio.run(run())

    assert refreshed["catalog_hash"] == initial["catalog_hash"]
    assert refreshed["observed_catalog_hash"] != initial["observed_catalog_hash"]
    assert refreshed["catalog_hash_schema"] == CATALOG_HASH_SCHEMA


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
    assert details["schema_hash_schema"] == NODE_SCHEMA_HASH_SCHEMA
    assert len(details["catalog_hash"]) == 64
    assert details["catalog_hash_schema"] == CATALOG_HASH_SCHEMA
    assert len(details["observed_catalog_hash"]) == 64


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
