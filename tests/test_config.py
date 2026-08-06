import json

import pytest
from config import (
    BridgeSettingsStore,
    Settings,
    bridge_settings_payload,
)
from extra_model_paths_loader import ExtraModelPathsLoader


def test_bridge_settings_use_defaults_without_creating_a_file(tmp_path):
    path = tmp_path / "bridge_settings.json"
    store = BridgeSettingsStore(
        path,
        tmp_path / ".env",
        environ={},
    )

    value = store.load()

    assert value == Settings()
    assert value.enable_workflow_writes is True
    assert path.exists() is False


def test_explicit_workflow_write_opt_out_is_preserved(tmp_path):
    path = tmp_path / "bridge_settings.json"
    store = BridgeSettingsStore(path, tmp_path / ".env", environ={})

    store.update({"enable_workflow_writes": False})
    reloaded = BridgeSettingsStore(path, tmp_path / ".env", environ={}).load()

    assert reloaded.enable_workflow_writes is False


def test_legacy_environment_can_disable_default_workflow_writes(tmp_path):
    store = BridgeSettingsStore(
        tmp_path / "bridge_settings.json",
        tmp_path / ".env",
        environ={"FL_MCP_ENABLE_WORKFLOW_WRITES": "false"},
    )

    assert store.load().enable_workflow_writes is False


def test_legacy_environment_is_imported_once_and_left_in_place(tmp_path):
    path = tmp_path / "bridge_settings.json"
    legacy = tmp_path / ".env"
    legacy.write_text(
        "\n".join([
            "WS_PORT=8123",
            "FL_MCP_ENABLE_WORKFLOW_WRITES=true",
            "COMFYUI_PATH=/legacy/comfy",
            "TOOL_TIMEOUT=99999",
        ]),
        encoding="utf-8",
    )
    store = BridgeSettingsStore(
        path,
        legacy,
        environ={"WS_PORT": "8124"},
    )

    value = store.load()
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert value.ws_port == 8124
    assert value.enable_workflow_writes is True
    assert value.comfyui_path == "/legacy/comfy"
    assert "tool_timeout" not in persisted
    assert legacy.exists() is True
    assert store.migrated_from_env is True

    reloaded = BridgeSettingsStore(
        path,
        legacy,
        environ={"WS_PORT": "9000"},
    ).load()
    assert reloaded.ws_port == 8124


def test_bridge_settings_updates_are_validated_and_atomic(tmp_path):
    path = tmp_path / "bridge_settings.json"
    store = BridgeSettingsStore(path, tmp_path / ".env", environ={})

    value = store.update({
        "ws_port": 9100,
        "public_url": "",
        "comfyui_path": "",
        "wait_for_generation_completion": True,
        "generation_completion_timeout": 600,
    })

    assert value.ws_port == 9100
    assert value.public_url == ""
    assert value.comfyui_path is None
    assert value.wait_for_generation_completion is True
    assert value.generation_completion_timeout == 600
    assert path.with_suffix(".json.tmp").exists() is False

    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported bridge settings"):
        store.update({"api_key": "not-allowed"})
    with pytest.raises(ValueError, match="less than or equal to 65535"):
        store.update({"ws_port": 70000})
    with pytest.raises(ValueError, match="less than or equal to 3600"):
        store.update({"generation_completion_timeout": 3601})
    assert path.read_text(encoding="utf-8") == before


def test_bridge_settings_payload_reports_restart_changes(tmp_path):
    store = BridgeSettingsStore(
        tmp_path / "bridge_settings.json",
        tmp_path / ".env",
        environ={},
    )
    effective = store.load()
    store.update({"log_level": "DEBUG", "enable_git_writes": True})

    payload = bridge_settings_payload(store, effective)

    assert payload["effective"]["log_level"] == "INFO"
    assert payload["stored"]["log_level"] == "DEBUG"
    assert payload["pendingRestartFields"] == [
        "log_level",
        "enable_git_writes",
    ]
    assert payload["persisted"] is True


def test_extra_model_paths_loader_uses_the_configured_file(tmp_path):
    comfy_root = tmp_path / "ComfyUI"
    comfy_root.mkdir()
    configured = tmp_path / "shared-model-paths.yaml"
    configured.write_text("shared:\n  base_path: /models\n", encoding="utf-8")

    loader = ExtraModelPathsLoader(comfy_root, config_path=str(configured))

    assert loader.find_yaml_file() == configured
