import json
import logging
from types import SimpleNamespace

import mcp_server
import pytest
from config import MAX_GENERATION_COMPLETION_TIMEOUT_SECONDS


def execution_provenance():
    return {
        "schema": "fl-mcp.execution-provenance.v1",
        "source": "frontend_queue_capture",
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
            "workflow_id": "workflow-1",
            "revision": 0,
        },
        "graph_hash": "c" * 64,
        "graph_hash_schema": "fl-mcp.graph-precondition.v1",
        "raw_prompt_returned": False,
        "captured_at_ms": 123456789,
        "operation_id": "queue-op-test01",
        "operation_request_hash": "d" * 64,
    }


@pytest.fixture(autouse=True)
def queue_operation_claim(monkeypatch):
    claim = SimpleNamespace(request_hash="d" * 64)

    async def begin(request, ctx, tool):
        del ctx
        assert tool == "queue_workflow"
        return claim, request.model_dump(mode="json", exclude={"operation_id"}), None

    monkeypatch.setattr(mcp_server, "_begin_narrow_operation", begin)
    monkeypatch.setattr(mcp_server._narrow_edit_operations, "complete", lambda *args: None)
    monkeypatch.setattr(mcp_server._narrow_edit_operations, "discard_pending", lambda *args: True)


def test_history_completion_results_are_distinct():
    completed = mcp_server._history_completion_result(
        "prompt-1",
        {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {"9": {"images": [{"filename": "final.png"}]}},
        },
    )
    failed = mcp_server._history_completion_result(
        "prompt-2",
        {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [[
                    "execution_error",
                    {
                        "node_id": "9",
                        "node_type": "KSampler",
                        "exception_type": "RuntimeError",
                        "exception_message": "out of memory",
                        "traceback": ["not returned"],
                    },
                ]],
            },
        },
    )
    cancelled = mcp_server._history_completion_result(
        "prompt-3",
        {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_interrupted", {"node_id": "9"}]],
            },
        },
    )

    assert completed["status"] == "completed"
    assert completed["success"] is True
    assert completed["outputs"]["9"]["images"][0]["filename"] == "final.png"
    assert failed["status"] == "execution_error"
    assert failed["success"] is False
    assert failed["errors"] == [{
        "node_id": "9",
        "node_type": "KSampler",
        "exception_type": "RuntimeError",
        "exception_message_redacted": True,
        "traceback_redacted": True,
        "current_inputs_redacted": True,
    }]
    assert cancelled["status"] == "cancelled"
    assert cancelled["terminal"] is True


def test_per_call_wait_accepts_the_full_supported_timeout():
    request = mcp_server.QueueWorkflowRequest(
        operation_id="queue-op-test01",
        wait_for_completion=True,
        completion_timeout=MAX_GENERATION_COMPLETION_TIMEOUT_SECONDS,
    )

    assert request.completion_timeout == 3600


@pytest.mark.asyncio
async def test_wait_detects_a_prompt_removed_from_the_queue(monkeypatch):
    history_results = iter([None, None, None])
    queue_results = iter([
        {
            "success": True,
            "data": {"queue_running": [[1, "prompt-1"]], "queue_pending": []},
        },
        {
            "success": True,
            "data": {"queue_running": [], "queue_pending": []},
        },
    ])

    async def read_history(prompt_id, timeout):
        assert prompt_id == "prompt-1"
        assert timeout <= 5
        return next(history_results)

    async def comfy_request(method, path, **kwargs):
        assert method == "GET"
        assert path == "/queue"
        return next(queue_results)

    monkeypatch.setattr(mcp_server, "_read_history_completion", read_history)
    monkeypatch.setattr(mcp_server, "_comfy_request", comfy_request)

    result = await mcp_server._wait_for_generation_completion(
        "prompt-1",
        timeout_seconds=5,
        poll_interval=0,
    )

    assert result["status"] == "cancelled"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_wait_returns_timeout_without_model_status_calls(monkeypatch):
    async def read_history(prompt_id, timeout):
        assert prompt_id == "prompt-1"
        return None

    async def comfy_request(method, path, **kwargs):
        return {
            "success": True,
            "data": {"queue_running": [], "queue_pending": []},
        }

    monkeypatch.setattr(mcp_server, "_read_history_completion", read_history)
    monkeypatch.setattr(mcp_server, "_comfy_request", comfy_request)

    result = await mcp_server._wait_for_generation_completion(
        "prompt-1",
        timeout_seconds=0,
        poll_interval=0,
    )

    assert result["status"] == "timeout"
    assert result["terminal"] is False


@pytest.mark.asyncio
async def test_queue_workflow_preserves_immediate_return(monkeypatch):
    calls = []

    async def execute_tool(ctx, tool_name, parameters):
        calls.append((tool_name, parameters))
        return {
            "prompt_id": "prompt-1",
            "queue_number": 4,
            "batch_count": 1,
            "node_errors": {},
            "execution_provenance": execution_provenance(),
        }

    async def unexpected_wait(*args, **kwargs):
        raise AssertionError("Immediate queueing must not wait")

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "_wait_for_generation_completion",
        unexpected_wait,
    )

    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(operation_id="queue-op-test01", wait_for_completion=False),
        object(),
    )

    assert calls == [("queue_workflow", {
        "operation_id": "queue-op-test01",
        "operation_request_hash": "d" * 64,
        "operation_payload": {"wait_for_completion": False, "completion_timeout": None},
    })]
    assert result["status"] == "queued"
    assert result["waited"] is False
    assert result["success"] is True
    assert result["execution_provenance"] == execution_provenance()


@pytest.mark.asyncio
async def test_queue_recovery_is_sanitized_and_bound_to_the_exact_operation(monkeypatch):
    secret = "PRIVATE RECOVERED PROMPT"
    claim = SimpleNamespace(request_hash="d" * 64)
    recovered = {
        "prompt_id": "prompt-recovered",
        "queue_number": 9,
        "batch_count": 1,
        "node_errors": {},
        "execution_provenance": execution_provenance(),
        "raw_result": {"prompt": secret},
        "untrusted": secret,
    }

    async def begin(request, ctx, tool):
        del request, ctx, tool
        return claim, {}, recovered

    async def no_execute(*args):
        raise AssertionError("Recovered queues must not execute again")

    monkeypatch.setattr(mcp_server, "_begin_narrow_operation", begin)
    monkeypatch.setattr(mcp_server, "_execute_tool", no_execute)
    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(
            operation_id="queue-op-test01",
            wait_for_completion=False,
        ),
        object(),
    )
    assert result["prompt_id"] == "prompt-recovered"
    assert secret not in json.dumps(result)
    assert "raw_result" not in result
    assert "untrusted" not in result

    mismatched = execution_provenance()
    mismatched["operation_request_hash"] = "e" * 64
    recovered["execution_provenance"] = mismatched
    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(
            operation_id="queue-op-test01",
            wait_for_completion=False,
        ),
        object(),
    )
    assert "execution_provenance" not in result
    assert result["execution_provenance_attestation"]["reason"] == (
        "operation_binding_mismatch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provenance", "reason"),
    [
        (None, "missing"),
        ({"prompt": "private plaintext"}, "malformed"),
    ],
)
async def test_queue_workflow_preserves_accepted_queue_when_provenance_is_unavailable(
    monkeypatch,
    provenance,
    reason,
):
    async def execute_tool(ctx, tool_name, parameters):
        del ctx, tool_name, parameters
        result = {
            "prompt_id": "prompt-accepted-without-proof",
            "queue_number": 1,
            "batch_count": 1,
            "node_errors": {},
        }
        if provenance is not None:
            result["execution_provenance"] = provenance
        return result

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(operation_id="queue-op-test01", wait_for_completion=False),
        object(),
    )

    assert result["success"] is True
    assert result["queued"] is True
    assert result["prompt_id"] == "prompt-accepted-without-proof"
    assert result["status"] == "queued"
    assert "execution_provenance" not in result
    assert result["execution_provenance_attestation"] == {
        "available": False,
        "reason": reason,
        "queue_accepted": True,
    }
    assert "suggestion" not in result
    assert "retry" not in json.dumps(result).lower()


@pytest.mark.asyncio
async def test_queue_workflow_prompt_id_is_authoritative_over_node_errors(
    monkeypatch,
    caplog,
):
    secret = "PRIVATE VALIDATION INPUT: dusty woman identity"
    caplog.set_level(logging.DEBUG)

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, tool_name, parameters
        return {
            "prompt_id": "prompt-accepted-with-diagnostics",
            "queue_number": 3,
            "batch_count": 1,
            "node_errors": {"34": {"message": secret, "input": secret}},
            "execution_provenance": execution_provenance(),
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(operation_id="queue-op-test01", wait_for_completion=False),
        object(),
    )

    assert result["success"] is True
    assert result["queued"] is True
    assert result["status"] == "queued"
    assert result["prompt_id"] == "prompt-accepted-with-diagnostics"
    assert result["validation_warnings"] == {
        "node_errors_present": True,
        "details_redacted": True,
        "queue_accepted": True,
    }
    assert "node_errors" not in result
    assert "suggestion" not in result
    assert "retry" not in json.dumps(result).lower()
    assert secret not in json.dumps(result)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_queue_workflow_validation_failure_without_prompt_id_is_sanitized(
    monkeypatch,
    caplog,
):
    secret = "PRIVATE VALIDATION INPUT: face replacement prompt"
    caplog.set_level(logging.DEBUG)

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, tool_name, parameters
        return {
            "node_errors": {"34": {"message": secret, "input": secret}},
            "error": secret,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(operation_id="queue-op-test01", wait_for_completion=False),
        object(),
    )

    assert result == {
        "success": False,
        "status": "validation_failed",
        "side_effect_known": True,
        "error": "Workflow validation failed before a prompt ID was issued.",
        "node_errors_present": True,
        "node_error_details_redacted": True,
    }
    assert secret not in json.dumps(result)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_queue_workflow_without_prompt_id_returns_bounded_unknown_result(
    monkeypatch,
    caplog,
):
    secret = "PRIVATE PROMPT: dusty woman identity"
    caplog.set_level(logging.DEBUG)

    async def execute_tool(ctx, tool_name, parameters):
        del ctx, tool_name, parameters
        return {
            "error": secret,
            "raw_result": {"prompt": secret},
            "node_errors": {},
            "execution_provenance": {"prompt": secret},
            "untrusted_extra": [secret],
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)

    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(operation_id="queue-op-test01", wait_for_completion=False),
        object(),
    )

    assert result == {
        "success": False,
        "status": "queue_result_unconfirmed",
        "side_effect_known": False,
        "error": "Queue result did not contain a valid prompt ID.",
    }
    assert "raw_result" not in result
    assert secret not in json.dumps(result)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"prompt": "private plaintext"}),
        lambda value: value.pop("captured_at_ms"),
        lambda value: value["api_prompt"].update({"sha256": 123}),
        lambda value: value["api_prompt"].update({"canonical_bytes": True}),
        lambda value: value["api_prompt"].update({"canonical_bytes": 8 * 1024 * 1024 + 1}),
        lambda value: value["editable_workflow"].pop("workflow_id"),
        lambda value: value["editable_workflow"].update({"revision": True}),
    ],
)
def test_queue_execution_provenance_is_exact_bounded_and_plaintext_free(mutate):
    provenance = execution_provenance()
    mutate(provenance)

    with pytest.raises(RuntimeError, match="malformed"):
        mcp_server._validated_execution_provenance(provenance)


@pytest.mark.asyncio
async def test_queue_workflow_waits_once_with_per_call_timeout(monkeypatch):
    execute_count = 0
    wait_calls = []

    async def execute_tool(ctx, tool_name, parameters):
        nonlocal execute_count
        execute_count += 1
        return {
            "prompt_id": "prompt-1",
            "queue_number": 2,
            "batch_count": 1,
            "node_errors": {},
            "execution_provenance": execution_provenance(),
        }

    async def wait_for_completion(prompt_id, timeout_seconds):
        wait_calls.append((prompt_id, timeout_seconds))
        return {
            "success": True,
            "status": "completed",
            "completed": True,
            "terminal": True,
            "outputs": {},
            "prompt_id": prompt_id,
        }

    monkeypatch.setattr(mcp_server, "_execute_tool", execute_tool)
    monkeypatch.setattr(
        mcp_server,
        "_wait_for_generation_completion",
        wait_for_completion,
    )

    result = await mcp_server.queue_workflow.fn(
        mcp_server.QueueWorkflowRequest(
            operation_id="queue-op-test01",
            wait_for_completion=True,
            completion_timeout=17,
        ),
        object(),
    )

    assert execute_count == 1
    assert wait_calls == [("prompt-1", 17)]
    assert result["status"] == "completed"
    assert result["waited"] is True
    assert result["completion_timeout"] == 17
