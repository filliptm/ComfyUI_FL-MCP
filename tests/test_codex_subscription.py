import json

import codex_subscription
import pytest
from codex_subscription import CodexSubscriptionService


class FakeProcess:
    def __init__(
        self,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
    ):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_status_accepts_chatgpt_login_without_exposing_identity(monkeypatch):
    service = CodexSubscriptionService()
    monkeypatch.setattr(service, "cli_path", lambda: "/usr/local/bin/codex")

    async def fake_version(_cli):
        return "codex-cli 0.145.0"

    async def fake_subprocess(*args, **kwargs):
        del kwargs
        assert args[-2:] == ("login", "status")
        return FakeProcess(
            b"",
            b"Logged in using ChatGPT as must-not-leak@example.com",
        )

    monkeypatch.setattr(service, "_version", fake_version)
    monkeypatch.setattr(
        codex_subscription.asyncio,
        "create_subprocess_exec",
        fake_subprocess,
    )

    status = await service.status(refresh=True)

    assert status["configured"] is True
    assert status["source"] == "codex_cli"
    assert status["authMethod"] == "chatgpt"
    assert status["subscriptionType"] == "chatgpt"
    assert "email" not in status
    assert "must-not-leak" not in str(status)


@pytest.mark.asyncio
async def test_status_rejects_api_key_login_as_subscription(monkeypatch):
    service = CodexSubscriptionService()
    monkeypatch.setattr(service, "cli_path", lambda: "/usr/local/bin/codex")

    async def fake_version(_cli):
        return "codex-cli 0.145.0"

    async def fake_subprocess(*_args, **_kwargs):
        return FakeProcess(b"Logged in using an API key")

    monkeypatch.setattr(service, "_version", fake_version)
    monkeypatch.setattr(
        codex_subscription.asyncio,
        "create_subprocess_exec",
        fake_subprocess,
    )

    status = await service.status(refresh=True)

    assert status["configured"] is False
    assert status["authenticated"] is True
    assert status["authMethod"] == "api_key"
    assert "not a ChatGPT subscription" in status["message"]


@pytest.mark.asyncio
async def test_status_reports_missing_codex_cli(monkeypatch):
    service = CodexSubscriptionService()
    monkeypatch.setattr(service, "cli_path", lambda: None)

    status = await service.status(refresh=True)

    assert status["configured"] is False
    assert status["installed"] is False
    assert "not installed" in status["message"]


@pytest.mark.asyncio
async def test_models_returns_every_visible_cli_catalog_entry(monkeypatch):
    service = CodexSubscriptionService()
    monkeypatch.setattr(service, "cli_path", lambda: "/usr/local/bin/codex")
    payload = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "description": "Frontier model",
                "visibility": "list",
            },
            {
                "slug": "codex-auto-review",
                "display_name": "GPT-5.6-Terra",
                "visibility": "list",
            },
            {
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6-Terra",
                "visibility": "list",
            },
            {
                "slug": "internal-model",
                "display_name": "Internal",
                "visibility": "hidden",
            },
        ]
    }

    async def fake_subprocess(*args, **kwargs):
        del kwargs
        assert args[-2:] == ("debug", "models")
        return FakeProcess(json.dumps(payload).encode())

    monkeypatch.setattr(
        codex_subscription.asyncio,
        "create_subprocess_exec",
        fake_subprocess,
    )

    models = await service.models(refresh=True)

    assert [item["id"] for item in models] == [
        "gpt-5.6-sol",
        "codex-auto-review",
        "gpt-5.6-terra",
    ]
    assert models[0]["description"] == "Frontier model"
    assert models[1]["label"] == "GPT-5.6-Terra · Auto Review"
    assert models[2]["label"] == "GPT-5.6-Terra · Standard"
