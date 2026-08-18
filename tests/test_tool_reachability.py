"""Every registered MCP tool must be reachable through chat tool selection.

This invariant exists because 64 of 136 registered tools were once silently
unreachable: no test asserted that a registered tool could ever appear in the
set tools_for_message hands to the model, so tools shipped registered,
documented, and counted - but impossible for the assistant to call. A tool
that is deliberately kept away from the chat runtime must be listed in
DELIBERATELY_UNEXPOSED_TOOLS with a reason, so the exclusion is a visible,
reviewed decision instead of an accident.
"""

import re
from pathlib import Path

from chat_runtime import (
    BRANCH_COMPARISON_TOOLS,
    BRANCH_DISCOVERY_TOOLS,
    BRANCH_MUTATION_TOOLS,
    BRANCH_NAVIGATION_TOOLS,
    CANVAS_IMAGE_INSPECTION_TOOLS,
    CANVAS_IMAGE_READ_ONLY_TOOLS,
    CORE_CHAT_TOOLS,
    DELIBERATELY_UNEXPOSED_TOOLS,
    INTENT_GROUP_TRIGGERS,
    INTENT_TOOL_GROUPS,
    PROMPT_CONTEXT_INSPECTION_TOOLS,
    PROMPT_REFERENCE_TOOLS,
    PROMPT_VALUE_TOOLS,
    REFINEMENT_COMPILER_TOOLS,
    REFINEMENT_EXECUTION_TOOLS,
    REFINEMENT_MASK_TOOLS,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"


def registered_mcp_tools() -> set[str]:
    source = (BACKEND_DIR / "mcp_server.py").read_text(encoding="utf-8")
    return set(re.findall(r"@mcp\.tool\(\)\s*\nasync def (\w+)\(", source))


def chat_selectable_tools() -> set[str]:
    """Union of every tool set tools_for_message can ever return or add."""

    selectable: set[str] = set()
    selectable |= CORE_CHAT_TOOLS
    for group in INTENT_TOOL_GROUPS.values():
        selectable |= group
    selectable |= BRANCH_DISCOVERY_TOOLS
    selectable |= BRANCH_NAVIGATION_TOOLS
    selectable |= BRANCH_COMPARISON_TOOLS
    selectable |= BRANCH_MUTATION_TOOLS
    selectable |= REFINEMENT_COMPILER_TOOLS
    selectable |= REFINEMENT_EXECUTION_TOOLS
    selectable |= REFINEMENT_MASK_TOOLS
    selectable |= PROMPT_VALUE_TOOLS
    selectable |= PROMPT_REFERENCE_TOOLS
    selectable |= PROMPT_CONTEXT_INSPECTION_TOOLS
    selectable |= CANVAS_IMAGE_INSPECTION_TOOLS
    selectable |= CANVAS_IMAGE_READ_ONLY_TOOLS
    # Added by tools_for_message whenever the user enables a web search mode.
    selectable |= {"web_search", "web_fetch_page"}
    return selectable


def test_every_registered_tool_is_selectable_or_deliberately_unexposed():
    registered = registered_mcp_tools()
    assert len(registered) > 100, "tool registration extraction looks broken"
    unreachable = registered - chat_selectable_tools() - set(DELIBERATELY_UNEXPOSED_TOOLS)
    assert not unreachable, (
        "Registered MCP tools unreachable by the chat runtime and not listed "
        f"in DELIBERATELY_UNEXPOSED_TOOLS: {sorted(unreachable)}. Either add "
        "each tool to CORE_CHAT_TOOLS or an INTENT_TOOL_GROUPS group with a "
        "trigger, or record the deliberate exclusion with a reason."
    )


def test_deliberate_exclusions_are_real_registered_tools_with_reasons():
    registered = registered_mcp_tools()
    for tool, reason in DELIBERATELY_UNEXPOSED_TOOLS.items():
        assert tool in registered, (
            f"DELIBERATELY_UNEXPOSED_TOOLS lists {tool!r}, which is not a "
            "registered MCP tool - remove the stale entry."
        )
        assert isinstance(reason, str) and len(reason) >= 20, (
            f"DELIBERATELY_UNEXPOSED_TOOLS[{tool!r}] needs a real reason."
        )


def test_deliberate_exclusions_are_not_also_selectable():
    overlap = set(DELIBERATELY_UNEXPOSED_TOOLS) & chat_selectable_tools()
    assert not overlap, (
        f"{sorted(overlap)} are listed as deliberately unexposed but are "
        "also selectable - resolve the contradiction."
    )


def test_every_intent_group_has_a_selection_path():
    """A group without either a trigger regex or a keyword block is dead weight."""

    legacy_keyword_groups = {"debug", "manager", "models", "coding", "files"}
    for group_name in INTENT_TOOL_GROUPS:
        assert group_name in INTENT_GROUP_TRIGGERS or group_name in legacy_keyword_groups, (
            f"INTENT_TOOL_GROUPS[{group_name!r}] has no trigger regex and no "
            "legacy keyword block in tools_for_message - it can never fire."
        )
