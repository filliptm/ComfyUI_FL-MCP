from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
import workflow_branch_operations as branch_operations
from node_library import catalog_contract_hash
from pydantic import ValidationError
from workflow_branch_operations import (
    BranchOperationResult,
    RemoveBranchRequest,
    ReplaceBranchRequest,
    ResolveBranchSuccessorsRequest,
    compile_workflow_branch_operation,
    resolve_workflow_branch_successors,
)
from workflow_branches import WorkflowBranchRecord, discover_workflow_branches
from workflow_graph_patch import ApplyGraphPatchRequest, ApplyScopedGraphPatchRequest

WORKFLOW_IDENTITY = "fl-mcp-workflow:branch-operations:1"
GRAPH_HASH = "a" * 64


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"fl-mcp-branch-operation:{label}"))


def _slots(values: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"name": name, "type": slot_type} for name, slot_type in values]


def _node(
    node_id: int | str,
    node_type: str,
    *,
    inputs: tuple[tuple[str, str], ...] = (),
    outputs: tuple[tuple[str, str], ...] = (),
    widgets: list[Any] | None = None,
    pos: tuple[int, int] = (0, 0),
    size: tuple[int, int] = (180, 120),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "inputs": _slots(inputs),
        "outputs": _slots(outputs),
        "widgets_values": list(widgets or []),
        "pos": list(pos),
        "size": list(size),
    }


def _link(
    link_id: int,
    source: int | str,
    source_slot: int,
    target: int | str,
    target_slot: int,
    slot_type: str = "IMAGE",
) -> list[Any]:
    return [link_id, source, source_slot, target, target_slot, slot_type]


def _with_planned_layout(
    node: dict[str, Any],
    create: dict[str, Any],
) -> dict[str, Any]:
    """Materialize the exact compiler-pinned layout in a simulated postgraph."""

    hint = create["layout_hint"]
    node["pos"] = [hint["x"], hint["y"]]
    node["size"] = [hint["width"], hint["height"]]
    return node


def _schema(
    *,
    required: dict[str, Any] | None = None,
    optional: dict[str, Any] | None = None,
    hidden: dict[str, Any] | None = None,
    outputs: list[str] | None = None,
    output_names: list[str] | None = None,
    python_module: str = "nodes",
    **extra: Any,
) -> dict[str, Any]:
    inputs: dict[str, Any] = {"required": required or {}}
    if optional:
        inputs["optional"] = optional
    if hidden:
        inputs["hidden"] = hidden
    return {
        "input": inputs,
        "output": outputs or [],
        "output_name": output_names or [],
        "python_module": python_module,
        **extra,
    }


def _dynamic_target_schema() -> dict[str, Any]:
    return {
        "input": {
            "required": {
                "prompt": ["STRING", {"default": ""}],
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "nano",
                                "inputs": {
                                    "required": {
                                        "aspect_ratio": [["16:9", "21:9"]],
                                        "resolution": [["1K", "2K"]],
                                        "thinking_level": [["MINIMAL", "HIGH"]],
                                        "images": [
                                            "COMFY_AUTOGROW_V3",
                                            {
                                                "template": {
                                                    "input": {
                                                        "required": {
                                                            "image": ["IMAGE", {}]
                                                        }
                                                    },
                                                    "names": ["image_1", "image_2"],
                                                    "min": 0,
                                                }
                                            },
                                        ],
                                    }
                                },
                            },
                            {
                                "key": "text-only",
                                "inputs": {
                                    "required": {"quality": [["fast", "high"]]}
                                },
                            },
                        ]
                    },
                ],
            }
        },
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
        "python_module": "custom_nodes.dynamic",
    }


def _catalog() -> dict[str, Any]:
    return {
        "ImageSource": _schema(outputs=["IMAGE"], output_names=["image"]),
        "ConditioningSource": _schema(
            outputs=["CONDITIONING"],
            output_names=["conditioning"],
        ),
        "Arm": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "conditioning": ["CONDITIONING", {"forceInput": True}],
                "strength": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0}],
            },
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "OtherArm": _schema(
            required={"image": ["IMAGE", {"forceInput": True}]},
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "Join": _schema(
            required={
                "a": ["IMAGE", {"forceInput": True}],
                "b": ["IMAGE", {"forceInput": True}],
            },
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "Replacement": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "conditioning": ["CONDITIONING", {"forceInput": True}],
                "amount": ["FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0}],
            },
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "Pass": _schema(
            required={"image": ["IMAGE", {"forceInput": True}]},
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "ImageToMask": _schema(
            required={"image": ["IMAGE", {"forceInput": True}]},
            outputs=["MASK"],
            output_names=["mask"],
        ),
        "MaskSink": _schema(
            required={"mask": ["MASK", {"forceInput": True}]},
        ),
        "SafeFilter": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "strength": ["FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0}],
            },
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "SafeSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "label": ["STRING", {"default": "result"}],
            },
        ),
        "DynamicReplacement": _schema(
            required={
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "active",
                                "inputs": {
                                    "required": {
                                        "image": ["IMAGE", {"forceInput": True}],
                                        "amount": ["FLOAT", {"default": 0.5}],
                                    }
                                },
                            }
                        ]
                    },
                ]
            },
            outputs=["IMAGE"],
            output_names=["image"],
        ),
        "PartnerSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "prompt": ["STRING", {"default": ""}],
            },
            python_module="comfy_api_nodes.partner",
            api_node=True,
        ),
        "SecretSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "api_key": ["STRING", {"default": ""}],
            },
        ),
        "CamelSecretSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "clientSecret": ["STRING", {"default": ""}],
            },
        ),
        "AccessKeySink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "accessKey": ["STRING", {"default": ""}],
            },
        ),
        "NestedCredentialSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "config": ["STRING", {"default": {}}],
            },
        ),
        "HiddenAuthSink": _schema(
            required={"image": ["IMAGE", {"forceInput": True}]},
            hidden={"accessToken": "STRING"},
        ),
        "UploadSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "reference": [["asset.png"], {"image_upload": True}],
            },
        ),
        "VideoUploadSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "video": [["clip.mp4"], {"video_upload": True}],
            },
        ),
        "AudioUploadSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "audio": [["track.wav"], {"audioUpload": True}],
            },
        ),
        "FileUploadSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "file": [["payload.bin"], {"file-upload": "input"}],
            },
        ),
        "DirectoryUploadSink": _schema(
            required={
                "image": ["IMAGE", {"forceInput": True}],
                "directory": [["assets"], {"directory_upload": True}],
            },
        ),
        "DynamicNanoTarget": _dynamic_target_schema(),
    }


def _side_input_diamond() -> dict[str, Any]:
    return {
        "nodes": [
            _node(1, "ImageSource", outputs=(("image", "IMAGE"),)),
            _node(2, "Arm", inputs=(("image", "IMAGE"), ("conditioning", "CONDITIONING")), outputs=(("image", "IMAGE"),), widgets=[1.0], pos=(200, 0)),
            _node(3, "OtherArm", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),), pos=(200, 200)),
            _node(4, "Join", inputs=(("a", "IMAGE"), ("b", "IMAGE")), outputs=(("image", "IMAGE"),), pos=(500, 100)),
            _node(9, "ConditioningSource", outputs=(("conditioning", "CONDITIONING"),), pos=(0, -200)),
        ],
        "links": [
            _link(1, 1, 0, 2, 0),
            _link(2, 1, 0, 3, 0),
            _link(3, 2, 0, 4, 0),
            _link(4, 3, 0, 4, 1),
            _link(5, 9, 0, 2, 1, "CONDITIONING"),
        ],
        "definitions": {"subgraphs": []},
    }


def _linear_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            _node(1, "ImageSource", outputs=(("image", "IMAGE"),)),
            _node(2, "Pass", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),), pos=(200, 0)),
            _node(3, "SafeSink", inputs=(("image", "IMAGE"),), widgets=["result"], pos=(400, 0)),
        ],
        "links": [_link(1, 1, 0, 2, 0), _link(2, 2, 0, 3, 0)],
        "definitions": {"subgraphs": []},
    }


def _nested_linear_workflow(*, reused: bool = False) -> dict[str, Any]:
    definition = {
        "id": "nested-def",
        "name": "Nested",
        "inputs": [
            {
                "id": _uuid("nested-input"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [1],
            }
        ],
        "outputs": [
            {
                "id": _uuid("nested-output"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [2],
            }
        ],
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [
            _node(
                1,
                "Pass",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                pos=(200, 0),
            )
        ],
        "links": [_link(1, -10, 0, 1, 0), _link(2, 1, 0, -20, 0)],
    }
    nodes = [
        _node(
            10,
            "nested-def",
            inputs=(("image", "IMAGE"),),
            outputs=(("image", "IMAGE"),),
        )
    ]
    if reused:
        nodes.append(
            _node(
                "other",
                "nested-def",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            )
        )
    return {
        "nodes": nodes,
        "links": [],
        "definitions": {"subgraphs": [definition]},
    }


def _nested_branch(workflow: dict[str, Any], *, container_id: int | str = 10):
    return next(
        branch
        for scope in _branch_catalog(workflow).scopes
        if scope.scope.kind == "subgraph_instance"
        and scope.scope.scope_path[0].container_node_id == container_id
        for branch in scope.branches
        if branch.kind == "segment"
    )


def _nested_terminal_split_workflow() -> dict[str, Any]:
    definition = {
        "id": "nested-split",
        "name": "Nested split",
        "inputs": [
            {
                "id": _uuid("nested-split-input"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [1, 3],
            }
        ],
        "outputs": [
            {
                "id": _uuid("nested-split-output"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [],
            }
        ],
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [
            _node(
                2,
                "SafeFilter",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                widgets=[0.75],
                pos=(200, 0),
            ),
            _node(
                4,
                "SafeSink",
                inputs=(("image", "IMAGE"),),
                widgets=["result"],
                pos=(400, 0),
            ),
            _node(
                3,
                "SafeSink",
                inputs=(("image", "IMAGE"),),
                widgets=["sibling"],
                pos=(400, 200),
            ),
        ],
        "links": [
            _link(1, -10, 0, 2, 0),
            _link(2, 2, 0, 4, 0),
            _link(3, -10, 0, 3, 0),
        ],
    }
    return {
        "nodes": [
            _node(
                10,
                "nested-split",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            )
        ],
        "links": [],
        "definitions": {"subgraphs": [definition]},
    }


def _nested_side_input_diamond_workflow() -> dict[str, Any]:
    definition = {
        "id": "nested-diamond",
        "name": "Nested diamond",
        "inputs": [
            {
                "id": _uuid("nested-diamond-image"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [1, 2],
            },
        ],
        "outputs": [
            {
                "id": _uuid("nested-diamond-output"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [6],
            }
        ],
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [
            _node(
                2,
                "Arm",
                inputs=(("image", "IMAGE"), ("conditioning", "CONDITIONING")),
                outputs=(("image", "IMAGE"),),
                widgets=[1.0],
                pos=(200, 0),
            ),
            _node(
                3,
                "OtherArm",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                pos=(200, 200),
            ),
            _node(
                4,
                "Join",
                inputs=(("a", "IMAGE"), ("b", "IMAGE")),
                outputs=(("image", "IMAGE"),),
                pos=(500, 100),
            ),
            _node(
                9,
                "ConditioningSource",
                outputs=(("conditioning", "CONDITIONING"),),
                pos=(0, -200),
            ),
        ],
        "links": [
            _link(1, -10, 0, 2, 0),
            _link(2, -10, 0, 3, 0),
            _link(3, 2, 0, 4, 0),
            _link(4, 3, 0, 4, 1),
            _link(5, 9, 0, 2, 1, "CONDITIONING"),
            _link(6, 4, 0, -20, 0),
        ],
    }
    return {
        "nodes": [
            _node(
                10,
                "nested-diamond",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            )
        ],
        "links": [],
        "definitions": {"subgraphs": [definition]},
    }


def _terminal_split(
    terminal_type: str = "SafeSink",
    *,
    terminal_widgets: list[Any] | None = None,
) -> dict[str, Any]:
    widgets = ["result"] if terminal_widgets is None else terminal_widgets
    return {
        "nodes": [
            _node(1, "ImageSource", outputs=(("image", "IMAGE"),), pos=(0, 100)),
            _node(2, "SafeFilter", inputs=(("image", "IMAGE"),), outputs=(("image", "IMAGE"),), widgets=[0.75], pos=(200, 0)),
            _node(3, "SafeSink", inputs=(("image", "IMAGE"),), widgets=["sibling"], pos=(400, 200)),
            _node(4, terminal_type, inputs=(("image", "IMAGE"),), widgets=widgets, pos=(400, 0)),
        ],
        "links": [
            _link(1, 1, 0, 2, 0),
            _link(2, 2, 0, 4, 0),
            _link(3, 1, 0, 3, 0),
        ],
        "definitions": {"subgraphs": []},
    }


def _terminal_split_with_current_shaped_reroute() -> dict[str, Any]:
    workflow = _terminal_split()
    sibling = next(node for node in workflow["nodes"] if node["id"] == 3)
    sibling["inputs"][0]["link"] = 49
    workflow["nodes"].append(
        {
            "id": 58,
            "type": "Reroute",
            "inputs": [{"name": "", "type": "*", "link": 71}],
            "outputs": [{"name": "", "type": "IMAGE", "links": [49]}],
            "widgets_values": [],
            "properties": {"horizontal": False, "showOutputText": False},
            "pos": [300, 200],
            "size": [75, 26],
        }
    )
    workflow["links"] = [
        link for link in workflow["links"] if link[0] != 3
    ] + [
        _link(49, 58, 0, 3, 0),
        _link(71, 1, 0, 58, 0),
    ]
    return workflow


def _dynamic_live_inputs(link_id: int | None) -> list[dict[str, Any]]:
    return [
        {"name": f"runtime_prefix_{index}", "type": "IMAGE", "link": None}
        for index in range(5)
    ] + [
        {
            "name": "model.images.image_1",
            "type": "IMAGE",
            "link": link_id,
        }
    ]


def _dynamic_terminal_split_workflow() -> dict[str, Any]:
    dynamic = _node(
        2,
        "DynamicNanoTarget",
        outputs=(("IMAGE", "IMAGE"),),
        widgets=["prompt", "nano", "16:9", "1K", "MINIMAL"],
        pos=(200, 0),
    )
    dynamic["inputs"] = _dynamic_live_inputs(1)
    return {
        "nodes": [
            _node(1, "ImageSource", outputs=(("image", "IMAGE"),)),
            dynamic,
            _node(3, "SafeSink", inputs=(("image", "IMAGE"),), widgets=["sibling"]),
        ],
        "links": [_link(1, 1, 0, 2, 5), _link(2, 1, 0, 3, 0)],
        "definitions": {"subgraphs": []},
    }


def _nested_dynamic_terminal_split_workflow() -> dict[str, Any]:
    dynamic = _node(
        2,
        "DynamicNanoTarget",
        outputs=(("IMAGE", "IMAGE"),),
        widgets=["prompt", "nano", "16:9", "1K", "MINIMAL"],
        pos=(200, 0),
    )
    dynamic["inputs"] = _dynamic_live_inputs(1)
    definition = {
        "id": "nested-dynamic",
        "name": "Nested dynamic",
        "inputs": [
            {
                "id": _uuid("nested-dynamic-image"),
                "name": "image",
                "type": "IMAGE",
                "linkIds": [1, 2],
            }
        ],
        "outputs": [],
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [
            dynamic,
            _node(3, "SafeSink", inputs=(("image", "IMAGE"),), widgets=["sibling"]),
        ],
        "links": [_link(1, -10, 0, 2, 5), _link(2, -10, 0, 3, 0)],
    }
    return {
        "nodes": [_node(10, "nested-dynamic", inputs=(("image", "IMAGE"),))],
        "links": [],
        "definitions": {"subgraphs": [definition]},
    }


def _branch_catalog(workflow: dict[str, Any]):
    return discover_workflow_branches(
        workflow,
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
    )


def _arm_to(workflow: dict[str, Any], target_node_id: int | str) -> WorkflowBranchRecord:
    catalog = _branch_catalog(workflow)
    return next(
        branch
        for scope in catalog.scopes
        for branch in scope.branches
        if branch.kind == "split_arm"
        and next(
            edge
            for edge in branch.entry_edges
            if edge.edge_id == branch.primary_entry_edge_id
        ).target.node_id
        == target_node_id
    )


def _segment(workflow: dict[str, Any]) -> WorkflowBranchRecord:
    return next(
        branch
        for scope in _branch_catalog(workflow).scopes
        for branch in scope.branches
        if branch.kind == "segment"
    )


def _pins(workflow: dict[str, Any], branch: WorkflowBranchRecord) -> dict[str, Any]:
    return {
        "application_id": "branch-operation-test-v1",
        "branch_id": branch.branch_id,
        "expected_workflow_identity": WORKFLOW_IDENTITY,
        "expected_graph_hash": GRAPH_HASH,
        "expected_branch_catalog_hash": _branch_catalog(workflow).branch_catalog_hash,
    }


def _compile(request: Any, workflow: dict[str, Any], *, catalog: dict[str, Any] | None = None):
    active_catalog = catalog or _catalog()
    return compile_workflow_branch_operation(
        request,
        workflow,
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
        catalog=active_catalog,
        catalog_hash=catalog_contract_hash(active_catalog),
        source="branch-operation-fixture",
    )


def _resolve_successors(
    compiled: dict[str, Any],
    workflow: dict[str, Any],
    aliases: dict[str, int | str],
    *,
    graph_hash: str = "b" * 64,
) -> dict[str, Any]:
    request = ResolveBranchSuccessorsRequest(
        apply_request=compiled["apply_request"],
        pending_successor_locator=compiled["pending_successor_locator"],
        expected_workflow_identity=WORKFLOW_IDENTITY,
        expected_graph_hash=graph_hash,
        aliases=aliases,
    )
    return resolve_workflow_branch_successors(
        request,
        workflow,
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=graph_hash,
        catalog=_catalog(),
    )


def test_replace_maps_every_side_input_and_exit_and_preserves_sibling() -> None:
    workflow = _side_input_diamond()
    branch = _arm_to(workflow, 2)
    entry_by_input = {edge.target.input: edge for edge in branch.entry_edges}
    [exit_edge] = branch.exit_edges
    request = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "replacement",
                "capability": "replacement image and conditioning transform",
                "requested_node_type": "Replacement",
                "values": {"amount": 0.8},
            }
        ],
        "entry_mappings": [
            {
                "entry_edge_id": entry_by_input["image"].edge_id,
                "target_alias": "replacement",
                "target_input": "image",
                "target_mode": "slot",
            },
            {
                "entry_edge_id": entry_by_input["conditioning"].edge_id,
                "target_alias": "replacement",
                "target_input": "conditioning",
                "target_mode": "slot",
            },
        ],
        "exit_mappings": [
            {
                "exit_edge_id": exit_edge.edge_id,
                "source_alias": "replacement",
                "source_output": "image",
                "source_output_index": 0,
            }
        ],
    }

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    assert result["queued"] is False
    assert result["successor_branch_id"] is None
    assert result["pending_successor_locator"]["expected_state"] == "created_region"
    plan = result["plan"]
    assert [item["ref"]["node_id"] for item in plan["remove_nodes"]] == [2]
    assert [item["alias"] for item in plan["create_nodes"]] == ["replacement"]
    assert len(plan["remove_edges"]) == 3
    assert len(plan["add_edges"]) == 3
    final_existing_ids = {
        item["node_id"] for item in result["expected_final"]["nodes"] if "node_id" in item
    }
    assert {1, 3, 4, 9}.issubset(final_existing_ids)
    ApplyGraphPatchRequest.model_validate(result["apply_request"])


@pytest.mark.parametrize("missing", ["entry_mappings", "exit_mappings"])
def test_replace_requires_every_exact_boundary_mapping(missing: str) -> None:
    workflow = _side_input_diamond()
    branch = _arm_to(workflow, 2)
    request = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "replacement",
                "capability": "replacement",
                "requested_node_type": "Replacement",
            }
        ],
        "entry_mappings": [
            {
                "entry_edge_id": edge.edge_id,
                "target_alias": "replacement",
                "target_input": edge.target.input,
            }
            for edge in branch.entry_edges
        ],
        "exit_mappings": [
            {
                "exit_edge_id": edge.edge_id,
                "source_alias": "replacement",
                "source_output": "image",
            }
            for edge in branch.exit_edges
        ],
    }
    request[missing] = []

    result = _compile(request, workflow)

    assert not result["valid"]
    assert result["apply_request"] is None
    assert {item["code"] for item in result["issues"]} & {
        "incomplete_entry_mapping",
        "incomplete_exit_mapping",
    }


def test_replace_uses_semantic_dynamic_input_resolution() -> None:
    workflow = _linear_workflow()
    branch = _segment(workflow)
    request = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "dynamic",
                "capability": "dynamic replacement",
                "requested_node_type": "DynamicReplacement",
                "values": {"model": "active", "amount": 0.7},
            }
        ],
        "entry_mappings": [
            {
                "entry_edge_id": branch.entry_edges[0].edge_id,
                "target_alias": "dynamic",
                "target_input": "image",
            }
        ],
        "exit_mappings": [
            {
                "exit_edge_id": branch.exit_edges[0].edge_id,
                "source_alias": "dynamic",
                "source_output": "image",
            }
        ],
    }

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    dynamic_target = next(
        edge["target"]
        for edge in result["plan"]["add_edges"]
        if edge["target"]["ref"] == {"alias": "dynamic"}
    )
    assert dynamic_target["input"] == "model.image"
    assert dynamic_target["socket_index"] == 0


def test_isolated_node_replace_and_delete_are_structured_graph_patches() -> None:
    workflow = {
        "nodes": [_node(77, "ImageSource", outputs=(("image", "IMAGE"),))],
        "links": [],
        "definitions": {"subgraphs": []},
    }
    branch = next(
        item
        for scope in _branch_catalog(workflow).scopes
        for item in scope.branches
        if item.kind == "isolated"
    )
    replace = _compile(
        {
            **_pins(workflow, branch),
            "operation": "replace",
            "replacement_nodes": [
                {
                    "alias": "replacement_source",
                    "capability": "local image source",
                    "requested_node_type": "ImageSource",
                }
            ],
        },
        workflow,
    )
    remove = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "delete"},
        workflow,
    )

    assert replace["valid"], replace["issues"]
    assert [item["ref"]["node_id"] for item in replace["plan"]["remove_nodes"]] == [77]
    assert [item["alias"] for item in replace["plan"]["create_nodes"]] == [
        "replacement_source"
    ]
    assert replace["plan"]["remove_edges"] == []
    assert replace["plan"]["add_edges"] == []
    assert remove["valid"], remove["issues"]
    assert [item["ref"]["node_id"] for item in remove["plan"]["remove_nodes"]] == [77]
    assert remove["plan"]["remove_edges"] == []
    assert remove["plan"]["add_edges"] == []


def test_terminal_delete_removes_only_selected_arm_and_never_queues() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    request = {**_pins(workflow, branch), "operation": "remove", "mode": "delete"}

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    assert result["queued"] is False
    assert [item["ref"]["node_id"] for item in result["plan"]["remove_nodes"]] == [3]
    final_ids = {
        item["node_id"] for item in result["expected_final"]["nodes"] if "node_id" in item
    }
    assert {1, 2, 4}.issubset(final_ids)
    assert result["pending_successor_locator"]["expected_state"] == "branch_removed"


def test_unique_linear_bypass_is_automatic_and_schema_checked() -> None:
    workflow = _linear_workflow()
    branch = _segment(workflow)
    request = {**_pins(workflow, branch), "operation": "remove", "mode": "bypass"}

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    assert [item["ref"]["node_id"] for item in result["plan"]["remove_nodes"]] == [2]
    [added] = result["plan"]["add_edges"]
    assert added["source"]["ref"] == {"node_id": 1}
    assert added["target"]["ref"] == {"node_id": 3}


def test_explicit_bypass_uses_only_exact_boundary_edge_ids() -> None:
    workflow = _linear_workflow()
    branch = _segment(workflow)
    request = {
        **_pins(workflow, branch),
        "operation": "remove",
        "mode": "bypass",
        "bypass_mappings": [
            {
                "entry_edge_id": branch.entry_edges[0].edge_id,
                "exit_edge_id": branch.exit_edges[0].edge_id,
            }
        ],
    }

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    assert len(result["plan"]["add_edges"]) == 1


def test_bypass_type_incompatibility_fails_in_semantic_compiler() -> None:
    workflow = {
        "nodes": [
            _node(1, "ImageSource", outputs=(("image", "IMAGE"),)),
            _node(2, "ImageToMask", inputs=(("image", "IMAGE"),), outputs=(("mask", "MASK"),)),
            _node(3, "MaskSink", inputs=(("mask", "MASK"),)),
        ],
        "links": [_link(1, 1, 0, 2, 0), _link(2, 2, 0, 3, 0, "MASK")],
        "definitions": {"subgraphs": []},
    }
    branch = _segment(workflow)

    result = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "bypass"},
        workflow,
    )

    assert not result["valid"]
    assert result["apply_request"] is None
    assert {item["code"] for item in result["issues"]} & {
        "edge_type_incompatible",
        "converter_inference_disabled",
    }


def test_delete_refuses_a_nonterminal_segment() -> None:
    workflow = _linear_workflow()
    branch = _segment(workflow)

    result = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "delete"},
        workflow,
    )

    assert not result["valid"]
    assert result["apply_request"] is None
    assert "terminal_delete_required" in {item["code"] for item in result["issues"]}


def test_ambiguous_bypass_returns_needs_choice_and_no_apply_envelope() -> None:
    workflow = _side_input_diamond()
    branch = _arm_to(workflow, 2)
    request = {**_pins(workflow, branch), "operation": "remove", "mode": "bypass"}

    result = _compile(request, workflow)

    assert not result["valid"]
    assert result["needs_choice"]
    assert result["apply_request"] is None
    [issue] = result["issues"]
    assert issue["code"] == "bypass_mapping_required"
    assert sorted(issue["details"]["entry_edge_ids"]) == sorted(
        edge.edge_id for edge in branch.entry_edges
    )


@pytest.mark.parametrize(
    ("pin", "value", "code"),
    [
        ("expected_workflow_identity", "different", "workflow_identity_changed"),
        ("expected_graph_hash", "b" * 64, "graph_changed"),
        ("expected_branch_catalog_hash", "b" * 64, "branch_catalog_changed"),
        ("branch_id", "b" * 64, "branch_not_found"),
    ],
)
def test_every_branch_precondition_fails_closed(pin: str, value: str, code: str) -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    request = {**_pins(workflow, branch), "operation": "remove", "mode": "delete"}
    request[pin] = value

    result = _compile(request, workflow)

    assert not result["valid"]
    assert result["apply_request"] is None
    assert code in {item["code"] for item in result["issues"]}


def test_nested_scope_direct_boundary_bypass_fails_closed() -> None:
    workflow = _nested_linear_workflow()
    nested_branch = _nested_branch(workflow)
    request = {
        **_pins(workflow, nested_branch),
        "operation": "remove",
        "mode": "bypass",
    }

    result = _compile(request, workflow)

    assert not result["valid"]
    assert result["queued"] is False
    assert result["apply_request"] is None
    assert "direct_scope_boundary_edge_unsupported" in {
        item["code"] for item in result["issues"]
    }


@pytest.mark.parametrize(
    "reserved_type",
    ["__fl_mcp_scope_input__", "__fl_mcp_scope_output__"],
)
def test_nested_branch_compile_rejects_live_boundary_type_collision(
    reserved_type: str,
) -> None:
    workflow = _nested_linear_workflow()
    nested_branch = _nested_branch(workflow)
    catalog = _catalog()
    catalog[reserved_type] = _schema(
        outputs=["IMAGE"],
        output_names=["real_custom_node_output"],
        python_module="custom_nodes.collision",
    )

    result = _compile(
        {
            **_pins(workflow, nested_branch),
            "operation": "remove",
            "mode": "bypass",
        },
        workflow,
        catalog=catalog,
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert {item["code"] for item in result["issues"]} == {
        "scope_boundary_node_type_collision"
    }


def test_nested_scope_replace_maps_both_virtual_boundaries_exactly() -> None:
    workflow = _nested_linear_workflow()
    branch = _nested_branch(workflow)
    [entry] = branch.entry_edges
    [exit_] = branch.exit_edges
    request = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "replacement",
                "capability": "exact loaded pass replacement",
                "requested_node_type": "Pass",
            }
        ],
        "entry_mappings": [
            {
                "entry_edge_id": entry.edge_id,
                "target_alias": "replacement",
                "target_input": "image",
                "target_mode": "slot",
            }
        ],
        "exit_mappings": [
            {
                "exit_edge_id": exit_.edge_id,
                "source_alias": "replacement",
                "source_output": "image",
                "source_output_index": 0,
            }
        ],
    }

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    assert result["plan"]["operation"] == "scoped_patch"
    assert result["plan"]["expected_delta"]["final_node_count"] == 1
    assert {item["alias"] for item in result["plan"]["create_nodes"]} == {
        "replacement"
    }
    assert {item["ref"]["node_id"] for item in result["plan"]["remove_nodes"]} == {1}
    refs = [
        (edge["source"]["ref"], edge["target"]["ref"])
        for edge in result["plan"]["add_edges"]
    ]
    assert any("scope_input" in source for source, _ in refs)
    assert any("scope_output" in target for _, target in refs)
    ApplyScopedGraphPatchRequest.model_validate(result["apply_request"])


def test_nested_replace_cannot_create_compiler_only_boundary_type() -> None:
    workflow = _nested_linear_workflow()
    branch = _nested_branch(workflow)
    [entry] = branch.entry_edges
    [exit_] = branch.exit_edges
    request = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "replacement",
                "capability": "exact loaded pass replacement",
                "requested_node_type": "Pass",
            },
            {
                "alias": "private_shadow",
                "capability": "compiler boundary input",
                "requested_node_type": "__fl_mcp_scope_input__",
            },
        ],
        "entry_mappings": [
            {
                "entry_edge_id": entry.edge_id,
                "target_alias": "replacement",
                "target_input": "image",
                "target_mode": "slot",
            }
        ],
        "exit_mappings": [
            {
                "exit_edge_id": exit_.edge_id,
                "source_alias": "replacement",
                "source_output": "image",
                "source_output_index": 0,
            }
        ],
    }

    result = _compile(request, workflow)

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert "scope_boundary_node_type_reserved" in {
        item["code"] for item in result["issues"]
    }


def test_reused_nested_definition_requires_complete_shared_authority() -> None:
    workflow = _nested_linear_workflow(reused=True)
    branch = _nested_branch(workflow)
    base = {**_pins(workflow, branch), "operation": "remove", "mode": "bypass"}

    refused = _compile(base, workflow)
    assert not refused["valid"]
    assert "instance_detach_not_supported" in {
        item["code"] for item in refused["issues"]
    }

    allowed = _compile(
        {
            **base,
            "scope_edit_mode": "shared_definition",
            "affected_scope_paths": [
                [{"container_node_id": "other", "subgraph_id": "nested-def"}],
                [{"container_node_id": 10, "subgraph_id": "nested-def"}],
            ],
        },
        workflow,
    )
    assert not allowed["valid"]
    assert "direct_scope_boundary_edge_unsupported" in {
        item["code"] for item in allowed["issues"]
    }
    assert "instance_detach_not_supported" not in {
        item["code"] for item in allowed["issues"]
    }
    assert allowed["plan"] is None
    assert allowed["apply_request"] is None


def test_nested_terminal_arm_clone_shares_exact_scope_input_boundary() -> None:
    workflow = _nested_terminal_split_workflow()
    branch = next(
        branch
        for scope in _branch_catalog(workflow).scopes
        if scope.scope.kind == "subgraph_instance"
        for branch in scope.branches
        if branch.kind == "split_arm"
        and next(
            edge
            for edge in branch.entry_edges
            if edge.edge_id == branch.primary_entry_edge_id
        ).target.node_id
        == 2
    )

    result = _compile(
        {
            **_pins(workflow, branch),
            "operation": "clone",
            "layout_offset": {"x": 120, "y": 40},
        },
        workflow,
    )

    assert result["valid"], result["issues"]
    assert result["plan"]["operation"] == "scoped_patch"
    assert {item["node_type"] for item in result["plan"]["create_nodes"]} == {
        "SafeFilter",
        "SafeSink",
    }
    assert len(result["plan"]["add_edges"]) == 2
    assert any(
        "scope_input" in edge["source"]["ref"]
        for edge in result["plan"]["add_edges"]
    )
    assert all(
        "scope_output" not in edge["target"]["ref"]
        for edge in result["plan"]["add_edges"]
    )
    assert {item["ref"]["node_id"] for item in result["plan"]["assertions"]["nodes"]} == {2, 4}
    assert len(result["plan"]["assertions"]["edges"]) == 2
    assert any(
        "scope_input" in edge["source"]["ref"]
        for edge in result["plan"]["assertions"]["edges"]
    )
    ApplyScopedGraphPatchRequest.model_validate(result["apply_request"])


def test_safe_terminal_clone_copies_named_values_offsets_layout_and_shares_source() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    request = {
        **_pins(workflow, branch),
        "operation": "clone",
        "layout_offset": {"x": 120, "y": 40},
    }

    result = _compile(request, workflow)

    assert result["valid"], result["issues"]
    assert result["queued"] is False
    assert result["plan"]["remove_nodes"] == []
    assert result["plan"]["remove_edges"] == []
    assert len(result["plan"]["create_nodes"]) == 2
    assert len(result["plan"]["add_edges"]) == 2
    assert {item["ref"]["node_id"] for item in result["plan"]["assertions"]["nodes"]} == {1, 2, 4}
    assert len(result["plan"]["assertions"]["edges"]) == 2
    assert all(item["layout_hint"] is not None for item in result["plan"]["create_nodes"])
    positions = {(item["node_type"], item["layout_hint"]["x"], item["layout_hint"]["y"]) for item in result["plan"]["create_nodes"]}
    assert ("SafeFilter", 320, 40) in positions
    assert ("SafeSink", 520, 40) in positions
    assert any(edge["source"]["ref"] == {"node_id": 1} for edge in result["plan"]["add_edges"])
    assert result["pending_successor_locator"]["created_aliases"]
    assert result["successor_branch_id"] is None
    assert ApplyGraphPatchRequest.model_validate(result["apply_request"]).model_dump(
        mode="json"
    ) == result["apply_request"]


def test_root_branch_operation_compiles_with_current_shaped_native_reroute() -> None:
    workflow = _terminal_split_with_current_shaped_reroute()
    branch = _arm_to(workflow, 2)

    result = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)

    assert result["valid"], result["issues"]
    assert result["queued"] is False
    assert "workflow_graph_invalid" not in {item["code"] for item in result["issues"]}


def test_safe_terminal_segment_clone_includes_its_private_sink_boundary() -> None:
    workflow = _linear_workflow()
    branch = _segment(workflow)

    result = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)

    assert result["valid"], result["issues"]
    assert {item["node_type"] for item in result["plan"]["create_nodes"]} == {
        "Pass",
        "SafeSink",
    }
    assert len(result["plan"]["add_edges"]) == 2
    assert any(edge["source"]["ref"] == {"node_id": 1} for edge in result["plan"]["add_edges"])


def test_clone_accepts_exact_graph_patch_provenance_and_regenerates_it() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    node = next(item for item in workflow["nodes"] if item["id"] == 2)
    node["properties"] = {
        "fl_mcp_workflow_graph_patch": {
            "schema": "fl-mcp.workflow-graph-patch.v2",
            "application_id": "prior-graph-patch-application",
            "patch_hash": "1" * 64,
            "alias": "prior_filter",
            "schema_hash": "2" * 64,
        }
    }

    result = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)

    assert result["valid"], result["issues"]
    serialized_plan = json.dumps(result["plan"], sort_keys=True)
    assert "prior-graph-patch-application" not in serialized_plan
    assert "prior_filter" not in serialized_plan
    assert "fl_mcp_workflow_graph_patch" not in serialized_plan


@pytest.mark.parametrize(
    "provenance",
    [
        {
            "schema": "fl-mcp.workflow-graph-patch.v2",
            "application_id": "prior-graph-patch-application",
            "patch_hash": "not-a-hash",
            "alias": "prior_filter",
            "schema_hash": "2" * 64,
        },
        {
            "schema": "fl-mcp.workflow-graph-patch.v2",
            "application_id": "prior-graph-patch-application",
            "patch_hash": "1" * 64,
            "alias": "prior_filter",
            "schema_hash": "2" * 64,
            "api_token": "PROVENANCE-SECRET-SENTINEL",
        },
        {
            "schema": ["PROVENANCE-LIST-SENTINEL"],
            "application_id": "prior-graph-patch-application",
            "patch_hash": "1" * 64,
            "alias": "prior_filter",
            "schema_hash": "2" * 64,
        },
        {
            "schema": {"name": "PROVENANCE-OBJECT-SENTINEL"},
            "application_id": "prior-graph-patch-application",
            "patch_hash": "1" * 64,
            "alias": "prior_filter",
            "schema_hash": "2" * 64,
        },
    ],
)
def test_clone_rejects_malformed_or_secret_bearing_graph_patch_provenance(
    provenance: dict[str, Any],
) -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    node = next(item for item in workflow["nodes"] if item["id"] == 2)
    node["properties"] = {"fl_mcp_workflow_graph_patch": provenance}

    result = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert "clone_properties_unsupported" in {
        item["code"] for item in result["issues"]
    }
    assert "PROVENANCE-SECRET-SENTINEL" not in json.dumps(result, sort_keys=True)
    assert "PROVENANCE-LIST-SENTINEL" not in json.dumps(result, sort_keys=True)
    assert "PROVENANCE-OBJECT-SENTINEL" not in json.dumps(result, sort_keys=True)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("mode", 4, "clone_execution_state_unsupported"),
        ("flags", {"collapsed": True}, "clone_execution_state_unsupported"),
        (
            "properties",
            {"runtime_policy": "muted"},
            "clone_properties_unsupported",
        ),
        (
            "properties",
            {"Node name for S&R": "custom-save-alias"},
            "clone_properties_unsupported",
        ),
    ],
)
def test_clone_rejects_unreproducible_serialized_execution_state(
    field: str,
    value: Any,
    expected_code: str,
) -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    node = next(item for item in workflow["nodes"] if item["id"] == 2)
    node[field] = value

    result = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert expected_code in {item["code"] for item in result["issues"]}


def test_clone_detaches_reconvergent_exit_and_asserts_complete_original_region() -> None:
    workflow = _side_input_diamond()
    branch = _arm_to(workflow, 2)

    result = _compile(
        {
            **_pins(workflow, branch),
            "operation": "clone",
            "acknowledged_risks": ["heavy"],
        },
        workflow,
    )

    assert result["valid"], result["issues"]
    [created] = result["plan"]["create_nodes"]
    assert created["node_type"] == "Arm"
    assert result["plan"]["remove_edges"] == []
    assert result["plan"]["remove_nodes"] == []
    assert len(result["plan"]["add_edges"]) == 2
    assert all(
        edge["target"]["ref"] == {"alias": created["alias"]}
        for edge in result["plan"]["add_edges"]
    )
    assert all(
        edge["target"]["ref"] != {"node_id": 4}
        for edge in result["plan"]["add_edges"]
    )
    assert {item["ref"]["node_id"] for item in result["plan"]["assertions"]["nodes"]} == {1, 2, 4, 9}
    assert len(result["plan"]["assertions"]["edges"]) == 3
    warning = next(
        item for item in result["issues"] if item["code"] == "clone_outputs_detached"
    )
    assert warning["severity"] == "warning"
    assert warning["details"] == {
        "detached_exit_edge_ids": [branch.exit_edges[0].edge_id],
        "external_exit_connections_created": 0,
    }


def test_nested_clone_detaches_reconvergent_exit_and_asserts_scope_boundary() -> None:
    workflow = _nested_side_input_diamond_workflow()
    branch = next(
        item
        for scope in _branch_catalog(workflow).scopes
        if scope.scope.kind == "subgraph_instance"
        for item in scope.branches
        if item.kind == "split_arm" and item.owned_node_ids == [2]
    )

    result = _compile(
        {
            **_pins(workflow, branch),
            "operation": "clone",
            "acknowledged_risks": ["heavy"],
        },
        workflow,
    )

    assert result["valid"], result["issues"]
    assert result["plan"]["operation"] == "scoped_patch"
    assert {item["ref"]["node_id"] for item in result["plan"]["assertions"]["nodes"]} == {2, 4, 9}
    assert len(result["plan"]["assertions"]["edges"]) == 3
    assert sum(
        "scope_input" in edge["source"]["ref"]
        for edge in result["plan"]["assertions"]["edges"]
    ) == 1
    assert all(
        "scope_output" not in edge["target"]["ref"]
        for edge in result["plan"]["add_edges"]
    )
    assert len(result["plan"]["add_edges"]) == 2
    assert next(
        item for item in result["issues"] if item["code"] == "clone_outputs_detached"
    )["details"]["detached_exit_edge_ids"] == [branch.exit_edges[0].edge_id]


@pytest.mark.parametrize(
    ("node_type", "widgets", "expected_code", "forbidden_value"),
    [
        (
            "SecretSink",
            ["TOP-SECRET-SENTINEL"],
            "clone_secret_value_unsupported",
            "TOP-SECRET-SENTINEL",
        ),
        (
            "CamelSecretSink",
            ["CAMEL-SECRET-SENTINEL"],
            "clone_secret_value_unsupported",
            "CAMEL-SECRET-SENTINEL",
        ),
        (
            "AccessKeySink",
            ["ACCESS-KEY-SENTINEL"],
            "clone_secret_value_unsupported",
            "ACCESS-KEY-SENTINEL",
        ),
        (
            "NestedCredentialSink",
            [{"credentials": {"accessToken": "NESTED-SECRET-SENTINEL"}}],
            "clone_secret_value_unsupported",
            "NESTED-SECRET-SENTINEL",
        ),
        (
            "HiddenAuthSink",
            [],
            "clone_secret_value_unsupported",
            None,
        ),
        (
            "UploadSink",
            ["asset.png"],
            "clone_attachment_mapping_required",
            "asset.png",
        ),
        (
            "VideoUploadSink",
            ["clip.mp4"],
            "clone_attachment_mapping_required",
            "clip.mp4",
        ),
        (
            "AudioUploadSink",
            ["track.wav"],
            "clone_attachment_mapping_required",
            "track.wav",
        ),
        (
            "FileUploadSink",
            ["payload.bin"],
            "clone_attachment_mapping_required",
            "payload.bin",
        ),
        (
            "DirectoryUploadSink",
            ["assets"],
            "clone_attachment_mapping_required",
            "assets",
        ),
    ],
)
def test_clone_never_copies_secrets_or_attachments(
    node_type: str,
    widgets: list[Any],
    expected_code: str,
    forbidden_value: str | None,
) -> None:
    workflow = _terminal_split(node_type, terminal_widgets=widgets)
    branch = _arm_to(workflow, 2)

    result = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)

    serialized = str(result)
    assert not result["valid"]
    assert result["apply_request"] is None
    assert expected_code in {item["code"] for item in result["issues"]}
    if forbidden_value is not None:
        assert forbidden_value not in serialized


def test_clone_never_emits_a_nonempty_secret_even_when_it_is_schema_default() -> None:
    catalog = _catalog()
    catalog["SecretSink"]["input"]["required"]["api_key"][1]["default"] = (
        "DEFAULT-SECRET-SENTINEL"
    )
    workflow = _terminal_split(
        "SecretSink",
        terminal_widgets=["DEFAULT-SECRET-SENTINEL"],
    )
    branch = _arm_to(workflow, 2)

    result = _compile(
        {**_pins(workflow, branch), "operation": "clone"},
        workflow,
        catalog=catalog,
    )

    assert not result["valid"]
    assert result["apply_request"] is None
    assert "clone_secret_value_unsupported" in {
        item["code"] for item in result["issues"]
    }
    assert "DEFAULT-SECRET-SENTINEL" not in str(result)


def test_clone_refuses_credentials_described_only_by_schema_metadata() -> None:
    secret = "TOOLTIP-SECRET-SENTINEL"
    catalog = _catalog()
    catalog["SafeSink"]["input"]["required"]["label"][1].update(
        {"tooltip": "Enter API key for the hosted service"}
    )
    workflow = _terminal_split("SafeSink", terminal_widgets=[secret])
    branch = _arm_to(workflow, 2)

    result = _compile(
        {**_pins(workflow, branch), "operation": "clone"},
        workflow,
        catalog=catalog,
    )

    serialized = str(result)
    assert not result["valid"]
    assert result["apply_request"] is None
    assert "clone_secret_value_unsupported" in {
        item["code"] for item in result["issues"]
    }
    assert secret not in serialized


def test_partner_api_clone_requires_explicit_acknowledgements() -> None:
    workflow = _terminal_split("PartnerSink", terminal_widgets=["prompt"])
    branch = _arm_to(workflow, 2)
    base = {**_pins(workflow, branch), "operation": "clone"}

    refused = _compile(base, workflow)
    allowed = _compile(
        {**base, "acknowledged_risks": ["partner", "api"]},
        workflow,
    )

    assert not refused["valid"]
    assert "clone_risk_acknowledgement_required" in {
        item["code"] for item in refused["issues"]
    }
    assert allowed["valid"], allowed["issues"]
    assert allowed["warning_count"] >= 1


def test_reversed_workflow_and_request_order_produce_same_patch_hash() -> None:
    workflow = _side_input_diamond()
    branch = _arm_to(workflow, 2)
    entry_by_input = {edge.target.input: edge for edge in branch.entry_edges}
    [exit_edge] = branch.exit_edges
    base = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "first",
                "capability": "pass image",
                "requested_node_type": "Replacement",
                "values": {"amount": 0.6},
            },
            {
                "alias": "second",
                "capability": "pass image",
                "requested_node_type": "Pass",
            },
        ],
        "replacement_edges": [
            {
                "source_alias": "first",
                "source_output": "image",
                "target_alias": "second",
                "target_input": "image",
            }
        ],
        "entry_mappings": [
            {
                "entry_edge_id": entry_by_input["image"].edge_id,
                "target_alias": "first",
                "target_input": "image",
            },
            {
                "entry_edge_id": entry_by_input["conditioning"].edge_id,
                "target_alias": "first",
                "target_input": "conditioning",
            },
        ],
        "exit_mappings": [
            {
                "exit_edge_id": exit_edge.edge_id,
                "source_alias": "second",
                "source_output": "image",
            }
        ],
    }
    reversed_workflow = deepcopy(workflow)
    reversed_workflow["nodes"].reverse()
    reversed_workflow["links"].reverse()
    reversed_request = deepcopy(base)
    reversed_request["replacement_nodes"].reverse()
    reversed_request["entry_mappings"].reverse()

    first = _compile(base, workflow)
    second = _compile(reversed_request, reversed_workflow)

    assert first["valid"], first["issues"]
    assert second["valid"], second["issues"]
    assert first["patch_hash"] == second["patch_hash"]
    assert first["apply_request"] == second["apply_request"]


def test_strict_request_and_result_models_reject_extra_fields() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    with pytest.raises(ValidationError):
        RemoveBranchRequest.model_validate(
            {
                **_pins(workflow, branch),
                "operation": "remove",
                "mode": "delete",
                "unexpected": True,
            }
        )
    valid = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "delete"},
        workflow,
    )
    BranchOperationResult.model_validate(valid)
    with pytest.raises(ValidationError):
        BranchOperationResult.model_validate({**valid, "unexpected": True})


def test_shared_definition_scope_paths_are_bounded_before_discovery() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    payload = {
        **_pins(workflow, branch),
        "operation": "remove",
        "mode": "delete",
        "scope_edit_mode": "shared_definition",
        "affected_scope_paths": [
            [
                {
                    "container_node_id": index,
                    "subgraph_id": _uuid(f"over-depth-{index}"),
                }
                for index in range(33)
            ]
        ],
    }

    with pytest.raises(ValidationError, match="at most 32 items"):
        RemoveBranchRequest.model_validate(payload)


def test_replacement_values_are_byte_bounded_before_discovery() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    payload = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "replacement",
                "capability": "pass image",
                "requested_node_type": "Pass",
                "values": {"oversized": "x" * 1_100_000},
            }
        ],
    }

    with pytest.raises(ValidationError, match="branch_operation_request_too_large"):
        ReplaceBranchRequest.model_validate(payload)


def test_successor_resolver_returns_empty_lineage_for_attested_remove() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    compiled = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "delete"},
        workflow,
    )
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"] = [item for item in post_workflow["nodes"] if item["id"] != 3]
    post_workflow["links"] = [item for item in post_workflow["links"] if item[3] != 3]

    result = _resolve_successors(compiled, post_workflow, {})

    assert result["valid"], result["issues"]
    assert result["queued"] is False
    assert result["successor_branch_ids"] == []
    assert result["successor_branch_id"] is None
    assert result["lineage"] == [
        {
            "scope_path": [],
            "predecessor_branch_id": branch.branch_id,
            "predecessor_present": False,
            "successor_branch_ids": [],
        }
    ]


def test_successor_resolver_finds_exact_replacement_region() -> None:
    workflow = {
        "nodes": [_node(77, "ImageSource", outputs=(("image", "IMAGE"),))],
        "links": [],
        "definitions": {"subgraphs": []},
    }
    branch = next(
        item
        for scope in _branch_catalog(workflow).scopes
        for item in scope.branches
        if item.kind == "isolated"
    )
    compiled = _compile(
        {
            **_pins(workflow, branch),
            "operation": "replace",
            "replacement_nodes": [
                {
                    "alias": "replacement_source",
                    "capability": "local image source",
                    "requested_node_type": "ImageSource",
                }
            ],
        },
        workflow,
    )
    post_workflow = {
        "nodes": [_node("new-source", "ImageSource", outputs=(("image", "IMAGE"),))],
        "links": [],
        "definitions": {"subgraphs": []},
    }

    result = _resolve_successors(
        compiled,
        post_workflow,
        {"replacement_source": "new-source"},
    )

    assert result["valid"], result["issues"]
    assert len(result["successor_branch_ids"]) == 1
    assert result["successor_branch_id"] == result["successor_branch_ids"][0]
    assert result["lineage"][0]["predecessor_present"] is False


def test_successor_resolver_covers_clone_preserved_and_created_arms() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    aliases = {
        item["alias"]: node_id
        for item, node_id in zip(
            compiled["plan"]["create_nodes"],
            (5, 6),
            strict=True,
        )
    }
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"].extend(
        [
            _with_planned_layout(
                _node(
                    5,
                    "SafeFilter",
                    inputs=(("image", "IMAGE"),),
                    outputs=(("image", "IMAGE"),),
                    widgets=[0.75],
                ),
                compiled["plan"]["create_nodes"][0],
            ),
            _with_planned_layout(
                _node(
                    6,
                    "SafeSink",
                    inputs=(("image", "IMAGE"),),
                    widgets=["result"],
                ),
                compiled["plan"]["create_nodes"][1],
            ),
        ]
    )
    post_workflow["links"].extend(
        [_link(4, 1, 0, 5, 0), _link(5, 5, 0, 6, 0)]
    )

    result = _resolve_successors(compiled, post_workflow, aliases)

    assert result["valid"], result["issues"]
    assert result["successor_branch_id"] is None
    cloned_arm = _arm_to(post_workflow, 5)
    assert set(result["successor_branch_ids"]) == {
        branch.branch_id,
        cloned_arm.branch_id,
    }
    assert result["lineage"][0]["predecessor_present"] is True


def test_successor_resolver_keeps_exact_preserved_segment_after_clone() -> None:
    workflow = _linear_workflow()
    branch = _segment(workflow)
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    aliases = {
        item["alias"]: node_id
        for item, node_id in zip(
            compiled["plan"]["create_nodes"],
            (4, 5),
            strict=True,
        )
    }
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"].extend(
        [
            _with_planned_layout(
                _node(
                    4,
                    "Pass",
                    inputs=(("image", "IMAGE"),),
                    outputs=(("image", "IMAGE"),),
                ),
                compiled["plan"]["create_nodes"][0],
            ),
            _with_planned_layout(
                _node(
                    5,
                    "SafeSink",
                    inputs=(("image", "IMAGE"),),
                    widgets=["result"],
                ),
                compiled["plan"]["create_nodes"][1],
            ),
        ]
    )
    post_workflow["links"].extend(
        [_link(3, 1, 0, 4, 0), _link(4, 4, 0, 5, 0)]
    )

    result = _resolve_successors(compiled, post_workflow, aliases)

    assert result["valid"], result["issues"]
    post_catalog = _branch_catalog(post_workflow)
    cloned_segment = next(
        item
        for scope in post_catalog.scopes
        for item in scope.branches
        if item.kind == "segment" and {4, 5}.issubset(item.selectable_node_ids)
    )
    assert set(result["successor_branch_ids"]) == {
        branch.branch_id,
        cloned_segment.branch_id,
    }
    assert result["successor_branch_id"] is None


def test_successor_resolver_covers_detached_reconvergent_clone_region() -> None:
    workflow = _side_input_diamond()
    branch = _arm_to(workflow, 2)
    compiled = _compile(
        {
            **_pins(workflow, branch),
            "operation": "clone",
            "acknowledged_risks": ["heavy"],
        },
        workflow,
    )
    [created] = compiled["plan"]["create_nodes"]
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"].append(
        _with_planned_layout(
            _node(
                5,
                "Arm",
                inputs=(("image", "IMAGE"), ("conditioning", "CONDITIONING")),
                outputs=(("image", "IMAGE"),),
                widgets=[1.0],
            ),
            created,
        )
    )
    post_workflow["links"].extend(
        [
            _link(6, 1, 0, 5, 0),
            _link(7, 9, 0, 5, 1, "CONDITIONING"),
        ]
    )

    result = _resolve_successors(compiled, post_workflow, {created["alias"]: 5})

    assert result["valid"], result["issues"]
    post_catalog = _branch_catalog(post_workflow)
    created_arms = {
        item.branch_id
        for scope in post_catalog.scopes
        for item in scope.branches
        if item.kind == "split_arm" and item.owned_node_ids == [5]
    }
    assert created_arms
    assert set(result["successor_branch_ids"]) == {
        branch.branch_id,
        *created_arms,
    }
    assert result["successor_branch_id"] is None


def test_successor_resolver_covers_detached_nonterminal_segment() -> None:
    workflow = _side_input_diamond()
    branch = next(
        item
        for scope in _branch_catalog(workflow).scopes
        for item in scope.branches
        if item.kind == "segment" and item.owned_node_ids == [3]
    )
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    assert compiled["valid"], compiled["issues"]
    [created] = compiled["plan"]["create_nodes"]
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"].append(
        _with_planned_layout(
            _node(
                5,
                "OtherArm",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
            ),
            created,
        )
    )
    post_workflow["links"].append(_link(6, 1, 0, 5, 0))

    result = _resolve_successors(compiled, post_workflow, {created["alias"]: 5})

    assert result["valid"], result["issues"]
    created_successors = set(result["successor_branch_ids"]) - {branch.branch_id}
    assert created_successors
    records = {
        item.branch_id: item
        for scope in _branch_catalog(post_workflow).scopes
        for item in scope.branches
        if item.branch_id in created_successors
    }
    assert set(records) == created_successors
    assert all(5 in item.selectable_node_ids for item in records.values())
    assert result["successor_branch_id"] is None


def test_successor_resolver_rejects_unplanned_created_incident_edge() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    aliases = {
        item["alias"]: node_id
        for item, node_id in zip(
            compiled["plan"]["create_nodes"],
            (5, 6),
            strict=True,
        )
    }
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"].extend(
        [
            _with_planned_layout(
                _node(
                    5,
                    "SafeFilter",
                    inputs=(("image", "IMAGE"),),
                    outputs=(("image", "IMAGE"),),
                    widgets=[0.75],
                ),
                compiled["plan"]["create_nodes"][0],
            ),
            _with_planned_layout(
                _node(
                    6,
                    "SafeSink",
                    inputs=(("image", "IMAGE"),),
                    widgets=["result"],
                ),
                compiled["plan"]["create_nodes"][1],
            ),
        ]
    )
    post_workflow["links"].extend(
        [
            _link(4, 1, 0, 5, 0),
            _link(5, 5, 0, 6, 0),
            _link(6, 1, 0, 6, 0),
        ]
    )

    result = _resolve_successors(compiled, post_workflow, aliases)

    assert result["valid"] is False
    assert result["lineage"] == []
    assert "completed_patch_created_node_incident_edge_mismatch" in {
        item["code"] for item in result["issues"]
    }


def test_successor_resolver_rejects_clone_when_exact_predecessor_disappears() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    aliases = {
        item["alias"]: node_id
        for item, node_id in zip(
            compiled["plan"]["create_nodes"],
            (5, 6),
            strict=True,
        )
    }
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"] = [
        item for item in post_workflow["nodes"] if item["id"] not in {2, 4}
    ]
    post_workflow["nodes"].extend(
        [
            _with_planned_layout(
                _node(
                    5,
                    "SafeFilter",
                    inputs=(("image", "IMAGE"),),
                    outputs=(("image", "IMAGE"),),
                    widgets=[0.75],
                ),
                compiled["plan"]["create_nodes"][0],
            ),
            _with_planned_layout(
                _node(
                    6,
                    "SafeSink",
                    inputs=(("image", "IMAGE"),),
                    widgets=["result"],
                ),
                compiled["plan"]["create_nodes"][1],
            ),
        ]
    )
    post_workflow["links"] = [
        item
        for item in post_workflow["links"]
        if item[1] not in {2, 4} and item[3] not in {2, 4}
    ]
    post_workflow["links"].extend(
        [_link(4, 1, 0, 5, 0), _link(5, 5, 0, 6, 0)]
    )

    result = _resolve_successors(compiled, post_workflow, aliases)

    assert result["valid"] is False
    assert result["lineage"] == []
    assert "completed_patch_asserted_edge_missing" in {
        item["code"] for item in result["issues"]
    }


def test_successor_resolver_rejects_clone_profile_mismatch_without_fallback() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 2)
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    aliases = {
        item["alias"]: node_id
        for item, node_id in zip(
            compiled["plan"]["create_nodes"],
            (5, 6),
            strict=True,
        )
    }
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"].extend(
        [
            _with_planned_layout(
                _node(
                    5,
                    "OtherArm",
                    inputs=(("image", "IMAGE"),),
                    outputs=(("image", "IMAGE"),),
                ),
                compiled["plan"]["create_nodes"][0],
            ),
            _with_planned_layout(
                _node(
                    6,
                    "SafeSink",
                    inputs=(("image", "IMAGE"),),
                    widgets=["result"],
                ),
                compiled["plan"]["create_nodes"][1],
            ),
        ]
    )
    post_workflow["links"].extend(
        [_link(4, 1, 0, 5, 0), _link(5, 5, 0, 6, 0)]
    )

    result = _resolve_successors(compiled, post_workflow, aliases)

    assert result["valid"] is False
    assert result["lineage"] == []
    assert "completed_patch_node_mismatch" in {
        item["code"] for item in result["issues"]
    }


def test_lineage_edge_bound_is_classified_before_locator_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    monkeypatch.setattr(branch_operations, "_MAX_LINEAGE_PREDECESSOR_EDGES", 0)

    result = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "delete"},
        workflow,
    )

    assert result["valid"] is False
    assert result["pending_successor_locator"] is None
    assert "branch_lineage_predecessor_edge_limit_exceeded" in {
        item["code"] for item in result["issues"]
    }


def test_successor_resolver_rejects_forged_locator_facts() -> None:
    workflow = _terminal_split()
    branch = _arm_to(workflow, 3)
    compiled = _compile(
        {**_pins(workflow, branch), "operation": "remove", "mode": "delete"},
        workflow,
    )
    forged = deepcopy(compiled["pending_successor_locator"])
    forged["predecessor_owned_node_ids"] = [4]
    post_workflow = deepcopy(workflow)
    post_workflow["nodes"] = [item for item in post_workflow["nodes"] if item["id"] != 3]
    post_workflow["links"] = [item for item in post_workflow["links"] if item[3] != 3]

    result = resolve_workflow_branch_successors(
        {
            "apply_request": compiled["apply_request"],
            "pending_successor_locator": forged,
            "expected_workflow_identity": WORKFLOW_IDENTITY,
            "expected_graph_hash": "b" * 64,
            "aliases": {},
        },
        post_workflow,
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash="b" * 64,
        catalog=_catalog(),
    )

    assert result["valid"] is False
    assert result["lineage"] == []
    assert "branch_lineage_removed_node_mismatch" in {
        item["code"] for item in result["issues"]
    }

    for field in ("predecessor_kind", "predecessor_fingerprint"):
        forged_profile = deepcopy(compiled["pending_successor_locator"])
        forged_profile["scope_locators"][0][field] = (
            "split_arm" if field == "predecessor_kind" else "f" * 64
        )
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ResolveBranchSuccessorsRequest.model_validate(
                {
                    "apply_request": compiled["apply_request"],
                    "pending_successor_locator": forged_profile,
                    "expected_workflow_identity": WORKFLOW_IDENTITY,
                    "expected_graph_hash": "b" * 64,
                    "aliases": {},
                }
            )


@pytest.mark.parametrize("reused", [False, True])
def test_successor_resolver_returns_exact_nested_scope_lineage(reused: bool) -> None:
    workflow = _nested_linear_workflow(reused=reused)
    branch = _nested_branch(workflow)
    [entry] = branch.entry_edges
    [exit_] = branch.exit_edges
    request = {
        **_pins(workflow, branch),
        "operation": "replace",
        "replacement_nodes": [
            {
                "alias": "replacement",
                "capability": "exact loaded pass replacement",
                "requested_node_type": "Pass",
            }
        ],
        "entry_mappings": [
            {
                "entry_edge_id": entry.edge_id,
                "target_alias": "replacement",
                "target_input": "image",
                "target_mode": "slot",
            }
        ],
        "exit_mappings": [
            {
                "exit_edge_id": exit_.edge_id,
                "source_alias": "replacement",
                "source_output": "image",
                "source_output_index": 0,
            }
        ],
    }
    if reused:
        request.update(
            {
                "scope_edit_mode": "shared_definition",
                "affected_scope_paths": [
                    [{"container_node_id": 10, "subgraph_id": "nested-def"}],
                    [{"container_node_id": "other", "subgraph_id": "nested-def"}],
                ],
            }
        )
    compiled = _compile(request, workflow)
    assert compiled["valid"], compiled["issues"]
    post_workflow = deepcopy(workflow)
    definition = post_workflow["definitions"]["subgraphs"][0]
    definition["nodes"] = [
        _node(
            2,
            "Pass",
            inputs=(("image", "IMAGE"),),
            outputs=(("image", "IMAGE"),),
        )
    ]
    definition["links"] = [_link(1, -10, 0, 2, 0), _link(2, 2, 0, -20, 0)]

    result = _resolve_successors(compiled, post_workflow, {"replacement": 2})

    assert result["valid"], result["issues"]
    expected_scope_count = 2 if reused else 1
    assert len(result["lineage"]) == expected_scope_count
    assert len(result["successor_branch_ids"]) == expected_scope_count
    assert all(item["predecessor_present"] is False for item in result["lineage"])
    if reused:
        assert result["successor_branch_id"] is None
        assert {
            tuple(
                (step["container_node_id"], step["subgraph_id"])
                for step in item["scope_path"]
            )
            for item in result["lineage"]
        } == {
            ((10, "nested-def"),),
            (("other", "nested-def"),),
        }
    else:
        assert result["successor_branch_id"] == result["successor_branch_ids"][0]


def test_successor_resolver_returns_detached_nested_clone_lineage() -> None:
    workflow = _nested_side_input_diamond_workflow()
    branch = next(
        item
        for scope in _branch_catalog(workflow).scopes
        if scope.scope.kind == "subgraph_instance"
        for item in scope.branches
        if item.kind == "split_arm" and item.owned_node_ids == [2]
    )
    compiled = _compile(
        {
            **_pins(workflow, branch),
            "operation": "clone",
            "acknowledged_risks": ["heavy"],
        },
        workflow,
    )
    assert compiled["valid"], compiled["issues"]
    [created] = compiled["plan"]["create_nodes"]
    post_workflow = deepcopy(workflow)
    definition = post_workflow["definitions"]["subgraphs"][0]
    definition["nodes"].append(
        _with_planned_layout(
            _node(
                5,
                "Arm",
                inputs=(("image", "IMAGE"), ("conditioning", "CONDITIONING")),
                outputs=(("image", "IMAGE"),),
                widgets=[1.0],
            ),
            created,
        )
    )
    definition["links"].extend(
        [
            _link(7, -10, 0, 5, 0),
            _link(8, 9, 0, 5, 1, "CONDITIONING"),
        ]
    )
    definition["inputs"][0]["linkIds"] = [1, 2, 7]

    result = _resolve_successors(compiled, post_workflow, {created["alias"]: 5})

    assert result["valid"], result["issues"]
    post_catalog = _branch_catalog(post_workflow)
    created_arms = {
        item.branch_id
        for scope in post_catalog.scopes
        if scope.scope.kind == "subgraph_instance"
        for item in scope.branches
        if item.kind == "split_arm" and item.owned_node_ids == [5]
    }
    assert created_arms
    assert set(result["successor_branch_ids"]) == {
        branch.branch_id,
        *created_arms,
    }
    assert result["successor_branch_id"] is None


def test_successor_resolver_accepts_root_dynamic_clone_live_socket_reprojection() -> None:
    workflow = _dynamic_terminal_split_workflow()
    branch = _arm_to(workflow, 2)
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    assert compiled["valid"], compiled["issues"]
    [created] = compiled["plan"]["create_nodes"]
    post_workflow = deepcopy(workflow)
    cloned = _node(
        4,
        "DynamicNanoTarget",
        outputs=(("IMAGE", "IMAGE"),),
        widgets=["prompt", "nano", "16:9", "1K", "MINIMAL"],
    )
    cloned["inputs"] = _dynamic_live_inputs(3)
    post_workflow["nodes"].append(_with_planned_layout(cloned, created))
    post_workflow["links"].append(_link(3, 1, 0, 4, 5))

    result = _resolve_successors(compiled, post_workflow, {created["alias"]: 4})

    assert result["valid"], result["issues"]
    cloned_arm = _arm_to(post_workflow, 4)
    assert set(result["successor_branch_ids"]) == {
        branch.branch_id,
        cloned_arm.branch_id,
    }
    assert result["successor_branch_id"] is None


def test_successor_resolver_accepts_scoped_dynamic_clone_live_socket_reprojection() -> None:
    workflow = _nested_dynamic_terminal_split_workflow()
    branch = next(
        item
        for scope in _branch_catalog(workflow).scopes
        if scope.scope.kind == "subgraph_instance"
        for item in scope.branches
        if item.kind == "split_arm" and item.owned_node_ids == [2]
    )
    compiled = _compile({**_pins(workflow, branch), "operation": "clone"}, workflow)
    assert compiled["valid"], compiled["issues"]
    [created] = compiled["plan"]["create_nodes"]
    post_workflow = deepcopy(workflow)
    definition = post_workflow["definitions"]["subgraphs"][0]
    cloned = _node(
        4,
        "DynamicNanoTarget",
        outputs=(("IMAGE", "IMAGE"),),
        widgets=["prompt", "nano", "16:9", "1K", "MINIMAL"],
    )
    cloned["inputs"] = _dynamic_live_inputs(3)
    definition["nodes"].append(_with_planned_layout(cloned, created))
    definition["links"].append(_link(3, -10, 0, 4, 5))
    definition["inputs"][0]["linkIds"] = [1, 2, 3]

    result = _resolve_successors(compiled, post_workflow, {created["alias"]: 4})

    assert result["valid"], result["issues"]
    post_catalog = _branch_catalog(post_workflow)
    cloned_arm = next(
        item
        for scope in post_catalog.scopes
        if scope.scope.kind == "subgraph_instance"
        for item in scope.branches
        if item.kind == "split_arm" and item.owned_node_ids == [4]
    )
    assert set(result["successor_branch_ids"]) == {
        branch.branch_id,
        cloned_arm.branch_id,
    }
    assert result["successor_branch_id"] is None
