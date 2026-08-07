import copy

import pytest
from pydantic import ValidationError

from node_library import catalog_contract_hash, node_schema_hash
from workflow_refinement import (
    ApplyWorkflowRefinementRequest,
    GRAPH_PRECONDITION_HASH_SCHEMA,
    NORMALIZED_GRAPH_SCHEMA,
    NormalizedGraphEdge,
    NormalizedGraphOutput,
    WORKFLOW_REFINEMENT_SCHEMA,
    PlanWorkflowRefinementRequest,
    WorkflowRefinementExistingOutput,
    WorkflowRefinementNode,
    WorkflowRefinementSideInputMapping,
    _refinement_has_cycle,
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


def _wavelet_catalog():
    return {
        "GeminiNanoBanana2V2": {
            "python_module": "comfy_api_nodes.nodes_gemini",
            "input": {"required": {"prompt": ["STRING", {"default": ""}]}},
            "output": ["IMAGE", "STRING", "IMAGE"],
            "output_name": ["IMAGE", "text", "thought_image"],
        },
        "LoadImage": {
            "python_module": "nodes",
            "input": {"required": {"image": ["COMBO", {}]}},
            "output": ["IMAGE", "MASK"],
            "output_name": ["IMAGE", "MASK"],
        },
        "WaveletColorFix": {
            "python_module": "custom_nodes.ComfyUI-FrameUtilitys",
            "input": {
                "required": {
                    "target_image": ["IMAGE", {}],
                    "source_image": ["IMAGE", {}],
                    "align_method": [
                        ["adain", "wavelet"],
                        {"default": "wavelet"},
                    ],
                },
                "optional": {"guide_image": ["IMAGE", {}]},
            },
            "output": ["IMAGE"],
            "output_name": ["image"],
        },
        "SaveImage": {
            "python_module": "nodes",
            "input": {
                "required": {
                    "images": ["IMAGE", {}],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                }
            },
            # The terminal node may expose an output even when the requested append
            # intentionally has no downstream edge.
            "output": ["IMAGE"],
            "output_name": ["images"],
        },
        "PreviewImage": {
            "python_module": "nodes",
            "input": {"required": {"images": ["IMAGE", {}]}},
            "output": [],
            "output_name": [],
        },
    }


def _wavelet_workflow():
    return {
        "nodes": [
            {
                "id": 48,
                "type": "LoadImage",
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [2]},
                    {"name": "MASK", "type": "MASK", "links": []},
                ],
            },
            {
                "id": 52,
                "type": "GeminiNanoBanana2V2",
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [1]},
                    {"name": "text", "type": "STRING", "links": []},
                    {"name": "thought_image", "type": "IMAGE", "links": []},
                ],
            },
            {
                "id": 60,
                "type": "PreviewImage",
                "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
                "outputs": [],
            },
            {
                "id": 61,
                "type": "PreviewImage",
                "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
                "outputs": [],
            },
        ],
        "links": [
            [1, 52, 0, 60, 0, "IMAGE"],
            [2, 48, 0, 61, 0, "IMAGE"],
        ],
    }


def _wavelet_append_request(*, side_input_mappings=None, terminal_source=None):
    graph = normalize_workflow_graph(_wavelet_workflow())
    return PlanWorkflowRefinementRequest.model_validate(
        {
            "application_id": "refinement-wavelet-0001",
            "expected_workflow_identity": "fl-mcp-workflow-wavelet",
            "expected_graph_hash": "e" * 64,
            "graph": graph.model_dump(mode="json"),
            "expected_path": {"edges": []},
            "terminal_source": terminal_source
            or {"node_id": 52, "source_output": "IMAGE", "source_output_index": 0},
            "side_input_mappings": side_input_mappings
            if side_input_mappings is not None
            else [
                {
                    "source_node_id": 48,
                    "source_output": "IMAGE",
                    "source_output_index": 0,
                    "target_alias": "wavelet_fix",
                    "target_input": "source_image",
                }
            ],
            "replacement_nodes": [
                {
                    "alias": "wavelet_fix",
                    "node_type": "WaveletColorFix",
                    "values": {"align_method": "wavelet"},
                    "chain_input": "target_image",
                    "chain_output": "image",
                    "chain_output_index": 0,
                },
                {
                    "alias": "save_image",
                    "node_type": "SaveImage",
                    "values": {"filename_prefix": "ren-wavelet-color-fix"},
                    "chain_input": "images",
                    "chain_output": None,
                },
            ],
        }
    )


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


def test_terminal_append_compiles_exact_wavelet_fan_in_and_outputless_save_sink():
    catalog = _wavelet_catalog()
    result = _compile(_wavelet_append_request(), catalog)

    assert result["valid"] is True
    assert result["operation"] == "append"
    assert result["plan"]["expected_path"] == {"nodes": [], "connections": []}
    replacement = result["plan"]["replacement"]
    assert replacement["input"] is None
    assert replacement["output"] is None
    assert [node["alias"] for node in replacement["nodes"]] == [
        "wavelet_fix",
        "save_image",
    ]
    assert replacement["nodes"][0] == {
        "alias": "wavelet_fix",
        "node_type": "WaveletColorFix",
        "schema_hash": node_schema_hash("WaveletColorFix", catalog["WaveletColorFix"]),
        "values": {"align_method": "wavelet"},
    }
    assert replacement["nodes"][1]["values"] == {
        "filename_prefix": "ren-wavelet-color-fix"
    }
    assert replacement["connections"] == [
        {
            "source_alias": "wavelet_fix",
            "source_output_index": 0,
            "source_output": "image",
            "target_alias": "save_image",
            "target_input_index": 0,
            "target_input": "images",
            "type": "IMAGE",
        }
    ]
    assert replacement["primary_input"] == {
        "source_node_id": 52,
        "source_node_type": "GeminiNanoBanana2V2",
        "source_schema_hash": node_schema_hash(
            "GeminiNanoBanana2V2",
            catalog["GeminiNanoBanana2V2"],
        ),
        "source_output_index": 0,
        "source_output": "IMAGE",
        "target_alias": "wavelet_fix",
        "target_input_index": 0,
        "target_input": "target_image",
        "type": "IMAGE",
    }
    assert replacement["side_inputs"] == [
        {
            "source_node_id": 48,
            "source_node_type": "LoadImage",
            "source_schema_hash": node_schema_hash("LoadImage", catalog["LoadImage"]),
            "source_output_index": 0,
            "source_output": "IMAGE",
            "target_alias": "wavelet_fix",
            "target_input_index": 1,
            "target_input": "source_image",
            "type": "IMAGE",
        }
    ]
    assert result["graph"]["node_count"] == 4
    assert result["graph"]["edge_count"] == 2
    assert ApplyWorkflowRefinementRequest.model_validate(result["apply_request"])


def test_existing_output_name_and_index_must_resolve_to_the_same_loaded_slot():
    result = _compile(
        _wavelet_append_request(
            terminal_source={
                "node_id": 52,
                "source_output": "thought_image",
                "source_output_index": 0,
            }
        ),
        _wavelet_catalog(),
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert {issue["code"] for issue in result["issues"]} >= {
        "active_source_output_missing"
    }


def test_existing_output_must_match_active_canvas_and_catalog_even_when_unconnected():
    stale_workflow = _wavelet_workflow()
    nano = next(node for node in stale_workflow["nodes"] if node["id"] == 52)
    nano["outputs"][2]["name"] = "legacy_thought_image"
    stale_graph = normalize_workflow_graph(stale_workflow)
    payload = _wavelet_append_request().model_dump(mode="json")
    payload["graph"] = stale_graph.model_dump(mode="json")
    payload["terminal_source"] = {
        "node_id": 52,
        "source_output": "legacy_thought_image",
        "source_output_index": 2,
    }
    stale = _compile(
        PlanWorkflowRefinementRequest.model_validate(payload),
        _wavelet_catalog(),
    )
    assert stale["valid"] is False
    assert any(
        issue["code"] == "existing_source_schema_mismatch"
        for issue in stale["issues"]
    )

    stale_type_workflow = _wavelet_workflow()
    stale_type_nano = next(
        node for node in stale_type_workflow["nodes"] if node["id"] == 52
    )
    stale_type_nano["outputs"][2]["type"] = "LATENT"
    stale_type_payload = _wavelet_append_request().model_dump(mode="json")
    stale_type_payload["graph"] = normalize_workflow_graph(
        stale_type_workflow
    ).model_dump(mode="json")
    stale_type_payload["terminal_source"] = {
        "node_id": 52,
        "source_output": "thought_image",
        "source_output_index": 2,
    }
    stale_type = _compile(
        PlanWorkflowRefinementRequest.model_validate(stale_type_payload),
        _wavelet_catalog(),
    )
    assert stale_type["valid"] is False
    assert any(
        issue["code"] == "existing_source_schema_mismatch"
        for issue in stale_type["issues"]
    )

    # The unconnected thought_image slot is still present in the pinned canvas
    # snapshot and therefore resolves when canvas and catalog agree exactly.
    valid_payload = _wavelet_append_request().model_dump(mode="json")
    valid_payload["terminal_source"] = {
        "node_id": 52,
        "source_output": "thought_image",
        "source_output_index": 2,
    }
    valid = _compile(
        PlanWorkflowRefinementRequest.model_validate(valid_payload),
        _wavelet_catalog(),
    )
    assert valid["valid"] is True
    assert valid["plan"]["replacement"]["primary_input"][
        "source_output_index"
    ] == 2


def test_duplicate_dynamic_output_names_require_index_and_resolve_exact_unconnected_slot():
    workflow = {
        "nodes": [
            {
                "id": 10,
                "type": "DynamicSource",
                "inputs": [],
                "outputs": [
                    {"name": "image", "type": "IMAGE", "links": []},
                    {"name": "image", "type": "IMAGE", "links": []},
                ],
            }
        ],
        "links": [],
    }
    catalog = {
        "DynamicSource": {
            "python_module": "custom_nodes.dynamic",
            "input": {"required": {}},
            "output": ["IMAGE", "IMAGE"],
            "output_name": ["image", "image"],
        },
        "SaveImage": _wavelet_catalog()["SaveImage"],
    }
    graph = normalize_workflow_graph(workflow)
    base = {
        "application_id": "refinement-dynamic-output",
        "expected_workflow_identity": "fl-mcp-workflow-dynamic",
        "expected_graph_hash": "d" * 64,
        "graph": graph.model_dump(mode="json"),
        "expected_path": {"edges": []},
        "terminal_source": {"node_id": 10, "source_output": "image"},
        "replacement_nodes": [
            {
                "alias": "save_image",
                "node_type": "SaveImage",
                "values": {"filename_prefix": "dynamic-output"},
                "chain_input": "images",
                "chain_output": None,
            }
        ],
    }
    ambiguous = _compile(PlanWorkflowRefinementRequest.model_validate(base), catalog)
    assert ambiguous["valid"] is False
    assert any(
        issue["code"] == "active_source_output_ambiguous"
        for issue in ambiguous["issues"]
    )

    exact_payload = copy.deepcopy(base)
    exact_payload["terminal_source"]["source_output_index"] = 1
    exact = _compile(
        PlanWorkflowRefinementRequest.model_validate(exact_payload),
        catalog,
    )
    assert exact["valid"] is True
    primary = exact["plan"]["replacement"]["primary_input"]
    assert primary["source_output"] == "image"
    assert primary["source_output_index"] == 1


def test_append_rejects_missing_required_side_mapping_and_connection_value_conflict():
    missing = _compile(
        _wavelet_append_request(side_input_mappings=[]),
        _wavelet_catalog(),
    )
    assert missing["valid"] is False
    assert any(
        issue["code"] == "missing_required_connection"
        and issue["path"].endswith("inputs.source_image")
        for issue in missing["issues"]
    )

    payload = _wavelet_append_request().model_dump(mode="json")
    payload["replacement_nodes"][0]["values"]["source_image"] = "not-an-image"
    conflicted = _compile(
        PlanWorkflowRefinementRequest.model_validate(payload),
        _wavelet_catalog(),
    )
    assert conflicted["valid"] is False
    assert any(
        issue["code"] == "value_for_connection_input"
        for issue in conflicted["issues"]
    )


def test_side_input_order_is_hash_invariant_but_source_or_target_changes_are_not():
    source_image = {
        "source_node_id": 48,
        "source_output": "IMAGE",
        "source_output_index": 0,
        "target_alias": "wavelet_fix",
        "target_input": "source_image",
    }
    guide_image = {
        "source_node_id": 52,
        "source_output": "thought_image",
        "source_output_index": 2,
        "target_alias": "wavelet_fix",
        "target_input": "guide_image",
    }
    catalog = _wavelet_catalog()
    first = _compile(
        _wavelet_append_request(side_input_mappings=[source_image, guide_image]),
        catalog,
    )
    reordered = _compile(
        _wavelet_append_request(side_input_mappings=[guide_image, source_image]),
        catalog,
    )
    changed_source = _compile(
        _wavelet_append_request(
            side_input_mappings=[
                source_image,
                {
                    **guide_image,
                    "source_node_id": 48,
                    "source_output": "IMAGE",
                    "source_output_index": 0,
                },
            ]
        ),
        catalog,
    )
    changed_target = _compile(
        _wavelet_append_request(
            side_input_mappings=[
                {**source_image, "target_input": "guide_image"},
                {**guide_image, "target_input": "source_image"},
            ]
        ),
        catalog,
    )

    assert all(
        result["valid"] is True
        for result in (first, reordered, changed_source, changed_target)
    )
    assert first["refinement_hash"] == reordered["refinement_hash"]
    assert first["refinement_hash"] != changed_source["refinement_hash"]
    assert first["refinement_hash"] != changed_target["refinement_hash"]


def test_linear_insert_accepts_retained_side_input_and_rejects_resulting_cycle():
    workflow = _wavelet_workflow()
    graph = normalize_workflow_graph(workflow)
    path_edge = next(
        edge
        for edge in graph.edges
        if edge.source_node_id == 52 and edge.target_node_id == 60
    )
    payload = {
        "application_id": "refinement-wavelet-linear",
        "expected_workflow_identity": "fl-mcp-workflow-wavelet",
        "expected_graph_hash": "c" * 64,
        "graph": graph.model_dump(mode="json"),
        "expected_path": {"edges": [path_edge.model_dump(mode="json")]},
        "side_input_mappings": [
            {
                "source_node_id": 48,
                "source_output": "IMAGE",
                "target_alias": "wavelet_fix",
                "target_input": "source_image",
            }
        ],
        "replacement_nodes": [
            {
                "alias": "wavelet_fix",
                "node_type": "WaveletColorFix",
                "values": {"align_method": "wavelet"},
                "chain_input": "target_image",
                "chain_output": "image",
            }
        ],
    }
    accepted = _compile(
        PlanWorkflowRefinementRequest.model_validate(payload),
        _wavelet_catalog(),
    )
    assert accepted["valid"] is True
    assert accepted["operation"] == "insert"
    assert accepted["plan"]["replacement"]["side_inputs"][0][
        "source_node_id"
    ] == 48

    cyclic_workflow = copy.deepcopy(workflow)
    preview = next(node for node in cyclic_workflow["nodes"] if node["id"] == 60)
    preview["outputs"] = [{"name": "IMAGE", "type": "IMAGE", "links": []}]
    cyclic_graph = normalize_workflow_graph(cyclic_workflow)
    cyclic_path = next(
        edge
        for edge in cyclic_graph.edges
        if edge.source_node_id == 52 and edge.target_node_id == 60
    )
    cyclic_payload = copy.deepcopy(payload)
    cyclic_payload["graph"] = cyclic_graph.model_dump(mode="json")
    cyclic_payload["expected_path"] = {
        "edges": [cyclic_path.model_dump(mode="json")]
    }
    cyclic_payload["side_input_mappings"][0]["source_node_id"] = 60
    cyclic_catalog = copy.deepcopy(_wavelet_catalog())
    cyclic_catalog["PreviewImage"]["output"] = ["IMAGE"]
    cyclic_catalog["PreviewImage"]["output_name"] = ["IMAGE"]
    rejected = _compile(
        PlanWorkflowRefinementRequest.model_validate(cyclic_payload),
        cyclic_catalog,
    )
    assert rejected["valid"] is False
    assert any(issue["code"] == "refinement_cycle" for issue in rejected["issues"])


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
        "missing_required_connection",
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


@pytest.mark.parametrize("invalid_index", [True, "0", 0.0, -1])
@pytest.mark.parametrize(
    "plan_kind,index_path",
    [
        ("append", ("replacement", "connections", 0, "source_output_index")),
        ("append", ("replacement", "connections", 0, "target_input_index")),
        ("append", ("replacement", "primary_input", "source_output_index")),
        ("append", ("replacement", "primary_input", "target_input_index")),
        ("append", ("replacement", "side_inputs", 0, "source_output_index")),
        ("append", ("replacement", "side_inputs", 0, "target_input_index")),
        ("linear", ("expected_path", "connections", 0, "source_output_index")),
        ("linear", ("expected_path", "connections", 0, "target_input_index")),
        ("linear", ("replacement", "input", "target_input_index")),
        ("linear", ("replacement", "output", "source_output_index")),
    ],
)
def test_apply_envelope_rejects_tampered_non_strict_slot_indexes(
    plan_kind,
    index_path,
    invalid_index,
):
    compiled = (
        _compile(_wavelet_append_request(), _wavelet_catalog())
        if plan_kind == "append"
        else _compile(
            _request(
                _workflow(with_middle=False, sibling=False),
                path_indexes=[0],
                replacement_nodes=[_processor()],
            )
        )
    )
    assert compiled["valid"] is True
    tampered = copy.deepcopy(compiled["apply_request"])
    target = tampered["plan"]
    for segment in index_path[:-1]:
        target = target[segment]
    target[index_path[-1]] = invalid_index

    with pytest.raises(ValidationError):
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


def test_refinement_contract_rejects_ambiguous_append_and_ignored_side_inputs():
    append_payload = _wavelet_append_request().model_dump(mode="json")
    append_payload["expected_path"]["edges"] = [
        normalize_workflow_graph(_wavelet_workflow()).edges[0].model_dump(mode="json")
    ]
    with pytest.raises(ValidationError, match="must not define an expected path"):
        PlanWorkflowRefinementRequest.model_validate(append_payload)

    delete_payload = _request(
        _workflow(with_middle=True, sibling=False),
        path_indexes=[0, 1],
        replacement_nodes=[],
    ).model_dump(mode="json")
    delete_payload["side_input_mappings"] = [
        {
            "source_node_id": 1,
            "source_output": "IMAGE",
            "target_alias": "missing",
            "target_input": "image",
        }
    ]
    with pytest.raises(ValidationError, match="cannot accompany deletion"):
        PlanWorkflowRefinementRequest.model_validate(delete_payload)

    missing_output_ref = _wavelet_append_request().model_dump(mode="json")
    missing_output_ref["terminal_source"] = {"node_id": 52}
    with pytest.raises(ValidationError, match="source_output or source_output_index"):
        PlanWorkflowRefinementRequest.model_validate(missing_output_ref)

    invalid_terminal = _wavelet_append_request().model_dump(mode="json")
    invalid_terminal["replacement_nodes"][-1]["chain_output"] = None
    invalid_terminal["replacement_nodes"][-1]["chain_output_index"] = 0
    with pytest.raises(ValidationError, match="chain_output_index"):
        PlanWorkflowRefinementRequest.model_validate(invalid_terminal)


@pytest.mark.parametrize("invalid_index", [True, False, "0", "2", 0.0, 1.5, -1])
def test_external_output_indexes_require_strict_nonnegative_integers(invalid_index):
    with pytest.raises(ValidationError):
        WorkflowRefinementExistingOutput.model_validate(
            {"node_id": 1, "source_output_index": invalid_index}
        )


@pytest.mark.parametrize("invalid_index", [True, False, "0", "2", 0.0, 1.5, -1])
def test_chain_and_normalized_indexes_require_strict_nonnegative_integers(
    invalid_index,
):
    with pytest.raises(ValidationError):
        WorkflowRefinementNode.model_validate(
            {
                "alias": "processor",
                "node_type": "ImageProcessor",
                "values": {},
                "chain_input": "image",
                "chain_output": "IMAGE",
                "chain_output_index": invalid_index,
            }
        )
    with pytest.raises(ValidationError):
        NormalizedGraphOutput.model_validate(
            {
                "node_id": 1,
                "output": "IMAGE",
                "output_index": invalid_index,
                "type": "IMAGE",
            }
        )
    with pytest.raises(ValidationError):
        WorkflowRefinementSideInputMapping.model_validate(
            {
                "source_node_id": 1,
                "source_output_index": invalid_index,
                "target_alias": "target",
                "target_input": "image",
            }
        )


def test_missing_side_source_diagnostic_names_source_node_id_field():
    request = _wavelet_append_request(
        side_input_mappings=[
            {
                "source_node_id": 999,
                "source_output": "IMAGE",
                "source_output_index": 0,
                "target_alias": "wavelet_fix",
                "target_input": "source_image",
            }
        ]
    )
    result = _compile(request, _wavelet_catalog())
    issue = next(
        item for item in result["issues"]
        if item["code"] == "existing_source_node_missing"
    )
    assert issue["path"] == "side_input_mappings[0].source_node_id"


def test_cycle_detection_handles_five_thousand_node_chain_iteratively():
    edges = {}
    for node_id in range(4_999):
        edge = NormalizedGraphEdge(
            source_node_id=node_id,
            source_output="IMAGE",
            source_output_index=0,
            target_node_id=node_id + 1,
            target_input="image",
            target_input_index=0,
            type="IMAGE",
        )
        edges[node_id] = edge

    assert _refinement_has_cycle(
        edges,
        removed_path_edges=[],
        removed_nodes=[],
        planned_edges=[],
    ) is False
    assert _refinement_has_cycle(
        edges,
        removed_path_edges=[],
        removed_nodes=[],
        planned_edges=[
            (
                ("existing", "int", "4999"),
                ("existing", "int", "0"),
            )
        ],
    ) is True
