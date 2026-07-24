"""Non-secret chat settings and secure provider credentials."""

from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FL_MCP_DATA_DIR", PROJECT_ROOT / ".fl_mcp"))
SETTINGS_PATH = DATA_DIR / "chat_settings.json"
KEYRING_SERVICE = "comfyui-fl-mcp"
APPROVAL_MODES = {"autonomous_edits", "bypass_all"}
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "lmstudio": {
        "label": "LM Studio",
        "type": "openai_compatible",
        "base_url": "http://127.0.0.1:1234/v1",
        "requires_key": False,
        "default_model": "",
    },
    "ollama": {
        "label": "Ollama",
        "type": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "requires_key": False,
        "default_model": "",
    },
    "openai": {
        "label": "OpenAI",
        "type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "requires_key": True,
        "default_model": "gpt-5-mini",
    },
    "openrouter": {
        "label": "OpenRouter",
        "type": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "requires_key": True,
        "default_model": "openai/gpt-5-mini",
    },
    "anthropic": {
        "label": "Anthropic",
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "requires_key": True,
        "default_model": "claude-sonnet-4-5",
    },
    "claude_subscription": {
        "label": "Claude subscription",
        "type": "claude_cli",
        "base_url": "",
        "requires_key": False,
        "default_model": "sonnet",
        "models": [
            {"id": "default", "label": "Account default"},
            {"id": "best", "label": "Best available (Fable or Opus)"},
            {"id": "fable", "label": "Claude Fable 5"},
            {"id": "sonnet", "label": "Claude Sonnet · Daily coding"},
            {"id": "opus", "label": "Claude Opus · Complex reasoning"},
            {"id": "haiku", "label": "Claude Haiku · Fast and efficient"},
            {"id": "sonnet[1m]", "label": "Claude Sonnet · 1M context"},
            {"id": "opus[1m]", "label": "Claude Opus · 1M context"},
            {
                "id": "opusplan",
                "label": "Claude Opus planning · Sonnet execution",
            },
        ],
    },
    "codex_subscription": {
        "label": "Codex subscription",
        "type": "codex_cli",
        "base_url": "",
        "requires_key": False,
        "default_model": "gpt-5.6-sol",
        "models": [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol (recommended)"},
            {
                "id": "codex-auto-review",
                "label": "GPT-5.6-Terra · Auto Review",
            },
            {"id": "gpt-5.6-terra", "label": "GPT-5.6-Terra · Standard"},
            {"id": "gpt-5.6-luna", "label": "GPT-5.6-Luna"},
            {"id": "gpt-5.5", "label": "GPT-5.5"},
            {"id": "gpt-5.4", "label": "GPT-5.4"},
            {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
            {"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"},
        ],
    },
    "custom": {
        "label": "Custom endpoint",
        "type": "openai_compatible",
        "base_url": "",
        "requires_key": False,
        "default_model": "",
    },
}

ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "custom": "FL_MCP_CHAT_API_KEY",
}


def default_settings() -> dict[str, Any]:
    return {
        "provider": "lmstudio",
        "model": "",
        "base_url": PROVIDER_PRESETS["lmstudio"]["base_url"],
        "approval_mode": "autonomous_edits",
        "always_allowed_tools": [],
        "temperature": 0.2,
    }


class ChatSettingsStore:
    """Small JSON settings store that never accepts secret values."""

    ALLOWED_FIELDS = {
        "provider",
        "model",
        "base_url",
        "approval_mode",
        "always_allowed_tools",
        "temperature",
    }

    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        value = default_settings()
        with self._lock:
            if self.path.exists():
                try:
                    saved = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(saved, dict):
                        value.update({
                            key: saved[key]
                            for key in self.ALLOWED_FIELDS
                            if key in saved
                        })
                except (OSError, json.JSONDecodeError):
                    pass
        return self._normalize(value)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        if any("key" in key.lower() or "token" in key.lower() for key in changes):
            raise ValueError("Credentials must use the credential endpoint.")
        unknown = set(changes) - self.ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")
        value = self.load()
        value.update(changes)
        value = self._normalize(value)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return value

    def public(self) -> dict[str, Any]:
        value = self.load()
        value["presets"] = deepcopy(PROVIDER_PRESETS)
        return value

    def always_allow_tool(self, tool_name: str) -> dict[str, Any]:
        normalized = str(tool_name).strip()
        if not TOOL_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid tool name for an approval rule.")
        with self._lock:
            value = self.load()
            return self.update({
                "always_allowed_tools": [
                    *value["always_allowed_tools"],
                    normalized,
                ],
            })

    def _normalize(self, value: dict[str, Any]) -> dict[str, Any]:
        provider = str(value.get("provider") or "lmstudio").lower()
        if provider not in PROVIDER_PRESETS:
            raise ValueError(f"Unsupported provider: {provider}")
        preset = PROVIDER_PRESETS[provider]
        base_url = str(value.get("base_url") or preset["base_url"]).strip().rstrip("/")
        if preset["type"] == "openai_compatible" and not base_url:
            raise ValueError("An OpenAI-compatible base URL is required.")
        temperature = float(value.get("temperature", 0.2))
        if temperature < 0 or temperature > 2:
            raise ValueError("temperature must be between 0 and 2")
        approval_mode = str(
            value.get("approval_mode") or "autonomous_edits"
        ).strip().lower()
        if approval_mode not in APPROVAL_MODES:
            raise ValueError(f"Unsupported approval mode: {approval_mode}")
        raw_allowed_tools = value.get("always_allowed_tools", [])
        if not isinstance(raw_allowed_tools, list):
            raise ValueError("always_allowed_tools must be a list.")
        always_allowed_tools = sorted({
            str(tool_name).strip()
            for tool_name in raw_allowed_tools
            if str(tool_name).strip()
        })
        if any(
            not TOOL_NAME_PATTERN.fullmatch(tool_name)
            for tool_name in always_allowed_tools
        ):
            raise ValueError("always_allowed_tools contains an invalid tool name.")
        return {
            "provider": provider,
            "model": str(value.get("model") or preset["default_model"]).strip(),
            "base_url": base_url,
            "approval_mode": approval_mode,
            "always_allowed_tools": always_allowed_tools,
            "temperature": temperature,
        }


class CredentialStore:
    """Keychain-first provider credentials with process-memory fallback."""

    def __init__(self):
        self._memory: dict[str, str] = {}
        self._keyring_error: str | None = None

    def get(self, provider: str) -> str | None:
        if provider in self._memory:
            return self._memory[provider]
        env_name = ENV_KEYS.get(provider)
        if env_name and os.getenv(env_name):
            return os.getenv(env_name)
        try:
            import keyring

            value = keyring.get_password(KEYRING_SERVICE, provider)
            if value:
                return value
        except Exception as exc:
            self._keyring_error = str(exc)
        return None

    def set(self, provider: str, credential: str) -> dict[str, Any]:
        if provider not in PROVIDER_PRESETS:
            raise ValueError(f"Unsupported provider: {provider}")
        provider_type = PROVIDER_PRESETS[provider]["type"]
        if provider_type in {"claude_cli", "codex_cli"}:
            manager = "Claude Code" if provider_type == "claude_cli" else "Codex"
            raise ValueError(
                f"{PROVIDER_PRESETS[provider]['label']} credentials are managed by {manager}."
            )
        value = credential.strip()
        if not value:
            raise ValueError("Credential cannot be empty.")
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, provider, value)
            self._memory.pop(provider, None)
            self._keyring_error = None
            return {"stored": True, "storage": "keychain", "persistent": True}
        except Exception as exc:
            self._memory[provider] = value
            self._keyring_error = str(exc)
            return {
                "stored": True,
                "storage": "memory",
                "persistent": False,
                "warning": "OS keychain unavailable; this credential lasts until backend restart.",
            }

    def clear(self, provider: str) -> None:
        self._memory.pop(provider, None)
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, provider)
        except Exception:
            pass

    def status(self, provider: str) -> dict[str, Any]:
        env_name = ENV_KEYS.get(provider)
        source = None
        if provider in self._memory:
            source = "memory"
        elif env_name and os.getenv(env_name):
            source = "environment"
        else:
            try:
                import keyring

                if keyring.get_password(KEYRING_SERVICE, provider):
                    source = "keychain"
            except Exception as exc:
                self._keyring_error = str(exc)
        return {
            "configured": source is not None,
            "source": source,
            "keychain_available": self._keyring_error is None,
        }


chat_settings = ChatSettingsStore()
credential_store = CredentialStore()
