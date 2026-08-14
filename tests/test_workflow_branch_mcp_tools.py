from types import SimpleNamespace

import mcp_server
import pytest
from node_library import NodeCatalogSnapshot, catalog_contract_hash
from workflow_branch_operations import (
    RemoveBranchRequest,
    ResolveBranchSuccessorsRequest,
)
from workflow_branch_queries import CompareWorkflowBranchesRequest
from workflow_branch_tools import (
    DiscoverWorkflowBranchesRequest,
    NavigateWorkflowBranchRequest,
)

WORKFLOW_IDENTITY = "fl-mcp-workflow:branch-mcp-tools:1"
GRAPH_HASH = "a" * 64


def _context():
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"client": None, "node_catalog_store": None}
        )
    )


def _catalog() -> dict:
    return {
        "BranchSource": {
            "display_name": "Branch Source",
            "category": "test",
            "input": {"required": {}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "nodes",
        },
        "BranchSink": {
            "display_name": "Branch Sink",
            "category": "test",
            "input": {"required": {"image": ["IMAGE"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
        },
    }


def _workflow() -> dict:
    return {
        "version": 0.4,
        "last_node_id": 3,
        "last_link_id": 2,
        "nodes": [
            {
                "id": 1,
                "type": "BranchSource",
                "title": "Split",
                "pos": [0, 0],
                "size": [180, 80],
                "widgets_values": [],
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [1, 2]}
                ],
            },
            {
                "id": 2,
                "type": "BranchSink",
                "title": "Hero Upscale",
                "pos": [300, -100],
                "size": [180, 80],
                "widgets_values": [],
                "inputs": [{"name": "image", "type": "IMAGE", "link": 1}],
                "outputs": [],
            },
            {
                "id": 3,
                "type": "BranchSink",
                "title": "Preview Output",
                "pos": [300, 100],
                "size": [180, 80],
                "widgets_values": [],
                "inputs": [{"name": "image", "type": "IMAGE", "link": 2}],
                "outputs": [],
            },
        ],
        "links": [
            [1, 1, 0, 2, 0, "IMAGE"],
            [2, 1, 0, 3, 0, "IMAGE"],
        ],
        "groups": [],
        "config": {},
        "extra": {},
        "definitions": {"subgraphs": []},
    }


def _active() -> dict:
    return {
        "workflow": _workflow(),
        "workflow_identity": WORKFLOW_IDENTITY,
        "graph_hash": GRAPH_HASH,
    }


def _snapshot() -> NodeCatalogSnapshot:
    catalog = _catalog()
    return NodeCatalogSnapshot(
        data=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="test",
        observed_catalog_hash=catalog_contract_hash(catalog),
        catalog_hash_schema="fl-mcp.comfy-node-catalog-contract.v1",
        fetched_at=1.0,
        expires_at=2.0,
    )


@pytest.fixture
def branch_tool_environment(monkeypatch):
    async def active(_ctx):
        return _active()

    snapshot = _snapshot()
    client = SimpleNamespace(catalog_snapshot=lambda **_kwargs: snapshot)

    async def catalog_snapshot(**_kwargs):
        return snapshot

    client.catalog_snapshot = catalog_snapshot
    monkeypatch.setattr(mcp_server, "_active_editable_workflow", active)
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **_kwargs: client)
    return snapshot


@pytest.mark.asyncio
async def test_discover_compare_and_compile_are_read_only_and_share_exact_pins(
    branch_tool_environment,
):
    discovered = await mcp_server.workflow_branches_discover.fn(
        DiscoverWorkflowBranchesRequest(query="hero upscale", kinds=["split_arm"]),
        _context(),
    )
    assert discovered["valid"] is True
    assert discovered["resolution"]["status"] == "resolved"
    selected = discovered["selected_branch"]
    assert selected["branch_id"] == discovered["resolution"]["selected"]["branch_id"]
    assert selected["entry_edges"]

    listed = await mcp_server.workflow_branches_discover.fn(
        DiscoverWorkflowBranchesRequest(),
        _context(),
    )
    branch_ids = [item["branch_id"] for item in listed["resolution"]["candidates"]]
    compared = await mcp_server.workflow_branch_compare.fn(
        CompareWorkflowBranchesRequest(
            expected_workflow_identity=WORKFLOW_IDENTITY,
            expected_graph_hash=GRAPH_HASH,
            expected_branch_catalog_hash=listed["branch_catalog_hash"],
            left_branch_id=branch_ids[0],
            right_branch_id=branch_ids[1],
        ),
        _context(),
    )
    assert compared["status"] == "compared"
    assert compared["queued"] is False

    compiled = await mcp_server.compile_workflow_branch_operation.fn(
        RemoveBranchRequest(
            application_id="branch-remove-mcp-0001",
            branch_id=selected["branch_id"],
            expected_workflow_identity=WORKFLOW_IDENTITY,
            expected_graph_hash=GRAPH_HASH,
            expected_branch_catalog_hash=discovered["branch_catalog_hash"],
            mode="delete",
        ),
        _context(),
    )
    assert compiled["valid"] is True
    assert compiled["apply_request"] is not None
    assert compiled["queued"] is False


@pytest.mark.asyncio
async def test_navigation_dispatches_only_one_exact_backend_resolved_payload(
    branch_tool_environment,
    monkeypatch,
):
    discovered = await mcp_server.workflow_branches_discover.fn(
        DiscoverWorkflowBranchesRequest(query="hero upscale", kinds=["split_arm"]),
        _context(),
    )
    branch = discovered["selected_branch"]
    calls = []

    async def execute(_ctx, name, parameters, **_kwargs):
        calls.append((name, parameters))
        return {
            "branch_id": parameters["branch_id"],
            "workflow_identity": parameters["expected_workflow_identity"],
            "graph_hash": parameters["expected_graph_hash"],
            "scope_path": parameters["scope_path"],
            "scope_graph_id": "root",
            "selected_node_ids": parameters["node_ids"],
            "selected_count": len(parameters["node_ids"]),
            "fitted_count": len(parameters["node_ids"]),
            "fit_method": "native",
            "queued": False,
        }

    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute)
    result = await mcp_server.workflow_branch_navigate.fn(
        NavigateWorkflowBranchRequest(
            branch_id=branch["branch_id"],
            expected_workflow_identity=WORKFLOW_IDENTITY,
            expected_graph_hash=GRAPH_HASH,
            expected_branch_catalog_hash=discovered["branch_catalog_hash"],
        ),
        _context(),
    )

    assert result["success"] is result["navigated"] is True
    assert result["queued"] is False
    assert calls == [
        (
            "navigate_workflow_branch",
            {
                "branch_id": branch["branch_id"],
                "expected_workflow_identity": WORKFLOW_IDENTITY,
                "expected_graph_hash": GRAPH_HASH,
                "scope_path": [],
                "node_ids": branch["selectable_node_ids"],
            },
        )
    ]


@pytest.mark.asyncio
async def test_stale_navigation_never_reaches_the_browser(
    branch_tool_environment,
    monkeypatch,
):
    discovered = await mcp_server.workflow_branches_discover.fn(
        DiscoverWorkflowBranchesRequest(query="hero upscale", kinds=["split_arm"]),
        _context(),
    )
    calls = []

    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("stale navigation must not dispatch")

    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute)
    result = await mcp_server.workflow_branch_navigate.fn(
        NavigateWorkflowBranchRequest(
            branch_id=discovered["selected_branch"]["branch_id"],
            expected_workflow_identity=WORKFLOW_IDENTITY,
            expected_graph_hash="b" * 64,
            expected_branch_catalog_hash=discovered["branch_catalog_hash"],
        ),
        _context(),
    )

    assert result["success"] is result["selection_changed"] is False
    assert result["error"]["code"] == "branch_navigation_stale"
    assert calls == []


async def _compiled_branch_remove_request():
    discovered = await mcp_server.workflow_branches_discover.fn(
        DiscoverWorkflowBranchesRequest(query="hero upscale", kinds=["split_arm"]),
        _context(),
    )
    compiled = await mcp_server.compile_workflow_branch_operation.fn(
        RemoveBranchRequest(
            application_id="branch-successor-mcp-0001",
            branch_id=discovered["selected_branch"]["branch_id"],
            expected_workflow_identity=WORKFLOW_IDENTITY,
            expected_graph_hash=GRAPH_HASH,
            expected_branch_catalog_hash=discovered["branch_catalog_hash"],
            mode="delete",
        ),
        _context(),
    )
    assert compiled["valid"], compiled["issues"]
    post = _workflow()
    post["nodes"] = [item for item in post["nodes"] if item["id"] != 2]
    post["links"] = [item for item in post["links"] if item[3] != 2]
    request = ResolveBranchSuccessorsRequest(
        apply_request=compiled["apply_request"],
        pending_successor_locator=compiled["pending_successor_locator"],
        expected_workflow_identity=WORKFLOW_IDENTITY,
        expected_graph_hash="b" * 64,
        aliases={},
    )
    return request, post


@pytest.mark.asyncio
async def test_successor_tool_requires_attested_completion_and_never_dispatches(
    branch_tool_environment,
    monkeypatch,
):
    request, post = await _compiled_branch_remove_request()

    async def active(_ctx):
        return {
            "workflow": post,
            "workflow_identity": WORKFLOW_IDENTITY,
            "graph_hash": "b" * 64,
        }

    def completed(_active, apply_request, *, catalog):
        assert apply_request == request.apply_request
        assert catalog == _catalog()
        return {
            "success": True,
            "applied": False,
            "already_applied": True,
            "application_id": apply_request.application_id,
            "patch_hash": apply_request.patch_hash,
            "workflow_identity": WORKFLOW_IDENTITY,
            "graph_hash": "b" * 64,
            "aliases": {},
            "queued": False,
        }

    async def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError("read-only lineage resolution must not dispatch")

    monkeypatch.setattr(mcp_server, "_active_editable_workflow", active)
    monkeypatch.setattr(mcp_server, "_completed_graph_patch_result", completed)
    monkeypatch.setattr(mcp_server, "_execute_tool", forbidden_dispatch)

    result = await mcp_server.resolve_workflow_branch_successor.fn(
        request,
        _context(),
    )

    assert result["valid"], result["issues"]
    assert result["lineage"][0]["successor_branch_ids"] == []
    assert result["queued"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_state", ["missing", "forged", "wrong_aliases", "wrong_hash"])
async def test_successor_tool_fails_closed_without_exact_completed_apply(
    branch_tool_environment,
    monkeypatch,
    completion_state,
):
    request, post = await _compiled_branch_remove_request()

    async def active(_ctx):
        return {
            "workflow": post,
            "workflow_identity": WORKFLOW_IDENTITY,
            "graph_hash": "b" * 64,
        }

    def completed(_active, apply_request, *, catalog):
        assert catalog == _catalog()
        if completion_state == "missing":
            return None
        if completion_state == "forged":
            return {"success": False, "error": {"code": "invalid_graph_patch_ledger"}}
        return {
            "success": True,
            "applied": False,
            "already_applied": True,
            "application_id": apply_request.application_id,
            "patch_hash": apply_request.patch_hash,
            "workflow_identity": WORKFLOW_IDENTITY,
            "graph_hash": "c" * 64 if completion_state == "wrong_hash" else "b" * 64,
            "aliases": {"forged": 99} if completion_state == "wrong_aliases" else {},
            "queued": False,
        }

    async def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError("failed lineage resolution must not dispatch")

    monkeypatch.setattr(mcp_server, "_active_editable_workflow", active)
    monkeypatch.setattr(mcp_server, "_completed_graph_patch_result", completed)
    monkeypatch.setattr(mcp_server, "_execute_tool", forbidden_dispatch)

    result = await mcp_server.resolve_workflow_branch_successor.fn(
        request,
        _context(),
    )

    assert result["valid"] is False
    assert result["lineage"] == []
    assert result["queued"] is False
    assert result["issues"][0]["code"] in {
        "graph_patch_completion_not_attested",
        "graph_patch_completion_facts_changed",
    }
