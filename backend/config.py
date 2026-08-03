"""Configuration for the ComfyUI FL-MCP bridge."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FL_MCP_DATA_DIR", PROJECT_ROOT / ".fl_mcp"))
SETTINGS_PATH = DATA_DIR / "bridge_settings.json"
LEGACY_ENV_PATH = PROJECT_ROOT / ".env"

LEGACY_ENV_KEYS = {
    "BACKEND_LAUNCH_MODE": "backend_launch_mode",
    "AUTO_START_BACKEND": "auto_start_backend",
    "AUTO_RESTART_BACKEND": "auto_restart_backend",
    "LOG_BACKEND_TO_FILE": "log_backend_to_file",
    "WS_HOST": "ws_host",
    "WS_PORT": "ws_port",
    "COMFYUI_SERVER_URL": "comfyui_server_url",
    "COMFYUI_API_TIMEOUT": "comfyui_api_timeout",
    "PUBLIC_URL": "public_url",
    "LOG_LEVEL": "log_level",
    "COMFYUI_PATH": "comfyui_path",
    "COMFYUI_EXTRA_MODEL_PATHS": "extra_model_paths_path",
    "FL_MCP_ENABLE_WORKFLOW_WRITES": "enable_workflow_writes",
    "FL_MCP_ENABLE_CUSTOM_NODE_WRITES": "enable_custom_node_writes",
    "FL_MCP_ENABLE_GIT_WRITES": "enable_git_writes",
    "FL_MCP_ENABLE_MANAGER_MUTATIONS": "enable_manager_mutations",
    "FL_MCP_ENABLE_COMFY_PROCESS_CONTROL": "enable_comfy_process_control",
}


class Settings(BaseModel):
    """Validated, non-secret bridge settings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    backend_launch_mode: Literal["auto", "terminal", "subprocess", "manual"] = "subprocess"
    auto_start_backend: bool = True
    auto_restart_backend: bool = True
    log_backend_to_file: bool = True

    ws_host: str = Field("127.0.0.1", min_length=1)
    ws_port: int = Field(8000, ge=1, le=65535)

    comfyui_server_url: str = "http://127.0.0.1:8188"
    comfyui_api_timeout: int = Field(10, ge=1, le=300)
    public_url: str = "http://127.0.0.1:8000"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    wait_for_generation_completion: bool = False
    generation_completion_timeout: int = Field(300, ge=1, le=3600)
    comfyui_path: str | None = None
    extra_model_paths_path: str | None = None

    enable_workflow_writes: bool = False
    enable_custom_node_writes: bool = False
    enable_git_writes: bool = False
    enable_manager_mutations: bool = False
    enable_comfy_process_control: bool = False

    @field_validator("comfyui_server_url")
    @classmethod
    def validate_comfyui_server_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("ComfyUI server URL must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Public URL must be empty or an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("comfyui_path", "extra_model_paths_path", mode="before")
    @classmethod
    def normalize_optional_path(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class BridgeSettingsStore:
    """Atomic JSON settings store with one-time legacy environment import."""

    def __init__(
        self,
        path: Path = SETTINGS_PATH,
        legacy_env_path: Path = LEGACY_ENV_PATH,
        environ: Mapping[str, str] | None = None,
    ):
        self.path = path
        self.legacy_env_path = legacy_env_path
        self.environ = environ if environ is not None else os.environ
        self._lock = threading.RLock()
        self.migrated_from_env = False

    def load(self) -> Settings:
        with self._lock:
            if self.path.exists():
                return self._read()

            legacy = self._legacy_values()
            value = Settings.model_validate(legacy)
            if legacy:
                self._write(value)
                self.migrated_from_env = True
            return value

    def update(self, changes: Mapping[str, Any]) -> Settings:
        if not isinstance(changes, Mapping):
            raise ValueError("Bridge settings update must be a JSON object")
        unknown = set(changes) - set(Settings.model_fields)
        if unknown:
            raise ValueError(f"Unsupported bridge settings: {', '.join(sorted(unknown))}")
        with self._lock:
            current = self.load().model_dump(mode="json")
            value = Settings.model_validate(current | dict(changes))
            self._write(value)
            return value

    def _read(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bridge settings file contains invalid JSON: {self.path}") from exc
        if not isinstance(raw, dict):
            raise ValueError("Bridge settings file must contain a JSON object")
        return Settings.model_validate(raw)

    def _write(self, value: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _legacy_values(self) -> dict[str, Any]:
        file_values = (
            dotenv_values(self.legacy_env_path)
            if self.legacy_env_path.is_file()
            else {}
        )
        normalized_file = {str(key).upper(): value for key, value in file_values.items()}
        normalized_environment = {
            str(key).upper(): value for key, value in self.environ.items()
        }
        values: dict[str, Any] = {}
        for environment_key, field_name in LEGACY_ENV_KEYS.items():
            value = normalized_environment.get(
                environment_key,
                normalized_file.get(environment_key),
            )
            if value is not None:
                values[field_name] = value
        return values


bridge_settings_store = BridgeSettingsStore()
settings = bridge_settings_store.load()


def bridge_settings_payload(
    store: BridgeSettingsStore = bridge_settings_store,
    effective: Settings = settings,
) -> dict[str, Any]:
    stored = store.load()
    stored_values = stored.model_dump(mode="json")
    effective_values = effective.model_dump(mode="json")
    return {
        "stored": stored_values,
        "effective": effective_values,
        "defaults": Settings().model_dump(mode="json"),
        "pendingRestartFields": [
            field
            for field in Settings.model_fields
            if stored_values[field] != effective_values[field]
        ],
        "persisted": store.path.exists(),
        "migratedFromEnv": store.migrated_from_env,
    }
