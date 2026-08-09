from node_library import catalog_contract_hash
from workflow_compiler import (
    CompileWorkflowSpecRequest,
    WorkflowSpecNode,
    _canonicalize_node_values,
    _dynamic_selector_defaults,
    _resolve_runtime_name,
    compile_workflow_spec,
)


def _catalog():
    return {
        "LoadImage": {
            "display_name": "Load Image",
            "category": "image",
            "input": {"required": {"image": [["example.png"], {}]}},
            "output": ["IMAGE", "MASK"],
            "output_name": ["IMAGE", "MASK"],
            "python_module": "nodes",
        },
        "GeminiNanoBanana2V2": {
            "display_name": "Nano Banana 2",
            "category": "partner/image/Gemini",
            "input": {
                "required": {
                    "prompt": ["STRING", {"default": ""}],
                    "model": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {
                            "options": [
                                {
                                    "key": "Nano Banana 2 (Gemini 3.1 Flash Image)",
                                    "inputs": {
                                        "required": {
                                            "aspect_ratio": [
                                                "COMBO",
                                                {"options": ["auto", "16:9"], "default": "auto"},
                                            ],
                                            "resolution": [
                                                "COMBO",
                                                {"options": ["1K", "2K"], "default": "1K"},
                                            ],
                                            "thinking_level": [
                                                "COMBO",
                                                {"options": ["MINIMAL", "HIGH"], "default": "MINIMAL"},
                                            ],
                                            "images": [
                                                "COMFY_AUTOGROW_V3",
                                                {
                                                    "template": {
                                                        "input": {"required": {"image": ["IMAGE"]}},
                                                        "names": ["image_1", "image_2"],
                                                        "min": 0,
                                                    }
                                                },
                                            ],
                                        },
                                    },
                                }
                            ]
                        },
                    ],
                    "seed": ["INT", {"default": 42}],
                    "response_modalities": [["IMAGE", "IMAGE+TEXT"], {"default": "IMAGE"}],
                },
                "optional": {
                    "system_prompt": ["STRING", {"default": "system"}],
                    "temperature": ["FLOAT", {"default": 1.0}],
                    "top_p": ["FLOAT", {"default": 0.95}],
                },
            },
            "output": ["IMAGE", "STRING", "IMAGE"],
            "output_name": ["IMAGE", "STRING", "thought_image"],
            "api_node": True,
            "python_module": "comfy_api_nodes.nodes_gemini",
        },
        "SaveImage": {
            "display_name": "Save Image",
            "category": "image",
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                }
            },
            "output": [],
            "output_name": [],
            "output_node": True,
            "python_module": "nodes",
        },
    }


def _request():
    return CompileWorkflowSpecRequest.model_validate(
        {
            "application_id": "ren-partner-test-compiler",
            "nodes": [
                {
                    "alias": "main_image",
                    "capability": "load main image",
                    "requested_node_type": "LoadImage",
                    "required_output_types": ["IMAGE"],
                },
                {
                    "alias": "reference_image",
                    "capability": "load reference image",
                    "requested_node_type": "LoadImage",
                    "required_output_types": ["IMAGE"],
                },
                {
                    "alias": "generate",
                    "capability": "Nano Banana 2 image editing",
                    "requested_node_type": "GeminiNanoBanana2V2",
                    "required_input_types": ["IMAGE"],
                    "required_output_types": ["IMAGE"],
                    "values": {
                        "prompt": "Preserve image 1 and use image 2 as atmosphere.",
                        "model": "Nano Banana 2 (Gemini 3.1 Flash Image)",
                        "aspect_ratio": "16:9",
                        "resolution": "1K",
                        "thinking_level": "MINIMAL",
                    },
                },
                {
                    "alias": "output",
                    "capability": "save final image",
                    "requested_node_type": "SaveImage",
                    "required_input_types": ["IMAGE"],
                    "values": {"filename_prefix": "ren-partner-test"},
                },
            ],
            "connections": [
                {
                    "source_alias": "main_image",
                    "source_output": "IMAGE",
                    "target_alias": "generate",
                    "target_input": "image_1",
                },
                {
                    "source_alias": "reference_image",
                    "source_output": "IMAGE",
                    "target_alias": "generate",
                    "target_input": "image_2",
                },
                {
                    "source_alias": "generate",
                    "source_output": "IMAGE",
                    "target_alias": "output",
                    "target_input": "images",
                },
            ],
            "attachments": [
                {
                    "node_alias": "main_image",
                    "image": {
                        "filename": "main.png",
                        "subfolder": "ren-chat/session",
                        "type": "input",
                    },
                },
                {
                    "node_alias": "reference_image",
                    "image": {
                        "filename": "reference.png",
                        "subfolder": "ren-chat/session",
                        "type": "input",
                    },
                },
            ],
        }
    )


def test_dynamic_selector_is_inferred_from_requested_non_default_socket():
    node_info = {
        "input": {
            "required": {
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "image",
                                "inputs": {
                                    "required": {"quality": ["INT", {"default": 1}]}
                                },
                            },
                            {
                                "key": "video",
                                "inputs": {
                                    "required": {
                                        "duration": ["INT", {"default": 5}],
                                        "reference": ["VIDEO"],
                                    }
                                },
                            },
                        ]
                    },
                ]
            }
        }
    }

    assert _dynamic_selector_defaults(
        node_info,
        {"duration": 7},
        {"reference"},
    ) == {"model": "video"}


def test_connected_convertible_widget_does_not_receive_its_schema_default():
    node = WorkflowSpecNode(
        alias="video_combine",
        capability="combine video frames",
        requested_node_type="VHS_VideoCombine",
        values={"filename_prefix": "AnimateDiff"},
    )
    info = {
        "input": {
            "required": {
                "images": ["IMAGE"],
                "frame_rate": ["FLOAT", {"default": 8.0}],
                "filename_prefix": ["STRING", {"default": "AnimateDiff"}],
            }
        }
    }

    values, _, issues = _canonicalize_node_values(
        node,
        info,
        set(),
        {"images", "frame_rate"},
    )

    assert issues == []
    assert values == {"filename_prefix": "AnimateDiff"}


def test_unique_semantic_widget_leaf_aliases_use_exact_loaded_names():
    wavelet = WorkflowSpecNode(
        alias="wavelet",
        capability="wavelet color alignment",
        requested_node_type="WaveletColorFix",
        values={"alignment": "wavelet"},
    )
    wavelet_info = {
        "input": {
            "required": {
                "target_image": ["IMAGE"],
                "source_image": ["IMAGE"],
                "align_method": [["wavelet", "adain"], {"default": "wavelet"}],
            }
        }
    }

    values, _, issues = _canonicalize_node_values(
        wavelet,
        wavelet_info,
        set(),
        {"target_image", "source_image"},
    )

    assert issues == []
    assert values == {"align_method": "wavelet"}

    seedance = WorkflowSpecNode(
        alias="seedance",
        capability="reference video",
        requested_node_type="ReferenceVideo",
        values={
            "model": "Seedance 2.0",
            "aspect_ratio": "16:9",
        },
    )
    seedance_info = {
        "input": {
            "required": {
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [{
                            "key": "Seedance 2.0",
                            "inputs": {
                                "required": {
                                    "ratio": [
                                        ["adaptive", "16:9"],
                                        {"default": "adaptive"},
                                    ]
                                }
                            },
                        }]
                    },
                ]
            }
        }
    }

    values, _, issues = _canonicalize_node_values(
        seedance,
        seedance_info,
        set(),
        set(),
    )

    assert issues == []
    assert values == {
        "model": "Seedance 2.0",
        "model.ratio": "16:9",
    }


def test_semantic_widget_aliases_keep_exact_precedence_and_ambiguity_closed():
    exact_node = WorkflowSpecNode(
        alias="exact",
        capability="alignment utility",
        requested_node_type="ExactAlignment",
        values={"alignment": "wavelet"},
    )
    exact_info = {
        "input": {
            "required": {
                "alignment": [["wavelet", "adain"], {"default": "wavelet"}],
                "align_method": [["wavelet", "adain"], {"default": "adain"}],
            }
        }
    }
    values, _, issues = _canonicalize_node_values(
        exact_node,
        exact_info,
        set(),
        set(),
    )
    assert issues == []
    assert values["alignment"] == "wavelet"
    assert values["align_method"] == "adain"

    ambiguous_node = exact_node.model_copy(
        update={"alias": "ambiguous", "values": {"aligning": "wavelet"}}
    )
    ambiguous_info = {
        "input": {
            "required": {
                "align_method": [["wavelet", "adain"], {"default": "wavelet"}],
                "align_mode": [["wavelet", "adain"], {"default": "wavelet"}],
            }
        }
    }
    reversed_info = {
        "input": {
            "required": dict(reversed(ambiguous_info["input"]["required"].items()))
        }
    }

    for info in (ambiguous_info, reversed_info):
        values, _, issues = _canonicalize_node_values(
            ambiguous_node,
            info,
            set(),
            set(),
        )
        assert values == {
            "align_method": "wavelet",
            "align_mode": "wavelet",
        }
        assert [item["code"] for item in issues] == ["ambiguous_widget"]

    resolved, issues = _resolve_runtime_name(
        "aspect_ratio",
        ["model.ratio", "output.ratio"],
        path="nodes.generator.values.aspect_ratio",
        kind="widget",
        allow_semantic_widget_alias=True,
    )
    assert resolved is None
    assert [item["code"] for item in issues] == ["ambiguous_widget"]


def test_semantic_compiler_produces_one_exact_attachment_aware_partner_plan():
    catalog = _catalog()
    catalog_hash = catalog_contract_hash(catalog)
    request = _request()
    result = compile_workflow_spec(
        request,
        catalog,
        catalog_hash=catalog_hash,
        source="http://127.0.0.1:8188/object_info",
        validated_attachment_values={
            ("main_image", "image"): "ren-chat/session/main.png",
            ("reference_image", "image"): "ren-chat/session/reference.png",
        },
    )

    assert result["valid"] is True
    assert result["error_count"] == 0
    assert any(
        item["code"] == "partner_authentication_cost_privacy_review_required"
        for item in result["issues"]
    )
    assert result["selected_node_types"]["generate"] == "GeminiNanoBanana2V2"
    generator = next(node for node in result["plan"]["nodes"] if node["alias"] == "generate")
    assert generator["values"]["model.aspect_ratio"] == "16:9"
    assert generator["values"]["model.resolution"] == "1K"
    assert generator["values"]["model.thinking_level"] == "MINIMAL"
    assert generator["values"]["system_prompt"] == "system"
    assert generator["values"]["temperature"] == 1.0
    assert generator["values"]["top_p"] == 0.95
    connections = {
        (edge["source_alias"], edge["target_alias"]): edge["target_input"]
        for edge in result["plan"]["connections"]
    }
    assert connections == {
        ("main_image", "generate"): "model.images.image_1",
        ("reference_image", "generate"): "model.images.image_2",
        ("generate", "output"): "images",
    }
    assert len(result["plan"]["attachments"]) == 2
    assert result["partner_review"] == {
        "required": True,
        "nodes": [{"alias": "generate", "node_type": "GeminiNanoBanana2V2"}],
        "authentication": "may_be_required",
        "cost": "may_consume_credits_only_when_executed",
        "privacy": "inputs_may_be_sent_to_the_external_partner_only_when_executed",
        "current_operation_transmits_images": False,
        "web_lookup_required": False,
    }
    assert result["apply_request"]["plan_hash"] == result["plan_hash"]
    assert result["apply_request"]["application_id"] == "ren-partner-test-compiler"
    assert result["apply_request"]["attachments"] == request.model_dump(mode="json")[
        "attachments"
    ]


def test_compiler_defaults_dynamic_model_before_resolving_short_dotted_inputs():
    request_payload = _request().model_dump(mode="json")
    generate = next(
        node for node in request_payload["nodes"] if node["alias"] == "generate"
    )
    generate["values"].pop("model")
    generate.pop("requested_node_type")

    catalog = _catalog()
    catalog["GeminiImage2Node"] = {
        **catalog["GeminiNanoBanana2V2"],
        "display_name": "Gemini Image 2",
        "description": (
            "Nano Banana 2 partner image editing with two reference images, "
            "lighting, and atmosphere"
        ),
    }
    request = CompileWorkflowSpecRequest.model_validate(request_payload)
    result = compile_workflow_spec(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://comfy/object_info",
        validated_attachment_values={
            (binding.node_alias, binding.input_name): binding.image.widget_value()
            for binding in request.attachments
        },
    )

    assert result["valid"] is True
    assert result["selected_node_types"]["generate"] == "GeminiNanoBanana2V2"
    generate_plan = next(
        node for node in result["plan"]["nodes"] if node["alias"] == "generate"
    )
    assert generate_plan["values"]["model"] == (
        "Nano Banana 2 (Gemini 3.1 Flash Image)"
    )
    assert generate_plan["values"]["model.aspect_ratio"] == "16:9"
    assert generate_plan["values"]["model.thinking_level"] == "MINIMAL"


def test_semantic_compiler_fails_closed_on_unknown_short_runtime_name():
    catalog = _catalog()
    request = _request()
    request.nodes[2].values["quality_magic"] = "high"
    result = compile_workflow_spec(
        request,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://127.0.0.1:8188/object_info",
        validated_attachment_values={
            ("main_image", "image"): "ren-chat/session/main.png",
            ("reference_image", "image"): "ren-chat/session/reference.png",
        },
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert any(item["code"] == "unknown_widget" for item in result["issues"])
