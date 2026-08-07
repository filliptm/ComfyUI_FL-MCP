from types import SimpleNamespace

import mcp_server
import pytest
from node_library import CompatibleNode, NodeCatalogSnapshot, NodeSearchResult


def fake_context():
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"client": None})
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
    assert result["catalog"]["catalog_hash"] == "a" * 64


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

    async def catalog_snapshot(self):
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
    assert result["selected_node_types"] == {"source": "Source"}
    assert result["catalog"]["catalog_hash"] == "c" * 64
    assert result["resolution_hash"]


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
