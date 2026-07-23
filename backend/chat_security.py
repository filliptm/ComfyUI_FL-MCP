"""Risk classification for embedded assistant MCP calls."""

from __future__ import annotations

from typing import Literal


ToolRisk = Literal["read_only", "canvas_edit", "approval_required"]

CANVAS_EDIT_TOOLS = {
    "create_nodes",
    "remove_nodes",
    "bypass_nodes",
    "unbypass_nodes",
    "pin_nodes",
    "unpin_nodes",
    "select_nodes",
    "focus_on_nodes",
    "set_node_values",
    "connect_nodes",
    "connect_nodes_batch",
    "auto_connect_workflow",
    "modify_layout",
    "set_batch_count",
    "workflow_close_current",
}

APPROVAL_REQUIRED_TOOLS = {
    "workflow_load_json",
    "workflow_save_current",
    "workflow_rename_file",
    "workflow_delete_file",
    "workflow_duplicate_current",
    "queue_workflow",
    "cancel_workflow",
    "enable_auto_queue",
    "disable_auto_queue",
    "comfy_history_delete",
    "comfy_settings_set",
    "manager_queue_action",
    "manager_queue_start",
    "manager_queue_reset",
    "manager_v4_queue_action",
    "delete_queue_items",
    "comfy_upload_image",
    "comfy_upload_mask",
    "comfy_asset_upload",
    "comfy_assets_upload",
    "clear_error_buffer",
    "custom_nodes_write_file",
    "custom_nodes_apply_patch",
    "custom_nodes_create_pack",
    "custom_nodes_git_commit",
    "custom_nodes_git_push",
    "comfy_restart",
    "frontend_execute_command",
}

READ_ONLY_TOOLS = {
    "calculate_expressions",
    "wait",
    "query_workflow",
    "workflow_overview",
    "workflow_diagram",
    "frontend_list_commands",
    "frontend_list_keybindings",
    "workflow_get_current_json",
    "workflow_get_tabs",
    "workflow_list_files",
    "workflow_read_file",
    "find_node",
    "take_screenshot",
    "get_current_node_selection",
    "get_node_values",
    "get_node_slots",
    "get_layout",
    "get_queue_status",
    "comfy_jobs_list",
    "comfy_job_get",
    "comfy_free_memory",
    "comfy_settings_get",
    "manager_queue_status",
    "manager_v4_status",
    "manager_v4_queue_status",
    "manager_v4_installed_packs",
    "manager_v4_snapshots",
    "manager_v4_node_mappings",
    "manager_v4_external_models",
    "generate_seed",
    "generate_float",
    "generate_int",
    "random_choice",
    "get_system_info",
    "mcp_capability_audit",
    "comfy_models_list",
    "comfy_workflow_templates_list",
    "comfy_global_subgraphs_list",
    "comfy_node_replacements_get",
    "comfy_assets_list",
    "comfy_asset_get",
    "comfy_tags_list",
    "comfy_list_folders",
    "comfy_read_file",
    "comfy_search_resources",
    "extract_workflow_from_image",
    "node_library_search",
    "node_library_get_details",
    "node_library_find_compatible",
    "manager_search_nodes",
    "manager_get_node_mappings",
    "manager_check_updates",
    "manager_search_external_models",
    "get_execution_history",
    "get_queue_status_details",
    "get_execution_details",
    "custom_nodes_list_packs",
    "custom_nodes_read_file",
    "custom_nodes_read_file_excerpt",
    "custom_nodes_search",
    "custom_nodes_validate_pack",
    "custom_nodes_git_status",
    "custom_nodes_git_diff",
    "comfy_get_logs",
    "comfy_status",
}


def classify_tool(tool_name: str) -> ToolRisk:
    """Unknown tools fail closed and require explicit approval."""
    if tool_name in READ_ONLY_TOOLS:
        return "read_only"
    if tool_name in CANVAS_EDIT_TOOLS:
        return "canvas_edit"
    return "approval_required"


def requires_approval(tool_name: str) -> bool:
    return classify_tool(tool_name) == "approval_required"
