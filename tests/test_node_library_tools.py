from types import SimpleNamespace

import mcp_server
import pytest
from node_catalog_store import NodeCatalogStore
from node_library import (
    CompatibleNode,
    NodeCatalogSnapshot,
    NodeSearchResult,
    node_schema_hash,
)


def fake_context(node_catalog_store=None):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={
                "client": None,
                "node_catalog_store": node_catalog_store,
            }
        )
    )


def search_result(node_type, score):
    return NodeSearchResult(
        node_type=node_type,
        display_name=node_type,
        category="test",
        description="",
        inputs={},
        outputs=["IMAGE"],
        match_reason="test match",
        origin="custom",
        python_module="custom_nodes.example",
        schema_hash=node_type.lower().ljust(64, "0")[:64],
        score=score,
    )


def compatible(node_type, direction):
    return CompatibleNode(
        node_type=node_type,
        display_name=node_type,
        category="test",
        direction=direction,
        connection={
            "source_output": "IMAGE",
            "target_input": "image",
            "data_type": "IMAGE",
        },
        description="",
    )


class FakeSearchClient:
    def __init__(self):
        self.max_results = None

    async def search_nodes(self, **kwargs):
        self.max_results = kwargs["max_results"]
        return [
            search_result("Exact", 100),
            search_result("Second", 80),
            search_result("Hidden", 70),
        ]

    async def catalog_status(self):
        return {
            "state": "fresh",
            "node_count": 3,
            "catalog_hash": "a" * 64,
        }


@pytest.mark.asyncio
async def test_mcp_search_preserves_provenance_and_exact_truncation(monkeypatch):
    client = FakeSearchClient()
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.node_library_search.fn(
        mcp_server.NodeLibrarySearchRequest(query="example", max_results=2),
        fake_context(),
    )

    assert client.max_results == 3
    assert result["truncated"] is True
    assert [item["node_type"] for item in result["results"]] == ["Exact", "Second"]
    assert result["results"][0]["origin"] == "custom"
    assert result["results"][0]["python_module"] == "custom_nodes.example"
    assert result["results"][0]["output_types"] == ["IMAGE"]
    assert "inputs" not in result["results"][0]
    assert result["compact"] is True
    assert result["schema_details_tool"] == "node_library_get_details"
    assert result["catalog"]["catalog_hash"] == "a" * 64


@pytest.mark.asyncio
async def test_mcp_node_knowledge_search_is_compact_and_discovery_only():
    store = NodeCatalogStore(":memory:")
    catalog = {
        "CustomImageLoader": {
            "display_name": "Custom Image Loader",
            "category": "image/loaders",
            "description": "Load an image from a custom source",
            "python_module": "custom_nodes.example",
            "input": {"required": {}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        }
    }
    try:
        store.reconcile(catalog, source="http://127.0.0.1:8188/object_info")
        result = await mcp_server.node_knowledge_search.fn(
            mcp_server.NodeKnowledgeSearchRequest(query="image loader"),
            fake_context(store),
        )
    finally:
        store.close()

    assert result["ok"] is True
    assert result["discovery_only"] is True
    assert result["build_authority"].startswith("live /object_info")
    assert result["results"][0]["node_type"] == "CustomImageLoader"
    assert result["results"][0]["origin"] == "custom"
    assert "schema" not in result["results"][0]


def test_verified_connection_lessons_are_scoped_to_exact_node_schemas():
    store = NodeCatalogStore(":memory:")
    catalog = {
        "Source": {
            "python_module": "nodes",
            "input": {"required": {}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        },
        "Target": {
            "python_module": "custom_nodes.target",
            "input": {"required": {"image": ["IMAGE", {}]}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        },
    }
    try:
        store.reconcile(catalog, source="object_info")
        plan = {
            "nodes": [
                {
                    "alias": "source",
                    "node_type": "Source",
                    "schema_hash": node_schema_hash("Source", catalog["Source"]),
                },
                {
                    "alias": "target",
                    "node_type": "Target",
                    "schema_hash": node_schema_hash("Target", catalog["Target"]),
                },
            ],
            "connections": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "source_output_index": 0,
                    "target_alias": "target",
                    "target_input": "image",
                }
            ],
        }

        mcp_server._record_verified_connection_lessons(
            store,
            plan=plan,
            plan_hash="a" * 64,
            application_id="verified-application-0001",
        )

        source_lessons = store.get_verified_lessons("Source")
        target_lessons = store.get_verified_lessons("Target")
        assert source_lessons[0]["payload"]["direction"] == "downstream"
        assert target_lessons[0]["payload"]["direction"] == "upstream"
        assert target_lessons[0]["payload"]["target_input"] == "image"
        assert source_lessons[0]["payload"]["source_schema_hash"] == (
            node_schema_hash("Source", catalog["Source"])
        )
        assert target_lessons[0]["payload"]["target_schema_hash"] == (
            node_schema_hash("Target", catalog["Target"])
        )
    finally:
        store.close()


def test_verified_graph_patch_lessons_include_existing_to_new_edges():
    store = NodeCatalogStore(":memory:")
    catalog = {
        "Source": {
            "python_module": "nodes",
            "input": {"required": {}},
            "output": ["FLOAT"],
            "output_name": ["fps"],
        },
        "Target": {
            "python_module": "custom_nodes.target",
            "input": {"required": {"frame_rate": ["FLOAT", {"default": 8.0}]}},
            "output": [],
            "output_name": [],
        },
    }
    try:
        store.reconcile(catalog, source="object_info")
        plan = {
            "assertions": {
                "nodes": [
                    {
                        "ref": {"node_id": 7},
                        "node_type": "Source",
                        "schema_hash": node_schema_hash("Source", catalog["Source"]),
                    }
                ]
            },
            "create_nodes": [
                {
                    "alias": "target",
                    "node_type": "Target",
                    "schema_hash": node_schema_hash("Target", catalog["Target"]),
                }
            ],
            "add_edges": [
                {
                    "source": {
                        "ref": {"node_id": 7},
                        "output_index": 0,
                        "output": "fps",
                        "type": "FLOAT",
                    },
                    "target": {
                        "ref": {"alias": "target"},
                        "input_index": 0,
                        "occurrence_index": 0,
                        "socket_index": None,
                        "input": "frame_rate",
                        "type": "FLOAT",
                        "mode": "convert_widget",
                    },
                }
            ],
        }

        mcp_server._record_verified_graph_patch_lessons(
            store,
            plan=plan,
            patch_hash="b" * 64,
            application_id="verified-graph-patch-0001",
        )

        source = store.get_verified_lessons("Source")[0]["payload"]
        target = store.get_verified_lessons("Target")[0]["payload"]
        assert source["direction"] == "downstream"
        assert target["direction"] == "upstream"
        assert target["target_mode"] == "convert_widget"
        assert target["target_input_index"] == 0
        assert source["source_schema_hash"] == node_schema_hash(
            "Source", catalog["Source"]
        )
        assert target["target_schema_hash"] == node_schema_hash(
            "Target", catalog["Target"]
        )
    finally:
        store.close()


def test_active_verified_lessons_become_internal_capability_priors():
    store = NodeCatalogStore(":memory:")
    catalog = {
        "Converter": {
            "display_name": "Converter",
            "input": {"required": {"source": ["IMAGE"]}},
            "output": ["VIDEO"],
            "output_name": ["video"],
            "python_module": "custom_nodes.converter",
        },
        "SaveVideo": {
            "display_name": "Save Video",
            "input": {"required": {"video": ["VIDEO"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
    }
    try:
        store.reconcile(catalog, source="object_info")
        schema_hash = node_schema_hash("Converter", catalog["Converter"])
        save_hash = node_schema_hash("SaveVideo", catalog["SaveVideo"])
        store.record_verified_lesson(
            "Converter",
            schema_hash,
            "downstream:test",
            {
                "evidence": "atomic_graph_patch_application",
                "source_node_type": "Converter",
                "source_schema_hash": schema_hash,
                "target_node_type": "SaveVideo",
                "target_schema_hash": save_hash,
            },
        )

        lessons = mcp_server._active_verified_capability_lessons(fake_context(store))
        assert len(lessons) == 1
        assert lessons[0].node_type == "Converter"
        assert lessons[0].schema_hash == schema_hash
        assert lessons[0].payload["evidence"] == "atomic_graph_patch_application"
    finally:
        store.close()


class FakeCompatibilityClient:
    def __init__(self, results):
        self.results = results
        self.max_results = None

    async def find_compatible_nodes(self, **kwargs):
        self.max_results = kwargs["max_results"]
        return self.results

    async def catalog_status(self):
        return {"state": "fresh", "node_count": 10, "catalog_hash": "b" * 64}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "expected_truncated"),
    [
        (
            [
                compatible("DownA", "downstream"),
                compatible("DownB", "downstream"),
                compatible("UpA", "upstream"),
                compatible("UpB", "upstream"),
            ],
            False,
        ),
        (
            [
                compatible("DownA", "downstream"),
                compatible("DownB", "downstream"),
                compatible("DownHidden", "downstream"),
                compatible("UpA", "upstream"),
                compatible("UpB", "upstream"),
            ],
            True,
        ),
    ],
)
async def test_mcp_compatible_truncation_is_per_direction(
    monkeypatch,
    results,
    expected_truncated,
):
    client = FakeCompatibilityClient(results)
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.node_library_find_compatible.fn(
        mcp_server.NodeLibraryFindCompatibleRequest(
            node_type="Source",
            direction="both",
            max_results=2,
        ),
        fake_context(),
    )

    assert client.max_results == 3
    assert result["truncated"] is expected_truncated
    assert [item["direction"] for item in result["compatible_nodes"]] == [
        "downstream",
        "downstream",
        "upstream",
        "upstream",
    ]


class FakePlannerClient:
    source = "http://127.0.0.1:8188/object_info"

    def __init__(self):
        self.catalog = {
            "Source": {
                "input": {"required": {}},
                "output": ["IMAGE"],
                "output_name": ["IMAGE"],
                "python_module": "nodes",
            }
        }

    async def catalog_snapshot(self, *, force_refresh=False):
        self.force_refresh = force_refresh
        return NodeCatalogSnapshot(
            data=self.catalog,
            source=self.source,
            catalog_hash="c" * 64,
            observed_catalog_hash="d" * 64,
            catalog_hash_schema="test",
            fetched_at=1.0,
            expires_at=2.0,
        )


@pytest.mark.asyncio
async def test_mcp_plan_workflow_is_a_read_only_catalog_pinned_dry_run(monkeypatch):
    client = FakePlannerClient()
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.plan_workflow.fn(
        mcp_server.PlanWorkflowRequest(
            nodes=[{"alias": "source", "node_type": "Source"}],
            expected_catalog_hash="c" * 64,
        ),
        fake_context(),
    )

    assert result["valid"] is True
    assert client.force_refresh is True
    assert result["catalog"]["catalog_hash"] == "c" * 64
    assert result["plan"]["nodes"][0]["schema_hash"]


@pytest.mark.asyncio
async def test_mcp_resolves_semantic_roles_against_one_catalog_snapshot(monkeypatch):
    client = FakePlannerClient()
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.resolve_workflow_spec.fn(
        mcp_server.ResolveWorkflowSpecRequest(
            capabilities=[
                {
                    "alias": "source",
                    "capability": "source image",
                    "requested_node_type": "Source",
                    "required_output_types": ["IMAGE"],
                }
            ],
            expected_catalog_hash="c" * 64,
        ),
        fake_context(),
    )

    assert result["valid"] is True
    assert client.force_refresh is True
    assert result["selected_node_types"] == {"source": "Source"}
    assert result["catalog"]["catalog_hash"] == "c" * 64
    assert result["resolution_hash"]


def test_empty_attachment_validation_does_not_resolve_comfy_installation(monkeypatch):
    def fail_if_called():
        raise AssertionError("ComfyUI discovery must not run without attachments")

    monkeypatch.setattr(mcp_server, "get_comfy_tools", fail_if_called)
    request = mcp_server.PlanWorkflowRequest(
        nodes=[{"alias": "source", "node_type": "Source"}],
    )

    assert mcp_server._validated_plan_attachment_values(request) == {}


@pytest.mark.asyncio
async def test_mcp_compiles_semantic_workflow_into_ready_apply_request(monkeypatch):
    client = FakePlannerClient()
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.compile_workflow_spec.fn(
        mcp_server.CompileWorkflowSpecRequest(
            application_id="semantic-application-0001",
            nodes=[
                {
                    "alias": "source",
                    "capability": "source image",
                    "requested_node_type": "Source",
                    "required_output_types": ["IMAGE"],
                }
            ],
            expected_catalog_hash="c" * 64,
        ),
        fake_context(),
    )

    assert result["valid"] is True
    assert result["selected_node_types"] == {"source": "Source"}
    assert result["apply_request"]["plan_hash"] == result["plan_hash"]
    assert result["apply_request"]["application_id"] == "semantic-application-0001"
    assert result["partner_review"]["web_lookup_required"] is False


def _valid_apply_request(client, *, plan_hash=None, catalog_hash="c" * 64):
    plan_request = mcp_server.PlanWorkflowRequest(
        nodes=[{"alias": "source", "node_type": "Source"}],
        expected_catalog_hash="c" * 64,
    )
    compiled = mcp_server.compile_workflow_plan(
        plan_request,
        client.catalog,
        catalog_hash="c" * 64,
        source=client.source,
    )
    return mcp_server.ApplyWorkflowPlanRequest(
        nodes=plan_request.nodes,
        connections=[],
        expected_catalog_hash=catalog_hash,
        plan_hash=plan_hash or compiled["plan_hash"],
        application_id="atomic-application-0001",
    )


@pytest.mark.asyncio
async def test_mcp_apply_recompiles_then_sends_one_canonical_frontend_transaction(
    monkeypatch,
):
    client = FakePlannerClient()
    captured = {}

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        captured.update(name=name, payload=payload, timeout_ms=timeout_ms)
        return {
            "success": True,
            "applied": True,
            "already_applied": False,
            "aliases": {"source": 41},
            "node_count": 1,
            "connection_count": 0,
            "queued": False,
        }

    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    request = _valid_apply_request(client)

    result = await mcp_server.apply_workflow_plan.fn(request, fake_context())

    assert result["success"] is True
    assert result["validation"]["valid"] is True
    assert captured["name"] == "apply_workflow_plan"
    assert captured["timeout_ms"] == 60000
    assert captured["payload"]["application_id"] == "atomic-application-0001"
    assert captured["payload"]["plan_hash"] == request.plan_hash
    assert captured["payload"]["plan"]["nodes"][0]["alias"] == "source"


@pytest.mark.asyncio
async def test_mcp_apply_rejects_plan_hash_mismatch_before_frontend_mutation(monkeypatch):
    client = FakePlannerClient()

    async def unexpected_execute(*args, **kwargs):
        raise AssertionError("frontend must not be called")

    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)
    monkeypatch.setattr(mcp_server, "_execute_tool", unexpected_execute)
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_plan.fn(
        _valid_apply_request(client, plan_hash="f" * 64),
        fake_context(),
    )

    assert result["success"] is False
    assert result["applied"] is False
    assert result["error"]["code"] == "plan_hash_mismatch"
    assert result["validation"]["valid"] is True
    assert result["rollback"]["attempted"] is False


@pytest.mark.asyncio
async def test_mcp_apply_rejects_changed_catalog_before_frontend_mutation(monkeypatch):
    client = FakePlannerClient()

    async def unexpected_execute(*args, **kwargs):
        raise AssertionError("frontend must not be called")

    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)
    monkeypatch.setattr(mcp_server, "_execute_tool", unexpected_execute)
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_plan.fn(
        _valid_apply_request(client, catalog_hash="e" * 64),
        fake_context(),
    )

    assert result["success"] is False
    assert result["error"]["code"] == "plan_invalid"
    assert result["validation"]["valid"] is False
    assert result["rollback"]["attempted"] is False
    assert any(
        item["code"] == "catalog_changed"
        for item in result["validation"]["issues"]
    )
