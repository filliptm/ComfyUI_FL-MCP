"""FastAPI routes for the embedded FL-MCP Assistant."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from chat_config import PROVIDER_PRESETS, chat_settings, credential_store
from chat_runtime import chat_runtime, normalize_approval_decision
from chat_store import chat_store
from claude_subscription import claude_subscription
from codex_subscription import codex_subscription
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from manager import manager

router = APIRouter(prefix="/api/chat", tags=["chat"])


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
    return value


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
) -> dict[str, Any]:
    return {
        "conversations": chat_store.list_conversations(limit, view),
        "view": view,
    }


@router.post("/conversations")
async def create_conversation(request: Request) -> JSONResponse:
    data = await request.json()
    settings = chat_settings.load()
    conversation = chat_store.create_conversation(
        title=str(data.get("title") or "New chat")[:120],
        provider=settings["provider"],
        model=settings["model"],
    )
    return JSONResponse({"conversation": conversation}, status_code=201)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    conversation = chat_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {
        "conversation": conversation,
        "messages": chat_store.list_messages(conversation_id),
    }


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    data = await request.json()
    allowed = {"title", "archived"}
    unknown = set(data) - allowed
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported updates: {', '.join(sorted(unknown))}",
        )
    conversation = chat_store.update_conversation(
        conversation_id,
        title=str(data["title"])[:120] if "title" in data else None,
        archived=bool(data["archived"]) if "archived" in data else None,
    )
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


@router.post("/runs")
async def start_run(request: Request) -> StreamingResponse:
    data = await request.json()
    session_id = str(data.get("sessionId") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="sessionId is required.")
    try:
        state = await chat_runtime.start(
            session_id=session_id,
            conversation_id=(
                str(data["conversationId"]) if data.get("conversationId") else None
            ),
            message=str(data.get("message") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        chat_runtime.subscribe(state.run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-FL-MCP-Run-Id": state.run_id,
            "X-FL-MCP-Conversation-Id": state.conversation_id,
        },
    )


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
async def cancel_run(run_id: str) -> dict[str, bool]:
    if not await chat_runtime.cancel(run_id):
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
