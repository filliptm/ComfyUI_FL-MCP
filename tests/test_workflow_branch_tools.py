import pytest
from pydantic import ValidationError
from workflow_branch_tools import (
    DiscoverWorkflowBranchesRequest,
    NavigateWorkflowBranchRequest,
    discover_workflow_branch_selection,
)

WORKFLOW_IDENTITY = "fl-mcp-workflow:branch-tools:1"
GRAPH_HASH = "a" * 64


def _node(node_id: int, node_type: str, title: str) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "title": title,
        "pos": [node_id * 100, 0],
        "size": [180, 80],
        "widgets_values": [],
        "inputs": [{"name": "in", "type": "IMAGE"}] if node_id != 1 else [],
        "outputs": [{"name": "out", "type": "IMAGE", "links": []}],
    }


def _workflow() -> dict:
    nodes = [
        _node(1, "Source", "Source"),
        _node(2, "Upscale", "Upscale branch"),
        _node(3, "Preview", "Preview output"),
        _node(4, "Save", "Save"),
    ]
    links = [
        [1, 1, 0, 2, 0, "IMAGE"],
        [2, 1, 0, 3, 0, "IMAGE"],
        [3, 2, 0, 4, 0, "IMAGE"],
    ]
    by_id = {item[0]: item for item in links}
    nodes[0]["outputs"][0]["links"] = [1, 2]
    nodes[1]["outputs"][0]["links"] = [3]
    for node in nodes[1:]:
        incoming = next((item for item in links if item[3] == node["id"]), None)
        if incoming is not None:
            node["inputs"][0]["link"] = incoming[0]
    return {
        "version": 0.4,
        "last_node_id": 4,
        "last_link_id": 3,
        "nodes": nodes,
        "links": list(by_id.values()),
        "groups": [],
        "config": {},
        "extra": {},
        "definitions": {"subgraphs": []},
    }


def test_unfiltered_discovery_lists_without_authorizing_one_branch() -> None:
    _, result = discover_workflow_branch_selection(
        DiscoverWorkflowBranchesRequest(),
        _workflow(),
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
    )

    assert result.valid
    assert result.resolution.status == "listed"
    assert result.resolution.selected is None
    assert result.selected_scope is None
    assert result.selected_branch is None
    assert result.read_only and result.queued is False


def test_unique_query_returns_exact_full_branch_boundary_facts() -> None:
    catalog, listed = discover_workflow_branch_selection(
        DiscoverWorkflowBranchesRequest(),
        _workflow(),
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
    )
    candidate = next(
        item for item in listed.resolution.candidates if "Upscale" in item.label
    )

    _, resolved = discover_workflow_branch_selection(
        DiscoverWorkflowBranchesRequest(branch_id=candidate.branch_id),
        _workflow(),
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
    )

    assert resolved.resolution.status == "resolved"
    assert resolved.branch_catalog_hash == catalog.branch_catalog_hash
    assert resolved.selected_branch is not None
    assert resolved.selected_branch.branch_id == candidate.branch_id
    exact_edges = {
        edge.edge_id
        for edge in [
            *resolved.selected_branch.entry_edges,
            *resolved.selected_branch.exit_edges,
            *resolved.selected_branch.cut_edges,
            *resolved.selected_branch.internal_edges,
        ]
    }
    assert exact_edges
    assert all(len(edge_id) == 64 for edge_id in exact_edges)


def test_semantic_tie_never_exposes_full_selected_branch() -> None:
    tied = _workflow()
    tied["nodes"] = tied["nodes"][:3]
    tied["nodes"][1]["title"] = "alternate path"
    tied["nodes"][2]["title"] = "alternate path"
    tied["nodes"][1]["outputs"][0]["links"] = []
    tied["nodes"][2]["outputs"][0]["links"] = []
    tied["links"] = tied["links"][:2]
    tied["last_node_id"] = 3
    tied["last_link_id"] = 2
    _, result = discover_workflow_branch_selection(
        DiscoverWorkflowBranchesRequest(query="alternate path"),
        tied,
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
    )

    assert result.resolution.status == "needs_choice"
    assert result.selected_branch is None
    assert result.selected_scope is None


def test_tool_requests_are_strict_and_navigation_requires_every_pin() -> None:
    with pytest.raises(ValidationError):
        DiscoverWorkflowBranchesRequest.model_validate({"unknown": True})
    with pytest.raises(ValidationError):
        DiscoverWorkflowBranchesRequest(direction="upstream")
    with pytest.raises(ValidationError):
        NavigateWorkflowBranchRequest.model_validate(
            {
                "branch_id": "b" * 64,
                "expected_workflow_identity": WORKFLOW_IDENTITY,
                "expected_graph_hash": GRAPH_HASH,
            }
        )
