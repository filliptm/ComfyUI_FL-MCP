import json

import pytest
from chat_config import PROVIDER_PRESETS, ChatSettingsStore, CredentialStore


def test_settings_store_contains_no_credentials(tmp_path):
    path = tmp_path / "settings.json"
    store = ChatSettingsStore(path)
    value = store.update({
        "provider": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1/",
        "model": "local-model",
    })
    assert value["base_url"] == "http://127.0.0.1:1234/v1"
    saved = json.loads(path.read_text())
    assert all("key" not in key.lower() for key in saved)


def test_settings_reject_secret_fields(tmp_path):
    store = ChatSettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError, match="credential"):
        store.update({"api_key": "secret"})


def test_approval_preferences_persist_and_validate(tmp_path):
    store = ChatSettingsStore(tmp_path / "settings.json")
    value = store.update({
        "approval_mode": "bypass_all",
        "always_allowed_tools": [
            "queue_workflow",
            "workflow_delete_file",
            "queue_workflow",
        ],
    })

    assert value["approval_mode"] == "bypass_all"
    assert value["always_allowed_tools"] == [
        "queue_workflow",
        "workflow_delete_file",
    ]
    assert store.load()["approval_mode"] == "bypass_all"

    with pytest.raises(ValueError, match="approval mode"):
        store.update({"approval_mode": "unsafe_unknown_mode"})
    with pytest.raises(ValueError, match="invalid tool name"):
        store.update({"always_allowed_tools": ["bad tool name"]})


def test_always_allow_tool_adds_one_persistent_rule(tmp_path):
    store = ChatSettingsStore(tmp_path / "settings.json")

    store.always_allow_tool("queue_workflow")
    store.always_allow_tool("queue_workflow")

    assert store.load()["always_allowed_tools"] == ["queue_workflow"]


def test_claude_subscription_is_separate_from_anthropic_api():
    subscription = PROVIDER_PRESETS["claude_subscription"]
    anthropic = PROVIDER_PRESETS["anthropic"]

    assert subscription["type"] == "claude_cli"
    assert subscription["requires_key"] is False
    assert subscription["default_model"] == "sonnet"
    assert [item["id"] for item in subscription["models"]] == [
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
    assert anthropic["type"] == "anthropic"
    assert anthropic["requires_key"] is True


def test_claude_subscription_rejects_manually_stored_credentials():
    with pytest.raises(ValueError, match="managed by Claude Code"):
        CredentialStore().set("claude_subscription", "oauth-token")


def test_codex_subscription_is_separate_from_openai_api():
    subscription = PROVIDER_PRESETS["codex_subscription"]
    openai = PROVIDER_PRESETS["openai"]

    assert subscription["type"] == "codex_cli"
    assert subscription["requires_key"] is False
    assert subscription["default_model"] == "gpt-5.6-sol"
    assert [item["id"] for item in subscription["models"]] == [
        "gpt-5.6-sol",
        "codex-auto-review",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
    ]
    assert openai["type"] == "openai_compatible"
    assert openai["requires_key"] is True


def test_codex_subscription_rejects_manually_stored_credentials():
    with pytest.raises(ValueError, match="managed by Codex"):
        CredentialStore().set("codex_subscription", "oauth-token")
