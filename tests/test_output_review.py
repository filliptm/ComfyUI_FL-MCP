from types import SimpleNamespace

import mcp_server
import pytest
from comfy_models import ComfyFolderType
from mcp.types import ImageContent, TextContent
from PIL import Image


class FakeComfyTools:
    def __init__(self, output_root, history):
        self.output_root = output_root
        self.history = history

    async def fetch_history(self, prompt_id=None, max_items=10):
        del max_items
        if prompt_id:
            return self.history.get(prompt_id)
        return self.history

    def _iter_all_paths(self, folder_type):
        assert folder_type == ComfyFolderType.OUTPUT
        yield self.output_root


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
        mcp_server._resolve_output_image_path(tools, {
            "filename": "../secret.png",
            "subfolder": "",
            "type": "output",
        })
