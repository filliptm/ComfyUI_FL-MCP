"""Runtime provider configuration for Ren.

The .env file remains the base configuration. This module stores UI-managed
provider choices and credentials in an ignored local file so users can switch
connections from the sidebar without editing files or restarting ComfyUI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from config import settings
from model_defaults import get_default_model


PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / ".ren"
CONFIG_FILE = CONFIG_DIR / "provider_config.json"

PROVIDERS = ("anthropic", "openrouter", "local", "gemini", "openai")
KEY_FIELDS = {
    "anthropic": "anthropic_api_key",
    "openrouter": "openrouter_api_key",
    "gemini": "google_api_key",
    "openai": "openai_api_key",
}

MODEL_OPTIONS = {
    "anthropic": [
        {"id": "claude-3-5-sonnet-20241022", "label": "Claude 3.5 Sonnet"},
        {"id": "claude-3-5-haiku-20241022", "label": "Claude 3.5 Haiku"},
        {"id": "claude-3-opus-20240229", "label": "Claude 3 Opus"},
    ],
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5"},
        {"id": "anthropic/claude-opus-4", "label": "Claude Opus 4"},
        {"id": "anthropic/claude-haiku-4.5", "label": "Claude Haiku 4.5"},
        {"id": "openai/gpt-4o", "label": "GPT-4o"},
        {"id": "openai/gpt-5", "label": "GPT-5"},
        {"id": "qwen/qwen3-coder", "label": "Qwen3 Coder"},
        {"id": "deepseek/deepseek-v3.2", "label": "DeepSeek V3.2"},
        {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
    ],
    "gemini": [
        {"id": "gemini-2.0-flash-exp", "label": "Gemini 2.0 Flash"},
        {"id": "gemini-1.5-pro", "label": "Gemini 1.5 Pro"},
    ],
    "openai": [
        {"id": "gpt-4o", "label": "GPT-4o"},
        {"id": "gpt-4-turbo-preview", "label": "GPT-4 Turbo"},
        {"id": "o1-preview", "label": "o1 Preview"},
    ],
}


def _default_config() -> Dict[str, Any]:
    models = {
        "anthropic": get_default_model("anthropic"),
        "openrouter": get_default_model("openrouter"),
        "gemini": get_default_model("gemini"),
        "openai": get_default_model("openai"),
        "local": "",
    }
    if settings.llm_model and settings.llm_provider in models:
        models[settings.llm_provider] = settings.llm_model

    return {
        "provider": settings.llm_provider,
        "models": models,
        "keys": {},
        "local": {
            "baseURL": settings.local_llm_url or "http://127.0.0.1:1234/v1",
            "model": settings.llm_model if settings.llm_provider == "local" else "",
            "apiKey": settings.local_api_key or "local",
        },
    }


def load_config() -> Dict[str, Any]:
    config = _default_config()
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                _deep_update(config, saved)
        except Exception:
            pass
    return config


def save_config(config: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _deep_update(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _mask(value: str) -> Optional[str]:
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def apply_to_settings() -> Dict[str, Any]:
    config = load_config()
    provider = config.get("provider")
    if provider not in PROVIDERS:
        provider = settings.llm_provider

    settings.llm_provider = provider
    models = config.get("models") or {}

    if provider == "local":
        local = config.get("local") or {}
        settings.local_llm_url = local.get("baseURL") or settings.local_llm_url
        settings.local_api_key = local.get("apiKey") or settings.local_api_key or "local"
        settings.llm_model = local.get("model") or None
    else:
        settings.llm_model = models.get(provider) or get_default_model(provider)

    keys = config.get("keys") or {}
    for key_provider, field in KEY_FIELDS.items():
        saved_key = keys.get(key_provider)
        if saved_key:
            setattr(settings, field, saved_key)

    return config


def select_provider(provider: str, model: Optional[str] = None) -> Dict[str, Any]:
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    config = load_config()
    config["provider"] = provider
    if model:
        if provider == "local":
            config.setdefault("local", {})["model"] = model
        else:
            config.setdefault("models", {})[provider] = model
    save_config(config)
    apply_to_settings()
    return config


def set_api_key(provider: str, api_key: str) -> Dict[str, Any]:
    if provider not in KEY_FIELDS:
        raise ValueError(f"Provider does not use an API key here: {provider}")
    config = load_config()
    config.setdefault("keys", {})[provider] = api_key
    save_config(config)
    apply_to_settings()
    return config


def clear_api_key(provider: str) -> Dict[str, Any]:
    if provider not in KEY_FIELDS:
        raise ValueError(f"Provider does not use an API key here: {provider}")
    config = load_config()
    config.setdefault("keys", {}).pop(provider, None)
    save_config(config)
    setattr(settings, KEY_FIELDS[provider], "")
    apply_to_settings()
    return config


def set_local_config(base_url: str, model: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    config = load_config()
    config["provider"] = "local"
    config["local"] = {
        "baseURL": base_url,
        "model": model,
        "apiKey": api_key or "local",
    }
    config.setdefault("models", {})["local"] = model
    save_config(config)
    apply_to_settings()
    return config


def clear_local_config() -> Dict[str, Any]:
    config = load_config()
    config["local"] = {
        "baseURL": "http://127.0.0.1:1234/v1",
        "model": "",
        "apiKey": "local",
    }
    config.setdefault("models", {})["local"] = ""
    save_config(config)
    settings.local_llm_url = "http://127.0.0.1:1234/v1"
    settings.local_api_key = "local"
    if settings.llm_provider == "local":
        settings.llm_model = None
    apply_to_settings()
    return config


def status() -> Dict[str, Any]:
    config = apply_to_settings()
    keys = config.get("keys") or {}
    local = config.get("local") or {}
    provider = config.get("provider") if config.get("provider") in PROVIDERS else settings.llm_provider

    def configured(key_provider: str, env_value: str = "") -> bool:
        return bool(keys.get(key_provider) or env_value)

    return {
        "provider": provider,
        "model": settings.llm_model or settings.resolved_model,
        "models": config.get("models") or {},
        "modelOptions": MODEL_OPTIONS,
        "providers": {
            "anthropic": {
                "configured": configured("anthropic", settings.anthropic_api_key),
                "keyPreview": _mask(keys.get("anthropic") or settings.anthropic_api_key),
            },
            "openrouter": {
                "configured": configured("openrouter", settings.openrouter_api_key),
                "keyPreview": _mask(keys.get("openrouter") or settings.openrouter_api_key),
            },
            "local": {
                "configured": bool(local.get("baseURL") and local.get("model")),
                "baseURL": local.get("baseURL") or "http://127.0.0.1:1234/v1",
                "model": local.get("model") or None,
            },
            "gemini": {
                "configured": configured("gemini", settings.google_api_key),
                "keyPreview": _mask(keys.get("gemini") or settings.google_api_key),
            },
            "openai": {
                "configured": configured("openai", settings.openai_api_key),
                "keyPreview": _mask(keys.get("openai") or settings.openai_api_key),
            },
        },
    }
