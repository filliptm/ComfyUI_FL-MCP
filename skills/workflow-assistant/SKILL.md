---
name: workflow-assistant
description: "Use when working with ComfyUI workflows through FL-MCP or similar MCP tools: inspect, explain, create, edit, debug, compact, validate, queue, screenshot, or organize node graphs while preserving a disciplined workflow-first process."
---

# Workflow Assistant

Use this skill when a user wants help with a live ComfyUI workflow, saved workflow JSON, generated image metadata, custom node graph, or FL-MCP tool session.

This skill is workflow-first. It borrows the old Ren Agent's operating discipline and cognitive modes, but does not adopt a persona, product UI, hosted chat runtime, or `ren://` links.

## Core Loop

1. Inspect before changing anything.
   - Use `workflow_overview` for graph health and node counts.
   - Use `workflow_get_current_json` or `query_workflow` for exact graph structure.
   - Use `get_current_node_selection` when the user says "this", "that node", or points at the canvas.
2. Plan the smallest useful change.
   - Prefer existing nodes and existing graph patterns.
   - Do not create replacement nodes when the user only asked to change a parameter.
   - Batch node creation or connection calls when the tool supports it.
   - Treat nodes as rectangles. Never plan spacing from x/y points alone or guess that different node types share one size.
3. Modify through explicit graph tools.
   - Use `set_node_values` for widget values.
   - Use `connect_nodes` or `connect_nodes_batch` for links.
   - Omit x/y from `create_nodes` when exact placement is unimportant; collision-aware creation will place nodes beside the graph.
   - Use `workflow_load_json` only when replacing or restructuring a larger graph.
4. Verify immediately.
   - Re-run `workflow_overview`.
   - For high-risk nodes, inspect slots or values directly.
   - Read the final `position` and measured `size` returned by `create_nodes`; use those bounds for subsequent manual placement.
   - After layout changes, use `take_screenshot` or `focus_on_nodes` to verify visually.
5. Queue only after validation.
   - Check required sampler, decoder, loader, and save connections.
   - After queueing, inspect queue/history/errors before declaring success.

## Cognitive Modes

Detect the user's current working mode and adapt:

- Outcome framing: clarify desired output, constraints, model family, resolution, speed, and style before graph changes.
- Forage and sense-make: search installed nodes, models, templates, and Manager mappings; present a small curated set of options.
- Architecture: describe the pipeline in modules and use Mermaid diagrams when they reduce ambiguity.
- Prototype: favor fast, reversible edits and A/B branches; keep explanations short.
- Debug: form one hypothesis at a time, insert preview/probe nodes at boundaries, and avoid multiple simultaneous fixes.
- Tuning: identify the few high-leverage controls, suggest ranges, and keep seeds/settings reproducible when comparing.
- Cleanup: group, name, compact, and document nodes so the graph remains understandable.
- Performance: look for VRAM peaks, oversized resolutions, duplicated model loads, excessive branches, and cache opportunities.
- Validation: compare the current graph and outputs against the original goal.

## FL-MCP Tool Patterns

Always wrap tool input as required by the active MCP client. In many FL-MCP clients, tool arguments are shaped as `{"request": {...}}`.

Common patterns:

- Understand the open graph: `workflow_overview`, `workflow_diagram`, `workflow_get_current_json`.
- Explain selected nodes: `get_current_node_selection`, then `get_node_values` or `get_node_slots`.
- Change parameters: find the existing node, then call `set_node_values`, then verify.
- Add or rewire nodes: inspect the surrounding layout, create nodes in a batch, use the returned rectangles, connect them, then `modify_layout` when semantic organization is needed and take a screenshot.
- Compact or clean layout: `get_layout`, `modify_layout`, optionally edit group bounds via workflow JSON, then `take_screenshot`.
- Debug execution: `queue_workflow`, `wait`, `get_execution_history`, `get_execution_details`, and error-buffer tools.
- Check assets/models: `comfy_models_list`, `comfy_list_folders`, `comfy_read_file`, `extract_workflow_from_image`.
- Find custom node packs: `manager_search_nodes`, `manager_v4_node_mappings`, `custom_nodes_search`.

## Workflow Rules

- Never assume the graph is valid because it looks connected. Verify required slots.
- Never place multiple new nodes at the same coordinates. Prefer automatic placement, or calculate manual positions from measured width and height plus a visible gap.
- Never assume requested creation coordinates were retained. Collision avoidance may adjust them; use the final bounds returned by `create_nodes`.
- Never set a ComfyUI KSampler seed to a negative value.
- If a KSampler has `control_after_generate` set to fixed and the same prompt/settings are queued again, ComfyUI may reuse cached work. Change an intentional parameter or explain why no new run appears.
- If positive and negative conditioning share the same prompt node, call it out unless the user intentionally asked for that.
- Keep one shared model/CLIP/VAE loader when building multi-branch workflows unless the user asks for different models.
- Avoid duplicating heavy loaders when branches can share outputs.
- Use SaveImage prefixes that make output folders understandable for the current task.
- When a tool fails, report the concrete failure and pick the next safest diagnostic step.

## Response Style

- Be direct, practical, and concise.
- Match the user's skill level without saying you are doing so.
- For command-style requests, perform the action and report what changed.
- For debugging, lead with the observed issue, evidence, and next fix.
- For architecture explanations, include a compact Mermaid diagram when it helps.

## References

Read `references/comfy-workflow-patterns.md` when creating or restructuring workflows, troubleshooting common node graph patterns, or choosing a standard graph shape.
