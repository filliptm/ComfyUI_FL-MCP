from copy import deepcopy
from types import SimpleNamespace

import mcp_server
import pytest
from node_library import (
    NodeCatalogSnapshot,
    catalog_contract_hash,
    normalize_node_schema_contract,
)
from workflow_capability_graph import VerifiedCapabilityLesson
from workflow_graph_patch import (
    ApplyGraphPatchRequest,
    GraphPatchPlan,
    graph_patch_hash,
)
from workflow_refinement_compiler import (
    CompileWorkflowRefinementSpecRequest,
)
from workflow_refinement_compiler import (
    compile_workflow_refinement_spec as compile_semantic_refinement,
)

CATALOG_HASH = "b" * 64
CONTENT_HASH = "c" * 64
WORKFLOW_IDENTITY = "fl-mcp-empty-tool-workflow-0001"
GRAPH_HASH = "a" * 64


def _context():
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"client": None, "node_catalog_store": None}
        )
    )


def _catalog() -> dict:
    return {
        "LoadImage": {
            "display_name": "Load Image",
            "category": "image",
            "input": {
                "required": {
                    "image": [
                        ["example.png"],
                        {"image_upload": True},
                    ]
                }
            },
            "output": ["IMAGE", "MASK"],
            "output_name": ["IMAGE", "MASK"],
            "python_module": "nodes",
        },
        "EmptyImage": {
            "display_name": "Empty Image",
            "category": "image",
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
        "CreateVideo": {
            "display_name": "Create Video",
            "category": "video",
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "fps": ["FLOAT", {"default": 30.0}],
                    "bit_depth": ["INT", {"default": 8}],
                }
            },
            "output": ["VIDEO"],
            "output_name": ["VIDEO"],
            "python_module": "comfy_extras.nodes_video",
        },
        "SaveVideo": {
            "display_name": "Save Video",
            "category": "video",
            "input": {
                "required": {
                    "video": ["VIDEO"],
                    "filename_prefix": ["STRING", {"default": "video/ComfyUI"}],
                    "format": [["auto", "mp4"], {"default": "auto"}],
                    "codec": [["auto", "h264"], {"default": "auto"}],
                }
            },
            "output": ["VIDEO"],
            "output_name": ["VIDEO"],
            "output_node": True,
            "python_module": "comfy_extras.nodes_video",
        },
    }


def _empty_workflow() -> dict:
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


def _semantic_request() -> CompileWorkflowRefinementSpecRequest:
    return CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "graph-patch-tool-build-0001",
            "create_nodes": [
                {
                    "alias": "canvas",
                    "capability": "create an empty image",
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
                    "capability": "save an image",
                    "requested_node_type": "SaveImage",
                    "values": {"filename_prefix": "ren-empty-tool-e2e"},
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


def _semantic_image_to_video_request() -> CompileWorkflowRefinementSpecRequest:
    return CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "graph-patch-image-to-video-0001",
            "allow_inferred_converters": True,
            "create_nodes": [
                {
                    "alias": "canvas",
                    "capability": "create an empty image",
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
                    "capability": "save a video",
                    "requested_node_type": "SaveVideo",
                    "values": {"filename_prefix": "ren-semantic-image-to-video"},
                },
            ],
            "add_edges": [
                {
                    "source_alias": "canvas",
                    "source_output": "IMAGE",
                    "target_alias": "save",
                    "target_input": "VIDEO",
                }
            ],
        }
    )


def _attachment_semantic_request() -> CompileWorkflowRefinementSpecRequest:
    return CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "graph-patch-attachment-tool-0001",
            "create_nodes": [
                {
                    "alias": "source",
                    "capability": "load the attached image",
                    "requested_node_type": "LoadImage",
                }
            ],
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


def _selected_layout_workflow() -> dict:
    return {
        "version": 0.4,
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


def _selected_layout_request() -> CompileWorkflowRefinementSpecRequest:
    return CompileWorkflowRefinementSpecRequest.model_validate(
        {
            "application_id": "graph-patch-selected-layout-0001",
            "existing_nodes": [
                {"alias": "target", "selected": True, "node_type": "EmptyImage"}
            ],
            "update_nodes": [
                {"target_alias": "target", "move_by": {"x": 50, "y": -25}}
            ],
        }
    )


class _FakeComfyTools:
    def __init__(self, input_root):
        self.input_root = input_root

    def _iter_all_paths(self, folder_type):
        return iter([self.input_root])


def _active_workflow_result(
    workflow: dict | None = None,
    *,
    workflow_identity: str = WORKFLOW_IDENTITY,
    graph_hash: str = GRAPH_HASH,
    graph_patch_content_hash: str | None = None,
) -> dict:
    return {
        "api_format": False,
        "workflow": deepcopy(workflow if workflow is not None else _empty_workflow()),
        "workflow_identity": workflow_identity,
        "workflow_identity_schema": mcp_server.WORKFLOW_IDENTITY_SCHEMA,
        "graph_hash": graph_hash,
        "graph_hash_schema": mcp_server.GRAPH_PRECONDITION_HASH_SCHEMA,
        "graph_patch_content_hash": graph_patch_content_hash,
    }


class _FakeCatalogClient:
    source = "http://127.0.0.1:8188/object_info"

    def __init__(self, *, catalog_hash: str | None = None):
        self.data = _catalog()
        self.catalog_hash = catalog_hash or catalog_contract_hash(self.data)

    async def catalog_snapshot(self, *, force_refresh: bool = False):
        assert force_refresh is True
        return NodeCatalogSnapshot(
            data=self.data,
            source=self.source,
            catalog_hash=self.catalog_hash,
            observed_catalog_hash=self.catalog_hash,
            catalog_hash_schema="fl-mcp.comfy-node-catalog-contract.v1",
            fetched_at=1.0,
            expires_at=2.0,
        )


def _compiled_request() -> ApplyGraphPatchRequest:
    catalog = _catalog()
    compiled = compile_semantic_refinement(
        _semantic_request(),
        _empty_workflow(),
        workflow_identity=WORKFLOW_IDENTITY,
        graph_hash=GRAPH_HASH,
        catalog=catalog,
        catalog_hash=catalog_contract_hash(catalog),
        source=_FakeCatalogClient.source,
    )
    assert compiled["valid"] is True, compiled["issues"]
    return ApplyGraphPatchRequest.model_validate(compiled["apply_request"])


def _request() -> ApplyGraphPatchRequest:
    plan = GraphPatchPlan.model_validate(
        {
            "operation": "patch",
            "expected_workflow_identity": "fl-mcp-workflow-before",
            "expected_graph_hash": "a" * 64,
            "assertions": {"nodes": [], "edges": []},
            "create_nodes": [
                {
                    "alias": "new_node",
                    "node_type": "EmptyImage",
                    "schema_hash": "d" * 64,
                    "values": {},
                }
            ],
            "expected_delta": {
                "created_node_count": 1,
                "updated_node_count": 0,
                "removed_node_count": 0,
                "added_edge_count": 0,
                "removed_edge_count": 0,
                "final_node_count": 1,
                "final_edge_count": 0,
            },
        }
    )
    return ApplyGraphPatchRequest(
        application_id="graph-patch-retry-0001",
        expected_catalog_hash=CATALOG_HASH,
        patch_hash=graph_patch_hash(plan, CATALOG_HASH),
        plan=plan,
    )


def _active(request: ApplyGraphPatchRequest) -> dict:
    return {
        "workflow_identity": request.plan.expected_workflow_identity,
        "graph_hash": "e" * 64,
        "graph_patch_content_hash": CONTENT_HASH,
        "workflow": {
            "extra": {
                "fl_mcp_graph_patch_ledger": {
                    "schema": "fl-mcp.workflow-graph-patch.v2",
                    "order": [request.application_id],
                    "entries": {
                        request.application_id: {
                            "patch_hash": request.patch_hash,
                            "result_content_hash": CONTENT_HASH,
                            "aliases": {"new_node": 9},
                            "created_node_ids": [9],
                            "removed_node_ids": [],
                        }
                    },
                }
            }
        },
    }


def test_backend_idempotency_survives_post_patch_graph_hash_change_in_same_workflow():
    request = _request()
    result = mcp_server._completed_graph_patch_result(_active(request), request)

    assert result["success"] is True
    assert result["applied"] is False
    assert result["already_applied"] is True
    assert result["verification"]["idempotency_verified"] is True
    assert result["created_node_ids"] == [9]
    assert result["queued"] is False


def test_backend_idempotency_does_not_cross_workflow_identity():
    request = _request()
    active = _active(request)
    active["workflow_identity"] = "fl-mcp-different-workflow"

    assert mcp_server._completed_graph_patch_result(active, request) is None


def test_backend_idempotency_rejects_changed_content_and_application_conflict():
    request = _request()
    changed = _active(request)
    changed["graph_patch_content_hash"] = "f" * 64
    changed_result = mcp_server._completed_graph_patch_result(changed, request)
    assert changed_result["error"]["code"] == "graph_patch_idempotency_conflict"

    conflicting = _active(request)
    conflicting["workflow"]["extra"]["fl_mcp_graph_patch_ledger"]["entries"][
        request.application_id
    ]["patch_hash"] = "0" * 64
    conflict_result = mcp_server._completed_graph_patch_result(conflicting, request)
    assert conflict_result["error"]["code"] == "graph_patch_idempotency_conflict"


def test_backend_idempotency_returns_none_when_application_was_not_recorded():
    request = _request()
    active = _active(request)
    active["workflow"]["extra"] = {}
    assert mcp_server._completed_graph_patch_result(active, request) is None


@pytest.mark.asyncio
async def test_compile_tool_passes_verified_lessons_to_semantic_planning(monkeypatch):
    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        assert name == "workflow_get_current_json"
        assert payload == {"api_format": False}
        return _active_workflow_result()

    prior = VerifiedCapabilityLesson(
        node_type="EmptyImage",
        schema_hash="d" * 64,
        payload={
            "evidence": "atomic_graph_patch_application",
            "source_node_type": "EmptyImage",
            "target_node_type": "SaveImage",
        },
    )
    captured = {}
    original_compile = mcp_server.compile_semantic_refinement

    def capture_compile(*args, **kwargs):
        captured["lessons"] = kwargs.get("verified_lessons")
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: _FakeCatalogClient(),
    )
    monkeypatch.setattr(
        mcp_server,
        "_active_verified_capability_lessons",
        lambda ctx: (prior,),
    )
    monkeypatch.setattr(mcp_server, "compile_semantic_refinement", capture_compile)

    planned = await mcp_server.compile_workflow_refinement_spec.fn(
        _semantic_request(),
        _context(),
    )

    assert planned["valid"] is True
    assert captured["lessons"] == (prior,)


@pytest.mark.asyncio
async def test_compile_and_apply_tools_round_trip_one_unchanged_empty_canvas_patch(
    monkeypatch,
):
    calls: list[tuple[str, dict, int]] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append((name, deepcopy(payload), timeout_ms))
        if name == "workflow_get_current_json":
            return _active_workflow_result()
        assert name == "apply_workflow_graph_patch"
        assert payload["schema_contracts"]
        return {
            "success": True,
            "applied": True,
            "already_applied": False,
            "patch_schema": "fl-mcp.workflow-graph-patch.v2",
            "operation": "patch",
            "application_id": payload["application_id"],
            "patch_hash": payload["patch_hash"],
            "expected_workflow_identity": payload["plan"]["expected_workflow_identity"],
            "graph_hash": "d" * 64,
            "aliases": {"canvas": 1, "save": 2},
            "created_node_ids": [1, 2],
            "removed_node_ids": [],
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
        lambda **kwargs: _FakeCatalogClient(),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    planned = await mcp_server.compile_workflow_refinement_spec.fn(
        _semantic_request(),
        _context(),
    )
    assert planned["valid"] is True, planned["issues"]
    assert planned["plan"]["expected_delta"] == {
        "created_node_count": 2,
        "updated_node_count": 0,
        "removed_node_count": 0,
        "added_edge_count": 1,
        "removed_edge_count": 0,
        "final_node_count": 2,
        "final_edge_count": 1,
    }
    unchanged_apply_envelope = deepcopy(planned["apply_request"])
    request = ApplyGraphPatchRequest.model_validate(unchanged_apply_envelope)

    result = await mcp_server.apply_workflow_graph_patch.fn(request, _context())

    assert result["success"] is True
    assert result["applied"] is True
    assert result["queued"] is False
    assert result["validation"]["valid"] is True
    assert [name for name, _, _ in calls] == [
        "workflow_get_current_json",
        "workflow_get_current_json",
        "apply_workflow_graph_patch",
    ]
    frontend_payload = calls[-1][1]
    schema_contracts = frontend_payload.pop("schema_contracts")
    assert frontend_payload == unchanged_apply_envelope
    assert set(schema_contracts) == {"EmptyImage", "SaveImage"}
    create_facts = {
        item["node_type"]: item["schema_hash"]
        for item in unchanged_apply_envelope["plan"]["create_nodes"]
    }
    for node_type, contract in schema_contracts.items():
        assert contract["schema_hash"] == create_facts[node_type]
        assert contract["schema"] == normalize_node_schema_contract(_catalog()[node_type])
    assert calls[-1][2] == 240000
    assert not any("queue" in name for name, _, _ in calls)


@pytest.mark.asyncio
async def test_inferred_float_default_survives_integral_json_apply_round_trip(
    monkeypatch,
):
    calls: list[tuple[str, dict, int]] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append((name, deepcopy(payload), timeout_ms))
        if name == "workflow_get_current_json":
            return _active_workflow_result()
        assert name == "apply_workflow_graph_patch"
        return {
            "success": True,
            "applied": True,
            "already_applied": False,
            "patch_schema": "fl-mcp.workflow-graph-patch.v2",
            "operation": "patch",
            "application_id": payload["application_id"],
            "patch_hash": payload["patch_hash"],
            "expected_workflow_identity": payload["plan"]["expected_workflow_identity"],
            "graph_hash": "d" * 64,
            "aliases": {
                "canvas": 1,
                next(
                    item["alias"]
                    for item in payload["plan"]["create_nodes"]
                    if item["node_type"] == "CreateVideo"
                ): 2,
                "save": 3,
            },
            "created_node_ids": [1, 2, 3],
            "removed_node_ids": [],
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
        lambda **kwargs: _FakeCatalogClient(),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    planned = await mcp_server.compile_workflow_refinement_spec.fn(
        _semantic_image_to_video_request(),
        _context(),
    )
    assert planned["valid"] is True, planned["issues"]
    converter = next(
        item
        for item in planned["apply_request"]["plan"]["create_nodes"]
        if item["node_type"] == "CreateVideo"
    )
    assert converter["values"]["fps"] == 30.0

    transported = deepcopy(planned["apply_request"])
    transported_converter = next(
        item
        for item in transported["plan"]["create_nodes"]
        if item["node_type"] == "CreateVideo"
    )
    transported_converter["values"]["fps"] = 30
    request = ApplyGraphPatchRequest.model_validate(transported)

    result = await mcp_server.apply_workflow_graph_patch.fn(request, _context())

    assert result["success"] is True
    assert result["applied"] is True
    assert result["queued"] is False
    assert result["validation"]["valid"] is True
    assert [name for name, _, _ in calls] == [
        "workflow_get_current_json",
        "workflow_get_current_json",
        "apply_workflow_graph_patch",
    ]
    frontend_converter = next(
        item
        for item in calls[-1][1]["plan"]["create_nodes"]
        if item["node_type"] == "CreateVideo"
    )
    assert frontend_converter["values"]["fps"] == 30.0
    assert not any("queue" in name for name, _, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected_nodes", "expected_valid", "issue_code"),
    [
        ([{"id": 2}], True, None),
        ([], False, "existing_selector_no_match"),
        ([{"id": 1}, {"id": 2}], False, "existing_selector_ambiguous"),
    ],
)
async def test_compile_tool_resolves_live_selection_internally_and_exactly_once(
    monkeypatch,
    selected_nodes,
    expected_valid,
    issue_code,
):
    calls: list[str] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append(name)
        assert timeout_ms == 30000
        if name == "workflow_get_current_json":
            assert payload == {"api_format": False}
            return _active_workflow_result(_selected_layout_workflow())
        assert name == "get_selected_nodes"
        assert payload == {}
        return {"nodes": deepcopy(selected_nodes)}

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: _FakeCatalogClient(),
    )

    planned = await mcp_server.compile_workflow_refinement_spec.fn(
        _selected_layout_request(),
        _context(),
    )

    assert planned["valid"] is expected_valid
    assert calls == ["workflow_get_current_json", "get_selected_nodes"]
    if expected_valid:
        assert planned["selection"][0]["selected"]["node_id"] == 2
        assert planned["plan"]["update_nodes"][0]["ref"] == {"node_id": 2}
        assert planned["plan"]["update_nodes"][0]["layout_hint"] == {
            "x": 150,
            "y": 175,
            "width": 220,
            "height": 130,
        }
    else:
        assert planned["plan"] is None
        assert planned["issues"][0]["code"] == issue_code
    assert not any("queue" in name for name in calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("application_id", "wrong-application"),
        ("patch_hash", "0" * 64),
        ("patch_schema", "stale-schema"),
        ("queued", True),
        ("verification", {"valid": False, "issues": []}),
        ("verification", None),
    ],
)
def test_frontend_success_requires_exact_request_and_verification_attestation(field, value):
    request = _compiled_request()
    result = {
        "success": True,
        "applied": True,
        "already_applied": False,
        "patch_schema": "fl-mcp.workflow-graph-patch.v2",
        "operation": "patch",
        "application_id": request.application_id,
        "patch_hash": request.patch_hash,
        "expected_workflow_identity": request.plan.expected_workflow_identity,
        "graph_hash": "d" * 64,
        "aliases": {"canvas": 1, "save": 2},
        "created_node_ids": [1, 2],
        "removed_node_ids": [],
        "verification": {"valid": True, "issues": []},
        "rollback": {"attempted": False, "complete": True, "errors": []},
        "queued": False,
    }
    result[field] = value

    with pytest.raises(RuntimeError, match="frontend"):
        mcp_server._attest_graph_patch_frontend_result(result, request)


@pytest.mark.asyncio
async def test_apply_tool_short_circuits_a_verified_backend_idempotent_retry(
    monkeypatch,
):
    request = _compiled_request()
    workflow = _empty_workflow()
    workflow["extra"]["fl_mcp_graph_patch_ledger"] = {
        "schema": "fl-mcp.workflow-graph-patch.v2",
        "order": [request.application_id],
        "entries": {
            request.application_id: {
                "patch_hash": request.patch_hash,
                "result_content_hash": CONTENT_HASH,
                "aliases": {"canvas": 11, "save": 12},
                "created_node_ids": [11, 12],
                "removed_node_ids": [],
            }
        },
    }
    calls: list[str] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append(name)
        assert name == "workflow_get_current_json"
        return _active_workflow_result(
            workflow,
            workflow_identity=request.plan.expected_workflow_identity,
            graph_hash="f" * 64,
            graph_patch_content_hash=CONTENT_HASH,
        )

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: pytest.fail("an idempotent retry must not refresh the catalog"),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_graph_patch.fn(request, _context())

    assert result["success"] is True
    assert result["applied"] is False
    assert result["already_applied"] is True
    assert result["created_node_ids"] == [11, 12]
    assert result["validation"]["idempotency_verified"] is True
    assert result["queued"] is False
    assert calls == ["workflow_get_current_json"]


@pytest.mark.asyncio
async def test_apply_tool_categorizes_catalog_drift_before_frontend_mutation(
    monkeypatch,
):
    request = _compiled_request()
    calls: list[str] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append(name)
        assert name == "workflow_get_current_json"
        return _active_workflow_result()

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: _FakeCatalogClient(catalog_hash="9" * 64),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    result = await mcp_server.apply_workflow_graph_patch.fn(request, _context())

    assert result["success"] is False
    assert result["applied"] is False
    assert result["error"]["code"] == "patch_invalid"
    assert result["queued"] is False
    assert any(
        issue["code"] == "catalog_changed"
        for issue in result["validation"]["issues"]
    )
    assert calls == ["workflow_get_current_json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "replaced"])
async def test_apply_rejects_deleted_or_replaced_compiler_attested_attachment(
    monkeypatch,
    tmp_path,
    change,
):
    input_root = tmp_path / "input"
    attachment_path = input_root / "ren-chat" / "session" / "subject.png"
    attachment_path.parent.mkdir(parents=True)
    original = b"compiler-attested-image-bytes"
    attachment_path.write_bytes(original)
    calls: list[str] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append(name)
        assert name == "workflow_get_current_json"
        return _active_workflow_result()

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: _FakeCatalogClient(),
    )
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: _FakeComfyTools(input_root),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    planned = await mcp_server.compile_workflow_refinement_spec.fn(
        _attachment_semantic_request(),
        _context(),
    )
    assert planned["valid"] is True, planned["issues"]
    attachment = planned["plan"]["attachments"][0]
    assert attachment["size_bytes"] == len(original)
    assert len(attachment["sha256"]) == 64
    unchanged_apply_envelope = deepcopy(planned["apply_request"])

    if change == "deleted":
        attachment_path.unlink()
    else:
        attachment_path.write_bytes(b"X" * len(original))

    result = await mcp_server.apply_workflow_graph_patch.fn(
        ApplyGraphPatchRequest.model_validate(unchanged_apply_envelope),
        _context(),
    )

    assert result["success"] is False
    assert result["applied"] is False
    assert result["error"]["code"] == "attachment_missing_or_changed"
    assert result["validation"]["issues"][0]["code"] == "attachment_missing_or_changed"
    assert result["queued"] is False
    assert calls == ["workflow_get_current_json", "workflow_get_current_json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "replaced"])
async def test_apply_rechecks_attachment_after_awaited_catalog_refresh(
    monkeypatch,
    tmp_path,
    change,
):
    input_root = tmp_path / "input"
    attachment_path = input_root / "ren-chat" / "session" / "subject.png"
    attachment_path.parent.mkdir(parents=True)
    original = b"compiler-attested-image-bytes"
    attachment_path.write_bytes(original)
    calls: list[str] = []

    async def execute_tool(ctx, name, payload, timeout_ms=30000):
        calls.append(name)
        assert name == "workflow_get_current_json"
        return _active_workflow_result()

    class MutatingCatalogClient(_FakeCatalogClient):
        def __init__(self):
            super().__init__()
            self.snapshot_count = 0

        async def catalog_snapshot(self, *, force_refresh: bool = False):
            self.snapshot_count += 1
            if self.snapshot_count == 2:
                if change == "deleted":
                    attachment_path.unlink()
                else:
                    attachment_path.write_bytes(b"X" * len(original))
            return await super().catalog_snapshot(force_refresh=force_refresh)

    catalog_client = MutatingCatalogClient()
    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "get_node_library_client",
        lambda **kwargs: catalog_client,
    )
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: _FakeComfyTools(input_root),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    planned = await mcp_server.compile_workflow_refinement_spec.fn(
        _attachment_semantic_request(),
        _context(),
    )
    assert planned["valid"] is True, planned["issues"]

    result = await mcp_server.apply_workflow_graph_patch.fn(
        ApplyGraphPatchRequest.model_validate(deepcopy(planned["apply_request"])),
        _context(),
    )

    assert catalog_client.snapshot_count == 2
    assert result["success"] is False
    assert result["applied"] is False
    assert result["error"]["code"] == "attachment_missing_or_changed"
    assert result["validation"]["issues"][0]["code"] == "attachment_missing_or_changed"
    assert result["queued"] is False
    assert calls == ["workflow_get_current_json", "workflow_get_current_json"]
