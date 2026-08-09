"""FastAPI routes for the embedded FL-MCP Assistant."""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx
from chat_config import (
    PROVIDER_PRESETS,
    REASONING_EFFORTS,
    SEARCH_MODES,
    chat_settings,
    credential_store,
)
from chat_runtime import chat_runtime, normalize_approval_decision
from chat_store import chat_store
from claude_subscription import claude_subscription
from codex_subscription import codex_subscription
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from manager import manager
from web_fetcher import WebFetchError
from web_image_service import WebImagePreviewError, WebImagePreviewService
from web_security import WebUrlError

router = APIRouter(prefix="/api/chat", tags=["chat"])
web_image_previews = WebImagePreviewService(max_dimension=192)


async def _connection_status(provider: str, *, refresh: bool = False) -> dict[str, Any]:
    provider_type = PROVIDER_PRESETS[provider]["type"]
    if provider_type == "claude_cli":
        return await claude_subscription.status(refresh=refresh)
    if provider_type == "codex_cli":
        return await codex_subscription.status(refresh=refresh)
    return credential_store.status(provider)


@router.get("/status")
async def chat_status(session_id: str | None = Query(default=None)) -> dict[str, Any]:
    available, error = chat_runtime.available()
    settings = chat_settings.load()
    provider = settings["provider"]
    credential = await _connection_status(provider)
    preset = PROVIDER_PRESETS[provider]
    configured = bool(settings["model"]) and (
        credential["configured"]
        if preset["type"] in {"claude_cli", "codex_cli"}
        else (not preset["requires_key"] or credential["configured"])
    )
    return {
        "available": available,
        "error": error,
        "configured": configured,
        "provider": provider,
        "model": settings["model"],
        "bridgeConnected": bool(
            session_id and manager.has_connection(session_id, "frontend")
        ),
        "credential": credential,
    }


@router.get("/settings")
async def get_chat_settings() -> dict[str, Any]:
    settings = chat_settings.public()
    settings["credential"] = await _connection_status(settings["provider"])
    settings["searchCredential"] = credential_store.status("tavily")
    return settings


@router.patch("/settings")
async def update_chat_settings(request: Request) -> dict[str, Any]:
    try:
        value = chat_settings.update(await request.json())
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    value["resolvedApprovals"] = chat_runtime.sync_approval_settings(value)
    value["presets"] = PROVIDER_PRESETS
    value["credential"] = await _connection_status(value["provider"])
    value["searchCredential"] = credential_store.status("tavily")
    return value


@router.get("/web-images/preview")
async def web_image_preview(
    url: str = Query(min_length=1, max_length=2048),
) -> Response:
    """Return a safe, bounded local preview for one public raster image."""

    try:
        preview = await web_image_previews.preview(url)
    except (WebUrlError, WebImagePreviewError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WebFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=preview.content,
        media_type=preview.media_type,
        headers={
            "Cache-Control": "private, max-age=1800",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "X-FL-MCP-Original-Size": (
                f"{preview.original_size[0]}x{preview.original_size[1]}"
            ),
        },
    )


@router.get("/models")
async def list_provider_models() -> dict[str, Any]:
    settings = chat_settings.load()
    provider = settings["provider"]
    provider_type = PROVIDER_PRESETS[provider]["type"]
    if provider_type == "claude_cli":
        return {
            "models": PROVIDER_PRESETS[provider]["models"],
            "source": provider_type,
            "catalog": "claude_code_aliases",
        }
    if provider_type == "codex_cli":
        discovered = await codex_subscription.models()
        return {
            "models": discovered or PROVIDER_PRESETS[provider]["models"],
            "source": provider_type,
            "catalog": "installed_cli" if discovered else "bundled_fallback",
        }
    if provider_type == "anthropic":
        model = settings["model"] or PROVIDER_PRESETS[provider]["default_model"]
        return {"models": [{"id": model, "label": model}], "source": "configured"}
    headers = {}
    credential = credential_store.get(provider)
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{settings['base_url']}/models", headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not load models from {settings['base_url']}: {exc}",
        ) from exc
    models = []
    for item in payload.get("data", []):
        if isinstance(item, dict) and item.get("id"):
            model_id = str(item["id"])
            models.append({"id": model_id, "label": model_id})
    return {"models": models, "source": settings["base_url"]}


@router.put("/credentials/{provider}")
async def set_provider_credential(provider: str, request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return credential_store.set(provider, str(data.get("credential") or ""))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/credentials/{provider}")
async def clear_provider_credential(provider: str) -> dict[str, bool]:
    credential_store.clear(provider)
    return {"cleared": True}


@router.post("/claude/login")
async def launch_claude_login() -> dict[str, Any]:
    try:
        return claude_subscription.launch_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/claude/refresh")
async def refresh_claude_status() -> dict[str, Any]:
    return await claude_subscription.status(refresh=True)


@router.post("/codex/login")
async def launch_codex_login() -> dict[str, Any]:
    try:
        return codex_subscription.launch_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/codex/refresh")
async def refresh_codex_status() -> dict[str, Any]:
    return await codex_subscription.status(refresh=True)


@router.get("/conversations")
async def list_conversations(
    limit: int = 100,
    view: Literal["active", "archived"] = "active",
    workflow_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return {
        "conversations": chat_store.list_conversations(limit, view, workflow_id),
        "view": view,
    }


def _workflow_context(value: Any) -> dict[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("workflow must be an object.")
    workflow_id = str(value.get("id") or "").replace("\r", " ").replace("\n", " ").replace("`", "'").strip()
    if not workflow_id:
        raise ValueError("workflow.id is required.")
    if len(workflow_id) > 200:
        raise ValueError("workflow.id is too long.")
    workflow_name = str(value.get("name") or "").replace("\r", " ").replace("\n", " ").replace("`", "'").strip()
    workflow_path = str(value.get("path") or "").replace("\r", " ").replace("\n", " ").replace("`", "'").strip()
    return {
        "id": workflow_id,
        "path": workflow_path[:1000] or None,
        "name": workflow_name[:200] or "Workflow",
    }


@router.post("/conversations")
async def create_conversation(request: Request) -> JSONResponse:
    data = await request.json()
    settings = chat_settings.load()
    try:
        workflow = _workflow_context(data.get("workflow"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conversation = chat_store.create_conversation(
        title=str(data.get("title") or "New chat")[:120],
        provider=settings["provider"],
        model=settings["model"],
        workflow_id=workflow["id"] if workflow else None,
        workflow_path=workflow["path"] if workflow else None,
        workflow_name=workflow["name"] if workflow else None,
    )
    return JSONResponse({"conversation": conversation}, status_code=201)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    before: str | None = Query(default=None),
) -> dict[str, Any]:
    conversation = chat_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        page = chat_store.list_messages_page(
            conversation_id,
            limit=limit,
            before_message_id=before,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "conversation": conversation,
        **page,
    }


@router.post("/conversations/{conversation_id}/messages/{message_id}/version")
async def select_message_version(
    conversation_id: str,
    message_id: str,
    request: Request,
) -> dict[str, Any]:
    if not chat_store.get_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    data = await request.json()
    try:
        direction = int(data.get("direction") or 0)
        messages = chat_store.select_message_version(
            conversation_id,
            message_id,
            direction,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"messages": messages}


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    data = await request.json()
    allowed = {"title", "archived", "workflow"}
    unknown = set(data) - allowed
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported updates: {', '.join(sorted(unknown))}",
        )
    try:
        workflow = _workflow_context(data.get("workflow")) if "workflow" in data else None
        if workflow:
            conversation = chat_store.bind_conversation(
                conversation_id,
                workflow["id"],
                workflow["path"],
                workflow["name"],
            )
        else:
            conversation = chat_store.get_conversation(conversation_id)
        if conversation and ({"title", "archived"} & set(data)):
            conversation = chat_store.update_conversation(
                conversation_id,
                title=str(data["title"])[:120] if "title" in data else None,
                archived=bool(data["archived"]) if "archived" in data else None,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"conversation": conversation}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, bool]:
    conversation = chat_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if not conversation["archivedAt"]:
        raise HTTPException(
            status_code=409,
            detail="Archive the conversation before deleting it permanently.",
        )
    if not chat_store.delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": True}


def _run_request_values(data: dict[str, Any]) -> dict[str, Any]:
    session_id = str(data.get("sessionId") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="sessionId is required.")
    reasoning_effort = str(
        data.get("reasoningEffort") or "default"
    ).strip().lower()
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"Unsupported reasoning effort: {reasoning_effort}")
    search_mode = str(
        data.get("searchMode") or chat_settings.load().get("search_mode") or "free"
    ).strip().lower()
    if search_mode not in SEARCH_MODES:
        raise ValueError(f"Unsupported web search mode: {search_mode}")
    workflow = _workflow_context(data.get("workflow"))
    return {
        "session_id": session_id,
        "message": str(data.get("message") or ""),
        "reasoning_effort": reasoning_effort,
        "search_mode": search_mode,
        "attachments": data.get("attachments"),
        "workflow": workflow,
    }


def _run_stream(state: Any) -> StreamingResponse:
    user_message_id = getattr(state, "user_message_id", None) or ""
    user_message_revision = getattr(state, "user_message_revision", None) or {}
    return StreamingResponse(
        chat_runtime.subscribe(state.run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-FL-MCP-Run-Id": state.run_id,
            "X-FL-MCP-Conversation-Id": state.conversation_id,
            "X-FL-MCP-User-Message-Id": user_message_id,
            "X-FL-MCP-User-Revision-Root-Id": (
                str(user_message_revision.get("rootId") or "")
            ),
            "X-FL-MCP-User-Revision-Index": (
                str(user_message_revision.get("index") or 1)
            ),
            "X-FL-MCP-User-Revision-Count": (
                str(user_message_revision.get("count") or 1)
            ),
        },
    )


@router.post("/runs")
async def start_run(request: Request) -> StreamingResponse:
    data = await request.json()
    try:
        values = _run_request_values(data)
        state = await chat_runtime.start(
            **values,
            conversation_id=(
                str(data["conversationId"]) if data.get("conversationId") else None
            ),
            edit_message_id=(
                str(data["editMessageId"]) if data.get("editMessageId") else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_stream(state)


@router.post("/runs/{run_id}/steer")
async def steer_run(run_id: str, request: Request) -> StreamingResponse:
    try:
        state = await chat_runtime.steer(
            run_id,
            **_run_request_values(await request.json()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_stream(state)


@router.get("/runs/{run_id}/stream")
async def attach_run(run_id: str) -> StreamingResponse:
    if run_id not in chat_runtime.runs:
        raise HTTPException(status_code=404, detail="Run not found.")
    return StreamingResponse(
        chat_runtime.subscribe(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, bool]:
    body = await request.body()
    data = json.loads(body) if body else {}
    reason = str(data.get("reason") or "stopped")
    if reason not in {"stopped", "workflow_switched"}:
        raise HTTPException(status_code=400, detail="Unsupported cancellation reason.")
    if not await chat_runtime.cancel(run_id, reason=reason):
        raise HTTPException(status_code=404, detail="Active run not found.")
    return {"cancelled": True}


@router.post("/approvals/{approval_id}")
async def resolve_approval(approval_id: str, request: Request) -> dict[str, Any]:
    data = await request.json()
    decision = data.get("decision")
    if decision is None and "approved" in data:
        decision = bool(data["approved"])
    if decision is None:
        raise HTTPException(status_code=400, detail="decision is required.")
    try:
        resolution = normalize_approval_decision(decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not await chat_runtime.resolve_approval(approval_id, decision):
        raise HTTPException(status_code=404, detail="Pending approval not found.")
    return {
        "resolved": True,
        "approved": resolution in {"approved", "always_allowed"},
        "resolution": resolution,
    }
