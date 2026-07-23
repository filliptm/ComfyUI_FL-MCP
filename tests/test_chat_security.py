import ast
from pathlib import Path

from chat_security import (
    APPROVAL_REQUIRED_TOOLS,
    CANVAS_EDIT_TOOLS,
    READ_ONLY_TOOLS,
    classify_tool,
)


def _mcp_tool_names():
    source = Path(__file__).resolve().parents[1] / "backend" / "mcp_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        ):
            names.add(node.name)
    return names


def test_every_mcp_tool_has_an_explicit_risk_classification():
    classified = READ_ONLY_TOOLS | CANVAS_EDIT_TOOLS | APPROVAL_REQUIRED_TOOLS
    assert _mcp_tool_names() - classified == set()
    assert classify_tool("unknown_future_tool") == "approval_required"


def test_consequential_tools_require_approval():
    for name in (
        "queue_workflow",
        "workflow_delete_file",
        "manager_queue_action",
        "custom_nodes_write_file",
        "custom_nodes_git_push",
        "comfy_restart",
    ):
        assert classify_tool(name) == "approval_required"
