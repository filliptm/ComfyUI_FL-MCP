import asyncio
import hashlib
from types import SimpleNamespace

import mcp_server
import pytest
from comfy_models import ComfyFolderType
from mcp.types import ImageContent, TextContent
from PIL import Image, ImageDraw


class FakeComfyTools:
    def __init__(self, output_root, history, input_root=None):
        self.output_root = output_root
        self.input_root = input_root or output_root
        self.history = history

    async def fetch_history(self, prompt_id=None, max_items=10):
        del max_items
        if prompt_id:
            return self.history.get(prompt_id)
        return self.history

    def _iter_all_paths(self, folder_type):
        if folder_type == ComfyFolderType.OUTPUT:
            yield self.output_root
        elif folder_type == ComfyFolderType.INPUT:
            yield self.input_root
        else:
            raise AssertionError(f"Unexpected folder type: {folder_type}")


def fake_context():
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={
                "client": SimpleNamespace(session_id="test-session"),
            }
        ),
    )


async def async_value(value):
    return value


def image_attestation(path):
    payload = path.read_bytes()
    with Image.open(path) as image:
        width, height = image.size
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "width": width,
        "height": height,
    }


def active_mask_workflow(node_id=7, *, graph_hash="a" * 64):
    return {
        "workflow": {
            "nodes": [
                {
                    "id": node_id,
                    "type": "LoadImage",
                    "inputs": [],
                    "outputs": [
                        {"name": "IMAGE", "type": "IMAGE", "links": []},
                        {"name": "MASK", "type": "MASK", "links": []},
                    ],
                }
            ],
            "links": [],
        },
        "workflow_identity": "workflow-mask-test",
        "graph_hash": graph_hash,
    }


def active_prompt_workflow():
    return {
        "workflow": {
            "nodes": [
                {
                    "id": 10,
                    "type": "ReferenceImageEditor",
                    "inputs": [
                        {"name": "image_2", "type": "IMAGE", "link": 1},
                        {"name": "prompt", "type": "STRING", "link": 2},
                    ],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                },
                {
                    "id": 33,
                    "type": "LoadImage",
                    "inputs": [],
                    "outputs": [
                        {"name": "IMAGE", "type": "IMAGE", "links": [1]},
                        {"name": "MASK", "type": "MASK", "links": []},
                    ],
                },
                {
                    "id": 34,
                    "type": "PrimitiveStringMultiline",
                    "inputs": [],
                    "outputs": [{"name": "STRING", "type": "STRING", "links": [2]}],
                },
            ],
            "links": [
                [1, 33, 0, 10, 0, "IMAGE"],
                [2, 34, 0, 10, 1, "STRING"],
            ],
        },
        "workflow_identity": "workflow-prompt-test",
        "graph_hash": "b" * 64,
    }


def active_direct_prompt_workflow(*, widget_name="prompt", graph_hash="d" * 64):
    return {
        "workflow": {
            "nodes": [
                {
                    "id": 33,
                    "type": "LoadImage",
                    "inputs": [],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                },
                {
                    "id": 8,
                    "type": "GeminiNanoBanana2V2",
                    "inputs": [
                        {
                            "name": "prompt",
                            "type": "STRING",
                            "widget": {"name": widget_name},
                            "link": None,
                        },
                        {
                            "name": "system_prompt",
                            "type": "STRING",
                            "widget": {"name": "system_prompt"},
                            "link": None,
                        },
                        {"name": "image_2", "type": "IMAGE", "link": 1},
                    ],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
                },
            ],
            "links": [[1, 33, 0, 8, 2, "IMAGE"]],
        },
        "workflow_identity": "workflow-direct-prompt",
        "graph_hash": graph_hash,
    }


@pytest.fixture(autouse=True)
def clear_mask_context_tokens(monkeypatch):
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


def test_vision_preview_stays_below_claude_transport_limit():
    noise = Image.effect_noise((1600, 900), 100).convert("L")
    opaque = Image.merge(
        "RGBA",
        (noise, noise, noise, Image.new("L", noise.size, 255)),
    )

    content, preview_format, preview_size = mcp_server._bounded_vision_preview(
        opaque,
        2048,
    )

    assert preview_format == "jpeg"
    assert len(content) <= mcp_server.MCP_VISION_PREVIEW_MAX_BYTES
    assert preview_size == (1600, 900)
    assert opaque.size == (1600, 900)


def test_transparent_vision_preview_is_bounded_without_dropping_alpha():
    noise = Image.effect_noise((1600, 900), 100).convert("L")
    alpha = Image.new("L", noise.size, 255)
    ImageDraw.Draw(alpha).rectangle((0, 0, 799, 899), fill=0)
    transparent = Image.merge("RGBA", (noise, noise, noise, alpha))

    content, preview_format, preview_size = mcp_server._bounded_vision_preview(
        transparent,
        2048,
    )

    assert preview_format == "png"
    assert len(content) <= mcp_server.MCP_VISION_PREVIEW_MAX_BYTES
    assert max(preview_size) < 1600


def test_output_candidates_preserve_node_and_image_order():
    candidates = mcp_server._output_image_candidates({
        "preview": {"images": [{"filename": "preview.png", "type": "temp"}]},
        "save": {"images": [
            {"filename": "final-1.png", "subfolder": "run"},
            {"filename": "final-2.png", "subfolder": "run"},
        ]},
    })

    assert [item["nodeId"] for item in candidates] == ["preview", "save", "save"]
    assert candidates[-1]["filename"] == "final-2.png"
    assert mcp_server._output_image_candidates(
        {"save": {"images": [{"filename": "final.png"}]}},
        "missing",
    ) == []


@pytest.mark.asyncio
async def test_view_output_image_returns_bounded_visual_content(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    image_folder = output_root / "run"
    image_folder.mkdir(parents=True)
    image_path = image_folder / "final.png"
    Image.new("RGB", (3000, 1500), "#4d7cff").save(image_path)
    history = {
        "prompt-newest": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "29": {"images": [{
                    "filename": "final.png",
                    "subfolder": "run",
                    "type": "output",
                }]},
            },
        },
    }
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(output_root, history),
    )

    result = await mcp_server.view_output_image.fn(
        mcp_server.ViewOutputImageRequest(max_dimension=1024),
        fake_context(),
    )

    assert result.structured_content["promptId"] == "prompt-newest"
    assert result.structured_content["nodeId"] == "29"
    assert result.structured_content["relativePath"] == "output/run/final.png"
    assert result.structured_content["image"] == {
        "filename": "final.png",
        "subfolder": "run",
        "type": "output",
    }
    assert result.structured_content["originalSize"] == {"width": 3000, "height": 1500}
    assert result.structured_content["previewSize"] == {"width": 1024, "height": 512}
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.content[1], ImageContent)
    assert result.content[1].mimeType == "image/jpeg"
    assert len(result.content[1].data) > 100


@pytest.mark.asyncio
async def test_view_chat_image_returns_uploaded_input_as_visual_content(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    chat_folder = input_root / "ren-chat" / "session-1"
    chat_folder.mkdir(parents=True)
    Image.new("RGB", (800, 600), "#ef66aa").save(chat_folder / "reference.png")
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root=input_root),
    )

    result = await mcp_server.view_chat_image.fn(
        mcp_server.ViewChatImageRequest(image={
            "filename": "reference.png",
            "subfolder": "ren-chat/session-1",
            "type": "input",
        }),
        fake_context(),
    )

    assert result.structured_content["image"]["type"] == "input"
    assert result.structured_content["originalSize"] == {"width": 800, "height": 600}
    assert isinstance(result.content[1], ImageContent)


def test_output_path_resolution_rejects_traversal(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir()
    (tmp_path / "secret.png").write_bytes(b"not-an-image")
    tools = FakeComfyTools(output_root, {})

    with pytest.raises(mcp_server.ComfyUINotFoundError):
        mcp_server._resolve_comfy_image_path(tools, {
            "filename": "../secret.png",
            "subfolder": "",
            "type": "output",
        })


def test_mask_overlay_preview_reports_coverage_and_bounds(tmp_path):
    image_path = tmp_path / "masked.png"
    image = Image.new("RGBA", (100, 50), (40, 80, 120, 255))
    alpha = image.getchannel("A")
    ImageDraw.Draw(alpha).rectangle((10, 5, 29, 14), fill=0)
    image.putalpha(alpha)
    image.save(image_path)

    preview, preview_format, original_size, preview_size, mask = (
        mcp_server._mask_overlay_preview(image_path, 512)
    )

    assert preview_format == "jpeg"
    assert len(preview) > 100
    assert original_size == (100, 50)
    assert preview_size == (100, 50)
    assert mask == {
        "coveragePercent": 4.0,
        "bounds": {"x": 10, "y": 5, "width": 20, "height": 10},
    }


def test_normalized_mask_regions_must_fit_inside_image():
    with pytest.raises(ValueError, match="0..1"):
        mcp_server.EditNodeMaskRequest(
            operation_id="mask-test-0001",
            node_id=1,
            mask_context_token="mask-context-token",
            coordinate_space="normalized",
            regions=[{
                "x": 0.8,
                "y": 0.2,
                "width": 0.3,
                "height": 0.2,
            }],
        )


def test_normalized_polygon_mask_region_is_accepted_and_serialized():
    request = mcp_server.EditNodeMaskRequest(
        operation_id="mask-test-0002",
        node_id=1,
        mask_context_token="mask-context-token",
        coordinate_space="normalized",
        regions=[{
            "shape": "polygon",
            "points": [
                {"x": 0.1, "y": 0.1},
                {"x": 0.8, "y": 0.2},
                {"x": 0.4, "y": 0.9},
            ],
        }],
    )

    region = request.model_dump()["regions"][0]
    assert region["shape"] == "polygon"
    assert region["points"] == [
        {"x": 0.1, "y": 0.1},
        {"x": 0.8, "y": 0.2},
        {"x": 0.4, "y": 0.9},
    ]


@pytest.mark.parametrize(
    "region",
    [
        {"shape": "polygon", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]},
        {
            "shape": "polygon",
            "points": [{"x": index, "y": index % 2} for index in range(65)],
        },
        {
            "shape": "polygon",
            "points": [{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 2, "y": 2}],
        },
        {
            "shape": "polygon",
            "x": 0,
            "y": 0,
            "width": 1,
            "height": 1,
            "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0, "y": 1}],
        },
        {
            "shape": "rectangle",
            "x": 0,
            "y": 0,
            "width": 1,
            "height": 1,
            "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0, "y": 1}],
        },
    ],
)
def test_invalid_polygon_geometry_is_rejected(region):
    with pytest.raises(ValueError):
        mcp_server.EditNodeMaskRequest(
            operation_id="mask-test-0003",
            node_id=1,
            mask_context_token="mask-context-token",
            regions=[region],
        )


def test_normalized_polygon_points_must_fit_inside_image():
    with pytest.raises(ValueError, match="polygon points"):
        mcp_server.EditNodeMaskRequest(
            operation_id="mask-test-0004",
            node_id=1,
            mask_context_token="mask-context-token",
            coordinate_space="normalized",
            regions=[{
                "shape": "polygon",
                "points": [
                    {"x": 0.1, "y": 0.1},
                    {"x": 1.1, "y": 0.2},
                    {"x": 0.4, "y": 0.9},
                ],
            }],
        )


@pytest.mark.asyncio
async def test_view_node_mask_returns_visual_overlay(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    image = Image.new("RGBA", (64, 32), (20, 40, 60, 255))
    alpha = image.getchannel("A")
    ImageDraw.Draw(alpha).rectangle((4, 5, 11, 12), fill=0)
    image.putalpha(alpha)
    image.save(input_root / "mask.png")
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        assert tool_name == "get_node_image_ref"
        assert parameters == {
            "node_id": 7,
            "expected_workflow_identity": "workflow-mask-test",
            "expected_graph_hash": "a" * 64,
        }
        return {
            "node_id": 7,
            "node_type": "LoadImage",
            "title": "LOAD & MASK IMAGE",
            "image": {"filename": "mask.png", "subfolder": "", "type": "input"},
            "workflow_identity": "workflow-mask-test",
            "graph_hash": "a" * 64,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_mask_workflow()),
    )

    result = await mcp_server.view_node_mask.fn(
        mcp_server.ViewNodeMaskRequest(node_id=7),
        fake_context(),
    )

    assert result.structured_content["mask"]["coveragePercent"] == 3.125
    assert result.structured_content["node_id"] == 7
    assert result.structured_content["mask_context_token"]
    assert result.structured_content["source_attestation"] == image_attestation(
        input_root / "mask.png"
    )
    assert [item.type for item in result.content] == ["text", "image"]


@pytest.mark.asyncio
async def test_edit_node_mask_returns_saved_mask_overlay(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    Image.new("RGBA", (40, 20), (20, 40, 60, 255)).save(input_root / "edited.png")
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        if tool_name == "get_node_image_ref":
            return {
                "node_id": 7,
                "image": {"filename": "edited.png", "subfolder": "", "type": "input"},
                "workflow_identity": "workflow-mask-test",
                "graph_hash": "a" * 64,
            }
        assert tool_name == "edit_node_mask"
        assert parameters["clear_existing"] is True
        assert "mask_context_token" not in parameters
        assert parameters["expected_workflow_identity"] == "workflow-mask-test"
        assert parameters["expected_graph_hash"] == "a" * 64
        assert parameters["expected_source_image"] == {
            "filename": "edited.png",
            "subfolder": "",
            "type": "input",
        }
        return {
            "success": True,
            "node_id": 7,
            "image": {"filename": "edited.png", "subfolder": "", "type": "input"},
            "review_required": True,
            "review_token": "review-1",
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_mask_workflow()),
    )
    context_token, _ = mcp_server._mask_context_tokens.issue(
        session_id="test-session",
        workflow_identity="workflow-mask-test",
        graph_hash="a" * 64,
        node_id=7,
        source_image={"filename": "edited.png", "subfolder": "", "type": "input"},
        source_attestation=image_attestation(input_root / "edited.png"),
    )

    result = await mcp_server.edit_node_mask.fn(
        mcp_server.EditNodeMaskRequest(
            operation_id="mask-test-0005",
            node_id=7,
            mask_context_token=context_token,
            clear_existing=True,
            regions=[{"x": 1, "y": 2, "width": 3, "height": 4}],
        ),
        fake_context(),
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["review_required"] is True
    assert result.structured_content["review_token"] == "review-1"
    assert [item.type for item in result.content] == ["text", "image"]


@pytest.mark.asyncio
async def test_edit_node_mask_rejects_stale_graph_before_frontend_upload(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    calls = []

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, parameters
        calls.append(tool_name)
        raise AssertionError("stale graph must fail before a frontend mask call")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_mask_workflow(graph_hash="b" * 64)),
    )
    context_token, _ = mcp_server._mask_context_tokens.issue(
        session_id="test-session",
        workflow_identity="workflow-mask-test",
        graph_hash="a" * 64,
        node_id=7,
        source_image={"filename": "source.png", "type": "input"},
    )

    with pytest.raises(ValueError, match="graph changed"):
        await mcp_server.edit_node_mask.fn(
            mcp_server.EditNodeMaskRequest(
                operation_id="mask-test-0006",
                node_id=7,
                mask_context_token=context_token,
                regions=[{"x": 1, "y": 2, "width": 3, "height": 4}],
            ),
            fake_context(),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_edit_node_mask_rejects_same_reference_byte_overwrite_before_frontend(
    tmp_path,
    monkeypatch,
):
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_path = input_root / "source.png"
    Image.new("RGBA", (80, 40), (10, 20, 30, 255)).save(source_path)
    inspected = image_attestation(source_path)
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_mask_workflow()),
    )
    calls = []

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, parameters
        calls.append(tool_name)
        if tool_name == "get_node_image_ref":
            return {
                "node_id": 7,
                "image": {"filename": "source.png", "subfolder": "", "type": "input"},
                "workflow_identity": "workflow-mask-test",
                "graph_hash": "a" * 64,
            }
        raise AssertionError("same-reference overwrite must fail before frontend mutation")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    context_token, _ = mcp_server._mask_context_tokens.issue(
        session_id="test-session",
        workflow_identity="workflow-mask-test",
        graph_hash="a" * 64,
        node_id=7,
        source_image={"filename": "source.png", "subfolder": "", "type": "input"},
        source_attestation=inspected,
    )
    Image.new("RGBA", (80, 40), (200, 30, 40, 255)).save(source_path)

    with pytest.raises(ValueError, match="source bytes changed"):
        await mcp_server.edit_node_mask.fn(
            mcp_server.EditNodeMaskRequest(
                operation_id="mask-test-0007",
                node_id=7,
                mask_context_token=context_token,
                regions=[{"x": 1, "y": 2, "width": 3, "height": 4}],
            ),
            fake_context(),
        )

    assert calls == ["get_node_image_ref"]


@pytest.mark.asyncio
async def test_view_prompt_reference_image_resolves_image2_socket(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    Image.new("RGB", (320, 160), "#44aaff").save(input_root / "character.png")
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_prompt_workflow()),
    )

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        assert tool_name == "get_node_image_ref"
        assert parameters == {
            "node_id": 33,
            "expected_workflow_identity": "workflow-prompt-test",
            "expected_graph_hash": "b" * 64,
        }
        return {
            "node_id": 33,
            "image": {"filename": "character.png", "subfolder": "", "type": "input"},
            "workflow_identity": "workflow-prompt-test",
            "graph_hash": "b" * 64,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.view_prompt_reference_image.fn(
        mcp_server.ViewPromptReferenceImageRequest(),
        fake_context(),
    )

    assert result.structured_content["producer_node_id"] == 33
    assert result.structured_content["consumer_input"] == "image_2"
    assert result.structured_content["prompt_producer"]["producer_node_id"] == 34
    assert result.structured_content["source_attestation"] == image_attestation(
        input_root / "character.png"
    )
    assert [item.type for item in result.content] == ["text", "image"]


def test_view_canvas_images_returns_every_exact_visual_page(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    Image.new("RGB", (320, 160), "#44aaff").save(input_root / "car.png")
    Image.new("RGB", (240, 240), "#668844").save(input_root / "truck.png")
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    active = {
        "workflow": {"nodes": [], "links": []},
        "workflow_identity": "workflow-canvas-images",
        "graph_hash": "c" * 64,
    }
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active),
    )

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        assert tool_name == "get_canvas_image_refs"
        assert parameters == {
            "node_ids": None,
            "offset": 0,
            "limit": 8,
            "expected_workflow_identity": "workflow-canvas-images",
            "expected_graph_hash": "c" * 64,
        }
        return {
            "success": True,
            "workflow_identity": "workflow-canvas-images",
            "graph_hash": "c" * 64,
            "images": [
                {
                    "page_index": 0,
                    "image": {
                        "filename": "car.png",
                        "subfolder": "",
                        "type": "input",
                    },
                    "source_count": 2,
                    "sources": [
                        {
                            "node_id": 3,
                            "node_type": "LoadImage",
                            "title": "Main car",
                            "position": {"x": 0, "y": 0},
                            "image_index": None,
                        },
                        {
                            "node_id": 10,
                            "node_type": "LoadImage",
                            "title": "Duplicate car",
                            "position": {"x": 300, "y": 0},
                            "image_index": None,
                        },
                    ],
                },
                {
                    "page_index": 1,
                    "image": {
                        "filename": "truck.png",
                        "subfolder": "",
                        "type": "input",
                    },
                    "source_count": 1,
                    "sources": [{
                        "node_id": "12",
                        "node_type": "LoadImage",
                        "title": "Truck",
                        "position": {"x": 600, "y": 0},
                        "image_index": None,
                    }],
                },
            ],
            "total_count": 2,
            "offset": 0,
            "limit": 8,
            "has_more": False,
            "next_offset": None,
            "deduplicated": True,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = asyncio.run(mcp_server.view_canvas_images.fn(
        mcp_server.ViewCanvasImagesRequest(),
        fake_context(),
    ))

    assert result.structured_content["returned_count"] == 2
    assert result.structured_content["total_count"] == 2
    assert result.structured_content["has_more"] is False
    assert result.structured_content["images"][0]["source_count"] == 2
    assert [source["node_id"] for source in result.structured_content["images"][0]["sources"]] == [
        3,
        10,
    ]
    assert [item.type for item in result.content] == [
        "text",
        "text",
        "image",
        "text",
        "image",
    ]


def test_view_canvas_images_rejects_graph_change_during_pixel_read(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    Image.new("RGB", (64, 64), "#112233").save(input_root / "source.png")
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    reads = 0

    async def active_workflow(ctx):
        nonlocal reads
        del ctx
        reads += 1
        return {
            "workflow": {"nodes": [], "links": []},
            "workflow_identity": "workflow-canvas-images",
            "graph_hash": ("c" if reads == 1 else "d") * 64,
        }

    monkeypatch.setattr(mcp_server, "_active_editable_workflow", active_workflow)

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, tool_name, parameters
        return {
            "success": True,
            "workflow_identity": "workflow-canvas-images",
            "graph_hash": "c" * 64,
            "images": [{
                "page_index": 0,
                "image": {
                    "filename": "source.png",
                    "subfolder": "",
                    "type": "input",
                },
                "source_count": 1,
                "sources": [{
                    "node_id": 1,
                    "node_type": "LoadImage",
                    "title": "Source",
                }],
            }],
            "total_count": 1,
            "offset": 0,
            "limit": 8,
            "has_more": False,
            "next_offset": None,
            "deduplicated": True,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    with pytest.raises(RuntimeError, match="changed during image inspection"):
        asyncio.run(mcp_server.view_canvas_images.fn(
            mcp_server.ViewCanvasImagesRequest(),
            fake_context(),
        ))


@pytest.mark.asyncio
async def test_reference_prompt_update_rejects_same_ref_overwrite(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    reference_path = input_root / "character.png"
    Image.new("RGB", (320, 160), "#44aaff").save(reference_path)
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_prompt_workflow()),
    )
    calls = []

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        calls.append(tool_name)
        if tool_name == "get_node_image_ref":
            return {
                "node_id": 33,
                "image": {"filename": "character.png", "subfolder": "", "type": "input"},
                "workflow_identity": "workflow-prompt-test",
                "graph_hash": "b" * 64,
            }
        raise AssertionError(f"unexpected mutation after stale reference: {tool_name}")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    viewed = await mcp_server.view_prompt_reference_image.fn(
        mcp_server.ViewPromptReferenceImageRequest(),
        fake_context(),
    )
    Image.new("RGB", (320, 160), "#ff8844").save(reference_path)

    with pytest.raises(ValueError, match="reference bytes changed"):
        await mcp_server.update_connected_prompt.fn(
            mcp_server.UpdateConnectedPromptRequest(
                operation_id="prompt-ref-overwrite-0001",
                prompt="use the inspected identity",
                operation="append",
                prompt_context_token=viewed.structured_content["prompt_context_token"],
                reference_image_used=True,
            ),
            fake_context(),
        )

    assert calls == ["get_node_image_ref", "get_node_image_ref"]


@pytest.mark.asyncio
async def test_update_connected_prompt_updates_exact_string_widget_without_planner(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_prompt_workflow()),
    )
    calls = []
    updated = False

    async def execute_tool(ctx, tool_name, parameters):
        nonlocal updated
        del ctx
        calls.append((tool_name, parameters))
        if tool_name == "get_node_values_exact":
            return {
                "success": True,
                "node_id": 34,
                "values": {
                    "value": "new referenced character" if updated else "old character prompt"
                },
                "workflow_identity": "workflow-prompt-test",
                "graph_hash": "b" * 64,
            }
        if tool_name == "set_node_values_exact":
            updated = True
            assert parameters["node_id"] == 34
            assert parameters["widget_name"] == "value"
            assert parameters["expected_current_value"] == "old character prompt"
            return {
                "success": True,
                "node_id": 34,
                "widget_name": "value",
                "applied": ["value"],
                "verified": True,
                "queued": False,
                "workflow_identity": "workflow-prompt-test",
                "previous_graph_hash": "b" * 64,
                "graph_hash": "b" * 64,
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="prompt-test-0001",
            prompt="new referenced character",
            consumer_node_id=10,
        ),
        fake_context(),
    )

    assert result["success"] is True
    assert result["producer_node_id"] == 34
    assert result["producer_widget"] == "value"
    assert result["old_value_attestation"]["character_count"] == len("old character prompt")
    assert result["new_value_attestation"]["character_count"] == len("new referenced character")
    assert "old_value" not in result and "new_value" not in result
    assert [name for name, _ in calls] == [
        "get_node_values_exact",
        "set_node_values_exact",
        "get_node_values_exact",
    ]


@pytest.mark.asyncio
async def test_update_connected_prompt_updates_exact_direct_nano_widget(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    active = active_direct_prompt_workflow(widget_name="actual_prompt_widget")
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active),
    )
    state = {"actual_prompt_widget": "old", "set_calls": []}

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        if tool_name == "get_node_values_exact":
            assert parameters["node_id"] == 8
            return {
                "success": True,
                "node_id": 8,
                "values": {
                    "prompt": "decoy must remain untouched",
                    "actual_prompt_widget": state["actual_prompt_widget"],
                    "system_prompt": "system",
                    "model": "gemini",
                },
                "workflow_identity": "workflow-direct-prompt",
                "graph_hash": "d" * 64,
            }
        if tool_name == "set_node_values_exact":
            state["set_calls"].append(parameters)
            assert parameters["node_id"] == 8
            assert parameters["widget_name"] == "actual_prompt_widget"
            assert parameters["expected_current_value"] == "old"
            state["actual_prompt_widget"] = parameters["value"]
            return {
                "success": True,
                "node_id": 8,
                "widget_name": "actual_prompt_widget",
                "applied": ["actual_prompt_widget"],
                "verified": True,
                "queued": False,
                "workflow_identity": "workflow-direct-prompt",
                "previous_graph_hash": "d" * 64,
                "graph_hash": "d" * 64,
            }
        raise AssertionError(tool_name)

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="direct-prompt-0001",
            prompt="new exact Nano prompt",
            consumer_node_id=8,
        ),
        fake_context(),
    )

    assert result["success"] is True
    assert result["target_mode"] == "direct_widget"
    assert result["producer_node_id"] == 8
    assert result["consumer_node_id"] == 8
    assert result["producer_widget"] == "actual_prompt_widget"
    assert state["actual_prompt_widget"] == "new exact Nano prompt"
    assert len(state["set_calls"]) == 1


@pytest.mark.asyncio
async def test_direct_prompt_missing_attested_widget_never_mutates(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    active = active_direct_prompt_workflow(widget_name="actual_prompt_widget")
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active),
    )
    calls = []

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, parameters
        calls.append(tool_name)
        assert tool_name == "get_node_values_exact"
        return {
            "success": True,
            "node_id": 8,
            "values": {"prompt": "wrong widget", "system_prompt": "system"},
            "workflow_identity": "workflow-direct-prompt",
            "graph_hash": "d" * 64,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="direct-prompt-0002",
            prompt="must not apply",
            consumer_node_id=8,
        ),
        fake_context(),
    )

    assert result["success"] is False
    assert result["reason"] == "direct_prompt_widget_value_unavailable"
    assert calls == ["get_node_values_exact"]
    assert len(mcp_server._narrow_edit_operations) == 0


@pytest.mark.asyncio
async def test_ambiguous_direct_prompt_choice_releases_precommit_operation(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    active = active_direct_prompt_workflow()
    duplicate = active_direct_prompt_workflow()["workflow"]["nodes"][1]
    duplicate = {**duplicate, "id": "8"}
    active["workflow"]["nodes"].append(duplicate)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active),
    )

    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="direct-prompt-choice-0001",
            prompt="must not apply",
        ),
        fake_context(),
    )

    assert result["success"] is False
    assert result["needs_choice"] is True
    assert len(mcp_server._narrow_edit_operations) == 0


@pytest.mark.asyncio
async def test_direct_prompt_failed_verification_rolls_back_exact_widget(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    before = active_direct_prompt_workflow(graph_hash="d" * 64)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(before),
    )
    state = {"prompt": "old", "phase": "before", "set_calls": []}

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        if tool_name == "get_node_values_exact":
            return {
                "success": True,
                "node_id": 8,
                # Force post-write verification failure even though the first
                # transaction claimed success.
                "values": {"prompt": "wrong" if state["phase"] == "after" else "old"},
                "workflow_identity": "workflow-direct-prompt",
                "graph_hash": "e" * 64 if state["phase"] == "after" else "d" * 64,
            }
        if tool_name == "set_node_values_exact":
            state["set_calls"].append(parameters)
            assert parameters["node_id"] == 8
            assert parameters["widget_name"] == "prompt"
            if state["phase"] == "before":
                state["phase"] = "after"
                state["prompt"] = parameters["value"]
                return {
                    "success": True,
                    "node_id": 8,
                    "widget_name": "prompt",
                    "applied": ["prompt"],
                    "verified": True,
                    "queued": False,
                    "workflow_identity": "workflow-direct-prompt",
                    "previous_graph_hash": "d" * 64,
                    "graph_hash": "e" * 64,
                }
            assert parameters["expected_graph_hash"] == "e" * 64
            assert parameters["expected_current_value"] == "new"
            assert parameters["value"] == "old"
            state["prompt"] = "old"
            return {
                "success": True,
                "node_id": 8,
                "widget_name": "prompt",
                "applied": ["prompt"],
                "verified": True,
                "queued": False,
                "workflow_identity": "workflow-direct-prompt",
                "previous_graph_hash": "e" * 64,
                "graph_hash": "d" * 64,
            }
        raise AssertionError(tool_name)

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    with pytest.raises(RuntimeError, match="original prompt was restored exactly"):
        await mcp_server.update_connected_prompt.fn(
            mcp_server.UpdateConnectedPromptRequest(
                operation_id="direct-prompt-rollback-0001",
                prompt="new",
                consumer_node_id=8,
            ),
            fake_context(),
        )

    assert state["prompt"] == "old"
    assert len(state["set_calls"]) == 2
    assert all(call["node_id"] == 8 for call in state["set_calls"])
    assert all(call["widget_name"] == "prompt" for call in state["set_calls"])


@pytest.mark.asyncio
async def test_reference_token_selects_correlated_prompt_amid_unrelated_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    active = active_prompt_workflow()
    active["workflow"]["nodes"].extend(
        [
            {
                "id": 40,
                "type": "UnrelatedPromptConsumer",
                "inputs": [{"name": "prompt", "type": "STRING", "link": 3}],
                "outputs": [],
            },
            {
                "id": 41,
                "type": "PrimitiveStringMultiline",
                "inputs": [],
                "outputs": [{"name": "STRING", "type": "STRING", "links": [3]}],
            },
        ]
    )
    active["workflow"]["links"].append([3, 41, 0, 40, 0, "STRING"])
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active),
    )
    input_root = tmp_path / "input"
    input_root.mkdir()
    reference_path = input_root / "character.png"
    Image.new("RGB", (64, 64), "#44aaff").save(reference_path)
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    token, _ = mcp_server._prompt_context_tokens.issue(
        session_id="test-session",
        workflow_identity="workflow-prompt-test",
        graph_hash="b" * 64,
        node_id=34,
        source_image={"filename": "character.png", "subfolder": "", "type": "input"},
        source_attestation=image_attestation(reference_path),
        reference_node_id=33,
        reference_consumer_node_id=10,
        prompt_consumer_node_id=10,
    )
    updated = False

    async def execute_tool(ctx, tool_name, parameters):
        nonlocal updated
        del ctx
        if tool_name == "get_node_image_ref":
            assert parameters["node_id"] == 33
            return {
                "node_id": 33,
                    "image": {"filename": "character.png", "subfolder": "", "type": "input"},
                "workflow_identity": "workflow-prompt-test",
                "graph_hash": "b" * 64,
            }
        if tool_name == "get_node_values_exact":
            assert parameters["node_id"] == 34
            return {
                "success": True,
                "node_id": 34,
                "values": {"value": "new" if updated else "old"},
                "workflow_identity": "workflow-prompt-test",
                "graph_hash": "b" * 64,
            }
        if tool_name == "set_node_values_exact":
            assert parameters["node_id"] == 34
            assert parameters["expected_reference_node_id"] == 33
            assert parameters["expected_reference_image"] == {
                "filename": "character.png",
                "subfolder": "",
                "type": "input",
            }
            assert parameters["expected_reference_attestation"] == image_attestation(
                reference_path
            )
            updated = True
            return {
                "success": True,
                "node_id": 34,
                "widget_name": "value",
                "applied": ["value"],
                "verified": True,
                "queued": False,
                "workflow_identity": "workflow-prompt-test",
                "previous_graph_hash": "b" * 64,
                "graph_hash": "b" * 64,
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="prompt-test-0002",
            prompt="new",
            prompt_context_token=token,
            reference_image_used=True,
        ),
        fake_context(),
    )

    assert result["success"] is True
    assert result["producer_node_id"] == 34


@pytest.mark.asyncio
async def test_reference_token_updates_correlated_direct_nano_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    active = active_direct_prompt_workflow()
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active),
    )
    input_root = tmp_path / "input"
    input_root.mkdir()
    reference_path = input_root / "reference.png"
    Image.new("RGB", (80, 40), "#4477aa").save(reference_path)
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    state = {"prompt": "old"}

    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        if tool_name == "get_node_image_ref":
            assert parameters["node_id"] == 33
            return {
                "node_id": 33,
                "image": {"filename": "reference.png", "subfolder": "", "type": "input"},
                "workflow_identity": "workflow-direct-prompt",
                "graph_hash": "d" * 64,
            }
        if tool_name == "get_node_values_exact":
            assert parameters["node_id"] == 8
            return {
                "success": True,
                "node_id": 8,
                "values": {"prompt": state["prompt"], "system_prompt": "system"},
                "workflow_identity": "workflow-direct-prompt",
                "graph_hash": "d" * 64,
            }
        if tool_name == "set_node_values_exact":
            assert parameters["node_id"] == 8
            assert parameters["widget_name"] == "prompt"
            assert parameters["expected_reference_node_id"] == 33
            assert parameters["expected_reference_attestation"] == image_attestation(
                reference_path
            )
            state["prompt"] = parameters["value"]
            return {
                "success": True,
                "node_id": 8,
                "widget_name": "prompt",
                "applied": ["prompt"],
                "verified": True,
                "queued": False,
                "workflow_identity": "workflow-direct-prompt",
                "previous_graph_hash": "d" * 64,
                "graph_hash": "d" * 64,
            }
        raise AssertionError(tool_name)

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    viewed = await mcp_server.view_prompt_reference_image.fn(
        mcp_server.ViewPromptReferenceImageRequest(consumer_node_id=8),
        fake_context(),
    )
    assert viewed.structured_content["prompt_context_token"]
    assert viewed.structured_content["prompt_producer"] == {
        "target_mode": "direct_widget",
        "producer_node_id": 8,
        "producer_node_type": "GeminiNanoBanana2V2",
        "producer_widget": "prompt",
        "consumer_node_id": 8,
        "consumer_node_type": "GeminiNanoBanana2V2",
        "consumer_input": "prompt",
        "consumer_input_index": 0,
    }

    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="direct-reference-prompt-0001",
            prompt="new from image_2",
            consumer_node_id=8,
            consumer_input="prompt",
            prompt_context_token=viewed.structured_content["prompt_context_token"],
            reference_image_used=True,
        ),
        fake_context(),
    )

    assert result["success"] is True
    assert result["target_mode"] == "direct_widget"
    assert result["producer_node_id"] == 8
    assert result["producer_widget"] == "prompt"
    assert state["prompt"] == "new from image_2"


@pytest.mark.asyncio
async def test_update_connected_prompt_appends_without_exposing_or_replacing_old_text(
    monkeypatch,
):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_prompt_workflow()),
    )
    old_prompt = "rainy alley, cinematic lighting"
    new_prompt = f"{old_prompt}; match the character identity from image2"
    updated = False
    set_parameters = None

    async def execute_tool(ctx, tool_name, parameters):
        nonlocal updated, set_parameters
        del ctx
        if tool_name == "get_node_values_exact":
            return {
                "success": True,
                "node_id": 34,
                "values": {"value": new_prompt if updated else old_prompt},
                "workflow_identity": "workflow-prompt-test",
                "graph_hash": "b" * 64,
            }
        if tool_name == "set_node_values_exact":
            set_parameters = parameters
            updated = True
            return {
                "success": True,
                "node_id": 34,
                "widget_name": "value",
                "applied": ["value"],
                "verified": True,
                "queued": False,
                "workflow_identity": "workflow-prompt-test",
                "previous_graph_hash": "b" * 64,
                "graph_hash": "b" * 64,
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.update_connected_prompt.fn(
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="prompt-test-0003",
            prompt="match the character identity from image2",
            operation="append",
            separator="; ",
            consumer_node_id=10,
        ),
        fake_context(),
    )

    assert set_parameters is not None
    assert set_parameters["expected_current_value"] == old_prompt
    assert set_parameters["value"] == new_prompt
    assert result["operation"] == "append"
    assert result["verified"] is True
    assert result["queued"] is False
    assert "old_value" not in result and "new_value" not in result


@pytest.mark.parametrize(
    ("current", "operation", "operand", "separator", "expected"),
    [
        ("old", "replace", "new", " ", "new"),
        ("old", "append", "new", ", ", "old, new"),
        ("old", "prepend", "new", " | ", "new | old"),
        ("keep remove keep", "remove_exact", "remove ", " ", "keep keep"),
        ("", "append", "new", ", ", "new"),
    ],
)
def test_derive_prompt_update_is_literal_and_preserves_untouched_text(
    current,
    operation,
    operand,
    separator,
    expected,
):
    assert mcp_server._derive_prompt_update(
        current,
        operation=operation,
        operand=operand,
        separator=separator,
    ) == expected


@pytest.mark.parametrize(
    ("current", "operand", "match"),
    [
        ("unchanged", "missing", "absent"),
        ("x x", "x", "ambiguous"),
        ("only", "only", "cannot be empty"),
    ],
)
def test_derive_prompt_remove_exact_fails_closed(current, operand, match):
    with pytest.raises(ValueError, match=match):
        mcp_server._derive_prompt_update(
            current,
            operation="remove_exact",
            operand=operand,
            separator=" ",
        )


def test_derive_prompt_update_rejects_oversized_result_before_mutation():
    with pytest.raises(ValueError, match="size limit"):
        mcp_server._derive_prompt_update(
            "x" * mcp_server.MAX_CONNECTED_PROMPT_CHARACTERS,
            operation="append",
            operand="y",
            separator="",
        )


def test_reference_prompt_request_flag_requires_context_token():
    with pytest.raises(ValueError, match="prompt_context_token"):
        mcp_server.UpdateConnectedPromptRequest(
            operation_id="prompt-test-0004",
            prompt="updated prompt",
            reference_image_used=True,
        )


@pytest.mark.asyncio
async def test_routed_reference_prompt_env_requires_context_token(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.setenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", "1")

    with pytest.raises(ValueError, match="prompt_context_token"):
        await mcp_server.update_connected_prompt.fn(
            mcp_server.UpdateConnectedPromptRequest(
                operation_id="prompt-test-0005",
                prompt="updated prompt",
            ),
            fake_context(),
        )
    assert len(mcp_server._narrow_edit_operations) == 0


@pytest.mark.asyncio
async def test_invalid_prompt_context_token_releases_precommit_operation(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_direct_prompt_workflow()),
    )

    with pytest.raises(ValueError, match="token"):
        await mcp_server.update_connected_prompt.fn(
            mcp_server.UpdateConnectedPromptRequest(
                operation_id="prompt-invalid-token-0001",
                prompt="updated prompt",
                prompt_context_token="not-a-real-token",
                reference_image_used=True,
            ),
            fake_context(),
        )

    assert len(mcp_server._narrow_edit_operations) == 0


@pytest.mark.asyncio
async def test_prompt_recovery_probe_failure_releases_new_operation(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)

    async def unavailable_recovery(ctx, claim, payload):
        del ctx, claim, payload
        raise RuntimeError("recovery transport unavailable")

    monkeypatch.setattr(mcp_server, "_recover_narrow_operation", unavailable_recovery)
    with pytest.raises(RuntimeError, match="recovery transport unavailable"):
        await mcp_server.update_connected_prompt.fn(
            mcp_server.UpdateConnectedPromptRequest(
                operation_id="prompt-recovery-fail-0001",
                prompt="updated prompt",
                consumer_node_id=8,
            ),
            fake_context(),
        )

    assert len(mcp_server._narrow_edit_operations) == 0


@pytest.mark.asyncio
async def test_confirm_mask_review_forwards_review_token(monkeypatch):
    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        assert tool_name == "confirm_mask_review"
        assert parameters["node_id"] == 7
        assert parameters["review_token"] == "review-1"
        assert parameters["operation_id"] == "confirm-test-0001"
        assert len(parameters["operation_request_hash"]) == 64
        assert parameters["operation_payload"] == {
            "node_id": 7,
            "review_token": "review-1",
        }
        return {"success": True, "approved": True}

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.confirm_mask_review.fn(
        mcp_server.ConfirmMaskReviewRequest(
            operation_id="confirm-test-0001",
            node_id=7,
            review_token="review-1",
        ),
        fake_context(),
    )

    assert result == {"success": True, "approved": True}


@pytest.mark.asyncio
async def test_prompt_append_lost_reply_replays_without_duplicate_mutation(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_prompt_workflow()),
    )
    state = {"prompt": "rainy alley", "set_calls": 0, "cached": None}

    async def recover(ctx, claim, payload):
        del ctx, claim, payload
        if state["cached"] is None:
            raise mcp_server.FrontendToolExecutionError(
                "not found", code="narrow_edit_operation_not_found"
            )
        return dict(state["cached"], already_applied=True, queued=False)

    async def execute(ctx, tool_name, parameters):
        del ctx
        if tool_name == "get_node_values_exact":
            return {
                "success": True,
                "node_id": 34,
                "values": {"value": state["prompt"]},
                "workflow_identity": "workflow-prompt-test",
                "graph_hash": "b" * 64,
            }
        if tool_name == "set_node_values_exact":
            state["set_calls"] += 1
            state["prompt"] = parameters["value"]
            state["cached"] = {
                "success": True,
                "node_id": 34,
                "widget_name": "value",
                "applied": ["value"],
                "verified": True,
                "workflow_identity": "workflow-prompt-test",
                "previous_graph_hash": "b" * 64,
                "graph_hash": "c" * 64,
                "queued": False,
            }
            raise RuntimeError("reply lost after browser commit")
        raise AssertionError(tool_name)

    monkeypatch.setattr(mcp_server, "_recover_narrow_operation", recover)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute)
    request = mcp_server.UpdateConnectedPromptRequest(
        operation_id="prompt-lost-0001",
        prompt="preserve dusty woman's exact identity",
        operation="append",
        separator="; ",
        consumer_node_id=10,
    )

    with pytest.raises(RuntimeError, match="reply lost"):
        await mcp_server.update_connected_prompt.fn(request, fake_context())
    mcp_server._narrow_edit_operations.clear()
    mcp_server._prompt_context_tokens.clear()
    replay = await mcp_server.update_connected_prompt.fn(request, fake_context())

    assert state["set_calls"] == 1
    assert state["prompt"] == "rainy alley; preserve dusty woman's exact identity"
    assert replay["already_applied"] is True
    assert replay["queued"] is False


@pytest.mark.asyncio
async def test_direct_prompt_lost_reply_replays_without_duplicate_mutation(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.delenv("FL_MCP_PROMPT_REFERENCE_REQUIRED", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_direct_prompt_workflow()),
    )
    state = {"prompt": "old", "set_calls": 0, "cached": None}

    async def recover(ctx, claim, payload):
        del ctx, claim, payload
        if state["cached"] is None:
            raise mcp_server.FrontendToolExecutionError(
                "not found", code="narrow_edit_operation_not_found"
            )
        return dict(state["cached"], already_applied=True, queued=False)

    async def execute(ctx, tool_name, parameters):
        del ctx
        if tool_name == "get_node_values_exact":
            return {
                "success": True,
                "node_id": 8,
                "values": {"prompt": state["prompt"], "system_prompt": "system"},
                "workflow_identity": "workflow-direct-prompt",
                "graph_hash": "d" * 64,
            }
        if tool_name == "set_node_values_exact":
            state["set_calls"] += 1
            state["prompt"] = parameters["value"]
            state["cached"] = {
                "success": True,
                "target_mode": "direct_widget",
                "producer_node_id": 8,
                "producer_node_type": "GeminiNanoBanana2V2",
                "producer_widget": "prompt",
                "widget_name": "prompt",
                "consumer_node_id": 8,
                "consumer_input": "prompt",
                "operation": "replace",
                "workflow_identity": "workflow-direct-prompt",
                "previous_graph_hash": "d" * 64,
                "graph_hash": "d" * 64,
                "verified": True,
                "queued": False,
            }
            raise RuntimeError("reply lost after direct prompt commit")
        raise AssertionError(tool_name)

    monkeypatch.setattr(mcp_server, "_recover_narrow_operation", recover)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute)
    request = mcp_server.UpdateConnectedPromptRequest(
        operation_id="direct-prompt-lost-0001",
        prompt="new",
        consumer_node_id=8,
    )

    with pytest.raises(RuntimeError, match="reply lost"):
        await mcp_server.update_connected_prompt.fn(request, fake_context())
    mcp_server._narrow_edit_operations.clear()
    replay = await mcp_server.update_connected_prompt.fn(request, fake_context())

    assert state["set_calls"] == 1
    assert state["prompt"] == "new"
    assert replay["already_applied"] is True
    assert replay["target_mode"] == "direct_widget"


@pytest.mark.asyncio
async def test_mask_edit_lost_reply_reuses_pending_upload_and_review_token(
    tmp_path,
    monkeypatch,
):
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_path = input_root / "source.png"
    pending_path = input_root / "pending.png"
    Image.new("RGBA", (64, 32), (20, 40, 60, 255)).save(source_path)
    Image.new("RGBA", (64, 32), (20, 40, 60, 0)).save(pending_path)
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(tmp_path / "output", {}, input_root),
    )
    monkeypatch.setattr(mcp_server.settings, "enable_workflow_writes", True)
    monkeypatch.setattr(
        mcp_server,
        "_active_editable_workflow",
        lambda ctx: async_value(active_mask_workflow()),
    )
    source_ref = {"filename": "source.png", "subfolder": "", "type": "input"}
    cached = {
        "success": True,
        "node_id": 7,
        "source_image": source_ref,
        "image": {"filename": "pending.png", "subfolder": "", "type": "input"},
        "review_required": True,
        "review_token": "stable-review-token",
        "queued": False,
    }
    state = {"edit_calls": 0, "committed": False}

    async def recover(ctx, claim, payload):
        del ctx, claim, payload
        if not state["committed"]:
            raise mcp_server.FrontendToolExecutionError(
                "not found", code="narrow_edit_operation_not_found"
            )
        return dict(cached, already_applied=True)

    async def execute(ctx, tool_name, parameters):
        del ctx, parameters
        if tool_name == "get_node_image_ref":
            return {
                "node_id": 7,
                "image": source_ref,
                "workflow_identity": "workflow-mask-test",
                "graph_hash": "a" * 64,
            }
        if tool_name == "edit_node_mask":
            state["edit_calls"] += 1
            state["committed"] = True
            raise RuntimeError("reply lost after one mask upload")
        raise AssertionError(tool_name)

    monkeypatch.setattr(mcp_server, "_recover_narrow_operation", recover)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute)
    context_token, _ = mcp_server._mask_context_tokens.issue(
        session_id="test-session",
        workflow_identity="workflow-mask-test",
        graph_hash="a" * 64,
        node_id=7,
        source_image=source_ref,
        source_attestation=image_attestation(source_path),
    )
    request = mcp_server.EditNodeMaskRequest(
        operation_id="mask-lost-00001",
        node_id=7,
        mask_context_token=context_token,
        coordinate_space="normalized",
        clear_existing=True,
        regions=[{"shape": "ellipse", "x": 0.4, "y": 0.2, "width": 0.2, "height": 0.3}],
    )

    with pytest.raises(RuntimeError, match="reply lost"):
        await mcp_server.edit_node_mask.fn(request, fake_context())
    mcp_server._narrow_edit_operations.clear()
    mcp_server._mask_context_tokens.clear()
    replay = await mcp_server.edit_node_mask.fn(request, fake_context())

    assert state["edit_calls"] == 1
    assert replay.structured_content["review_token"] == "stable-review-token"
    assert replay.structured_content["already_applied"] is True
    assert replay.structured_content["queued"] is False


@pytest.mark.asyncio
async def test_confirm_lost_reply_returns_same_receipt_without_second_commit(monkeypatch):
    state = {"confirm_calls": 0, "cached": None}

    async def recover(ctx, claim, payload):
        del ctx, claim, payload
        if state["cached"] is None:
            raise mcp_server.FrontendToolExecutionError(
                "not found", code="narrow_edit_operation_not_found"
            )
        return dict(state["cached"], already_applied=True, queued=False)

    async def execute(ctx, tool_name, parameters):
        del ctx, parameters
        assert tool_name == "confirm_mask_review"
        state["confirm_calls"] += 1
        state["cached"] = {
            "success": True,
            "node_id": 7,
            "image": {"filename": "approved.png", "type": "input"},
            "review_token": "stable-review-token",
            "approved": True,
            "workflow_identity": "workflow-mask-test",
            "graph_hash": "d" * 64,
            "queued": False,
        }
        raise RuntimeError("reply lost after approval commit")

    monkeypatch.setattr(mcp_server, "_recover_narrow_operation", recover)
    monkeypatch.setattr(mcp_server, "_execute_tool", execute)
    request = mcp_server.ConfirmMaskReviewRequest(
        operation_id="confirm-lost-01",
        node_id=7,
        review_token="stable-review-token",
    )

    with pytest.raises(RuntimeError, match="reply lost"):
        await mcp_server.confirm_mask_review.fn(request, fake_context())
    mcp_server._narrow_edit_operations.clear()
    replay = await mcp_server.confirm_mask_review.fn(request, fake_context())

    assert state["confirm_calls"] == 1
    assert replay["approved"] is True
    assert replay["already_applied"] is True
    assert replay["queued"] is False
