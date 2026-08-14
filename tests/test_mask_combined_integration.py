import hashlib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import mcp_server
import pytest
from comfy_models import ComfyFolderType
from PIL import Image, ImageDraw

WORKFLOW_IDENTITY = "workflow-combined-mask-test"
GRAPH_HASH_BEFORE_PROMPT = "a" * 64
GRAPH_HASH_AFTER_PROMPT = "b" * 64
OLD_PROMPT = "brown-haired woman in a factory arrival scene"
IDENTITY_CLAUSE = "match the dusty woman's facial identity from image_2"


class FakeComfyTools:
    def __init__(self, input_root: Path, output_root: Path):
        self.input_root = input_root
        self.output_root = output_root

    def _iter_all_paths(self, folder_type):
        if folder_type == ComfyFolderType.INPUT:
            yield self.input_root
        elif folder_type == ComfyFolderType.OUTPUT:
            yield self.output_root
        else:
            raise AssertionError(f"Unexpected folder type: {folder_type}")


def fake_context():
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={
                "client": SimpleNamespace(session_id="combined-mask-session"),
            }
        ),
    )


def exact_user_topology():
    """The recorded image_1/image_2 graph, using serialized integer node IDs."""

    return {
        "nodes": [
            {
                "id": 1,
                "type": "LoadImage",
                "title": "LOAD & MASK IMAGE",
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [1]},
                    {"name": "MASK", "type": "MASK", "links": [2]},
                ],
            },
            {
                "id": 3,
                "type": "InpaintCropImproved",
                "inputs": [
                    {"name": "image", "type": "IMAGE", "link": 1},
                    {"name": "mask", "type": "MASK", "link": 2},
                ],
                "outputs": [
                    {"name": "cropped_image", "type": "IMAGE", "links": [3]},
                ],
            },
            {
                "id": 10,
                "type": "LoadImage",
                "title": "DUSTY WOMAN REFERENCE",
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [4]},
                    {"name": "MASK", "type": "MASK", "links": []},
                ],
            },
            {
                "id": 13,
                "type": "ImageResizeKJv2",
                "inputs": [
                    {"name": "image", "type": "IMAGE", "link": 4},
                ],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [5]},
                ],
            },
            {
                "id": 11,
                "type": "ImageBatchMulti",
                "inputs": [
                    {"name": "image_1", "type": "IMAGE", "link": 3},
                    {"name": "image_2", "type": "IMAGE", "link": 5},
                ],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [6]},
                ],
            },
            {
                "id": 34,
                "type": "PrimitiveStringMultiline",
                "title": "Generation prompt",
                "inputs": [],
                "outputs": [
                    {"name": "STRING", "type": "STRING", "links": [7]},
                ],
            },
            {
                "id": 2,
                "type": "GeminiImage2Node",
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": 6},
                    {"name": "prompt", "type": "STRING", "link": 7},
                ],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": []},
                ],
            },
        ],
        "links": [
            [1, 1, 0, 3, 0, "IMAGE"],
            [2, 1, 1, 3, 1, "MASK"],
            [3, 3, 0, 11, 0, "IMAGE"],
            [4, 10, 0, 13, 0, "IMAGE"],
            [5, 13, 0, 11, 1, "IMAGE"],
            [6, 11, 0, 2, 0, "IMAGE"],
            [7, 34, 0, 2, 1, "STRING"],
        ],
    }


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_combined_harness(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()

    reference_path = input_root / "dusty-reference.png"
    source_path = input_root / "factory-source.png"
    pending_path = input_root / "factory-source-mask-pending.png"
    Image.new("RGB", (3000, 1800), "#96745e").save(reference_path)
    Image.new("RGBA", (2400, 1600), (54, 42, 36, 255)).save(source_path)
    pending = Image.new("RGBA", (2400, 1600), (54, 42, 36, 255))
    alpha = pending.getchannel("A")
    ImageDraw.Draw(alpha).ellipse((1040, 360, 1370, 790), fill=0)
    pending.putalpha(alpha)
    pending.save(pending_path)

    refs = {
        10: {"filename": reference_path.name, "subfolder": "", "type": "input"},
        1: {"filename": source_path.name, "subfolder": "", "type": "input"},
    }
    # Live LiteGraph IDs may be strings even though the serialized workflow IDs
    # are integers. The frontend projects internally, then returns the canonical
    # serialized ID supplied by the resolver.
    runtime_nodes = {
        "10": {"id": "10", "image": refs[10]},
        "1": {"id": "1", "image": refs[1]},
    }
    state = {
        "graph_hash": GRAPH_HASH_BEFORE_PROMPT,
        "prompt": OLD_PROMPT,
        "selected_runtime_id": "10",
    }
    bridge_calls = []
    projection_log = []

    async def active_workflow(_ctx):
        return {
            "workflow": exact_user_topology(),
            "workflow_identity": WORKFLOW_IDENTITY,
            "graph_hash": state["graph_hash"],
        }

    async def execute_tool(_ctx, tool_name, parameters):
        bridge_calls.append((tool_name, parameters.copy()))
        if tool_name == "get_node_image_ref":
            assert parameters["expected_workflow_identity"] == WORKFLOW_IDENTITY
            assert parameters["expected_graph_hash"] == state["graph_hash"]
            serialized_id = parameters["node_id"]
            runtime_node = runtime_nodes[str(serialized_id)]
            assert type(serialized_id) is int
            assert type(runtime_node["id"]) is str
            projection_log.append((serialized_id, runtime_node["id"]))
            return {
                "success": True,
                "node_id": serialized_id,
                "node_type": "LoadImage",
                "image": runtime_node["image"],
                "workflow_identity": WORKFLOW_IDENTITY,
                "graph_hash": state["graph_hash"],
            }
        if tool_name == "get_node_values_exact":
            assert parameters == {
                "expected_workflow_identity": WORKFLOW_IDENTITY,
                "expected_graph_hash": state["graph_hash"],
                "node_id": 34,
            }
            return {
                "success": True,
                "node_id": 34,
                "values": {"value": state["prompt"]},
                "workflow_identity": WORKFLOW_IDENTITY,
                "graph_hash": state["graph_hash"],
            }
        if tool_name == "set_node_values_exact":
            assert parameters["node_id"] == 34
            assert parameters["widget_name"] == "value"
            assert parameters["expected_workflow_identity"] == WORKFLOW_IDENTITY
            assert parameters["expected_graph_hash"] == GRAPH_HASH_BEFORE_PROMPT
            assert parameters["expected_current_value"] == OLD_PROMPT
            assert parameters["value"] == f"{OLD_PROMPT}; {IDENTITY_CLAUSE}"
            assert parameters["expected_reference_node_id"] == 10
            assert parameters["expected_reference_image"] == refs[10]
            assert parameters["expected_reference_attestation"] == {
                "sha256": file_digest(reference_path),
                "size_bytes": reference_path.stat().st_size,
                "width": 3000,
                "height": 1800,
            }
            state["prompt"] = parameters["value"]
            state["graph_hash"] = GRAPH_HASH_AFTER_PROMPT
            return {
                "success": True,
                "node_id": 34,
                "widget_name": "value",
                "applied": ["value"],
                "verified": True,
                "queued": False,
                "workflow_identity": WORKFLOW_IDENTITY,
                "previous_graph_hash": GRAPH_HASH_BEFORE_PROMPT,
                "graph_hash": GRAPH_HASH_AFTER_PROMPT,
            }
        if tool_name == "edit_node_mask":
            assert parameters["node_id"] == 1
            assert parameters["expected_workflow_identity"] == WORKFLOW_IDENTITY
            assert parameters["expected_graph_hash"] == GRAPH_HASH_AFTER_PROMPT
            assert parameters["expected_source_image"] == refs[1]
            assert parameters["expected_source_attestation"] == {
                "sha256": file_digest(source_path),
                "size_bytes": source_path.stat().st_size,
                "width": 2400,
                "height": 1600,
            }
            assert parameters["coordinate_space"] == "normalized"
            assert parameters["clear_existing"] is True
            return {
                "success": True,
                "node_id": 1,
                "image": {
                    "filename": pending_path.name,
                    "subfolder": "",
                    "type": "input",
                },
                "review_required": True,
                "review_token": "pending-human-review",
                "queued": False,
            }
        raise AssertionError(f"Unexpected bridge tool (planner/retry/queue): {tool_name}")

    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(input_root, output_root),
    )
    monkeypatch.setattr(mcp_server, "_active_editable_workflow", active_workflow)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    return SimpleNamespace(
        state=state,
        refs=refs,
        bridge_calls=bridge_calls,
        projection_log=projection_log,
        reference_path=reference_path,
        source_path=source_path,
        pending_path=pending_path,
    )


@pytest.fixture(autouse=True)
def clear_context_tokens(monkeypatch):
    async def no_prior_narrow_receipt(ctx, claim, payload):
        del ctx, claim, payload
        raise mcp_server.FrontendToolExecutionError(
            "not found", code="narrow_edit_operation_not_found"
        )

    monkeypatch.setattr(mcp_server, "_recover_narrow_operation", no_prior_narrow_receipt)
    mcp_server._mask_context_tokens.clear()
    mcp_server._prompt_context_tokens.clear()
    mcp_server._narrow_edit_operations.clear()
    yield
    mcp_server._mask_context_tokens.clear()
    mcp_server._prompt_context_tokens.clear()
    mcp_server._narrow_edit_operations.clear()


@pytest.mark.asyncio
async def test_combined_reference_prompt_and_mask_lane_uses_four_agent_calls(tmp_path, monkeypatch):
    harness = install_combined_harness(tmp_path, monkeypatch)
    reference_digest = file_digest(harness.reference_path)
    source_digest = file_digest(harness.source_path)
    ctx = fake_context()
    agent_calls = []

    agent_calls.append("view_prompt_reference_image")
    reference = await mcp_server.view_prompt_reference_image.fn(
        mcp_server.ViewPromptReferenceImageRequest(max_dimension=512),
        ctx,
    )
    reference_result = reference.structured_content
    assert reference_result["producer_node_id"] == 10
    assert reference_result["direct_producer_node_id"] == 13
    assert reference_result["route_node_ids"] == [10, 13, 11]
    assert reference_result["consumer_node_id"] == 11
    assert reference_result["consumer_input"] == "image_2"
    assert reference_result["prompt_producer"]["producer_node_id"] == 34
    assert reference_result["prompt_producer"]["consumer_node_id"] == 2
    assert reference_result["prompt_context_token"]
    assert reference_result["source_image"] == harness.refs[10]
    assert reference_result["originalSize"] == {"width": 3000, "height": 1800}
    assert max(reference_result["previewSize"].values()) <= 512

    agent_calls.append("update_connected_prompt")
    prompt_update = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="combined-prompt-0001",
            prompt=IDENTITY_CLAUSE,
            operation="append",
            separator="; ",
            prompt_context_token=reference_result["prompt_context_token"],
            reference_image_used=True,
        ),
        ctx,
    )
    assert prompt_update["success"] is True
    assert prompt_update["producer_node_id"] == 34
    assert prompt_update["consumer_node_id"] == 2
    assert prompt_update["previous_graph_hash"] == GRAPH_HASH_BEFORE_PROMPT
    assert prompt_update["graph_hash"] == GRAPH_HASH_AFTER_PROMPT
    assert prompt_update["queued"] is False

    agent_calls.append("view_node_mask")
    mask_view = await mcp_server.view_node_mask.fn(
        mcp_server.ViewNodeMaskRequest(max_dimension=512),
        ctx,
    )
    mask_result = mask_view.structured_content
    assert mask_result["node_id"] == 1
    assert mask_result["resolution"] == "unique_topology_mask_source"
    assert mask_result["graph_hash"] == GRAPH_HASH_AFTER_PROMPT
    assert mask_result["source_image"] == harness.refs[1]
    assert mask_result["originalSize"] == {"width": 2400, "height": 1600}
    assert max(mask_result["previewSize"].values()) <= 512
    assert mask_result["mask"] == {"coveragePercent": 0.0, "bounds": None}

    agent_calls.append("edit_node_mask")
    pending = await mcp_server.edit_node_mask.fn(
        mcp_server.EditNodeMaskRequest(
            operation_id="combined-mask-0001",
            node_id=mask_result["node_id"],
            mask_context_token=mask_result["mask_context_token"],
            coordinate_space="normalized",
            clear_existing=True,
            regions=[{
                "shape": "ellipse",
                "x": 0.43,
                "y": 0.225,
                "width": 0.14,
                "height": 0.27,
            }],
        ),
        ctx,
    )
    pending_result = pending.structured_content
    assert pending_result["success"] is True
    assert pending_result["review_required"] is True
    assert pending_result["review_token"] == "pending-human-review"
    assert pending_result["queued"] is False
    assert pending_result["originalSize"] == {"width": 2400, "height": 1600}
    assert pending_result["previewSize"] == {"width": 2048, "height": 1365}

    assert agent_calls == [
        "view_prompt_reference_image",
        "update_connected_prompt",
        "view_node_mask",
        "edit_node_mask",
    ]
    bridge_names = [name for name, _ in harness.bridge_calls]
    assert bridge_names == [
        "get_node_image_ref",
        "get_node_image_ref",
        "get_node_values_exact",
        "set_node_values_exact",
        "get_node_values_exact",
        "get_node_image_ref",
        "get_node_image_ref",
        "edit_node_mask",
    ]
    assert Counter(harness.projection_log) == Counter({(10, "10"): 2, (1, "1"): 2})
    assert "get_selected_nodes" not in bridge_names
    assert not any(
        marker in name
        for name in bridge_names
        for marker in ("plan", "compile", "queue", "confirm")
    )
    assert file_digest(harness.reference_path) == reference_digest
    assert file_digest(harness.source_path) == source_digest
    with Image.open(harness.pending_path) as pending_image:
        assert pending_image.size == (2400, 1600)


@pytest.mark.asyncio
async def test_mask_inspected_before_prompt_hash_change_stales_before_frontend_edit(
    tmp_path,
    monkeypatch,
):
    harness = install_combined_harness(tmp_path, monkeypatch)
    ctx = fake_context()
    mask_view = await mcp_server.view_node_mask.fn(
        mcp_server.ViewNodeMaskRequest(max_dimension=512),
        ctx,
    )
    mask_result = mask_view.structured_content
    assert mask_result["graph_hash"] == GRAPH_HASH_BEFORE_PROMPT

    # This models the graph-precondition transition produced by the prompt edit.
    # A mask token issued before that transition must never be silently rebased.
    harness.state["prompt"] = f"{OLD_PROMPT}; {IDENTITY_CLAUSE}"
    harness.state["graph_hash"] = GRAPH_HASH_AFTER_PROMPT
    calls_before_edit = list(harness.bridge_calls)
    with pytest.raises(ValueError, match="active graph changed"):
        await mcp_server.edit_node_mask.fn(
            mcp_server.EditNodeMaskRequest(
                operation_id="combined-mask-0002",
                node_id=mask_result["node_id"],
                mask_context_token=mask_result["mask_context_token"],
                coordinate_space="normalized",
                clear_existing=True,
                regions=[{
                    "shape": "ellipse",
                    "x": 0.43,
                    "y": 0.225,
                    "width": 0.14,
                    "height": 0.27,
                }],
            ),
            ctx,
        )

    assert harness.bridge_calls == calls_before_edit
    assert [name for name, _ in harness.bridge_calls] == ["get_node_image_ref"]
