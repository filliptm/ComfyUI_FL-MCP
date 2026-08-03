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
        request_context=SimpleNamespace(lifespan_context={"client": None}),
    )


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
    assert result.structured_content["originalSize"] == {"width": 3000, "height": 1500}
    assert result.structured_content["previewSize"] == {"width": 1024, "height": 512}
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.content[1], ImageContent)
    assert result.content[1].mimeType == "image/jpeg"
    assert len(result.content[1].data) > 100


@pytest.mark.asyncio
async def test_view_output_image_selects_newest_matching_history_entry(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "output"
    output_root.mkdir()
    Image.new("RGB", (32, 32), "#4d7cff").save(output_root / "latest.png")
    history = {
        "prompt-oldest": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "1": {"images": [{"filename": "oldest.png", "type": "output"}]},
            },
        },
        "prompt-newest": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "2": {"images": [{"filename": "latest.png", "type": "output"}]},
            },
        },
    }
    monkeypatch.setattr(
        mcp_server,
        "get_comfy_tools",
        lambda: FakeComfyTools(output_root, history),
    )

    result = await mcp_server.view_output_image.fn(
        mcp_server.ViewOutputImageRequest(),
        fake_context(),
    )

    assert result.structured_content["promptId"] == "prompt-newest"


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
            node_id=1,
            coordinate_space="normalized",
            regions=[{
                "x": 0.8,
                "y": 0.2,
                "width": 0.3,
                "height": 0.2,
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
        del ctx, parameters
        assert tool_name == "get_node_image_ref"
        return {
            "node_id": 7,
            "node_type": "LoadImage",
            "title": "LOAD & MASK IMAGE",
            "image": {"filename": "mask.png", "subfolder": "", "type": "input"},
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.view_node_mask.fn(
        mcp_server.ViewNodeMaskRequest(node_id=7),
        fake_context(),
    )

    assert result.structured_content["mask"]["coveragePercent"] == 3.125
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
        assert tool_name == "edit_node_mask"
        assert parameters["clear_existing"] is True
        return {
            "success": True,
            "node_id": 7,
            "image": {"filename": "edited.png", "subfolder": "", "type": "input"},
            "review_required": True,
            "review_token": "review-1",
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.edit_node_mask.fn(
        mcp_server.EditNodeMaskRequest(
            node_id=7,
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
async def test_confirm_mask_review_forwards_review_token(monkeypatch):
    async def execute_tool(ctx, tool_name, parameters):
        del ctx
        assert tool_name == "confirm_mask_review"
        assert parameters == {"node_id": 7, "review_token": "review-1"}
        return {"success": True, "approved": True}

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.confirm_mask_review.fn(
        mcp_server.ConfirmMaskReviewRequest(
            node_id=7,
            review_token="review-1",
        ),
        fake_context(),
    )

    assert result == {"success": True, "approved": True}
