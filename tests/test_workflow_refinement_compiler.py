import copy

import pytest
from node_library import catalog_contract_hash, node_schema_hash
from workflow_capability_graph import VerifiedCapabilityLesson
from workflow_graph_patch import (
    ApplyGraphPatchRequest,
    compile_graph_patch,
    graph_patch_request_from_apply,
)
from workflow_refinement import normalize_workflow_graph
from workflow_refinement_compiler import (
    CompileWorkflowRefinementSpecRequest,
    _canonicalize_update_mapping,
    compile_workflow_refinement_spec,
)
from workflow_schema_capabilities import normalize_node_schema


def _live_resize_image_mask_schema():
    """Persisted 2026-08-08 ResizeImageMaskNode /object_info shape."""

    return {
        "display_name": "Resize Image/Mask",
        "category": "image/transform",
        "description": "Resize an image or mask using various scaling methods.",
        "input": {
            "required": {
                "input": [
                    "COMFY_MATCHTYPE_V3",
                    {
                        "template": {
                            "allowed_types": "IMAGE,MASK",
                            "template_id": "input_type",
                        }
                    },
                ],
                "resize_type": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "scale dimensions",
                                "inputs": {
                                    "required": {
                                        "width": [
                                            "INT",
                                            {"default": 512, "min": 0, "max": 16384},
                                        ],
                                        "height": [
                                            "INT",
                                            {"default": 512, "min": 0, "max": 16384},
                                        ],
                                        "crop": [
                                            "COMBO",
                                            {
                                                "options": ["disabled", "center"],
                                                "default": "center",
                                            },
                                        ],
                                    }
                                },
                            },
                            {
                                "key": "scale by multiplier",
                                "inputs": {
                                    "required": {
                                        "multiplier": [
                                            "FLOAT",
                                            {
                                                "default": 1.0,
                                                "min": 0.01,
                                                "max": 8.0,
                                            },
                                        ]
                                    }
                                },
                            },
                            {
                                "key": "scale longer dimension",
                                "inputs": {
                                    "required": {
                                        "longer_size": [
                                            "INT",
                                            {"default": 512, "min": 0, "max": 16384},
                                        ]
                                    }
                                },
                            },
                            {
                                "key": "scale shorter dimension",
                                "inputs": {
                                    "required": {
                                        "shorter_size": [
                                            "INT",
                                            {"default": 512, "min": 0, "max": 16384},
                                        ]
                                    }
                                },
                            },
                            {
                                "key": "scale width",
                                "inputs": {
                                    "required": {
                                        "width": [
                                            "INT",
                                            {"default": 512, "min": 0, "max": 16384},
                                        ]
                                    }
                                },
                            },
                            {
                                "key": "scale height",
                                "inputs": {
                                    "required": {
                                        "height": [
                                            "INT",
                                            {"default": 512, "min": 0, "max": 16384},
                                        ]
                                    }
                                },
                            },
                            {
                                "key": "scale total pixels",
                                "inputs": {
                                    "required": {
                                        "megapixels": [
                                            "FLOAT",
                                            {
                                                "default": 1.0,
                                                "min": 0.01,
                                                "max": 16.0,
                                            },
                                        ]
                                    }
                                },
                            },
                            {
                                "key": "match size",
                                "inputs": {
                                    "required": {
                                        "match": ["IMAGE,MASK", {}],
                                        "crop": [
                                            "COMBO",
                                            {
                                                "options": ["disabled", "center"],
                                                "default": "center",
                                            },
                                        ],
                                    }
                                },
                            },
                            {
                                "key": "scale to multiple",
                                "inputs": {
                                    "required": {
                                        "multiple": [
                                            "INT",
                                            {"default": 8, "min": 1, "max": 16384},
                                        ]
                                    }
                                },
                            },
                        ]
                    },
                ],
                "scale_method": [
                    "COMBO",
                    {
                        "options": [
                            "nearest-exact",
                            "bilinear",
                            "area",
                            "bicubic",
                            "lanczos",
                        ],
                        "default": "area",
                    },
                ],
            }
        },
        "input_order": {"required": ["input", "resize_type", "scale_method"]},
        "is_input_list": False,
        "output": ["COMFY_MATCHTYPE_V3"],
        "output_name": ["resized"],
        "output_matchtypes": ["input_type"],
        "output_is_list": [False],
        "python_module": "comfy_extras.nodes_post_processing",
    }


def _catalog():
    return {
        "LoadImage": {
            "display_name": "Load Image",
            "input": {
                "required": {
                    "image": [
                        ["old.png"],
                        {"image_upload": True},
                    ]
                }
            },
            "output": ["IMAGE", "MASK"],
            "output_name": ["IMAGE", "MASK"],
            "python_module": "nodes",
        },
        "WaveletColorFix": {
            "display_name": "Wavelet Color Fix",
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
            "display_name": "Save Image",
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                }
            },
            "output": [],
            "output_name": [],
            "python_module": "nodes",
        },
        "ByteDance2ReferenceNode": {
            "display_name": "Seedance 2.0 Omni Reference",
            "category": "partner/video",
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
                                            "prompt": ["STRING", {"default": ""}],
                                            "resolution": [["720p", "1080p"], {"default": "1080p"}],
                                            "ratio": [["16:9", "adaptive"], {"default": "adaptive"}],
                                            "duration": ["INT", {"default": 7}],
                                            "generate_audio": ["BOOLEAN", {"default": True}],
                                            "reference_images": [
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
                                        "optional": {
                                            "auto_downscale": ["BOOLEAN", {"default": True}],
                                            "auto_upscale": ["BOOLEAN", {"default": False}],
                                        },
                                    },
                                }
                            ]
                        },
                    ],
                    "seed": ["INT", {"default": 0}],
                    "watermark": ["BOOLEAN", {"default": False}],
                }
            },
            "output": ["VIDEO"],
            "output_name": ["VIDEO"],
            "python_module": "comfy_api_nodes.nodes_bytedance",
            "api_node": True,
        },
        "SaveVideo": {
            "display_name": "Save Video",
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
            "display_name": "Get Video Components",
            "input": {"required": {"video": ["VIDEO"]}},
            "output": ["IMAGE", "AUDIO", "FLOAT", "INT"],
            "output_name": ["images", "audio", "fps", "bit_depth"],
            "python_module": "comfy_extras.nodes_video",
        },
        "VHS_VideoCombine": {
            "display_name": "Video Combine",
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "frame_rate": ["FLOAT", {"default": 8.0}],
                    "loop_count": ["INT", {"default": 0}],
                    "filename_prefix": ["STRING", {"default": "AnimateDiff"}],
                    "format": [["video/h264-mp4", "video/webm"], {"default": "video/h264-mp4"}],
                    "pingpong": ["BOOLEAN", {"default": False}],
                    "save_output": ["BOOLEAN", {"default": True}],
                },
                "optional": {"audio": ["AUDIO"]},
            },
            "output": ["VHS_FILENAMES"],
            "output_name": ["Filenames"],
            "python_module": "custom_nodes.ComfyUI-VideoHelperSuite",
        },
        "EmptyImage": {
            "display_name": "Empty Image",
            "input": {
                "required": {
                    "width": ["INT", {"default": 512, "min": 1}],
                    "height": ["INT", {"default": 512, "min": 1}],
                    "batch_size": ["INT", {"default": 1, "min": 1}],
                    "color": ["INT", {"default": 0, "min": 0}],
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "nodes",
        },
        "ResizeImageMaskNode": _live_resize_image_mask_schema(),
        "MultiOptionImageNode": {
            "display_name": "Multi Option Image Node",
            "input": {
                "required": {
                    "model": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {
                            "options": [
                                {
                                    "key": "full",
                                    "inputs": {
                                        "required": {"image": ["IMAGE", {}]},
                                    },
                                },
                                {
                                    "key": "lite",
                                    "inputs": {
                                        "required": {"image": ["IMAGE", {}]},
                                    },
                                },
                            ]
                        },
                    ]
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "partner.multi_option",
        },
        "DynamicUpdateNode": {
            "display_name": "Dynamic Update Node",
            "input": {
                "required": {
                    "model": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {
                            "options": [
                                {
                                    "key": "A",
                                    "inputs": {
                                        "required": {
                                            "a": ["IMAGE", {}],
                                            "aspect_ratio": [
                                                "COMBO",
                                                {
                                                    "options": ["1:1", "16:9"],
                                                    "default": "1:1",
                                                },
                                            ],
                                        }
                                    },
                                },
                                {
                                    "key": "B",
                                    "inputs": {
                                        "required": {
                                            "b": ["IMAGE", {}],
                                            "aspect_ratio": [
                                                "COMBO",
                                                {
                                                    "options": ["1:1", "16:9"],
                                                    "default": "16:9",
                                                },
                                            ],
                                        }
                                    },
                                },
                            ]
                        },
                    ]
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
            "python_module": "partner.dynamic_update",
        },
        "DynamicSlotBeforeSelector": {
            "display_name": "Dynamic Slot Before Selector",
            "input": {
                "required": {
                    "payload": [
                        "COMFY_DYNAMICSLOT_V3",
                        {
                            "slotType": "IMAGE",
                            "inputs": {
                                "required": {
                                    "strength": ["FLOAT", {"default": 0.5}]
                                }
                            },
                        },
                    ],
                    "model": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {
                            "options": [
                                {
                                    "key": "A",
                                    "inputs": {
                                        "required": {
                                            "quality": [
                                                ["draft", "high"],
                                                {"default": "draft"},
                                            ]
                                        }
                                    },
                                },
                                {
                                    "key": "B",
                                    "inputs": {
                                        "required": {
                                            "quality": [
                                                ["draft", "high"],
                                                {"default": "high"},
                                            ]
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
            "python_module": "partner.dynamic_slot_selector",
        },
        "MaskSource": {
            "display_name": "Mask Source",
            "input": {},
            "output": ["MASK"],
            "output_name": ["MASK"],
            "python_module": "nodes",
        },
        "ComfySwitch": {
            "display_name": "Comfy Switch",
            "input": {
                "required": {
                    "on_true": [
                        "COMFY_MATCHTYPE_V3",
                        {
                            "template": {
                                "template_id": "switch",
                                "allowed_types": "*",
                            }
                        },
                    ]
                }
            },
            "output": ["COMFY_MATCHTYPE_V3"],
            "output_name": ["value"],
            "output_matchtypes": ["switch"],
            "python_module": "nodes",
        },
        "DualMatchSwitch": {
            "display_name": "Dual Match Switch",
            "input": {
                "required": {
                    "left": [
                        "COMFY_MATCHTYPE_V3",
                        {
                            "template": {
                                "template_id": "switch",
                                "allowed_types": "*",
                            }
                        },
                    ],
                    "right": [
                        "COMFY_MATCHTYPE_V3",
                        {
                            "template": {
                                "template_id": "switch",
                                "allowed_types": "*",
                            }
                        },
                    ],
                }
            },
            "output": ["COMFY_MATCHTYPE_V3"],
            "output_name": ["value"],
            "output_matchtypes": ["switch"],
            "python_module": "nodes",
        },
        "ResizeMatch": {
            "display_name": "Resize Match",
            "input": {
                "required": {
                    "input": [
                        "COMFY_MATCHTYPE_V3",
                        {
                            "template": {
                                "template_id": "input_type",
                                "allowed_types": ["IMAGE", "MASK"],
                            }
                        },
                    ]
                }
            },
            "output": ["COMFY_MATCHTYPE_V3"],
            "output_name": ["output"],
            "output_matchtypes": ["input_type"],
            "python_module": "nodes",
        },
        "ListImageSource": {
            "display_name": "List Image Source",
            "input": {},
            "output": ["IMAGE"],
            "output_name": ["images"],
            "output_is_list": [True],
            "python_module": "nodes",
        },
        "ListImageTarget": {
            "display_name": "List Image Target",
            "input": {"required": {"images": ["IMAGE", {}]}},
            "is_input_list": True,
            "output": [],
            "python_module": "nodes",
        },
        "ScalarImageTarget": {
            "display_name": "Scalar Image Target",
            "input": {"required": {"images": ["IMAGE", {}]}},
            "output": [],
            "python_module": "nodes",
        },
    }


def _workflow():
    return {
        "last_node_id": 61,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 60,
                "type": "WaveletColorFix",
                "order": 0,
                "inputs": [
                    {"name": "target_image", "type": "IMAGE", "link": None},
                    {"name": "source_image", "type": "IMAGE", "link": None},
                    {"name": "align_method", "type": "COMBO", "widget": {"name": "align_method"}, "link": None},
                ],
                "outputs": [{"name": "image", "type": "IMAGE", "links": [1]}],
                "properties": {
                    "fl_mcp_workflow_refinement": {"alias": "wavelet_color_fix"}
                },
                "widgets_values": ["wavelet"],
            },
            {
                "id": 61,
                "type": "SaveImage",
                "order": 1,
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": 1},
                    {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}, "link": None},
                ],
                "outputs": [],
                "properties": {
                    "fl_mcp_workflow_refinement": {"alias": "save_image"}
                },
                "widgets_values": ["ComfyUI"],
            },
        ],
        "links": [[1, 60, 0, 61, 0, "IMAGE"]],
    }


def _request():
    return CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-seedance-patch-0001",
            "existing_nodes": [
                {
                    "alias": "wavelet",
                    "node_type": "WaveletColorFix",
                    "workflow_alias": "wavelet_color_fix",
                }
            ],
            "create_nodes": [
                {
                    "alias": "seedance_reference",
                    "capability": "Seedance 2.0 Omni Reference",
                    "requested_node_type": "ByteDance2ReferenceNode",
                    "values": {
                        "model": "Seedance 2.0",
                        "prompt": "Animate the approved image without changing its subject.",
                        "resolution": "1080p",
                        "ratio": "adaptive",
                        "duration": 7,
                        "generate_audio": True,
                        "seed": 0,
                        "watermark": False,
                    },
                },
                {
                    "alias": "save_video",
                    "capability": "save native video",
                    "requested_node_type": "SaveVideo",
                },
                {
                    "alias": "video_components",
                    "capability": "convert native video to image audio and fps components",
                    "requested_node_type": "GetVideoComponents",
                },
                {
                    "alias": "video_combine",
                    "capability": "combine image frames into video",
                    "requested_node_type": "VHS_VideoCombine",
                },
            ],
            "add_edges": [
                {
                    "source_alias": "wavelet",
                    "source_output": "image",
                    "target_alias": "seedance_reference",
                    "target_input": "image_1",
                },
                {
                    "source_alias": "seedance_reference",
                    "source_output": "VIDEO",
                    "target_alias": "save_video",
                    "target_input": "video",
                },
                {
                    "source_alias": "seedance_reference",
                    "source_output": "VIDEO",
                    "target_alias": "video_components",
                    "target_input": "video",
                },
                {
                    "source_alias": "video_components",
                    "source_output": "images",
                    "target_alias": "video_combine",
                    "target_input": "images",
                },
                {
                    "source_alias": "video_components",
                    "source_output": "audio",
                    "target_alias": "video_combine",
                    "target_input": "audio",
                },
                {
                    "source_alias": "video_components",
                    "source_output": "fps",
                    "target_alias": "video_combine",
                    "target_input": "frame_rate",
                },
            ],
        }
    )


def test_semantic_refinement_compiler_builds_one_ready_arbitrary_dag_patch():
    catalog = _catalog()
    result = compile_workflow_refinement_spec(
        _request(),
        _workflow(),
        workflow_identity="fl-mcp-workflow-semantic-0001",
        graph_hash="a" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://127.0.0.1:8188/object_info",
    )

    assert result["valid"] is True, result["issues"]
    assert result["needs_choice"] is False
    assert result["selection"][0]["selected"]["node_id"] == 60
    assert result["apply_request"]["plan"]["expected_delta"] == {
        "created_node_count": 4,
        "updated_node_count": 0,
        "removed_node_count": 0,
        "added_edge_count": 6,
        "removed_edge_count": 0,
        "final_node_count": 6,
        "final_edge_count": 7,
    }
    plan = result["plan"]
    combine = next(item for item in plan["create_nodes"] if item["alias"] == "video_combine")
    assert "frame_rate" not in combine["values"]
    fps = next(item for item in plan["add_edges"] if item["source"]["output"] == "fps")
    assert fps["source"]["ref"] == {"alias": "video_components"}
    assert fps["target"]["ref"] == {"alias": "video_combine"}
    assert fps["target"]["input"] == "frame_rate"
    assert fps["target"]["mode"] == "convert_widget"
    assert fps["target"]["socket_index"] is None
    assert result["partner_review"]["required"] is True


def test_latest_seedance_request_compiles_once_with_semantic_dynamic_input():
    catalog = _catalog()
    catalog["VHS_VideoCombine"]["input"]["required"]["format"][1]["formats"] = {
        "video/h264-mp4": {}
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "ren-expand-seedance2-wavelet-video-20260808-v1",
            "existing_nodes": [
                {
                    "alias": "wavelet_colour_fix",
                    "title_contains": "Wavelet Color Fix",
                    "occurrence": "only",
                }
            ],
            "create_nodes": [
                {
                    "alias": "seedance_2_omni_reference",
                    "capability": (
                        "Installed Seedance 2.0 partner Omni Reference image-to-video "
                        "node that accepts an image reference and outputs native "
                        "ComfyUI VIDEO"
                    ),
                    "allowed_origins": ["partner"],
                    "required_input_types": ["IMAGE"],
                    "required_output_types": ["VIDEO"],
                },
                {
                    "alias": "save_video",
                    "capability": (
                        "Native ComfyUI Save Video node that saves a native VIDEO input"
                    ),
                    "allowed_origins": ["native"],
                    "required_input_types": ["VIDEO"],
                },
                {
                    "alias": "video_components",
                    "capability": (
                        "Native ComfyUI node that converts/decomposes a native VIDEO "
                        "into IMAGE frames and exposes matching FPS and audio"
                    ),
                    "allowed_origins": ["native"],
                    "required_input_types": ["VIDEO"],
                    "required_output_types": ["IMAGE", "AUDIO", "FLOAT"],
                },
                {
                    "alias": "video_combine",
                    "capability": (
                        "Installed Video Combine node that encodes IMAGE frames, "
                        "accepts audio, and accepts frame rate so it can match the "
                        "source video FPS"
                    ),
                    "allowed_origins": ["custom"],
                    "required_input_types": ["IMAGE"],
                },
            ],
            "add_edges": [
                {
                    "source_alias": "wavelet_colour_fix",
                    "source_output": "image",
                    "target_alias": "seedance_2_omni_reference",
                    "target_input": "image",
                },
                {
                    "source_alias": "seedance_2_omni_reference",
                    "source_output": "video",
                    "target_alias": "save_video",
                    "target_input": "video",
                },
                {
                    "source_alias": "seedance_2_omni_reference",
                    "source_output": "video",
                    "target_alias": "video_components",
                    "target_input": "video",
                },
                {
                    "source_alias": "video_components",
                    "source_output": "images",
                    "target_alias": "video_combine",
                    "target_input": "images",
                },
                {
                    "source_alias": "video_components",
                    "source_output": "audio",
                    "target_alias": "video_combine",
                    "target_input": "audio",
                },
                {
                    "source_alias": "video_components",
                    "source_output": "fps",
                    "target_alias": "video_combine",
                    "target_input": "frame_rate",
                    "target_mode": "convert_widget",
                },
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        _workflow(),
        workflow_identity="fl-mcp-live-seedance-replay",
        graph_hash="a" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    assert result["needs_choice"] is False
    assert result["resolution"]["selected_node_types"]["video_components"] == (
        "GetVideoComponents"
    )
    reference_edge = next(
        item
        for item in result["plan"]["add_edges"]
        if item["target"]["ref"] == {"alias": "seedance_2_omni_reference"}
    )
    assert reference_edge["target"]["input"] == (
        "model.reference_images.image_1"
    )
    assert reference_edge["target"]["type"] == "IMAGE"
    assert result["error_count"] == 0
    assert result["warning_count"] == 2
    assert result["apply_request"] is not None
    assert {item["code"] for item in result["issues"]} == {
        "partner_authentication_cost_privacy_review_required",
        "schema_adapter_required",
    }


def test_generic_autogrow_allocates_first_two_unused_type_compatible_slots():
    catalog = _catalog()
    catalog["GenericReferenceCollector"] = {
        "display_name": "Generic Reference Collector",
        "input": {
            "required": {
                "reference_images": [
                    "COMFY_AUTOGROW_V3",
                    {
                        "template": {
                            "input": {"required": {"image": ["IMAGE"]}},
                            "names": ["slot_1", "slot_2"],
                            "min": 0,
                        }
                    },
                ]
            }
        },
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
        "python_module": "custom_nodes.generic_reference_pack",
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "generic-autogrow-two-source-0001",
            "create_nodes": [
                {
                    "alias": "first_source",
                    "capability": "empty image",
                    "requested_node_type": "EmptyImage",
                },
                {
                    "alias": "second_source",
                    "capability": "empty image",
                    "requested_node_type": "EmptyImage",
                },
                {
                    "alias": "collector",
                    "capability": "generic reference collector",
                    "requested_node_type": "GenericReferenceCollector",
                },
            ],
            "add_edges": [
                {
                    "source_alias": "first_source",
                    "source_output": "IMAGE",
                    "target_alias": "collector",
                    "target_input": "reference image",
                },
                {
                    "source_alias": "second_source",
                    "source_output": "IMAGE",
                    "target_alias": "collector",
                    "target_input": "reference image",
                },
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        {"nodes": [], "links": [], "groups": [], "extra": {}},
        workflow_identity="fl-mcp-generic-autogrow",
        graph_hash="b" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    targets_by_source = {
        item["source"]["ref"]["alias"]: item["target"]["input"]
        for item in result["plan"]["add_edges"]
    }
    assert targets_by_source == {
        "first_source": "reference_images.slot_1",
        "second_source": "reference_images.slot_2",
    }


def test_seedance_two_semantic_image_edges_allocate_image_1_then_image_2():
    catalog = _catalog()
    payload = _request().model_dump(mode="json")
    payload["application_id"] = "seedance-two-semantic-images-0001"
    payload["create_nodes"].insert(
        0,
        {
            "alias": "second_image",
            "capability": "empty image",
            "requested_node_type": "EmptyImage",
        },
    )
    payload["add_edges"][0]["target_input"] = "first available reference image"
    payload["add_edges"].insert(
        1,
        {
            "source_alias": "second_image",
            "source_output": "IMAGE",
            "target_alias": "seedance_reference",
            "target_input": "next reference image",
        },
    )

    result = compile_workflow_refinement_spec(
        CompileWorkflowRefinementSpecRequest.model_validate(payload),
        _workflow(),
        workflow_identity="fl-mcp-seedance-two-images",
        graph_hash="e" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    targets_by_source = {
        next(iter(item["source"]["ref"].values())): item["target"]["input"]
        for item in result["plan"]["add_edges"]
        if item["target"]["ref"] == {"alias": "seedance_reference"}
    }
    assert targets_by_source == {
        60: "model.reference_images.image_1",
        "second_image": "model.reference_images.image_2",
    }


def test_semantic_autogrow_fails_closed_with_compact_group_candidates():
    catalog = _catalog()
    seedance_inputs = catalog["ByteDance2ReferenceNode"]["input"]["required"][
        "model"
    ][1]["options"][0]["inputs"]["required"]
    seedance_inputs["style_images"] = [
        "COMFY_AUTOGROW_V3",
        {
            "template": {
                "input": {"required": {"image": ["IMAGE"]}},
                "names": ["style_1", "style_2"],
                "min": 0,
            }
        },
    ]
    request = _request()
    request.add_edges[0].target_input = "first available image"

    result = compile_workflow_refinement_spec(
        request,
        _workflow(),
        workflow_identity="fl-mcp-ambiguous-autogrow",
        graph_hash="c" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is False
    issue = next(
        item
        for item in result["issues"]
        if item["code"] == "ambiguous_semantic_input_slot"
    )
    assert issue["candidate_count"] == 2
    assert {item["dynamic_group"] for item in issue["candidates"]} == {
        "model.reference_images",
        "model.style_images",
    }
    assert all(
        set(item) >= {"path", "input_index", "socket_index", "types"}
        for item in issue["candidates"]
    )
    assert result["apply_request"] is None


@pytest.mark.parametrize(
    ("target_input", "expected_path"),
    [
        ("first reference image", "model.reference_images.image_1"),
        ("second reference image", "model.reference_images.image_2"),
        ("2nd reference image", "model.reference_images.image_2"),
    ],
)
def test_semantic_autogrow_resolves_explicit_ordinals(
    target_input,
    expected_path,
):
    catalog = _catalog()
    request = _request()
    request.add_edges[0].target_input = target_input

    result = compile_workflow_refinement_spec(
        request,
        _workflow(),
        workflow_identity="fl-mcp-explicit-autogrow-ordinal",
        graph_hash="d" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    edge = next(
        item
        for item in result["plan"]["add_edges"]
        if item["target"]["ref"] == {"alias": "seedance_reference"}
    )
    assert edge["target"]["input"] == expected_path


def test_exact_live_seedance_request_compiles_one_pass_without_internal_names():
    catalog = _catalog()
    reference = copy.deepcopy(catalog["ByteDance2ReferenceNode"])
    reference["display_name"] = "ByteDance Seedance 2.0 Reference to Video"
    model_inputs = reference["input"]["required"]["model"][1]["options"][0][
        "inputs"
    ]["required"]
    model_inputs["resolution"] = [
        ["480p", "720p", "1080p"],
        {"default": "480p"},
    ]
    catalog["ByteDance2ReferenceNode"] = reference
    catalog["ByteDance2FirstLastFrameNode"] = {
        "display_name": "ByteDance Seedance 2.0 First-Last-Frame to Video",
        "category": "partner/video/ByteDance",
        "description": (
            "Generate video using Seedance 2.0 from a first frame image and "
            "optional last frame image."
        ),
        "input": {
            "required": {
                "first_frame": ["IMAGE", {}],
                "prompt": ["STRING", {"default": ""}],
            }
        },
        "output": ["VIDEO"],
        "output_name": ["VIDEO"],
        "python_module": "comfy_api_nodes.nodes_bytedance",
        "api_node": True,
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate({
        "application_id": "ren-seedance-reference-video-five-node-v1",
        "allow_inferred_converters": False,
        "create_nodes": [
            {
                "alias": "target_black",
                "requested_node_type": "EmptyImage",
                "allowed_origins": ["native"],
                "capability": (
                    "Create a solid black image tensor at 512 by 512 with batch size 1."
                ),
                "required_output_types": ["IMAGE"],
                "values": {
                    "width": 512,
                    "height": 512,
                    "batch_size": 1,
                    "color": 0,
                },
            },
            {
                "alias": "source_gray",
                "requested_node_type": "EmptyImage",
                "allowed_origins": ["native"],
                "capability": (
                    "Create a solid mid-gray image tensor at 512 by 512 with batch size 1."
                ),
                "required_output_types": ["IMAGE"],
                "values": {
                    "width": 512,
                    "height": 512,
                    "batch_size": 1,
                    "color": 8421504,
                },
            },
            {
                "alias": "wavelet_fix",
                "allowed_origins": ["custom"],
                "capability": (
                    "Locally loaded Wavelet Color Fix node that color-aligns a source "
                    "image to a target image using wavelet alignment."
                ),
                "preferred_node_types": ["WaveletColorFix", "Wavelet Color Fix"],
                "required_input_types": ["IMAGE", "IMAGE"],
                "required_output_types": ["IMAGE"],
                "values": {"alignment": "wavelet"},
            },
            {
                "alias": "seedance",
                "allowed_origins": ["partner"],
                "capability": (
                    "Locally loaded Seedance 2.0 reference-to-video partner node. Use "
                    "the first available dynamic reference image input, model Seedance "
                    "2.0, a text prompt, 16:9 aspect ratio, 480p resolution, 4-second "
                    "duration, generated audio, deterministic seed 17, and no watermark."
                ),
                "required_input_types": ["IMAGE"],
                "required_output_types": ["VIDEO"],
                "values": {
                    "model": "Seedance 2.0",
                    "prompt": (
                        "A locked-off cinematic industrial factory at dusk, subtle "
                        "steam and warm practical lights"
                    ),
                    "aspect_ratio": "16:9",
                    "resolution": "480p",
                    "duration": 4,
                    "generate_audio": True,
                    "seed": 17,
                    "watermark": False,
                },
            },
            {
                "alias": "save_video",
                "requested_node_type": "SaveVideo",
                "allowed_origins": ["native"],
                "capability": "Save a native VIDEO with the requested filename prefix.",
                "required_input_types": ["VIDEO"],
                "values": {
                    "filename_prefix": "ren-semantic-seedance-direct"
                },
            },
        ],
        "add_edges": [
            {
                "source_alias": "target_black",
                "source_output": "IMAGE",
                "target_alias": "wavelet_fix",
                "target_input": "target_image",
            },
            {
                "source_alias": "source_gray",
                "source_output": "IMAGE",
                "target_alias": "wavelet_fix",
                "target_input": "source_image",
            },
            {
                "source_alias": "wavelet_fix",
                "source_output": "IMAGE",
                "target_alias": "seedance",
                "target_input": "first available reference image",
            },
            {
                "source_alias": "seedance",
                "source_output": "VIDEO",
                "target_alias": "save_video",
                "target_input": "video",
            },
        ],
    })

    result = compile_workflow_refinement_spec(
        request,
        {"nodes": [], "links": [], "groups": [], "extra": {}},
        workflow_identity="fl-mcp-live-seedance-natural-request",
        graph_hash="f" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    assert result["needs_choice"] is False
    assert result["resolution"]["selected_node_types"]["seedance"] == (
        "ByteDance2ReferenceNode"
    )
    nodes = {item["alias"]: item for item in result["plan"]["create_nodes"]}
    assert nodes["wavelet_fix"]["values"]["align_method"] == "wavelet"
    assert nodes["seedance"]["values"]["model.ratio"] == "16:9"
    reference_edge = next(
        item
        for item in result["plan"]["add_edges"]
        if item["target"]["ref"] == {"alias": "seedance"}
    )
    assert reference_edge["target"]["input"] == (
        "model.reference_images.image_1"
    )
    assert result["plan"]["expected_delta"]["created_node_count"] == 5
    assert result["plan"]["expected_delta"]["added_edge_count"] == 4
    assert result["apply_request"] is not None


def test_existing_dynamic_update_uses_unique_semantic_ratio_leaf_alias():
    node_info = _catalog()["ByteDance2ReferenceNode"]
    capabilities = normalize_node_schema("ByteDance2ReferenceNode", node_info)

    expected, expected_issues = _canonicalize_update_mapping(
        {"aspect_ratio": "adaptive"},
        capabilities,
        context_values={"model": "Seedance 2.0"},
        connected_inputs=set(),
        path="update_nodes[0].expected_values",
    )
    updated, update_issues = _canonicalize_update_mapping(
        {"aspect_ratio": "16:9"},
        capabilities,
        context_values={"model": "Seedance 2.0"},
        connected_inputs=set(),
        path="update_nodes[0].set_values",
    )

    assert expected_issues == []
    assert update_issues == []
    assert expected == {"model.ratio": "adaptive"}
    assert updated == {"model.ratio": "16:9"}


def test_unknown_endpoint_reports_bounded_structured_candidates():
    catalog = _catalog()
    request = _request()
    request.add_edges[0].target_input = "not a loaded input"

    result = compile_workflow_refinement_spec(
        request,
        _workflow(),
        workflow_identity="fl-mcp-endpoint-candidates",
        graph_hash="d" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    issue = next(item for item in result["issues"] if item["code"] == "unknown_input_slot")
    assert issue["candidate_count"] > 0
    assert len(issue["candidates"]) <= 8
    assert all(
        set(item) >= {"path", "name", "input_index", "types", "mode"}
        for item in issue["candidates"]
    )
    assert any(
        item["path"] == "model.reference_images.image_1"
        for item in issue["candidates"]
    )


def test_semantic_attachment_can_update_an_existing_load_image_with_integrity_facts():
    catalog = _catalog()
    workflow = _workflow()
    workflow["last_node_id"] = 62
    workflow["nodes"].append(
        {
            "id": 62,
            "type": "LoadImage",
            "order": 2,
            "inputs": [
                {
                    "name": "image",
                    "type": "COMBO",
                    "widget": {"name": "image"},
                    "link": None,
                }
            ],
            "outputs": [
                {"name": "IMAGE", "type": "IMAGE", "links": []},
                {"name": "MASK", "type": "MASK", "links": []},
            ],
            "widgets_values": ["old.png"],
        }
    )
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "existing-load-image-attachment-0001",
            "existing_nodes": [{"alias": "source", "node_id": 62}],
            "attachments": [
                {
                    "target_alias": "source",
                    "target_input": "image",
                    "image": {
                        "filename": "subject.png",
                        "subfolder": "ren-chat/session",
                        "type": "input",
                    },
                }
            ],
        }
    )
    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-existing-load-image",
        graph_hash="a" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
        validated_attachment_values={
            ("source", "image"): {
                "widget_value": "ren-chat/session/subject.png",
                "size_bytes": 321,
                "sha256": "d" * 64,
            }
        },
    )

    assert result["valid"] is True, result["issues"]
    attachment = result["plan"]["attachments"][0]
    assert attachment["ref"] == {"node_id": 62}
    assert attachment["file_type"] == "input"
    assert attachment["size_bytes"] == 321
    assert attachment["sha256"] == "d" * 64


def test_semantic_attachment_rejects_save_image_filename_prefix_target():
    catalog = _catalog()
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "reject-save-prefix-attachment-0001",
            "create_nodes": [
                {
                    "alias": "source",
                    "capability": "empty image",
                    "requested_node_type": "EmptyImage",
                },
                {
                    "alias": "save",
                    "capability": "save image",
                    "requested_node_type": "SaveImage",
                },
            ],
            "add_edges": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "target_alias": "save",
                    "target_input": "images",
                }
            ],
            "attachments": [
                {
                    "target_alias": "save",
                    "target_input": "filename_prefix",
                    "image": {
                        "filename": "subject.png",
                        "subfolder": "ren-chat/session",
                        "type": "input",
                    },
                }
            ],
        }
    )
    result = compile_workflow_refinement_spec(
        request,
        {
            "last_node_id": 0,
            "last_link_id": 0,
            "nodes": [],
            "links": [],
            "groups": [],
            "config": {},
            "extra": {},
        },
        workflow_identity="fl-mcp-reject-save-prefix",
        graph_hash="b" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
        validated_attachment_values={
            ("save", "filename_prefix"): {
                "widget_value": "ren-chat/session/subject.png",
                "size_bytes": 321,
                "sha256": "d" * 64,
            }
        },
    )

    assert result["valid"] is False
    assert any(
        issue["code"] == "attachment_target_not_image_upload"
        for issue in result["issues"]
    )


def test_existing_selector_returns_candidates_instead_of_guessing():
    workflow = _workflow()
    duplicate = dict(workflow["nodes"][0])
    duplicate["id"] = 62
    duplicate["properties"] = {}
    duplicate["outputs"] = [{"name": "image", "type": "IMAGE", "links": []}]
    workflow["nodes"].append(duplicate)
    request = _request()
    request.existing_nodes[0].workflow_alias = None

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-workflow-semantic-0001",
        graph_hash="a" * 64,
        catalog=_catalog(),
        catalog_hash=catalog_contract_hash(_catalog()),
        source="http://127.0.0.1:8188/object_info",
    )

    assert result["valid"] is False
    assert result["needs_choice"] is True
    assert result["issues"][0]["code"] == "existing_selector_ambiguous"
    assert len(result["selection"][0]["candidates"]) == 2


def test_semantic_refinement_compiler_removes_terminal_and_incident_edge():
    catalog = _catalog()
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-remove-terminal-0001",
            "existing_nodes": [
                {
                    "alias": "save",
                    "node_type": "SaveImage",
                    "workflow_alias": "save_image",
                }
            ],
            "remove_nodes": ["save"],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        _workflow(),
        workflow_identity="fl-mcp-workflow-semantic-0001",
        graph_hash="a" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="http://127.0.0.1:8188/object_info",
    )

    assert result["valid"] is True, result["issues"]
    plan = result["plan"]
    assert len(plan["remove_nodes"]) == 1
    assert len(plan["remove_edges"]) == 1
    assert plan["remove_nodes"][0]["expected_incident_edges"] == plan["remove_edges"]
    assert plan["expected_delta"] == {
        "created_node_count": 0,
        "updated_node_count": 0,
        "removed_node_count": 1,
        "added_edge_count": 0,
        "removed_edge_count": 1,
        "final_node_count": 1,
        "final_edge_count": 0,
    }


def test_create_only_empty_canvas_uses_the_same_graph_patch_kernel_and_round_trip():
    catalog = _catalog()
    workflow = {
        "version": 0.4,
        "last_node_id": 0,
        "last_link_id": 0,
        "nodes": [],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-empty-build-0001",
            "create_nodes": [
                {
                    "alias": "canvas",
                    "capability": "empty image",
                    "requested_node_type": "EmptyImage",
                    "values": {
                        "width": 512,
                        "height": 512,
                        "batch_size": 1,
                        "color": 0,
                    },
                },
                {
                    "alias": "save",
                    "capability": "save image",
                    "requested_node_type": "SaveImage",
                    "values": {"filename_prefix": "ren-empty-build"},
                },
            ],
            "add_edges": [
                {
                    "source_alias": "canvas",
                    "source_output": "IMAGE",
                    "target_alias": "save",
                    "target_input": "images",
                }
            ],
        }
    )
    catalog_hash = catalog_contract_hash(catalog)
    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-empty-workflow-0001",
        graph_hash="e" * 64,
        catalog=catalog,
        catalog_hash=catalog_hash,
        source="http://127.0.0.1:8188/object_info",
    )

    assert result["valid"] is True, result["issues"]
    assert result["plan"]["expected_delta"] == {
        "created_node_count": 2,
        "updated_node_count": 0,
        "removed_node_count": 0,
        "added_edge_count": 1,
        "removed_edge_count": 0,
        "final_node_count": 2,
        "final_edge_count": 1,
    }
    reconstructed = graph_patch_request_from_apply(
        result["apply_request"],
        normalize_workflow_graph(workflow),
    )
    recompiled = compile_graph_patch(
        reconstructed,
        catalog,
        catalog_hash=catalog_hash,
        source="http://127.0.0.1:8188/object_info",
    )
    assert recompiled["valid"] is True, recompiled["issues"]
    assert recompiled["patch_hash"] == result["patch_hash"]
    assert recompiled["plan"] == result["plan"]


def test_semantic_matchtype_edge_binds_from_an_existing_active_output():
    catalog = _catalog()
    workflow = {
        "version": 0.4,
        "last_node_id": 1,
        "last_link_id": 0,
        "nodes": [
            {
                "id": 1,
                "type": "ComfySwitch",
                "order": 0,
                "inputs": [
                    {"name": "on_true", "type": "*", "link": None},
                ],
                "outputs": [
                    {"name": "value", "type": "IMAGE", "links": []},
                ],
                "properties": {
                    "fl_mcp_workflow_refinement": {"alias": "switch"}
                },
                "widgets_values": [],
            }
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-matchtype-edge-0001",
            "existing_nodes": [
                {
                    "alias": "switch",
                    "node_id": 1,
                    "node_type": "ComfySwitch",
                }
            ],
            "create_nodes": [
                {
                    "alias": "resize",
                    "capability": "resize match",
                    "requested_node_type": "ResizeMatch",
                }
            ],
            "add_edges": [
                {
                    "source_alias": "switch",
                    "source_output": "value",
                    "target_alias": "resize",
                    "target_input": "input",
                }
            ],
        }
    )
    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-matchtype-workflow",
        graph_hash="f" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    assert result["plan"]["add_edges"][0]["source"]["type"] == "IMAGE"
    assert result["plan"]["add_edges"][0]["target"]["type"] == "IMAGE"


def _semantic_list_patch(target_type):
    catalog = _catalog()
    workflow = {
        "version": 0.4,
        "last_node_id": 0,
        "last_link_id": 0,
        "nodes": [],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": f"semantic-list-{target_type.lower()}",
            "create_nodes": [
                {
                    "alias": "source",
                    "capability": "list image source",
                    "requested_node_type": "ListImageSource",
                },
                {
                    "alias": "target",
                    "capability": "image target",
                    "requested_node_type": target_type,
                },
            ],
            "add_edges": [
                {
                    "source_alias": "source",
                    "source_output": "images",
                    "target_alias": "target",
                    "target_input": "images",
                }
            ],
        }
    )
    return compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-list-workflow",
        graph_hash="1" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )


def test_semantic_list_edges_follow_native_comfy_mapping_for_all_targets():
    list_target = _semantic_list_patch("ListImageTarget")
    assert list_target["valid"] is True, list_target["issues"]

    scalar_target = _semantic_list_patch("ScalarImageTarget")
    assert scalar_target["valid"] is True, scalar_target["issues"]


def test_multi_option_dynamic_combo_uses_the_selected_duplicate_path():
    catalog = _catalog()
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "multi-option-new-build-0001",
            "create_nodes": [
                {
                    "alias": "source",
                    "capability": "empty image source",
                    "requested_node_type": "EmptyImage",
                },
                {
                    "alias": "dynamic",
                    "capability": "multi option image processor",
                    "requested_node_type": "MultiOptionImageNode",
                    "values": {"model": "lite"},
                },
            ],
            "add_edges": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "target_alias": "dynamic",
                    "target_input": "image",
                }
            ],
        }
    )
    workflow = {
        "last_node_id": 0,
        "last_link_id": 0,
        "nodes": [],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-multi-option-empty",
        graph_hash="2" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    edge = result["plan"]["add_edges"][0]
    assert edge["target"]["input"] == "model.image"
    assert edge["target"]["socket_index"] == 0


def test_existing_multi_option_baseline_does_not_block_an_unrelated_append():
    catalog = _catalog()
    workflow = {
        "last_node_id": 2,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "MultiOptionImageNode",
                "order": 1,
                "inputs": [{"name": "model.image", "type": "IMAGE", "link": 1}],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": ["lite"],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "multi-option-existing-0001",
            "existing_nodes": [
                {"alias": "dynamic", "node_id": 2},
            ],
            "create_nodes": [
                {
                    "alias": "save",
                    "capability": "save image",
                    "requested_node_type": "SaveImage",
                }
            ],
            "add_edges": [
                {
                    "source_alias": "dynamic",
                    "source_output": "IMAGE",
                    "target_alias": "save",
                    "target_input": "images",
                }
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-multi-option-existing",
        graph_hash="3" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]


def _compile_created_matchtype_chain(edges):
    catalog = _catalog()
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-created-match-chain-0001",
            "create_nodes": [
                {
                    "alias": "image",
                    "capability": "empty image source",
                    "requested_node_type": "EmptyImage",
                },
                {
                    "alias": "switch",
                    "capability": "polymorphic switch",
                    "requested_node_type": "ComfySwitch",
                },
                {
                    "alias": "resize",
                    "capability": "polymorphic resize",
                    "requested_node_type": "ResizeMatch",
                },
            ],
            "add_edges": edges,
        }
    )
    workflow = {
        "last_node_id": 0,
        "last_link_id": 0,
        "nodes": [],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    return compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-created-match-chain",
        graph_hash="4" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )


def test_created_matchtype_chain_is_solved_graph_wide_and_edge_order_invariant():
    image_to_switch = {
        "source_alias": "image",
        "source_output": "IMAGE",
        "target_alias": "switch",
        "target_input": "on_true",
    }
    switch_to_resize = {
        "source_alias": "switch",
        "source_output": "value",
        "target_alias": "resize",
        "target_input": "input",
    }

    forward = _compile_created_matchtype_chain(
        [image_to_switch, switch_to_resize]
    )
    reverse = _compile_created_matchtype_chain(
        [switch_to_resize, image_to_switch]
    )

    assert forward["valid"] is True, forward["issues"]
    assert reverse["valid"] is True, reverse["issues"]
    assert forward["patch_hash"] == reverse["patch_hash"]
    assert forward["plan"] == reverse["plan"]
    assert {
        edge["source"]["type"] for edge in forward["plan"]["add_edges"]
    } == {"IMAGE"}
    assert {
        edge["target"]["type"] for edge in forward["plan"]["add_edges"]
    } == {"IMAGE"}


def test_created_matchtype_fan_in_conflict_fails_independent_of_edge_order():
    catalog = _catalog()
    edges = [
        {
            "source_alias": "image",
            "source_output": "IMAGE",
            "target_alias": "switch",
            "target_input": "left",
        },
        {
            "source_alias": "mask",
            "source_output": "MASK",
            "target_alias": "switch",
            "target_input": "right",
        },
    ]

    def compile_with_order(ordered_edges):
        request = CompileWorkflowRefinementSpecRequest.model_validate(
            {
                "application_id": "semantic-match-conflict-0001",
                "create_nodes": [
                    {
                        "alias": "image",
                        "capability": "empty image source",
                        "requested_node_type": "EmptyImage",
                    },
                    {
                        "alias": "mask",
                        "capability": "mask source",
                        "requested_node_type": "MaskSource",
                    },
                    {
                        "alias": "switch",
                        "capability": "dual polymorphic switch",
                        "requested_node_type": "DualMatchSwitch",
                    },
                ],
                "add_edges": ordered_edges,
            }
        )
        return compile_workflow_refinement_spec(
            request,
            {"nodes": [], "links": [], "groups": [], "extra": {}},
            workflow_identity="fl-mcp-match-conflict",
            graph_hash="5" * 64,
            catalog=catalog,
            catalog_hash=catalog_contract_hash(catalog),
            source="fixture",
        )

    forward = compile_with_order(edges)
    reverse = compile_with_order(list(reversed(edges)))
    assert forward["valid"] is False
    assert reverse["valid"] is False
    assert any(
        issue["code"] == "matchtype_binding_conflict"
        for issue in forward["issues"]
    )
    assert any(
        issue["code"] == "matchtype_binding_conflict"
        for issue in reverse["issues"]
    )


def test_unconstrained_created_matchtype_edge_fails_as_ambiguous():
    result = _compile_created_matchtype_chain(
        [
            {
                "source_alias": "switch",
                "source_output": "value",
                "target_alias": "resize",
                "target_input": "input",
            }
        ]
    )

    assert result["valid"] is False
    assert any(issue["code"] == "edge_type_ambiguous" for issue in result["issues"])


def test_existing_dynamic_selector_update_activates_new_socket_before_connection():
    catalog = _catalog()
    workflow = {
        "last_node_id": 4,
        "last_link_id": 0,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "DynamicUpdateNode",
                "order": 1,
                "inputs": [
                    {
                        "name": "model",
                        "type": "COMBO",
                        "widget": {"name": "model"},
                        "link": None,
                    },
                    {"name": "model.a", "type": "IMAGE", "link": None},
                    {
                        "name": "model.aspect_ratio",
                        "type": "COMBO",
                        "widget": {"name": "model.aspect_ratio"},
                        "link": None,
                    },
                ],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": ["A", "1:1"],
            },
            {
                "id": 3,
                "type": "GetVideoComponents",
                "order": 2,
                "inputs": [{"name": "video", "type": "VIDEO", "link": None}],
                "outputs": [
                    {"name": "images", "type": "IMAGE", "links": []},
                    {"name": "audio", "type": "AUDIO", "links": []},
                    {"name": "fps", "type": "FLOAT", "links": []},
                    {"name": "bit_depth", "type": "INT", "links": []},
                ],
                "widgets_values": [],
            },
            {
                "id": 4,
                "type": "VHS_VideoCombine",
                "order": 3,
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": None},
                    {
                        "name": "frame_rate",
                        "type": "FLOAT",
                        "widget": {"name": "frame_rate"},
                        "link": None,
                    },
                ],
                "outputs": [
                    {"name": "Filenames", "type": "VHS_FILENAMES", "links": []}
                ],
                "widgets_values": [8.0, 0, "AnimateDiff", "video/h264-mp4", False, True],
            },
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-existing-dynamic-update-0001",
            "existing_nodes": [
                {"alias": "source", "node_id": 1},
                {"alias": "dynamic", "node_id": 2},
                {"alias": "components", "node_id": 3},
                {"alias": "combine", "node_id": 4},
            ],
            "update_nodes": [
                {
                    "target_alias": "dynamic",
                    "expected_values": {"model": "A", "aspect_ratio": "1:1"},
                    "set_values": {"model": "B", "aspect_ratio": "16:9"},
                }
            ],
            "add_edges": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "target_alias": "dynamic",
                    "target_input": "model.b",
                },
                {
                    "source_alias": "components",
                    "source_output": "fps",
                    "target_alias": "combine",
                    "target_input": "frame_rate",
                },
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-existing-dynamic-update",
        graph_hash="6" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    update = result["plan"]["update_nodes"][0]
    assert update["expected_values"] == {
        "model": "A",
        "model.aspect_ratio": "1:1",
    }
    assert update["set_values"] == {
        "model": "B",
        "model.aspect_ratio": "16:9",
    }
    dynamic_edge = next(
        edge
        for edge in result["plan"]["add_edges"]
        if edge["target"]["input"] == "model.b"
    )
    assert dynamic_edge["target"]["socket_index"] == 0
    assert dynamic_edge["target"]["mode"] == "slot"
    widget_edge = next(
        edge
        for edge in result["plan"]["add_edges"]
        if edge["target"]["input"] == "frame_rate"
    )
    assert widget_edge["target"]["mode"] == "convert_widget"
    assert widget_edge["target"]["socket_index"] is None
    reconstructed = graph_patch_request_from_apply(
        result["apply_request"],
        normalize_workflow_graph(workflow),
    )
    recompiled = compile_graph_patch(
        reconstructed,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )
    assert recompiled["valid"] is True, recompiled["issues"]
    assert recompiled["patch_hash"] == result["patch_hash"]
    assert recompiled["plan"] == result["plan"]


def test_existing_vhs_frame_rate_widget_is_converted_in_the_atomic_patch():
    catalog = _catalog()
    workflow = {
        "last_node_id": 2,
        "last_link_id": 0,
        "nodes": [
            {
                "id": 1,
                "type": "GetVideoComponents",
                "order": 0,
                "inputs": [{"name": "video", "type": "VIDEO", "link": None}],
                "outputs": [
                    {"name": "images", "type": "IMAGE", "links": []},
                    {"name": "audio", "type": "AUDIO", "links": []},
                    {"name": "fps", "type": "FLOAT", "links": []},
                    {"name": "bit_depth", "type": "INT", "links": []},
                ],
                "widgets_values": [],
            },
            {
                "id": 2,
                "type": "VHS_VideoCombine",
                "order": 1,
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": None},
                    {
                        "name": "frame_rate",
                        "type": "FLOAT",
                        "widget": {"name": "frame_rate"},
                        "link": None,
                    },
                ],
                "outputs": [
                    {"name": "Filenames", "type": "VHS_FILENAMES", "links": []}
                ],
                "widgets_values": [8.0, 0, "AnimateDiff", "video/h264-mp4", False, True],
            },
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-existing-vhs-widget-0001",
            "existing_nodes": [
                {"alias": "components", "node_id": 1},
                {"alias": "combine", "node_id": 2},
            ],
            "add_edges": [
                {
                    "source_alias": "components",
                    "source_output": "fps",
                    "target_alias": "combine",
                    "target_input": "frame_rate",
                }
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-existing-vhs-widget",
        graph_hash="7" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    edge = result["plan"]["add_edges"][0]
    assert edge["source"]["output"] == "fps"
    assert edge["target"]["input"] == "frame_rate"
    assert edge["target"]["mode"] == "convert_widget"
    assert edge["target"]["socket_index"] is None


def test_selector_update_atomically_removes_old_branch_edge_and_adds_new_branch_edge():
    catalog = _catalog()
    workflow = {
        "last_node_id": 2,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "DynamicUpdateNode",
                "order": 1,
                "inputs": [{"name": "model.a", "type": "IMAGE", "link": 1}],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": ["A", "1:1"],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-selector-edge-swap-0001",
            "existing_nodes": [
                {"alias": "source", "node_id": 1},
                {"alias": "dynamic", "node_id": 2},
            ],
            "update_nodes": [
                {
                    "target_alias": "dynamic",
                    "expected_values": {"model": "A", "aspect_ratio": "1:1"},
                    "set_values": {"model": "B", "aspect_ratio": "16:9"},
                }
            ],
            "remove_edges": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "target_alias": "dynamic",
                    "target_input": "model.a",
                }
            ],
            "add_edges": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "target_alias": "dynamic",
                    "target_input": "model.b",
                }
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-selector-edge-swap",
        graph_hash="8" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    plan = result["plan"]
    assert plan["remove_edges"][0]["target"]["input"] == "model.a"
    assert plan["remove_edges"][0]["target"]["socket_index"] == 0
    assert plan["add_edges"][0]["target"]["input"] == "model.b"
    assert plan["add_edges"][0]["target"]["socket_index"] == 0
    assert plan["update_nodes"][0]["expected_values"] == {
        "model": "A",
        "model.aspect_ratio": "1:1",
    }
    assert plan["update_nodes"][0]["set_values"] == {
        "model": "B",
        "model.aspect_ratio": "16:9",
    }
    assert plan["expected_delta"] == {
        "created_node_count": 0,
        "updated_node_count": 1,
        "removed_node_count": 0,
        "added_edge_count": 1,
        "removed_edge_count": 1,
        "final_node_count": 2,
        "final_edge_count": 1,
    }
    reconstructed = graph_patch_request_from_apply(
        result["apply_request"],
        normalize_workflow_graph(workflow),
    )
    recompiled = compile_graph_patch(
        reconstructed,
        catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )
    assert recompiled["valid"] is True, recompiled["issues"]
    assert recompiled["patch_hash"] == result["patch_hash"]
    assert recompiled["plan"] == result["plan"]


def test_disconnected_dynamic_slot_does_not_shift_current_selector_recovery():
    catalog = _catalog()
    workflow = {
        "last_node_id": 2,
        "last_link_id": 0,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "DynamicSlotBeforeSelector",
                "order": 1,
                "inputs": [{"name": "payload", "type": "IMAGE", "link": None}],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                # The disconnected payload means its dependent strength widget is
                # absent; model is therefore the first serialized widget.
                "widgets_values": ["B", "high"],
            },
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-dynamic-slot-selector-0001",
            "existing_nodes": [
                {"alias": "source", "node_id": 1},
                {"alias": "dynamic", "node_id": 2},
            ],
            "update_nodes": [
                {
                    "target_alias": "dynamic",
                    "set_values": {"model": "A", "quality": "draft"},
                }
            ],
            "add_edges": [
                {
                    "source_alias": "source",
                    "source_output": "IMAGE",
                    "target_alias": "dynamic",
                    "target_input": "payload",
                }
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-dynamic-slot-selector",
        graph_hash="9" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is True, result["issues"]
    update = result["plan"]["update_nodes"][0]
    assert update["expected_values"] == {"model": "B"}
    assert update["set_values"] == {"model": "A", "model.quality": "draft"}
    edge = result["plan"]["add_edges"][0]
    assert edge["target"]["input"] == "payload"
    assert edge["target"]["socket_index"] == 0


def test_selected_selector_moves_only_the_exact_selected_duplicate_node():
    catalog = _catalog()
    workflow = {
        "last_node_id": 2,
        "last_link_id": 0,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "pos": [10, 20],
                "size": [210, 120],
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "EmptyImage",
                "order": 1,
                "pos": [100, 200],
                "size": [220, 130],
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": [512, 512, 1, 0],
            },
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-selected-layout-0001",
            "existing_nodes": [
                {"alias": "target", "selected": True, "node_type": "EmptyImage"}
            ],
            "update_nodes": [
                {"target_alias": "target", "move_by": {"x": 50, "y": -25}}
            ],
        }
    )

    def compile_with_selection(selected_node_ids):
        return compile_workflow_refinement_spec(
            request,
            workflow,
            workflow_identity="fl-mcp-selected-layout",
            graph_hash="a" * 64,
            catalog=catalog,
            catalog_hash=catalog_contract_hash(catalog),
            source="fixture",
            selected_node_ids=selected_node_ids,
        )

    selected = compile_with_selection([2])
    assert selected["valid"] is True, selected["issues"]
    assert selected["selection"][0]["selected"]["node_id"] == 2
    assert selected["plan"]["update_nodes"] == [
        {
            "ref": {"node_id": 2},
            "node_type": "EmptyImage",
            "schema_hash": selected["plan"]["update_nodes"][0]["schema_hash"],
            "expected_values": {},
            "set_values": {},
            "layout_hint": {
                "x": 150,
                "y": 175,
                "width": 220,
                "height": 130,
            },
        }
    ]
    assert selected["plan"]["assertions"]["nodes"][0]["ref"] == {"node_id": 2}

    none_selected = compile_with_selection([])
    assert none_selected["valid"] is False
    assert none_selected["issues"][0]["code"] == "existing_selector_no_match"

    multiple_selected = compile_with_selection([1, 2])
    assert multiple_selected["valid"] is False
    assert multiple_selected["issues"][0]["code"] == "existing_selector_ambiguous"


def test_live_wavelet_retry_payload_compiles_with_numeric_string_id_fallback():
    catalog = _catalog()
    workflow = {
        "last_node_id": 2,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "SaveImage",
                "order": 1,
                "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
                "outputs": [],
                "widgets_values": ["ren-pr34-simple"],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
        "groups": [],
        "config": {},
        "extra": {},
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "ren-pr34-wavelet-refine-v1",
            "existing_nodes": [
                {
                    "alias": "target_image",
                    "node_id": "1",
                    "node_type": "EmptyImage",
                    "occurrence": "only",
                },
                {
                    "alias": "save_image",
                    "node_id": "2",
                    "node_type": "SaveImage",
                    "occurrence": "only",
                },
            ],
            "create_nodes": [
                {
                    "alias": "source_image",
                    "capability": (
                        "Create exactly one additional native Empty Image source; no other "
                        "extra nodes."
                    ),
                    "requested_node_type": "EmptyImage",
                    "allowed_origins": ["native"],
                    "required_output_types": ["IMAGE"],
                    "values": {
                        "width": 512,
                        "height": 512,
                        "batch_size": 1,
                        "color": 8421504,
                    },
                },
                {
                    "alias": "wavelet_color_fix",
                    "capability": (
                        "Locally loaded Wavelet Color Fix node with IMAGE inputs named "
                        "source_image and target_image, an IMAGE output named image, and "
                        "align_method set to wavelet. Add no intermediary or utility nodes."
                    ),
                    "allowed_origins": ["custom", "native"],
                    "required_input_types": ["IMAGE"],
                    "required_output_types": ["IMAGE"],
                    "values": {"align_method": "wavelet"},
                },
            ],
            "update_nodes": [
                {
                    "target_alias": "save_image",
                    "expected_values": {"filename_prefix": "ren-pr34-simple"},
                    "set_values": {"filename_prefix": "ren-pr34-wavelet"},
                }
            ],
            "remove_edges": [
                {
                    "source_alias": "target_image",
                    "source_output": "IMAGE",
                    "target_alias": "save_image",
                    "target_input": "images",
                    "target_mode": "slot",
                }
            ],
            "add_edges": [
                {
                    "source_alias": "source_image",
                    "source_output": "IMAGE",
                    "target_alias": "wavelet_color_fix",
                    "target_input": "source_image",
                    "target_mode": "slot",
                },
                {
                    "source_alias": "target_image",
                    "source_output": "IMAGE",
                    "target_alias": "wavelet_color_fix",
                    "target_input": "target_image",
                    "target_mode": "slot",
                },
                {
                    "source_alias": "wavelet_color_fix",
                    "source_output": "image",
                    "target_alias": "save_image",
                    "target_input": "images",
                    "target_mode": "slot",
                },
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-live-wavelet-retry",
        graph_hash="d" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="live-fixture",
    )

    assert result["valid"] is True, result["issues"]
    assert [item["selected"]["node_id"] for item in result["selection"]] == [1, 2]
    assert result["resolution"]["selected_node_types"] == {
        "source_image": "EmptyImage",
        "wavelet_color_fix": "WaveletColorFix",
    }
    assert result["plan"]["expected_delta"] == {
        "created_node_count": 2,
        "updated_node_count": 1,
        "removed_node_count": 0,
        "added_edge_count": 3,
        "removed_edge_count": 1,
        "final_node_count": 4,
        "final_edge_count": 3,
    }
    assert result["plan"]["update_nodes"][0]["ref"] == {"node_id": 2}


def test_numeric_string_id_collision_remains_fail_closed():
    catalog = _catalog()
    workflow = {
        "last_node_id": 1,
        "last_link_id": 0,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": "1",
                "type": "SaveImage",
                "order": 1,
                "inputs": [{"name": "images", "type": "IMAGE", "link": None}],
                "outputs": [],
                "widgets_values": ["old-prefix"],
            },
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }

    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "typed-id-collision-save-image",
            "existing_nodes": [
                {
                    "alias": "target",
                    "node_id": "1",
                    "node_type": "SaveImage",
                }
            ],
            "update_nodes": [
                {
                    "target_alias": "target",
                    "expected_values": {"filename_prefix": "old-prefix"},
                    "set_values": {"filename_prefix": "new-prefix"},
                }
            ],
        }
    )
    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-typed-id-collision",
        graph_hash="e" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="fixture",
    )

    assert result["valid"] is False
    assert any(
        issue["code"] == "frontend_node_identity_collision"
        for issue in result["issues"]
    )


def test_live_resize_image_mask_build_and_branch_update_are_one_pass_deterministic():
    catalog = _catalog()
    catalog_hash = catalog_contract_hash(catalog)
    empty_workflow = {
        "last_node_id": 0,
        "last_link_id": 0,
        "nodes": [],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }

    build_results = []
    for selector_alias, source_output, source_output_index in (
        ({"resize_mode": "scale dimensions"}, "resized", None),
        ({"scale_dimensions": "dimensions"}, "IMAGE", None),
        ({"resize_mode": "scale dimensions"}, "image", 0),
    ):
        request = CompileWorkflowRefinementSpecRequest.model_validate(
            {
                "application_id": "live-resize-image-mask-build-v1",
                "create_nodes": [
                    {
                        "alias": "source",
                        "capability": "Create one native 512 by 512 empty image.",
                        "requested_node_type": "EmptyImage",
                        "values": {
                            "width": 512,
                            "height": 512,
                            "batch_size": 1,
                            "color": 0,
                        },
                    },
                    {
                        "alias": "resize",
                        "capability": (
                            "Use the exact loaded Resize Image/Mask node in scale "
                            "dimensions mode."
                        ),
                        "requested_node_type": "ResizeImageMaskNode",
                        "values": {
                            **selector_alias,
                            "width": 768,
                            "height": 512,
                            "crop": "center",
                            "scale_method": "area",
                        },
                    },
                    {
                        "alias": "save",
                        "capability": "Save the resized image.",
                        "requested_node_type": "SaveImage",
                        "values": {"filename_prefix": "ren-pr34-resize"},
                    },
                ],
                "add_edges": [
                    {
                        "source_alias": "source",
                        "source_output": "IMAGE",
                        "target_alias": "resize",
                        "target_input": "input",
                    },
                    {
                        "source_alias": "resize",
                        "source_output": source_output,
                        **(
                            {"source_output_index": source_output_index}
                            if source_output_index is not None
                            else {}
                        ),
                        "target_alias": "save",
                        "target_input": "images",
                    },
                ],
            }
        )
        build_results.append(
            compile_workflow_refinement_spec(
                request,
                empty_workflow,
                workflow_identity="fl-mcp-live-resize-build",
                graph_hash="f" * 64,
                catalog=catalog,
                catalog_hash=catalog_hash,
                source="persisted-live-fixture",
            )
        )

    for result in build_results:
        assert result["valid"] is True, result["issues"]
        assert result["resolution"]["selected_node_types"]["resize"] == (
            "ResizeImageMaskNode"
        )
        resize = next(
            item for item in result["plan"]["create_nodes"] if item["alias"] == "resize"
        )
        assert resize["values"] == {
            "resize_type": "scale dimensions",
            "resize_type.crop": "center",
            "resize_type.height": 512,
            "resize_type.width": 768,
            "scale_method": "area",
        }
        resize_input = next(
            edge
            for edge in result["plan"]["add_edges"]
            if edge["target"]["ref"] == {"alias": "resize"}
        )
        resize_output = next(
            edge
            for edge in result["plan"]["add_edges"]
            if edge["source"]["ref"] == {"alias": "resize"}
        )
        assert resize_input["source"]["type"] == "IMAGE"
        assert resize_input["target"]["type"] == "IMAGE"
        assert resize_output["source"] == {
            "ref": {"alias": "resize"},
            "output_index": 0,
            "output": "resized",
            "type": "IMAGE",
        }
        assert resize_output["target"]["type"] == "IMAGE"
    assert len({result["patch_hash"] for result in build_results}) == 1

    active_workflow = {
        "last_node_id": 3,
        "last_link_id": 2,
        "nodes": [
            {
                "id": 1,
                "type": "EmptyImage",
                "order": 0,
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                "widgets_values": [512, 512, 1, 0],
            },
            {
                "id": 2,
                "type": "ResizeImageMaskNode",
                "order": 1,
                "inputs": [
                    {"name": "input", "type": "IMAGE", "link": 1},
                    {
                        "name": "resize_type",
                        "type": "COMBO",
                        "widget": {"name": "resize_type"},
                        "link": None,
                    },
                    {
                        "name": "width",
                        "type": "INT",
                        "widget": {"name": "width"},
                        "link": None,
                    },
                    {
                        "name": "height",
                        "type": "INT",
                        "widget": {"name": "height"},
                        "link": None,
                    },
                    {
                        "name": "crop",
                        "type": "COMBO",
                        "widget": {"name": "crop"},
                        "link": None,
                    },
                    {
                        "name": "scale_method",
                        "type": "COMBO",
                        "widget": {"name": "scale_method"},
                        "link": None,
                    },
                ],
                "outputs": [{"name": "resized", "type": "IMAGE", "links": [2]}],
                "widgets_values": ["scale dimensions", 768, 512, "center", "area"],
            },
            {
                "id": 3,
                "type": "SaveImage",
                "order": 2,
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": 2},
                    {
                        "name": "filename_prefix",
                        "type": "STRING",
                        "widget": {"name": "filename_prefix"},
                        "link": None,
                    },
                ],
                "outputs": [],
                "widgets_values": ["ren-pr34-resize"],
            },
        ],
        "links": [
            [1, 1, 0, 2, 0, "IMAGE"],
            [2, 2, 0, 3, 0, "IMAGE"],
        ],
        "groups": [],
        "config": {},
        "extra": {},
    }

    update_results = []
    for selector_values in (
        {"resize_type": "scale by multiplier", "multiplier": 1.5},
        {"resize_mode": "scale by multiplier", "multiplier": 1.5},
    ):
        update_request = CompileWorkflowRefinementSpecRequest.model_validate(
            {
                "application_id": "live-resize-image-mask-update-v1",
                "existing_nodes": [
                    {
                        "alias": "resize",
                        "title_contains": "Resize Image/Mask",
                    }
                ],
                "update_nodes": [
                    {
                        "target_alias": "resize",
                        "set_values": selector_values,
                    }
                ],
            }
        )
        update_results.append(
            compile_workflow_refinement_spec(
                update_request,
                active_workflow,
                workflow_identity="fl-mcp-live-resize-update",
                graph_hash="e" * 64,
                catalog=catalog,
                catalog_hash=catalog_hash,
                source="persisted-live-fixture",
            )
        )

    for result in update_results:
        assert result["valid"] is True, result["issues"]
        assert result["selection"][0]["selected"]["node_id"] == 2
        assert result["plan"]["add_edges"] == []
        assert result["plan"]["remove_edges"] == []
        assert result["plan"]["expected_delta"] == {
            "created_node_count": 0,
            "updated_node_count": 1,
            "removed_node_count": 0,
            "added_edge_count": 0,
            "removed_edge_count": 0,
            "final_node_count": 3,
            "final_edge_count": 2,
        }
        update = result["plan"]["update_nodes"][0]
        assert update["expected_values"] == {"resize_type": "scale dimensions"}
        assert update["set_values"] == {
            "resize_type": "scale by multiplier",
            "resize_type.multiplier": 1.5,
        }
        assert not ({"width", "height", "crop"} & set(update["set_values"]))
        assert len(result["expected_final"]["edges"]) == 2
    assert update_results[0]["patch_hash"] == update_results[1]["patch_hash"]


def _converter_empty_workflow():
    return {
        "version": 0.4,
        "last_node_id": 0,
        "last_link_id": 0,
        "nodes": [],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
    }


def _conversion_schema(
    *,
    input_types=(),
    outputs=(),
    output_names=(),
    python_module="nodes",
    **metadata,
):
    required = {
        f"input_{index + 1}": [input_type]
        for index, input_type in enumerate(input_types)
    }
    return {
        "display_name": metadata.pop("display_name", "Synthetic conversion node"),
        "input": {"required": required},
        "output": list(outputs),
        "output_name": list(output_names or outputs),
        "python_module": python_module,
        **metadata,
    }


def _compile_converter_fixture(
    catalog,
    *,
    add_edges,
    create_nodes,
    allow_inferred_converters=True,
    verified_lessons=(),
):
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "semantic-converter-inference-fixture-v1",
            "create_nodes": create_nodes,
            "add_edges": add_edges,
            "allow_inferred_converters": allow_inferred_converters,
        }
    )
    return compile_workflow_refinement_spec(
        request,
        _converter_empty_workflow(),
        workflow_identity="fl-mcp-converter-inference-fixture",
        graph_hash="9" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="synthetic-converter-fixture",
        verified_lessons=verified_lessons,
    )


def _source_and_sink_nodes(sink_types=("READY",)):
    nodes = [
        {
            "alias": "source",
            "capability": "synthetic source",
            "requested_node_type": "SyntheticSource",
        }
    ]
    nodes.extend(
        {
            "alias": f"sink_{index + 1}",
            "capability": f"synthetic {sink_type} sink",
            "requested_node_type": f"{sink_type.title()}Sink",
        }
        for index, sink_type in enumerate(sink_types)
    )
    return nodes


def _source_to_sink_edges(source_type="RAW", sink_types=("READY",)):
    return [
        {
            "source_alias": "source",
            "source_output": source_type,
            "target_alias": f"sink_{index + 1}",
            "target_input": "value",
        }
        for index, _ in enumerate(sink_types)
    ]


def _basic_converter_catalog(*, converters=None, sink_types=("READY",)):
    catalog = {
        "SyntheticSource": _conversion_schema(
            outputs=("RAW",),
            output_names=("RAW",),
            display_name="Synthetic source",
        ),
    }
    for sink_type in sink_types:
        catalog[f"{sink_type.title()}Sink"] = {
            "display_name": f"{sink_type} sink",
            "input": {"required": {"value": [sink_type]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        }
    catalog.update(converters or {})
    return catalog


def test_converter_inference_is_additive_and_exact_no_extra_mode_is_fail_closed():
    direct_catalog = _basic_converter_catalog(sink_types=("RAW",))
    direct = _compile_converter_fixture(
        direct_catalog,
        create_nodes=_source_and_sink_nodes(sink_types=("RAW",)),
        add_edges=_source_to_sink_edges(sink_types=("RAW",)),
        allow_inferred_converters=False,
    )
    assert direct["valid"] is True, direct["issues"]
    assert direct["inferred_routes"] == []
    assert len(direct["plan"]["create_nodes"]) == 2
    assert len(direct["plan"]["add_edges"]) == 1

    mismatch_catalog = _basic_converter_catalog(
        converters={
            "RawToReady": _conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
            )
        }
    )
    mismatch = _compile_converter_fixture(
        mismatch_catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
        allow_inferred_converters=False,
    )
    assert mismatch["valid"] is False
    assert mismatch["apply_request"] is None
    assert mismatch["inferred_routes"][0]["status"] == "disabled"
    assert any(
        item["code"] == "converter_inference_disabled"
        for item in mismatch["issues"]
    )


def test_generic_one_hop_converter_is_synthesized_from_schema_types():
    catalog = _basic_converter_catalog(
        converters={
            "RawToReady": _conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
            )
        }
    )
    result = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )

    assert result["valid"] is True, result["issues"]
    assert result["inferred_routes"][0]["status"] == "resolved"
    selected = result["inferred_routes"][0]["selected"]
    assert [item["node_type"] for item in selected] == ["RawToReady"]
    inferred_alias = selected[0]["alias"]
    assert inferred_alias.startswith("inferred_converter_")
    assert {item["alias"] for item in result["plan"]["create_nodes"]} == {
        "source",
        "sink_1",
        inferred_alias,
    }
    assert len(result["plan"]["add_edges"]) == 2

    reversed_catalog_result = _compile_converter_fixture(
        dict(reversed(list(catalog.items()))),
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )
    assert reversed_catalog_result["valid"] is True, reversed_catalog_result["issues"]
    assert reversed_catalog_result["patch_hash"] == result["patch_hash"]
    assert reversed_catalog_result["plan"] == result["plan"]
    assert reversed_catalog_result["inferred_routes"] == result["inferred_routes"]


def test_list_source_selects_list_compatible_converter_order_independently():
    catalog = {
        "ListRawSource": {
            **_conversion_schema(
                outputs=("RAW",),
                output_names=("RAW",),
                display_name="List raw source",
            ),
            "output_is_list": [True],
        },
        "ReadySink": {
            "display_name": "Ready sink",
            "input": {"required": {"value": ["READY"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "ScalarRawToReady": _conversion_schema(
            input_types=("RAW",),
            outputs=("READY",),
            output_names=("ready",),
        ),
        "ListRawToReady": {
            **_conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
            ),
            "is_input_list": True,
        },
    }
    nodes = [
        {
            "alias": "source",
            "capability": "list raw source",
            "requested_node_type": "ListRawSource",
        },
        {
            "alias": "sink_1",
            "capability": "first ready sink",
            "requested_node_type": "ReadySink",
        },
        {
            "alias": "sink_2",
            "capability": "second ready sink",
            "requested_node_type": "ReadySink",
        },
    ]
    edges = _source_to_sink_edges(sink_types=("READY", "READY"))

    forward = _compile_converter_fixture(
        catalog,
        create_nodes=nodes,
        add_edges=edges,
    )
    reverse = _compile_converter_fixture(
        dict(reversed(list(catalog.items()))),
        create_nodes=list(reversed(nodes)),
        add_edges=list(reversed(edges)),
    )

    assert forward["valid"] is True, forward["issues"]
    selected = forward["inferred_routes"][0]["selected"]
    assert [item["node_type"] for item in selected] == ["ListRawToReady"]
    assert selected[0]["input_bindings"] == [
        {
            "path": "input_1",
            "type": "RAW",
            "source_cardinality": "list",
            "target_cardinality": "list",
            "cardinality_effect": "exact",
            "mode": "slot",
        }
    ]
    assert forward["inferred_routes"][0]["source"]["cardinality"] == "list"
    assert forward["inferred_routes"][0]["required_endpoints"] == [
        {"type": "READY", "cardinality": "scalar"}
    ]
    assert all(
        "cardinality" not in edge[endpoint]
        for edge in forward["plan"]["add_edges"]
        for endpoint in ("source", "target")
    )
    assert reverse["valid"] is True, reverse["issues"]
    assert reverse["patch_hash"] == forward["patch_hash"]
    assert reverse["plan"] == forward["plan"]
    assert reverse["inferred_routes"] == forward["inferred_routes"]


def _mapped_cardinality_catalog():
    return {
        "ListRawSource": {
            **_conversion_schema(
                outputs=("RAW",),
                output_names=("RAW",),
                display_name="List raw source",
            ),
            "output_is_list": [True],
        },
        "MappedPre": _conversion_schema(
            input_types=("RAW",),
            outputs=("MID",),
            output_names=("MID",),
            display_name="Mapped preprocessor",
        ),
        "DoneSink": {
            "display_name": "Done sink",
            "input": {"required": {"value": ["DONE"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "ScalarMidToDone": _conversion_schema(
            input_types=("MID",),
            outputs=("DONE",),
            output_names=("done",),
        ),
        "ListMidToDone": {
            **_conversion_schema(
                input_types=("MID",),
                outputs=("DONE",),
                output_names=("done",),
            ),
            "is_input_list": True,
        },
    }


def test_mapped_direct_predecessor_propagates_effective_list_cardinality():
    catalog = _mapped_cardinality_catalog()
    nodes = [
        {
            "alias": "source",
            "capability": "list raw source",
            "requested_node_type": "ListRawSource",
        },
        {
            "alias": "pre",
            "capability": "mapped preprocessor",
            "requested_node_type": "MappedPre",
        },
        {
            "alias": "sink_1",
            "capability": "done sink",
            "requested_node_type": "DoneSink",
        },
    ]
    edges = [
        {
            "source_alias": "source",
            "source_output": "RAW",
            "target_alias": "pre",
            "target_input": "input_1",
        },
        {
            "source_alias": "pre",
            "source_output": "MID",
            "target_alias": "sink_1",
            "target_input": "value",
        },
    ]

    forward = _compile_converter_fixture(
        catalog,
        create_nodes=nodes,
        add_edges=edges,
    )
    reverse = _compile_converter_fixture(
        dict(reversed(list(catalog.items()))),
        create_nodes=list(reversed(nodes)),
        add_edges=list(reversed(edges)),
    )

    assert forward["valid"] is True, forward["issues"]
    route = forward["inferred_routes"][0]
    assert route["source"]["cardinality"] == "list"
    assert [item["node_type"] for item in route["selected"]] == [
        "ListMidToDone"
    ]
    assert route["selected"][0]["input_bindings"][0][
        "cardinality_effect"
    ] == "exact"
    assert reverse["valid"] is True, reverse["issues"]
    assert reverse["patch_hash"] == forward["patch_hash"]
    assert reverse["plan"] == forward["plan"]
    assert reverse["inferred_routes"] == forward["inferred_routes"]


def test_retained_baseline_predecessor_propagates_effective_list_cardinality():
    catalog = _mapped_cardinality_catalog()
    workflow = {
        **_converter_empty_workflow(),
        "last_node_id": 2,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 1,
                "type": "ListRawSource",
                "inputs": [],
                "outputs": [
                    {"name": "RAW", "type": "RAW", "links": [1]}
                ],
                "widgets_values": [],
            },
            {
                "id": 2,
                "type": "MappedPre",
                "inputs": [
                    {"name": "input_1", "type": "RAW", "link": 1}
                ],
                "outputs": [
                    {"name": "MID", "type": "MID", "links": []}
                ],
                "widgets_values": [],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "RAW"]],
    }
    request = CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "baseline-effective-cardinality-fixture-v1",
            "existing_nodes": [{"alias": "pre", "node_id": 2}],
            "create_nodes": [
                {
                    "alias": "sink_1",
                    "capability": "done sink",
                    "requested_node_type": "DoneSink",
                }
            ],
            "add_edges": [
                {
                    "source_alias": "pre",
                    "source_output": "MID",
                    "target_alias": "sink_1",
                    "target_input": "value",
                }
            ],
        }
    )

    result = compile_workflow_refinement_spec(
        request,
        workflow,
        workflow_identity="fl-mcp-baseline-effective-cardinality",
        graph_hash="8" * 64,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source="baseline-effective-cardinality-fixture",
    )

    assert result["valid"] is True, result["issues"]
    route = result["inferred_routes"][0]
    assert route["source"]["cardinality"] == "list"
    assert [item["node_type"] for item in route["selected"]] == [
        "ListMidToDone"
    ]


def test_generic_two_hop_converter_route_is_synthesized_in_order():
    catalog = _basic_converter_catalog(
        converters={
            "RawToMiddle": _conversion_schema(
                input_types=("RAW",),
                outputs=("MIDDLE",),
                output_names=("middle",),
            ),
            "MiddleToReady": _conversion_schema(
                input_types=("MIDDLE",),
                outputs=("READY",),
                output_names=("ready",),
            ),
        }
    )
    result = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )

    assert result["valid"] is True, result["issues"]
    selected = result["inferred_routes"][0]["selected"]
    assert [item["node_type"] for item in selected] == [
        "RawToMiddle",
        "MiddleToReady",
    ]
    assert len(result["plan"]["create_nodes"]) == 4
    assert len(result["plan"]["add_edges"]) == 3


def test_native_create_video_is_inferred_with_stable_schema_defaults():
    catalog = {
        "ImageSource": _conversion_schema(
            outputs=("IMAGE",),
            output_names=("IMAGE",),
        ),
        "VideoSink": {
            "display_name": "Video sink",
            "input": {"required": {"video": ["VIDEO"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "CreateVideo": {
            "display_name": "Create Video",
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "fps": ["FLOAT", {"default": 30.0}],
                },
                "optional": {"audio": ["AUDIO"]},
            },
            "output": ["VIDEO"],
            "output_name": ["video"],
            "python_module": "comfy_extras.nodes_video_model",
        },
    }
    result = _compile_converter_fixture(
        catalog,
        create_nodes=[
            {
                "alias": "source",
                "capability": "image source",
                "requested_node_type": "ImageSource",
            },
            {
                "alias": "sink_1",
                "capability": "video sink",
                "requested_node_type": "VideoSink",
            },
        ],
        add_edges=[
            {
                "source_alias": "source",
                "source_output": "IMAGE",
                "target_alias": "sink_1",
                "target_input": "video",
            }
        ],
    )

    assert result["valid"] is True, result["issues"]
    selected = result["inferred_routes"][0]["selected"]
    assert [item["node_type"] for item in selected] == ["CreateVideo"]
    inferred_alias = selected[0]["alias"]
    inferred = next(
        item
        for item in result["plan"]["create_nodes"]
        if item["alias"] == inferred_alias
    )
    assert inferred["values"] == {"fps": 30.0}
    assert len(result["plan"]["add_edges"]) == 2

    # Model/tool JSON boundaries may rewrite the mathematically identical
    # integral float spelling from 30.0 to 30.  The compiler-generated hash
    # must still validate so the backend can perform its authoritative schema
    # recompile before any frontend mutation.
    apply_request = result["apply_request"]
    apply_inferred = next(
        item
        for item in apply_request["plan"]["create_nodes"]
        if item["alias"] == inferred_alias
    )
    apply_inferred["values"]["fps"] = 30
    validated = ApplyGraphPatchRequest.model_validate(apply_request)
    assert validated.patch_hash == result["patch_hash"]


def _video_bundle_fixture(add_edges):
    catalog = {
        "VideoSource": _conversion_schema(
            outputs=("VIDEO",),
            output_names=("VIDEO",),
        ),
        "ImageSink": {
            "display_name": "Image sink",
            "input": {"required": {"value": ["IMAGE"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "AudioSink": {
            "display_name": "Audio sink",
            "input": {"required": {"value": ["AUDIO"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "FloatSink": {
            "display_name": "Float sink",
            "input": {"required": {"value": ["FLOAT"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "GetVideoComponents": {
            "display_name": "Get Video Components",
            "input": {"required": {"video": ["VIDEO"]}},
            "output": ["IMAGE", "AUDIO", "FLOAT", "INT"],
            "output_name": ["images", "audio", "fps", "bit_depth"],
            "python_module": "comfy_extras.nodes_video",
        },
    }
    return _compile_converter_fixture(
        catalog,
        create_nodes=[
            {
                "alias": "source",
                "capability": "video source",
                "requested_node_type": "VideoSource",
            },
            {
                "alias": "sink_1",
                "capability": "image sink",
                "requested_node_type": "ImageSink",
            },
            {
                "alias": "sink_2",
                "capability": "audio sink",
                "requested_node_type": "AudioSink",
            },
            {
                "alias": "sink_3",
                "capability": "float sink",
                "requested_node_type": "FloatSink",
            },
        ],
        add_edges=add_edges,
    )


def test_multi_output_converter_is_shared_for_a_bundled_source_fan_out():
    edges = [
        {
            "source_alias": "source",
            "source_output": "VIDEO",
            "target_alias": "sink_1",
            "target_input": "value",
        },
        {
            "source_alias": "source",
            "source_output": "VIDEO",
            "target_alias": "sink_2",
            "target_input": "value",
        },
        {
            "source_alias": "source",
            "source_output": "VIDEO",
            "target_alias": "sink_3",
            "target_input": "value",
        },
    ]
    result = _video_bundle_fixture(edges)

    assert result["valid"] is True, result["issues"]
    selected = result["inferred_routes"][0]["selected"]
    assert [item["node_type"] for item in selected] == ["GetVideoComponents"]
    inferred_alias = selected[0]["alias"]
    inferred_nodes = [
        item
        for item in result["plan"]["create_nodes"]
        if item["alias"].startswith("inferred_converter_")
    ]
    assert [item["alias"] for item in inferred_nodes] == [inferred_alias]
    assert len(result["plan"]["add_edges"]) == 4
    assert {
        edge["source"]["output"]
        for edge in result["plan"]["add_edges"]
        if edge["source"]["ref"] == {"alias": inferred_alias}
    } == {"images", "audio", "fps"}

    reversed_result = _video_bundle_fixture(list(reversed(edges)))
    assert reversed_result["valid"] is True, reversed_result["issues"]
    assert reversed_result["patch_hash"] == result["patch_hash"]
    assert reversed_result["plan"] == result["plan"]
    assert reversed_result["inferred_routes"] == result["inferred_routes"]


def test_equal_best_converter_routes_require_an_explicit_choice():
    catalog = _basic_converter_catalog(
        converters={
            "RawToReadyA": _conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
            ),
            "RawToReadyB": _conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
            ),
        }
    )
    result = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )

    assert result["valid"] is False
    assert result["needs_choice"] is True
    assert result["apply_request"] is None
    assert result["inferred_routes"][0]["status"] == "needs_choice"
    assert [
        choice["node_types"]
        for choice in result["inferred_routes"][0]["choices"]
    ] == [["RawToReadyA"], ["RawToReadyB"]]
    issue = next(
        item
        for item in result["issues"]
        if item["code"] == "ambiguous_conversion_route"
    )
    assert issue["route_candidates"] == result["inferred_routes"][0]["choices"]


def _union_target_converter_fixture(*, include_mask_converter):
    converters = {
        "RawToImage": _conversion_schema(
            input_types=("RAW",),
            outputs=("IMAGE",),
            output_names=("image",),
        )
    }
    if include_mask_converter:
        converters["RawToMask"] = _conversion_schema(
            input_types=("RAW",),
            outputs=("MASK",),
            output_names=("mask",),
        )
    catalog = {
        "SyntheticSource": _conversion_schema(
            outputs=("RAW",),
            output_names=("RAW",),
        ),
        "ImageOrMaskSink": {
            "display_name": "Image or mask sink",
            "input": {"required": {"value": ["IMAGE,MASK", {}]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        **converters,
    }
    return _compile_converter_fixture(
        catalog,
        create_nodes=[
            {
                "alias": "source",
                "capability": "raw source",
                "requested_node_type": "SyntheticSource",
            },
            {
                "alias": "sink_1",
                "capability": "image or mask sink",
                "requested_node_type": "ImageOrMaskSink",
            },
        ],
        add_edges=_source_to_sink_edges(),
    )


def test_union_target_uses_the_unique_safe_routable_type():
    result = _union_target_converter_fixture(include_mask_converter=False)

    assert result["valid"] is True, result["issues"]
    assert result["needs_choice"] is False
    assert result["inferred_routes"][0]["required_types"] == ["IMAGE"]
    assert result["inferred_routes"][0]["selected"][0]["node_type"] == "RawToImage"
    final_edge = next(
        item
        for item in result["plan"]["add_edges"]
        if item["target"]["ref"] == {"alias": "sink_1"}
    )
    assert final_edge["source"]["output"] == "image"
    assert final_edge["source"]["type"] == "IMAGE"
    assert final_edge["target"]["type"] == "IMAGE"


def test_union_target_with_equal_safe_alternatives_requires_choice():
    result = _union_target_converter_fixture(include_mask_converter=True)

    assert result["valid"] is False
    assert result["needs_choice"] is True
    assert result["apply_request"] is None
    assert result["inferred_routes"][0]["status"] == "needs_choice"
    assert {
        (
            tuple(item["node_types"]),
            tuple(target["type"] for target in item["target_types"]),
        )
        for item in result["inferred_routes"][0]["choices"]
    } == {
        (("RawToImage",), ("IMAGE",)),
        (("RawToMask",), ("MASK",)),
    }


def test_converter_needing_another_source_branch_fails_with_explicit_diagnostic():
    catalog = _basic_converter_catalog(
        converters={
            "RawAndControlToReady": _conversion_schema(
                input_types=("RAW", "CONTROL"),
                outputs=("READY",),
                output_names=("ready",),
            )
        }
    )
    result = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    issue = next(
        item
        for item in result["issues"]
        if item["code"] == "conversion_route_requires_explicit_side_inputs"
    )
    assert "side-input edges" in issue["message"]
    rejection = next(
        item
        for item in issue["rejections"]
        if item["node_type"] == "RawAndControlToReady"
    )
    assert rejection["goal_types_produced"] == ["READY"]
    assert rejection["missing_input_types"] == ["CONTROL"]


def test_inferred_converter_does_not_fan_one_source_into_repeated_type_inputs():
    catalog = _basic_converter_catalog(
        converters={
            "TwoRawInputsToReady": _conversion_schema(
                input_types=("RAW", "RAW"),
                outputs=("READY",),
                output_names=("ready",),
            )
        }
    )
    result = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    issue = next(
        item
        for item in result["issues"]
        if item["code"] == "conversion_route_requires_explicit_input_mappings"
    )
    assert "map those inputs explicitly" in issue["message"]
    rejected = result["inferred_routes"][0]["rejected_implicit_bindings"]
    assert rejected[0]["node_types"] == ["TwoRawInputsToReady"]
    assert rejected[0]["bindings"] == [
        {
            "node_type": "TwoRawInputsToReady",
            "input_type": "RAW",
            "input_paths": ["input_1", "input_2"],
            "bindings": [
                {
                    "path": "input_1",
                    "source_cardinality": "scalar",
                    "target_cardinality": "scalar",
                    "cardinality_effect": "exact",
                },
                {
                    "path": "input_2",
                    "source_cardinality": "scalar",
                    "target_cardinality": "scalar",
                    "cardinality_effect": "exact",
                },
            ],
        }
    ]


def test_inferred_converter_synthesizes_the_exact_nondefault_dynamic_branch():
    catalog = {
        "MaskSource": _conversion_schema(
            outputs=("MASK",),
            output_names=("MASK",),
        ),
        "ReadySink": {
            "display_name": "Ready sink",
            "input": {"required": {"value": ["READY"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "BranchingTransform": {
            "display_name": "Branching Transform",
            "input": {
                "required": {
                    "mode": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {
                            "options": [
                                {
                                    "key": "image mode",
                                    "inputs": {
                                        "required": {"payload": ["IMAGE"]}
                                    },
                                },
                                {
                                    "key": "mask mode",
                                    "inputs": {
                                        "required": {"payload": ["MASK"]}
                                    },
                                },
                            ]
                        },
                    ]
                }
            },
            "output": ["READY"],
            "output_name": ["ready"],
            "python_module": "nodes",
        },
    }
    result = _compile_converter_fixture(
        catalog,
        create_nodes=[
            {
                "alias": "source",
                "capability": "mask source",
                "requested_node_type": "MaskSource",
            },
            {
                "alias": "sink_1",
                "capability": "ready sink",
                "requested_node_type": "ReadySink",
            },
        ],
        add_edges=[
            {
                "source_alias": "source",
                "source_output": "MASK",
                "target_alias": "sink_1",
                "target_input": "value",
            }
        ],
    )

    assert result["valid"] is True, result["issues"]
    selected = result["inferred_routes"][0]["selected"][0]
    assert selected["node_type"] == "BranchingTransform"
    assert selected["selector_values"] == [
        {"path": "mode", "value": "mask mode"}
    ]
    route_choice = result["inferred_routes"][0]["choices"][0]
    assert route_choice["cost"]["nondefault_selector_count"] == 1
    assert route_choice["selector_values"] == [
        {
            "node_type": "BranchingTransform",
            "values": [
                {
                    "path": "mode",
                    "value": "mask mode",
                    "provenance": "dynamic_selector_branch",
                }
            ],
        }
    ]
    inferred = next(
        item
        for item in result["plan"]["create_nodes"]
        if item["alias"] == selected["alias"]
    )
    assert inferred["values"] == {"mode": "mask mode"}


def test_independent_sibling_converter_permutations_compile_once_canonically():
    catalog = {
        "SyntheticSource": _conversion_schema(
            outputs=("RAW",),
            output_names=("RAW",),
        ),
        "AlphaSink": {
            "display_name": "Alpha sink",
            "input": {"required": {"value": ["ALPHA"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "BetaSink": {
            "display_name": "Beta sink",
            "input": {"required": {"value": ["BETA"]}},
            "output": [],
            "output_name": [],
            "python_module": "nodes",
            "output_node": True,
        },
        "RawToAlpha": _conversion_schema(
            input_types=("RAW",),
            outputs=("ALPHA",),
            output_names=("alpha",),
        ),
        "RawToBeta": _conversion_schema(
            input_types=("RAW",),
            outputs=("BETA",),
            output_names=("beta",),
        ),
    }
    create_nodes = [
        {
            "alias": "source",
            "capability": "raw source",
            "requested_node_type": "SyntheticSource",
        },
        {
            "alias": "sink_1",
            "capability": "alpha sink",
            "requested_node_type": "AlphaSink",
        },
        {
            "alias": "sink_2",
            "capability": "beta sink",
            "requested_node_type": "BetaSink",
        },
    ]
    edges = [
        {
            "source_alias": "source",
            "source_output": "RAW",
            "target_alias": "sink_1",
            "target_input": "value",
        },
        {
            "source_alias": "source",
            "source_output": "RAW",
            "target_alias": "sink_2",
            "target_input": "value",
        },
    ]
    forward = _compile_converter_fixture(
        catalog,
        create_nodes=create_nodes,
        add_edges=edges,
    )
    reverse = _compile_converter_fixture(
        dict(reversed(list(catalog.items()))),
        create_nodes=create_nodes,
        add_edges=list(reversed(edges)),
    )

    assert forward["valid"] is True, forward["issues"]
    assert forward["needs_choice"] is False
    assert [
        item["node_type"] for item in forward["inferred_routes"][0]["selected"]
    ] == ["RawToAlpha", "RawToBeta"]
    assert len(forward["plan"]["create_nodes"]) == 5
    assert len(forward["plan"]["add_edges"]) == 4
    assert reverse["valid"] is True, reverse["issues"]
    assert reverse["patch_hash"] == forward["patch_hash"]
    assert reverse["plan"] == forward["plan"]
    assert reverse["inferred_routes"] == forward["inferred_routes"]


def test_verified_schema_lesson_ranks_an_otherwise_more_expensive_route():
    catalog = _basic_converter_catalog(
        converters={
            "NativeRawToReady": _conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
                python_module="nodes",
            ),
            "CustomRawToReady": _conversion_schema(
                input_types=("RAW",),
                outputs=("READY",),
                output_names=("ready",),
                python_module="custom_nodes.synthetic_converter",
            ),
        }
    )
    without_lesson = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )
    assert without_lesson["valid"] is True, without_lesson["issues"]
    assert without_lesson["inferred_routes"][0]["selected"][0]["node_type"] == (
        "NativeRawToReady"
    )

    lesson = VerifiedCapabilityLesson(
        node_type="CustomRawToReady",
        schema_hash=node_schema_hash(
            "CustomRawToReady", catalog["CustomRawToReady"]
        ),
        payload={
            "evidence": "atomic_graph_patch_application",
            "source_node_type": "SyntheticSource",
            "source_schema_hash": node_schema_hash(
                "SyntheticSource", catalog["SyntheticSource"]
            ),
            "target_node_type": "CustomRawToReady",
            "target_schema_hash": node_schema_hash(
                "CustomRawToReady", catalog["CustomRawToReady"]
            ),
        },
    )
    with_lesson = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
        verified_lessons=(lesson,),
    )
    assert with_lesson["valid"] is True, with_lesson["issues"]
    selected = with_lesson["inferred_routes"][0]["selected"][0]
    assert selected["node_type"] == "CustomRawToReady"
    assert selected["verified_lesson_count"] == 1
    assert with_lesson["inferred_routes"][0]["accepted_verified_lesson_count"] == 1


def test_allow_inferred_converters_is_a_strict_boolean():
    with pytest.raises(ValueError):
        CompileWorkflowRefinementSpecRequest.model_validate(
            {
                "application_id": "strict-converter-flag-v1",
                "create_nodes": [
                    {
                        "alias": "source",
                        "capability": "source",
                        "requested_node_type": "SyntheticSource",
                    }
                ],
                "allow_inferred_converters": "false",
            }
        )


@pytest.mark.parametrize(
    ("schema_overrides", "expected_code"),
    [
        (
            {
                "python_module": "comfy_api_nodes.synthetic",
                "api_node": True,
                "category": "partner/transform",
            },
            "partner_node_excluded",
        ),
        (
            {"description": "A heavyweight diffusion model image generator"},
            "heavy_node_excluded",
        ),
        ({"output_node": True}, "output_node_excluded"),
        ({"python_module": "mystery.synthetic"}, "unknown_origin_excluded"),
        ({"deprecated": True}, "deprecated_node"),
        ({"experimental": True}, "experimental_node"),
    ],
)
def test_compiler_inference_keeps_risky_intermediaries_excluded(
    schema_overrides, expected_code
):
    converter = _conversion_schema(
        input_types=("RAW",),
        outputs=("READY",),
        output_names=("ready",),
    )
    converter.update(schema_overrides)
    catalog = _basic_converter_catalog(
        converters={"RiskyRawToReady": converter}
    )
    result = _compile_converter_fixture(
        catalog,
        create_nodes=_source_and_sink_nodes(),
        add_edges=_source_to_sink_edges(),
    )

    assert result["valid"] is False
    assert result["apply_request"] is None
    assert result["inferred_routes"][0]["status"] == "unresolved"
    assert expected_code in result["inferred_routes"][0]["issue_codes"]
