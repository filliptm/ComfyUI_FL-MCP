from types import SimpleNamespace

from comfy_runtime_paths import export_comfy_runtime_paths


def test_export_runtime_paths_uses_comfyuis_active_directories(tmp_path):
    input_root = tmp_path / "desktop" / "input"
    output_root = tmp_path / "desktop" / "output"
    temp_root = tmp_path / "desktop" / "temp"
    folder_paths = SimpleNamespace(
        get_input_directory=lambda: str(input_root),
        get_output_directory=lambda: str(output_root),
        get_temp_directory=lambda: str(temp_root),
    )
    environ = {}

    exported = export_comfy_runtime_paths(folder_paths, environ)

    assert exported == {
        "input": input_root.resolve(),
        "output": output_root.resolve(),
        "temp": temp_root.resolve(),
    }
    assert environ == {
        "FL_MCP_COMFYUI_INPUT_DIR": str(input_root.resolve()),
        "FL_MCP_COMFYUI_OUTPUT_DIR": str(output_root.resolve()),
        "FL_MCP_COMFYUI_TEMP_DIR": str(temp_root.resolve()),
    }
