import chat_routes
import server
from chat_config import ChatSettingsStore, CredentialStore
from chat_store import ChatStore
from fastapi.testclient import TestClient


def test_chat_settings_and_conversation_crud_use_http_api(tmp_path, monkeypatch):
    settings = ChatSettingsStore(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")
    monkeypatch.setattr(chat_routes, "chat_settings", settings)
    monkeypatch.setattr(chat_routes, "chat_store", store)
    monkeypatch.setattr(chat_routes, "credential_store", CredentialStore())

    with TestClient(server.app) as client:
        response = client.patch("/api/chat/settings", json={
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1/",
            "model": "qwen3",
        })
        assert response.status_code == 200
        assert response.json()["base_url"] == "http://127.0.0.1:11434/v1"

        created = client.post("/api/chat/conversations", json={})
        assert created.status_code == 201
        conversation_id = created.json()["conversation"]["id"]

        loaded = client.get(f"/api/chat/conversations/{conversation_id}")
        assert loaded.status_code == 200
        assert loaded.json()["messages"] == []

        renamed = client.patch(
            f"/api/chat/conversations/{conversation_id}",
            json={"title": "Workflow review"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["conversation"]["title"] == "Workflow review"

        active = client.get("/api/chat/conversations?view=active")
        assert [item["id"] for item in active.json()["conversations"]] == [
            conversation_id
        ]
        assert client.get("/api/chat/conversations?view=archived").json()[
            "conversations"
        ] == []

        active_delete = client.delete(f"/api/chat/conversations/{conversation_id}")
        assert active_delete.status_code == 409

        archived = client.patch(
            f"/api/chat/conversations/{conversation_id}",
            json={"archived": True},
        )
        assert archived.status_code == 200
        assert archived.json()["conversation"]["archivedAt"]
        assert client.get("/api/chat/conversations?view=active").json()[
            "conversations"
        ] == []
        assert [item["id"] for item in client.get(
            "/api/chat/conversations?view=archived"
        ).json()["conversations"]] == [conversation_id]

        deleted = client.delete(f"/api/chat/conversations/{conversation_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/chat/conversations/{conversation_id}").status_code == 404


def test_chat_settings_reject_secret_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_routes,
        "chat_settings",
        ChatSettingsStore(tmp_path / "settings.json"),
    )

    with TestClient(server.app) as client:
        response = client.patch("/api/chat/settings", json={"api_key": "not-allowed"})

    assert response.status_code == 400
    assert "credential endpoint" in response.json()["detail"]


def test_claude_subscription_status_and_models_use_cli_auth(tmp_path, monkeypatch):
    settings = ChatSettingsStore(tmp_path / "settings.json")
    settings.update({"provider": "claude_subscription", "model": "sonnet"})
    monkeypatch.setattr(chat_routes, "chat_settings", settings)

    async def fake_status(*, refresh=False):
        return {
            "configured": True,
            "source": "claude_cli",
            "installed": True,
            "authenticated": True,
            "authMethod": "claude.ai",
            "subscriptionType": "max",
            "version": "test",
            "message": "Claude Code is signed in with a Max subscription.",
            "refreshed": refresh,
        }

    monkeypatch.setattr(chat_routes.claude_subscription, "status", fake_status)

    with TestClient(server.app) as client:
        status = client.get("/api/chat/status").json()
        models = client.get("/api/chat/models").json()
        refreshed = client.post("/api/chat/claude/refresh", json={}).json()

    assert status["configured"] is True
    assert status["credential"]["source"] == "claude_cli"
    assert models["source"] == "claude_cli"
    assert [item["id"] for item in models["models"]] == [
        "default",
        "best",
        "fable",
        "sonnet",
        "opus",
        "haiku",
        "sonnet[1m]",
        "opus[1m]",
        "opusplan",
    ]
    assert models["catalog"] == "claude_code_aliases"
    assert refreshed["refreshed"] is True


def test_codex_subscription_status_and_models_use_chatgpt_auth(tmp_path, monkeypatch):
    settings = ChatSettingsStore(tmp_path / "settings.json")
    settings.update({
        "provider": "codex_subscription",
        "model": "gpt-5.6-sol",
    })
    monkeypatch.setattr(chat_routes, "chat_settings", settings)

    async def fake_status(*, refresh=False):
        return {
            "configured": True,
            "source": "codex_cli",
            "installed": True,
            "authenticated": True,
            "authMethod": "chatgpt",
            "subscriptionType": "chatgpt",
            "version": "codex-cli test",
            "message": "Codex is signed in with a ChatGPT subscription.",
            "refreshed": refresh,
        }

    monkeypatch.setattr(chat_routes.codex_subscription, "status", fake_status)

    async def fake_models(*, refresh=False):
        assert refresh is False
        return [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol"},
            {
                "id": "codex-auto-review",
                "label": "GPT-5.6-Terra · Auto Review",
            },
        ]

    monkeypatch.setattr(chat_routes.codex_subscription, "models", fake_models)

    with TestClient(server.app) as client:
        status = client.get("/api/chat/status").json()
        models = client.get("/api/chat/models").json()
        refreshed = client.post("/api/chat/codex/refresh", json={}).json()

    assert status["configured"] is True
    assert status["credential"]["source"] == "codex_cli"
    assert models["source"] == "codex_cli"
    assert [item["id"] for item in models["models"]] == [
        "gpt-5.6-sol",
        "codex-auto-review",
    ]
    assert models["catalog"] == "installed_cli"
    assert refreshed["refreshed"] is True
