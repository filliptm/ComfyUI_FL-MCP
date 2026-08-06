import comfy_tools
import pytest
from comfy_models import ComfyFolderType


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@pytest.mark.asyncio
async def test_fetch_history_uses_exact_prompt_endpoint(monkeypatch):
    requests = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None, timeout=None):
            requests.append((url, params, timeout))
            return FakeResponse({
                "prompt/older": {
                    "prompt": {"large": "workflow"},
                    "status": {"status_str": "success"},
                },
            })

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    result = await tools.fetch_history(prompt_id="prompt/older")

    assert requests == [
        ("http://comfy/history/prompt%2Folder", None, 10.0),
    ]
    assert result == {"status": {"status_str": "success"}}


def test_runtime_image_directories_override_source_tree_defaults(tmp_path, monkeypatch):
    comfy_root = tmp_path / "ComfyUI"
    for directory in ("custom_nodes", "models", "output"):
        (comfy_root / directory).mkdir(parents=True)
    for filename in ("nodes.py", "folder_paths.py"):
        (comfy_root / filename).write_text("", encoding="utf-8")

    desktop_root = tmp_path / "DesktopData"
    runtime_paths = {
        ComfyFolderType.INPUT: desktop_root / "input",
        ComfyFolderType.OUTPUT: desktop_root / "output",
        ComfyFolderType.TEMP: desktop_root / "temp",
    }
    for path in runtime_paths.values():
        path.mkdir(parents=True)
    monkeypatch.setenv("FL_MCP_COMFYUI_INPUT_DIR", str(runtime_paths[ComfyFolderType.INPUT]))
    monkeypatch.setenv("FL_MCP_COMFYUI_OUTPUT_DIR", str(runtime_paths[ComfyFolderType.OUTPUT]))
    monkeypatch.setenv("FL_MCP_COMFYUI_TEMP_DIR", str(runtime_paths[ComfyFolderType.TEMP]))

    tools = comfy_tools.ComfyUITools(comfyui_root=str(comfy_root))

    for folder_type, expected in runtime_paths.items():
        assert list(tools._iter_all_paths(folder_type)) == [expected.resolve()]
