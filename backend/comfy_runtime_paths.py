"""Share ComfyUI's authoritative runtime image directories with child processes."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

RUNTIME_IMAGE_PATH_ENV = {
    "input": "FL_MCP_COMFYUI_INPUT_DIR",
    "output": "FL_MCP_COMFYUI_OUTPUT_DIR",
    "temp": "FL_MCP_COMFYUI_TEMP_DIR",
}


def export_comfy_runtime_paths(
    folder_paths_module: Any,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, Path]:
    """Export directories after ComfyUI has applied Desktop/CLI overrides."""

    target = environ if environ is not None else os.environ
    getters = {
        "input": folder_paths_module.get_input_directory,
        "output": folder_paths_module.get_output_directory,
        "temp": folder_paths_module.get_temp_directory,
    }
    exported: dict[str, Path] = {}
    for folder_type, getter in getters.items():
        path = Path(getter()).expanduser().resolve()
        target[RUNTIME_IMAGE_PATH_ENV[folder_type]] = str(path)
        exported[folder_type] = path
    return exported


def configured_runtime_paths(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    """Read trusted runtime image roots inherited from the ComfyUI process."""

    source = environ if environ is not None else os.environ
    configured: dict[str, Path] = {}
    for folder_type, env_name in RUNTIME_IMAGE_PATH_ENV.items():
        raw_path = str(source.get(env_name) or "").strip()
        if raw_path:
            configured[folder_type] = Path(raw_path).expanduser().resolve()
    return configured
