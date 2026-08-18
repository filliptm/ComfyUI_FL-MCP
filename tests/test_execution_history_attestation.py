import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import comfy_tools
import mcp_server
import pytest


def test_browser_submission_record_verifies_in_python_history_attestation():
    root = Path(__file__).resolve().parents[1]
    script = """
import { prepareExecutionSubmission } from './web/js/execution_provenance.js';
const original = {
  output: {
    '1': {class_type: 'LoadImage', inputs: {image: 'private-mask.png'}},
    '34': {class_type: 'PrimitiveStringMultiline', inputs: {value: 'private prompt', weight: 1e-7}},
  },
  workflow: {
    id: 'workflow-cross-runtime', revision: 3,
    nodes: [
      {id: 1, type: 'LoadImage', widgets_values: ['private-mask.png']},
      {id: 34, type: 'PrimitiveStringMultiline', widgets_values: ['private prompt', 1e-7]},
    ],
    links: [], extra: {},
  },
};
const prepared = await prepareExecutionSubmission(original, {
  operationId: 'queue-op-cross-runtime', operationRequestHash: 'd'.repeat(64),
});
process.stdout.write(JSON.stringify({original, prepared}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        # A cold Node start on a loaded Windows CI runner has repeatedly blown
        # a 10s budget (two consecutive PR runs flaked and passed on rerun);
        # the script itself finishes in well under a second once Node is up.
        timeout=60,
    )
    payload = json.loads(completed.stdout)
    prepared = payload["prepared"]
    history_tuple = [
        1,
        "prompt-cross-runtime",
        prepared["submission"]["output"],
        {"extra_pnginfo": {"workflow": prepared["submission"]["workflow"]}},
        ["34"],
    ]

    attestation = comfy_tools._submission_attestation(
        history_tuple,
        attest_node_ids=(1, 34),
    )

    assert attestation["available"] is True
    assert attestation["verified"] is True
    assert attestation["graph_hash"] == prepared["provenance"]["graph_hash"]
    assert attestation["api_prompt"] == prepared["provenance"]["api_prompt"]
    assert attestation["editable_workflow"]["sha256"] == (
        prepared["provenance"]["editable_workflow"]["sha256"]
    )
    serialized = json.dumps(attestation, sort_keys=True)
    assert "private prompt" not in serialized
    assert "private-mask.png" not in serialized


@pytest.mark.asyncio
async def test_specific_execution_history_requests_submission_attestation(monkeypatch):
    calls = []
    attestation = {
        "schema": "fl-mcp.execution-submission-attestation.v1",
        "available": True,
        "hash_algorithm": "sha256",
        "source": "frontend_queue_capture",
        "verified": True,
        "api_prompt": {
            "schema": "fl-mcp.execution-api-prompt.typed-v1",
            "sha256": "a" * 64,
            "canonical_bytes": 123,
            "node_count": 2,
        },
        "editable_workflow": {
            "schema": "fl-mcp.execution-workflow.typed-v1",
            "sha256": "b" * 64,
            "canonical_bytes": 456,
            "node_count": 2,
        },
        "node_attestations": [],
        "graph_hash": "c" * 64,
        "graph_hash_schema": "fl-mcp.graph-precondition.v1",
    }

    class FakeComfyTools:
        async def fetch_history(
            self,
            prompt_id=None,
            max_items=10,
            *,
            include_submission_attestation=False,
            attest_node_ids=(),
        ):
            calls.append(
                (prompt_id, max_items, include_submission_attestation, attest_node_ids)
            )
            return {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"2": {"images": [{"filename": "result.png"}]}},
                "submission_attestation": attestation,
            }

    async def ignore_activity(ctx, tool_name):
        del ctx, tool_name

    monkeypatch.setattr(mcp_server, "get_comfy_tools", FakeComfyTools)
    monkeypatch.setattr(mcp_server, "_report_tool_activity", ignore_activity)
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context={}))

    result = await mcp_server.get_execution_history.fn(
        mcp_server.GetWorkflowHistoryRequest(
            prompt_id="prompt-1",
            attest_node_ids=[1, 34],
        ),
        context,
    )

    assert calls == [("prompt-1", 10, True, (1, 34))]
    assert result["status"] == "success"
    assert result["outputs"] == {"2": {"images": [{"filename": "result.png"}]}}
    assert result["submission_attestation"] == attestation
    assert "prompt" not in result


def test_attested_node_ids_require_specific_history_and_exact_uniqueness():
    with pytest.raises(ValueError, match="requires one exact prompt_id"):
        mcp_server.GetWorkflowHistoryRequest(attest_node_ids=[1])
    with pytest.raises(ValueError, match="duplicate exact typed IDs"):
        mcp_server.GetWorkflowHistoryRequest(
            prompt_id="prompt-1",
            attest_node_ids=[1, 1],
        )

    request = mcp_server.GetWorkflowHistoryRequest(
        prompt_id="prompt-1",
        attest_node_ids=[1, "1"],
    )
    assert request.attest_node_ids == [1, "1"]


@pytest.mark.asyncio
async def test_failed_history_redacts_messages_inputs_traceback_and_exception_text(monkeypatch):
    secret = "secret face-swap prompt and private-image.png"
    attestation = {
        "schema": "fl-mcp.execution-submission-attestation.v1",
        "available": False,
        "reason": "submission_malformed",
    }

    class FakeComfyTools:
        async def fetch_history(self, **kwargs):
            assert kwargs["include_submission_attestation"] is True
            return {
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [
                        [
                            "execution_error",
                            {
                                "node_id": "34",
                                "node_type": "PrimitiveStringMultiline",
                                "exception_type": "ValueError",
                                "exception_message": secret,
                                "traceback": [secret],
                                "current_inputs": {"value": [secret]},
                                "executed": ["1", secret],
                            },
                        ],
                        ["status", {"message": secret}],
                    ],
                },
                "outputs": {},
                "submission_attestation": attestation,
            }

    async def ignore_activity(ctx, tool_name):
        del ctx, tool_name

    monkeypatch.setattr(mcp_server, "get_comfy_tools", FakeComfyTools)
    monkeypatch.setattr(mcp_server, "_report_tool_activity", ignore_activity)
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context={}))

    result = await mcp_server.get_execution_history.fn(
        mcp_server.GetWorkflowHistoryRequest(prompt_id="prompt-failed"),
        context,
    )

    assert result["errors"] == [
        {
            "node_id": "34",
            "node_type": "PrimitiveStringMultiline",
            "exception_type": "ValueError",
            "exception_message_redacted": True,
            "traceback_redacted": True,
            "current_inputs_redacted": True,
        }
    ]
    assert "messages" not in result
    assert "executed_nodes" not in result
    assert secret not in json.dumps(result, sort_keys=True)


def test_history_error_facts_reject_plaintext_disguised_as_identifiers():
    secret = "private prompt disguised as a node type"
    errors = mcp_server._execution_history_error_facts(
        {
            "messages": [
                [
                    "execution_error",
                    {
                        "node_id": secret,
                        "node_type": secret,
                        "exception_type": secret,
                        "exception_message": secret,
                    },
                ]
            ]
        }
    )

    assert errors[0]["node_id"] is None
    assert errors[0]["node_type"] is None
    assert errors[0]["exception_type"] is None
    assert secret not in json.dumps(errors, sort_keys=True)
