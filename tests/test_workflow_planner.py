import pytest
from node_library import catalog_contract_hash
from pydantic import ValidationError
from workflow_planner import PlanWorkflowRequest, compile_workflow_plan


def _catalog():
    return {
        "ImageSource": {
            "display_name": "Image Source",
            "category": "image",
            "input": {"required": {}, "optional": {}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "nodes",
        },
        "ImageProcessor": {
            "display_name": "Image Processor",
            "category": "image",
            "input": {
                "required": {
                    "image": ["IMAGE"],
                    "strength": ["FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0}],
                    "mode": [["soft", "hard"], {"default": "soft"}],
                },
                "optional": {},
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "comfy_extras.nodes_example",
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


def _compile(payload, catalog=None):
    catalog = catalog or _catalog()
    return compile_workflow_plan(
        PlanWorkflowRequest.model_validate(payload),
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://127.0.0.1:8188/object_info",
    )


def _valid_payload():
    return {
        "nodes": [
            {"alias": "source", "node_type": "ImageSource"},
            {
                "alias": "process",
                "node_type": "ImageProcessor",
                "values": {"strength": 0.75, "mode": "soft"},
            },
            {
                "alias": "save",
                "node_type": "SaveImage",
                "values": {"filename_prefix": "planned"},
            },
        ],
        "connections": [
            {
                "source_alias": "source",
                "source_output": "IMAGE",
                "target_alias": "process",
                "target_input": "image",
            },
            {
                "source_alias": "process",
                "source_output": "IMAGE",
                "target_alias": "save",
                "target_input": "images",
            },
        ],
    }


def test_valid_plan_is_catalog_pinned_and_order_independent():
    payload = _valid_payload()
    first = _compile(payload)
    reordered = _compile(
        {
            "nodes": list(reversed(payload["nodes"])),
            "connections": list(reversed(payload["connections"])),
        }
    )

    assert first["valid"] is True
    assert first["plan_hash"] == reordered["plan_hash"]
    assert len(first["plan_hash"]) == 64
    assert first["catalog"]["state"] == "pinned"
    assert first["requirements"]["output_node_aliases"] == ["save"]
    assert [node["alias"] for node in first["plan"]["nodes"]] == [
        "process",
        "save",
        "source",
    ]

    payload["nodes"][1]["values"]["strength"] = 0
    integer_float = _compile(payload)
    payload["nodes"][1]["values"]["strength"] = 0.0
    explicit_float = _compile(payload)
    assert integer_float["plan_hash"] == explicit_float["plan_hash"]


def test_catalog_change_and_schema_errors_block_plan_hash():
    payload = _valid_payload()
    payload["expected_catalog_hash"] = "f" * 64
    payload["nodes"][1]["values"] = {
        "strength": 2.0,
        "mode": "unknown",
        "typo": True,
    }
    result = _compile(payload)
    codes = {issue["code"] for issue in result["issues"]}

    assert result["valid"] is False
    assert result["plan_hash"] is None
    assert {"catalog_changed", "value_above_maximum", "invalid_option", "unknown_widget"} <= codes


def test_active_widget_values_must_be_explicit():
    result = _compile(
        {
            "nodes": [
                {
                    "alias": "process",
                    "node_type": "ImageProcessor",
                    "values": {"strength": 0.5},
                }
            ]
        }
    )

    missing = [issue for issue in result["issues"] if issue["code"] == "missing_widget_value"]
    assert [issue["path"] for issue in missing] == ["nodes[0].values.mode"]


def test_connection_validation_rejects_missing_slots_types_and_cycles():
    catalog = _catalog()
    catalog["MaskSource"] = {
        "input": {"required": {}},
        "output": ["MASK"],
        "output_name": ["MASK"],
        "python_module": "custom_nodes.mask",
    }
    incompatible = _compile(
        {
            "nodes": [
                {"alias": "mask", "node_type": "MaskSource"},
                {
                    "alias": "process",
                    "node_type": "ImageProcessor",
                    "values": {"strength": 0.5, "mode": "soft"},
                },
            ],
            "connections": [
                {
                    "source_alias": "mask",
                    "source_output": "MASK",
                    "target_alias": "process",
                    "target_input": "image",
                }
            ],
        },
        catalog,
    )
    assert {issue["code"] for issue in incompatible["issues"]} == {
        "incompatible_slot_types",
        "missing_required_connection",
    }

    cyclic = _compile(
        {
            "nodes": [
                {
                    "alias": "first",
                    "node_type": "ImageProcessor",
                    "values": {"strength": 0.5, "mode": "soft"},
                },
                {
                    "alias": "second",
                    "node_type": "ImageProcessor",
                    "values": {"strength": 0.5, "mode": "soft"},
                },
            ],
            "connections": [
                {
                    "source_alias": "first",
                    "source_output": "IMAGE",
                    "target_alias": "second",
                    "target_input": "image",
                },
                {
                    "source_alias": "second",
                    "source_output": "IMAGE",
                    "target_alias": "first",
                    "target_input": "image",
                },
            ],
        }
    )
    assert any(issue["code"] == "workflow_cycle" for issue in cyclic["issues"])


def test_duplicate_output_names_require_an_explicit_index():
    catalog = _catalog()
    catalog["DualImage"] = {
        "input": {"required": {}},
        "output": ["IMAGE", "IMAGE"],
        "output_name": ["IMAGE", "IMAGE"],
        "python_module": "custom_nodes.dual",
    }
    payload = {
        "nodes": [
            {"alias": "source", "node_type": "DualImage"},
            {
                "alias": "process",
                "node_type": "ImageProcessor",
                "values": {"strength": 0.5, "mode": "soft"},
            },
        ],
        "connections": [
            {
                "source_alias": "source",
                "source_output": "IMAGE",
                "target_alias": "process",
                "target_input": "image",
            }
        ],
    }
    ambiguous = _compile(payload, catalog)
    assert any(issue["code"] == "ambiguous_output_slot" for issue in ambiguous["issues"])

    payload["connections"][0]["source_output_index"] = 1
    exact = _compile(payload, catalog)
    assert exact["valid"] is True
    assert exact["resolved_connections"][0]["source_output_index"] == 1


def test_partner_dynamic_combo_and_autogrow_inputs_are_resolved():
    catalog = _catalog()
    catalog["GeminiNanoBanana"] = {
        "display_name": "Nano Banana",
        "category": "partner/image/Gemini",
        "input": {
            "required": {
                "prompt": ["STRING", {"default": ""}],
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "Model Pro",
                                "inputs": {
                                    "required": {
                                        "resolution": [
                                            "COMBO",
                                            {
                                                "options": ["1K", "2K"],
                                                "default": "1K",
                                            },
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
                                    }
                                },
                            },
                            {"key": "Model Lite", "inputs": {"required": {}}},
                        ]
                    },
                ],
            }
        },
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
        "api_node": True,
        "python_module": "comfy_api_nodes.nodes_gemini",
    }
    result = _compile(
        {
            "nodes": [
                {"alias": "reference", "node_type": "ImageSource"},
                {
                    "alias": "generate",
                    "node_type": "GeminiNanoBanana",
                    "values": {
                        "prompt": "nighttime factory",
                        "model": "Model Pro",
                        "model.resolution": "2K",
                    },
                },
            ],
            "connections": [
                {
                    "source_alias": "reference",
                    "source_output": "IMAGE",
                    "target_alias": "generate",
                    "target_input": "model.images.image_1",
                }
            ],
        },
        catalog,
    )

    assert result["valid"] is True
    assert result["requirements"]["partner_authentication_aliases"] == ["generate"]
    assert result["warning_count"] == 1
    assert result["issues"][0]["code"] == "partner_authentication_may_be_required"
    assert result["resolved_connections"][0]["target_input"] == "model.images.image_1"
    assert "inputs" not in result["resolved_nodes"][0]

    invalid = _compile(
        {
            "nodes": [
                {
                    "alias": "generate",
                    "node_type": "GeminiNanoBanana",
                    "values": {
                        "model": "Model Lite",
                        "model.resolution": "1K",
                    },
                }
            ]
        },
        catalog,
    )
    assert any(issue["code"] == "unknown_widget" for issue in invalid["issues"])

    missing_selection = _compile(
        {
            "nodes": [
                {"alias": "generate", "node_type": "GeminiNanoBanana"},
            ]
        },
        catalog,
    )
    assert any(
        issue["code"] == "missing_dynamic_selection" for issue in missing_selection["issues"]
    )


def test_dynamic_slot_expands_nested_requirements_only_when_connected():
    catalog = _catalog()
    catalog["MaskSource"] = {
        "input": {"required": {}},
        "output": ["MASK"],
        "output_name": ["MASK"],
        "python_module": "nodes",
    }
    catalog["DynamicTarget"] = {
        "input": {
            "required": {},
            "optional": {
                "guide": [
                    "COMFY_DYNAMICSLOT_V3",
                    {
                        "slotType": "IMAGE",
                        "inputs": {
                            "required": {
                                "mask": ["MASK"],
                                "enabled": ["BOOLEAN", {"default": True}],
                            }
                        },
                    },
                ]
            },
        },
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
        "python_module": "custom_nodes.dynamic",
    }
    payload = {
        "nodes": [
            {"alias": "image", "node_type": "ImageSource"},
            {"alias": "mask", "node_type": "MaskSource"},
            {
                "alias": "target",
                "node_type": "DynamicTarget",
                "values": {"guide.enabled": True},
            },
        ],
        "connections": [
            {
                "source_alias": "image",
                "source_output": "IMAGE",
                "target_alias": "target",
                "target_input": "guide",
            },
            {
                "source_alias": "mask",
                "source_output": "MASK",
                "target_alias": "target",
                "target_input": "guide.mask",
            },
        ],
    }

    valid = _compile(payload, catalog)
    assert valid["valid"] is True
    target = next(node for node in valid["resolved_nodes"] if node["alias"] == "target")
    assert target["origin"] == "custom"

    payload["connections"] = []
    inactive = _compile(payload, catalog)
    assert any(issue["code"] == "unknown_widget" for issue in inactive["issues"])


def test_request_contract_rejects_unknown_fields_and_invalid_aliases():
    with pytest.raises(ValidationError):
        PlanWorkflowRequest.model_validate(
            {
                "nodes": [{"alias": "Bad Alias", "node_type": "ImageSource"}],
                "unexpected": True,
            }
        )
