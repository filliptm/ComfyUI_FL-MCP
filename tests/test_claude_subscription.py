import json

import claude_subscription
import pytest
from claude_subscription import ClaudeSubscriptionService


class FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


@pytest.mark.asyncio
async def test_status_uses_only_non_identity_claude_auth_fields(monkeypatch):
    service = ClaudeSubscriptionService()
    monkeypatch.setattr(service, "cli_path", lambda: "/usr/local/bin/claude")

    async def fake_version(_cli):
        return "2.1.170 (Claude Code)"

    async def fake_subprocess(*args, **kwargs):
        del kwargs
        assert args[-2:] == ("auth", "status")
        return FakeProcess(json.dumps({
            "loggedIn": True,
            "authMethod": "claude.ai",
            "subscriptionType": "max",
            "email": "must-not-leak@example.com",
            "orgId": "must-not-leak",
        }).encode())

    monkeypatch.setattr(service, "_version", fake_version)
    monkeypatch.setattr(
        claude_subscription.asyncio,
        "create_subprocess_exec",
        fake_subprocess,
    )

    status = await service.status(refresh=True)

    assert status["configured"] is True
    assert status["source"] == "claude_cli"
    assert status["subscriptionType"] == "max"
    assert "email" not in status
    assert "orgId" not in status


@pytest.mark.asyncio
async def test_status_reports_missing_cli_without_running_a_process(monkeypatch):
    service = ClaudeSubscriptionService()
    monkeypatch.setattr(service, "cli_path", lambda: None)

    status = await service.status(refresh=True)

    assert status["configured"] is False
    assert status["installed"] is False
    assert "not installed" in status["message"]
