import mcp_server
import pytest
from config import MAX_GENERATION_COMPLETION_TIMEOUT_SECONDS


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
        "exception_message": "out of memory",
    }]
    assert cancelled["status"] == "cancelled"
    assert cancelled["terminal"] is True


def test_per_call_wait_accepts_the_full_supported_timeout():
    request = mcp_server.QueueWorkflowRequest(
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
        mcp_server.QueueWorkflowRequest(wait_for_completion=False),
        object(),
    )

    assert calls == [("queue_workflow", {})]
    assert result["status"] == "queued"
    assert result["waited"] is False
    assert result["success"] is True


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
