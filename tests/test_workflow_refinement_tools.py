from types import SimpleNamespace

import mcp_server
import pytest
from node_library import NodeCatalogSnapshot
from workflow_refinement import (
    GRAPH_PRECONDITION_HASH_SCHEMA,
    PlanWorkflowRefinementRequest,
    WORKFLOW_IDENTITY_SCHEMA,
    compile_workflow_refinement,
    normalize_workflow_graph,
)


GRAPH_HASH = "a" * 64
CATALOG_HASH = "b" * 64
WORKFLOW_IDENTITY = "fl-mcp-workflow-1"


def fake_context():
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"client": None, "node_catalog_store": None}
        )
    )


def catalog():
    return {
        "Source": {
            "python_module": "nodes",
            "input": {"required": {}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        },
        "Processor": {
            "python_module": "custom_nodes.processor",
            "input": {
                "required": {
                    "image": ["IMAGE", {}],
                    "strength": [
                        "FLOAT",
                        {"default": 0.5, "min": 0.0, "max": 1.0},
                    ],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        },
        "Sink": {
            "python_module": "nodes",
            "input": {"required": {"image": ["IMAGE", {}]}},
            "output": [],
            "output_name": [],
        },
    }


def workflow(*, parallel=False):
    source_outputs = [{"name": "IMAGE", "type": "IMAGE", "links": [7]}]
    sink_inputs = [{"name": "image", "type": "IMAGE", "link": 7}]
    links = [[7, 1, 0, 3, 0, "IMAGE"]]
    if parallel:
        source_outputs.append({"name": "ALT", "type": "IMAGE", "links": [8]})
        sink_inputs.append({"name": "alt", "type": "IMAGE", "link": 8})
        links.append([8, 1, 1, 3, 1, "IMAGE"])
    return {
        "version": 0.4,
        "last_node_id": 3,
        "last_link_id": 8 if parallel else 7,
        "nodes": [
            {
                "id": 1,
                "type": "Source",
                "pos": [0, 0],
                "size": [200, 100],
                "inputs": [],
                "outputs": source_outputs,
                "widgets_values": [],
            },
            {
                "id": 3,
                "type": "Sink",
                "pos": [500, 0],
                "size": [200, 100],
                "inputs": sink_inputs,
                "outputs": [],
                "widgets_values": [],
            },
        ],
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
    }


def replacement_node():
    return {
        "alias": "processor",
        "node_type": "Processor",
        "values": {"strength": 0.5},
        "chain_input": "image",
        "chain_output": "IMAGE",
    }


class FakeCatalogClient:
    source = "http://127.0.0.1:8188/object_info"

    def __init__(self, catalog_hash=CATALOG_HASH):
        self.catalog_hash = catalog_hash

    async def catalog_snapshot(self, *, force_refresh=False):
        assert force_refresh is True
        return NodeCatalogSnapshot(
            data=catalog(),
            source=self.source,
            catalog_hash=self.catalog_hash,
            observed_catalog_hash=self.catalog_hash,
            catalog_hash_schema="fl-mcp.comfy-node-catalog-contract.v1",
            fetched_at=1.0,
            expires_at=2.0,
        )


def compiled_apply_request():
    graph = normalize_workflow_graph(workflow())
    planned = compile_workflow_refinement(
        PlanWorkflowRefinementRequest.model_validate(
            {
                "application_id": "refinement-tool-test-0001",
                "expected_workflow_identity": WORKFLOW_IDENTITY,
                "expected_graph_hash": GRAPH_HASH,
                "graph": graph.model_dump(mode="json"),
                "expected_path": {
                    "edges": [graph.edges[0].model_dump(mode="json")]
                },
                "replacement_nodes": [replacement_node()],
                "expected_catalog_hash": CATALOG_HASH,
            }
        ),
        catalog(),
        catalog_hash=CATALOG_HASH,
        source=FakeCatalogClient.source,
    )
    assert planned["valid"] is True
    return mcp_server.ApplyWorkflowRefinementRequest.model_validate(
        planned["apply_request"]
    )


@pytest.mark.asyncio
async def test_plan_refinement_reads_graph_once_and_returns_ready_apply_request(
    monkeypatch,
):
    calls = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append((name, payload, timeout_ms))
        assert name == "workflow_get_current_json"
        return {
            "api_format": False,
            "workflow": workflow(),
            "workflow_identity": WORKFLOW_IDENTITY,
            "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
            "graph_hash": GRAPH_HASH,
            "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: FakeCatalogClient(),
    )

    result = await mcp_server.plan_workflow_refinement.fn(
        mcp_server.PlanCurrentWorkflowRefinementRequest(
            application_id="refinement-tool-test-0001",
            path_node_ids=[1, 3],
            replacement_nodes=[replacement_node()],
        ),
        fake_context(),
    )

    assert result["valid"] is True
    assert result["operation"] == "insert"
    assert result["apply_request"]["expected_catalog_hash"] == CATALOG_HASH
    assert result["plan"]["expected_workflow_identity"] == WORKFLOW_IDENTITY
    assert result["plan"]["expected_graph_hash"] == GRAPH_HASH
    assert [item[0] for item in calls] == ["workflow_get_current_json"]


@pytest.mark.asyncio
async def test_plan_refinement_rejects_ambiguous_parallel_node_pair(monkeypatch):
    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        return {
            "workflow": workflow(parallel=True),
            "workflow_identity": WORKFLOW_IDENTITY,
            "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
            "graph_hash": GRAPH_HASH,
            "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: pytest.fail("catalog must not be read for an ambiguous path"),
    )

    result = await mcp_server.plan_workflow_refinement.fn(
        mcp_server.PlanCurrentWorkflowRefinementRequest(
            application_id="refinement-tool-test-0002",
            path_node_ids=[1, 3],
            replacement_nodes=[replacement_node()],
        ),
        fake_context(),
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert result["issues"][0]["code"] == "path_edge_ambiguous"


@pytest.mark.asyncio
async def test_apply_refinement_recompiles_and_sends_only_frontend_envelope(
    monkeypatch,
):
    request = compiled_apply_request()
    calls = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append((name, payload, timeout_ms))
        if name == "workflow_get_current_json":
            return {
                "workflow": workflow(),
                "workflow_identity": WORKFLOW_IDENTITY,
                "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
                "graph_hash": GRAPH_HASH,
                "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
            }
        assert name == "apply_workflow_refinement"
        return {
            "success": True,
            "applied": True,
            "already_applied": False,
            "operation": "insert",
            "queued": False,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: FakeCatalogClient(),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_refinement.fn(request, fake_context())

    assert result["success"] is True
    assert result["validation"]["valid"] is True
    assert [call[0] for call in calls] == [
        "workflow_get_current_json",
        "apply_workflow_refinement",
    ]
    frontend_payload = calls[-1][1]
    assert set(frontend_payload) == {"application_id", "refinement_hash", "plan"}
    assert frontend_payload["refinement_hash"] == request.refinement_hash
    assert calls[-1][2] == 240000


@pytest.mark.asyncio
async def test_apply_refinement_rejects_an_identical_different_workflow_tab(monkeypatch):
    request = compiled_apply_request()

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        assert name == "workflow_get_current_json"
        return {
            "workflow": workflow(),
            "workflow_identity": "fl-mcp-workflow-2",
            "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
            "graph_hash": GRAPH_HASH,
            "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: pytest.fail("catalog must not be read for a different tab"),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_refinement.fn(request, fake_context())

    assert result["success"] is False
    assert result["error"]["code"] == "workflow_identity_changed"
    assert result["validation"]["graph"]["state"] == "different_workflow"


@pytest.mark.asyncio
async def test_apply_refinement_reports_a_busy_canvas_as_retryable(monkeypatch):
    request = compiled_apply_request()

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        if name == "workflow_get_current_json":
            return {
                "workflow": workflow(),
                "workflow_identity": WORKFLOW_IDENTITY,
                "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
                "graph_hash": GRAPH_HASH,
                "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
            }
        raise mcp_server.FrontendToolExecutionError(
            "Another canvas mutation is already active.",
            code="canvas_mutation_busy",
            details={"retryable": True},
        )

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: FakeCatalogClient(),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_refinement.fn(request, fake_context())

    assert result["success"] is False
    assert result["error"]["code"] == "canvas_mutation_busy"
    assert result["validation"]["retryable"] is True
    assert result["rollback"]["attempted"] is False


@pytest.mark.asyncio
async def test_apply_refinement_rejects_changed_catalog_before_mutation(monkeypatch):
    request = compiled_apply_request()

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        assert name == "workflow_get_current_json"
        return {
            "workflow": workflow(),
            "workflow_identity": WORKFLOW_IDENTITY,
            "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
            "graph_hash": GRAPH_HASH,
            "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: FakeCatalogClient("c" * 64),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_refinement.fn(request, fake_context())

    assert result["success"] is False
    assert result["error"]["code"] == "refinement_invalid"
    assert any(
        issue["code"] == "catalog_changed"
        for issue in result["validation"]["issues"]
    )


@pytest.mark.asyncio
async def test_apply_refinement_allows_frontend_ledger_to_confirm_safe_retry(
    monkeypatch,
):
    request = compiled_apply_request()
    calls = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append(name)
        if name == "workflow_get_current_json":
            return {
                "workflow": workflow(),
                "workflow_identity": WORKFLOW_IDENTITY,
                "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
                "graph_hash": "d" * 64,
                "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
            }
        assert name == "apply_workflow_refinement"
        return {
            "success": True,
            "applied": False,
            "already_applied": True,
            "operation": "insert",
            "queued": False,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_refinement.fn(request, fake_context())

    assert result["success"] is True
    assert result["already_applied"] is True
    assert result["validation"]["valid"] is True
    assert calls == ["workflow_get_current_json", "apply_workflow_refinement"]
