from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from backend.workflow_scope import (
    SCOPE_INPUT_NODE_ID,
    SCOPE_INPUT_NODE_TYPE,
    SCOPE_OUTPUT_NODE_ID,
    SCOPE_OUTPUT_NODE_TYPE,
    WORKFLOW_SCOPE_PROJECTION_SCHEMA,
    WORKFLOW_SCOPE_SCHEMA,
    WorkflowScopeError,
    WorkflowScopeLimits,
    WorkflowScopeStep,
    enumerate_workflow_scope_instances,
    project_workflow_scope,
    resolve_workflow_scope,
    resolve_workflow_scope_edit,
    workflow_definition_hash,
)


class _ExplodingOversizedMapping(dict[str, Any]):
    def items(self) -> Any:
        raise AssertionError("oversized mapping must fail before items()")

    def values(self) -> Any:
        raise AssertionError("oversized mapping must fail before values()")


class _ExplodingOversizedList(list[Any]):
    def __iter__(self) -> Any:
        raise AssertionError("oversized list must fail before iteration")


class _DeepcopyBomb:
    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        raise AssertionError("non-JSON metadata must fail before deepcopy")


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"fl-mcp-test:{label}"))


def _port(
    owner: str,
    direction: str,
    index: int,
    name: str,
    slot_type: str,
    link_ids: list[int | str],
) -> dict[str, Any]:
    return {
        "id": _uuid(f"{owner}:{direction}:{index}"),
        "name": name,
        "type": slot_type,
        "linkIds": link_ids,
    }


def _node(
    node_id: int | str,
    node_type: str,
    *,
    inputs: tuple[tuple[str, str], ...] = (),
    outputs: tuple[tuple[str, str], ...] = (),
    widgets: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "inputs": [{"name": name, "type": slot_type} for name, slot_type in inputs],
        "outputs": [{"name": name, "type": slot_type} for name, slot_type in outputs],
        "widgets_values": list(widgets or []),
    }


def _scope_node(node_id: int | str, definition_id: str) -> dict[str, Any]:
    return _node(
        node_id,
        definition_id,
        inputs=(("image", "IMAGE"),),
        outputs=(("image", "IMAGE"),),
    )


def _link(
    link_id: int | str,
    source_id: int | str,
    source_slot: int,
    target_id: int | str,
    target_slot: int,
    slot_type: str = "IMAGE",
) -> list[Any]:
    return [link_id, source_id, source_slot, target_id, target_slot, slot_type]


def _linear_definition(
    definition_id: str,
    *,
    node_id: int | str = 1,
    node_type: str = "Pass",
    link_offset: int = 0,
) -> dict[str, Any]:
    first = link_offset + 1
    second = link_offset + 2
    return {
        "id": definition_id,
        "name": f"Definition {definition_id}",
        "version": 1,
        "inputNode": {"id": SCOPE_INPUT_NODE_ID, "bounding": [0, 0, 75, 100]},
        "outputNode": {"id": SCOPE_OUTPUT_NODE_ID, "bounding": [500, 0, 75, 100]},
        "inputs": [_port(definition_id, "input", 0, "image", "IMAGE", [first])],
        "outputs": [_port(definition_id, "output", 0, "image", "IMAGE", [second])],
        "nodes": [
            _node(
                node_id,
                node_type,
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            )
        ],
        "links": [
            _link(first, SCOPE_INPUT_NODE_ID, 0, node_id, 0),
            _link(second, node_id, 0, SCOPE_OUTPUT_NODE_ID, 0),
        ],
        "groups": [],
        "reroutes": [],
        "extra": {},
    }


def _reroute_definition(definition_id: str) -> dict[str, Any]:
    return {
        "id": definition_id,
        "name": f"Definition {definition_id}",
        "version": 1,
        "inputNode": {"id": SCOPE_INPUT_NODE_ID},
        "outputNode": {"id": SCOPE_OUTPUT_NODE_ID},
        "inputs": [_port(definition_id, "input", 0, "vae", "VAE", [1])],
        "outputs": [_port(definition_id, "output", 0, "vae", "VAE", [2])],
        "nodes": [
            {
                "id": 295,
                "type": "Reroute",
                "inputs": [{"name": "", "type": "*", "link": 1}],
                "outputs": [{"name": "", "type": "VAE", "links": [2]}],
                "widgets_values": [],
            }
        ],
        "links": [
            _link(1, SCOPE_INPUT_NODE_ID, 0, 295, 0, "VAE"),
            _link(2, 295, 0, SCOPE_OUTPUT_NODE_ID, 0, "VAE"),
        ],
        "groups": [],
        "reroutes": [],
        "extra": {},
    }


def _workflow(
    nodes: list[dict[str, Any]],
    definitions: list[dict[str, Any]] | dict[str, dict[str, Any]],
    *,
    links: list[Any] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "links": links or [],
        "definitions": {"subgraphs": definitions},
        "extra": {"sentinel": "unchanged"},
    }


def _path(node_id: int | str, definition_id: str) -> list[dict[str, Any]]:
    return [{"container_node_id": node_id, "subgraph_id": definition_id}]


def _error_code(callable_: Any, expected_code: str) -> WorkflowScopeError:
    with pytest.raises(WorkflowScopeError) as captured:
        callable_()
    assert captured.value.code == expected_code
    assert captured.value.as_issue()["code"] == expected_code
    return captured.value


def test_exact_nested_resolution_exposes_stable_boundary_facts() -> None:
    leaf = _linear_definition("leaf", node_id=101, link_offset=100)
    outer = _linear_definition("outer", node_id=7, node_type="leaf", link_offset=200)
    workflow = _workflow([_scope_node(10, "outer")], [outer, leaf])
    path = [
        {"container_node_id": 10, "subgraph_id": "outer"},
        {"container_node_id": 7, "subgraph_id": "leaf"},
    ]

    resolved = resolve_workflow_scope(workflow, path)

    assert resolved.model_dump(mode="json", by_alias=True)["schema"] == WORKFLOW_SCOPE_SCHEMA
    assert resolved.definition_id == "leaf"
    assert resolved.definition_hash == workflow_definition_hash(leaf)
    assert resolved.definition_name == "Definition leaf"
    assert resolved.node_count == 1
    assert resolved.edge_count == 2
    assert [item.model_dump() for item in resolved.boundary_inputs] == [
        {
            "kind": "scope_input",
            "slot_id": _uuid("leaf:input:0"),
            "slot_index": 0,
            "name": "image",
            "type": "IMAGE",
            "link_ids": [101],
        }
    ]
    assert resolved.boundary_outputs[0].slot_id == _uuid("leaf:output:0")
    assert resolved.boundary_outputs[0].link_ids == [102]


@pytest.mark.parametrize("resolver", [resolve_workflow_scope, project_workflow_scope])
def test_nested_resolution_rejects_a_malformed_ancestor_definition(resolver: Any) -> None:
    leaf = _linear_definition("leaf", node_id=101, link_offset=100)
    outer = _linear_definition("outer", node_id=7, node_type="leaf", link_offset=200)
    outer["inputs"][0]["linkIds"] = []
    workflow = _workflow([_scope_node(10, "outer")], [outer, leaf])
    path = [
        {"container_node_id": 10, "subgraph_id": "outer"},
        {"container_node_id": 7, "subgraph_id": "leaf"},
    ]

    _error_code(lambda: resolver(workflow, path), "boundary_link_ids_mismatch")


@pytest.mark.parametrize(
    "replacement",
    [
        {"name": "wrong", "type": "IMAGE"},
        {"name": "image", "type": "VIDEO"},
    ],
)
def test_scope_container_interface_requires_exact_name_and_type(
    replacement: dict[str, str],
) -> None:
    definition = _linear_definition("interface")
    container = _scope_node(10, "interface")
    container["inputs"][0] = replacement
    workflow = _workflow([container], [definition])

    _error_code(
        lambda: resolve_workflow_scope(workflow, _path(10, "interface")),
        "scope_container_interface_mismatch",
    )


def test_scope_container_interface_requires_exact_slot_order() -> None:
    definition = _linear_definition("ordered-interface")
    definition["inputs"].append(_port("ordered-interface", "input", 1, "mask", "MASK", [3]))
    definition["nodes"][0]["inputs"].append({"name": "mask", "type": "MASK"})
    definition["links"].append(_link(3, SCOPE_INPUT_NODE_ID, 1, 1, 1, "MASK"))
    container = _scope_node(10, "ordered-interface")
    container["inputs"] = [
        {"name": "mask", "type": "MASK"},
        {"name": "image", "type": "IMAGE"},
    ]
    workflow = _workflow([container], [definition])

    _error_code(
        lambda: resolve_workflow_scope(workflow, _path(10, "ordered-interface")),
        "scope_container_interface_mismatch",
    )


def test_typed_container_ids_are_distinct_and_never_coerced() -> None:
    definition = _linear_definition("shared")
    workflow = _workflow(
        [_scope_node(1, "shared"), _scope_node("1", "shared")],
        [definition],
    )

    numeric = resolve_workflow_scope(workflow, _path(1, "shared"))
    textual = resolve_workflow_scope(workflow, _path("1", "shared"))

    assert type(numeric.scope_path[0].container_node_id) is int
    assert type(textual.scope_path[0].container_node_id) is str
    inventory = enumerate_workflow_scope_instances(workflow)
    assert {
        (type(item.scope_path[0].container_node_id).__name__, item.scope_path[0].container_node_id)
        for item in inventory.instances
    } == {("int", 1), ("str", "1")}

    with pytest.raises(ValidationError):
        WorkflowScopeStep(container_node_id=True, subgraph_id="shared")


def test_root_negative_container_id_is_not_a_virtual_boundary_collision() -> None:
    definition = _linear_definition("negative-root")
    workflow = _workflow([_scope_node(SCOPE_INPUT_NODE_ID, "negative-root")], [definition])

    resolved = resolve_workflow_scope(
        workflow,
        _path(SCOPE_INPUT_NODE_ID, "negative-root"),
    )

    assert resolved.scope_path[0].container_node_id == SCOPE_INPUT_NODE_ID


def test_definition_hash_is_mapping_order_independent_and_value_exact() -> None:
    definition = _linear_definition("stable")
    reordered = dict(reversed(list(definition.items())))
    changed = deepcopy(definition)
    changed["nodes"][0]["widgets_values"] = [0.5]

    assert workflow_definition_hash(definition) == workflow_definition_hash(reordered)
    assert workflow_definition_hash(definition) != workflow_definition_hash(changed)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"x": 1e-7}, "6fe21de92ebeca874cd63605595cbc888309ca35d4e1b3233701b93674fde4a0"),
        ({"x": 1e-6}, "20857fe3e561827e80e0bc0c980fc55fb76fe8fa083b961fd0b7701b3a9c4c9d"),
        ({"x": 1e20}, "9481add5f0e8b965b2fff70e0e56dc40df36150bd072b1fc44265e2d3392e9a3"),
        (
            {"x": 18_446_744_073_709_552_000},
            "882dfa5af02c3bb6368ac6bfb67ae4f542e7134bdf8da43d0ba868ed4ed9ca4b",
        ),
        ({"x": -0.0}, "86c114de3d4a984398811df119dc51205d7e54292927f8ca6a98e5c630075287"),
        (
            {"extra": {"\ue000": "bmp", "\U00010000": "astral"}},
            "ee77d87f4a21863fb57ee42246864af31c22fef375d5eb7cdc871da4210f6cae",
        ),
        (
            {"nested": [None, True, False, "a\n\0", {"😀": "ok"}]},
            "a1396cf88511d623523a42052856cb1d3f6158ea8f4056b5abbcb467ed592b10",
        ),
    ],
)
def test_definition_hash_v2_matches_cross_runtime_golden_vectors(
    definition: dict[str, Any],
    expected: str,
) -> None:
    assert workflow_definition_hash(definition) == expected


def test_definition_hash_v2_uses_the_browser_number_domain() -> None:
    assert workflow_definition_hash({"x": -0.0}) == workflow_definition_hash({"x": 0})
    assert workflow_definition_hash({"x": 1}) == workflow_definition_hash({"x": 1.0})
    assert workflow_definition_hash({"x": 10**20}) == workflow_definition_hash({"x": 1e20})
    _error_code(
        lambda: workflow_definition_hash({"x": 10**400}),
        "non_json_scope_value",
    )


@pytest.mark.parametrize(
    "non_json",
    [
        {"value": (1, 2)},
        {1: "integer key"},
        {"value": float("inf")},
        {"value": "\ud800"},
    ],
)
def test_definition_hash_rejects_non_exact_json_values(non_json: Any) -> None:
    _error_code(
        lambda: workflow_definition_hash(non_json),
        "non_json_scope_value",
    )


@pytest.mark.parametrize(
    ("value", "limits", "code"),
    [
        (
            {"nested": {"too": {"deep": True}}},
            WorkflowScopeLimits(max_definition_json_depth=2),
            "definition_json_depth_exceeded",
        ),
        (
            {"many": [1, 2, 3]},
            WorkflowScopeLimits(max_definition_json_items=4),
            "definition_json_item_limit_exceeded",
        ),
        (
            {"large": "x" * 1_025},
            WorkflowScopeLimits(max_definition_json_bytes=1_024),
            "definition_json_size_exceeded",
        ),
    ],
)
def test_definition_hash_work_is_strictly_bounded(
    value: dict[str, Any],
    limits: WorkflowScopeLimits,
    code: str,
) -> None:
    _error_code(lambda: workflow_definition_hash(value, limits=limits), code)


@pytest.mark.parametrize("resolver", [resolve_workflow_scope, project_workflow_scope])
def test_scope_resolution_rejects_non_json_metadata_before_any_deepcopy(resolver: Any) -> None:
    definition = _linear_definition("no-copy")
    definition["nodes"][0]["properties"] = {"bomb": _DeepcopyBomb()}
    workflow = _workflow([_scope_node(1, "no-copy")], [definition])

    _error_code(
        lambda: resolver(workflow, _path(1, "no-copy")),
        "non_json_scope_value",
    )


def test_expected_definition_hash_is_an_exact_stale_scope_pin() -> None:
    definition = _linear_definition("pinned")
    workflow = _workflow([_scope_node(1, "pinned")], [definition])
    expected = workflow_definition_hash(definition)
    resolve_workflow_scope(workflow, _path(1, "pinned"), expected_definition_hash=expected)

    changed = deepcopy(workflow)
    changed["definitions"]["subgraphs"][0]["name"] = "Changed"
    error = _error_code(
        lambda: resolve_workflow_scope(
            changed,
            _path(1, "pinned"),
            expected_definition_hash=expected,
        ),
        "scope_definition_changed",
    )
    assert set(error.details) == {"expected_definition_hash", "actual_definition_hash"}


def test_projection_keeps_virtual_ids_inside_excluded_compiler_material() -> None:
    definition = _linear_definition("projected", node_id=55)
    workflow = _workflow([_scope_node(8, "projected")], [definition])
    before = deepcopy(workflow)

    projection = project_workflow_scope(workflow, _path(8, "projected"))

    assert projection.schema_ == WORKFLOW_SCOPE_PROJECTION_SCHEMA
    assert {item.node_id for item in projection.graph.nodes} == {
        SCOPE_INPUT_NODE_ID,
        SCOPE_OUTPUT_NODE_ID,
        55,
    }
    types = {item.node_id: item.node_type for item in projection.graph.nodes}
    assert types[SCOPE_INPUT_NODE_ID] == SCOPE_INPUT_NODE_TYPE
    assert types[SCOPE_OUTPUT_NODE_ID] == SCOPE_OUTPUT_NODE_TYPE
    assert projection.compiler_workflow["nodes"][0]["id"] == SCOPE_INPUT_NODE_ID
    public = projection.model_dump(mode="json", by_alias=True)
    assert set(public) == {"schema", "resolution"}
    assert "-10" not in str(public)
    assert "-20" not in str(public)
    assert workflow == before
    projection.compiler_workflow["nodes"][1]["widgets_values"].append("compiler-only")
    assert workflow == before


def test_projection_requires_exact_bounded_widget_arrays() -> None:
    definition = _linear_definition("widget-shape")
    definition["nodes"][0]["widgets_values"] = {"unexpected": "mapping"}
    workflow = _workflow([_scope_node(8, "widget-shape")], [definition])
    _error_code(
        lambda: project_workflow_scope(workflow, _path(8, "widget-shape")),
        "invalid_scope_widget_values",
    )

    definition = _linear_definition("widget-bound")
    definition["nodes"][0]["widgets_values"] = _ExplodingOversizedList([1])
    workflow = _workflow([_scope_node(8, "widget-bound")], [definition])
    _error_code(
        lambda: project_workflow_scope(
            workflow,
            _path(8, "widget-bound"),
            limits=WorkflowScopeLimits(max_widgets_per_node=0),
        ),
        "node_widget_limit_exceeded",
    )


def test_unique_definition_is_instance_writable_without_shared_acknowledgement() -> None:
    definition = _linear_definition("unique")
    workflow = _workflow([_scope_node(4, "unique")], [definition])

    policy = resolve_workflow_scope_edit(workflow, _path(4, "unique"))

    assert policy.allowed
    assert policy.status == "unique_definition"
    assert policy.instance_count == 1
    assert policy.affected_scope_paths == [
        [WorkflowScopeStep(container_node_id=4, subgraph_id="unique")]
    ]


def test_reused_definition_requires_explicit_complete_shared_acknowledgement() -> None:
    definition = _linear_definition("shared")
    workflow = _workflow(
        [_scope_node(20, "shared"), _scope_node(10, "shared")],
        [definition],
    )
    selected = _path(20, "shared")

    choice = resolve_workflow_scope_edit(workflow, selected)

    assert not choice.allowed
    assert choice.status == "shared_definition_requires_acknowledgement"
    assert choice.instance_count == 2
    assert [path[0].container_node_id for path in choice.affected_scope_paths] == [10, 20]

    _error_code(
        lambda: resolve_workflow_scope_edit(
            workflow,
            selected,
            requested_mode="shared_definition",
        ),
        "affected_scope_paths_required",
    )
    _error_code(
        lambda: resolve_workflow_scope_edit(
            workflow,
            selected,
            requested_mode="shared_definition",
            acknowledged_scope_paths=[_path(20, "shared")],
        ),
        "affected_scope_paths_mismatch",
    )

    shared = resolve_workflow_scope_edit(
        workflow,
        selected,
        requested_mode="shared_definition",
        acknowledged_scope_paths=[_path(20, "shared"), _path(10, "shared")],
    )
    assert shared.allowed
    assert shared.status == "shared_definition"
    assert shared.instance_count == 2


def test_nested_reuse_inventory_returns_every_exact_affected_path() -> None:
    leaf = _linear_definition("leaf", node_id=101, link_offset=100)
    outer = _linear_definition("outer", node_id=5, node_type="leaf", link_offset=200)
    workflow = _workflow(
        [_scope_node(1, "outer"), _scope_node(2, "outer")],
        [outer, leaf],
    )

    inventory = enumerate_workflow_scope_instances(workflow)
    leaf_paths = [item.scope_path for item in inventory.instances if item.definition_id == "leaf"]

    assert inventory.instance_count == 4
    assert inventory.definition_count == 2
    assert [[step.container_node_id for step in path] for path in leaf_paths] == [
        [1, 5],
        [2, 5],
    ]
    policy = resolve_workflow_scope_edit(
        workflow,
        [
            {"container_node_id": 1, "subgraph_id": "outer"},
            {"container_node_id": 5, "subgraph_id": "leaf"},
        ],
    )
    assert not policy.allowed
    assert policy.instance_count == 2


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda definition: definition["inputNode"].update(id=-11), "virtual_boundary_id_mismatch"),
        (
            lambda definition: definition["inputNode"].update(id=-10.0),
            "virtual_boundary_id_mismatch",
        ),
        (
            lambda definition: definition["outputNode"].update(id=-20.0),
            "virtual_boundary_id_mismatch",
        ),
        (
            lambda definition: definition["inputs"][0].update(id="not-a-uuid"),
            "invalid_boundary_slot_id",
        ),
        (
            lambda definition: definition["outputs"][0].update(id=definition["inputs"][0]["id"]),
            "duplicate_boundary_slot_id",
        ),
        (
            lambda definition: definition["inputs"][0].update(linkIds=[]),
            "boundary_link_ids_mismatch",
        ),
        (
            lambda definition: definition["outputs"][0].update(linkIds=[2, 3]),
            "boundary_link_ids_mismatch",
        ),
    ],
)
def test_boundary_endpoint_facts_fail_closed(mutate: Any, code: str) -> None:
    definition = _linear_definition("boundary")
    mutate(definition)
    workflow = _workflow([_scope_node(1, "boundary")], [definition])

    _error_code(lambda: resolve_workflow_scope(workflow, _path(1, "boundary")), code)


def test_multiple_physical_edges_into_one_scope_output_are_rejected() -> None:
    definition = _linear_definition("occupied")
    definition["nodes"].append(_node(2, "Second", outputs=(("image", "IMAGE"),)))
    definition["links"].append(_link(3, 2, 0, SCOPE_OUTPUT_NODE_ID, 0))
    definition["outputs"][0]["linkIds"] = [2, 3]
    workflow = _workflow([_scope_node(1, "occupied")], [definition])

    _error_code(
        lambda: project_workflow_scope(workflow, _path(1, "occupied")),
        "occupied_scope_output",
    )


def test_multiple_physical_edges_into_any_node_input_are_rejected() -> None:
    definition = _linear_definition("occupied-input")
    definition["nodes"].append(_node(2, "Second", outputs=(("image", "IMAGE"),)))
    definition["links"].append(_link(3, 2, 0, 1, 0))
    workflow = _workflow([_scope_node(1, "occupied-input")], [definition])

    _error_code(
        lambda: project_workflow_scope(workflow, _path(1, "occupied-input")),
        "occupied_scope_input",
    )


def test_scope_graph_cycle_and_wrong_boundary_direction_are_classified() -> None:
    cyclic = _linear_definition("cyclic")
    cyclic["nodes"] = [
        _node(1, "A", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
        _node(2, "B", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),)),
    ]
    cyclic["links"] = [_link(1, 1, 0, 2, 0), _link(2, 2, 0, 1, 0)]
    cyclic["inputs"][0]["linkIds"] = []
    cyclic["outputs"][0]["linkIds"] = []
    workflow = _workflow([_scope_node(9, "cyclic")], [cyclic])
    _error_code(lambda: project_workflow_scope(workflow, _path(9, "cyclic")), "scope_graph_cycle")

    backwards = _linear_definition("backwards")
    backwards["links"][0] = _link(1, 1, 0, SCOPE_INPUT_NODE_ID, 0)
    backwards["nodes"][0]["outputs"].append({"name": "extra", "type": "IMAGE"})
    backwards["nodes"][0]["inputs"] = []
    workflow = _workflow([_scope_node(9, "backwards")], [backwards])
    _error_code(
        lambda: project_workflow_scope(workflow, _path(9, "backwards")),
        "invalid_boundary_edge_direction",
    )


def test_exact_real_shaped_reroute_gets_private_stable_slot_identities() -> None:
    definition = _reroute_definition("reroute")
    workflow = _workflow(
        [
            _node(
                9,
                "reroute",
                inputs=(("vae", "VAE"),),
                outputs=(("vae", "VAE"),),
            )
        ],
        [definition],
    )
    expected_hash = workflow_definition_hash(definition)

    projection = project_workflow_scope(workflow, _path(9, "reroute"))

    assert projection.resolution.definition_hash == expected_hash
    compiler_reroute = projection.compiler_workflow["nodes"][1]
    assert compiler_reroute["inputs"] == [{"name": "__fl_mcp_reroute_input_0__", "type": "VAE"}]
    assert compiler_reroute["outputs"] == [{"name": "__fl_mcp_reroute_output_0__", "type": "VAE"}]
    assert definition["nodes"][0]["inputs"][0]["name"] == ""


def test_reroute_requires_one_concrete_physical_type() -> None:
    definition = _reroute_definition("reroute-conflict")
    definition["nodes"][0]["outputs"][0]["type"] = "*"
    definition["links"][1][5] = "MODEL"
    definition["outputs"][0]["type"] = "MODEL"
    workflow = _workflow(
        [
            _node(
                9,
                "reroute-conflict",
                inputs=(("vae", "VAE"),),
                outputs=(("vae", "MODEL"),),
            )
        ],
        [definition],
    )

    _error_code(
        lambda: project_workflow_scope(workflow, _path(9, "reroute-conflict")),
        "reroute_type_mismatch",
    )


def test_blank_slots_remain_invalid_for_arbitrary_non_reroute_nodes() -> None:
    definition = _linear_definition("blank")
    definition["nodes"][0]["inputs"][0]["name"] = ""
    workflow = _workflow([_scope_node(9, "blank")], [definition])

    _error_code(
        lambda: project_workflow_scope(workflow, _path(9, "blank")),
        "unresolved_scope_fact",
    )


def test_recursive_definition_reference_is_rejected_during_inventory() -> None:
    recursive = _linear_definition("recursive", node_type="recursive")
    workflow = _workflow([_scope_node(1, "recursive")], [recursive])

    _error_code(
        lambda: enumerate_workflow_scope_instances(workflow),
        "recursive_subgraph_definition",
    )

    _error_code(
        lambda: resolve_workflow_scope(
            workflow,
            [
                {"container_node_id": 1, "subgraph_id": "recursive"},
                {"container_node_id": 1, "subgraph_id": "recursive"},
            ],
        ),
        "recursive_subgraph_definition",
    )


@pytest.mark.parametrize(
    ("limits", "workflow_factory", "code"),
    [
        (
            WorkflowScopeLimits(max_instances=1),
            lambda: _workflow(
                [_scope_node(1, "shared"), _scope_node(2, "shared")],
                [_linear_definition("shared")],
            ),
            "instance_limit_exceeded",
        ),
        (
            WorkflowScopeLimits(max_scope_nodes=1),
            lambda: _workflow(
                [_scope_node(1, "many")],
                [
                    {
                        **_linear_definition("many"),
                        "nodes": [
                            _node(1, "A", outputs=(("image", "IMAGE"),)),
                            _node(2, "B", inputs=(("image", "IMAGE"),)),
                        ],
                        "links": [_link(1, 1, 0, 2, 0)],
                        "inputs": [_port("many", "input", 0, "image", "IMAGE", [])],
                        "outputs": [_port("many", "output", 0, "image", "IMAGE", [])],
                    }
                ],
            ),
            "scope_node_limit_exceeded",
        ),
        (
            WorkflowScopeLimits(max_scope_edges=1),
            lambda: _workflow([_scope_node(1, "edges")], [_linear_definition("edges")]),
            "scope_edge_limit_exceeded",
        ),
        (
            WorkflowScopeLimits(max_scope_ports=0),
            lambda: _workflow([_scope_node(1, "ports")], [_linear_definition("ports")]),
            "scope_port_limit_exceeded",
        ),
        (
            WorkflowScopeLimits(max_slots_per_node=0),
            lambda: _workflow([_scope_node(1, "slots")], [_linear_definition("slots")]),
            "node_slot_limit_exceeded",
        ),
    ],
)
def test_scope_expansion_and_projection_limits_fail_classified(
    limits: WorkflowScopeLimits,
    workflow_factory: Any,
    code: str,
) -> None:
    workflow = workflow_factory()
    _error_code(lambda: enumerate_workflow_scope_instances(workflow, limits=limits), code)


def test_definition_node_and_link_limits_fail_before_mapping_iteration() -> None:
    workflow = _workflow([], [])
    workflow["definitions"]["subgraphs"] = _ExplodingOversizedMapping({"first": {}, "second": {}})
    _error_code(
        lambda: enumerate_workflow_scope_instances(
            workflow,
            limits=WorkflowScopeLimits(max_definitions=1),
        ),
        "definition_limit_exceeded",
    )

    workflow = _workflow([], [])
    workflow["nodes"] = _ExplodingOversizedMapping({"first": {}, "second": {}})
    _error_code(
        lambda: enumerate_workflow_scope_instances(
            workflow,
            limits=WorkflowScopeLimits(max_expanded_nodes=1),
        ),
        "expanded_node_limit_exceeded",
    )

    workflow = _workflow([], [])
    workflow["links"] = _ExplodingOversizedMapping({"edge": []})
    _error_code(
        lambda: enumerate_workflow_scope_instances(
            workflow,
            limits=WorkflowScopeLimits(max_expanded_edges=0),
        ),
        "expanded_edge_limit_exceeded",
    )


def test_port_and_slot_limits_fail_before_mapping_iteration() -> None:
    definition = _linear_definition("bounded-ports")
    definition["inputs"] = _ExplodingOversizedMapping(
        {
            "0": definition["inputs"][0],
            "1": deepcopy(definition["inputs"][0]),
        }
    )
    workflow = _workflow([_scope_node(1, "bounded-ports")], [definition])
    _error_code(
        lambda: resolve_workflow_scope(
            workflow,
            _path(1, "bounded-ports"),
            limits=WorkflowScopeLimits(max_scope_ports=1),
        ),
        "scope_port_limit_exceeded",
    )

    definition = _linear_definition("bounded-slots")
    definition["nodes"][0]["inputs"] = _ExplodingOversizedMapping(
        {
            "0": definition["nodes"][0]["inputs"][0],
            "1": deepcopy(definition["nodes"][0]["inputs"][0]),
        }
    )
    workflow = _workflow([_scope_node(1, "bounded-slots")], [definition])
    _error_code(
        lambda: resolve_workflow_scope(
            workflow,
            _path(1, "bounded-slots"),
            limits=WorkflowScopeLimits(max_slots_per_node=1),
        ),
        "node_slot_limit_exceeded",
    )


def test_path_and_acknowledgement_limits_fail_before_sequence_iteration() -> None:
    definition = _linear_definition("bounded-path")
    workflow = _workflow([_scope_node(1, "bounded-path")], [definition])
    oversized = _ExplodingOversizedList(
        [
            {"container_node_id": 1, "subgraph_id": "bounded-path"},
            {"container_node_id": 1, "subgraph_id": "bounded-path"},
        ]
    )
    _error_code(
        lambda: resolve_workflow_scope(
            workflow,
            oversized,
            limits=WorkflowScopeLimits(max_depth=1),
        ),
        "scope_depth_limit_exceeded",
    )
    _error_code(
        lambda: resolve_workflow_scope_edit(
            workflow,
            _path(1, "bounded-path"),
            acknowledged_scope_paths=oversized,
            limits=WorkflowScopeLimits(max_instances=1),
        ),
        "acknowledgement_limit_exceeded",
    )


def test_depth_limit_is_enforced_before_recursive_expansion() -> None:
    leaf = _linear_definition("leaf", node_id=3, link_offset=300)
    middle = _linear_definition("middle", node_id=2, node_type="leaf", link_offset=200)
    outer = _linear_definition("outer", node_id=1, node_type="middle", link_offset=100)
    workflow = _workflow([_scope_node(9, "outer")], [outer, middle, leaf])

    _error_code(
        lambda: enumerate_workflow_scope_instances(
            workflow,
            limits=WorkflowScopeLimits(max_depth=2),
        ),
        "scope_depth_limit_exceeded",
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda workflow: workflow["definitions"]["subgraphs"].append(
                deepcopy(workflow["definitions"]["subgraphs"][0])
            ),
            "duplicate_subgraph_definition_id",
        ),
        (
            lambda workflow: workflow["definitions"]["subgraphs"][0]["nodes"].append(
                deepcopy(workflow["definitions"]["subgraphs"][0]["nodes"][0])
            ),
            "duplicate_scope_node_id",
        ),
        (
            lambda workflow: workflow["definitions"]["subgraphs"][0]["links"].append(
                deepcopy(workflow["definitions"]["subgraphs"][0]["links"][0])
            ),
            "duplicate_scope_link_id",
        ),
        (
            lambda workflow: workflow["definitions"]["subgraphs"][0]["nodes"][0].update(id=True),
            "invalid_scope_node_id",
        ),
        (
            lambda workflow: workflow["definitions"]["subgraphs"][0]["links"][0].__setitem__(
                1, 999
            ),
            "scope_link_endpoint_missing",
        ),
    ],
)
def test_malformed_scope_identity_and_topology_are_classified(mutate: Any, code: str) -> None:
    workflow = _workflow([_scope_node(8, "malformed")], [_linear_definition("malformed")])
    mutate(workflow)

    _error_code(lambda: project_workflow_scope(workflow, _path(8, "malformed")), code)


def test_object_collections_resolve_without_losing_exact_ids() -> None:
    definition = _linear_definition("mapped", node_id="inside", link_offset=40)
    definition["nodes"] = {"inside": definition["nodes"][0]}
    definition["links"] = {
        "first": {
            "id": 41,
            "origin_id": SCOPE_INPUT_NODE_ID,
            "origin_slot": 0,
            "target_id": "inside",
            "target_slot": 0,
            "type": "IMAGE",
        },
        "second": {
            "id": 42,
            "origin_id": "inside",
            "origin_slot": 0,
            "target_id": SCOPE_OUTPUT_NODE_ID,
            "target_slot": 0,
            "type": "IMAGE",
        },
    }
    workflow = _workflow(
        [_scope_node("container", "mapped")],
        {"mapped": definition},
    )

    projection = project_workflow_scope(workflow, _path("container", "mapped"))

    assert projection.resolution.node_count == 1
    assert {edge.source_node_id for edge in projection.graph.edges} == {
        SCOPE_INPUT_NODE_ID,
        "inside",
    }


@pytest.mark.parametrize(
    "conflict",
    [
        {"link_id": "1"},
        {"source_node_id": 1},
        {"source_output_index": 1},
        {"target_node_id": 2},
        {"target_input_index": 1},
        {"link_type": "VIDEO"},
    ],
)
def test_mapping_link_alias_conflicts_are_rejected_before_normalization(
    conflict: dict[str, Any],
) -> None:
    definition = _linear_definition("alias-conflict")
    mapping_link = {
        "id": 1,
        "origin_id": SCOPE_INPUT_NODE_ID,
        "origin_slot": 0,
        "target_id": 1,
        "target_slot": 0,
        "type": "IMAGE",
        **conflict,
    }
    definition["links"][0] = mapping_link
    workflow = _workflow([_scope_node(7, "alias-conflict")], [definition])

    _error_code(
        lambda: project_workflow_scope(workflow, _path(7, "alias-conflict")),
        "conflicting_scope_link_aliases",
    )


def test_identical_mapping_link_aliases_compile_from_one_canonical_parse() -> None:
    definition = _linear_definition("alias-identical")
    definition["links"][0] = {
        "id": 1,
        "link_id": 1,
        "origin_id": SCOPE_INPUT_NODE_ID,
        "source_id": SCOPE_INPUT_NODE_ID,
        "source_node_id": SCOPE_INPUT_NODE_ID,
        "from_node_id": SCOPE_INPUT_NODE_ID,
        "origin_slot": 0,
        "source_slot": 0,
        "source_output_index": 0,
        "target_id": 1,
        "target_node_id": 1,
        "to_node_id": 1,
        "target_slot": 0,
        "target_input_index": 0,
        "type": "IMAGE",
        "link_type": "IMAGE",
    }
    workflow = _workflow([_scope_node(7, "alias-identical")], [definition])

    projection = project_workflow_scope(workflow, _path(7, "alias-identical"))

    assert projection.compiler_workflow["links"][0] == [
        1,
        SCOPE_INPUT_NODE_ID,
        0,
        1,
        0,
        "IMAGE",
    ]
    assert projection.graph.edges[0].source_node_id == SCOPE_INPUT_NODE_ID


@pytest.mark.parametrize(
    "slots",
    [
        {
            "1": {"name": "aux", "type": "IMAGE"},
            "0": {"name": "image", "type": "IMAGE"},
        },
        {
            "z": {"name": "aux", "type": "IMAGE"},
            "a": {"name": "image", "type": "IMAGE"},
        },
    ],
)
def test_slot_mappings_use_the_same_canonical_order_as_normalization(
    slots: dict[str, Any],
) -> None:
    definition = _linear_definition("mapped-slots")
    definition["nodes"][0]["inputs"] = deepcopy(slots)
    definition["nodes"][0]["outputs"] = deepcopy(slots)
    workflow = _workflow([_scope_node(7, "mapped-slots")], [definition])

    projection = project_workflow_scope(workflow, _path(7, "mapped-slots"))

    by_source = {edge.source_node_id: edge for edge in projection.graph.edges}
    assert by_source[SCOPE_INPUT_NODE_ID].target_input == "image"
    assert by_source[1].source_output == "image"
    assert projection.compiler_workflow["nodes"][1]["inputs"][0]["name"] == "image"


def test_mixed_named_and_numeric_slot_mapping_is_classified() -> None:
    definition = _linear_definition("mixed-slots")
    definition["nodes"][0]["inputs"] = {
        "0": {"name": "image", "type": "IMAGE"},
        "named": {"name": "other", "type": "IMAGE"},
    }
    workflow = _workflow([_scope_node(7, "mixed-slots")], [definition])

    _error_code(
        lambda: project_workflow_scope(workflow, _path(7, "mixed-slots")),
        "mixed_scope_slot_keys",
    )


def test_scope_path_must_match_every_container_type_and_definition() -> None:
    definition = _linear_definition("actual")
    workflow = _workflow([_scope_node(1, "actual")], [definition])

    _error_code(
        lambda: resolve_workflow_scope(workflow, _path(1, "claimed")),
        "scope_container_type_mismatch",
    )
    _error_code(
        lambda: resolve_workflow_scope(workflow, _path(2, "actual")),
        "scope_container_missing",
    )
    missing = _workflow([_scope_node(1, "missing")], [definition])
    _error_code(
        lambda: resolve_workflow_scope(missing, _path(1, "missing")),
        "subgraph_definition_missing",
    )


@pytest.mark.parametrize(
    ("name", "code"),
    [
        (42, "unresolved_scope_fact"),
        ("x" * 513, "scope_fact_too_long"),
    ],
)
def test_optional_definition_name_failures_remain_classified(name: Any, code: str) -> None:
    definition = _linear_definition("named")
    definition["name"] = name
    workflow = _workflow([_scope_node(1, "named")], [definition])

    _error_code(lambda: resolve_workflow_scope(workflow, _path(1, "named")), code)


def test_acknowledgement_rejects_typed_path_substitution_and_duplicates() -> None:
    definition = _linear_definition("shared")
    workflow = _workflow(
        [_scope_node(1, "shared"), _scope_node("1", "shared")],
        [definition],
    )

    _error_code(
        lambda: resolve_workflow_scope_edit(
            workflow,
            _path(1, "shared"),
            requested_mode="shared_definition",
            acknowledged_scope_paths=[_path(1, "shared"), _path(1, "shared")],
        ),
        "affected_scope_paths_mismatch",
    )
    policy = resolve_workflow_scope_edit(
        workflow,
        _path(1, "shared"),
        requested_mode="shared_definition",
        acknowledged_scope_paths=[_path("1", "shared"), _path(1, "shared")],
    )
    assert policy.allowed
