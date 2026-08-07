import copy

import pytest
from pydantic import ValidationError

from node_library import catalog_contract_hash
from workflow_refinement import (
    ApplyWorkflowRefinementRequest,
    GRAPH_PRECONDITION_HASH_SCHEMA,
    NORMALIZED_GRAPH_SCHEMA,
    WORKFLOW_REFINEMENT_SCHEMA,
    PlanWorkflowRefinementRequest,
    compile_workflow_refinement,
    normalize_workflow_graph,
)


def _catalog():
    return {
        "ImageProcessor": {
            "display_name": "Image Processor",
            "input": {
                "required": {
                    "strength": ["FLOAT", {"min": 0.0, "max": 1.0}],
                    "mode": [["soft", "hard"]],
                    "image": ["IMAGE"],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "comfy_extras.nodes_example",
        },
        "ImageProcessorTwo": {
            "display_name": "Image Processor Two",
            "input": {
                "required": {
                    "strength": ["FLOAT", {"min": 0.0, "max": 1.0}],
                    "image": ["IMAGE"],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "comfy_extras.nodes_example",
        },
        "MaskProcessor": {
            "input": {"required": {"image": ["IMAGE"]}},
            "output": ["MASK"],
            "output_name": ["MASK"],
            "python_module": "comfy_extras.nodes_mask",
        },
        "NeedsMask": {
            "input": {
                "required": {
                    "image": ["IMAGE"],
                    "mask": ["MASK"],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "comfy_extras.nodes_mask",
        },
    }


def _node(node_id, node_type, *, input_type=None, output_type=None):
    inputs = []
    if input_type:
        inputs.append({"name": "image" if node_type != "Save" else "images", "type": input_type})
    outputs = []
    if output_type:
        outputs.append({"name": output_type, "type": output_type, "links": []})
    return {
        "id": node_id,
        "type": node_type,
        "inputs": inputs,
        "outputs": outputs,
        "widgets_values": [],
    }


def _workflow(*, with_middle=True, sibling=True, middle_output="IMAGE"):
    nodes = [
        _node(1, "Source", output_type="IMAGE"),
        _node(3, "Save", input_type=middle_output),
    ]
    links = []
    next_link = 1
    if with_middle:
        nodes.insert(
            1,
            _node(2, "ExistingProcessor", input_type="IMAGE", output_type=middle_output),
        )
        links.extend(
            [
                [next_link, 1, 0, 2, 0, "IMAGE"],
                [next_link + 1, 2, 0, 3, 0, middle_output],
            ]
        )
        next_link += 2
    else:
        links.append([next_link, 1, 0, 3, 0, "IMAGE"])
        next_link += 1
    if sibling:
        nodes.append(_node(4, "Preview", input_type="IMAGE"))
        links.append([next_link, 1, 0, 4, 0, "IMAGE"])
    return {"nodes": nodes, "links": links}


def _request(
    workflow,
    *,
    path_indexes,
    replacement_nodes,
    expected_catalog_hash=None,
    application_id="refinement-test-0001",
):
    graph = normalize_workflow_graph(workflow)
    return PlanWorkflowRefinementRequest.model_validate(
        {
            "application_id": application_id,
            "expected_workflow_identity": "fl-mcp-workflow-1",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": expected_catalog_hash,
            "graph": graph.model_dump(mode="json"),
            "expected_path": {
                "edges": [graph.edges[index].model_dump(mode="json") for index in path_indexes]
            },
            "replacement_nodes": replacement_nodes,
        }
    )


def _compile(request, catalog=None):
    catalog = catalog or _catalog()
    return compile_workflow_refinement(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://127.0.0.1:8188/object_info",
    )


def _processor(alias="processor", node_type="ImageProcessor", **overrides):
    value = {
        "alias": alias,
        "node_type": node_type,
        "values": {"strength": 0.5, "mode": "soft"},
        "chain_input": "image",
        "chain_output": "IMAGE",
    }
    value.update(overrides)
    return value


def test_normalizer_resolves_array_and_mapping_links_with_exact_slot_facts():
    workflow = _workflow(with_middle=False, sibling=False)
    graph = normalize_workflow_graph(workflow)

    assert graph.schema == NORMALIZED_GRAPH_SCHEMA
    assert graph.complete is True
    assert [node.node_id for node in graph.nodes] == [1, 3]
    assert graph.edges[0].model_dump() == {
        "source_node_id": 1,
        "source_output": "IMAGE",
        "source_output_index": 0,
        "target_node_id": 3,
        "target_input": "images",
        "target_input_index": 0,
        "type": "IMAGE",
    }

    mapped = copy.deepcopy(workflow)
    mapped["nodes"] = {str(node["id"]): node for node in mapped["nodes"]}
    mapped["links"] = {
        "7": {
            "id": 7,
            "origin_id": 1,
            "origin_slot": 0,
            "target_id": 3,
            "target_slot": 0,
            "type": "IMAGE",
        }
    }
    assert normalize_workflow_graph(mapped) == graph


def test_named_slot_mappings_use_canonical_key_order_not_insertion_order():
    first = _workflow(with_middle=False, sibling=False)
    second = copy.deepcopy(first)
    first["nodes"][1]["inputs"] = {
        "z_mask": {"name": "mask", "type": "MASK", "link": None},
        "a_image": {"name": "images", "type": "IMAGE", "link": 1},
    }
    second["nodes"][1]["inputs"] = {
        "a_image": {"name": "images", "type": "IMAGE", "link": 1},
        "z_mask": {"name": "mask", "type": "MASK", "link": None},
    }

    first_graph = normalize_workflow_graph(first)
    second_graph = normalize_workflow_graph(second)

    assert first_graph.model_dump(mode="json") == second_graph.model_dump(mode="json")
    assert first_graph.edges[0].target_input == "images"


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value["links"].append(list(value["links"][0])), "duplicate edge"),
        (lambda value: value["links"][0].__setitem__(1, 99), "missing endpoint"),
        (lambda value: value["links"][0].__setitem__(2, 4), "missing endpoint slot"),
        (lambda value: value["nodes"][1]["inputs"][0].pop("name"), "unnamed or untyped"),
    ],
)
def test_normalizer_never_marks_malformed_or_incomplete_graphs_complete(mutate, match):
    workflow = _workflow(with_middle=False, sibling=False)
    mutate(workflow)
    with pytest.raises(ValueError, match=match):
        normalize_workflow_graph(workflow)


def test_insert_plan_is_canonical_hashed_and_frontend_apply_ready():
    workflow = _workflow(with_middle=False, sibling=True)
    request = _request(
        workflow,
        path_indexes=[0],
        replacement_nodes=[_processor()],
    )
    result = _compile(request)

    assert result["valid"] is True
    assert result["operation"] == "insert"
    assert len(result["refinement_hash"]) == 64
    assert result["refinement_hash_schema"] == WORKFLOW_REFINEMENT_SCHEMA
    assert result["graph"]["graph_hash_schema"] == GRAPH_PRECONDITION_HASH_SCHEMA
    assert result["plan"]["expected_path"]["nodes"] == []
    assert len(result["plan"]["expected_path"]["connections"]) == 1
    replacement = result["plan"]["replacement"]
    assert replacement["input"] == {
        "target_alias": "processor",
        "target_input_index": 0,
        "target_input": "image",
        "type": "IMAGE",
    }
    assert replacement["output"]["source_output_index"] == 0
    assert replacement["nodes"][0]["values"]["strength"] == 0.5

    validated = ApplyWorkflowRefinementRequest.model_validate(result["apply_request"])
    assert validated.model_dump(mode="json") == result["apply_request"]
    assert set(result["apply_request"]) == {
        "application_id",
        "expected_catalog_hash",
        "refinement_hash",
        "plan",
    }
    assert result["apply_request"]["expected_catalog_hash"] == result["catalog"][
        "catalog_hash"
    ]


def test_replace_plan_derives_internal_nodes_and_exact_alias_chain():
    workflow = _workflow(with_middle=True, sibling=True)
    request = _request(
        workflow,
        path_indexes=[0, 2],
        replacement_nodes=[
            _processor("first"),
            _processor(
                "second",
                "ImageProcessorTwo",
                values={"strength": 0.25},
            ),
        ],
    )
    result = _compile(request)

    assert result["valid"] is True
    assert result["operation"] == "replace"
    assert result["plan"]["expected_path"]["nodes"] == [
        {"node_id": 2, "node_type": "ExistingProcessor"}
    ]
    replacement = result["plan"]["replacement"]
    assert [node["alias"] for node in replacement["nodes"]] == ["first", "second"]
    assert replacement["connections"] == [
        {
            "source_alias": "first",
            "source_output_index": 0,
            "source_output": "IMAGE",
            "target_alias": "second",
            "target_input_index": 0,
            "target_input": "image",
            "type": "IMAGE",
        }
    ]


def test_chain_input_value_conflict_is_rejected_and_not_planned_as_a_widget():
    request = _request(
        _workflow(with_middle=False, sibling=False),
        path_indexes=[0],
        replacement_nodes=[
            _processor(values={"strength": 0.5, "mode": "soft", "image": "fake"})
        ],
    )
    result = _compile(request)

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert any(
        item["code"] == "value_for_connection_input" for item in result["issues"]
    )


def test_delete_plan_splices_one_strict_internal_node_and_has_no_replacement():
    request = _request(
        _workflow(with_middle=True, sibling=True),
        path_indexes=[0, 2],
        replacement_nodes=[],
    )
    result = _compile(request)

    assert result["valid"] is True
    assert result["operation"] == "delete"
    assert result["plan"]["replacement"] is None
    assert result["plan"]["expected_path"]["nodes"][0]["node_id"] == 2
    assert ApplyWorkflowRefinementRequest.model_validate(result["apply_request"])


def test_nonlinear_internal_node_is_rejected_before_sibling_loss():
    workflow = _workflow(with_middle=True, sibling=False)
    workflow["nodes"].append(_node(5, "SecondPreview", input_type="IMAGE"))
    workflow["links"].append([9, 2, 0, 5, 0, "IMAGE"])
    request = _request(
        workflow,
        path_indexes=[0, 1],
        replacement_nodes=[_processor()],
    )
    result = _compile(request)

    assert result["valid"] is False
    assert result["refinement_hash"] is None
    assert result["apply_request"] is None
    issue = next(item for item in result["issues"] if item["code"] == "non_linear_target")
    assert "sibling branch" in issue["message"]


def test_missing_changed_or_discontiguous_path_fails_closed():
    workflow = _workflow(with_middle=True, sibling=True)
    graph = normalize_workflow_graph(workflow)
    changed = graph.edges[0].model_copy(update={"source_output": "changed"})
    request = PlanWorkflowRefinementRequest.model_validate(
        {
            "application_id": "refinement-test-path",
            "expected_workflow_identity": "fl-mcp-workflow-1",
            "expected_graph_hash": "b" * 64,
            "graph": graph.model_dump(mode="json"),
            "expected_path": {
                "edges": [
                    changed.model_dump(mode="json"),
                    graph.edges[1].model_dump(mode="json"),
                ]
            },
            "replacement_nodes": [_processor()],
        }
    )
    result = _compile(request)
    codes = {item["code"] for item in result["issues"]}

    assert result["valid"] is False
    assert {"path_edge_changed", "path_not_contiguous"} <= codes


def test_replacement_catalog_values_slots_and_required_side_inputs_are_validated():
    workflow = _workflow(with_middle=False, sibling=False)
    request = _request(
        workflow,
        path_indexes=[0],
        replacement_nodes=[
            _processor(values={"strength": 4.0, "mode": "invalid"}),
            _processor(
                "masked",
                "NeedsMask",
                values={},
            ),
        ],
        expected_catalog_hash="f" * 64,
    )
    result = _compile(request)
    codes = {item["code"] for item in result["issues"]}

    assert result["valid"] is False
    assert {
        "catalog_changed",
        "value_above_maximum",
        "invalid_option",
        "required_side_input_unsupported",
    } <= codes


def test_incompatible_replacement_and_delete_boundaries_are_rejected():
    insert = _request(
        _workflow(with_middle=False, sibling=False),
        path_indexes=[0],
        replacement_nodes=[
            {
                "alias": "masker",
                "node_type": "MaskProcessor",
                "values": {},
                "chain_input": "image",
                "chain_output": "MASK",
            }
        ],
    )
    insert_result = _compile(insert)
    assert any(
        item["code"] == "incompatible_boundary_output"
        for item in insert_result["issues"]
    )

    delete = _request(
        _workflow(with_middle=True, sibling=False, middle_output="MASK"),
        path_indexes=[0, 1],
        replacement_nodes=[],
    )
    delete_result = _compile(delete)
    assert any(
        item["code"] == "incompatible_delete_boundaries"
        for item in delete_result["issues"]
    )


def test_refinement_hash_is_stable_but_changes_with_canonical_values():
    workflow = _workflow(with_middle=False, sibling=True)
    first = _compile(
        _request(workflow, path_indexes=[0], replacement_nodes=[_processor()])
    )
    reordered = copy.deepcopy(workflow)
    reordered["nodes"] = list(reversed(reordered["nodes"]))
    reordered["links"] = list(reversed(reordered["links"]))
    second = _compile(
        _request(reordered, path_indexes=[0], replacement_nodes=[_processor()])
    )
    changed = _compile(
        _request(
            workflow,
            path_indexes=[0],
            replacement_nodes=[_processor(values={"strength": 0.75, "mode": "soft"})],
        )
    )

    assert first["refinement_hash"] == second["refinement_hash"]
    assert first["refinement_hash"] != changed["refinement_hash"]


def test_apply_request_rejects_a_plan_tampered_under_an_old_hash():
    compiled = _compile(
        _request(
            _workflow(with_middle=False, sibling=False),
            path_indexes=[0],
            replacement_nodes=[_processor()],
        )
    )
    tampered = copy.deepcopy(compiled["apply_request"])
    tampered["plan"]["replacement"]["nodes"][0]["values"]["strength"] = 0.9

    with pytest.raises(ValidationError, match="canonical plan"):
        ApplyWorkflowRefinementRequest.model_validate(tampered)


def test_frontend_graph_hash_is_required_opaque_lowercase_sha256():
    graph = normalize_workflow_graph(_workflow(with_middle=False, sibling=False))
    payload = {
        "application_id": "refinement-test-hash",
        "expected_workflow_identity": "fl-mcp-workflow-1",
        "expected_graph_hash": "opaque",
        "graph": graph.model_dump(mode="json"),
        "expected_path": {"edges": [graph.edges[0].model_dump(mode="json")]},
        "replacement_nodes": [_processor()],
    }
    with pytest.raises(ValidationError, match="expected_graph_hash"):
        PlanWorkflowRefinementRequest.model_validate(payload)
