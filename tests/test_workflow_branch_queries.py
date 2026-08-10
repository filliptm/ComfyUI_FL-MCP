import copy
import hashlib

import pytest
from pydantic import ValidationError
from workflow_branch_queries import (
    CompareWorkflowBranchesRequest,
    ResolveWorkflowBranchRequest,
    compare_workflow_branches,
    resolve_workflow_branch,
)
from workflow_branches import WorkflowBranchCatalog, discover_workflow_branches

GRAPH_HASH = "a" * 64
WORKFLOW_IDENTITY = "workflow:test-branches"


def _slot(name, slot_type):
    return {"name": name, "type": slot_type}


def _node(node_id, node_type, *, inputs=(), outputs=(), title=None, widgets=None, alias=None):
    node = {
        "id": node_id,
        "type": node_type,
        "inputs": [_slot(name, slot_type) for name, slot_type in inputs],
        "outputs": [_slot(name, slot_type) for name, slot_type in outputs],
    }
    if title is not None:
        node["title"] = title
    if widgets is not None:
        node["widgets_values"] = widgets
    if alias is not None:
        node["properties"] = {"fl_mcp_workflow_graph_patch": {"alias": alias}}
    return node


def _diamond(
    *,
    left_type="Processor",
    right_type="Processor",
    left_title=None,
    right_title=None,
    left_widgets=None,
    right_widgets=None,
    left_alias=None,
    right_alias=None,
):
    return {
        "nodes": [
            _node(1, "Source", outputs=(("image", "IMAGE"),)),
            _node(
                2,
                left_type,
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                title=left_title,
                widgets=left_widgets,
                alias=left_alias,
            ),
            _node(
                3,
                right_type,
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                title=right_title,
                widgets=right_widgets,
                alias=right_alias,
            ),
            _node(
                4,
                "Merge",
                inputs=(("left", "IMAGE"), ("right", "IMAGE")),
                outputs=(("image", "IMAGE"),),
            ),
        ],
        "links": [
            [10, 1, 0, 2, 0, "IMAGE"],
            [11, 1, 0, 3, 0, "IMAGE"],
            [12, 2, 0, 4, 0, "IMAGE"],
            [13, 3, 0, 4, 1, "IMAGE"],
        ],
    }


def _catalog(workflow):
    result = discover_workflow_branches(
        workflow,
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
    )
    assert result.valid, result.issues
    return result


def _request(catalog, **overrides):
    payload = {
        "expected_workflow_identity": catalog.workflow_identity,
        "expected_graph_hash": catalog.graph_hash,
        "expected_branch_catalog_hash": catalog.branch_catalog_hash,
    }
    payload.update(overrides)
    return ResolveWorkflowBranchRequest.model_validate(payload)


def _compare_request(catalog, left, right, **overrides):
    payload = {
        "expected_workflow_identity": catalog.workflow_identity,
        "expected_graph_hash": catalog.graph_hash,
        "expected_branch_catalog_hash": catalog.branch_catalog_hash,
        "left_branch_id": left.branch_id,
        "right_branch_id": right.branch_id,
    }
    payload.update(overrides)
    return CompareWorkflowBranchesRequest.model_validate(payload)


def _root(catalog):
    return next(scope for scope in catalog.scopes if scope.scope.kind == "root")


def _arms(catalog):
    return [branch for branch in _root(catalog).branches if branch.kind == "split_arm"]


def _arm_owning(catalog, node_id):
    return next(branch for branch in _arms(catalog) if node_id in branch.owned_node_ids)


def _reversed_catalog(catalog):
    payload = catalog.model_dump(mode="json", by_alias=True)
    payload["scopes"].reverse()
    for scope in payload["scopes"]:
        scope["branches"].reverse()
    return WorkflowBranchCatalog.model_validate(payload)


def _reversed_workflow(workflow):
    result = copy.deepcopy(workflow)
    result["nodes"].reverse()
    result["links"].reverse()
    return result


def _processor_schema(widget_name="strength", widget_type="FLOAT"):
    return {
        "input": {
            "required": {
                "image": ["IMAGE", {"forceInput": True}],
                widget_name: [widget_type, {"default": 0.5}],
            }
        },
        "input_order": {"required": ["image", widget_name]},
        "is_input_list": False,
        "output": ["IMAGE"],
        "output_name": ["image"],
        "output_is_list": [False],
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
                                                    "names": ["image_1", "image_2"],
                                                }
                                            },
                                        ],
                                    }
                                },
                            }
                        ]
                    },
                ],
            }
        },
        "input_order": {"required": ["prompt", "model"]},
        "is_input_list": False,
        "output": ["IMAGE"],
        "output_name": ["image"],
        "output_is_list": [False],
    }


def _nested_workflow():
    definition_id = "subgraph-demo"
    definition = {
        "id": definition_id,
        "name": "Nested Diamond",
        "inputs": [_slot("image", "IMAGE")],
        "outputs": [_slot("left", "IMAGE"), _slot("right", "IMAGE")],
        "nodes": [
            _node(
                2,
                "Processor",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                widgets=[0.25],
            ),
            _node(
                3,
                "Processor",
                inputs=(("image", "IMAGE"),),
                outputs=(("image", "IMAGE"),),
                widgets=[0.75],
            ),
        ],
        "links": [
            [1, -10, 0, 2, 0, "IMAGE"],
            [2, -10, 0, 3, 0, "IMAGE"],
            [3, 2, 0, -20, 0, "IMAGE"],
            [4, 3, 0, -20, 1, "IMAGE"],
        ],
    }
    return {
        "nodes": [
            _node(
                100,
                definition_id,
                inputs=(("image", "IMAGE"),),
                outputs=(("left", "IMAGE"), ("right", "IMAGE")),
            ),
            _node(2, "RootDuplicate"),
        ],
        "links": [],
        "definitions": {"subgraphs": [definition]},
    }


def test_filterless_and_filter_only_requests_list_without_authorizing_selection():
    catalog = _catalog(_diamond())
    result = resolve_workflow_branch(_request(catalog), catalog)
    assert result.status == "listed"
    assert result.selected is None
    assert result.needs_choice is False
    assert result.candidate_count > 1

    only_one = resolve_workflow_branch(
        _request(catalog, kinds=["split_arm"], writable=True, query=None),
        catalog,
    )
    assert only_one.status == "listed"
    assert only_one.selected is None


def test_exact_branch_id_is_authoritative_positive_evidence():
    catalog = _catalog(_diamond())
    arm = _arm_owning(catalog, 2)
    result = resolve_workflow_branch(_request(catalog, branch_id=arm.branch_id), catalog)
    assert result.status == "resolved"
    assert result.selected.branch_id == arm.branch_id
    assert "exact_branch_id" in result.selected.evidence
    assert result.read_only is True and result.queued is False


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("expected_workflow_identity", "workflow:stale", "stale_workflow_identity"),
        ("expected_graph_hash", "b" * 64, "stale_graph_hash"),
        ("expected_branch_catalog_hash", "c" * 64, "stale_branch_catalog_hash"),
    ],
)
def test_all_live_pins_fail_closed(field, value, code):
    catalog = _catalog(_diamond())
    request = _request(catalog, **{field: value})
    result = resolve_workflow_branch(request, catalog)
    assert result.status == "stale"
    assert result.selected is None
    assert [issue.code for issue in result.issues] == [code]


def test_query_ties_are_symmetric_and_never_alphabetically_selected():
    workflow = _diamond(left_type="UpscaleAlpha", right_type="UpscaleBeta")
    catalog = _catalog(workflow)
    request = _request(catalog, kinds=["split_arm"], query="the upscale branch")
    forward = resolve_workflow_branch(request, catalog)
    reversed_result = resolve_workflow_branch(request, _reversed_catalog(catalog))
    assert forward.status == reversed_result.status == "needs_choice"
    assert forward.selected is reversed_result.selected is None
    assert [item.branch_id for item in forward.candidates] == [
        item.branch_id for item in reversed_result.candidates
    ]


def test_title_and_semantic_alias_are_bounded_non_widget_query_evidence():
    workflow = _diamond(
        left_title="Hero Upscale Branch",
        right_title="Preview Output",
        left_alias="hero_upscale",
        right_alias="preview_output",
        left_widgets=["do-not-search-this-widget"],
        right_widgets=["another-hidden-widget"],
    )
    catalog = _catalog(workflow)
    title = resolve_workflow_branch(
        _request(catalog, kinds=["split_arm"], query="hero upscale"),
        catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
    )
    assert title.status == "resolved"
    assert title.selected.branch_id == _arm_owning(catalog, 2).branch_id
    alias = resolve_workflow_branch(
        _request(catalog, kinds=["split_arm"], query="preview output"),
        catalog,
        workflow=_reversed_workflow(workflow),
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
    )
    assert alias.status == "resolved"
    assert alias.selected.branch_id == _arm_owning(catalog, 3).branch_id
    widget = resolve_workflow_branch(
        _request(catalog, kinds=["split_arm"], query="do not search this widget"),
        catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
    )
    assert widget.status == "not_found"


def test_symmetric_title_tie_requires_choice_under_reversed_serialization():
    workflow = _diamond(left_title="Upscale", right_title="Upscale")
    catalog = _catalog(workflow)
    request = _request(catalog, kinds=["split_arm"], query="upscale")
    first = resolve_workflow_branch(
        request,
        catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
    )
    second = resolve_workflow_branch(
        request,
        _reversed_catalog(catalog),
        workflow=_reversed_workflow(workflow),
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
    )
    assert first.status == second.status == "needs_choice"
    assert [item.branch_id for item in first.candidates] == [
        item.branch_id for item in second.candidates
    ]


def test_interior_node_containing_and_downstream_discovery_are_exact():
    catalog = _catalog(_diamond())
    containing = resolve_workflow_branch(
        _request(
            catalog,
            kinds=["split_arm"],
            endpoint_anchor={"node_id": 2},
            direction="containing",
        ),
        catalog,
    )
    assert containing.status == "resolved"
    assert containing.selected.branch_id == _arm_owning(catalog, 2).branch_id

    downstream = resolve_workflow_branch(
        _request(
            catalog,
            kinds=["split_arm"],
            endpoint_anchor={"node_id": 2},
            direction="downstream",
        ),
        catalog,
    )
    assert downstream.status == "resolved"
    assert downstream.selected.branch_id == _arm_owning(catalog, 2).branch_id


def test_exact_slot_anchor_uses_declared_endpoint_type_and_reports_fanout_choice():
    catalog = _catalog(_diamond())
    request = _request(
        catalog,
        kinds=["split_arm"],
        endpoint_anchor={
            "node_id": 1,
            "slot_index": 0,
            "slot_name": "image",
            "type": "IMAGE",
            "endpoint_role": "source",
        },
        direction="downstream",
    )
    result = resolve_workflow_branch(request, catalog)
    assert result.status == "needs_choice"
    assert {item.branch_id for item in result.candidates} == {
        branch.branch_id for branch in _arms(catalog)
    }

    wrong_type = request.model_copy(
        update={"endpoint_anchor": request.endpoint_anchor.model_copy(update={"type": "VIDEO"})}
    )
    assert resolve_workflow_branch(wrong_type, catalog).status == "not_found"


def test_partial_slot_anchor_is_rejected_by_strict_request_model():
    catalog = _catalog(_diamond())
    with pytest.raises(ValidationError):
        _request(catalog, endpoint_anchor={"node_id": 1, "slot_index": 0})


def test_typed_integer_and_string_node_ids_never_collapse():
    workflow = {
        "nodes": [
            _node(1, "Source", outputs=(("image", "IMAGE"),)),
            _node(2, "Target", inputs=(("image", "IMAGE"),)),
            _node("2", "Target", inputs=(("image", "IMAGE"),)),
        ],
        "links": [
            [1, 1, 0, 2, 0, "IMAGE"],
            [2, 1, 0, "2", 0, "IMAGE"],
        ],
    }
    catalog = _catalog(workflow)
    integer = resolve_workflow_branch(
        _request(
            catalog,
            kinds=["segment"],
            endpoint_anchor={"node_id": 2},
            direction="containing",
        ),
        catalog,
    )
    string = resolve_workflow_branch(
        _request(
            catalog,
            kinds=["segment"],
            endpoint_anchor={"node_id": "2"},
            direction="containing",
        ),
        catalog,
    )
    assert integer.status == string.status == "resolved"
    assert integer.selected.branch_id != string.selected.branch_id


def test_candidate_and_node_lists_are_compact_and_explicitly_truncated():
    branch_count = 6
    nodes = [_node(1, "Source", outputs=(("image", "IMAGE"),))]
    nodes.extend(
        _node(
            index + 2,
            "Upscale",
            inputs=(("image", "IMAGE"),),
            outputs=(("image", "IMAGE"),),
        )
        for index in range(branch_count)
    )
    nodes.append(
        _node(
            100,
            "Merge",
            inputs=tuple((f"image_{index}", "IMAGE") for index in range(branch_count)),
        )
    )
    links = []
    for index in range(branch_count):
        node_id = index + 2
        links.append([index * 2, 1, 0, node_id, 0, "IMAGE"])
        links.append([index * 2 + 1, node_id, 0, 100, index, "IMAGE"])
    catalog = _catalog({"nodes": nodes, "links": links})
    result = resolve_workflow_branch(
        _request(catalog, kinds=["split_arm"], query="upscale", max_candidates=2),
        catalog,
    )
    assert result.status == "needs_choice"
    assert result.returned_candidate_count == 2
    assert result.candidate_count >= branch_count
    assert result.omitted_candidate_count == result.candidate_count - 2
    assert result.candidates_truncated is True


def test_comparison_reports_exact_boundary_structure_and_value_difference():
    workflow = _diamond(left_widgets=[0.25], right_widgets=[0.75])
    catalog = _catalog(workflow)
    left, right = _arms(catalog)
    result = compare_workflow_branches(
        _compare_request(catalog, left, right),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema()},
    )
    assert result.status == "compared"
    # The arms target different exact Merge slots, so their full boundary
    # structures correctly differ even though their owned classes match.
    assert result.structurally_equal is False
    assert result.value_equal is False
    assert result.left.values.status == result.right.values.status == "available"
    assert result.read_only is True and result.queued is False


def test_branch_fingerprint_is_stable_across_widget_and_title_changes():
    before = _catalog(
        _diamond(left_title="Before", right_title="Before", left_widgets=[0.1], right_widgets=[0.2])
    )
    after = _catalog(
        _diamond(left_title="After", right_title="After", left_widgets=[0.8], right_widgets=[0.9])
    )
    before_by_id = {branch.branch_id: branch.branch_fingerprint for branch in _arms(before)}
    after_by_id = {branch.branch_id: branch.branch_fingerprint for branch in _arms(after)}
    assert before_by_id == after_by_id


def test_values_and_dynamic_facts_are_unavailable_without_exact_schema_mapping():
    workflow = _diamond(left_widgets=[0.25], right_widgets=[0.75])
    catalog = _catalog(workflow)
    result = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
    )
    assert result.left.values.status == "unavailable"
    assert result.left.dynamic_facts.status == "unavailable"
    assert result.value_equal is None


def test_dynamic_selector_and_autogrow_connection_change_dynamic_digest():
    workflow = _diamond(
        left_type="GeminiNanoBanana2V2",
        right_type="GeminiNanoBanana2V2",
        left_widgets=["prompt", "Nano Banana 2", "2K"],
        right_widgets=["prompt", "Nano Banana 2", "2K"],
    )
    for node in workflow["nodes"]:
        if node["id"] in {2, 3}:
            node["inputs"][0]["name"] = "model.images.image_1"
    workflow["nodes"][1]["inputs"][0]["link"] = 10
    catalog = _catalog(workflow)
    result = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"GeminiNanoBanana2V2": _nano_schema()},
    )
    assert result.left.dynamic_facts.status == "available"
    assert result.right.dynamic_facts.status == "available"
    assert result.left.dynamic_facts.digest != result.right.dynamic_facts.digest
    kinds = {item.fact_kind for item in result.left.dynamic_facts.items}
    assert {"dynamic_selector", "dynamic_group", "dynamic_input"}.issubset(kinds)
    assert any(item.connected is True for item in result.left.dynamic_facts.items)


def test_duplicate_typed_node_anchor_across_scopes_fails_closed_until_scoped():
    workflow = _nested_workflow()
    catalog = _catalog(workflow)
    nested = next(scope for scope in catalog.scopes if scope.scope.kind == "subgraph_instance")
    ambiguous = resolve_workflow_branch(
        _request(catalog, endpoint_anchor={"node_id": 2}),
        catalog,
    )
    assert ambiguous.status == "needs_choice"
    assert [issue.code for issue in ambiguous.issues] == ["ambiguous_anchor_scope"]

    scoped = resolve_workflow_branch(
        _request(
            catalog,
            scope_path=nested.scope.scope_path,
            kinds=["split_arm"],
            endpoint_anchor={"node_id": 2},
        ),
        catalog,
    )
    assert scoped.status == "resolved"
    assert scoped.selected.scope_path == nested.scope.scope_path


def test_nested_scope_comparison_is_read_only_and_deterministic():
    workflow = _nested_workflow()
    catalog = _catalog(workflow)
    nested = next(scope for scope in catalog.scopes if scope.scope.kind == "subgraph_instance")
    arms = [branch for branch in nested.branches if branch.kind == "split_arm"]
    result = compare_workflow_branches(
        _compare_request(catalog, *arms),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema()},
    )
    assert result.status == "compared"
    assert result.left.scope_path == result.right.scope_path == nested.scope.scope_path
    assert result.left.writable is result.right.writable is True
    assert result.value_equal is False


def test_credentials_are_redacted_not_hashed_and_never_claim_equality():
    left_secret = "sk-live-left-super-secret"
    right_secret = "sk-live-right-super-secret"
    workflow = _diamond(left_widgets=[left_secret], right_widgets=[right_secret])
    catalog = _catalog(workflow)
    schema = _processor_schema("api_key", "STRING")
    schema["input"]["required"]["api_key"][1]["default"] = left_secret
    result = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": schema},
    )
    serialized = result.model_dump_json()
    assert left_secret not in serialized and right_secret not in serialized
    assert hashlib.sha256(left_secret.encode()).hexdigest() not in serialized
    assert hashlib.sha256(right_secret.encode()).hexdigest() not in serialized
    assert "api_key" not in serialized
    assert hashlib.sha256(b"api_key").hexdigest() not in serialized
    assert result.left.values.status == result.right.values.status == "partial"
    assert result.value_equal is None


def test_nested_credential_keys_and_schema_invalid_values_are_never_hashed():
    left_secret = "LEFT-NESTED-SENTINEL"
    right_secret = "RIGHT-NESTED-SENTINEL"
    workflow = _diamond(
        left_widgets=[{"clientSecret": left_secret}],
        right_widgets=[{"nested": {"accessToken": right_secret}}],
    )
    catalog = _catalog(workflow)
    result = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema("config", "STRING")},
    )

    serialized = result.model_dump_json()
    assert left_secret not in serialized and right_secret not in serialized
    assert hashlib.sha256(left_secret.encode()).hexdigest() not in serialized
    assert hashlib.sha256(right_secret.encode()).hexdigest() not in serialized
    assert result.left.values.status == result.right.values.status == "partial"
    assert result.value_equal is None


def test_schema_metadata_credential_description_is_redacted_before_hashing():
    left_secret = "LEFT-TOOLTIP-SENTINEL"
    right_secret = "RIGHT-TOOLTIP-SENTINEL"
    workflow = _diamond(
        left_widgets=[left_secret],
        right_widgets=[right_secret],
    )
    catalog = _catalog(workflow)
    schema = _processor_schema("config", "STRING")
    schema["input"]["required"]["config"][1].update(
        {"tooltip": "Enter API key for the hosted service"}
    )

    result = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": schema},
    )

    serialized = result.model_dump_json()
    assert left_secret not in serialized and right_secret not in serialized
    assert hashlib.sha256(left_secret.encode()).hexdigest() not in serialized
    assert hashlib.sha256(right_secret.encode()).hexdigest() not in serialized
    assert result.left.values.status == result.right.values.status == "partial"
    assert result.value_equal is None


def test_comparison_is_invariant_to_reversed_workflow_and_catalog_serialization():
    workflow = _diamond(left_widgets=[0.25], right_widgets=[0.75])
    catalog = _catalog(workflow)
    request = _compare_request(catalog, *_arms(catalog))
    forward = compare_workflow_branches(
        request,
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema()},
    )
    reversed_result = compare_workflow_branches(
        request,
        catalog=_reversed_catalog(catalog),
        workflow=_reversed_workflow(workflow),
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema()},
    )
    assert forward.model_dump(mode="json") == reversed_result.model_dump(mode="json")


def test_comparison_missing_branch_and_stale_pin_fail_closed():
    catalog = _catalog(_diamond())
    left, right = _arms(catalog)
    missing = _compare_request(catalog, left, right).model_copy(
        update={"right_branch_id": "f" * 64}
    )
    assert compare_workflow_branches(missing, catalog=catalog).status == "not_found"
    stale = _compare_request(catalog, left, right).model_copy(
        update={"expected_graph_hash": "b" * 64}
    )
    assert compare_workflow_branches(stale, catalog=catalog).status == "stale"


def test_comparison_rejects_raw_workflow_with_different_pinned_topology():
    workflow = _diamond()
    catalog = _catalog(workflow)
    changed = copy.deepcopy(workflow)
    changed["links"].pop()
    result = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=changed,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema()},
    )
    assert result.status == "invalid_catalog"
    assert [issue.code for issue in result.issues] == ["workflow_catalog_mismatch"]


def test_unattested_optional_workflow_bytes_fail_stale_before_query_or_compare():
    workflow = _diamond(left_title="Upscale", right_title="Preview")
    catalog = _catalog(workflow)
    changed = copy.deepcopy(workflow)
    changed["nodes"][1]["title"] = "Preview"
    changed["nodes"][2]["title"] = "Upscale"
    changed["nodes"][1]["widgets_values"] = [0.9]
    changed["nodes"][2]["widgets_values"] = [0.8]

    resolution = resolve_workflow_branch(
        _request(catalog, kinds=["split_arm"], query="upscale"),
        catalog,
        workflow=changed,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash="b" * 64,
    )
    comparison = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=changed,
        workflow_identity_attestation=WORKFLOW_IDENTITY,
        workflow_graph_hash="b" * 64,
        schema_mapping={"Processor": _processor_schema()},
    )
    wrong_identity = compare_workflow_branches(
        _compare_request(catalog, *_arms(catalog)),
        catalog=catalog,
        workflow=workflow,
        workflow_identity_attestation="workflow:other-tab",
        workflow_graph_hash=GRAPH_HASH,
        schema_mapping={"Processor": _processor_schema()},
    )

    assert resolution.status == "stale"
    assert comparison.status == "stale"
    assert wrong_identity.status == "stale"
    assert [issue.code for issue in resolution.issues] == [
        "workflow_graph_hash_unattested"
    ]
    assert [issue.code for issue in comparison.issues] == [
        "workflow_graph_hash_unattested"
    ]
    assert [issue.code for issue in wrong_identity.issues] == [
        "workflow_identity_unattested"
    ]
