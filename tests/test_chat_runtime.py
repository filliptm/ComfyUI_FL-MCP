import asyncio
import json
from types import SimpleNamespace

import chat_runtime as chat_runtime_module
import pytest
from chat_config import ChatSettingsStore
from chat_runtime import (
    ActiveRun,
    ChatRuntime,
    PendingApproval,
    approval_fingerprint,
    bridge_settings,
    claude_tool_name,
    codex_tool_name,
    install_codex_approval_handler,
    normalize_approval_decision,
    normalize_assistant_timeline,
    should_request_approval,
    tool_result_content,
    tools_for_message,
    wait_for_claude_mcp,
)
from chat_store import ChatStore


def _payload(raw: str):
    line = next(line for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(line[5:].strip())


@pytest.mark.asyncio
async def test_run_events_track_text_tools_retries_and_replay(tmp_path):
    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")
    runtime = ChatRuntime(store)
    state = ActiveRun("run-1", "conversation-1", "session-1")
    runtime.runs[state.run_id] = state

    await runtime.publish(state, {
        "type": "TOOL_CALL_START",
        "toolCallId": "first",
        "toolCallName": "workflow_overview",
    })
    await runtime.publish(state, {
        "type": "TOOL_CALL_ARGS",
        "toolCallId": "first",
        "delta": "{}",
    })
    await runtime.publish(state, {
        "type": "TOOL_CALL_START",
        "toolCallId": "retry",
        "toolCallName": "workflow_overview",
    })
    await runtime.publish(state, {
        "type": "TOOL_CALL_RESULT",
        "toolCallId": "retry",
        "content": '{"nodes": 3}',
    })
    await runtime.publish(state, {
        "type": "TEXT_MESSAGE_CONTENT",
        "delta": "Three nodes.",
    })
    await runtime.publish(state, {"type": "RUN_FINISHED"})

    assert state.assistant_text == "Three nodes."
    assert state.tool_steps[0]["status"] == "retried"
    assert state.tool_steps[0]["arguments"] == "{}"
    assert state.tool_steps[0]["contentOffset"] == 0
    assert state.tool_steps[1]["status"] == "done"
    assert state.tool_steps[1]["result"] == '{"nodes": 3}'
    assert state.tool_steps[1]["contentOffset"] == 0

    state.done = True
    replay = [_payload(raw) async for raw in runtime.subscribe(state.run_id)]
    assert replay[0]["type"] == "TOOL_CALL_START"
    assert replay[-1]["type"] == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_tool_steps_capture_chronological_text_offsets(tmp_path):
    runtime = ChatRuntime(ChatStore(tmp_path / "chat.db", tmp_path / "missing.db"))
    state = ActiveRun("run-1", "conversation-1", "session-1")

    await runtime.publish(state, {
        "type": "TEXT_MESSAGE_CONTENT",
        "delta": "Before tool. ",
    })
    await runtime.publish(state, {
        "type": "TOOL_CALL_START",
        "toolCallId": "tool-1",
        "toolCallName": "workflow_overview",
    })
    await runtime.publish(state, {
        "type": "TEXT_MESSAGE_CONTENT",
        "delta": "After tool.",
    })

    assert state.tool_steps[0]["contentOffset"] == len("Before tool. ")
    content, steps = normalize_assistant_timeline(
        f"  {state.assistant_text}  ",
        [{**state.tool_steps[0], "contentOffset": state.tool_steps[0]["contentOffset"] + 2}],
    )
    assert content == "Before tool. After tool."
    assert steps[0]["contentOffset"] == len("Before tool. ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approved", "resolution"),
    [(True, "approved"), (False, "denied")],
)
async def test_approval_resolution_preserves_boolean_compatibility(
    tmp_path,
    approved,
    resolution,
):
    runtime = ChatRuntime(ChatStore(tmp_path / "chat.db", tmp_path / "missing.db"))
    future = asyncio.get_running_loop().create_future()
    runtime.approvals["approval-1"] = PendingApproval(
        "approval-1",
        "run-1",
        future,
    )

    assert await runtime.resolve_approval("approval-1", approved)
    assert await future == resolution


@pytest.mark.asyncio
async def test_always_allow_resolution_persists_tool_rule(
    tmp_path,
    monkeypatch,
):
    settings = ChatSettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(chat_runtime_module, "chat_settings", settings)
    runtime = ChatRuntime(ChatStore(tmp_path / "chat.db", tmp_path / "missing.db"))
    state_settings = {
        "approval_mode": "autonomous_edits",
        "always_allowed_tools": [],
    }
    runtime.runs["run-1"] = ActiveRun(
        "run-1",
        "conversation-1",
        "session-1",
        settings=state_settings,
    )
    future = asyncio.get_running_loop().create_future()
    runtime.approvals["approval-1"] = PendingApproval(
        "approval-1",
        "run-1",
        future,
        "queue_workflow",
    )

    assert await runtime.resolve_approval("approval-1", "always_allow")
    assert await future == "always_allowed"
    assert settings.load()["always_allowed_tools"] == ["queue_workflow"]
    assert state_settings["always_allowed_tools"] == ["queue_workflow"]


def test_approval_policy_supports_tool_rules_and_global_bypass():
    defaults = {
        "approval_mode": "autonomous_edits",
        "always_allowed_tools": [],
    }
    assert should_request_approval("queue_workflow", defaults) is True
    assert should_request_approval("workflow_overview", defaults) is False
    assert should_request_approval("queue_workflow", {
        **defaults,
        "always_allowed_tools": ["queue_workflow"],
    }) is False
    assert should_request_approval("workflow_delete_file", {
        **defaults,
        "approval_mode": "bypass_all",
    }) is False
    assert normalize_approval_decision("allow_once") == "approved"
    assert normalize_approval_decision("always_allow") == "always_allowed"
    assert normalize_approval_decision("deny") == "denied"


@pytest.mark.asyncio
async def test_global_bypass_updates_active_runs_and_releases_pending_approvals(
    tmp_path,
):
    runtime = ChatRuntime(ChatStore(tmp_path / "chat.db", tmp_path / "missing.db"))
    settings = {
        "approval_mode": "autonomous_edits",
        "always_allowed_tools": [],
    }
    state = ActiveRun("run-1", "conversation-1", "session-1", settings=settings)
    runtime.runs[state.run_id] = state
    future = asyncio.get_running_loop().create_future()
    runtime.approvals["approval-1"] = PendingApproval(
        "approval-1",
        state.run_id,
        future,
        "queue_workflow",
    )

    resolved = runtime.sync_approval_settings({
        "approval_mode": "bypass_all",
        "always_allowed_tools": [],
    })

    assert resolved == 1
    assert state.settings["approval_mode"] == "bypass_all"
    assert await future == "approved"
    assert runtime.approvals == {}


def test_intent_tool_filter_keeps_core_and_adds_narrow_groups():
    basic = tools_for_message("Inspect the open graph")
    assert "workflow_overview" in basic
    assert "manager_queue_action" not in basic

    manager = tools_for_message("Install a missing custom node with Manager")
    assert "manager_search_nodes" in manager
    assert "manager_queue_action" in manager

    coding = tools_for_message("Patch Python code in this custom node pack")
    assert "custom_nodes_apply_patch" in coding
    assert "comfy_models_list" not in coding


def test_completed_run_retention_is_bounded(tmp_path):
    runtime = ChatRuntime(ChatStore(tmp_path / "chat.db", tmp_path / "missing.db"))
    runtime.MAX_RETAINED_RUNS = 2
    runtime.runs = {
        "one": ActiveRun("one", "conversation", "session", done=True),
        "two": ActiveRun("two", "conversation", "session", done=True),
        "active": ActiveRun("active", "conversation", "session"),
    }

    runtime._prune_completed_runs()

    assert "one" not in runtime.runs
    assert set(runtime.runs) == {"two", "active"}


def test_embedded_mcp_uses_loaded_bridge_port(monkeypatch):
    monkeypatch.setattr(bridge_settings, "ws_host", "0.0.0.0")
    monkeypatch.setattr(bridge_settings, "ws_port", 18000)

    assert ChatRuntime._ws_url() == "ws://127.0.0.1:18000/ws"


def test_empty_request_retry_uses_same_approval_fingerprint():
    assert approval_fingerprint("queue_workflow", {}) == approval_fingerprint(
        "queue_workflow",
        {"request": {}},
    )
    assert approval_fingerprint("queue_workflow", {"request": {"count": 2}}) != (
        approval_fingerprint("queue_workflow", {})
    )


def test_claude_tool_names_and_results_are_normalized():
    assert claude_tool_name("mcp__ren__workflow_overview") == "workflow_overview"
    assert claude_tool_name("Read") is None
    assert tool_result_content({"nodes": 2}) == '{"nodes":2}'


def test_codex_approval_tool_names_and_compatibility_hook_are_normalized():
    assert codex_tool_name({
        "_meta": {"tool_name": "queue_workflow"},
        "message": "ignored",
    }) == "queue_workflow"
    assert codex_tool_name({
        "message": 'Allow the ren MCP server to run tool "workflow_delete_file"?',
    }) == "workflow_delete_file"

    class SyncClient:
        _approval_handler = None

    class AsyncClient:
        _sync = SyncClient()

    class Codex:
        _client = AsyncClient()

    def handler(*_args):
        return {"action": "decline"}

    install_codex_approval_handler(Codex(), handler)
    assert Codex._client._sync._approval_handler is handler


@pytest.mark.asyncio
async def test_cancel_expires_pending_approval_before_provider_interrupt(tmp_path):
    runtime = ChatRuntime(ChatStore(tmp_path / "chat.db", tmp_path / "missing.db"))
    state = ActiveRun("run-1", "conversation-1", "session-1")
    interrupted = False

    async def active_task():
        await asyncio.Event().wait()

    async def interrupt():
        nonlocal interrupted
        interrupted = True
        assert await future == "expired"

    future = asyncio.get_running_loop().create_future()
    runtime.approvals["approval-1"] = PendingApproval(
        "approval-1",
        state.run_id,
        future,
    )
    state.task = asyncio.create_task(active_task())
    state.cancel_callback = interrupt
    runtime.runs[state.run_id] = state

    assert await runtime.cancel(state.run_id)
    assert interrupted is True
    assert "approval-1" not in runtime.approvals
    with pytest.raises(asyncio.CancelledError):
        await state.task


@pytest.mark.asyncio
async def test_claude_waits_for_mcp_tool_discovery(monkeypatch):
    class FakeClient:
        calls = 0

        async def get_mcp_status(self):
            self.calls += 1
            status = "connected" if self.calls == 2 else "pending"
            return {"mcpServers": [{"name": "ren", "status": status}]}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("chat_runtime.asyncio.sleep", no_wait)
    client = FakeClient()
    await wait_for_claude_mcp(client, timeout=1)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_claude_subscription_streams_tools_approvals_and_persists_session(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import (
        PermissionResultAllow,
        ResultMessage,
        StreamEvent,
        ToolResultBlock,
        UserMessage,
    )

    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")
    conversation = store.create_conversation(
        provider="claude_subscription",
        model="sonnet",
    )
    store.append_message(
        conversation["id"],
        "user",
        "Queue this workflow after checking it.",
    )
    runtime = ChatRuntime(store)
    state = ActiveRun("run-1", conversation["id"], "canvas-session")
    store.create_run(state.run_id, state.conversation_id)
    monkeypatch.setattr("chat_runtime.shutil.which", lambda _name: "/fake/claude")

    async def fake_query(*, prompt, options):
        prompt_items = [item async for item in prompt]
        assert prompt_items[0]["message"]["content"].startswith("Queue this workflow")
        assert options.cli_path == "/fake/claude"
        assert options.env["ANTHROPIC_API_KEY"] == ""
        assert options.mcp_servers["ren"]["env"]["FL_MCP_ALLOWED_TOOLS"]

        safe = await options.can_use_tool(
            "mcp__ren__workflow_overview",
            {"request": {}},
            None,
        )
        assert isinstance(safe, PermissionResultAllow)

        approval_task = asyncio.create_task(options.can_use_tool(
            "mcp__ren__queue_workflow",
            {"request": {}},
            None,
        ))
        while not runtime.approvals:
            await asyncio.sleep(0)
        approval_id = next(iter(runtime.approvals))
        assert await runtime.resolve_approval(approval_id, True)
        assert isinstance(await approval_task, PermissionResultAllow)

        yield StreamEvent(
            uuid="event-1",
            session_id="claude-session",
            event={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield StreamEvent(
            uuid="event-2",
            session_id="claude-session",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Checked. "},
            },
        )
        yield StreamEvent(
            uuid="event-3",
            session_id="claude-session",
            event={
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "mcp__ren__workflow_overview",
                    "input": {},
                },
            },
        )
        yield StreamEvent(
            uuid="event-4",
            session_id="claude-session",
            event={
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"request":{}}',
                },
            },
        )
        yield UserMessage(content=[
            ToolResultBlock(tool_use_id="tool-1", content='{"total_nodes":8}')
        ])
        yield StreamEvent(
            uuid="event-5",
            session_id="claude-session",
            event={
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "text_delta", "text": "Eight nodes."},
            },
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=2,
            session_id="claude-session",
            result="Checked. Eight nodes.",
            usage={"input_tokens": 10, "output_tokens": 4},
        )

    runtime.claude_query_factory = fake_query
    await runtime._execute_claude_subscription(
        state,
        {
            "provider": "claude_subscription",
            "model": "sonnet",
            "temperature": 0.2,
        },
    )

    payloads = [_payload(raw) for raw in state.events]
    assert payloads[0]["type"] == "RUN_STARTED"
    assert any(item["type"] == "TOOL_CALL_START" for item in payloads)
    assert any(
        item.get("name") == "approval_required"
        for item in payloads
        if item["type"] == "CUSTOM"
    )
    assert payloads[-1]["type"] == "RUN_FINISHED"
    assistant = store.list_messages(conversation["id"])[-1]
    assert assistant["content"] == "Checked. Eight nodes."
    assert assistant["metadata"]["claudeSessionId"] == "claude-session"
    assert assistant["metadata"]["toolSteps"][0]["contentOffset"] == len("Checked. ")


@pytest.mark.asyncio
async def test_codex_subscription_streams_ren_tools_and_persists_thread(
    tmp_path,
    monkeypatch,
):
    import openai_codex
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        ItemCompletedNotification,
        ItemStartedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )
    from openai_codex.models import Notification

    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")
    conversation = store.create_conversation(
        provider="codex_subscription",
        model="gpt-5.6-sol",
    )
    store.append_message(
        conversation["id"],
        "user",
        "Inspect the open workflow.",
    )
    runtime = ChatRuntime(store)
    state = ActiveRun("run-1", conversation["id"], "canvas-session")
    store.create_run(state.run_id, state.conversation_id)

    events = [
        Notification(
            method="item/started",
            payload=ItemStartedNotification.model_validate({
                "threadId": "codex-thread",
                "turnId": "codex-turn",
                "startedAtMs": 1,
                "item": {
                    "type": "mcpToolCall",
                    "id": "tool-1",
                    "server": "ren",
                    "tool": "workflow_overview",
                    "arguments": {"request": {}},
                    "status": "inProgress",
                },
            }),
        ),
        Notification(
            method="item/completed",
            payload=ItemCompletedNotification.model_validate({
                "threadId": "codex-thread",
                "turnId": "codex-turn",
                "completedAtMs": 2,
                "item": {
                    "type": "mcpToolCall",
                    "id": "tool-1",
                    "server": "ren",
                    "tool": "workflow_overview",
                    "arguments": {"request": {}},
                    "status": "completed",
                    "result": {
                        "content": [{"type": "text", "text": '{"total_nodes":8}'}],
                        "structuredContent": {"total_nodes": 8},
                    },
                },
            }),
        ),
        Notification(
            method="item/agentMessage/delta",
            payload=AgentMessageDeltaNotification(
                thread_id="codex-thread",
                turn_id="codex-turn",
                item_id="message-1",
                delta="Eight nodes.",
            ),
        ),
        Notification(
            method="item/completed",
            payload=ItemCompletedNotification.model_validate({
                "threadId": "codex-thread",
                "turnId": "codex-turn",
                "completedAtMs": 3,
                "item": {
                    "type": "agentMessage",
                    "id": "message-1",
                    "text": "Eight nodes.",
                },
            }),
        ),
        Notification(
            method="thread/tokenUsage/updated",
            payload=ThreadTokenUsageUpdatedNotification.model_validate({
                "threadId": "codex-thread",
                "turnId": "codex-turn",
                "tokenUsage": {
                    "last": {
                        "cachedInputTokens": 0,
                        "inputTokens": 10,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 0,
                        "totalTokens": 12,
                    },
                    "total": {
                        "cachedInputTokens": 0,
                        "inputTokens": 10,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 0,
                        "totalTokens": 12,
                    },
                },
            }),
        ),
        Notification(
            method="turn/completed",
            payload=TurnCompletedNotification.model_validate({
                "threadId": "codex-thread",
                "turn": {
                    "id": "codex-turn",
                    "items": [],
                    "status": "completed",
                },
            }),
        ),
    ]

    class FakeTurn:
        async def interrupt(self):
            return None

        async def stream(self):
            for event in events:
                yield event

    class FakeThread:
        def __init__(self, codex, thread_id):
            self.codex = codex
            self.id = thread_id

        async def turn(self, input_text, **_kwargs):
            assert input_text == "Inspect the open workflow."
            return FakeTurn()

    class FakeSyncClient:
        _approval_handler = None

    class FakeClient:
        def __init__(self):
            self._sync = FakeSyncClient()

        async def request(self, method, _params, *, response_model):
            del response_model
            if method == "config/read":
                return SimpleNamespace(config=SimpleNamespace(
                    model_dump=lambda **_kwargs: {
                        "mcp_servers": {"other": {}},
                        "plugins": {"example@plugin": {}},
                    }
                ))
            assert method == "mcpServerStatus/list"
            return SimpleNamespace(data=[
                SimpleNamespace(name="ren", tools={"workflow_overview": {}})
            ])

        async def thread_start(self, params):
            assert params.config["features"]["hooks"] is False
            assert params.config["mcp_servers"]["other"]["enabled"] is False
            assert params.config["plugins"]["example@plugin"]["enabled"] is False
            assert params.config["mcp_servers"]["ren"]["enabled_tools"]
            return SimpleNamespace(thread=SimpleNamespace(id="codex-thread"))

    class FakeCodex:
        def __init__(self, config):
            assert config.env["OPENAI_API_KEY"] == ""
            assert config.env["CODEX_API_KEY"] == ""
            self._client = FakeClient()

        async def __aenter__(self):
            return self

        async def account(self):
            return SimpleNamespace(account=SimpleNamespace(
                root=SimpleNamespace(type="chatgpt")
            ))

        async def close(self):
            return None

    monkeypatch.setattr(openai_codex, "AsyncThread", FakeThread)
    runtime.codex_factory = FakeCodex

    await runtime._execute_codex_subscription(
        state,
        {
            "provider": "codex_subscription",
            "model": "gpt-5.6-sol",
            "temperature": 0.2,
        },
    )

    payloads = [_payload(raw) for raw in state.events]
    assert payloads[0]["type"] == "RUN_STARTED"
    assert any(item["type"] == "TOOL_CALL_START" for item in payloads)
    assert payloads[-1]["type"] == "RUN_FINISHED"
    assistant = store.list_messages(conversation["id"])[-1]
    assert assistant["content"] == "Eight nodes."
    assert assistant["metadata"]["codexThreadId"] == "codex-thread"
    assert assistant["metadata"]["usage"]["total"]["totalTokens"] == 12
    assert assistant["metadata"]["toolSteps"][0]["name"] == "workflow_overview"
