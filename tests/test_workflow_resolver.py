import copy

from backend.workflow_resolver import (
    WORKFLOW_SPEC_RESOLUTION_SCHEMA,
    ResolveWorkflowSpecRequest,
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


def test_equal_scores_use_lexical_tiebreak_and_report_it():
    shared = node_info(display_name="Utility", description="Special utility")
    result = resolve(
        [{"alias": "utility", "capability": "special utility"}],
        {"zeta": copy.deepcopy(shared), "Alpha": copy.deepcopy(shared)},
    )

    assert result["selected_node_types"] == {"utility": "Alpha"}
    assert result["issues"][0]["code"] == "lexical_tiebreak_applied"
    assert result["warning_count"] == 1


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
