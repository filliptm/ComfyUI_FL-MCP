from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

import pytest
from workflow_branches import (
    BRANCH_DISCOVERY_LIMITS,
    BranchDiscoveryLimits,
    WorkflowBranchCatalog,
    WorkflowBranchRecord,
    WorkflowBranchScope,
    discover_workflow_branches,
)

GRAPH_HASH = "a" * 64
WORKFLOW_IDENTITY = "fl-mcp-workflow:test:1"


def _slots(values: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"name": name, "type": slot_type} for name, slot_type in values]


def _node(
    node_id: int | str,
    node_type: str,
    *,
    inputs: tuple[tuple[str, str], ...] = (),
    outputs: tuple[tuple[str, str], ...] = (),
    widgets_values: list[Any] | None = None,
    title: str | None = None,
    pos: tuple[int, int] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "inputs": _slots(inputs),
        "outputs": _slots(outputs),
    }
    if widgets_values is not None:
        result["widgets_values"] = widgets_values
    if title is not None:
        result["title"] = title
    if pos is not None:
        result["pos"] = list(pos)
    return result


def _link(
    link_id: int,
    source_id: int | str,
    source_slot: int,
    target_id: int | str,
    target_slot: int,
    slot_type: str = "IMAGE",
) -> list[Any]:
    return [link_id, source_id, source_slot, target_id, target_slot, slot_type]


def _workflow(
    nodes: list[dict[str, Any]],
    links: list[Any],
    *,
    subgraphs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "links": links,
        "definitions": {"subgraphs": subgraphs or []},
    }


def _catalog(
    workflow: dict[str, Any],
    *,
    graph_hash: str = GRAPH_HASH,
    workflow_identity: str = WORKFLOW_IDENTITY,
    limits: BranchDiscoveryLimits | None = None,
) -> WorkflowBranchCatalog:
    return discover_workflow_branches(
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        limits=limits,
    )


def _root(catalog: WorkflowBranchCatalog) -> WorkflowBranchScope:
    return next(scope for scope in catalog.scopes if scope.scope.kind == "root")


def _branches(
    scope: WorkflowBranchScope,
    kind: str,
) -> list[WorkflowBranchRecord]:
    return [branch for branch in scope.branches if branch.kind == kind]


def _primitive_edge_counts(scope: WorkflowBranchScope) -> Counter[str]:
    return Counter(
        edge_id
        for branch in scope.branches
        if branch.kind == "segment"
        for edge_id in branch.edge_ids
    )


def _linear_workflow(*, offset: int = 0) -> dict[str, Any]:
    return _workflow(
        [
            _node(1 + offset, "Source", outputs=(("image", "IMAGE"),)),
            _node(
                2 + offset,
                "Middle",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            ),
            _node(3 + offset, "Sink", inputs=(("images", "IMAGE"),)),
        ],
        [
            _link(1, 1 + offset, 0, 2 + offset, 0),
            _link(2, 2 + offset, 0, 3 + offset, 0),
        ],
    )


def _diamond_workflow() -> dict[str, Any]:
    return _workflow(
        [
            _node(1, "Source", outputs=(("image", "IMAGE"),)),
            _node(2, "ArmA", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(3, "ArmB", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(
                4,
                "Join",
                inputs=(("a", "IMAGE"), ("b", "IMAGE")),
                outputs=(("image", "IMAGE"),),
            ),
            _node(5, "Sink", inputs=(("images", "IMAGE"),)),
        ],
        [
            _link(1, 1, 0, 2, 0),
            _link(2, 1, 0, 3, 0),
            _link(3, 2, 0, 4, 0),
            _link(4, 3, 0, 4, 1),
            _link(5, 4, 0, 5, 0),
        ],
    )


def _real_reroute_node(
    node_id: int | str = 14,
    *,
    output_type: str = "*",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "Reroute",
        "inputs": [{"name": "", "type": "*", "link": 1}],
        "outputs": [{"name": "", "type": output_type, "links": [2]}],
    }


def _reroute_workflow(
    *,
    output_type: str = "*",
    incoming_type: str | None = "VAE",
    outgoing_type: str | None = "VAE",
) -> dict[str, Any]:
    return _workflow(
        [
            _node(13, "Source", outputs=(("vae", "VAE"),)),
            _real_reroute_node(output_type=output_type),
            _node(15, "Sink", inputs=(("vae", "VAE"),)),
        ],
        [
            _link(1, 13, 0, 14, 0, incoming_type),
            _link(2, 14, 0, 15, 0, outgoing_type),
        ],
    )


def _subgraph_definition(
    subgraph_id: str,
    *,
    nodes: list[dict[str, Any]],
    links: list[Any],
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": subgraph_id,
        "name": name or subgraph_id,
        "inputs": [{"name": "image", "type": "IMAGE"}],
        "outputs": [{"name": "image", "type": "IMAGE"}],
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": nodes,
        "links": links,
    }


def test_linear_graph_is_one_maximal_segment_with_exact_boundary_facts() -> None:
    catalog = _catalog(_linear_workflow())

    assert catalog.valid
    assert catalog.graph_hash == GRAPH_HASH
    scope = _root(catalog)
    assert scope.writable
    assert scope.reasons == []
    assert scope.node_count == 3
    assert scope.edge_count == 2
    [segment] = _branches(scope, "segment")
    assert segment.owned_node_ids == [2]
    assert segment.interior_node_ids == [2]
    assert segment.boundary_node_ids == [1, 3]
    assert segment.selectable_node_ids == [1, 2, 3]
    assert len(segment.entry_edges) == 1
    assert len(segment.exit_edges) == 1
    assert len(segment.cut_edges) == 2
    assert segment.internal_edges == []
    assert segment.entry_edges[0].source.output == "image"
    assert segment.exit_edges[0].target.input == "images"
    assert _primitive_edge_counts(scope) == Counter(dict.fromkeys(segment.edge_ids, 1))


def test_exact_real_reroute_is_normalized_by_incident_type_stably() -> None:
    workflow = _reroute_workflow()
    forward = _catalog(workflow)
    reversed_workflow = deepcopy(workflow)
    reversed_workflow["nodes"].reverse()
    reversed_workflow["links"].reverse()
    reversed_catalog = _catalog(reversed_workflow)

    assert forward.valid
    assert reversed_catalog.valid
    assert reversed_catalog.branch_catalog_hash == forward.branch_catalog_hash
    [segment] = _branches(_root(forward), "segment")
    assert segment.owned_node_ids == [14]
    [entry] = segment.entry_edges
    [exit_edge] = segment.exit_edges
    assert entry.target.input == "__fl_mcp_reroute_input_0__"
    assert entry.target.type == "VAE"
    assert exit_edge.source.output == "__fl_mcp_reroute_output_0__"
    assert exit_edge.source.type == "VAE"
    assert entry.link_type == exit_edge.link_type == "VAE"
    assert workflow["nodes"][1]["inputs"][0]["name"] == ""


def test_exact_real_reroute_is_normalized_inside_a_subgraph_instance() -> None:
    definition = _subgraph_definition(
        "reroute-definition",
        nodes=[_real_reroute_node(295, output_type="VAE")],
        links=[
            _link(1, -10, 0, 295, 0, "VAE"),
            _link(2, 295, 0, -20, 0, "VAE"),
        ],
    )
    definition["inputs"][0]["type"] = "VAE"
    definition["outputs"][0]["type"] = "VAE"
    workflow = _workflow(
        [
            _node(
                9,
                "reroute-definition",
                inputs=(("vae", "VAE"),),
                outputs=(("vae", "VAE"),),
            )
        ],
        [],
        subgraphs=[definition],
    )

    catalog = _catalog(workflow)

    assert catalog.valid
    nested = next(
        scope for scope in catalog.scopes if scope.scope.kind == "subgraph_instance"
    )
    reroute_edges = [
        edge
        for branch in nested.branches
        for edge in branch.entry_edges + branch.exit_edges + branch.internal_edges
        if edge.source.node_id == 295 or edge.target.node_id == 295
    ]
    assert reroute_edges
    assert {
        endpoint
        for edge in reroute_edges
        for endpoint in (edge.source.output, edge.target.input)
        if "reroute" in endpoint
    } == {"__fl_mcp_reroute_input_0__", "__fl_mcp_reroute_output_0__"}
    assert {edge.link_type for edge in reroute_edges} == {"VAE"}


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("wrong_shape", "invalid_reroute_shape"),
        ("missing_incident", "unresolved_reroute_type"),
        ("untyped_incident", "unresolved_reroute_type"),
        ("wildcard_incident", "unresolved_reroute_type"),
        ("mixed_incident", "reroute_type_mismatch"),
        ("declared_type_mismatch", "reroute_type_mismatch"),
    ],
)
def test_reroute_normalization_fails_closed(
    case: str,
    expected_code: str,
) -> None:
    workflow = _reroute_workflow()
    if case == "wrong_shape":
        workflow["nodes"][1]["inputs"].append({"name": "", "type": "*"})
    elif case == "missing_incident":
        workflow = _workflow([_real_reroute_node()], [])
    elif case == "untyped_incident":
        workflow["links"][0][5] = None
    elif case == "wildcard_incident":
        workflow["links"][1][5] = "*"
    elif case == "mixed_incident":
        workflow["links"][1][5] = "MODEL"
    elif case == "declared_type_mismatch":
        workflow["nodes"][1]["outputs"][0]["type"] = "MODEL"

    catalog = _catalog(workflow)

    assert not catalog.valid
    assert expected_code in {issue.code for issue in catalog.issues}
    assert all(not scope.writable for scope in catalog.scopes)


def test_arbitrary_blank_slot_node_still_fails_closed() -> None:
    workflow = _linear_workflow()
    workflow["nodes"][1]["inputs"][0]["name"] = ""

    catalog = _catalog(workflow)

    assert not catalog.valid
    assert "unresolved_slot_fact" in {issue.code for issue in catalog.issues}


def test_diamond_exposes_segments_and_exclusive_split_arms_until_join() -> None:
    catalog = _catalog(_diamond_workflow())

    assert catalog.valid
    scope = _root(catalog)
    segments = _branches(scope, "segment")
    arms = _branches(scope, "split_arm")
    assert len(segments) == 3
    assert len(arms) == 2
    assert _primitive_edge_counts(scope) == Counter(
        {edge_id: 1 for branch in segments for edge_id in branch.edge_ids}
    )
    assert sum(len(branch.edge_ids) for branch in segments) == 5
    assert {tuple(arm.owned_node_ids) for arm in arms} == {(2,), (3,)}
    assert all(arm.boundary_node_ids == [1, 4] for arm in arms)
    assert all(len(arm.entry_edges) == 1 for arm in arms)
    assert all(len(arm.exit_edges) == 1 for arm in arms)
    assert all(len(arm.sibling_branch_ids) == 1 for arm in arms)
    assert {arm.sibling_branch_ids[0] for arm in arms} == {arm.branch_id for arm in arms}
    assert all(arm.primitive_segment_ids for arm in arms)


def test_split_arm_includes_auxiliary_side_input_in_exact_entry_and_cut_sets() -> None:
    workflow = _workflow(
        [
            _node(1, "Split", outputs=(("image", "IMAGE"),)),
            _node(
                2,
                "ArmWithSideInput",
                inputs=(("image", "IMAGE"), ("conditioning", "CONDITIONING")),
                outputs=(("image", "IMAGE"),),
            ),
            _node(3, "OtherArm", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(4, "Join", inputs=(("a", "IMAGE"), ("b", "IMAGE"))),
            _node(9, "Conditioning", outputs=(("conditioning", "CONDITIONING"),)),
        ],
        [
            _link(1, 1, 0, 2, 0),
            _link(2, 1, 0, 3, 0),
            _link(3, 2, 0, 4, 0),
            _link(4, 3, 0, 4, 1),
            _link(5, 9, 0, 2, 1, "CONDITIONING"),
        ],
    )

    [arm] = [
        candidate
        for candidate in _branches(_root(_catalog(workflow)), "split_arm")
        if candidate.primary_entry_edge_id is not None
        and next(
            edge
            for edge in candidate.entry_edges
            if edge.edge_id == candidate.primary_entry_edge_id
        ).target.node_id
        == 2
    ]
    assert {(edge.source.node_id, edge.target.node_id) for edge in arm.entry_edges} == {
        (1, 2),
        (9, 2),
    }
    assert {(edge.source.node_id, edge.target.node_id) for edge in arm.cut_edges} == {
        (1, 2),
        (9, 2),
        (2, 4),
    }
    assert arm.boundary_node_ids == [1, 4, 9]
    primary = next(edge for edge in arm.entry_edges if edge.edge_id == arm.primary_entry_edge_id)
    assert (primary.source.node_id, primary.target.node_id) == (1, 2)


def test_edge_facts_separate_declared_endpoint_types_from_physical_link_type() -> None:
    workflow = _workflow(
        [
            _node(1, "Polymorphic", outputs=(("value", "*"),)),
            _node(2, "ImageSink", inputs=(("image", "IMAGE"),)),
        ],
        [_link(1, 1, 0, 2, 0, "IMAGE")],
    )

    [segment] = _branches(_root(_catalog(workflow)), "segment")
    [edge] = segment.entry_edges
    assert edge.source.type == "*"
    assert edge.target.type == "IMAGE"
    assert edge.link_type == "IMAGE"


def test_split_without_reconvergence_owns_each_arm_through_terminal() -> None:
    workflow = _workflow(
        [
            _node(1, "Split", outputs=(("image", "IMAGE"),)),
            _node(2, "A", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(3, "ATerminal", inputs=(("image", "IMAGE"),)),
            _node(4, "B", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(5, "BTerminal", inputs=(("image", "IMAGE"),)),
        ],
        [_link(1, 1, 0, 2, 0), _link(2, 2, 0, 3, 0), _link(3, 1, 0, 4, 0), _link(4, 4, 0, 5, 0)],
    )

    arms = _branches(_root(_catalog(workflow)), "split_arm")
    assert {tuple(arm.owned_node_ids) for arm in arms} == {(2, 3), (4, 5)}
    assert all(arm.exit_edges == [] for arm in arms)
    assert all(arm.boundary_node_ids == [1] for arm in arms)


def test_nested_diamond_arms_have_parent_child_and_sibling_relations() -> None:
    workflow = _workflow(
        [
            _node(1, "OuterSplit", outputs=(("image", "IMAGE"),)),
            _node(2, "InnerSplit", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(3, "InnerA", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(4, "InnerB", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(5, "OuterB", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(6, "InnerJoin", inputs=(("a", "IMAGE"), ("b", "IMAGE")), outputs=(("image", "IMAGE"),)),
            _node(7, "OuterJoin", inputs=(("a", "IMAGE"), ("b", "IMAGE"))),
        ],
        [
            _link(1, 1, 0, 2, 0),
            _link(2, 1, 0, 5, 0),
            _link(3, 2, 0, 3, 0),
            _link(4, 2, 0, 4, 0),
            _link(5, 3, 0, 6, 0),
            _link(6, 4, 0, 6, 1),
            _link(7, 6, 0, 7, 0),
            _link(8, 5, 0, 7, 1),
        ],
    )

    scope = _root(_catalog(workflow))
    arms = _branches(scope, "split_arm")
    assert len(arms) == 4
    inner = [arm for arm in arms if arm.entry_edges[0].source.node_id == 2]
    outer = [arm for arm in arms if arm.entry_edges[0].source.node_id == 1]
    assert len(inner) == len(outer) == 2
    parent = next(arm for arm in outer if 2 in arm.owned_node_ids)
    assert all(arm.parent_branch_ids == [parent.branch_id] for arm in inner)
    assert {arm.branch_id for arm in inner}.issubset(set(parent.child_branch_ids))
    assert all(len(arm.sibling_branch_ids) == 1 for arm in inner)


def test_join_split_creates_four_primitive_segments_and_two_arms() -> None:
    workflow = _workflow(
        [
            _node(1, "A", outputs=(("image", "IMAGE"),)),
            _node(2, "B", outputs=(("image", "IMAGE"),)),
            _node(3, "JoinSplit", inputs=(("a", "IMAGE"), ("b", "IMAGE")), outputs=(("image", "IMAGE"),)),
            _node(4, "C", inputs=(("image", "IMAGE"),)),
            _node(5, "D", inputs=(("image", "IMAGE"),)),
        ],
        [_link(1, 1, 0, 3, 0), _link(2, 2, 0, 3, 1), _link(3, 3, 0, 4, 0), _link(4, 3, 0, 5, 0)],
    )

    scope = _root(_catalog(workflow))
    assert len(_branches(scope, "segment")) == 4
    arms = _branches(scope, "split_arm")
    assert len(arms) == 2
    assert {tuple(arm.owned_node_ids) for arm in arms} == {(4,), (5,)}
    assert sum(_primitive_edge_counts(scope).values()) == 4


def test_disconnected_components_and_isolated_nodes_are_cataloged() -> None:
    workflow = _workflow(
        [
            _node(1, "Source", outputs=(("image", "IMAGE"),)),
            _node(2, "Sink", inputs=(("image", "IMAGE"),)),
            _node(3, "OtherSource", outputs=(("image", "IMAGE"),)),
            _node(4, "OtherSink", inputs=(("image", "IMAGE"),)),
            _node(5, "Lonely"),
        ],
        [_link(1, 1, 0, 2, 0), _link(2, 3, 0, 4, 0)],
    )

    scope = _root(_catalog(workflow))
    assert len(_branches(scope, "segment")) == 2
    [isolated] = _branches(scope, "isolated")
    assert isolated.owned_node_ids == [5]
    assert isolated.selectable_node_ids == [5]
    assert isolated.edge_ids == []


def test_parallel_slots_are_distinct_physical_edges_and_segments() -> None:
    workflow = _workflow(
        [
            _node(1, "Pair", outputs=(("left", "IMAGE"), ("right", "MASK"))),
            _node(2, "PairSink", inputs=(("left", "IMAGE"), ("right", "MASK"))),
        ],
        [_link(1, 1, 0, 2, 0, "IMAGE"), _link(2, 1, 1, 2, 1, "MASK")],
    )

    scope = _root(_catalog(workflow))
    segments = _branches(scope, "segment")
    assert len(segments) == 2
    assert len({edge_id for branch in segments for edge_id in branch.edge_ids}) == 2
    assert _primitive_edge_counts(scope) == Counter(
        {edge_id: 1 for branch in segments for edge_id in branch.edge_ids}
    )


def test_parallel_split_edges_into_shared_successor_do_not_advertise_empty_arms() -> None:
    workflow = _workflow(
        [
            _node(1, "Split", outputs=(("image", "IMAGE"),)),
            _node(2, "Shared", inputs=(("left", "IMAGE"), ("right", "IMAGE"))),
        ],
        [
            _link(1, 1, 0, 2, 0),
            _link(2, 1, 0, 2, 1),
        ],
    )

    scope = _root(_catalog(workflow))
    assert _branches(scope, "split_arm") == []
    segments = _branches(scope, "segment")
    assert len(segments) == 2
    assert _primitive_edge_counts(scope) == Counter(
        {edge_id: 1 for segment in segments for edge_id in segment.edge_ids}
    )


def test_reverse_serialization_is_fully_canonical() -> None:
    workflow = _diamond_workflow()
    reversed_workflow = deepcopy(workflow)
    reversed_workflow["nodes"].reverse()
    reversed_workflow["links"].reverse()

    first = _catalog(workflow)
    second = _catalog(reversed_workflow)

    assert first.model_dump(by_alias=True) == second.model_dump(by_alias=True)


def test_branch_ids_ignore_layout_title_widgets_graph_hash_and_unrelated_sibling() -> None:
    workflow = _workflow(
        [
            _node(1, "Split", outputs=(("image", "IMAGE"),), widgets_values=[1], title="Old", pos=(0, 0)),
            _node(2, "A", inputs=(("image", "IMAGE"),), title="A", pos=(100, 0)),
            _node(3, "B", inputs=(("image", "IMAGE"),), title="B", pos=(100, 100)),
        ],
        [_link(1, 1, 0, 2, 0), _link(2, 1, 0, 3, 0)],
    )
    first = _catalog(workflow)
    mutated = deepcopy(workflow)
    mutated["nodes"][0]["widgets_values"] = [999, "secret-like-value"]
    mutated["nodes"][0]["title"] = "New title"
    mutated["nodes"][0]["pos"] = [9999, -42]
    second = _catalog(mutated, graph_hash="b" * 64)
    assert first.graph_hash != second.graph_hash
    assert first.branch_catalog_hash == second.branch_catalog_hash
    assert [branch.branch_id for branch in _root(first).branches] == [
        branch.branch_id for branch in _root(second).branches
    ]

    extended = deepcopy(workflow)
    extended["nodes"].append(_node(4, "C", inputs=(("image", "IMAGE"),)))
    extended["links"].append(_link(3, 1, 0, 4, 0))
    before_arm = next(
        arm
        for arm in _branches(_root(first), "split_arm")
        if arm.entry_edges[0].target.node_id == 2
    )
    after_arm = next(
        arm
        for arm in _branches(_root(_catalog(extended)), "split_arm")
        if arm.entry_edges[0].target.node_id == 2
    )
    assert before_arm.branch_id == after_arm.branch_id
    assert before_arm.branch_fingerprint == after_arm.branch_fingerprint


def test_internal_topology_changes_id_while_clones_share_fingerprint() -> None:
    clone_workflow = _workflow(
        [
            *_linear_workflow()["nodes"],
            *_linear_workflow(offset=10)["nodes"],
        ],
        [
            *_linear_workflow()["links"],
            _link(3, 11, 0, 12, 0),
            _link(4, 12, 0, 13, 0),
        ],
    )
    segments = _branches(_root(_catalog(clone_workflow)), "segment")
    assert len(segments) == 2
    assert segments[0].branch_id != segments[1].branch_id
    assert segments[0].branch_fingerprint == segments[1].branch_fingerprint

    changed = _linear_workflow()
    changed["nodes"][1]["id"] = 4
    changed["links"] = [_link(1, 1, 0, 4, 0), _link(2, 4, 0, 3, 0)]
    original_id = _branches(_root(_catalog(_linear_workflow())), "segment")[0].branch_id
    changed_id = _branches(_root(_catalog(changed)), "segment")[0].branch_id
    assert original_id != changed_id


def test_numeric_and_string_node_ids_are_never_conflated() -> None:
    scope = _root(_catalog(_workflow([_node(2, "Same"), _node("2", "Same")], [])))
    isolated = _branches(scope, "isolated")
    assert len(isolated) == 2
    assert {type(branch.owned_node_ids[0]) for branch in isolated} == {int, str}
    assert isolated[0].branch_id != isolated[1].branch_id
    assert isolated[0].branch_fingerprint == isolated[1].branch_fingerprint


def test_cycles_and_oversize_graphs_fail_with_classified_issues() -> None:
    cycle = _workflow(
        [
            _node(1, "A", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(2, "B", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
        ],
        [_link(1, 1, 0, 2, 0), _link(2, 2, 0, 1, 0)],
    )
    cycle_result = _catalog(cycle)
    assert not cycle_result.valid
    assert [issue.code for issue in cycle_result.issues] == ["graph_cycle"]
    assert cycle_result.scopes == []

    oversize = _catalog(
        _linear_workflow(),
        limits=BranchDiscoveryLimits(max_nodes=2),
    )
    assert not oversize.valid
    assert [issue.code for issue in oversize.issues] == ["node_limit_exceeded"]


def test_malformed_bounded_text_facts_fail_classified_without_raw_validation() -> None:
    malformed_cases = [
        (
            _workflow([_node(1, "N" * 257)], []),
            "invalid_node_type",
        ),
        (
            _workflow(
                [_node(1, "Source", outputs=(("o" * 257, "IMAGE"),))],
                [],
            ),
            "unresolved_slot_fact",
        ),
        (
            _workflow([_node("node-" + "x" * 257, "Source")], []),
            "invalid_node_id",
        ),
    ]
    for workflow, expected_code in malformed_cases:
        result = _catalog(workflow)
        assert not result.valid
        assert expected_code in {issue.code for issue in result.issues}
        assert all(not scope.writable for scope in result.scopes)
        assert all(
            not branch.writable
            for scope in result.scopes
            for branch in scope.branches
        )


def test_any_definition_issue_makes_otherwise_valid_root_branches_read_only() -> None:
    malformed_definition = _subgraph_definition(
        "unused-definition",
        nodes=[],
        links=[],
        name="n" * 513,
    )
    result = _catalog(
        _workflow(
            _linear_workflow()["nodes"],
            _linear_workflow()["links"],
            subgraphs=[malformed_definition],
        )
    )

    assert not result.valid
    assert {issue.code for issue in result.issues} == {
        "invalid_subgraph_definition_name"
    }
    root = _root(result)
    assert not root.writable
    assert "branch_catalog_invalid" in root.reasons
    assert root.branches
    assert all(not branch.writable for branch in root.branches)
    assert all("branch_catalog_invalid" in branch.reasons for branch in root.branches)


def test_reused_subgraph_instances_require_explicit_shared_definition_acknowledgement() -> None:
    definition = _subgraph_definition(
        "diamond-def",
        nodes=[
            _node(1, "ArmA", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(2, "ArmB", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(3, "Join", inputs=(("a", "IMAGE"), ("b", "IMAGE")), outputs=(("image", "IMAGE"),)),
        ],
        links=[
            _link(1, -10, 0, 1, 0),
            _link(2, -10, 0, 2, 0),
            _link(3, 1, 0, 3, 0),
            _link(4, 2, 0, 3, 1),
            _link(5, 3, 0, -20, 0),
        ],
    )
    workflow = _workflow(
        [
            _node(100, "diamond-def", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(200, "diamond-def", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
        ],
        [],
        subgraphs=[definition],
    )

    catalog = _catalog(workflow)
    assert catalog.valid
    assert catalog.summary.scope_count == 3
    nested = [scope for scope in catalog.scopes if scope.scope.kind == "subgraph_instance"]
    assert len(nested) == 2
    assert {
        scope.scope.scope_path[0].container_node_id for scope in nested
    } == {100, 200}
    assert all(scope.scope.scope_path[0].subgraph_id == "diamond-def" for scope in nested)
    assert all(scope.scope.subgraph_id == "diamond-def" for scope in nested)
    assert all(not scope.writable for scope in nested)
    assert all(
        scope.reasons == ["shared_definition_acknowledgement_required"]
        for scope in nested
    )
    assert all(not branch.writable for scope in nested for branch in scope.branches)
    assert all(
        branch.reasons == ["shared_definition_acknowledgement_required"]
        for scope in nested
        for branch in scope.branches
    )
    for scope in nested:
        boundary_facts = {
            (node.node_id, node.boundary_kind, node.selectable)
            for branch in scope.branches
            for node in branch.nodes
            if node.boundary_kind is not None
        }
        assert (-10, "subgraph_input", False) in boundary_facts
        assert (-20, "subgraph_output", False) in boundary_facts
        assert all(-10 not in branch.selectable_node_ids for branch in scope.branches)
        assert all(-20 not in branch.selectable_node_ids for branch in scope.branches)
    first_segments = sorted(branch.branch_fingerprint for branch in nested[0].branches)
    second_segments = sorted(branch.branch_fingerprint for branch in nested[1].branches)
    assert first_segments == second_segments
    assert {branch.branch_id for branch in nested[0].branches}.isdisjoint(
        branch.branch_id for branch in nested[1].branches
    )


def test_nested_definition_paths_preserve_reused_local_ids_without_flattening() -> None:
    leaf = _subgraph_definition(
        "leaf-def",
        nodes=[_node(1, "Leaf", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        links=[_link(1, -10, 0, 1, 0), _link(2, 1, 0, -20, 0)],
    )
    outer = _subgraph_definition(
        "outer-def",
        nodes=[
            _node(1, "leaf-def", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))
        ],
        links=[_link(1, -10, 0, 1, 0), _link(2, 1, 0, -20, 0)],
    )
    workflow = _workflow(
        [_node(10, "outer-def", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        [],
        subgraphs=[outer, leaf],
    )

    catalog = _catalog(workflow)
    assert catalog.valid
    nested = sorted(
        (scope for scope in catalog.scopes if scope.scope.kind == "subgraph_instance"),
        key=lambda scope: len(scope.scope.scope_path),
    )
    assert len(nested) == 2
    assert nested[0].scope.scope_path[0].model_dump() == {
        "container_node_id": 10,
        "subgraph_id": "outer-def",
    }
    assert [step.model_dump() for step in nested[1].scope.scope_path] == [
        {"container_node_id": 10, "subgraph_id": "outer-def"},
        {"container_node_id": 1, "subgraph_id": "leaf-def"},
    ]
    assert nested[1].parent_scope_id == nested[0].scope_id
    assert nested[1].scope_id in nested[0].child_scope_ids
    assert all(scope.writable for scope in nested)
    assert all(not scope.reasons for scope in nested)
    assert all(branch.writable for scope in nested for branch in scope.branches)
    assert all(not branch.reasons for scope in nested for branch in scope.branches)
    assert all(-10 in {node.node_id for branch in scope.branches for node in branch.nodes} for scope in nested)


def test_object_link_manifests_and_reversed_definition_serialization_are_canonical() -> None:
    definition = _subgraph_definition(
        "object-links",
        nodes=[_node(1, "Pass", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        links=[],
    )
    definition["links"] = {
        "second": {
            "id": 2,
            "origin_id": 1,
            "origin_slot": 0,
            "target_id": -20,
            "target_slot": 0,
            "type": "IMAGE",
        },
        "first": {
            "id": 1,
            "origin_id": -10,
            "origin_slot": 0,
            "target_id": 1,
            "target_slot": 0,
            "type": "IMAGE",
        },
    }
    workflow = _workflow(
        [_node(7, "object-links", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        [],
        subgraphs=[definition],
    )
    reversed_workflow = deepcopy(workflow)
    reversed_definition = reversed_workflow["definitions"]["subgraphs"][0]
    reversed_definition["nodes"].reverse()
    reversed_definition["links"] = dict(reversed(reversed_definition["links"].items()))

    assert _catalog(workflow).model_dump(by_alias=True) == _catalog(
        reversed_workflow
    ).model_dump(by_alias=True)


def test_cycle_inside_reused_subgraph_is_classified_without_flattening_root() -> None:
    definition = _subgraph_definition(
        "cyclic-child",
        nodes=[
            _node(1, "A", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
            _node(2, "B", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
        ],
        links=[_link(1, 1, 0, 2, 0), _link(2, 2, 0, 1, 0)],
    )
    workflow = _workflow(
        [_node(10, "cyclic-child", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        [],
        subgraphs=[definition],
    )

    result = _catalog(workflow)
    assert not result.valid
    assert "graph_cycle" in {issue.code for issue in result.issues}
    assert len(result.scopes) == 1
    assert result.scopes[0].scope.kind == "root"
    assert not result.scopes[0].writable
    assert all(not branch.writable for branch in result.scopes[0].branches)


@pytest.mark.parametrize(
    ("definition", "expected_code"),
    [
        (
            _subgraph_definition(
                "collision",
                nodes=[_node(-10, "Illegal")],
                links=[],
            ),
            "duplicate_local_node_id",
        ),
        (
            _subgraph_definition(
                "recursive",
                nodes=[
                    _node(
                        1,
                        "recursive",
                        inputs=(("image", "IMAGE"),),
                        outputs=(("image", "IMAGE"),),
                    )
                ],
                links=[_link(1, -10, 0, 1, 0), _link(2, 1, 0, -20, 0)],
            ),
            "recursive_subgraph_definition",
        ),
    ],
)
def test_subgraph_virtual_id_collisions_and_recursion_fail_classified(
    definition: dict[str, Any],
    expected_code: str,
) -> None:
    workflow = _workflow(
        [
            _node(
                10,
                definition["id"],
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            )
        ],
        [],
        subgraphs=[definition],
    )

    result = _catalog(workflow)
    assert not result.valid
    assert expected_code in {issue.code for issue in result.issues}


def test_subgraph_depth_scope_and_split_work_limits_are_bounded() -> None:
    leaf = _subgraph_definition(
        "leaf",
        nodes=[_node(1, "Leaf", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        links=[_link(1, -10, 0, 1, 0), _link(2, 1, 0, -20, 0)],
    )
    outer = _subgraph_definition(
        "outer",
        nodes=[_node(1, "leaf", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        links=[_link(1, -10, 0, 1, 0), _link(2, 1, 0, -20, 0)],
    )
    workflow = _workflow(
        [_node(1, "outer", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),))],
        [],
        subgraphs=[outer, leaf],
    )
    depth = _catalog(workflow, limits=BranchDiscoveryLimits(max_depth=1))
    assert not depth.valid
    assert "subgraph_depth_limit_exceeded" in {issue.code for issue in depth.issues}

    scope_limit = _catalog(workflow, limits=BranchDiscoveryLimits(max_scopes=1))
    assert not scope_limit.valid
    assert "scope_limit_exceeded" in {issue.code for issue in scope_limit.issues}

    split = _diamond_workflow()
    work_limit = _catalog(split, limits=BranchDiscoveryLimits(max_split_arm_work=1))
    assert not work_limit.valid
    assert "split_arm_work_limit_exceeded" in {issue.code for issue in work_limit.issues}


def test_wide_split_topology_work_is_metered_and_fails_closed() -> None:
    arm_count = 96
    workflow = _workflow(
        [
            _node(1, "Split", outputs=(("image", "IMAGE"),)),
            *[
                _node(node_id, f"Terminal{node_id}", inputs=(("image", "IMAGE"),))
                for node_id in range(2, arm_count + 2)
            ],
        ],
        [
            _link(link_id, 1, 0, link_id + 1, 0)
            for link_id in range(1, arm_count + 1)
        ],
    )

    result = _catalog(
        workflow,
        limits=BranchDiscoveryLimits(max_split_arm_work=500),
    )
    assert not result.valid
    assert "split_arm_work_limit_exceeded" in {
        issue.code for issue in result.issues
    }
    assert all(not scope.writable for scope in result.scopes)
    assert all(
        not branch.writable
        for scope in result.scopes
        for branch in scope.branches
    )


def test_public_exports_and_default_limits_are_importable() -> None:
    assert isinstance(BRANCH_DISCOVERY_LIMITS, BranchDiscoveryLimits)
    assert BRANCH_DISCOVERY_LIMITS.max_depth == 8
