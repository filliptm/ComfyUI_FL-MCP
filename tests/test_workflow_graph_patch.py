import copy

import pytest
from node_library import catalog_contract_hash, node_schema_hash
from pydantic import ValidationError
from workflow_graph_patch import (
    GRAPH_PATCH_HASH_SCHEMA,
    GRAPH_PATCH_SCHEMA,
    ApplyGraphPatchRequest,
    GraphPatchEdge,
    GraphPatchPlan,
    GraphPatchTargetEndpoint,
    PlanGraphPatchRequest,
    compile_graph_patch,
    graph_patch_hash,
    graph_patch_request_from_apply,
)
from workflow_refinement import NormalizedGraphSnapshot


def _catalog():
    return {
        "LoadImage": {
            "input": {
                "required": {
                    "image": [
                        ["factory.png", "portrait.png"],
                        {"image_upload": True},
                    ]
                }
            },
            "output": ["IMAGE", "MASK"],
            "output_name": ["IMAGE", "MASK"],
            "python_module": "nodes",
        },
        "GeminiNanoBanana2V2": {
            "input": {
                "required": {
                    "control_0": ["IMAGE", {"forceInput": True}],
                    "control_1": ["IMAGE", {"forceInput": True}],
                    "control_2": ["IMAGE", {"forceInput": True}],
                    "control_3": ["IMAGE", {"forceInput": True}],
                    "control_4": ["IMAGE", {"forceInput": True}],
                    "model.images.image_1": ["IMAGE", {"forceInput": True}],
                    "model.images.image_2": ["IMAGE", {"forceInput": True}],
                    "model.images.image_3": ["IMAGE", {"forceInput": True}],
                }
            },
            "output": ["IMAGE", "STRING", "IMAGE"],
            "output_name": ["IMAGE", "STRING", "thought_image"],
            "python_module": "comfy_api_nodes.nodes_gemini",
        },
        "PreviewImage": {
            "input": {"required": {"images": ["IMAGE"]}},
            "output": ["IMAGE"],
            "output_name": ["images"],
            "python_module": "nodes",
        },
        "WaveletColorFix": {
            "input": {
                "required": {
                    "target_image": ["IMAGE"],
                    "source_image": ["IMAGE"],
                    "align_method": [["wavelet", "adain"], {"default": "wavelet"}],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["image"],
            "python_module": "custom_nodes.frame_utils",
        },
        "SaveImage": {
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["images"],
            "python_module": "nodes",
        },
        "ByteDance2ReferenceNode": {
            "input": {
                "required": {
                    "model": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {
                            "options": [
                                {
                                    "key": "Seedance 2.0",
                                    "inputs": {
                                        "required": {
                                            "prompt": ["STRING", {"default": "", "multiline": True}],
                                            "resolution": [["480p", "720p", "1080p", "4k"]],
                                            "ratio": [["16:9", "21:9", "adaptive"]],
                                            "duration": ["INT", {"default": 7, "min": 4, "max": 15}],
                                            "generate_audio": ["BOOLEAN", {"default": True}],
                                            "reference_images": [
                                                "COMFY_AUTOGROW_V3",
                                                {
                                                    "template": {
                                                        "input": {
                                                            "required": {
                                                                "reference_image": ["IMAGE"]
                                                            }
                                                        },
                                                        "names": ["image_1", "image_2"],
                                                        "min": 0,
                                                    }
                                                },
                                            ],
                                            "reference_videos": [
                                                "COMFY_AUTOGROW_V3",
                                                {
                                                    "template": {
                                                        "input": {
                                                            "required": {
                                                                "reference_video": ["VIDEO"]
                                                            }
                                                        },
                                                        "names": ["video_1"],
                                                        "min": 0,
                                                    }
                                                },
                                            ],
                                        },
                                        "optional": {
                                            "auto_downscale": ["BOOLEAN", {"default": True}],
                                            "auto_upscale": ["BOOLEAN", {"default": False}],
                                        },
                                    },
                                }
                            ]
                        },
                    ],
                    "seed": ["INT", {"default": 0, "min": 0, "max": 2147483647}],
                    "watermark": ["BOOLEAN", {"default": False}],
                }
            },
            "output": ["VIDEO"],
            "output_name": ["VIDEO"],
            "python_module": "comfy_api_nodes.nodes_bytedance",
            "api_node": True,
        },
        "SaveVideo": {
            "input": {
                "required": {
                    "video": ["VIDEO"],
                    "filename_prefix": ["STRING", {"default": "video/ComfyUI"}],
                    "format": [["auto", "mp4"], {"default": "auto"}],
                    "codec": [["auto", "h264"], {"default": "auto"}],
                }
            },
            "output": ["VIDEO"],
            "output_name": ["video"],
            "python_module": "comfy_extras.nodes_video",
        },
        "GetVideoComponents": {
            "input": {"required": {"video": ["VIDEO"]}},
            "output": ["IMAGE", "AUDIO", "FLOAT", "INT"],
            "output_name": ["images", "audio", "fps", "bit_depth"],
            "python_module": "comfy_extras.nodes_video",
        },
        "VHS_VideoCombine": {
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "frame_rate": ["FLOAT", {"default": 8, "min": 1}],
                    "loop_count": ["INT", {"default": 0, "min": 0}],
                    "filename_prefix": ["STRING", {"default": "AnimateDiff"}],
                    "format": [["video/h264-mp4", "video/webm"]],
                    "pingpong": ["BOOLEAN", {"default": False}],
                    "save_output": ["BOOLEAN", {"default": True}],
                },
                "optional": {"audio": ["AUDIO"]},
            },
            "output": ["VHS_FILENAMES"],
            "output_name": ["Filenames"],
            "python_module": "custom_nodes.ComfyUI-VideoHelperSuite",
        },
        "ComboSource": {
            "input": {},
            "output": ["COMBO"],
            "output_name": ["mode"],
        },
        "ComboTarget": {
            "input": {"required": {"mode": [["fast", "quality"], {"default": "fast"}]}},
            "output": [],
        },
        "ImageSource": {
            "input": {},
            "output": ["IMAGE"],
            "output_name": ["image"],
        },
        "MatchTarget": {
            "input": {
                "required": {
                    "payload": [
                        "COMFY_MATCHTYPE_V3",
                        {
                            "template": {
                                "template_id": "payload_type",
                                "allowed_types": ["IMAGE", "MASK"],
                            }
                        },
                    ]
                }
            },
            "output": [],
        },
        "ListImageSource": {
            "input": {},
            "output": ["IMAGE"],
            "output_name": ["images"],
            "output_is_list": [True],
        },
        "ScalarImageTarget": {
            "input": {"required": {"images": ["IMAGE", {}]}},
            "output": [],
        },
        "ListImageTarget": {
            "input": {"required": {"images": ["IMAGE", {}]}},
            "is_input_list": True,
            "output": [],
        },
        "AutogrowTarget": {
            "input": {
                "required": {
                    "items": [
                        "COMFY_AUTOGROW_V3",
                        {
                            "template": {
                                "input": {"required": {"item": ["IMAGE", {}]}},
                                "names": ["item_1", "item_2"],
                                "min": 1,
                            }
                        },
                    ]
                }
            },
            "output": [],
        },
    }


def _graph():
    nodes = [
        {"node_id": 48, "node_type": "LoadImage"},
        {"node_id": 49, "node_type": "LoadImage"},
        {"node_id": 50, "node_type": "GeminiNanoBanana2V2"},
        {"node_id": 53, "node_type": "PreviewImage"},
        {"node_id": 51, "node_type": "GeminiNanoBanana2V2"},
        {"node_id": 54, "node_type": "PreviewImage"},
        {"node_id": 52, "node_type": "GeminiNanoBanana2V2"},
        {"node_id": 60, "node_type": "WaveletColorFix"},
        {"node_id": 61, "node_type": "SaveImage"},
    ]
    outputs = []
    for node_id in (48, 49):
        outputs.extend(
            [
                {"node_id": node_id, "output": "IMAGE", "output_index": 0, "type": "IMAGE"},
                {"node_id": node_id, "output": "MASK", "output_index": 1, "type": "MASK"},
            ]
        )
    for node_id in (50, 51, 52):
        outputs.extend(
            [
                {"node_id": node_id, "output": "IMAGE", "output_index": 0, "type": "IMAGE"},
                {"node_id": node_id, "output": "STRING", "output_index": 1, "type": "STRING"},
                {"node_id": node_id, "output": "thought_image", "output_index": 2, "type": "IMAGE"},
            ]
        )
    outputs.extend(
        [
            {"node_id": 53, "output": "images", "output_index": 0, "type": "IMAGE"},
            {"node_id": 54, "output": "images", "output_index": 0, "type": "IMAGE"},
            {"node_id": 60, "output": "image", "output_index": 0, "type": "IMAGE"},
            {"node_id": 61, "output": "images", "output_index": 0, "type": "IMAGE"},
        ]
    )
    raw_edges = [
        (51, 0, "IMAGE", 52, 5, "model.images.image_1", "IMAGE"),
        (50, 0, "IMAGE", 51, 5, "model.images.image_1", "IMAGE"),
        (49, 0, "IMAGE", 50, 5, "model.images.image_1", "IMAGE"),
        (49, 0, "IMAGE", 52, 6, "model.images.image_2", "IMAGE"),
        (49, 0, "IMAGE", 51, 6, "model.images.image_2", "IMAGE"),
        (48, 0, "IMAGE", 51, 7, "model.images.image_3", "IMAGE"),
        (48, 0, "IMAGE", 50, 6, "model.images.image_2", "IMAGE"),
        (48, 0, "IMAGE", 52, 7, "model.images.image_3", "IMAGE"),
        (50, 0, "IMAGE", 53, 0, "images", "IMAGE"),
        (51, 0, "IMAGE", 54, 0, "images", "IMAGE"),
        (52, 0, "IMAGE", 60, 0, "target_image", "IMAGE"),
        (48, 0, "IMAGE", 60, 1, "source_image", "IMAGE"),
        (60, 0, "image", 61, 0, "images", "IMAGE"),
    ]
    edges = [
        {
            "source_node_id": source_id,
            "source_output_index": source_index,
            "source_output": source_name,
            "target_node_id": target_id,
            "target_input_index": target_index,
            "target_input": target_name,
            "type": edge_type,
        }
        for (
            source_id,
            source_index,
            source_name,
            target_id,
            target_index,
            target_name,
            edge_type,
        ) in raw_edges
    ]
    return NormalizedGraphSnapshot(
        schema="fl-mcp.normalized-workflow-graph.v1",
        complete=True,
        nodes=nodes,
        outputs=outputs,
        edges=edges,
    )


def _existing(node_id):
    return {"node_id": node_id}


def _new(alias):
    return {"alias": alias}


def _edge(
    source_ref,
    output_index,
    output,
    edge_type,
    target_ref,
    input_index,
    input_name,
    *,
    socket_index,
    occurrence_index=0,
    mode="slot",
):
    return {
        "source": {
            "ref": source_ref,
            "output_index": output_index,
            "output": output,
            "type": edge_type,
        },
        "target": {
            "ref": target_ref,
            "input_index": input_index,
            "occurrence_index": occurrence_index,
            "socket_index": socket_index,
            "input": input_name,
            "type": edge_type,
            "mode": mode,
        },
    }


def _assertion(node_id, node_type, catalog):
    return {
        "ref": _existing(node_id),
        "node_type": node_type,
        "schema_hash": node_schema_hash(node_type, catalog[node_type]),
    }


def _creates(catalog):
    return [
        {
            "alias": "seedance_reference",
            "node_type": "ByteDance2ReferenceNode",
            "schema_hash": node_schema_hash(
                "ByteDance2ReferenceNode", catalog["ByteDance2ReferenceNode"]
            ),
            "values": {
                "model": "Seedance 2.0",
                "model.prompt": "Preserve the subject and animate the approved image.",
                "model.resolution": "1080p",
                "model.ratio": "adaptive",
                "model.duration": 7,
                "model.generate_audio": True,
                "model.auto_downscale": True,
                "model.auto_upscale": False,
                "seed": 0,
                "watermark": False,
            },
            "layout_hint": {"x": 3000, "y": -400, "width": 420, "height": 420},
        },
        {
            "alias": "save_video",
            "node_type": "SaveVideo",
            "schema_hash": node_schema_hash("SaveVideo", catalog["SaveVideo"]),
            "values": {
                "filename_prefix": "video/ComfyUI",
                "format": "auto",
                "codec": "auto",
            },
        },
        {
            "alias": "video_components",
            "node_type": "GetVideoComponents",
            "schema_hash": node_schema_hash(
                "GetVideoComponents", catalog["GetVideoComponents"]
            ),
            "values": {},
        },
        {
            "alias": "video_combine",
            "node_type": "VHS_VideoCombine",
            "schema_hash": node_schema_hash(
                "VHS_VideoCombine", catalog["VHS_VideoCombine"]
            ),
            "values": {
                "loop_count": 0,
                "filename_prefix": "AnimateDiff",
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True,
            },
        },
    ]


def _seedance_edges():
    return [
        _edge(
            _existing(60),
            0,
            "image",
            "IMAGE",
            _new("seedance_reference"),
            6,
            "model.reference_images.image_1",
            socket_index=0,
        ),
        _edge(
            _new("seedance_reference"),
            0,
            "VIDEO",
            "VIDEO",
            _new("save_video"),
            0,
            "video",
            socket_index=0,
        ),
        _edge(
            _new("seedance_reference"),
            0,
            "VIDEO",
            "VIDEO",
            _new("video_components"),
            0,
            "video",
            socket_index=0,
        ),
        _edge(
            _new("video_components"),
            0,
            "images",
            "IMAGE",
            _new("video_combine"),
            0,
            "images",
            socket_index=0,
        ),
        _edge(
            _new("video_components"),
            1,
            "audio",
            "AUDIO",
            _new("video_combine"),
            7,
            "audio",
            socket_index=1,
        ),
        _edge(
            _new("video_components"),
            2,
            "fps",
            "FLOAT",
            _new("video_combine"),
            1,
            "frame_rate",
            socket_index=None,
            mode="convert_widget",
        ),
    ]


def _request(*, reverse=False):
    catalog = _catalog()
    creates = _creates(catalog)
    edges = _seedance_edges()
    if reverse:
        creates.reverse()
        edges.reverse()
    return PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-seedance-0001",
            "expected_workflow_identity": "fl-mcp-workflow-fixture-0001",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": _graph().model_dump(mode="json"),
            "assertions": {"nodes": [_assertion(60, "WaveletColorFix", catalog)], "edges": []},
            "create_nodes": creates,
            "add_edges": edges,
        }
    )


def _compile(request):
    catalog = _catalog()
    return compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://127.0.0.1:8188/object_info",
    )


def _codes(result):
    return {issue["code"] for issue in result["issues"]}


def test_seedance_graph_patch_supports_created_alias_fanout_and_widget_conversion():
    result = _compile(_request())

    assert result["valid"] is True, result["issues"]
    assert result["schema"] == GRAPH_PATCH_SCHEMA
    assert result["patch_hash_schema"] == GRAPH_PATCH_HASH_SCHEMA
    assert result["error_count"] == 0
    assert result["apply_request"]["plan"]["operation"] == "patch"
    assert result["apply_request"]["plan"]["expected_delta"] == {
        "created_node_count": 4,
        "updated_node_count": 0,
        "removed_node_count": 0,
        "added_edge_count": 6,
        "removed_edge_count": 0,
        "final_node_count": 13,
        "final_edge_count": 19,
    }
    assert len(result["expected_final"]["nodes"]) == 13
    assert len(result["expected_final"]["edges"]) == 19

    added = result["plan"]["add_edges"]
    fps_edge = next(edge for edge in added if edge["source"]["output"] == "fps")
    assert fps_edge["source"]["ref"] == {"alias": "video_components"}
    assert fps_edge["target"] == {
        "ref": {"alias": "video_combine"},
        "input_index": 1,
        "occurrence_index": 0,
        "socket_index": None,
        "input": "frame_rate",
        "type": "FLOAT",
        "mode": "convert_widget",
    }
    seedance_targets = [
        edge["target"]["ref"]
        for edge in added
        if edge["source"]["ref"] == {"alias": "seedance_reference"}
    ]
    assert seedance_targets == [{"alias": "save_video"}, {"alias": "video_components"}]

    baseline = {
        (
            edge.source_node_id,
            edge.source_output_index,
            edge.target_node_id,
            edge.target_input_index,
        )
        for edge in _graph().edges
    }
    final_existing = {
        (
            edge["source"]["ref"]["node_id"],
            edge["source"]["output_index"],
            edge["target"]["ref"]["node_id"],
            edge["target"]["socket_index"],
        )
        for edge in result["expected_final"]["edges"]
        if "node_id" in edge["source"]["ref"] and "node_id" in edge["target"]["ref"]
    }
    assert baseline <= final_existing


def test_canonical_order_and_hash_do_not_depend_on_request_order():
    forward = _compile(_request())
    reverse = _compile(_request(reverse=True))

    assert forward["valid"] and reverse["valid"]
    assert forward["patch_hash"] == reverse["patch_hash"]
    assert forward["plan"] == reverse["plan"]


def test_patch_hash_changes_when_a_canonical_value_changes():
    baseline = _compile(_request())
    changed_request = _request()
    changed_request.create_nodes[0].values["model.duration"] = 8
    changed = _compile(changed_request)

    assert baseline["valid"] and changed["valid"]
    assert baseline["patch_hash"] != changed["patch_hash"]


def test_apply_envelope_rejects_a_tampered_plan_or_hash():
    result = _compile(_request())
    envelope = copy.deepcopy(result["apply_request"])
    envelope["plan"]["create_nodes"][0]["values"]["model.duration"] = 8

    with pytest.raises(ValidationError, match="patch_hash"):
        ApplyGraphPatchRequest.model_validate(envelope)


def test_patch_hash_normalizes_only_equivalent_integral_float_spellings():
    result = _compile(_request())
    plan_payload = copy.deepcopy(result["plan"])
    combine = next(
        item for item in plan_payload["create_nodes"] if item["alias"] == "video_combine"
    )
    combine["values"]["frame_rate"] = 30.0
    plan = GraphPatchPlan.model_validate(plan_payload)
    catalog_hash = _request().expected_catalog_hash
    patch_hash = graph_patch_hash(plan, catalog_hash)

    equivalent = plan.model_dump(mode="json")
    equivalent_combine = next(
        item for item in equivalent["create_nodes"] if item["alias"] == "video_combine"
    )
    equivalent_combine["values"]["frame_rate"] = 30
    validated = ApplyGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-number-normalization-0001",
            "expected_catalog_hash": catalog_hash,
            "patch_hash": patch_hash,
            "plan": equivalent,
        }
    )
    assert graph_patch_hash(validated.plan, catalog_hash) == patch_hash

    changed = copy.deepcopy(equivalent)
    changed_combine = next(
        item for item in changed["create_nodes"] if item["alias"] == "video_combine"
    )
    changed_combine["values"]["frame_rate"] = 30.5
    with pytest.raises(ValidationError, match="patch_hash"):
        ApplyGraphPatchRequest.model_validate(
            {
                "application_id": "graph-patch-number-normalization-0001",
                "expected_catalog_hash": catalog_hash,
                "patch_hash": patch_hash,
                "plan": changed,
            }
        )


@pytest.mark.parametrize("bad", [True, "1", 1.0, -1])
def test_all_external_slot_indexes_are_strict_nonnegative_integers(bad):
    payload = _seedance_edges()[0]["target"]
    payload = {**payload, "input_index": bad}

    with pytest.raises(ValidationError):
        GraphPatchTargetEndpoint.model_validate(payload)


def test_target_index_domains_require_socket_only_for_existing_slot_mode():
    slot = _seedance_edges()[0]["target"]
    with pytest.raises(ValidationError, match="socket_index"):
        GraphPatchTargetEndpoint.model_validate({**slot, "socket_index": None})

    widget = _seedance_edges()[-1]["target"]
    with pytest.raises(ValidationError, match="socket_index=null"):
        GraphPatchTargetEndpoint.model_validate({**widget, "socket_index": 1})


def test_widget_target_requires_explicit_conversion_mode():
    request = _request()
    fps = next(edge for edge in request.add_edges if edge.source.output == "fps")
    fps.target.mode = "slot"
    fps.target.socket_index = 1
    result = _compile(request)

    assert result["valid"] is False
    assert "target_requires_widget_conversion" in _codes(result)


def test_unknown_new_reference_is_rejected_without_crashing_final_graph_math():
    payload = _request().model_dump(mode="json")
    payload["add_edges"][0]["source"]["ref"] = {"alias": "not_created"}
    request = PlanGraphPatchRequest.model_validate(payload)
    result = _compile(request)

    assert result["valid"] is False
    assert "unknown_new_ref" in _codes(result)
    assert "dangling_final_edge" in _codes(result)


def test_duplicate_and_occupied_targets_are_rejected():
    request = _request()
    duplicate = copy.deepcopy(request.add_edges[3])
    request.add_edges.append(duplicate)
    competing = copy.deepcopy(request.add_edges[4])
    competing.target = copy.deepcopy(request.add_edges[3].target)
    request.add_edges.append(competing)
    result = _compile(request)

    assert result["valid"] is False
    assert "duplicate_add_edge" in _codes(result)
    assert "occupied_input" in _codes(result)


def test_self_loop_and_multi_node_cycle_are_rejected():
    request = _request()
    request.add_edges.append(
        GraphPatchEdge.model_validate(
            _edge(
                _new("save_video"),
                0,
                "video",
                "VIDEO",
                _new("seedance_reference"),
                8,
                "model.reference_videos.video_1",
                socket_index=2,
            )
        )
    )
    request.add_edges.append(
        GraphPatchEdge.model_validate(
            _edge(
                _new("save_video"),
                0,
                "video",
                "VIDEO",
                _new("save_video"),
                0,
                "video",
                socket_index=0,
            )
        )
    )
    result = _compile(request)

    assert result["valid"] is False
    assert "self_loop" in _codes(result)
    assert "graph_cycle" in _codes(result)


def test_schema_name_index_and_type_facts_are_all_validated():
    request = _request()
    request.add_edges[0].target.input_index = 9
    request.add_edges[1].source.output = "wrong"
    request.add_edges[2].target.type = "IMAGE"
    result = _compile(request)

    assert result["valid"] is False
    assert "target_slot_mismatch" in _codes(result)
    assert "source_slot_mismatch" in _codes(result)
    assert "incompatible_edge_types" in _codes(result)


def test_stale_existing_and_created_schema_hashes_are_rejected():
    request = _request()
    request.assertions.nodes[0].schema_hash = "0" * 64
    request.create_nodes[0].schema_hash = "1" * 64
    result = _compile(request)

    assert result["valid"] is False
    assert "asserted_node_schema_mismatch" in _codes(result)
    assert "create_schema_mismatch" in _codes(result)


def test_remove_node_requires_every_incident_edge_and_explicit_edge_removal():
    catalog = _catalog()
    graph = _graph()
    incident = next(edge for edge in graph.edges if edge.target_node_id == 61)
    edge = _edge(
        _existing(incident.source_node_id),
        incident.source_output_index,
        incident.source_output,
        incident.type,
        _existing(incident.target_node_id),
        incident.target_input_index,
        incident.target_input,
        socket_index=incident.target_input_index,
    )
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-remove-0001",
            "expected_workflow_identity": "fl-mcp-workflow-fixture-0001",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": graph.model_dump(mode="json"),
            "assertions": {
                "nodes": [
                    _assertion(60, "WaveletColorFix", catalog),
                    _assertion(61, "SaveImage", catalog),
                ],
                "edges": [edge],
            },
            "remove_edges": [edge],
            "remove_nodes": [
                {
                    "ref": _existing(61),
                    "node_type": "SaveImage",
                    "schema_hash": node_schema_hash("SaveImage", catalog["SaveImage"]),
                    "expected_incident_edges": [],
                }
            ],
        }
    )
    result = _compile(request)

    assert result["valid"] is False
    assert "undeclared_incident_edge" in _codes(result)

    request.remove_nodes[0].expected_incident_edges = [GraphPatchEdge.model_validate(edge)]
    valid = _compile(request)
    assert valid["valid"] is True, valid["issues"]
    assert valid["plan"]["expected_delta"]["final_node_count"] == 8
    assert valid["plan"]["expected_delta"]["final_edge_count"] == 12


def test_remove_node_rejects_incident_edge_not_listed_in_remove_edges():
    catalog = _catalog()
    graph = _graph()
    incident = next(edge for edge in graph.edges if edge.target_node_id == 61)
    edge = _edge(
        _existing(60), 0, "image", "IMAGE", _existing(61), 0, "images", socket_index=0
    )
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-remove-0002",
            "expected_workflow_identity": "fl-mcp-workflow-fixture-0001",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": graph.model_dump(mode="json"),
            "assertions": {
                "nodes": [
                    _assertion(60, "WaveletColorFix", catalog),
                    _assertion(61, "SaveImage", catalog),
                ],
                "edges": [edge],
            },
            "remove_nodes": [
                {
                    "ref": _existing(61),
                    "node_type": "SaveImage",
                    "schema_hash": node_schema_hash("SaveImage", catalog["SaveImage"]),
                    "expected_incident_edges": [edge],
                }
            ],
        }
    )
    result = _compile(request)

    assert incident.target_node_id == 61
    assert "incident_edge_not_removed" in _codes(result)


def test_update_operation_is_schema_pinned_and_counted_without_graph_mutation():
    catalog = _catalog()
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-update-0001",
            "expected_workflow_identity": "fl-mcp-workflow-fixture-0001",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": _graph().model_dump(mode="json"),
            "assertions": {"nodes": [_assertion(60, "WaveletColorFix", catalog)]},
            "update_nodes": [
                {
                    "ref": _existing(60),
                    "node_type": "WaveletColorFix",
                    "schema_hash": node_schema_hash(
                        "WaveletColorFix", catalog["WaveletColorFix"]
                    ),
                    "expected_values": {"align_method": "wavelet"},
                    "set_values": {"align_method": "adain"},
                    "layout_hint": {"x": 2400, "y": -300},
                }
            ],
        }
    )
    result = _compile(request)

    assert result["valid"] is True, result["issues"]
    assert result["plan"]["expected_delta"]["updated_node_count"] == 1
    assert result["plan"]["expected_delta"]["final_node_count"] == 9
    assert result["plan"]["expected_delta"]["final_edge_count"] == 13


def test_attachment_is_exact_safe_relative_and_conflict_checked():
    catalog = _catalog()
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-attachment-0001",
            "expected_workflow_identity": "fl-mcp-workflow-fixture-0001",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": _graph().model_dump(mode="json"),
            "assertions": {"nodes": [_assertion(48, "LoadImage", catalog)]},
            "attachments": [
                {
                    "ref": _existing(48),
                    "input_index": 0,
                    "input": "image",
                    "type": "COMBO",
                    "filename": "image.png",
                    "subfolder": "ren-chat/session",
                    "file_type": "input",
                    "size_bytes": 123,
                    "sha256": "e" * 64,
                }
            ],
        }
    )
    result = _compile(request)
    assert result["valid"] is True, result["issues"]
    reconstructed = graph_patch_request_from_apply(
        result["apply_request"],
        _graph(),
    )
    recompiled = _compile(reconstructed)
    assert recompiled["patch_hash"] == result["patch_hash"]
    assert recompiled["plan"] == result["plan"]

    payload = request.model_dump(mode="json")
    payload["attachments"][0]["filename"] = "../secret.png"
    with pytest.raises(ValidationError, match="safe basename"):
        PlanGraphPatchRequest.model_validate(payload)

    for field, invalid in (
        ("filename", "nested/secret.png"),
        ("filename", "/absolute.png"),
        ("subfolder", "output"),
        ("subfolder", "ren-chat/../other"),
        ("subfolder", "/ren-chat/session"),
        ("file_type", "output"),
        ("file_type", "temp"),
    ):
        invalid_payload = request.model_dump(mode="json")
        invalid_payload["attachments"][0][field] = invalid
        with pytest.raises(ValidationError):
            PlanGraphPatchRequest.model_validate(invalid_payload)

    too_many_payload = request.model_dump(mode="json")
    too_many_payload["attachments"] = [
        copy.deepcopy(too_many_payload["attachments"][0]) for _ in range(9)
    ]
    with pytest.raises(ValidationError, match="at most 8 items"):
        PlanGraphPatchRequest.model_validate(too_many_payload)


def test_catalog_change_is_a_hard_safety_stop():
    request = _request()
    result = compile_graph_patch(
        request,
        _catalog(),
        catalog_hash="f" * 64,
        source="http://127.0.0.1:8188/object_info",
    )

    assert result["valid"] is False
    assert "catalog_changed" in _codes(result)


def test_baseline_edge_separates_schema_declaration_and_live_socket_indexes():
    catalog = {
        "ImageSource": {
            "input": {},
            "output": ["IMAGE"],
            "output_name": ["image"],
        },
        "WidgetBeforeSocket": {
            "input": {
                "required": {
                    "strength": ["FLOAT", {"default": 0.5}],
                    "image": ["IMAGE", {}],
                }
            },
            "output": [],
        },
        "NoInput": {"input": {}, "output": []},
    }
    graph = NormalizedGraphSnapshot.model_validate(
        {
            "schema": "fl-mcp.normalized-workflow-graph.v1",
            "complete": True,
            "nodes": [
                {"node_id": 1, "node_type": "ImageSource"},
                {"node_id": 2, "node_type": "WidgetBeforeSocket"},
            ],
            "outputs": [
                {
                    "node_id": 1,
                    "output": "image",
                    "output_index": 0,
                    "type": "IMAGE",
                }
            ],
            "edges": [
                {
                    "source_node_id": 1,
                    "source_output": "image",
                    "source_output_index": 0,
                    "target_node_id": 2,
                    "target_input": "image",
                    "target_input_index": 0,
                    "type": "IMAGE",
                }
            ],
        }
    )
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-index-domains",
            "expected_workflow_identity": "workflow-index-domains",
            "expected_graph_hash": "b" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": graph.model_dump(mode="json"),
            "create_nodes": [
                {
                    "alias": "no_input",
                    "node_type": "NoInput",
                    "schema_hash": node_schema_hash("NoInput", catalog["NoInput"]),
                    "values": {},
                }
            ],
        }
    )
    result = compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    target = result["expected_final"]["edges"][0]["target"]
    assert target["input_index"] == 1
    assert target["socket_index"] == 0
    assert target["input"] == "image"


def _dynamic_nano_baseline_fixture():
    catalog = {
        "ImageSource": {
            "input": {},
            "output": ["IMAGE"],
            "output_name": ["image"],
        },
        "DynamicNanoTarget": {
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
                                        "required": {
                                            "quality": [["fast", "high"]],
                                        }
                                    },
                                },
                            ]
                        },
                    ],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        },
        "ImageSink": {
            "input": {"required": {"images": ["IMAGE", {}]}},
            "output": [],
        },
    }
    graph = NormalizedGraphSnapshot.model_validate(
        {
            "schema": "fl-mcp.normalized-workflow-graph.v1",
            "complete": True,
            "nodes": [
                {"node_id": 1, "node_type": "ImageSource"},
                {
                    "node_id": 2,
                    "node_type": "DynamicNanoTarget",
                    "widget_values": ["prompt", "nano", "16:9", "1K", "MINIMAL"],
                },
            ],
            "outputs": [
                {
                    "node_id": 1,
                    "output": "image",
                    "output_index": 0,
                    "type": "IMAGE",
                },
                {
                    "node_id": 2,
                    "output": "IMAGE",
                    "output_index": 0,
                    "type": "IMAGE",
                },
            ],
            "edges": [
                {
                    "source_node_id": 1,
                    "source_output": "image",
                    "source_output_index": 0,
                    "target_node_id": 2,
                    "target_input": "model.images.image_1",
                    "target_input_index": 5,
                    "type": "IMAGE",
                }
            ],
        }
    )
    return catalog, graph


def _dynamic_nano_baseline_request(catalog, graph):
    return PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-dynamic-live-index",
            "expected_workflow_identity": "workflow-dynamic-live-index",
            "expected_graph_hash": "c" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": graph.model_dump(mode="json"),
            "assertions": {
                "nodes": [_assertion(2, "DynamicNanoTarget", catalog)],
            },
            "create_nodes": [
                {
                    "alias": "image_sink",
                    "node_type": "ImageSink",
                    "schema_hash": node_schema_hash(
                        "ImageSink",
                        catalog["ImageSink"],
                    ),
                    "values": {},
                }
            ],
            "add_edges": [
                _edge(
                    _existing(2),
                    0,
                    "IMAGE",
                    "IMAGE",
                    _new("image_sink"),
                    0,
                    "images",
                    socket_index=0,
                )
            ],
        }
    )


def test_existing_dynamic_baseline_preserves_live_socket_not_schema_projection():
    catalog, graph = _dynamic_nano_baseline_fixture()
    request = _dynamic_nano_baseline_request(catalog, graph)

    result = compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    retained = next(
        edge["target"]
        for edge in result["expected_final"]["edges"]
        if edge["target"]["ref"] == {"node_id": 2}
    )
    assert retained == {
        "ref": {"node_id": 2},
        "input_index": 5,
        "occurrence_index": 0,
        "socket_index": 5,
        "input": "model.images.image_1",
        "type": "IMAGE",
        "mode": "slot",
    }

    reconstructed = graph_patch_request_from_apply(result["apply_request"], graph)
    recompiled = compile_graph_patch(
        reconstructed,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )
    assert recompiled["valid"] is True, recompiled["issues"]
    assert recompiled["patch_hash"] == result["patch_hash"]
    assert recompiled["plan"] == result["plan"]


def test_static_dotted_baseline_cannot_use_dynamic_socket_projection_exception():
    catalog, graph = _dynamic_nano_baseline_fixture()
    catalog["StaticDottedTarget"] = {
        "input": {
            "required": {
                "prompt": ["STRING", {"default": ""}],
                "model": [["nano"]],
                "aspect_ratio": [["16:9"]],
                "resolution": [["1K"]],
                "thinking_level": [["MINIMAL"]],
                "model.images.image_1": ["IMAGE", {}],
            }
        },
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
    }
    graph.nodes[1].node_type = "StaticDottedTarget"
    request = _dynamic_nano_baseline_request(catalog, graph)
    request.assertions.nodes[0].node_type = "StaticDottedTarget"
    request.assertions.nodes[0].schema_hash = node_schema_hash(
        "StaticDottedTarget",
        catalog["StaticDottedTarget"],
    )

    result = compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is False
    assert "baseline_target_slot_unresolved" in _codes(result)
    assert "target_socket_index_mismatch" in _codes(result)


@pytest.mark.parametrize("scenario", ["wrong_path", "wrong_type", "inactive", "ambiguous"])
def test_existing_dynamic_baseline_still_rejects_unproven_endpoint(scenario):
    catalog, graph = _dynamic_nano_baseline_fixture()
    if scenario == "wrong_path":
        graph.edges[0].target_input = "model.images.image_99"
    elif scenario == "wrong_type":
        catalog["ImageSource"]["output"] = ["MASK"]
        catalog["ImageSource"]["output_name"] = ["mask"]
        graph.outputs[0].output = "mask"
        graph.outputs[0].type = "MASK"
        graph.edges[0].source_output = "mask"
        graph.edges[0].type = "MASK"
    elif scenario == "inactive":
        graph.nodes[1].widget_values[1] = "text-only"
    else:
        options = catalog["DynamicNanoTarget"]["input"]["required"]["model"][1][
            "options"
        ]
        options[1]["inputs"] = copy.deepcopy(options[0]["inputs"])
        graph.nodes[1].widget_values = []
    request = _dynamic_nano_baseline_request(catalog, graph)

    result = compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is False
    assert "baseline_target_slot_unresolved" in _codes(result)


def test_baseline_edge_preserves_an_already_converted_widget_socket():
    catalog = {
        "FloatSource": {
            "input": {},
            "output": ["FLOAT"],
            "output_name": ["value"],
        },
        "ConvertedWidgetTarget": {
            "input": {
                "required": {
                    "strength": ["FLOAT", {"default": 0.5}],
                    "image": ["IMAGE", {}],
                }
            },
            "output": [],
        },
        "NoInput": {"input": {}, "output": []},
    }
    graph = NormalizedGraphSnapshot.model_validate(
        {
            "schema": "fl-mcp.normalized-workflow-graph.v1",
            "complete": True,
            "nodes": [
                {"node_id": 1, "node_type": "FloatSource"},
                {"node_id": 2, "node_type": "ConvertedWidgetTarget"},
            ],
            "outputs": [
                {
                    "node_id": 1,
                    "output": "value",
                    "output_index": 0,
                    "type": "FLOAT",
                }
            ],
            "edges": [
                {
                    "source_node_id": 1,
                    "source_output": "value",
                    "source_output_index": 0,
                    "target_node_id": 2,
                    "target_input": "strength",
                    "target_input_index": 1,
                    "type": "FLOAT",
                }
            ],
        }
    )
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-converted-widget",
            "expected_workflow_identity": "workflow-converted-widget",
            "expected_graph_hash": "c" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": graph.model_dump(mode="json"),
            "create_nodes": [
                {
                    "alias": "no_input",
                    "node_type": "NoInput",
                    "schema_hash": node_schema_hash("NoInput", catalog["NoInput"]),
                    "values": {},
                }
            ],
        }
    )
    result = compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    target = result["expected_final"]["edges"][0]["target"]
    assert target == {
        "ref": {"node_id": 2},
        "input_index": 0,
        "occurrence_index": 0,
        "socket_index": 1,
        "input": "strength",
        "type": "FLOAT",
        "mode": "slot",
    }


def _new_node_patch(
    *,
    creates,
    edges,
):
    catalog = _catalog()
    return PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-capability-0001",
            "expected_workflow_identity": "fl-mcp-workflow-fixture-0001",
            "expected_graph_hash": "a" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": _graph().model_dump(mode="json"),
            "create_nodes": [
                {
                    "alias": alias,
                    "node_type": node_type,
                    "schema_hash": node_schema_hash(node_type, catalog[node_type]),
                    "values": {},
                }
                for alias, node_type in creates
            ],
            "add_edges": edges,
        }
    )


def test_legacy_combo_widget_conversion_uses_shared_capability_classification():
    request = _new_node_patch(
        creates=[("combo_source", "ComboSource"), ("combo_target", "ComboTarget")],
        edges=[
            _edge(
                _new("combo_source"),
                0,
                "mode",
                "COMBO",
                _new("combo_target"),
                0,
                "mode",
                socket_index=None,
                mode="convert_widget",
            )
        ],
    )

    result = _compile(request)
    assert result["valid"] is True, result["issues"]


def test_matchtype_edge_binds_to_the_explicit_concrete_edge_type():
    request = _new_node_patch(
        creates=[("image_source", "ImageSource"), ("match_target", "MatchTarget")],
        edges=[
            _edge(
                _new("image_source"),
                0,
                "image",
                "IMAGE",
                _new("match_target"),
                0,
                "payload",
                socket_index=0,
            )
        ],
    )

    result = _compile(request)
    assert result["valid"] is True, result["issues"]


@pytest.mark.parametrize(
    ("node_type", "missing_input"),
    [
        ("MatchTarget", "payload"),
        ("AutogrowTarget", "items.item_1"),
    ],
)
def test_capability_required_inputs_cannot_evade_connection_validation(
    node_type,
    missing_input,
):
    request = _new_node_patch(
        creates=[("required_target", node_type)],
        edges=[],
    )

    result = _compile(request)
    assert result["valid"] is False
    missing = [
        issue
        for issue in result["issues"]
        if issue["code"] == "missing_required_connection"
    ]
    assert any(issue["path"].endswith(missing_input) for issue in missing)


def test_all_comfy_list_cardinality_pairings_are_native_graph_edges():
    list_to_scalar = _new_node_patch(
        creates=[
            ("list_source", "ListImageSource"),
            ("scalar_target", "ScalarImageTarget"),
        ],
        edges=[
            _edge(
                _new("list_source"),
                0,
                "images",
                "IMAGE",
                _new("scalar_target"),
                0,
                "images",
                socket_index=0,
            )
        ],
    )
    list_to_scalar_result = _compile(list_to_scalar)
    assert list_to_scalar_result["valid"] is True, list_to_scalar_result["issues"]

    supported = _new_node_patch(
        creates=[
            ("list_source", "ListImageSource"),
            ("list_target", "ListImageTarget"),
        ],
        edges=[
            _edge(
                _new("list_source"),
                0,
                "images",
                "IMAGE",
                _new("list_target"),
                0,
                "images",
                socket_index=0,
            )
        ],
    )
    supported_result = _compile(supported)
    assert supported_result["valid"] is True, supported_result["issues"]


def test_attachment_cannot_target_a_node_removed_by_the_same_patch():
    catalog = _catalog()
    graph = NormalizedGraphSnapshot.model_validate(
        {
            "schema": "fl-mcp.normalized-workflow-graph.v1",
            "complete": True,
            "nodes": [{"node_id": 48, "node_type": "LoadImage"}],
            "outputs": [
                {
                    "node_id": 48,
                    "output": "IMAGE",
                    "output_index": 0,
                    "type": "IMAGE",
                },
                {
                    "node_id": 48,
                    "output": "MASK",
                    "output_index": 1,
                    "type": "MASK",
                },
            ],
            "edges": [],
        }
    )
    request = PlanGraphPatchRequest.model_validate(
        {
            "application_id": "graph-patch-remove-attachment",
            "expected_workflow_identity": "workflow-remove-attachment",
            "expected_graph_hash": "d" * 64,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "graph": graph.model_dump(mode="json"),
            "assertions": {"nodes": [_assertion(48, "LoadImage", catalog)]},
            "remove_nodes": [
                {
                    "ref": _existing(48),
                    "node_type": "LoadImage",
                    "schema_hash": node_schema_hash("LoadImage", catalog["LoadImage"]),
                    "expected_incident_edges": [],
                }
            ],
            "attachments": [
                {
                    "ref": _existing(48),
                    "input_index": 0,
                    "input": "image",
                    "type": "COMBO",
                    "filename": "replacement.png",
                    "subfolder": "ren-chat/session",
                    "file_type": "input",
                    "size_bytes": 123,
                    "sha256": "e" * 64,
                }
            ],
        }
    )

    result = _compile(request)
    assert result["valid"] is False
    assert "attachment_references_removed_node" in _codes(result)


def test_apply_envelope_round_trips_back_to_the_same_canonical_planner_request():
    planned = _compile(_request())
    reconstructed = graph_patch_request_from_apply(
        planned["apply_request"],
        _graph(),
    )
    recompiled = _compile(reconstructed)

    assert recompiled["valid"] is True, recompiled["issues"]
    assert recompiled["patch_hash"] == planned["patch_hash"]
    assert recompiled["plan"] == planned["plan"]


def test_unrelated_unloaded_frontend_branch_is_preserved_opaquely():
    """An independent edit must not require schemas for untouched UI artifacts."""

    catalog = _catalog()
    graph = NormalizedGraphSnapshot.model_validate(
        {
            "schema": "fl-mcp.normalized-workflow-graph.v1",
            "complete": True,
            "nodes": [
                {"node_id": 1, "node_type": "PrimitiveNode"},
                {"node_id": 2, "node_type": "MissingCustomNode"},
            ],
            "outputs": [],
            "edges": [
                {
                    "source_node_id": 1,
                    "source_output_index": 0,
                    "source_output": "value",
                    "target_node_id": 2,
                    "target_input_index": 0,
                    "target_input": "value",
                    "type": "*",
                }
            ],
        }
    )
    request = PlanGraphPatchRequest(
        application_id="opaque-branch-create-only",
        expected_workflow_identity="workflow-opaque-branch",
        expected_graph_hash="a" * 64,
        expected_catalog_hash=catalog_contract_hash(catalog),
        graph=graph,
        create_nodes=[
            {
                "alias": "independent_source",
                "node_type": "ImageSource",
                "schema_hash": node_schema_hash("ImageSource", catalog["ImageSource"]),
                "values": {},
            }
        ],
    )

    result = _compile(request)

    assert result["valid"] is True, result["issues"]
    assert len(result["expected_final"]["nodes"]) == 3
    assert len(result["expected_final"]["edges"]) == 1
    assert not any("baseline_" in issue["code"] for issue in result["issues"])


@pytest.mark.parametrize("bad", [True, "0", 0.0, -1])
@pytest.mark.parametrize(
    "field_path",
    [
        ("plan", "add_edges", 0, "source", "output_index"),
        ("plan", "add_edges", 0, "target", "input_index"),
        ("plan", "add_edges", 0, "target", "occurrence_index"),
        ("plan", "add_edges", 0, "target", "socket_index"),
        ("plan", "expected_delta", "added_edge_count"),
    ],
)
def test_apply_envelope_rejects_tampered_non_strict_slot_indexes(field_path, bad):
    result = _compile(_request())
    envelope = copy.deepcopy(result["apply_request"])
    target = envelope
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = bad

    with pytest.raises(ValidationError):
        ApplyGraphPatchRequest.model_validate(envelope)
