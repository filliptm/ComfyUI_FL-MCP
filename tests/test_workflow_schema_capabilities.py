from workflow_schema_capabilities import (
    SCHEMA_CAPABILITY_CONTRACT,
    classify_connection,
    classify_schema_compatibility,
    infer_dynamic_selector_values,
    materialize_inputs,
    normalize_node_schema,
    stable_default_for_spec,
)


def _video_components_schema():
    return {
        "input": {"required": {"video": ["VIDEO", {"tooltip": "Source video"}]}},
        "input_order": {"required": ["video"]},
        "is_input_list": False,
        "output": ["IMAGE", "AUDIO", "FLOAT", "INT"],
        "output_name": ["images", "audio", "fps", "bit_depth"],
        "output_is_list": [False, False, False, False],
        "output_matchtypes": None,
    }


def _nano_schema():
    return {
        "input": {
            "required": {
                "prompt": ["STRING", {"default": ""}],
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "Nano Banana 2",
                                "inputs": {
                                    "required": {
                                        "resolution": [
                                            "COMBO",
                                            {"options": ["1K", "2K", "4K"]},
                                        ],
                                        "images": [
                                            "COMFY_AUTOGROW_V3",
                                            {
                                                "template": {
                                                    "input": {
                                                        "required": {"image": ["IMAGE", {}]}
                                                    },
                                                    "min": 0,
                                                    "names": ["image_1", "image_2", "image_3"],
                                                }
                                            },
                                        ],
                                    },
                                    "optional": {"files": ["GEMINI_INPUT_FILES", {}]},
                                },
                            },
                            {
                                "key": "Nano Banana 2 Lite",
                                "inputs": {
                                    "required": {
                                        "resolution": [
                                            "COMBO",
                                            {"options": ["1K"], "default": "1K"},
                                        ],
                                        "images": [
                                            "COMFY_AUTOGROW_V3",
                                            {
                                                "template": {
                                                    "input": {
                                                        "required": {"image": ["IMAGE", {}]}
                                                    },
                                                    "min": 0,
                                                    "names": ["image_1", "image_2"],
                                                }
                                            },
                                        ],
                                    }
                                },
                            },
                        ]
                    },
                ],
            },
            "hidden": {
                "auth_token_comfy_org": ["AUTH_TOKEN_COMFY_ORG"],
                "api_key_comfy_org": ["API_KEY_COMFY_ORG"],
                "unique_id": ["UNIQUE_ID"],
                "comfy_usage_source": ["COMFY_USAGE_SOURCE"],
            },
        },
        "input_order": {
            "required": ["prompt", "model"],
            "hidden": [
                "auth_token_comfy_org",
                "api_key_comfy_org",
                "unique_id",
                "comfy_usage_source",
            ],
        },
        "is_input_list": False,
        "output": ["IMAGE", "STRING", "IMAGE"],
        "output_name": ["IMAGE", "STRING", "thought_image"],
        "output_is_list": [False, False, False],
    }


def _switch_schema():
    return {
        "input": {
            "required": {
                "switch": ["BOOLEAN", {}],
                "on_false": [
                    "COMFY_MATCHTYPE_V3",
                    {"template": {"allowed_types": "*", "template_id": "switch"}},
                ],
                "on_true": [
                    "COMFY_MATCHTYPE_V3",
                    {"template": {"allowed_types": "*", "template_id": "switch"}},
                ],
            }
        },
        "input_order": {"required": ["switch", "on_false", "on_true"]},
        "is_input_list": False,
        "output": ["COMFY_MATCHTYPE_V3"],
        "output_name": ["output"],
        "output_matchtypes": ["switch"],
        "output_is_list": [False],
    }


def _create_list_schema():
    return {
        "input": {
            "required": {
                "inputs": [
                    "COMFY_AUTOGROW_V3",
                    {
                        "template": {
                            "input": {
                                "required": {
                                    "input": [
                                        "COMFY_MATCHTYPE_V3",
                                        {
                                            "template": {
                                                "allowed_types": "*",
                                                "template_id": "type",
                                            }
                                        },
                                    ]
                                }
                            },
                            "max": 10,
                            "min": 1,
                            "prefix": "input",
                        }
                    },
                ]
            }
        },
        "is_input_list": True,
        "output": ["COMFY_MATCHTYPE_V3"],
        "output_name": ["list"],
        "output_matchtypes": ["type"],
        "output_is_list": [True],
    }


def _resize_schema():
    return {
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
                                        "width": ["INT", {"default": 512}],
                                        "height": ["INT", {"default": 512}],
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
                        ]
                    },
                ],
                "scale_method": [
                    "COMBO",
                    {"options": ["area", "lanczos"], "default": "area"},
                ],
            }
        },
        "input_order": {"required": ["input", "resize_type", "scale_method"]},
        "is_input_list": False,
        "output": ["COMFY_MATCHTYPE_V3"],
        "output_name": ["resized"],
        "output_matchtypes": ["input_type"],
        "output_is_list": [False],
    }


def test_vhs_convertible_frame_rate_and_video_component_output_indexes():
    vhs = normalize_node_schema(
        "VHS_VideoCombine",
        {
            "input": {
                "required": {
                    "images": ["IMAGE", {}],
                    "frame_rate": ["FLOAT", {"default": 8, "min": 1, "step": 1}],
                },
                # Real VHS nodes still use bare strings for hidden context inputs.
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
            },
            "input_order": {
                "required": ["images", "frame_rate"],
                "hidden": ["prompt", "extra_pnginfo"],
            },
            "output": ["VHS_FILENAMES"],
            "output_name": ["Filenames"],
            "output_is_list": [False],
        },
    )
    frame_rate = vhs.inputs_named("frame_rate")[0]
    assert vhs.contract == SCHEMA_CAPABILITY_CONTRACT
    assert classify_schema_compatibility(vhs).status == "supported"
    assert frame_rate.kind == "widget"
    assert frame_rate.widget is True
    assert frame_rate.widget_convertible is True
    assert frame_rate.connectable is False
    assert frame_rate.accepted_types == ("FLOAT",)
    assert frame_rate.default.available is True
    assert frame_rate.default.value == 8
    assert frame_rate.default.provenance == "schema_default"
    assert [item.hidden_kind for item in vhs.inputs if item.hidden] == ["context", "context"]

    video = normalize_node_schema("GetVideoComponents", _video_components_schema())
    assert [(item.index, item.name, item.produced_types) for item in video.outputs] == [
        (0, "images", ("IMAGE",)),
        (1, "audio", ("AUDIO",)),
        (2, "fps", ("FLOAT",)),
        (3, "bit_depth", ("INT",)),
    ]
    compatibility = classify_connection(video.outputs[2], frame_rate)
    assert compatibility.status == "adapter_required"
    assert {reason.code for reason in compatibility.reasons} == {
        "widget_conversion_required"
    }


def test_nano_dynamic_selector_dotted_inputs_autogrow_and_hidden_context():
    nano = normalize_node_schema("GeminiNanoBanana2V2", _nano_schema())

    assert nano.classification.status == "supported"
    selector = nano.inputs_named("model")[0]
    assert selector.kind == "dynamic_selector"
    assert selector.default.value == "Nano Banana 2"
    assert selector.default.provenance == "dynamic_option_first"
    assert {group.kind for group in nano.dynamic_groups} == {
        "dynamic_combo",
        "autogrow",
    }

    defaults = materialize_inputs(nano)
    default_paths = [item.capability.path for item in defaults]
    assert "model.resolution" in default_paths
    assert "model.images.image_1" in default_paths
    assert "model.images.image_3" in default_paths
    assert len([path for path in default_paths if path == "model.resolution"]) == 1
    image_1 = next(
        item for item in defaults if item.capability.path == "model.images.image_1"
    )
    assert image_1.activation_state == "active"
    assert image_1.socket_index == 0
    autogrow_rule = next(
        rule for rule in image_1.capability.activation if rule.kind == "autogrow_slot"
    )
    assert (autogrow_rule.ordinal, autogrow_rule.minimum, autogrow_rule.maximum) == (
        0,
        0,
        3,
    )

    lite = materialize_inputs(nano, values={"model": "Nano Banana 2 Lite"})
    lite_paths = [item.capability.path for item in lite]
    assert "model.images.image_2" in lite_paths
    assert "model.images.image_3" not in lite_paths
    resolution = [
        item.capability
        for item in lite
        if item.capability.path == "model.resolution"
    ]
    assert len(resolution) == 1
    assert resolution[0].default.value == "1K"
    assert resolution[0].duplicate_name is True

    hidden = {item.path: item.hidden_kind for item in nano.inputs if item.hidden}
    assert hidden == {
        "api_key_comfy_org": "auth",
        "auth_token_comfy_org": "auth",
        "comfy_usage_source": "usage",
        "unique_id": "context",
    }


def test_serialized_widgets_recover_the_active_multi_option_selector():
    nano = normalize_node_schema("GeminiNanoBanana2V2", _nano_schema())
    observed = ["prompt", "Nano Banana 2 Lite", "1K"]

    selector_values = infer_dynamic_selector_values(nano, observed)
    assert selector_values == {"model": "Nano Banana 2 Lite"}
    paths = {
        item.capability.path
        for item in materialize_inputs(nano, values=selector_values)
    }
    assert "model.images.image_2" in paths
    assert "model.images.image_3" not in paths


def test_autogrow_schema_expansion_is_bounded_before_allocating_slots():
    schema = {
        "input": {
            "required": {
                "images": [
                    "COMFY_AUTOGROW_V3",
                    {
                        "template": {
                            "input": {"required": {"image": ["IMAGE", {}]}},
                            "prefix": "image_",
                            "max": 10_001,
                        }
                    },
                ]
            }
        },
        "output": [],
    }

    normalized = normalize_node_schema("UnboundedAutogrow", schema)
    assert normalized.classification.status == "unsupported"
    assert "dynamic_input_limit_exceeded" in {
        reason.code for reason in normalized.classification.reasons
    }
    assert normalized.inputs == ()


def test_dynamic_slot_records_connection_activated_dependent_inputs():
    capabilities = normalize_node_schema(
        "DynamicSlotNode",
        {
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
                    ]
                }
            },
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        },
    )
    payload = capabilities.inputs_named("payload")[0]
    strength = capabilities.inputs_named("payload.strength")[0]
    assert payload.kind == "dynamic_slot"
    assert payload.force_input is True
    assert payload.connectable is True
    assert strength.activation[0].kind == "input_connected"
    assert strength.activation[0].source == "payload"
    disconnected = materialize_inputs(capabilities, connected_inputs=set())
    assert "payload.strength" not in [item.capability.path for item in disconnected]
    connected = materialize_inputs(capabilities, connected_inputs={"payload"})
    assert "payload.strength" in [item.capability.path for item in connected]


def test_matchtype_switch_binds_concrete_edges_but_requires_graph_adapter():
    switch = normalize_node_schema("ComfySwitchNode", _switch_schema())
    assert switch.classification.status == "adapter_required"
    assert {reason.code for reason in switch.classification.reasons} == {
        "graph_scoped_matchtype"
    }
    on_true = switch.inputs_named("on_true")[0]
    output = switch.outputs[0]
    assert on_true.matchtype.template_id == "switch"
    assert on_true.matchtype.allowed_types == ("*",)
    assert output.matchtype_template_id == "switch"
    assert output.matchtype_allowed_types == ("*",)

    image_source = normalize_node_schema(
        "ImageSource",
        {"input": {}, "output": ["IMAGE"], "output_name": ["IMAGE"]},
    )
    edge = classify_connection(
        image_source.outputs[0],
        on_true,
        target_binding_key="switch-node-42:switch",
    )
    assert edge.status == "supported"
    assert edge.type_bindings == {"switch-node-42:switch": "IMAGE"}
    unresolved = classify_connection(output, on_true)
    assert unresolved.status == "adapter_required"
    assert "unresolved_matchtype_edge" in {
        reason.code for reason in unresolved.reasons
    }


def test_create_list_preserves_autogrow_indexes_and_list_cardinality():
    create_list = normalize_node_schema("CreateList", _create_list_schema())
    assert create_list.classification.status == "adapter_required"
    materialized = materialize_inputs(create_list)
    assert [item.capability.path for item in materialized] == [
        f"inputs.input{index}" for index in range(10)
    ]
    assert [item.socket_index for item in materialized] == list(range(10))
    assert materialized[0].capability.required is True
    assert all(item.capability.cardinality == "list" for item in materialized)
    assert create_list.outputs[0].cardinality == "list"
    assert create_list.outputs[0].matchtype_template_id == "type"


def test_resize_matchtype_allowed_types_and_dynamic_branch_materialization():
    resize = normalize_node_schema("ResizeImageMaskNode", _resize_schema())
    assert resize.classification.status == "adapter_required"
    input_slot = resize.inputs_named("input")[0]
    assert input_slot.matchtype.allowed_types == ("IMAGE", "MASK")
    assert resize.outputs[0].matchtype_template_id == "input_type"
    assert resize.outputs[0].matchtype_allowed_types == ("IMAGE", "MASK")

    default_branch = materialize_inputs(resize)
    default_paths = [item.capability.path for item in default_branch]
    assert "resize_type.width" in default_paths
    assert "resize_type.height" in default_paths
    assert "resize_type.match" not in default_paths
    match_branch = materialize_inputs(resize, values={"resize_type": "match size"})
    match_paths = [item.capability.path for item in match_branch]
    assert "resize_type.match" in match_paths
    assert "resize_type.crop" in match_paths
    assert "resize_type.width" not in match_paths

    latent_source = normalize_node_schema(
        "LatentSource",
        {"input": {}, "output": ["LATENT"], "output_name": ["LATENT"]},
    )
    rejected = classify_connection(
        latent_source.outputs[0],
        input_slot,
        target_binding_key="resize-7:input_type",
    )
    assert rejected.status == "unsupported"
    assert "matchtype_input_rejects_type" in {
        reason.code for reason in rejected.reasons
    }
    conflicting_binding = classify_connection(
        latent_source.outputs[0],
        input_slot,
        type_bindings={"resize-7:input_type": "LATENT"},
        target_binding_key="resize-7:input_type",
    )
    assert conflicting_binding.status == "unsupported"
    assert "matchtype_binding_conflict" in {
        reason.code for reason in conflicting_binding.reasons
    }


def test_force_input_duplicate_outputs_defaults_and_unknown_adapters_are_explicit():
    capabilities = normalize_node_schema(
        "MixedNode",
        {
            "input": {
                "required": {
                    "forced_seed": ["INT", {"forceInput": True}],
                    "mode": ["COMBO", {"options": ["first", "second"]}],
                }
            },
            "output": ["IMAGE", "IMAGE"],
            "output_name": ["IMAGE", "IMAGE"],
            "output_is_list": [False, True],
        },
    )
    forced = capabilities.inputs_named("forced_seed")[0]
    assert forced.kind == "socket"
    assert forced.force_input is True
    assert forced.widget is False
    assert forced.connectable is True
    assert [output.index for output in capabilities.outputs_named("IMAGE")] == [0, 1]
    assert all(output.duplicate_name for output in capabilities.outputs)
    assert [output.cardinality for output in capabilities.outputs] == ["scalar", "list"]
    mode = capabilities.inputs_named("mode")[0]
    assert mode.default.value == "first"
    assert mode.default.provenance == "combo_option_first"
    assert stable_default_for_spec([["a", "b"], {}]).provenance == "legacy_enum_first"

    unsupported = normalize_node_schema(
        "FutureNode",
        {
            "input": {"required": {"future": ["COMFY_FUTURE_V9", {}]}},
            "output": [],
        },
    )
    assert unsupported.classification.status == "unsupported"
    assert [reason.code for reason in unsupported.classification.reasons] == [
        "unknown_comfy_input_type"
    ]


def test_list_scalar_connection_uses_native_comfy_execution_mapping():
    source = normalize_node_schema(
        "ListSource",
        {
            "input": {},
            "output": ["IMAGE"],
            "output_name": ["images"],
            "output_is_list": [True],
        },
    )
    target = normalize_node_schema(
        "ImageTarget",
        {"input": {"required": {"image": ["IMAGE", {}]}}, "output": []},
    )
    result = classify_connection(source.outputs[0], target.inputs_named("image")[0])
    assert result.status == "supported"
    assert result.reasons == ()
