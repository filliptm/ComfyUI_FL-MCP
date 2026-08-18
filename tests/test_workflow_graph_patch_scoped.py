from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import mcp_server
import pytest
from node_library import (
    NodeCatalogSnapshot,
    catalog_contract_hash,
    node_schema_hash,
    normalize_node_schema_contract,
)
from pydantic import ValidationError
from workflow_graph_patch import (
    SCOPED_GRAPH_PATCH_HASH_SCHEMA,
    SCOPED_GRAPH_PATCH_SCHEMA,
    ApplyScopedGraphPatchRequest,
    PlanScopedGraphPatchRequest,
    ScopedGraphPatchPlan,
    compile_scoped_graph_patch,
    scoped_graph_patch_hash,
    scoped_graph_patch_request_from_apply,
)
from workflow_scope import workflow_definition_hash

WORKFLOW_IDENTITY = "fl-mcp-workflow:scope-test:1"
ROOT_GRAPH_HASH = "1" * 64


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"fl-mcp-scoped-test:{label}"))


def _catalog() -> dict[str, Any]:
    return {
        "Pass": {
            "input": {"required": {"image": ["IMAGE", {"forceInput": True}]}},
            "output": ["IMAGE"],
            "output_name": ["image"],
            "python_module": "nodes",
        },
        "Replacement": {
            "input": {"required": {"image": ["IMAGE", {"forceInput": True}]}},
            "output": ["IMAGE"],
            "output_name": ["image"],
            "python_module": "custom_nodes.safe",
        },
    }


def _dynamic_target_schema() -> dict[str, Any]:
    return {
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
                                    "required": {"quality": [["fast", "high"]]}
                                },
                            },
                        ]
                    },
                ],
            }
        },
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
        "python_module": "custom_nodes.dynamic",
    }


def _node(node_id: int | str, node_type: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "inputs": [{"name": "image", "type": "IMAGE"}],
        "outputs": [{"name": "image", "type": "IMAGE"}],
        "widgets_values": [],
    }


def _definition(definition_id: str = "scope-def", *, node_id: int | str = 1) -> dict[str, Any]:
    input_id = _uuid(f"{definition_id}:input")
    output_id = _uuid(f"{definition_id}:output")
    return {
        "id": definition_id,
        "name": "Scoped definition",
        "version": 1,
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "inputs": [
            {
                "id": input_id,
                "name": "image",
                "type": "IMAGE",
                "linkIds": [1],
            }
        ],
        "outputs": [
            {
                "id": output_id,
                "name": "image",
                "type": "IMAGE",
                "linkIds": [2],
            }
        ],
        "nodes": [_node(node_id, "Pass")],
        "links": [
            [1, -10, 0, node_id, 0, "IMAGE"],
            [2, node_id, 0, -20, 0, "IMAGE"],
        ],
        "groups": [],
        "reroutes": [],
        "extra": {},
    }


def _workflow(*, reused: bool = False) -> dict[str, Any]:
    definition = _definition()
    nodes = [
        {
            "id": 100,
            "type": "scope-def",
            "inputs": [{"name": "image", "type": "IMAGE"}],
            "outputs": [{"name": "image", "type": "IMAGE"}],
            "widgets_values": [],
        }
    ]
    if reused:
        nodes.append(
            {
                "id": "second",
                "type": "scope-def",
                "inputs": [{"name": "image", "type": "IMAGE"}],
                "outputs": [{"name": "image", "type": "IMAGE"}],
                "widgets_values": [],
            }
        )
    return {
        "nodes": nodes,
        "links": [],
        "definitions": {"subgraphs": [definition]},
    }


def _completed_workflow() -> dict[str, Any]:
    workflow = _workflow()
    definition = workflow["definitions"]["subgraphs"][0]
    definition["nodes"] = [_node(2, "Replacement")]
    definition["links"] = [
        [1, -10, 0, 2, 0, "IMAGE"],
        [2, 2, 0, -20, 0, "IMAGE"],
    ]
    return workflow


def _path(node_id: int | str = 100) -> list[dict[str, Any]]:
    return [{"container_node_id": node_id, "subgraph_id": "scope-def"}]


def _boundary(kind: str) -> dict[str, Any]:
    return {
        "slot_id": _uuid(f"scope-def:{'input' if kind == 'scope_input' else 'output'}"),
        "slot_index": 0,
        "name": "image",
        "type": "IMAGE",
    }


def _source(ref: dict[str, Any]) -> dict[str, Any]:
    return {"ref": ref, "output_index": 0, "output": "image", "type": "IMAGE"}


def _target(ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": ref,
        "input_index": 0,
        "occurrence_index": 0,
        "socket_index": 0,
        "input": "image",
        "type": "IMAGE",
        "mode": "slot",
    }


def _edge(source_ref: dict[str, Any], target_ref: dict[str, Any]) -> dict[str, Any]:
    return {"source": _source(source_ref), "target": _target(target_ref)}


def _request(*, reused: bool = False, shared: bool = False) -> PlanScopedGraphPatchRequest:
    catalog = _catalog()
    input_ref = {"scope_input": _boundary("scope_input")}
    output_ref = {"scope_output": _boundary("scope_output")}
    old_ref = {"node_id": 1}
    new_ref = {"alias": "replacement"}
    entry = _edge(input_ref, old_ref)
    exit_ = _edge(old_ref, output_ref)
    affected = [_path()]
    if reused:
        affected = [_path(), _path("second")]
    return PlanScopedGraphPatchRequest.model_validate(
        {
            "application_id": "scope-replace-v1",
            "expected_workflow_identity": WORKFLOW_IDENTITY,
            "expected_graph_hash": ROOT_GRAPH_HASH,
            "expected_catalog_hash": catalog_contract_hash(catalog),
            "scope": {
                "kind": "subgraph_definition",
                "scope_path": _path(),
                "definition_id": "scope-def",
                "definition_hash": workflow_definition_hash(_definition()),
                "edit_mode": "shared_definition" if shared else "instance",
                "affected_scope_paths": affected,
            },
            "assertions": {
                "nodes": [
                    {
                        "ref": old_ref,
                        "node_type": "Pass",
                        "schema_hash": node_schema_hash("Pass", catalog["Pass"]),
                    }
                ],
                "edges": [entry, exit_],
            },
            "create_nodes": [
                {
                    "alias": "replacement",
                    "node_type": "Replacement",
                    "schema_hash": node_schema_hash("Replacement", catalog["Replacement"]),
                    "values": {},
                }
            ],
            "remove_edges": [entry, exit_],
            "add_edges": [_edge(input_ref, new_ref), _edge(new_ref, output_ref)],
            "remove_nodes": [
                {
                    "ref": old_ref,
                    "node_type": "Pass",
                    "schema_hash": node_schema_hash("Pass", catalog["Pass"]),
                    "expected_incident_edges": [entry, exit_],
                }
            ],
        }
    )


def _compile(
    request: PlanScopedGraphPatchRequest,
    *,
    workflow: dict[str, Any] | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_catalog = catalog or _catalog()
    return compile_scoped_graph_patch(
        request,
        workflow or _workflow(),
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=ROOT_GRAPH_HASH,
        catalog=active_catalog,
        catalog_hash=catalog_contract_hash(active_catalog),
        source="http://127.0.0.1:8188/object_info",
    )


def _codes(result: dict[str, Any]) -> set[str]:
    return {item["code"] for item in result["issues"]}


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"client": None, "node_catalog_store": None}
        )
    )


class _FakeCatalogClient:
    source = "http://127.0.0.1:8188/object_info"

    def __init__(self) -> None:
        self.data = _catalog()
        self.catalog_hash = catalog_contract_hash(self.data)

    async def catalog_snapshot(
        self,
        *,
        force_refresh: bool = False,
        max_age_seconds: float | None = None,
    ) -> NodeCatalogSnapshot:
        assert max_age_seconds == mcp_server.STRICT_CATALOG_FRESHNESS_SECONDS
        return NodeCatalogSnapshot(
            data=self.data,
            source=self.source,
            catalog_hash=self.catalog_hash,
            observed_catalog_hash=self.catalog_hash,
            catalog_hash_schema="fl-mcp.comfy-node-catalog-contract.v1",
            fetched_at=1.0,
            expires_at=2.0,
        )


def _active_workflow_result(
    workflow: dict[str, Any],
    *,
    workflow_identity: str = WORKFLOW_IDENTITY,
    graph_hash: str = ROOT_GRAPH_HASH,
) -> dict[str, Any]:
    return {
        "api_format": False,
        "workflow": deepcopy(workflow),
        "workflow_identity": workflow_identity,
        "workflow_identity_schema": mcp_server.WORKFLOW_IDENTITY_SCHEMA,
        "graph_hash": graph_hash,
        "graph_hash_schema": mcp_server.GRAPH_PRECONDITION_HASH_SCHEMA,
        "graph_patch_content_hash": None,
    }


def test_scoped_replace_round_trips_without_private_virtual_ids() -> None:
    result = _compile(_request())

    assert result["valid"] is True, result["issues"]
    assert result["schema"] == SCOPED_GRAPH_PATCH_SCHEMA
    assert result["patch_hash_schema"] == SCOPED_GRAPH_PATCH_HASH_SCHEMA
    assert result["plan"]["operation"] == "scoped_patch"
    assert result["plan"]["expected_delta"] == {
        "created_node_count": 1,
        "updated_node_count": 0,
        "removed_node_count": 1,
        "added_edge_count": 2,
        "removed_edge_count": 2,
        "final_node_count": 1,
        "final_edge_count": 2,
    }
    serialized = str(result)
    assert "'node_id': -10" not in serialized
    assert "'node_id': -20" not in serialized
    assert result["plan"]["add_edges"][0]["source"]["ref"] == {
        "scope_input": _boundary("scope_input")
    }
    assert result["plan"]["add_edges"][1]["target"]["ref"] == {
        "scope_output": _boundary("scope_output")
    }

    envelope = ApplyScopedGraphPatchRequest.model_validate(result["apply_request"])
    reconstructed = scoped_graph_patch_request_from_apply(envelope)
    replay = _compile(reconstructed)
    assert replay["valid"] is True
    assert replay["patch_hash"] == result["patch_hash"]
    assert replay["plan"] == result["plan"]


@pytest.mark.parametrize("field", ["slot_id", "slot_index", "name", "type"])
def test_scoped_boundary_identity_mismatch_fails_closed(field: str) -> None:
    payload = _request().model_dump(mode="json")
    identity = payload["add_edges"][0]["source"]["ref"]["scope_input"]
    replacements = {
        "slot_id": _uuid("wrong"),
        "slot_index": 1,
        "name": "wrong",
        "type": "MASK",
    }
    identity[field] = replacements[field]
    endpoint = payload["add_edges"][0]["source"]
    endpoint["output_index"] = identity["slot_index"]
    endpoint["output"] = identity["name"]
    endpoint["type"] = identity["type"]

    result = _compile(PlanScopedGraphPatchRequest.model_validate(payload))

    assert result["valid"] is False
    assert _codes(result) == {"scope_boundary_fact_mismatch"}


def test_boundary_endpoint_outer_facts_must_equal_embedded_identity() -> None:
    payload = _request().model_dump(mode="json")
    payload["add_edges"][0]["source"]["output"] = "wrong"

    with pytest.raises(ValidationError):
        PlanScopedGraphPatchRequest.model_validate(payload)


def test_direct_scope_input_to_output_addition_fails_closed() -> None:
    payload = _request().model_dump(mode="json")
    payload["create_nodes"] = []
    payload["add_edges"] = [
        _edge(
            {"scope_input": _boundary("scope_input")},
            {"scope_output": _boundary("scope_output")},
        )
    ]

    result = _compile(PlanScopedGraphPatchRequest.model_validate(payload))

    assert result["valid"] is False
    assert _codes(result) == {"direct_scope_boundary_edge_unsupported"}
    assert result["apply_request"] is None


def test_direct_scope_input_to_output_removal_fails_closed() -> None:
    payload = _request().model_dump(mode="json")
    payload["remove_edges"].append(
        _edge(
            {"scope_input": _boundary("scope_input")},
            {"scope_output": _boundary("scope_output")},
        )
    )

    result = _compile(PlanScopedGraphPatchRequest.model_validate(payload))

    assert result["valid"] is False
    assert _codes(result) == {"direct_scope_boundary_edge_unsupported"}
    assert result["apply_request"] is None


def test_scoped_apply_model_bounds_each_affected_scope_path() -> None:
    compiled = _compile(_request())
    assert compiled["valid"] is True, compiled["issues"]
    payload = deepcopy(compiled["apply_request"])
    payload["plan"]["scope"]["affected_scope_paths"] = [_path() * 33]

    with pytest.raises(ValidationError):
        ApplyScopedGraphPatchRequest.model_validate(payload)


@pytest.mark.parametrize(
    "slot_id",
    ["x" * 36, _uuid("noncanonical").upper()],
)
def test_scope_boundary_slot_ids_require_canonical_uuid_spelling(slot_id: str) -> None:
    payload = _request().model_dump(mode="json")
    payload["add_edges"][0]["source"]["ref"]["scope_input"]["slot_id"] = slot_id

    with pytest.raises(ValidationError):
        PlanScopedGraphPatchRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("endpoint", "wrong_ref"),
    [
        ("source", {"scope_output": _boundary("scope_output")}),
        ("target", {"scope_input": _boundary("scope_input")}),
    ],
)
def test_scope_boundary_refs_are_directional(
    endpoint: str,
    wrong_ref: dict[str, Any],
) -> None:
    payload = _request().model_dump(mode="json")
    payload["add_edges"][0][endpoint]["ref"] = wrong_ref

    with pytest.raises(ValidationError):
        PlanScopedGraphPatchRequest.model_validate(payload)


def test_private_virtual_node_refs_never_cross_the_public_contract() -> None:
    payload = _request().model_dump(mode="json")
    payload["add_edges"][0]["source"] = _source({"node_id": -10})

    result = _compile(PlanScopedGraphPatchRequest.model_validate(payload))

    assert result["valid"] is False
    assert _codes(result) == {"private_virtual_ref_forbidden"}


@pytest.mark.parametrize(
    "reserved_type",
    ["__fl_mcp_scope_input__", "__fl_mcp_scope_output__"],
)
def test_live_catalog_cannot_shadow_private_scope_boundary_types(
    reserved_type: str,
) -> None:
    catalog = _catalog()
    catalog[reserved_type] = {
        "input": {"required": {}},
        "output": ["IMAGE"],
        "output_name": ["real_custom_node_output"],
        "python_module": "custom_nodes.collision",
    }
    payload = _request().model_dump(mode="json")
    payload["expected_catalog_hash"] = catalog_contract_hash(catalog)

    result = _compile(
        PlanScopedGraphPatchRequest.model_validate(payload),
        catalog=catalog,
    )

    assert result["valid"] is False
    assert _codes(result) == {"scope_boundary_node_type_collision"}


@pytest.mark.parametrize(
    "reserved_type",
    ["__fl_mcp_scope_input__", "__fl_mcp_scope_output__"],
)
def test_public_scoped_create_cannot_name_private_boundary_type(
    reserved_type: str,
) -> None:
    payload = _request().model_dump(mode="json")
    payload["create_nodes"].append(
        {
            "alias": "private_shadow",
            "node_type": reserved_type,
            "schema_hash": "0" * 64,
            "values": {},
        }
    )

    result = _compile(PlanScopedGraphPatchRequest.model_validate(payload))

    assert result["valid"] is False
    assert _codes(result) == {"scope_boundary_node_type_reserved"}


def test_real_scoped_node_cannot_use_private_boundary_type() -> None:
    workflow = _completed_workflow()
    workflow["definitions"]["subgraphs"][0]["nodes"][0]["type"] = (
        "__fl_mcp_scope_input__"
    )
    payload = _request().model_dump(mode="json")
    payload["scope"]["definition_hash"] = workflow_definition_hash(
        workflow["definitions"]["subgraphs"][0]
    )

    result = _compile(
        PlanScopedGraphPatchRequest.model_validate(payload),
        workflow=workflow,
    )

    assert result["valid"] is False
    assert _codes(result) == {"scope_boundary_node_type_reserved"}


def test_scoped_attachments_are_rejected_by_the_public_model() -> None:
    payload = _request().model_dump(mode="json")
    payload["attachments"] = [
        {
            "ref": {"node_id": 1},
            "input_index": 0,
            "input": "image",
            "type": "IMAGE",
            "filename": "secret.png",
            "subfolder": "ren-chat",
            "file_type": "input",
            "size_bytes": 1,
            "sha256": "0" * 64,
        }
    ]

    with pytest.raises(ValidationError):
        PlanScopedGraphPatchRequest.model_validate(payload)


def test_reused_instance_edit_is_refused_and_shared_requires_complete_set() -> None:
    instance_result = _compile(_request(), workflow=_workflow(reused=True))
    assert instance_result["valid"] is False
    assert _codes(instance_result) == {"instance_detach_not_supported"}

    incomplete = _request(reused=True, shared=True).model_dump(mode="json")
    incomplete["scope"]["affected_scope_paths"] = [_path()]
    incomplete_result = _compile(
        PlanScopedGraphPatchRequest.model_validate(incomplete),
        workflow=_workflow(reused=True),
    )
    assert incomplete_result["valid"] is False
    assert _codes(incomplete_result) == {"affected_scope_paths_mismatch"}

    shared_result = _compile(
        _request(reused=True, shared=True),
        workflow=_workflow(reused=True),
    )
    assert shared_result["valid"] is True, shared_result["issues"]
    assert [
        path[0]["container_node_id"]
        for path in shared_result["plan"]["scope"]["affected_scope_paths"]
    ] == [100, "second"]


def test_root_scope_and_definition_stale_pins_stop_before_plan() -> None:
    request = _request()
    stale_root = compile_scoped_graph_patch(
        request,
        _workflow(),
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash="2" * 64,
        catalog=_catalog(),
        catalog_hash=catalog_contract_hash(_catalog()),
        source="test",
    )
    assert stale_root["valid"] is False
    assert _codes(stale_root) == {"graph_changed"}

    changed = _workflow()
    changed["definitions"]["subgraphs"][0]["name"] = "changed"
    stale_definition = _compile(request, workflow=changed)
    assert stale_definition["valid"] is False
    assert _codes(stale_definition) == {"scope_definition_changed"}


def test_affected_scope_order_is_canonical_and_hash_stable() -> None:
    first = _request(reused=True, shared=True)
    reversed_payload = deepcopy(first.model_dump(mode="json"))
    reversed_payload["scope"]["affected_scope_paths"].reverse()
    second = PlanScopedGraphPatchRequest.model_validate(reversed_payload)

    first_result = _compile(first, workflow=_workflow(reused=True))
    second_result = _compile(second, workflow=_workflow(reused=True))

    assert first_result["valid"] and second_result["valid"]
    assert first_result["patch_hash"] == second_result["patch_hash"]
    assert first_result["plan"] == second_result["plan"]


def test_scoped_operation_order_is_canonical_and_hash_stable() -> None:
    first = _request()
    reversed_payload = deepcopy(first.model_dump(mode="json"))
    reversed_payload["assertions"]["edges"].reverse()
    reversed_payload["remove_edges"].reverse()
    reversed_payload["add_edges"].reverse()
    reversed_payload["remove_nodes"][0]["expected_incident_edges"].reverse()
    second = PlanScopedGraphPatchRequest.model_validate(reversed_payload)

    first_result = _compile(first)
    second_result = _compile(second)

    assert first_result["valid"] and second_result["valid"]
    assert first_result["patch_hash"] == second_result["patch_hash"]
    assert first_result["plan"] == second_result["plan"]


def test_scoped_backend_idempotent_retry_is_plan_attested() -> None:
    compiled = _compile(_request())
    assert compiled["valid"] is True, compiled["issues"]
    request = ApplyScopedGraphPatchRequest.model_validate(compiled["apply_request"])
    workflow = _completed_workflow()
    workflow.setdefault("extra", {})["fl_mcp_graph_patch_ledger"] = {
        "schema": "fl-mcp.workflow-graph-patch.v2",
        "order": [request.application_id],
        "entries": {
            request.application_id: {
                "patch_hash": request.patch_hash,
                "result_content_hash": "c" * 64,
                "result_definition_hash": workflow_definition_hash(
                    workflow["definitions"]["subgraphs"][0]
                ),
                "aliases": {"replacement": 2},
                "created_node_ids": [2],
                "removed_node_ids": [1],
            }
        },
    }
    active = _active_workflow_result(workflow, graph_hash="d" * 64)
    active["graph_patch_content_hash"] = "c" * 64

    result = mcp_server._completed_graph_patch_result(
        active,
        request,
        catalog=_catalog(),
    )

    assert result["success"] is True
    assert result["already_applied"] is True
    assert result["patch_schema"] == SCOPED_GRAPH_PATCH_SCHEMA
    assert result["verification"]["idempotency_verified"] is True
    assert result["queued"] is False


def test_scoped_idempotency_rejects_same_count_asserted_edge_substitution() -> None:
    catalog = _catalog()
    base_request = _request()
    input_ref = {"scope_input": _boundary("scope_input")}
    old_ref = {"node_id": 1}
    retained_entry = _edge(input_ref, old_ref)
    retained_exit = _edge(old_ref, {"scope_output": _boundary("scope_output")})
    plan = ScopedGraphPatchPlan.model_validate(
        {
            "operation": "scoped_patch",
            "expected_workflow_identity": WORKFLOW_IDENTITY,
            "expected_graph_hash": ROOT_GRAPH_HASH,
            "scope": base_request.scope.model_dump(mode="json"),
            "assertions": {
                "nodes": [
                    {
                        "ref": old_ref,
                        "node_type": "Pass",
                        "schema_hash": node_schema_hash("Pass", catalog["Pass"]),
                    }
                ],
                "edges": [retained_entry, retained_exit],
            },
            "create_nodes": [
                {
                    "alias": "replacement",
                    "node_type": "Replacement",
                    "schema_hash": node_schema_hash(
                        "Replacement",
                        catalog["Replacement"],
                    ),
                    "values": {},
                }
            ],
            "expected_delta": {
                "created_node_count": 1,
                "updated_node_count": 0,
                "removed_node_count": 0,
                "added_edge_count": 0,
                "removed_edge_count": 0,
                "final_node_count": 2,
                "final_edge_count": 2,
            },
        }
    )
    catalog_hash = catalog_contract_hash(catalog)
    request = ApplyScopedGraphPatchRequest(
        application_id="scoped-asserted-edge-ledger",
        expected_catalog_hash=catalog_hash,
        patch_hash=scoped_graph_patch_hash(plan, catalog_hash),
        plan=plan,
    )
    workflow = _workflow()
    definition = workflow["definitions"]["subgraphs"][0]
    definition["nodes"].append(_node(2, "Replacement"))
    definition["links"] = [
        [1, -10, 0, 2, 0, "IMAGE"],
        [2, 1, 0, -20, 0, "IMAGE"],
    ]
    workflow.setdefault("extra", {})["fl_mcp_graph_patch_ledger"] = {
        "schema": "fl-mcp.workflow-graph-patch.v2",
        "order": [request.application_id],
        "entries": {
            request.application_id: {
                "patch_hash": request.patch_hash,
                "result_content_hash": "c" * 64,
                "result_definition_hash": workflow_definition_hash(definition),
                "aliases": {"replacement": 2},
                "created_node_ids": [2],
                "removed_node_ids": [],
            }
        },
    }
    active = _active_workflow_result(workflow, graph_hash="d" * 64)
    active["graph_patch_content_hash"] = "c" * 64

    result = mcp_server._completed_graph_patch_result(
        active,
        request,
        catalog=catalog,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_graph_patch_ledger"
    assert "completed_patch_asserted_edge_missing" in {
        issue["code"] for issue in result["validation"]["issues"]
    }
    assert result["queued"] is False


@pytest.mark.parametrize("inactive_exact", [False, True])
def test_scoped_idempotency_attests_exact_dynamic_live_socket_reprojection(
    inactive_exact: bool,
) -> None:
    catalog = _catalog()
    catalog.update(
        {
            "ImageSource": {
                "input": {},
                "output": ["IMAGE"],
                "output_name": ["image"],
                "python_module": "nodes",
            },
            "DynamicNanoTarget": _dynamic_target_schema(),
        }
    )
    pre_workflow = _workflow()
    pre_definition = pre_workflow["definitions"]["subgraphs"][0]
    pre_definition["nodes"].extend(
        [
            {
                "id": 2,
                "type": "ImageSource",
                "inputs": [],
                "outputs": [{"name": "image", "type": "IMAGE", "links": []}],
                "widgets_values": [],
            },
            {
                "id": 3,
                "type": "DynamicNanoTarget",
                "inputs": [
                    {
                        "name": f"runtime_prefix_{index}",
                        "type": "IMAGE",
                        "link": None,
                    }
                    for index in range(5)
                ]
                + [
                    {
                        "name": "model.images.image_1",
                        "type": "IMAGE",
                        "link": None,
                    }
                ],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                "widgets_values": ["prompt", "nano", "16:9", "1K", "MINIMAL"],
            },
        ]
    )
    scope = _request().scope.model_dump(mode="json")
    scope["definition_hash"] = workflow_definition_hash(pre_definition)
    added_edge = {
        "source": {
            "ref": {"node_id": 2},
            "output_index": 0,
            "output": "image",
            "type": "IMAGE",
        },
        "target": {
            "ref": {"node_id": 3},
            "input_index": 5,
            "occurrence_index": 0,
            "socket_index": 0,
            "input": "model.images.image_1",
            "type": "IMAGE",
            "mode": "slot",
        },
    }
    plan = ScopedGraphPatchPlan.model_validate(
        {
            "operation": "scoped_patch",
            "expected_workflow_identity": WORKFLOW_IDENTITY,
            "expected_graph_hash": ROOT_GRAPH_HASH,
            "scope": scope,
            "assertions": {
                "nodes": [
                    {
                        "ref": {"node_id": 2},
                        "node_type": "ImageSource",
                        "schema_hash": node_schema_hash(
                            "ImageSource",
                            catalog["ImageSource"],
                        ),
                    },
                    {
                        "ref": {"node_id": 3},
                        "node_type": "DynamicNanoTarget",
                        "schema_hash": node_schema_hash(
                            "DynamicNanoTarget",
                            catalog["DynamicNanoTarget"],
                        ),
                    },
                ],
                "edges": [],
            },
            "add_edges": [added_edge],
            "expected_delta": {
                "created_node_count": 0,
                "updated_node_count": 0,
                "removed_node_count": 0,
                "added_edge_count": 1,
                "removed_edge_count": 0,
                "final_node_count": 3,
                "final_edge_count": 3,
            },
        }
    )
    catalog_hash = catalog_contract_hash(catalog)
    request = ApplyScopedGraphPatchRequest(
        application_id="scoped-dynamic-retry-application",
        expected_catalog_hash=catalog_hash,
        patch_hash=scoped_graph_patch_hash(plan, catalog_hash),
        plan=plan,
    )
    workflow = deepcopy(pre_workflow)
    definition = workflow["definitions"]["subgraphs"][0]
    definition["nodes"][1]["outputs"][0]["links"] = [3]
    if inactive_exact:
        definition["nodes"][2]["widgets_values"][1] = "text-only"
        definition["nodes"][2]["inputs"] = [
            {
                "name": "model.images.image_1",
                "type": "IMAGE",
                "link": 3,
            }
        ]
        live_socket_index = 0
    else:
        definition["nodes"][2]["inputs"][5]["link"] = 3
        live_socket_index = 5
    definition["links"].append([3, 2, 0, 3, live_socket_index, "IMAGE"])
    workflow.setdefault("extra", {})["fl_mcp_graph_patch_ledger"] = {
        "schema": "fl-mcp.workflow-graph-patch.v2",
        "order": [request.application_id],
        "entries": {
            request.application_id: {
                "patch_hash": request.patch_hash,
                "result_content_hash": "c" * 64,
                "result_definition_hash": workflow_definition_hash(definition),
                "aliases": {},
                "created_node_ids": [],
                "removed_node_ids": [],
            }
        },
    }
    active = _active_workflow_result(workflow, graph_hash="d" * 64)
    active["graph_patch_content_hash"] = "c" * 64

    result = mcp_server._completed_graph_patch_result(
        active,
        request,
        catalog=catalog,
    )

    assert result["success"] is not inactive_exact
    if inactive_exact:
        assert result["error"]["code"] == "invalid_graph_patch_ledger"
        assert "completed_patch_added_edge_missing" in {
            issue["code"] for issue in result["validation"]["issues"]
        }
    else:
        assert result["already_applied"] is True
        assert result["verification"]["idempotency_verified"] is True
    assert result["queued"] is False


def test_scoped_completed_retry_requires_definition_hash_in_target_entry() -> None:
    compiled = _compile(_request())
    request = ApplyScopedGraphPatchRequest.model_validate(compiled["apply_request"])
    workflow = _completed_workflow()
    workflow.setdefault("extra", {})["fl_mcp_graph_patch_ledger"] = {
        "schema": "fl-mcp.workflow-graph-patch.v2",
        "order": [request.application_id],
        "entries": {
            request.application_id: {
                "patch_hash": request.patch_hash,
                "result_content_hash": "c" * 64,
                "aliases": {"replacement": 2},
                "created_node_ids": [2],
                "removed_node_ids": [1],
            }
        },
    }
    active = _active_workflow_result(workflow, graph_hash="d" * 64)
    active["graph_patch_content_hash"] = "c" * 64

    result = mcp_server._completed_graph_patch_result(
        active,
        request,
        catalog=_catalog(),
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_graph_patch_ledger"
    assert result["queued"] is False


def test_scoped_idempotency_accepts_attested_same_layout_no_op_retry() -> None:
    catalog = _catalog()
    workflow = _workflow()
    definition = workflow["definitions"]["subgraphs"][0]
    definition["nodes"][0]["pos"] = [10, 20]
    definition["nodes"][0]["size"] = [210, 120]
    scope = _request().scope.model_dump(mode="json")
    scope["definition_hash"] = workflow_definition_hash(definition)
    schema_hash = node_schema_hash("Pass", catalog["Pass"])
    plan = ScopedGraphPatchPlan.model_validate(
        {
            "operation": "scoped_patch",
            "expected_workflow_identity": WORKFLOW_IDENTITY,
            "expected_graph_hash": ROOT_GRAPH_HASH,
            "scope": scope,
            "assertions": {
                "nodes": [
                    {
                        "ref": {"node_id": 1},
                        "node_type": "Pass",
                        "schema_hash": schema_hash,
                    }
                ],
                "edges": [],
            },
            "update_nodes": [
                {
                    "ref": {"node_id": 1},
                    "node_type": "Pass",
                    "schema_hash": schema_hash,
                    "layout_hint": {
                        "x": 10,
                        "y": 20,
                        "width": 210,
                        "height": 120,
                    },
                }
            ],
            "expected_delta": {
                "created_node_count": 0,
                "updated_node_count": 1,
                "removed_node_count": 0,
                "added_edge_count": 0,
                "removed_edge_count": 0,
                "final_node_count": 1,
                "final_edge_count": 2,
            },
        }
    )
    catalog_hash = catalog_contract_hash(catalog)
    request = ApplyScopedGraphPatchRequest(
        application_id="scoped-no-op-retry-application",
        expected_catalog_hash=catalog_hash,
        patch_hash=scoped_graph_patch_hash(plan, catalog_hash),
        plan=plan,
    )
    workflow.setdefault("extra", {})["fl_mcp_graph_patch_ledger"] = {
        "schema": "fl-mcp.workflow-graph-patch.v2",
        "order": [request.application_id],
        "entries": {
            request.application_id: {
                "patch_hash": request.patch_hash,
                "result_content_hash": ROOT_GRAPH_HASH,
                "result_definition_hash": workflow_definition_hash(definition),
                "aliases": {},
                "created_node_ids": [],
                "removed_node_ids": [],
            }
        },
    }
    active = _active_workflow_result(workflow, graph_hash="d" * 64)
    active["graph_patch_content_hash"] = ROOT_GRAPH_HASH

    result = mcp_server._completed_graph_patch_result(
        active,
        request,
        catalog=catalog,
    )

    assert result["success"] is True
    assert result["already_applied"] is True
    assert result["verification"]["idempotency_verified"] is True
    assert result["queued"] is False


@pytest.mark.asyncio
async def test_scoped_apply_fresh_recompile_dispatches_exact_unchanged_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _compile(_request())
    assert compiled["valid"] is True, compiled["issues"]
    unchanged_envelope = deepcopy(compiled["apply_request"])
    request = ApplyScopedGraphPatchRequest.model_validate(unchanged_envelope)
    calls: list[tuple[str, dict[str, Any], int]] = []

    async def execute_tool(
        _ctx: Any,
        name: str,
        payload: dict[str, Any],
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        calls.append((name, deepcopy(payload), timeout_ms))
        if name == "workflow_get_current_json":
            return _active_workflow_result(_workflow())
        assert name == "apply_workflow_graph_patch"
        return {
            "success": True,
            "applied": True,
            "already_applied": False,
            "patch_schema": SCOPED_GRAPH_PATCH_SCHEMA,
            "operation": "scoped_patch",
            "application_id": request.application_id,
            "patch_hash": request.patch_hash,
            "expected_workflow_identity": WORKFLOW_IDENTITY,
            "graph_hash": "9" * 64,
            "aliases": {"replacement": 2},
            "created_node_ids": [2],
            "removed_node_ids": [1],
            "verification": {"valid": True, "issues": []},
            "rollback": {
                "attempted": False,
                "complete": True,
                "snapshot_restored": False,
                "hash_verified": False,
                "errors": [],
            },
            "queued": False,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **_kwargs: _FakeCatalogClient(),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_graph_patch.fn(request, _context())

    assert result["success"] is True
    assert result["applied"] is True
    assert result["validation"]["valid"] is True
    assert result["queued"] is False
    assert [name for name, _, _ in calls] == [
        "workflow_get_current_json",
        "apply_workflow_graph_patch",
    ]
    frontend_payload = calls[-1][1]
    schema_contracts = frontend_payload.pop("schema_contracts")
    assert frontend_payload == unchanged_envelope
    assert set(schema_contracts) == {"Pass", "Replacement"}
    for node_type, contract in schema_contracts.items():
        assert contract["schema"] == normalize_node_schema_contract(_catalog()[node_type])
    assert calls[-1][2] == 240_000
    assert not any("queue" in name for name, _, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("definition", "scope_definition_changed"),
        ("path", "scope_container_missing"),
        ("shared_instances", "affected_scope_paths_mismatch"),
    ],
)
async def test_scoped_apply_stale_scope_facts_stop_before_frontend_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected_code: str,
) -> None:
    planned_workflow = _workflow(reused=change == "shared_instances")
    plan_request = _request(
        reused=change == "shared_instances",
        shared=change == "shared_instances",
    )
    compiled = _compile(plan_request, workflow=planned_workflow)
    assert compiled["valid"] is True, compiled["issues"]
    request = ApplyScopedGraphPatchRequest.model_validate(compiled["apply_request"])
    active_workflow = deepcopy(planned_workflow)
    if change == "definition":
        active_workflow["definitions"]["subgraphs"][0]["name"] = "changed"
    elif change == "path":
        active_workflow["nodes"][0]["id"] = "moved"
    else:
        active_workflow["nodes"].append(
            {
                "id": "third",
                "type": "scope-def",
                "inputs": [{"name": "image", "type": "IMAGE"}],
                "outputs": [{"name": "image", "type": "IMAGE"}],
                "widgets_values": [],
            }
        )
    calls: list[str] = []

    async def execute_tool(
        _ctx: Any,
        name: str,
        _payload: dict[str, Any],
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        assert timeout_ms == 30_000
        calls.append(name)
        assert name == "workflow_get_current_json"
        return _active_workflow_result(active_workflow)

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **_kwargs: _FakeCatalogClient(),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_graph_patch.fn(request, _context())

    assert result["success"] is False
    assert result["applied"] is False
    assert result["error"]["code"] == "patch_invalid"
    assert {issue["code"] for issue in result["validation"]["issues"]} == {
        expected_code
    }
    assert result["queued"] is False
    assert calls == ["workflow_get_current_json"]
