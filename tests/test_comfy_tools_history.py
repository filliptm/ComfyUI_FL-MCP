import copy
import json

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


class FakeStreamResponse:
    def __init__(self, payload, *, headers=None, chunks=None):
        self.headers = headers or {}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.chunks = list(chunks) if chunks is not None else [encoded]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


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


def submitted_history(
    prompt_text="dust-covered woman",
    mask_ref="mask-source-a.png",
    *,
    include_capture=False,
):
    api_prompt = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": mask_ref},
        },
        "34": {
            "class_type": "PrimitiveStringMultiline",
            "inputs": {"value": prompt_text},
        },
    }
    editable_workflow = {
        "id": "workflow-1",
        "revision": 7,
        "nodes": [
            {"id": 1, "type": "LoadImage", "widgets_values": [mask_ref]},
            {
                "id": 34,
                "type": "PrimitiveStringMultiline",
                "widgets_values": [prompt_text],
            },
        ],
        "links": [],
    }
    payload = {
        "prompt-1": {
            "prompt": [
                7,
                "prompt-1",
                api_prompt,
                {"extra_pnginfo": {"workflow": editable_workflow}},
                ["34"],
            ],
            "status": {"status_str": "success", "completed": True},
            "outputs": {"2": {"images": [{"filename": "result.png"}]}},
        }
    }
    if include_capture:
        prompt_tuple = payload["prompt-1"]["prompt"]
        workflow = prompt_tuple[3]["extra_pnginfo"]["workflow"]
        workflow["extra"] = {}
        api_sha256, api_bytes = comfy_tools._bounded_typed_sha256(
            prompt_tuple[2],
            schema=comfy_tools.SUBMITTED_API_PROMPT_HASH_SCHEMA,
        )
        workflow_sha256, workflow_bytes = comfy_tools._bounded_typed_sha256(
            workflow,
            schema=comfy_tools.SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA,
        )
        workflow["extra"][comfy_tools.EXECUTION_PROVENANCE_EXTRA_KEY] = {
            "schema": comfy_tools.EXECUTION_PROVENANCE_SCHEMA,
            "source": comfy_tools.EXECUTION_PROVENANCE_SOURCE,
            "api_prompt": {
                "schema": comfy_tools.SUBMITTED_API_PROMPT_HASH_SCHEMA,
                "sha256": api_sha256,
                "canonical_bytes": api_bytes,
                "node_count": len(prompt_tuple[2]),
            },
            "editable_workflow": {
                "schema": comfy_tools.SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA,
                "sha256": workflow_sha256,
                "canonical_bytes": workflow_bytes,
                "node_count": len(workflow["nodes"]),
                "workflow_id": "workflow-1",
                "revision": 7,
            },
            "graph_hash": "c" * 64,
            "graph_hash_schema": comfy_tools.EXECUTION_GRAPH_HASH_SCHEMA,
            "raw_prompt_returned": False,
            "captured_at_ms": 1_786_531_200_000,
            "operation_id": "queue-op-history1",
            "operation_request_hash": "d" * 64,
        }
    return payload


def test_execution_typed_hash_vectors_cover_unicode_and_float_identity():
    api_prompt = {
        "34": {
            "class_type": "PrimitiveStringMultiline",
            "inputs": {"value": "dusty ☃", "weight": 0.125},
        },
        "1": {"class_type": "LoadImage", "inputs": {"image": "mask.png"}},
    }
    editable_workflow = {
        "nodes": [
            {"id": 34, "widgets_values": ["dusty ☃", 0.125]},
            {"id": 1, "widgets_values": ["mask.png"]},
        ],
        "links": [],
    }

    assert comfy_tools._bounded_typed_sha256(
        api_prompt,
        schema=comfy_tools.SUBMITTED_API_PROMPT_HASH_SCHEMA,
    ) == (
        "b4cf8c3e6d531fd67a78ee567512c0e793d21df02ded5882d5c5c02a198b6210",
        418,
    )
    assert comfy_tools._bounded_typed_sha256(
        editable_workflow,
        schema=comfy_tools.SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA,
    ) == (
        "73be8c970c787c7b207d0858f1305b3505734218f86851a5b3347d972bd6c432",
        357,
    )


@pytest.mark.asyncio
async def test_opt_in_history_attests_submission_without_returning_plaintext(monkeypatch):
    payloads = [
        submitted_history(include_capture=True),
        submitted_history(prompt_text="different private prompt", include_capture=True),
        submitted_history(mask_ref="mask-source-b.png", include_capture=True),
    ]

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None, timeout=None):
            del url, params, timeout
            raise AssertionError("opt-in history must use the bounded stream path")

        def stream(self, method, url, timeout=None):
            del method, url, timeout
            return FakeStreamResponse(copy.deepcopy(payloads.pop(0)))

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    base = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
        attest_node_ids=(1, 34),
    )
    prompt_changed = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
        attest_node_ids=(1, 34),
    )
    mask_changed = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
        attest_node_ids=(1, 34),
    )

    attestation = base["submission_attestation"]
    assert attestation["schema"] == comfy_tools.EXECUTION_SUBMISSION_ATTESTATION_SCHEMA
    assert attestation["available"] is True
    assert attestation["raw_prompt_returned"] is False
    assert attestation["source"] == comfy_tools.EXECUTION_PROVENANCE_SOURCE
    assert attestation["verified"] is True
    assert attestation["graph_hash"] == "c" * 64
    assert attestation["graph_hash_schema"] == comfy_tools.EXECUTION_GRAPH_HASH_SCHEMA
    assert attestation["workflow_id"] == "workflow-1"
    assert attestation["revision"] == 7
    assert attestation["api_prompt"]["node_count"] == 2
    assert attestation["editable_workflow"]["node_count"] == 2
    assert len(attestation["api_prompt"]["sha256"]) == 64
    assert len(attestation["editable_workflow"]["sha256"]) == 64
    assert [item["node_id"] for item in attestation["node_attestations"]] == [1, 34]
    assert [item["class_type"] for item in attestation["node_attestations"]] == [
        "LoadImage",
        "PrimitiveStringMultiline",
    ]
    assert attestation["node_attestations"][0]["string_inputs"] == [
        {
            "input": "image",
            "kind": "image_reference",
            "value_sha256": attestation["node_attestations"][0]["string_inputs"][0][
                "value_sha256"
            ],
            "utf8_bytes": len("mask-source-a.png"),
        }
    ]
    assert attestation["node_attestations"][1]["string_inputs"][0]["kind"] == "text"
    assert attestation["node_attestations"][1]["string_inputs"][0]["utf8_bytes"] == len(
        "dust-covered woman"
    )
    assert attestation["api_prompt"]["sha256"] != (
        prompt_changed["submission_attestation"]["api_prompt"]["sha256"]
    )
    assert attestation["api_prompt"]["sha256"] != (
        mask_changed["submission_attestation"]["api_prompt"]["sha256"]
    )
    assert attestation["editable_workflow"]["sha256"] != (
        prompt_changed["submission_attestation"]["editable_workflow"]["sha256"]
    )
    assert attestation["editable_workflow"]["sha256"] != (
        mask_changed["submission_attestation"]["editable_workflow"]["sha256"]
    )
    serialized = json.dumps(base, sort_keys=True)
    assert "prompt" not in base
    assert "dust-covered woman" not in serialized
    assert "mask-source-a.png" not in serialized


@pytest.mark.asyncio
async def test_history_rejects_capture_with_inconsistent_workflow_metadata(monkeypatch):
    payload = submitted_history(include_capture=True)
    record = payload["prompt-1"]["prompt"][3]["extra_pnginfo"]["workflow"]["extra"][
        comfy_tools.EXECUTION_PROVENANCE_EXTRA_KEY
    ]
    record["editable_workflow"]["revision"] = 8

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, timeout=None):
            del method, url, timeout
            return FakeStreamResponse(copy.deepcopy(payload))

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    result = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
    )

    attestation = result["submission_attestation"]
    assert attestation["verified"] is False
    assert attestation["verification_reason"] == "frontend_queue_capture_hash_mismatch"
    assert attestation["graph_hash"] is None
    assert attestation["graph_hash_schema"] is None


@pytest.mark.asyncio
async def test_history_rejects_mismatched_frontend_capture_but_keeps_derived_hashes(
    monkeypatch,
):
    payload = submitted_history(include_capture=True)
    payload["prompt-1"]["prompt"][2]["34"]["inputs"]["value"] = "changed after capture"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, timeout=None):
            del method, url, timeout
            return FakeStreamResponse(copy.deepcopy(payload))

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    result = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
    )
    attestation = result["submission_attestation"]

    assert attestation["available"] is True
    assert attestation["source"] == comfy_tools.EXECUTION_PROVENANCE_SOURCE
    assert attestation["verified"] is False
    assert attestation["verification_reason"] == "frontend_queue_capture_hash_mismatch"
    assert attestation["graph_hash"] is None
    assert attestation["graph_hash_schema"] is None
    assert len(attestation["api_prompt"]["sha256"]) == 64


@pytest.mark.asyncio
async def test_legacy_history_is_derived_without_graph_hash_claim(monkeypatch):
    payload = submitted_history()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, timeout=None):
            del method, url, timeout
            return FakeStreamResponse(copy.deepcopy(payload))

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    result = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
    )
    attestation = result["submission_attestation"]

    assert attestation["source"] == "history_derived"
    assert attestation["verified"] is False
    assert attestation["verification_reason"] == "frontend_queue_capture_missing"
    assert attestation["graph_hash"] is None
    assert attestation["graph_hash_schema"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_prompt", "reason"),
    [
        ([7, "prompt-1", {"1": {}}, {}], "submission_malformed"),
        (
            [
                7,
                "prompt-1",
                {"1": {"inputs": {"text": "x" * 512}}},
                {"extra_pnginfo": {"workflow": {"nodes": []}}},
            ],
            "submission_too_large",
        ),
    ],
)
async def test_unavailable_attestation_preserves_status_and_outputs(
    monkeypatch,
    raw_prompt,
    reason,
):
    payload = submitted_history()
    payload["prompt-1"]["prompt"] = raw_prompt

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None, timeout=None):
            del url, params, timeout
            raise AssertionError("opt-in history must use the bounded stream path")

        def stream(self, method, url, timeout=None):
            del method, url, timeout
            return FakeStreamResponse(copy.deepcopy(payload))

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    if reason == "submission_too_large":
        monkeypatch.setattr(comfy_tools, "MAX_EXECUTION_SUBMISSION_ATTESTATION_BYTES", 128)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    result = await tools.fetch_history(
        prompt_id="prompt-1",
        include_submission_attestation=True,
    )

    assert result["submission_attestation"] == {
        "schema": comfy_tools.EXECUTION_SUBMISSION_ATTESTATION_SCHEMA,
        "available": False,
        "reason": reason,
    }
    assert result["status"] == {"status_str": "success", "completed": True}
    assert result["outputs"] == {"2": {"images": [{"filename": "result.png"}]}}
    assert "prompt" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("use_content_length", [True, False])
async def test_opt_in_history_caps_transport_before_json_decode(
    monkeypatch,
    use_content_length,
):
    monkeypatch.setattr(comfy_tools, "MAX_EXECUTION_HISTORY_RESPONSE_BYTES", 32)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, timeout=None):
            del method, url, timeout
            if use_content_length:
                return FakeStreamResponse(
                    {},
                    headers={"content-length": "33"},
                    chunks=[b"must-not-be-read"],
                )
            return FakeStreamResponse({}, chunks=[b"x" * 20, b"y" * 20])

    monkeypatch.setattr(comfy_tools.httpx, "AsyncClient", FakeClient)
    tools = object.__new__(comfy_tools.ComfyUITools)
    tools.comfy_url = "http://comfy"

    with pytest.raises(comfy_tools.ComfyUIError, match="safe byte limit"):
        await tools.fetch_history(
            prompt_id="prompt-1",
            include_submission_attestation=True,
        )


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
