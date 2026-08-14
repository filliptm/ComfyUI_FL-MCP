import copy

from backend.workflow_resolver import (
    WORKFLOW_SPEC_RESOLUTION_SCHEMA,
    ResolveWorkflowSpecRequest,
    WorkflowCapabilitySpec,
    resolve_workflow_spec,
)

CATALOG_HASH = "a" * 64


def node_info(
    *,
    display_name: str,
    module: str = "nodes",
    category: str = "image",
    description: str = "",
    inputs: dict | None = None,
    outputs: list[str] | None = None,
    **extra,
):
    return {
        "display_name": display_name,
        "python_module": module,
        "category": category,
        "description": description,
        "input": inputs or {"required": {"image": ["IMAGE", {}]}},
        "output": outputs or ["IMAGE"],
        "output_name": outputs or ["IMAGE"],
        **extra,
    }


def live_resize_image_mask_node_info():
    """Relevant fields from the locally loaded ResizeImageMaskNode schema."""

    return {
        "api_node": False,
        "category": "image/transform",
        "deprecated": False,
        "description": "Resize an image or mask using various scaling methods.",
        "display_name": "Resize Image/Mask",
        "experimental": False,
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
                                        "crop": [
                                            "COMBO",
                                            {
                                                "default": "center",
                                                "options": ["disabled", "center"],
                                            },
                                        ],
                                        "height": ["INT", {"default": 512}],
                                        "width": ["INT", {"default": 512}],
                                    }
                                },
                            },
                            {
                                "key": "scale by multiplier",
                                "inputs": {
                                    "required": {
                                        "multiplier": ["FLOAT", {"default": 1.0}]
                                    }
                                },
                            },
                            {
                                "key": "match size",
                                "inputs": {
                                    "required": {
                                        "crop": [
                                            "COMBO",
                                            {
                                                "default": "center",
                                                "options": ["disabled", "center"],
                                            },
                                        ],
                                        "match": ["IMAGE,MASK", {}],
                                    }
                                },
                            },
                        ]
                    },
                ],
                "scale_method": [
                    "COMBO",
                    {
                        "default": "area",
                        "options": [
                            "nearest-exact",
                            "bilinear",
                            "area",
                            "bicubic",
                            "lanczos",
                        ],
                    },
                ],
            }
        },
        "input_order": {"required": ["input", "resize_type", "scale_method"]},
        "is_input_list": False,
        "output": ["COMFY_MATCHTYPE_V3"],
        "output_is_list": [False],
        "output_matchtypes": ["input_type"],
        "output_name": ["resized"],
        "python_module": "comfy_extras.nodes_post_processing",
        "search_aliases": [
            "resize",
            "resize image",
            "resize mask",
            "scale",
            "scale image",
            "scale mask",
            "image resize",
            "change size",
            "dimensions",
            "shrink",
            "enlarge",
        ],
    }


def resolve(capabilities, catalog, *, expected_catalog_hash=CATALOG_HASH):
    return resolve_workflow_spec(
        ResolveWorkflowSpecRequest(
            capabilities=capabilities,
            expected_catalog_hash=expected_catalog_hash,
        ),
        catalog,
        catalog_hash=CATALOG_HASH,
        source="http://comfy/object_info",
    )


def test_resolution_is_catalog_order_independent_and_prefers_native_by_policy():
    native = node_info(display_name="Image Upscale", description="Upscale an image")
    custom = node_info(
        display_name="Image Upscale",
        description="Upscale an image",
        module="custom_nodes.example",
    )
    request = [
        {
            "alias": "upscaler",
            "capability": "image upscale",
            "required_input_types": ["IMAGE"],
            "required_output_types": ["IMAGE"],
        }
    ]

    first = resolve(request, {"ZuluCustom": custom, "AlphaNative": native})
    second = resolve(request, {"AlphaNative": native, "ZuluCustom": custom})

    assert first["valid"] is True
    assert first["resolution_schema"] == WORKFLOW_SPEC_RESOLUTION_SCHEMA
    assert first["selected_node_types"] == {"upscaler": "AlphaNative"}
    assert second["selected_node_types"] == first["selected_node_types"]
    assert second["resolution_hash"] == first["resolution_hash"]
    assert first["policy"]["local_catalog_only"] is True
    assert first["policy"]["registry_candidates_eligible"] is False


def test_preferred_existing_custom_class_outranks_native_default():
    catalog = {
        "NativeUpscale": node_info(
            display_name="Image Upscale",
            description="Upscale an image",
        ),
        "ExistingCustomUpscale": node_info(
            display_name="Image Upscale",
            description="Upscale an image",
            module="custom_nodes.proven_pack",
        ),
    }

    result = resolve(
        [
            {
                "alias": "upscaler",
                "capability": "image upscale",
                "preferred_node_types": ["ExistingCustomUpscale"],
            }
        ],
        catalog,
    )

    selected = result["resolutions"][0]["selected"]
    assert selected["node_type"] == "ExistingCustomUpscale"
    assert selected["origin"] == "custom"
    assert "preferred class" in selected["match_reasons"][0]


def test_explicit_display_name_in_capability_outranks_keyword_rich_description():
    catalog = {
        "GeminiImage2Node": node_info(
            display_name="Gemini Image 2",
            module="comfy_api_nodes.nodes_gemini",
            category="partner/image",
            description=(
                "Nano Banana 2 partner image editing with two reference images, "
                "lighting transfer, and atmosphere"
            ),
        ),
        "GeminiNanoBanana2V2": node_info(
            display_name="Nano Banana 2",
            module="comfy_api_nodes.nodes_gemini",
            category="partner/image",
            description="Generate or edit an image",
        ),
    }

    result = resolve(
        [{
            "alias": "editor",
            "capability": "Nano Banana 2 partner image editing with two references",
            "allowed_origins": ["partner"],
            "required_input_types": ["IMAGE"],
            "required_output_types": ["IMAGE"],
        }],
        catalog,
    )

    assert result["selected_node_types"] == {"editor": "GeminiNanoBanana2V2"}
    assert "explicitly names display name" in (
        result["resolutions"][0]["selected"]["match_reasons"][0]
    )


def test_embedded_display_identity_survives_verbose_capability_constraints():
    result = resolve(
        [{
            "alias": "wavelet_color_fix",
            "capability": (
                "Locally loaded Wavelet Color Fix node with IMAGE inputs named "
                "source_image and target_image, an IMAGE output named image, and "
                "align_method set to wavelet. Add no intermediary or utility nodes."
            ),
            "allowed_origins": ["custom", "native"],
            "required_input_types": ["IMAGE"],
            "required_output_types": ["IMAGE"],
        }],
        {
            "WaveletColorFix": node_info(
                display_name="Wavelet Color Fix",
                module="custom_nodes.ComfyUI-FrameUtilitys",
                category="image/video",
                description=(
                    "Transfer source colors to target frames using AdaIN or wavelet "
                    "reconstruction"
                ),
                inputs={
                    "required": {
                        "target_image": ["IMAGE"],
                        "source_image": ["IMAGE"],
                        "align_method": [["wavelet", "adain"]],
                    }
                },
            )
        },
    )

    assert result["valid"] is True
    assert result["selected_node_types"] == {
        "wavelet_color_fix": "WaveletColorFix"
    }
    assert result["resolutions"][0]["selected"]["match_reasons"][0] in {
        "capability explicitly names node type",
        "capability explicitly names display name",
    }


def test_live_resize_identity_outranks_preferred_generic_scaler_unless_explicit():
    image_scale = node_info(
        display_name="Upscale Image",
        category="image/upscaling",
        inputs={
            "required": {
                "image": ["IMAGE"],
                "upscale_method": [["nearest-exact", "bilinear", "area"]],
                "width": ["INT", {"default": 512}],
                "height": ["INT", {"default": 512}],
                "crop": [["disabled", "center"]],
            }
        },
        search_aliases=[
            "resize",
            "resize image",
            "scale image",
            "image resize",
            "change size",
        ],
    )
    catalog = {
        "ImageScale": image_scale,
        "ResizeImageMaskNode": live_resize_image_mask_node_info(),
    }
    capability = (
        "Resize Image/Mask using scale dimensions at 1920 by 1080 with area "
        "interpolation while preserving IMAGE or MASK type; do not choose a generic "
        "image-only scaler."
    )
    role = {
        "alias": "resize",
        "capability": capability,
        "preferred_node_types": ["ImageScale"],
        "required_input_types": ["IMAGE"],
        "required_output_types": ["IMAGE"],
    }

    resolved = resolve([role], catalog)

    assert resolved["valid"] is True
    assert resolved["selected_node_types"] == {"resize": "ResizeImageMaskNode"}
    assert resolved["resolutions"][0]["candidates"][0]["node_type"] == (
        "ResizeImageMaskNode"
    )

    explicitly_requested = resolve(
        [{**role, "requested_node_type": "ImageScale"}],
        catalog,
    )
    assert explicitly_requested["valid"] is True
    assert explicitly_requested["selected_node_types"] == {"resize": "ImageScale"}


def test_candidate_descriptions_are_plain_and_bounded_for_model_context():
    catalog = {
        "VerboseNode": node_info(
            display_name="Verbose Utility",
            description="<div>Verbose utility &amp; guide</div>" + (" detail" * 200),
        )
    }

    result = resolve([{"alias": "utility", "capability": "verbose utility"}], catalog)

    description = result["resolutions"][0]["selected"]["description"]
    assert "<div>" not in description
    assert "&amp;" not in description
    assert len(description) <= 320
    assert description.endswith("…")


def test_explicit_partner_class_is_never_substituted_and_warns_before_execution():
    catalog = {
        "LocalGenerator": node_info(
            display_name="Generate Image",
            description="Generate image",
        ),
        "HostedGenerator": node_info(
            display_name="Generate Image",
            description="Generate image",
            module="comfy_api_nodes.nodes_vendor",
            category="partner/image",
            api_node=True,
        ),
    }

    result = resolve(
        [
            {
                "alias": "generator",
                "capability": "generate image",
                "requested_node_type": "HostedGenerator",
            }
        ],
        catalog,
    )

    assert result["selected_node_types"] == {"generator": "HostedGenerator"}
    assert result["warning_count"] == 1
    assert result["issues"][0]["code"] == (
        "partner_authentication_cost_privacy_review_required"
    )

    missing = resolve(
        [
            {
                "alias": "generator",
                "capability": "generate image",
                "requested_node_type": "MissingHostedGenerator",
            }
        ],
        catalog,
    )
    assert missing["valid"] is False
    assert missing["selected_node_types"] == {}
    assert missing["issues"][0]["code"] == "requested_node_not_loaded"


def test_type_constraints_include_nested_dynamic_inputs_and_ignore_enum_choices():
    dynamic = node_info(
        display_name="Dynamic Gemini Editor",
        module="comfy_api_nodes.nodes_gemini",
        category="partner/image",
        description="Edit image with prompt",
        inputs={
            "required": {
                "model": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "model-a",
                                "inputs": {
                                    "required": {
                                        "mode": [["fast", "quality"], {}],
                                        "image": ["IMAGE", {}],
                                    },
                                    "optional": {"mask": ["MASK", {}]},
                                },
                            }
                        ]
                    },
                ]
            }
        },
    )

    result = resolve(
        [
            {
                "alias": "editor",
                "capability": "edit image",
                "required_input_types": ["IMAGE", "MASK"],
                "required_output_types": ["IMAGE"],
            }
        ],
        {"DynamicEditor": dynamic},
    )

    selected = result["resolutions"][0]["selected"]
    assert selected["input_types"] == ["IMAGE", "MASK"]
    assert "fast" not in selected["input_types"]


def test_constraints_and_deprecation_fail_closed_without_registry_fallback():
    deprecated = node_info(
        display_name="Remove Background",
        description="Remove a background",
        module="custom_nodes.old",
        deprecated=True,
    )

    result = resolve(
        [
            {
                "alias": "remover",
                "capability": "remove background",
                "required_input_types": ["IMAGE"],
                "required_output_types": ["MASK"],
            }
        ],
        {"OldRemover": deprecated},
    )

    assert result["valid"] is False
    assert result["resolution_hash"] is None
    assert result["issues"][0]["code"] == "no_local_candidate"
    assert result["policy"]["registry_candidates_eligible"] is False


def test_multi_term_capability_does_not_fall_back_to_one_generic_word():
    catalog = {
        "LoadImage": node_info(
            display_name="Load Image",
            description="Load an image from disk",
        )
    }

    result = resolve(
        [{"alias": "upscaler", "capability": "image upscale"}],
        catalog,
    )

    assert result["valid"] is False
    assert result["issues"][0]["code"] == "no_local_candidate"


def test_equal_scores_require_an_explicit_choice():
    shared = node_info(display_name="Utility", description="Special utility")
    result = resolve(
        [{"alias": "utility", "capability": "special utility"}],
        {"zeta": copy.deepcopy(shared), "Alpha": copy.deepcopy(shared)},
    )

    assert result["valid"] is False
    assert result["needs_choice"] is True
    assert result["selected_node_types"] == {}
    assert result["issues"][0]["code"] == "ambiguous_local_candidate"
    assert result["error_count"] == 1
    assert result["warning_count"] == 0
    assert [
        candidate["node_type"]
        for candidate in result["resolutions"][0]["candidates"]
    ] == ["Alpha", "zeta"]


def test_live_seedance_reference_phrase_breaks_first_last_token_tie_stably():
    reference = node_info(
        display_name="ByteDance Seedance 2.0 Reference to Video",
        module="comfy_api_nodes.nodes_bytedance",
        category="partner/video/ByteDance",
        description=(
            "Generate, edit, or extend video using Seedance 2.0 with reference "
            "images, videos, and audio. Supports multimodal reference, video "
            "editing, and video extension."
        ),
        inputs={"required": {"reference_image": ["IMAGE", {}]}},
        outputs=["VIDEO"],
        api_node=True,
    )
    first_last = node_info(
        display_name="ByteDance Seedance 2.0 First-Last-Frame to Video",
        module="comfy_api_nodes.nodes_bytedance",
        category="partner/video/ByteDance",
        description=(
            "Generate video using Seedance 2.0 from a first frame image and "
            "optional last frame image."
        ),
        inputs={"required": {"first_frame": ["IMAGE", {}]}},
        outputs=["VIDEO"],
        api_node=True,
    )
    role = {
        "alias": "seedance",
        "capability": (
            "Seedance 2.0 reference-to-video partner node using its first "
            "dynamic reference image input."
        ),
        "allowed_origins": ["partner"],
        "required_input_types": ["IMAGE"],
        "required_output_types": ["VIDEO"],
    }

    first = resolve(
        [role],
        {
            "ByteDance2FirstLastFrameNode": first_last,
            "ByteDance2ReferenceNode": reference,
        },
    )
    reversed_catalog = resolve(
        [role],
        {
            "ByteDance2ReferenceNode": reference,
            "ByteDance2FirstLastFrameNode": first_last,
        },
    )

    assert first["valid"] is True, first["issues"]
    assert first["selected_node_types"] == {
        "seedance": "ByteDance2ReferenceNode"
    }
    assert reversed_catalog["resolution_hash"] == first["resolution_hash"]
    assert [
        item["node_type"] for item in first["resolutions"][0]["candidates"]
    ][:2] == ["ByteDance2ReferenceNode", "ByteDance2FirstLastFrameNode"]
    assert any(
        reason == (
            "contiguous display name phrase: seedance 2 0 reference to video"
        )
        for reason in first["resolutions"][0]["selected"]["match_reasons"]
    )


def test_equal_contiguous_identity_phrases_still_require_explicit_choice():
    shared = node_info(
        display_name="Reference to Video",
        module="comfy_api_nodes.nodes_vendor",
        category="partner/video",
        description="Generate a video from a reference image.",
        inputs={"required": {"reference_image": ["IMAGE", {}]}},
        outputs=["VIDEO"],
        api_node=True,
    )

    result = resolve(
        [{
            "alias": "generator",
            "capability": "reference-to-video partner node",
            "allowed_origins": ["partner"],
            "required_input_types": ["IMAGE"],
            "required_output_types": ["VIDEO"],
        }],
        {
            "AlphaReferenceVideo": copy.deepcopy(shared),
            "BetaReferenceVideo": copy.deepcopy(shared),
        },
    )

    assert result["valid"] is False
    assert result["needs_choice"] is True
    assert result["issues"][0]["code"] == "ambiguous_local_candidate"


def test_capability_set_fields_are_canonical_and_hash_invariant():
    repeated = WorkflowCapabilitySpec.model_validate({
        "alias": "upscaler",
        "capability": "image upscale " + ("with stable local schema " * 12),
        "preferred_node_types": ["Upscale", "Upscale"],
        "required_input_types": ["IMAGE", "IMAGE"],
        "required_output_types": ["IMAGE", "IMAGE"],
        "allowed_origins": ["native", "native"],
    })
    canonical = WorkflowCapabilitySpec.model_validate({
        "alias": "upscaler",
        "capability": repeated.capability,
        "preferred_node_types": ["Upscale"],
        "required_input_types": ["IMAGE"],
        "required_output_types": ["IMAGE"],
        "allowed_origins": ["native"],
    })

    assert len(repeated.capability) > 200
    assert repeated.model_dump(mode="json") == canonical.model_dump(mode="json")
    catalog = {
        "Upscale": node_info(
            display_name="Image Upscale",
            description="Upscale an image with stable local schema",
        )
    }
    repeated_result = resolve_workflow_spec(
        ResolveWorkflowSpecRequest(capabilities=[repeated]),
        catalog,
        catalog_hash=CATALOG_HASH,
        source="fixture",
    )
    canonical_result = resolve_workflow_spec(
        ResolveWorkflowSpecRequest(capabilities=[canonical]),
        catalog,
        catalog_hash=CATALOG_HASH,
        source="fixture",
    )

    assert repeated_result["valid"] is True
    assert repeated_result["resolution_hash"] == canonical_result["resolution_hash"]

    over_field_limit_before_deduplication = WorkflowCapabilitySpec.model_validate({
        "alias": "many_repeats",
        "capability": "image upscale",
        "required_input_types": ["IMAGE"] * 40,
        "required_output_types": ["IMAGE"] * 40,
    })
    assert over_field_limit_before_deduplication.required_input_types == ["IMAGE"]
    assert over_field_limit_before_deduplication.required_output_types == ["IMAGE"]

    implicit_origins = WorkflowCapabilitySpec.model_validate({
        "alias": "implicit_origins",
        "capability": "image upscale",
    })
    explicit_origins = WorkflowCapabilitySpec.model_validate({
        "alias": "implicit_origins",
        "capability": "image upscale",
        "allowed_origins": ["unknown", "partner", "custom", "native"],
    })
    assert implicit_origins.model_dump(mode="json") == explicit_origins.model_dump(
        mode="json"
    )


def test_catalog_mismatch_stops_resolution_and_hashing():
    result = resolve(
        [{"alias": "loader", "capability": "load image"}],
        {"LoadImage": node_info(display_name="Load Image")},
        expected_catalog_hash="b" * 64,
    )

    assert result["valid"] is False
    assert result["resolutions"] == []
    assert result["resolution_hash"] is None
    assert result["issues"][0]["code"] == "catalog_changed"


def test_unique_io_signature_resolves_verbose_get_video_components_role():
    component_schema = node_info(
        display_name="Get Video Components",
        module="comfy_extras.nodes_video",
        category="video",
        inputs={"required": {"video": ["VIDEO", {}]}},
        outputs=["IMAGE", "AUDIO", "FLOAT", "INT"],
    )
    result = resolve(
        [
            {
                "alias": "video_components",
                "capability": (
                    "Native ComfyUI node that converts/decomposes a native VIDEO "
                    "into IMAGE frames and exposes matching FPS and audio"
                ),
                "allowed_origins": ["native"],
                "required_input_types": ["VIDEO"],
                "required_output_types": ["IMAGE", "AUDIO", "FLOAT"],
            }
        ],
        {
            "GetVideoComponents": component_schema,
            "DeprecatedComponentUtility": {
                **copy.deepcopy(component_schema),
                "display_name": "Legacy Transport Utility",
                "deprecated": True,
            },
        },
    )

    assert result["valid"] is True, result["issues"]
    assert result["selected_node_types"] == {
        "video_components": "GetVideoComponents"
    }
    assert result["resolutions"][0]["selected"]["match_reasons"][0] == (
        "unique local class matching the exact I/O signature"
    )


def test_unique_io_signature_counts_convertible_create_video_fps_widget():
    result = resolve(
        [
            {
                "alias": "video_builder",
                "capability": (
                    "Assemble a pixel batch at its supplied scalar cadence into a "
                    "native transport container"
                ),
                "allowed_origins": ["native"],
                "required_input_types": ["IMAGE", "FLOAT"],
                "required_output_types": ["VIDEO"],
            }
        ],
        {
            "CreateVideo": node_info(
                display_name="Create Video",
                module="comfy_extras.nodes_video",
                category="video",
                inputs={
                    "required": {
                        "images": ["IMAGE", {}],
                        "fps": ["FLOAT", {"default": 24.0}],
                    },
                    "optional": {"audio": ["AUDIO", {}]},
                },
                outputs=["VIDEO"],
            )
        },
    )

    assert result["valid"] is True, result["issues"]
    assert result["selected_node_types"] == {"video_builder": "CreateVideo"}


def test_io_signature_fallback_never_guesses_between_multiple_safe_classes():
    shared = node_info(
        display_name="Transport Utility",
        module="comfy_extras.nodes_video",
        category="video",
        inputs={"required": {"video": ["VIDEO", {}]}},
        outputs=["IMAGE", "AUDIO", "FLOAT"],
    )
    result = resolve(
        [
            {
                "alias": "components",
                "capability": "Decompose a transport stream into frame batch soundtrack cadence",
                "allowed_origins": ["native"],
                "required_input_types": ["VIDEO"],
                "required_output_types": ["IMAGE", "AUDIO", "FLOAT"],
            }
        ],
        {
            "AlphaTransport": copy.deepcopy(shared),
            "BetaTransport": copy.deepcopy(shared),
        },
    )

    assert result["valid"] is False
    assert result["needs_choice"] is True
    assert result["selected_node_types"] == {}
    assert result["issues"][0]["code"] == "ambiguous_local_candidate"
    assert [
        item["node_type"] for item in result["resolutions"][0]["candidates"]
    ] == ["AlphaTransport", "BetaTransport"]
