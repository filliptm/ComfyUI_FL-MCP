from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from workflow_capability_graph import (
    MAX_SELECTOR_VARIANTS,
    CapabilityGraph,
    RouteEndpoint,
    RoutePolicy,
    VerifiedCapabilityLesson,
    build_capability_graph,
    derive_transform_profile,
    derive_transform_profiles,
)


def _schema(
    *,
    required: Mapping[str, Any] | None = None,
    optional: Mapping[str, Any] | None = None,
    outputs: tuple[str, ...] = (),
    output_names: tuple[str, ...] | None = None,
    module: str = "comfy_extras.synthetic_nodes",
    display_name: str = "Synthetic Transform",
    description: str = "",
    category: str = "transform",
    api_node: bool = False,
    output_node: bool = False,
) -> dict[str, Any]:
    input_groups: dict[str, Any] = {"required": dict(required or {})}
    if optional:
        input_groups["optional"] = dict(optional)
    value: dict[str, Any] = {
        "input": input_groups,
        "output": list(outputs),
        "output_name": list(output_names or outputs),
        "python_module": module,
        "display_name": display_name,
        "description": description,
        "category": category,
    }
    if api_node:
        value["api_node"] = True
    if output_node:
        value["output_node"] = True
    return value


def _create_video_schema() -> dict[str, Any]:
    return _schema(
        required={
            "fps": ["FLOAT", {"default": 30.0, "min": 1.0}],
            "images": ["IMAGE"],
        },
        optional={
            "audio": ["AUDIO"],
            "bit_depth": ["INT", {"default": 8}],
        },
        outputs=("VIDEO",),
        output_names=("video",),
        display_name="Create Video",
        category="video",
    )


def _video_components_schema() -> dict[str, Any]:
    return _schema(
        required={"video": ["VIDEO"]},
        outputs=("IMAGE", "AUDIO", "FLOAT", "INT"),
        output_names=("images", "audio", "fps", "bit_depth"),
        display_name="Get Video Components",
        category="video",
    )


def _wavelet_schema() -> dict[str, Any]:
    return _schema(
        required={
            "target_image": ["IMAGE"],
            "source_image": ["IMAGE"],
            "method": [["wavelet", "adain"], {"default": "wavelet"}],
        },
        outputs=("IMAGE",),
        output_names=("image",),
        module="custom_nodes.wavelet_color_fix",
        display_name="Wavelet Color Fix",
        category="image/color",
    )


def _seedance_schema() -> dict[str, Any]:
    return _schema(
        required={
            "model": [
                "COMFY_DYNAMICCOMBO_V3",
                {
                    "options": [
                        {
                            "key": "Reference mode",
                            "inputs": {
                                "required": {
                                    "prompt": ["STRING", {"default": ""}],
                                    "reference_images": [
                                        "COMFY_AUTOGROW_V3",
                                        {
                                            "template": {
                                                "input": {
                                                    "required": {
                                                        "image": ["IMAGE"]
                                                    }
                                                },
                                                "names": ["image_1", "image_2"],
                                                "min": 0,
                                            }
                                        },
                                    ],
                                }
                            },
                        }
                    ]
                },
            ],
        },
        outputs=("VIDEO",),
        output_names=("video",),
        module="comfy_api_nodes.nodes_bytedance",
        display_name="Seedance Reference",
        category="partner/video",
        api_node=True,
    )


def _branching_transform_schema(
    options: list[tuple[str, str]],
    *,
    default: str | None = None,
) -> dict[str, Any]:
    selector_metadata: dict[str, Any] = {
        "options": [
            {
                "key": key,
                "inputs": {"required": {"payload": [input_type]}},
            }
            for key, input_type in options
        ]
    }
    if default is not None:
        selector_metadata["default"] = default
    return _schema(
        required={"mode": ["COMFY_DYNAMICCOMBO_V3", selector_metadata]},
        outputs=("READY",),
        output_names=("ready",),
        display_name="Branching Transform",
    )


def _route_node_types(result: Any) -> tuple[str, ...]:
    assert result.selected is not None
    return tuple(step.node_type for step in result.selected.steps)


def test_profiles_are_derived_from_live_shaped_create_and_split_video_schemas():
    create = derive_transform_profile("CreateVideo", _create_video_schema())
    split = derive_transform_profile(
        "GetVideoComponents", _video_components_schema()
    )

    assert create.origin == "native"
    assert create.heavy is False
    assert create.schema_hash
    assert create.accepted_types == ("AUDIO", "FLOAT", "IMAGE", "INT")
    assert create.produced_types == ("VIDEO",)
    assert [item.path for item in create.default_active_inputs] == [
        "fps",
        "images",
        "audio",
        "bit_depth",
    ]
    fps = create.input_ports_named("fps")[0]
    assert fps.required is True
    assert fps.connectable is False
    assert fps.widget_convertible is True
    assert fps.stable_default is not None
    assert fps.stable_default.value == 30.0
    assert fps.stable_default.provenance == "schema_default"
    assert split.accepted_types == ("VIDEO",)
    assert [item.produced_types for item in split.outputs] == [
        ("IMAGE",),
        ("AUDIO",),
        ("FLOAT",),
        ("INT",),
    ]


def test_create_video_route_binds_multi_input_hyperedge_and_stable_fps_default():
    graph = build_capability_graph({"CreateVideo": _create_video_schema()})

    fully_bound = graph.find_route(
        available_types={"IMAGE", "FLOAT", "AUDIO"},
        required_types={"VIDEO"},
    )
    assert fully_bound.status == "resolved"
    assert _route_node_types(fully_bound) == ("CreateVideo",)
    step = fully_bound.selected.steps[0]  # type: ignore[union-attr]
    assert {(item.path, item.input_type, item.mode) for item in step.input_bindings} == {
        ("images", "IMAGE", "slot"),
        ("fps", "FLOAT", "convert_widget"),
    }
    assert step.required_stable_widget_values == ()

    defaulted = graph.find_route(
        available_types={"IMAGE"},
        required_types={"VIDEO"},
    )
    assert defaulted.status == "resolved"
    stable = defaulted.selected.steps[0].required_stable_widget_values  # type: ignore[union-attr]
    assert [(item.path, item.value, item.provenance) for item in stable] == [
        ("fps", 30.0, "schema_default")
    ]
    assert defaulted.selected.cost.defaulted_required_widget_count == 1  # type: ignore[union-attr]


def test_get_video_components_route_preserves_all_outputs_of_one_hyperedge():
    graph = CapabilityGraph.from_catalog(
        {"GetVideoComponents": _video_components_schema()}
    )
    result = graph.find_route(
        available_types={"VIDEO"},
        required_types={"IMAGE", "AUDIO", "FLOAT", "INT"},
    )

    assert result.status == "resolved"
    assert _route_node_types(result) == ("GetVideoComponents",)
    assert result.selected.resulting_types == (  # type: ignore[union-attr]
        "AUDIO",
        "FLOAT",
        "IMAGE",
        "INT",
        "VIDEO",
    )
    assert len(result.selected.steps[0].produced_outputs) == 4  # type: ignore[union-attr]


def test_wavelet_image_to_seedance_reference_is_direct_without_converter():
    wavelet = derive_transform_profile("WaveletColorFix", _wavelet_schema())
    seedance = derive_transform_profile("ByteDance2ReferenceNode", _seedance_schema())
    image_reference = next(
        item
        for item in seedance.default_active_inputs
        if "IMAGE" in item.accepted_types
    )

    result = CapabilityGraph.from_catalog({}).find_route(
        available_types=wavelet.produced_types,
        required_types=image_reference.accepted_types,
    )

    assert result.status == "direct"
    assert result.valid is True
    assert result.selected is not None
    assert result.selected.steps == ()
    assert result.selected.cost.intermediary_count == 0


def test_nondefault_dynamic_selector_branch_routes_with_exact_stable_value():
    schema = _branching_transform_schema(
        [("image mode", "IMAGE"), ("mask mode", "MASK")]
    )
    profiles = derive_transform_profiles("BranchingTransform", schema)

    assert len(profiles) == 2
    assert profiles[0].variant_key == "default"
    assert profiles[0].accepted_types == ("IMAGE",)
    assert profiles[0].nondefault_selector_count == 0
    assert profiles[1].accepted_types == ("MASK",)
    assert profiles[1].nondefault_selector_count == 1

    graph = CapabilityGraph.from_catalog({"BranchingTransform": schema})
    default_result = graph.find_route(
        available_types={"IMAGE"}, required_types={"READY"}
    )
    alternate_result = graph.find_route(
        available_types={"MASK"}, required_types={"READY"}
    )

    assert default_result.status == "resolved"
    assert default_result.selected is not None
    assert default_result.selected.steps[0].nondefault_selector_count == 0
    assert alternate_result.status == "resolved"
    assert alternate_result.selected is not None
    step = alternate_result.selected.steps[0]
    assert [(item.path, item.value, item.provenance) for item in step.selector_values] == [
        ("mode", "mask mode", "dynamic_selector_branch")
    ]
    assert [(item.path, item.value) for item in step.required_stable_widget_values] == [
        ("mode", "mask mode")
    ]
    assert alternate_result.selected.cost.nondefault_selector_count == 1


def test_default_dynamic_branch_wins_when_an_alternate_has_the_same_shape():
    schema = _branching_transform_schema(
        [("default path", "RAW"), ("alternate path", "RAW")]
    )
    result = CapabilityGraph.from_catalog(
        {"ShapeEquivalentTransform": schema}
    ).find_route(available_types={"RAW"}, required_types={"READY"})

    assert result.status == "resolved"
    assert result.selected is not None
    step = result.selected.steps[0]
    assert step.nondefault_selector_count == 0
    assert [(item.path, item.value) for item in step.selector_values] == [
        ("mode", "default path")
    ]


def test_equal_nondefault_dynamic_branches_require_a_choice():
    schema = _branching_transform_schema(
        [
            ("default image", "IMAGE"),
            ("mask method a", "MASK"),
            ("mask method b", "MASK"),
        ]
    )
    result = CapabilityGraph.from_catalog(
        {"AmbiguousBranchTransform": schema}
    ).find_route(available_types={"MASK"}, required_types={"READY"})

    assert result.status == "needs_choice"
    assert result.selected is None
    assert len(result.choices) == 2
    assert {
        route.steps[0].selector_values[0].value for route in result.choices
    } == {"mask method a", "mask method b"}


def test_nested_dynamic_selectors_enumerate_only_viable_active_branches():
    schema = _schema(
        required={
            "mode": [
                "COMFY_DYNAMICCOMBO_V3",
                {
                    "options": [
                        {
                            "key": "basic",
                            "inputs": {"required": {"payload": ["IMAGE"]}},
                        },
                        {
                            "key": "advanced",
                            "inputs": {
                                "required": {
                                    "method": [
                                        "COMFY_DYNAMICCOMBO_V3",
                                        {
                                            "options": [
                                                {
                                                    "key": "mask",
                                                    "inputs": {
                                                        "required": {
                                                            "payload": ["MASK"]
                                                        }
                                                    },
                                                },
                                                {
                                                    "key": "latent",
                                                    "inputs": {
                                                        "required": {
                                                            "payload": ["LATENT"]
                                                        }
                                                    },
                                                },
                                            ]
                                        },
                                    ]
                                }
                            },
                        },
                    ]
                },
            ]
        },
        outputs=("READY",),
    )
    profiles = derive_transform_profiles("NestedBranchTransform", schema)

    assert len(profiles) == 3
    assert [profile.accepted_types for profile in profiles] == [
        ("IMAGE",),
        ("MASK",),
        ("LATENT",),
    ]
    result = CapabilityGraph.from_catalog(
        {"NestedBranchTransform": schema}
    ).find_route(available_types={"LATENT"}, required_types={"READY"})

    assert result.status == "resolved"
    assert result.selected is not None
    assert [(item.path, item.value) for item in result.selected.steps[0].selector_values] == [
        ("mode", "advanced"),
        ("mode.method", "latent"),
    ]


def test_dynamic_selector_variant_expansion_is_bounded_and_fail_closed():
    schema = _branching_transform_schema(
        [(f"mode {index}", f"TYPE_{index}") for index in range(MAX_SELECTOR_VARIANTS + 1)]
    )
    graph = CapabilityGraph.from_catalog({"TooManyBranches": schema})

    assert graph.profile("TooManyBranches") is None
    assert [(item.code, item.node_type) for item in graph.issues] == [
        ("dynamic_selector_variant_limit_exceeded", "TooManyBranches")
    ]


def test_dynamic_variant_catalog_order_is_route_deterministic():
    branch = _branching_transform_schema(
        [("image mode", "IMAGE"), ("mask mode", "MASK")]
    )
    tail = _schema(required={"source": ["READY"]}, outputs=("DONE",))
    forward = CapabilityGraph.from_catalog(
        {"BranchingTransform": branch, "TailTransform": tail}
    )
    reverse = CapabilityGraph.from_catalog(
        {"TailTransform": tail, "BranchingTransform": branch}
    )

    assert forward.find_route(
        available_types={"MASK"}, required_types={"DONE"}
    ) == reverse.find_route(available_types={"MASK"}, required_types={"DONE"})


def test_equal_schema_routes_fail_closed_with_ranked_choices():
    shared = _schema(
        required={"source": ["RAW"]},
        outputs=("READY",),
        module="custom_nodes.transforms",
    )
    graph = CapabilityGraph.from_catalog(
        {"SecondTransform": shared, "FirstTransform": shared}
    )
    result = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
    )

    assert result.status == "needs_choice"
    assert result.valid is False
    assert result.needs_choice is True
    assert result.selected is None
    assert [route.steps[0].node_type for route in result.choices] == [
        "FirstTransform",
        "SecondTransform",
    ]
    assert result.issue_codes == ("ambiguous_conversion_route",)


def test_native_route_wins_and_verified_lessons_are_schema_scoped_priors_only():
    shape = {
        "required": {"source": ["RAW"]},
        "outputs": ("READY",),
    }
    catalog = {
        "NativeTransform": _schema(**shape),
        "CustomTransform": _schema(
            **shape,
            module="custom_nodes.transforms",
        ),
        "Consumer": _schema(
            required={"value": ["READY"]},
            output_node=True,
        ),
    }
    graph = CapabilityGraph.from_catalog(catalog)
    ordinary = graph.find_route(
        available_types={"RAW"}, required_types={"READY"}
    )
    assert _route_node_types(ordinary) == ("NativeTransform",)

    custom = graph.profile("CustomTransform")
    native = graph.profile("NativeTransform")
    consumer = graph.profile("Consumer")
    assert custom is not None
    assert native is not None
    assert consumer is not None
    verified = VerifiedCapabilityLesson(
        node_type="CustomTransform",
        schema_hash=custom.schema_hash,
        payload={
            "evidence": "atomic_graph_patch_application",
            "source_node_type": "CustomTransform",
            "source_schema_hash": custom.schema_hash,
            "target_node_type": "Consumer",
            "target_schema_hash": consumer.schema_hash,
        },
    )
    stale = VerifiedCapabilityLesson(
        node_type="NativeTransform",
        schema_hash="0" * 64,
        payload={
            "evidence": "atomic_graph_patch_application",
            "source_node_type": "NativeTransform",
            "source_schema_hash": native.schema_hash,
            "target_node_type": "Consumer",
            "target_schema_hash": consumer.schema_hash,
        },
    )
    learned = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        verified_lessons=(verified, stale),
    )

    assert _route_node_types(learned) == ("CustomTransform",)
    assert learned.accepted_verified_lesson_count == 1
    assert learned.ignored_verified_lesson_count == 1
    assert learned.selected.steps[0].verified_lesson_count == 1  # type: ignore[union-attr]
    assert "schema_valid_verified_lessons:1" in learned.selected.evidence  # type: ignore[union-attr]


def test_malformed_lesson_endpoint_types_are_ignored_without_crashing():
    catalog = {
        "NativeTransform": _schema(
            required={"source": ["RAW"]}, outputs=("READY",)
        ),
        "CustomTransform": _schema(
            required={"source": ["RAW"]},
            outputs=("READY",),
            module="custom_nodes.transforms",
        ),
        "Consumer": _schema(required={"value": ["READY"]}, output_node=True),
    }
    graph = CapabilityGraph.from_catalog(catalog)
    custom = graph.profile("CustomTransform")
    consumer = graph.profile("Consumer")
    assert custom is not None
    assert consumer is not None
    malformed = VerifiedCapabilityLesson(
        node_type="CustomTransform",
        schema_hash=custom.schema_hash,
        payload={
            "evidence": "atomic_graph_patch_application",
            "source_node_type": ["CustomTransform"],
            "source_schema_hash": custom.schema_hash,
            "target_node_type": "Consumer",
            "target_schema_hash": consumer.schema_hash,
        },
    )
    malformed_evidence = VerifiedCapabilityLesson(
        node_type="CustomTransform",
        schema_hash=custom.schema_hash,
        payload={
            "evidence": ["atomic_graph_patch_application"],
            "source_node_type": "CustomTransform",
            "source_schema_hash": custom.schema_hash,
            "target_node_type": "Consumer",
            "target_schema_hash": consumer.schema_hash,
        },
    )

    result = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        verified_lessons=(malformed, malformed_evidence),
    )

    assert _route_node_types(result) == ("NativeTransform",)
    assert result.accepted_verified_lesson_count == 0
    assert result.ignored_verified_lesson_count == 2


def test_verified_lesson_is_ignored_after_counterpart_schema_drift():
    custom_schema = _schema(
        required={"source": ["RAW"]},
        outputs=("READY",),
        module="custom_nodes.transforms",
    )
    native_schema = _schema(required={"source": ["RAW"]}, outputs=("READY",))
    consumer_v1 = _schema(required={"value": ["READY"]}, output_node=True)
    original = CapabilityGraph.from_catalog(
        {
            "NativeTransform": native_schema,
            "CustomTransform": custom_schema,
            "Consumer": consumer_v1,
        }
    )
    custom = original.profile("CustomTransform")
    consumer = original.profile("Consumer")
    assert custom is not None
    assert consumer is not None
    lesson = VerifiedCapabilityLesson(
        node_type="CustomTransform",
        schema_hash=custom.schema_hash,
        payload={
            "evidence": "atomic_graph_patch_application",
            "source_node_type": "CustomTransform",
            "source_schema_hash": custom.schema_hash,
            "target_node_type": "Consumer",
            "target_schema_hash": consumer.schema_hash,
        },
    )
    changed = CapabilityGraph.from_catalog(
        {
            "NativeTransform": native_schema,
            "CustomTransform": custom_schema,
            "Consumer": _schema(
                required={"changed_value": ["READY"]}, output_node=True
            ),
        }
    )

    result = changed.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        verified_lessons=(lesson,),
    )

    assert _route_node_types(result) == ("NativeTransform",)
    assert result.accepted_verified_lesson_count == 0
    assert result.ignored_verified_lesson_count == 1


def test_unsatisfied_required_side_input_fails_closed_with_evidence():
    graph = CapabilityGraph.from_catalog(
        {
            "TwoInputTransform": _schema(
                required={
                    "source": ["RAW"],
                    "calibration": ["CALIBRATION"],
                },
                outputs=("READY",),
                module="custom_nodes.transforms",
            )
        }
    )
    result = graph.find_route(
        available_types={"RAW"}, required_types={"READY"}
    )

    assert result.status == "unresolved"
    assert result.selected is None
    assert len(result.rejections) == 1
    rejection = result.rejections[0]
    assert rejection.node_type == "TwoInputTransform"
    assert rejection.code == "unsatisfied_required_inputs"
    assert rejection.missing_input_types == ("CALIBRATION",)


def test_optional_data_port_is_never_bound_from_the_implicit_primary_source():
    graph = CapabilityGraph.from_catalog(
        {
            "OptionalReferenceTransform": _schema(
                required={"source": ["RAW"]},
                optional={"reference": ["RAW"]},
                outputs=("READY",),
            )
        }
    )
    result = graph.find_route(
        available_types={"RAW"}, required_types={"READY"}
    )

    assert result.status == "resolved"
    assert result.selected is not None
    assert [
        (binding.path, binding.input_type, binding.required)
        for binding in result.selected.steps[0].input_bindings
    ] == [("source", "RAW", True)]


def test_optional_only_transform_fails_without_an_explicit_primary_binding():
    graph = CapabilityGraph.from_catalog(
        {
            "OptionalOnlyTransform": _schema(
                optional={"possible_source": ["RAW"]},
                outputs=("READY",),
            )
        }
    )
    result = graph.find_route(
        available_types={"RAW"}, required_types={"READY"}
    )

    assert result.status == "unresolved"
    assert result.selected is None
    assert any(
        item.node_type == "OptionalOnlyTransform"
        and item.code == "no_bound_upstream_input"
        for item in result.rejections
    )


def test_list_source_prefers_exact_list_input_independent_of_catalog_order():
    exact_list = _schema(
        required={"source": ["RAW"]},
        outputs=("READY",),
        module="custom_nodes.transforms",
    )
    exact_list["is_input_list"] = True
    mapped_scalar = _schema(
        required={"source": ["RAW"]},
        outputs=("READY",),
    )
    catalog = {
        "MappedScalarTransform": mapped_scalar,
        "ExactListTransform": exact_list,
    }
    forward = CapabilityGraph.from_catalog(catalog).find_route(
        available_endpoints={RouteEndpoint("RAW", "list")},
        required_endpoints={RouteEndpoint("READY", "scalar")},
    )
    reverse = CapabilityGraph.from_catalog(
        dict(reversed(list(catalog.items())))
    ).find_route(
        available_endpoints={RouteEndpoint("RAW", "list")},
        required_endpoints={RouteEndpoint("READY", "scalar")},
    )

    assert forward == reverse
    assert _route_node_types(forward) == ("ExactListTransform",)
    assert forward.selected is not None
    assert forward.selected.cost.cardinality_penalty == 0
    binding = forward.selected.steps[0].input_bindings[0]
    assert (
        binding.source_cardinality,
        binding.target_cardinality,
        binding.cardinality_effect,
    ) == ("list", "list", "exact")
    assert forward.selected.steps[0].produced_endpoints == (
        RouteEndpoint("READY", "scalar"),
    )


def test_list_source_can_use_unique_mapped_scalar_fallback_and_propagates_list():
    graph = CapabilityGraph.from_catalog(
        {
            "OnlyScalarTransform": _schema(
                required={"source": ["RAW"]}, outputs=("READY",)
            )
        }
    )
    result = graph.find_route(
        available_endpoints={RouteEndpoint("RAW", "list")},
        required_endpoints={RouteEndpoint("READY", "scalar")},
    )

    assert result.status == "resolved"
    assert _route_node_types(result) == ("OnlyScalarTransform",)
    assert result.selected is not None
    assert result.selected.cost.cardinality_penalty == 1
    binding = result.selected.steps[0].input_bindings[0]
    assert binding.cardinality_effect == "mapped_scalar_over_list"
    assert result.selected.steps[0].produced_endpoints == (
        RouteEndpoint("READY", "list"),
    )
    assert RouteEndpoint("READY", "list") in result.selected.resulting_endpoints


def test_mapped_output_cardinality_guides_the_next_converter_input():
    list_tail = _schema(required={"mid": ["MID"]}, outputs=("DONE",))
    list_tail["is_input_list"] = True
    graph = CapabilityGraph.from_catalog(
        {
            "MappedHead": _schema(
                required={"source": ["RAW"]}, outputs=("MID",)
            ),
            "ExactListTail": list_tail,
            "MappedScalarTail": _schema(
                required={"mid": ["MID"]}, outputs=("DONE",)
            ),
        }
    )
    result = graph.find_route(
        available_endpoints={RouteEndpoint("RAW", "list")},
        required_endpoints={RouteEndpoint("DONE", "scalar")},
    )

    assert result.status == "resolved"
    assert _route_node_types(result) == ("MappedHead", "ExactListTail")
    assert result.selected is not None
    assert result.selected.cost.cardinality_penalty == 1
    assert result.selected.steps[0].produced_endpoints == (
        RouteEndpoint("MID", "list"),
    )
    assert result.selected.steps[1].input_bindings[0].cardinality_effect == "exact"


def test_equal_exact_list_converter_routes_still_require_choice():
    first = _schema(required={"source": ["RAW"]}, outputs=("READY",))
    second = _schema(required={"source": ["RAW"]}, outputs=("READY",))
    first["is_input_list"] = True
    second["is_input_list"] = True
    result = CapabilityGraph.from_catalog(
        {"SecondListTransform": second, "FirstListTransform": first}
    ).find_route(
        available_endpoints={RouteEndpoint("RAW", "list")},
        required_endpoints={RouteEndpoint("READY", "scalar")},
    )

    assert result.status == "needs_choice"
    assert result.selected is None
    assert [route.steps[0].node_type for route in result.choices] == [
        "FirstListTransform",
        "SecondListTransform",
    ]


def test_unbound_legacy_wildcard_output_never_claims_a_concrete_conversion():
    graph = CapabilityGraph.from_catalog(
        {
            "GenericPassThrough": _schema(
                required={"value": ["*"]},
                outputs=("*",),
                module="custom_nodes.generic",
            )
        }
    )
    result = graph.find_route(
        available_types={"IMAGE"}, required_types={"VIDEO"}
    )

    assert result.status == "unresolved"
    assert result.selected is None


@pytest.mark.parametrize(
    ("schema_overrides", "expected_code"),
    [
        (
            {
                "module": "comfy_api_nodes.synthetic",
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
    ],
)
def test_risky_intermediaries_require_explicit_permission(
    schema_overrides: dict[str, Any], expected_code: str
):
    node_info = _schema(
        required={"source": ["RAW"]},
        outputs=("READY",),
        **schema_overrides,
    )
    graph = CapabilityGraph.from_catalog({"RiskyTransform": node_info})
    blocked = graph.find_route(
        available_types={"RAW"}, required_types={"READY"}
    )
    assert blocked.status == "unresolved"
    assert expected_code in blocked.issue_codes

    explicit = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        policy=RoutePolicy(
            explicitly_allowed_node_types=frozenset({"RiskyTransform"})
        ),
    )
    assert explicit.status == "resolved"
    assert _route_node_types(explicit) == ("RiskyTransform",)


def test_no_extra_and_exact_node_policies_are_enforced():
    graph = CapabilityGraph.from_catalog(
        {
            "AllowedTransform": _schema(
                required={"source": ["RAW"]}, outputs=("READY",)
            )
        }
    )
    no_extra = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        policy=RoutePolicy(allow_extra_nodes=False),
    )
    assert no_extra.status == "extra_nodes_disallowed"

    excluded = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        policy=RoutePolicy(
            exact_intermediary_node_types=frozenset({"DifferentTransform"})
        ),
    )
    assert excluded.status == "unresolved"

    included = graph.find_route(
        available_types={"RAW"},
        required_types={"READY"},
        policy=RoutePolicy(
            exact_intermediary_node_types=frozenset({"AllowedTransform"})
        ),
    )
    assert included.status == "resolved"


def test_generic_non_media_two_hop_hypergraph_route_and_depth_bound():
    catalog = {
        "NormalizeRecord": _schema(
            required={"record": ["RAW_RECORD"]},
            outputs=("NORMALIZED_RECORD", "QUALITY_REPORT"),
            output_names=("record", "report"),
        ),
        "CreateIndexEntry": _schema(
            required={
                "record": ["NORMALIZED_RECORD"],
                "language": ["STRING", {"default": "en"}],
            },
            outputs=("INDEX_ENTRY",),
        ),
        "BuildKnowledgeObject": _schema(
            required={"entry": ["INDEX_ENTRY"]},
            outputs=("KNOWLEDGE_OBJECT",),
        ),
    }
    graph = CapabilityGraph.from_catalog(catalog)

    two_hop = graph.find_route(
        available_types={"RAW_RECORD"}, required_types={"INDEX_ENTRY"}
    )
    assert two_hop.status == "resolved"
    assert _route_node_types(two_hop) == (
        "NormalizeRecord",
        "CreateIndexEntry",
    )
    assert "QUALITY_REPORT" in two_hop.selected.resulting_types  # type: ignore[union-attr]
    language = two_hop.selected.steps[1].required_stable_widget_values  # type: ignore[union-attr]
    assert [(item.path, item.value) for item in language] == [("language", "en")]

    too_deep = graph.find_route(
        available_types={"RAW_RECORD"}, required_types={"KNOWLEDGE_OBJECT"}
    )
    assert too_deep.status == "unresolved"


@pytest.mark.parametrize(
    ("source_type", "mid_type", "target_type"),
    [
        ("SPECTRUM", "FILTERED_SPECTRUM", "FEATURE_VECTOR"),
        ("MESH", "SIMPLIFIED_MESH", "COLLISION_SHAPE"),
        ("DOCUMENT", "TOKEN_STREAM", "SEARCH_INDEX"),
    ],
)
def test_schema_shapes_drive_routes_for_arbitrary_domains(
    source_type: str, mid_type: str, target_type: str
):
    catalog = {
        "FirstStage": _schema(
            required={"source": [source_type]}, outputs=(mid_type,)
        ),
        "SecondStage": _schema(
            required={"source": [mid_type]}, outputs=(target_type,)
        ),
    }
    result = build_capability_graph(catalog).find_route(
        available_types={source_type}, required_types={target_type}
    )

    assert result.status == "resolved"
    assert _route_node_types(result) == ("FirstStage", "SecondStage")


def test_catalog_order_does_not_change_profiles_or_route_result():
    entries = [
        (
            "FirstStage",
            _schema(required={"source": ["A"]}, outputs=("B",)),
        ),
        (
            "SecondStage",
            _schema(required={"source": ["B"]}, outputs=("C",)),
        ),
    ]
    forward = CapabilityGraph.from_catalog(dict(entries))
    reverse = CapabilityGraph.from_catalog(dict(reversed(entries)))

    assert forward.profiles == reverse.profiles
    assert forward.find_route(
        available_types={"A"}, required_types={"C"}
    ) == reverse.find_route(available_types={"A"}, required_types={"C"})
