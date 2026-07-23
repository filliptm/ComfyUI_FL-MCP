"""Persistent AG-UI chat runs backed by the FL-MCP stdio server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chat_config import (
    PROJECT_ROOT,
    PROVIDER_PRESETS,
    chat_settings,
    credential_store,
)
from chat_security import classify_tool, requires_approval
from chat_store import ChatStore, chat_store
from config import settings as bridge_settings

logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).with_name("chat_prompt.md")

CORE_CHAT_TOOLS = {
    "workflow_overview",
    "workflow_get_current_json",
    "find_node",
    "get_current_node_selection",
    "get_node_values",
    "get_node_slots",
    "create_nodes",
    "remove_nodes",
    "set_node_values",
    "connect_nodes_batch",
    "get_layout",
    "modify_layout",
    "take_screenshot",
    "queue_workflow",
    "get_queue_status",
    "mcp_capability_audit",
}

INTENT_TOOL_GROUPS = {
    "debug": {
        "get_execution_history",
        "get_execution_details",
        "get_queue_status_details",
        "clear_error_buffer",
        "comfy_get_logs",
    },
    "manager": {
        "manager_search_nodes",
        "manager_get_node_mappings",
        "manager_check_updates",
        "manager_queue_action",
        "manager_queue_status",
        "manager_queue_start",
        "manager_v4_installed_packs",
    },
    "models": {
        "comfy_models_list",
        "comfy_assets_list",
        "comfy_search_resources",
        "manager_search_external_models",
    },
    "coding": {
        "custom_nodes_list_packs",
        "custom_nodes_read_file_excerpt",
        "custom_nodes_search",
        "custom_nodes_write_file",
        "custom_nodes_apply_patch",
        "custom_nodes_validate_pack",
    },
    "files": {
        "workflow_list_files",
        "workflow_read_file",
        "workflow_save_current",
        "workflow_load_json",
        "workflow_delete_file",
    },
}

CLAUDE_BUILTIN_TOOLS = {
    "Task",
    "Agent",
    "Skill",
    "EnterPlanMode",
    "ExitPlanMode",
    "TodoWrite",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "AskUserQuestion",
    "ToolSearch",
    "ScheduleWakeup",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Bash",
    "BashOutput",
    "KillBash",
    "KillShell",
    "WebFetch",
    "WebSearch",
    "Monitor",
    "PushNotification",
    "RemoteTrigger",
    "CronCreate",
    "CronDelete",
    "CronList",
    "EnterWorktree",
    "ExitWorktree",
    "DesignSync",
    "Workflow",
}


def claude_tool_name(tool_name: str) -> str | None:
    prefix = "mcp__ren__"
    return tool_name[len(prefix):] if tool_name.startswith(prefix) else None


def tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if hasattr(content, "model_dump"):
        content = content.model_dump(mode="json", by_alias=True)
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def codex_tool_name(params: dict[str, Any]) -> str | None:
    """Extract the Ren tool name from a Codex MCP approval request."""
    metadata = params.get("_meta")
    if isinstance(metadata, dict) and metadata.get("tool_name"):
        return str(metadata["tool_name"])
    match = re.search(r'run tool "([^"]+)"', str(params.get("message") or ""))
    return match.group(1) if match else None


def install_codex_approval_handler(codex: Any, handler: Callable[..., Any]) -> None:
    """Install the SDK's synchronous app-server callback behind one compatibility gate."""
    try:
        sync_client = codex._client._sync
    except AttributeError as exc:
        raise RuntimeError(
            "The installed Codex SDK no longer exposes its approval callback. "
            "Install the FL-MCP-supported openai-codex version."
        ) from exc
    sync_client._approval_handler = handler


async def wait_for_claude_mcp(
    client: Any,
    *,
    server_name: str = "ren",
    timeout: float = 15,
) -> None:
    """Wait until Claude Code has discovered Ren's MCP tools."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_status = "pending"
    last_error = None
    while True:
        response = await client.get_mcp_status()
        servers = response.get("mcpServers", []) if isinstance(response, dict) else []
        server = next(
            (
                item
                for item in servers
                if isinstance(item, dict) and item.get("name") == server_name
            ),
            None,
        )
        if server:
            last_status = str(server.get("status") or "pending")
            last_error = server.get("error")
            if last_status == "connected":
                return
            if last_status in {"failed", "needs-auth", "disabled"}:
                detail = f": {last_error}" if last_error else ""
                raise RuntimeError(
                    f"Claude Code could not connect to the Ren MCP server "
                    f"({last_status}){detail}"
                )
        if asyncio.get_running_loop().time() >= deadline:
            detail = f": {last_error}" if last_error else ""
            raise RuntimeError(
                f"Claude Code timed out waiting for the Ren MCP server "
                f"({last_status}){detail}"
            )
        await asyncio.sleep(0.1)


def tools_for_message(message: str) -> set[str]:
    text = message.lower()
    selected = set(CORE_CHAT_TOOLS)
    if any(word in text for word in ("error", "broken", "debug", "failed", "queue")):
        selected.update(INTENT_TOOL_GROUPS["debug"])
    if any(
        word in text
        for word in ("install", "manager", "missing node", "custom node", "update node")
    ):
        selected.update(INTENT_TOOL_GROUPS["manager"])
    if any(word in text for word in ("model", "checkpoint", "lora", "vae", "asset")):
        selected.update(INTENT_TOOL_GROUPS["models"])
    if any(word in text for word in ("code", "python", "javascript", "custom node pack")):
        selected.update(INTENT_TOOL_GROUPS["coding"])
    if any(
        word in text
        for word in ("save workflow", "load workflow", "workflow file", "delete workflow")
    ):
        selected.update(INTENT_TOOL_GROUPS["files"])
    return selected


def approval_fingerprint(tool_name: str, tool_args: dict[str, Any]) -> str:
    """Treat an omitted empty request wrapper as the same retried tool call."""
    normalized_args = {} if tool_args in ({}, {"request": {}}) else tool_args
    return json.dumps(
        {"tool": tool_name, "arguments": normalized_args},
        sort_keys=True,
        separators=(",", ":"),
    )


def _event_payload(raw: str) -> dict[str, Any] | None:
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                value = json.loads(line[5:].strip())
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def normalize_assistant_timeline(
    text: str,
    tool_steps: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Keep persisted tool offsets aligned when response whitespace is trimmed."""
    content = text.strip()
    leading_trim = len(text) - len(text.lstrip())
    content_length = len(content)
    normalized_steps = []
    for step in tool_steps:
        try:
            raw_offset = int(step.get("contentOffset", len(text)))
        except (TypeError, ValueError):
            raw_offset = len(text)
        offset = raw_offset - leading_trim
        normalized_steps.append({
            **step,
            "contentOffset": max(0, min(offset, content_length)),
        })
    return content, normalized_steps


@dataclass
class ActiveRun:
    run_id: str
    conversation_id: str
    session_id: str
    events: list[str] = field(default_factory=list)
    subscribers: list[asyncio.Queue[str | None]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    done: bool = False
    assistant_text: str = ""
    tool_steps: list[dict[str, Any]] = field(default_factory=list)
    error_emitted: bool = False
    cancel_callback: Callable[[], Awaitable[Any]] | None = None


@dataclass
class PendingApproval:
    approval_id: str
    run_id: str
    future: asyncio.Future[str]


class ChatRuntime:
    MAX_EVENTS = 10_000
    MAX_RETAINED_RUNS = 100

    def __init__(self, store: ChatStore = chat_store):
        self.store = store
        self.runs: dict[str, ActiveRun] = {}
        self.approvals: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self.model_factory = None
        self.claude_query_factory = None
        self.claude_client_factory = None
        self.codex_factory = None

    def available(self) -> tuple[bool, str | None]:
        try:
            provider_type = PROVIDER_PRESETS[chat_settings.load()["provider"]]["type"]
            if provider_type == "claude_cli":
                import claude_agent_sdk  # noqa: F401
            elif provider_type == "codex_cli":
                import openai_codex  # noqa: F401
            else:
                import ag_ui  # noqa: F401
                import pydantic_ai  # noqa: F401
        except Exception as exc:
            return False, f"Chat dependencies are unavailable: {exc}"
        return True, None

    async def start(
        self,
        *,
        session_id: str,
        conversation_id: str | None,
        message: str,
    ) -> ActiveRun:
        text = message.strip()
        if not text:
            raise ValueError("Message cannot be empty.")
        settings = chat_settings.load()
        if not settings["model"]:
            raise ValueError("Choose a model before sending a message.")
        identifier = conversation_id or str(uuid.uuid4())
        conversation = self.store.ensure_conversation(
            identifier,
            settings["provider"],
            settings["model"],
        )
        self.store.update_conversation(
            identifier,
            provider=settings["provider"],
            model=settings["model"],
        )
        if conversation["title"] == "New chat":
            title = " ".join(text.split())[:60] or "New chat"
            self.store.update_conversation(identifier, title=title)
        async with self._lock:
            if any(
                not state.done and state.conversation_id == identifier
                for state in self.runs.values()
            ):
                raise ValueError("This conversation already has an active run.")
            user_message = self.store.append_message(
                identifier,
                "user",
                text,
                provider=settings["provider"],
                model=settings["model"],
            )
            run_id = str(uuid.uuid4())
            state = ActiveRun(run_id, identifier, session_id)
            self.runs[run_id] = state
            self._prune_completed_runs()
            self.store.create_run(run_id, identifier)
            state.task = asyncio.create_task(
                self._execute(state, user_message["id"]),
                name=f"fl-mcp-chat-{run_id}",
            )
            return state

    async def subscribe(self, run_id: str) -> AsyncIterator[str]:
        state = self.runs.get(run_id)
        if not state:
            raise KeyError(run_id)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        async with self._lock:
            replay = list(state.events)
            done = state.done
            if not done:
                state.subscribers.append(queue)
        try:
            for event in replay:
                yield event
            if done:
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            if queue in state.subscribers:
                state.subscribers.remove(queue)

    async def publish(self, state: ActiveRun, event: str | dict[str, Any]) -> None:
        raw = _sse(event) if isinstance(event, dict) else event
        if len(state.events) < self.MAX_EVENTS:
            state.events.append(raw)
        payload = _event_payload(raw)
        if payload:
            event_type = payload.get("type")
            if event_type == "RUN_ERROR":
                state.error_emitted = True
            if event_type == "TEXT_MESSAGE_CONTENT":
                state.assistant_text += str(payload.get("delta") or "")
            elif event_type == "TOOL_CALL_START":
                tool_name = str(payload.get("toolCallName") or "")
                for step in reversed(state.tool_steps):
                    if step.get("name") == tool_name and step.get("status") == "running":
                        step["status"] = "retried"
                        break
                state.tool_steps.append({
                    "id": payload.get("toolCallId"),
                    "name": tool_name,
                    "status": "running",
                    "risk": classify_tool(tool_name),
                    "arguments": "",
                    "contentOffset": len(state.assistant_text),
                })
            elif event_type == "TOOL_CALL_ARGS":
                tool_id = payload.get("toolCallId")
                for step in reversed(state.tool_steps):
                    if step.get("id") == tool_id:
                        step["arguments"] += str(payload.get("delta") or "")
                        break
            elif event_type == "TOOL_CALL_RESULT":
                tool_id = payload.get("toolCallId")
                for step in reversed(state.tool_steps):
                    if step.get("id") == tool_id:
                        step["status"] = "done"
                        step["result"] = payload.get("content")
                        break
            elif event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                terminal_status = "finished" if event_type == "RUN_FINISHED" else "failed"
                for step in state.tool_steps:
                    if step.get("status") == "running":
                        step["status"] = terminal_status
        for subscriber in list(state.subscribers):
            subscriber.put_nowait(raw)

    async def cancel(self, run_id: str) -> bool:
        state = self.runs.get(run_id)
        if not state or state.done or not state.task:
            return False
        self._expire_approvals(state.run_id)
        if state.cancel_callback is not None:
            try:
                await state.cancel_callback()
            except Exception:
                logger.debug("Provider interrupt failed for run %s", run_id, exc_info=True)
        state.task.cancel()
        return True

    async def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        pending = self.approvals.pop(approval_id, None)
        if not pending or pending.future.done():
            return False
        self.store.resolve_approval(approval_id, approved)
        pending.future.set_result("approved" if approved else "denied")
        return True

    async def shutdown(self) -> None:
        active_runs = [
            state
            for state in self.runs.values()
            if state.task is not None and not state.task.done()
        ]
        for state in active_runs:
            await self.cancel(state.run_id)
        tasks = [state.task for state in active_runs if state.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _expire_approvals(self, run_id: str) -> None:
        for approval_id, pending in list(self.approvals.items()):
            if pending.run_id != run_id:
                continue
            self.approvals.pop(approval_id, None)
            self.store.resolve_approval(approval_id, False)
            if not pending.future.done():
                pending.future.set_result("expired")

    def _prune_completed_runs(self) -> None:
        overflow = len(self.runs) - self.MAX_RETAINED_RUNS
        if overflow <= 0:
            return
        for run_id, state in list(self.runs.items()):
            if overflow <= 0:
                break
            if state.done:
                self.runs.pop(run_id, None)
                overflow -= 1

    async def _execute(self, state: ActiveRun, user_message_id: str) -> None:
        del user_message_id
        settings = chat_settings.load()
        try:
            provider_type = PROVIDER_PRESETS[settings["provider"]]["type"]
            if provider_type == "claude_cli":
                await self._execute_claude_subscription(state, settings)
                return
            if provider_type == "codex_cli":
                await self._execute_codex_subscription(state, settings)
                return

            from pydantic_ai import Agent
            from pydantic_ai.ag_ui import RunAgentInput, run_ag_ui
            from pydantic_ai.mcp import MCPServerStdio

            model = (
                self.model_factory(settings)
                if self.model_factory is not None
                else self._build_model(settings)
            )
            prompt = PROMPT_PATH.read_text(encoding="utf-8")
            latest_user_message = next(
                (
                    item["content"]
                    for item in reversed(self.store.list_messages(state.conversation_id))
                    if item["role"] == "user"
                ),
                "",
            )
            allowed_tools = tools_for_message(latest_user_message)
            retry_approval_grants: set[str] = set()

            async def prepare_tools(ctx, tool_definitions):
                del ctx
                return [
                    definition
                    for definition in tool_definitions
                    if definition.name in allowed_tools
                ]

            async def process_tool_call(ctx, call_tool, tool_name, tool_args):
                del ctx
                risk = classify_tool(tool_name)
                approval_key = approval_fingerprint(tool_name, tool_args)
                used_retry_grant = False
                if requires_approval(tool_name):
                    if approval_key in retry_approval_grants:
                        retry_approval_grants.remove(approval_key)
                        used_retry_grant = True
                        approved = True
                    else:
                        approval_id = str(uuid.uuid4())
                        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                        self.approvals[approval_id] = PendingApproval(
                            approval_id,
                            state.run_id,
                            future,
                        )
                        self.store.create_approval(
                            approval_id,
                            state.run_id,
                            tool_name,
                            tool_args,
                        )
                        await self.publish(state, {
                            "type": "CUSTOM",
                            "name": "approval_required",
                            "value": {
                                "approvalId": approval_id,
                                "runId": state.run_id,
                                "toolName": tool_name,
                                "arguments": tool_args,
                                "risk": risk,
                            },
                        })
                        try:
                            resolution = await asyncio.wait_for(future, timeout=120)
                            approved = resolution == "approved"
                        except TimeoutError:
                            self.approvals.pop(approval_id, None)
                            self.store.resolve_approval(approval_id, False)
                            resolution = "expired"
                            approved = False
                        await self.publish(state, {
                            "type": "CUSTOM",
                            "name": "approval_resolved",
                            "value": {
                                "approvalId": approval_id,
                                "approved": approved,
                                "resolution": resolution,
                            },
                        })
                    if not approved:
                        return {
                            "success": False,
                            "error": "user_denied: the user did not approve this action",
                        }
                    if not used_retry_grant:
                        retry_approval_grants.add(approval_key)
                try:
                    result = await call_tool(tool_name, tool_args, None)
                except Exception:
                    raise
                else:
                    retry_approval_grants.discard(approval_key)
                    return result

            environment = os.environ.copy()
            environment.update({
                "FL_MCP_MODE": "subprocess",
                "FL_MCP_SESSION_ID": state.session_id,
                "FL_MCP_WS_URL": self._ws_url(),
                "FL_MCP_CLIENT_ID": f"embedded-chat-{state.run_id}",
            })
            mcp_server = MCPServerStdio(
                sys.executable,
                [str(PROJECT_ROOT / "backend" / "mcp_server.py")],
                cwd=PROJECT_ROOT,
                env=environment,
                process_tool_call=process_tool_call,
                read_timeout=300,
            )
            agent = Agent(
                model,
                instructions=prompt,
                toolsets=[mcp_server],
                model_settings={"temperature": settings["temperature"]},
                prepare_tools=prepare_tools,
            )
            messages = [
                {
                    "id": item["id"],
                    "role": item["role"],
                    "content": item["content"],
                }
                for item in self.store.list_messages(state.conversation_id)
                if item["role"] in {"user", "assistant"}
            ]
            run_input = RunAgentInput.model_validate({
                "threadId": state.conversation_id,
                "runId": state.run_id,
                "state": {},
                "messages": messages,
                "tools": [],
                "context": [],
                "forwardedProps": {},
            })
            completed_result = None

            async def on_complete(result):
                nonlocal completed_result
                completed_result = result

            async for event in run_ag_ui(agent, run_input, on_complete=on_complete):
                await self.publish(state, event)

            serialized = None
            if completed_result is not None:
                serialized = json.loads(completed_result.all_messages_json())
            assistant_content, persisted_tool_steps = normalize_assistant_timeline(
                state.assistant_text,
                state.tool_steps,
            )
            self.store.append_message(
                state.conversation_id,
                "assistant",
                assistant_content,
                provider=settings["provider"],
                model=settings["model"],
                serialized=serialized,
                metadata={"toolSteps": persisted_tool_steps, "runId": state.run_id},
            )
            self.store.finish_run(state.run_id, "complete")
        except asyncio.CancelledError:
            self.store.finish_run(state.run_id, "cancelled")
            await self.publish(state, {
                "type": "RUN_ERROR",
                "message": "Response stopped.",
                "code": "cancelled",
            })
        except Exception as exc:
            logger.error("Embedded chat run failed: %s", exc, exc_info=True)
            self.store.finish_run(state.run_id, "error", str(exc))
            if not state.error_emitted:
                await self.publish(state, {
                    "type": "RUN_ERROR",
                    "message": str(exc),
                    "code": "chat_run_failed",
                })
        finally:
            state.done = True
            state.cancel_callback = None
            self._expire_approvals(state.run_id)
            for subscriber in list(state.subscribers):
                subscriber.put_nowait(None)

    async def _execute_claude_subscription(
        self,
        state: ActiveRun,
        settings: dict[str, Any],
    ) -> None:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            HookMatcher,
            PermissionResultAllow,
            PermissionResultDeny,
            ResultMessage,
            StreamEvent,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        cli_path = shutil.which("claude")
        if not cli_path:
            raise ValueError(
                "Claude Code is not installed or is not on PATH. "
                "Install Claude Code and run `claude auth login`."
            )

        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        claude_prompt = (
            f"{prompt}\n\n"
            "Claude Code integration rules:\n"
            "- Ren tools are MCP tools whose full names begin with `mcp__ren__`.\n"
            "- Invoke the actual MCP tools. Never print or simulate "
            "`<function_calls>`, `<invoke>`, or `<function_response>` markup.\n"
            "- Do not claim a tool succeeded unless its MCP result confirms it."
        )
        messages = self.store.list_messages(state.conversation_id)
        latest_user_message = next(
            (
                item["content"]
                for item in reversed(messages)
                if item["role"] == "user"
            ),
            "",
        )
        allowed_tools = tools_for_message(latest_user_message)
        claude_session_id = next(
            (
                str(item["metadata"]["claudeSessionId"])
                for item in reversed(messages)
                if item["role"] == "assistant"
                and item.get("metadata", {}).get("claudeSessionId")
            ),
            None,
        )
        environment = os.environ.copy()
        environment.update({
            "FL_MCP_MODE": "subprocess",
            "FL_MCP_SESSION_ID": state.session_id,
            "FL_MCP_WS_URL": self._ws_url(),
            "FL_MCP_CLIENT_ID": f"embedded-claude-{state.run_id}",
            "FL_MCP_ALLOWED_TOOLS": ",".join(sorted(allowed_tools)),
            "CLAUDE_AGENT_SDK_CLIENT_APP": "comfyui-fl-mcp/ren",
            # A configured Anthropic API key otherwise takes precedence over
            # the user's Claude Code subscription in non-interactive mode.
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_USE_BEDROCK": "",
            "CLAUDE_CODE_USE_VERTEX": "",
            "CLAUDE_CODE_USE_FOUNDRY": "",
        })

        async def keep_permission_stream_open(input_data, tool_use_id, context):
            del input_data, tool_use_id, context
            return {"continue_": True}

        async def can_use_tool(tool_name, input_data, context):
            del context
            short_name = claude_tool_name(tool_name)
            if short_name is None or short_name not in allowed_tools:
                return PermissionResultDeny(
                    message="Ren only allows the tools selected for this request.",
                )
            if not requires_approval(short_name):
                return PermissionResultAllow(updated_input=input_data)

            approval_id = str(uuid.uuid4())
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self.approvals[approval_id] = PendingApproval(
                approval_id,
                state.run_id,
                future,
            )
            self.store.create_approval(
                approval_id,
                state.run_id,
                short_name,
                input_data,
            )
            await self.publish(state, {
                "type": "CUSTOM",
                "name": "approval_required",
                "value": {
                    "approvalId": approval_id,
                    "runId": state.run_id,
                    "toolName": short_name,
                    "arguments": input_data,
                    "risk": classify_tool(short_name),
                },
            })
            try:
                resolution = await asyncio.wait_for(future, timeout=120)
                approved = resolution == "approved"
            except TimeoutError:
                self.approvals.pop(approval_id, None)
                self.store.resolve_approval(approval_id, False)
                resolution = "expired"
                approved = False
            await self.publish(state, {
                "type": "CUSTOM",
                "name": "approval_resolved",
                "value": {
                    "approvalId": approval_id,
                    "approved": approved,
                    "resolution": resolution,
                },
            })
            if approved:
                return PermissionResultAllow(updated_input=input_data)
            return PermissionResultDeny(
                message="user_denied: the user did not approve this action",
            )

        async def prompt_stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": latest_user_message,
                },
            }

        option_values: dict[str, Any] = {
            "tools": None,
            # Route every MCP permission decision through can_use_tool. Adding
            # safe tools to allowed_tools bypasses that callback in the SDK.
            "allowed_tools": [],
            "system_prompt": claude_prompt,
            "mcp_servers": {
                "ren": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [str(PROJECT_ROOT / "backend" / "mcp_server.py")],
                    "env": environment,
                }
            },
            "strict_mcp_config": True,
            "permission_mode": "default",
            "disallowed_tools": sorted(CLAUDE_BUILTIN_TOOLS),
            "model": settings["model"] or None,
            "cwd": PROJECT_ROOT,
            "cli_path": cli_path,
            "env": environment,
            "can_use_tool": can_use_tool,
            "hooks": {
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[keep_permission_stream_open])
                ]
            },
            "include_partial_messages": True,
            "setting_sources": [],
            "skills": [],
        }
        if claude_session_id:
            option_values["resume"] = claude_session_id
        else:
            try:
                uuid.UUID(state.conversation_id)
                option_values["session_id"] = state.conversation_id
            except ValueError:
                pass
        options = ClaudeAgentOptions(**option_values)

        block_tools: dict[int, str] = {}
        seen_tool_ids: set[str] = set()
        captured_session_id = claude_session_id
        result_message = None
        text_started = False

        await self.publish(state, {
            "type": "RUN_STARTED",
            "threadId": state.conversation_id,
            "runId": state.run_id,
        })
        client = None
        if self.claude_query_factory is not None:
            message_stream = self.claude_query_factory(
                prompt=prompt_stream(),
                options=options,
            )
        else:
            client_factory = self.claude_client_factory or ClaudeSDKClient
            client = client_factory(options)
            await client.connect()
            await wait_for_claude_mcp(client)
            session_id = captured_session_id or state.conversation_id
            await client.query(prompt_stream(), session_id=session_id)
            message_stream = client.receive_response()

        try:
            async for message in message_stream:
                message_session_id = getattr(message, "session_id", None)
                if message_session_id:
                    captured_session_id = str(message_session_id)

                if isinstance(message, StreamEvent):
                    event = message.event
                    event_type = event.get("type")
                    if event_type == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "text":
                            if not text_started:
                                text_started = True
                                await self.publish(state, {
                                    "type": "TEXT_MESSAGE_START",
                                    "messageId": state.run_id,
                                    "role": "assistant",
                                })
                        elif block.get("type") == "tool_use":
                            full_name = str(block.get("name") or "")
                            short_name = claude_tool_name(full_name)
                            tool_id = str(block.get("id") or uuid.uuid4())
                            if short_name:
                                seen_tool_ids.add(tool_id)
                                block_tools[int(event.get("index", -1))] = tool_id
                                await self.publish(state, {
                                    "type": "TOOL_CALL_START",
                                    "toolCallId": tool_id,
                                    "toolCallName": short_name,
                                })
                                initial_input = block.get("input")
                                if initial_input:
                                    await self.publish(state, {
                                        "type": "TOOL_CALL_ARGS",
                                        "toolCallId": tool_id,
                                        "delta": tool_result_content(initial_input),
                                    })
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            if not text_started:
                                text_started = True
                                await self.publish(state, {
                                    "type": "TEXT_MESSAGE_START",
                                    "messageId": state.run_id,
                                    "role": "assistant",
                                })
                            await self.publish(state, {
                                "type": "TEXT_MESSAGE_CONTENT",
                                "messageId": state.run_id,
                                "delta": str(delta["text"]),
                            })
                        elif delta.get("type") == "input_json_delta":
                            tool_id = block_tools.get(int(event.get("index", -1)))
                            partial = str(delta.get("partial_json") or "")
                            if tool_id and partial:
                                await self.publish(state, {
                                    "type": "TOOL_CALL_ARGS",
                                    "toolCallId": tool_id,
                                    "delta": partial,
                                })
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if (
                            isinstance(block, ToolUseBlock)
                            and block.id not in seen_tool_ids
                        ):
                            short_name = claude_tool_name(block.name)
                            if short_name:
                                seen_tool_ids.add(block.id)
                                await self.publish(state, {
                                    "type": "TOOL_CALL_START",
                                    "toolCallId": block.id,
                                    "toolCallName": short_name,
                                })
                                await self.publish(state, {
                                    "type": "TOOL_CALL_ARGS",
                                    "toolCallId": block.id,
                                    "delta": tool_result_content(block.input),
                                })
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if (
                            isinstance(block, ToolResultBlock)
                            and block.tool_use_id in seen_tool_ids
                        ):
                            await self.publish(state, {
                                "type": "TOOL_CALL_RESULT",
                                "toolCallId": block.tool_use_id,
                                "content": tool_result_content(block.content),
                            })
                elif isinstance(message, ResultMessage):
                    result_message = message
        finally:
            if client is not None:
                await asyncio.shield(client.disconnect())

        if result_message is None:
            raise RuntimeError("Claude Code ended without returning a result.")
        if result_message.is_error:
            details = result_message.errors or [result_message.subtype]
            raise RuntimeError("; ".join(str(item) for item in details if item))
        if not state.assistant_text and result_message.result:
            if not text_started:
                await self.publish(state, {
                    "type": "TEXT_MESSAGE_START",
                    "messageId": state.run_id,
                    "role": "assistant",
                })
            await self.publish(state, {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": state.run_id,
                "delta": result_message.result,
            })
        if text_started:
            await self.publish(state, {
                "type": "TEXT_MESSAGE_END",
                "messageId": state.run_id,
            })
        await self.publish(state, {
            "type": "RUN_FINISHED",
            "threadId": state.conversation_id,
            "runId": state.run_id,
        })

        assistant_content, persisted_tool_steps = normalize_assistant_timeline(
            state.assistant_text,
            state.tool_steps,
        )
        metadata = {
            "toolSteps": persisted_tool_steps,
            "runId": state.run_id,
            "claudeSessionId": captured_session_id,
            "usage": result_message.usage or {},
        }
        self.store.append_message(
            state.conversation_id,
            "assistant",
            assistant_content,
            provider=settings["provider"],
            model=settings["model"],
            metadata=metadata,
        )
        self.store.finish_run(state.run_id, "complete")

    async def _execute_codex_subscription(
        self,
        state: ActiveRun,
        settings: dict[str, Any],
    ) -> None:
        from openai_codex import AsyncCodex, AsyncThread, CodexConfig
        from openai_codex.generated.v2_all import (
            AgentMessageDeltaNotification,
            AgentMessageThreadItem,
            ApprovalsReviewer,
            AskForApproval,
            AskForApprovalValue,
            ConfigReadParams,
            ConfigReadResponse,
            ItemCompletedNotification,
            ItemStartedNotification,
            ListMcpServerStatusParams,
            ListMcpServerStatusResponse,
            McpToolCallThreadItem,
            SandboxMode,
            ThreadResumeParams,
            ThreadStartParams,
            ThreadTokenUsageUpdatedNotification,
            TurnCompletedNotification,
            TurnStatus,
        )

        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        codex_prompt = (
            f"{prompt}\n\n"
            "Codex integration rules:\n"
            "- Use only tools from the `ren` MCP server.\n"
            "- Invoke the actual Ren MCP tools; never simulate a tool call in text.\n"
            "- Do not use shell, file-editing, web, app, plugin, subagent, or other "
            "built-in tools.\n"
            "- Do not claim a tool succeeded unless its MCP result confirms it."
        )
        messages = self.store.list_messages(state.conversation_id)
        latest_user_message = next(
            (
                item["content"]
                for item in reversed(messages)
                if item["role"] == "user"
            ),
            "",
        )
        allowed_tools = tools_for_message(latest_user_message)
        codex_thread_id = next(
            (
                str(item["metadata"]["codexThreadId"])
                for item in reversed(messages)
                if item["role"] == "assistant"
                and item.get("metadata", {}).get("codexThreadId")
            ),
            None,
        )
        mcp_environment = {
            "FL_MCP_MODE": "subprocess",
            "FL_MCP_SESSION_ID": state.session_id,
            "FL_MCP_WS_URL": self._ws_url(),
            "FL_MCP_CLIENT_ID": f"embedded-codex-{state.run_id}",
            "FL_MCP_ALLOWED_TOOLS": ",".join(sorted(allowed_tools)),
        }
        ren_server = {
            "command": sys.executable,
            "args": [str(PROJECT_ROOT / "backend" / "mcp_server.py")],
            "cwd": str(PROJECT_ROOT),
            "env": mcp_environment,
            "required": True,
            "startup_timeout_sec": 15,
            "tool_timeout_sec": 300,
            "enabled_tools": sorted(allowed_tools),
            "default_tools_approval_mode": "approve",
            "tools": {
                name: {"approval_mode": "prompt"}
                for name in sorted(allowed_tools)
                if requires_approval(name)
            },
        }
        codex_environment = {
            # Explicit API keys otherwise take precedence over cached ChatGPT auth.
            "OPENAI_API_KEY": "",
            "CODEX_API_KEY": "",
        }
        config = CodexConfig(
            cwd=str(PROJECT_ROOT),
            env=codex_environment,
            client_name="comfyui_fl_mcp",
            client_title="ComfyUI FL-MCP Ren",
        )
        factory = self.codex_factory or AsyncCodex
        codex = factory(config)
        loop = asyncio.get_running_loop()

        async def request_approval(
            tool_name: str,
            arguments: dict[str, Any],
        ) -> bool:
            approval_id = str(uuid.uuid4())
            future: asyncio.Future[str] = loop.create_future()
            self.approvals[approval_id] = PendingApproval(
                approval_id,
                state.run_id,
                future,
            )
            self.store.create_approval(
                approval_id,
                state.run_id,
                tool_name,
                arguments,
            )
            await self.publish(state, {
                "type": "CUSTOM",
                "name": "approval_required",
                "value": {
                    "approvalId": approval_id,
                    "runId": state.run_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "risk": classify_tool(tool_name),
                },
            })
            try:
                resolution = await asyncio.wait_for(future, timeout=120)
                approved = resolution == "approved"
            except TimeoutError:
                self.approvals.pop(approval_id, None)
                self.store.resolve_approval(approval_id, False)
                resolution = "expired"
                approved = False
            await self.publish(state, {
                "type": "CUSTOM",
                "name": "approval_resolved",
                "value": {
                    "approvalId": approval_id,
                    "approved": approved,
                    "resolution": resolution,
                },
            })
            return approved

        def approval_handler(
            method: str,
            params: dict[str, Any] | None,
        ) -> dict[str, Any]:
            values = params or {}
            if method == "mcpServer/elicitation/request":
                metadata = values.get("_meta")
                is_tool_approval = (
                    isinstance(metadata, dict)
                    and metadata.get("codex_approval_kind") == "mcp_tool_call"
                )
                tool_name = codex_tool_name(values)
                arguments = (
                    metadata.get("tool_params")
                    if isinstance(metadata, dict)
                    and isinstance(metadata.get("tool_params"), dict)
                    else {}
                )
                if (
                    values.get("serverName") != "ren"
                    or not is_tool_approval
                    or tool_name not in allowed_tools
                    or not requires_approval(str(tool_name))
                ):
                    return {"action": "decline"}
                pending = asyncio.run_coroutine_threadsafe(
                    request_approval(str(tool_name), arguments),
                    loop,
                )
                try:
                    approved = pending.result(timeout=125)
                except (TimeoutError, FutureCancelledError):
                    pending.cancel()
                    approved = False
                return (
                    {"action": "accept", "content": {}}
                    if approved
                    else {"action": "decline"}
                )
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                return {"decision": "decline"}
            if method == "item/permissions/requestApproval":
                return {"permissions": {}}
            if method == "item/tool/call":
                return {
                    "success": False,
                    "contentItems": [{
                        "type": "inputText",
                        "text": "Only Ren MCP tools are available in embedded chat.",
                    }],
                }
            return {}

        install_codex_approval_handler(codex, approval_handler)
        await self.publish(state, {
            "type": "RUN_STARTED",
            "threadId": state.conversation_id,
            "runId": state.run_id,
        })

        usage: dict[str, Any] = {}
        seen_tool_ids: set[str] = set()
        completed_agent_text = ""
        completed_turn = None
        text_started = False
        entered = False
        try:
            await codex.__aenter__()
            entered = True
            account = await codex.account()
            account_value = getattr(account, "account", None)
            account_root = getattr(account_value, "root", account_value)
            if getattr(account_root, "type", None) != "chatgpt":
                raise ValueError(
                    "Codex is not signed in with a ChatGPT subscription. "
                    "Run `codex login`, then refresh the provider status."
                )

            config_params = ConfigReadParams(
                cwd=str(PROJECT_ROOT),
                include_layers=False,
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
            effective = await codex._client.request(
                "config/read",
                config_params,
                response_model=ConfigReadResponse,
            )
            effective_config = effective.config.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            isolated_mcp_servers = {
                name: {"enabled": False}
                for name in (effective_config.get("mcp_servers") or {})
                if name != "ren"
            }
            isolated_mcp_servers["ren"] = ren_server
            isolated_plugins = {
                name: {"enabled": False}
                for name in (effective_config.get("plugins") or {})
            }
            thread_config = {
                "features": {
                    "apps": False,
                    "goals": False,
                    "hooks": False,
                    "multi_agent": False,
                    "remote_plugin": False,
                    "shell_snapshot": False,
                    "shell_tool": False,
                    "unified_exec": False,
                },
                "web_search": "disabled",
                "mcp_servers": isolated_mcp_servers,
                "plugins": isolated_plugins,
            }
            approval_policy = AskForApproval(root=AskForApprovalValue.never)
            if codex_thread_id:
                resumed = await codex._client.thread_resume(
                    codex_thread_id,
                    ThreadResumeParams(
                        thread_id=codex_thread_id,
                        approval_policy=approval_policy,
                        approvals_reviewer=ApprovalsReviewer.user,
                        base_instructions=codex_prompt,
                        config=thread_config,
                        cwd=str(PROJECT_ROOT),
                        model=settings["model"],
                        sandbox=SandboxMode.read_only,
                    ),
                )
                thread = AsyncThread(codex, resumed.thread.id)
            else:
                started = await codex._client.thread_start(ThreadStartParams(
                    approval_policy=approval_policy,
                    approvals_reviewer=ApprovalsReviewer.user,
                    base_instructions=codex_prompt,
                    config=thread_config,
                    cwd=str(PROJECT_ROOT),
                    model=settings["model"],
                    sandbox=SandboxMode.read_only,
                    service_name="comfyui-fl-mcp/ren",
                ))
                thread = AsyncThread(codex, started.thread.id)
                codex_thread_id = thread.id

            status_params = ListMcpServerStatusParams(
                thread_id=thread.id,
                detail="full",
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
            server_status = await codex._client.request(
                "mcpServerStatus/list",
                status_params,
                response_model=ListMcpServerStatusResponse,
            )
            unexpected_servers = [
                item.name
                for item in server_status.data
                # This first-party picker can remain advertised by the host even
                # with apps/plugins disabled. Client-side dynamic tool calls are
                # denied by approval_handler above, so it is not executable.
                if item.name not in {"ren", "sites-design-picker"} and item.tools
            ]
            if unexpected_servers:
                raise RuntimeError(
                    "Codex tool isolation failed; unexpected MCP servers remained enabled."
                )

            turn = await thread.turn(
                latest_user_message,
                model=settings["model"],
                sandbox=None,
            )
            state.cancel_callback = turn.interrupt
            async for event in turn.stream():
                payload = event.payload
                if isinstance(payload, AgentMessageDeltaNotification):
                    if not text_started:
                        text_started = True
                        await self.publish(state, {
                            "type": "TEXT_MESSAGE_START",
                            "messageId": state.run_id,
                            "role": "assistant",
                        })
                    await self.publish(state, {
                        "type": "TEXT_MESSAGE_CONTENT",
                        "messageId": state.run_id,
                        "delta": payload.delta,
                    })
                elif isinstance(payload, ItemStartedNotification):
                    item = payload.item.root
                    if (
                        isinstance(item, McpToolCallThreadItem)
                        and item.server == "ren"
                        and item.tool in allowed_tools
                    ):
                        seen_tool_ids.add(item.id)
                        await self.publish(state, {
                            "type": "TOOL_CALL_START",
                            "toolCallId": item.id,
                            "toolCallName": item.tool,
                        })
                        await self.publish(state, {
                            "type": "TOOL_CALL_ARGS",
                            "toolCallId": item.id,
                            "delta": tool_result_content(item.arguments),
                        })
                elif isinstance(payload, ItemCompletedNotification):
                    item = payload.item.root
                    if isinstance(item, McpToolCallThreadItem) and item.server == "ren":
                        if item.id not in seen_tool_ids:
                            seen_tool_ids.add(item.id)
                            await self.publish(state, {
                                "type": "TOOL_CALL_START",
                                "toolCallId": item.id,
                                "toolCallName": item.tool,
                            })
                            await self.publish(state, {
                                "type": "TOOL_CALL_ARGS",
                                "toolCallId": item.id,
                                "delta": tool_result_content(item.arguments),
                            })
                        if item.error is not None:
                            result_content = {"error": item.error.message}
                        elif item.result is not None:
                            result_content = item.result
                        else:
                            result_content = {"status": item.status.value}
                        await self.publish(state, {
                            "type": "TOOL_CALL_RESULT",
                            "toolCallId": item.id,
                            "content": tool_result_content(result_content),
                        })
                    elif isinstance(item, AgentMessageThreadItem):
                        completed_agent_text = item.text
                elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                    usage = payload.token_usage.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                elif isinstance(payload, TurnCompletedNotification):
                    completed_turn = payload.turn
            state.cancel_callback = None
        finally:
            state.cancel_callback = None
            if entered:
                await asyncio.shield(codex.close())

        if completed_turn is None:
            raise RuntimeError("Codex ended without returning a completed turn.")
        if completed_turn.status == TurnStatus.failed:
            detail = (
                completed_turn.error.message
                if completed_turn.error is not None
                else "Codex turn failed."
            )
            raise RuntimeError(detail)
        if not state.assistant_text and completed_agent_text:
            if not text_started:
                text_started = True
                await self.publish(state, {
                    "type": "TEXT_MESSAGE_START",
                    "messageId": state.run_id,
                    "role": "assistant",
                })
            await self.publish(state, {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": state.run_id,
                "delta": completed_agent_text,
            })
        if text_started:
            await self.publish(state, {
                "type": "TEXT_MESSAGE_END",
                "messageId": state.run_id,
            })
        await self.publish(state, {
            "type": "RUN_FINISHED",
            "threadId": state.conversation_id,
            "runId": state.run_id,
        })

        assistant_content, persisted_tool_steps = normalize_assistant_timeline(
            state.assistant_text,
            state.tool_steps,
        )
        self.store.append_message(
            state.conversation_id,
            "assistant",
            assistant_content,
            provider=settings["provider"],
            model=settings["model"],
            metadata={
                "toolSteps": persisted_tool_steps,
                "runId": state.run_id,
                "codexThreadId": codex_thread_id,
                "usage": usage,
            },
        )
        self.store.finish_run(state.run_id, "complete")

    @staticmethod
    def _build_model(settings: dict[str, Any]):
        provider_id = settings["provider"]
        credential = credential_store.get(provider_id)
        if provider_id == "anthropic":
            if not credential:
                raise ValueError("Anthropic API key is not configured.")
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(
                settings["model"],
                provider=AnthropicProvider(api_key=credential),
            )

        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider

        requires_key = provider_id in {"openai", "openrouter"}
        if requires_key and not credential:
            raise ValueError(f"{provider_id.title()} API key is not configured.")
        return OpenAIModel(
            settings["model"],
            provider=OpenAIProvider(
                base_url=settings["base_url"],
                api_key=credential or "local",
            ),
        )

    @staticmethod
    def _ws_url() -> str:
        host = bridge_settings.ws_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = bridge_settings.ws_port
        return f"ws://{host}:{port}/ws"


chat_runtime = ChatRuntime()
