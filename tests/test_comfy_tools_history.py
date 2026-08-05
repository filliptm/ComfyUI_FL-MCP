import comfy_tools
import pytest


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
