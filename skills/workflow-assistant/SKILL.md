---
name: workflow-assistant
description: "Use when working with ComfyUI workflows through FL-MCP or similar MCP tools: inspect, explain, create, edit, debug, compact, validate, queue, screenshot, or organize node graphs while preserving a disciplined workflow-first process."
---

# Workflow Assistant

Use this skill when a user wants help with a live ComfyUI workflow, saved workflow JSON, generated image metadata, custom node graph, or FL-MCP tool session.

This skill is workflow-first. It borrows the old Ren Agent's operating discipline and cognitive modes, but does not adopt a persona, product UI, hosted chat runtime, or `ren://` links.

## Core Loop

1. Compile the whole requested graph change once.
   - For a whole-branch request such as find, jump, compare, clone, replace, or remove, call `workflow_branches_discover` first. Use exact returned workflow, graph, catalog, scope, branch, node, slot, and boundary-edge facts; never authorize a branch from its label or fingerprint.
   - Resolve comparison operands to two exact branch IDs under the same pinned catalog. One bounded listing may identify both uniquely; otherwise perform separate pinned discovery resolutions. Never guess two branches from one ambiguous text query.
   - For whole-branch clone, replace, or remove, pass the exact discovery pins and mappings to `compile_workflow_branch_operation`. Root operations return GraphPatch v2; supported nested operations return scoped GraphPatch v3.
   - For a new workflow, an added branch, a rewire, a removal, or another explicit topology change, call `compile_workflow_refinement_spec` first. Mask pixels and prompt text use their dedicated narrow lanes below; do not send value-only prompt edits through GraphPatch.
   - Include every requested role or exact class, value, edge, update/removal, attachment, and layout hint in one semantic request.
   - Describe the desired endpoints rather than guessing intermediary classes or dynamic dotted paths. The compiler prefers direct compatibility, then may infer a unique bounded supported local converter route. Partner/API/heavy/output nodes require explicit intent.
   - When the user says exactly, only, or no extra nodes, set `allow_inferred_converters=false`.
   - Use deterministic existing-node selectors. Set `selected=true` when the user says “selected”, “this”, or “that”; the compiler reads the live selection internally.
   - Do not precede the compiler with workflow JSON, overview, selection, node search/details, slots, values, or layout reads. It reads the active graph (including an empty canvas) and refreshed local native, partner, and custom schemas internally.
2. Respect deterministic outcomes.
   - If `valid=true`, pass the returned `apply_request` unchanged to `apply_workflow_graph_patch`.
   - After a successful whole-branch apply, call `resolve_workflow_branch_successor` with the unchanged apply envelope, compiler `pending_successor_locator`, and the apply result's exact aliases, workflow identity, and final graph hash. Treat its per-scope successor list as authoritative; zero or multiple successors are valid outcomes when explicitly attested.
   - If `needs_choice=true`, show the ranked node, endpoint, or route candidates and wait for the user. Never accept an alphabetical tie-break.
   - If a schema is classified unsupported, stop and report it. Use lower-level schema tools only in a focused diagnostic follow-up, never to bypass the failed atomic route in the same run.
3. Apply one atomic GraphPatch.
   - Keep the same application ID for retries so the idempotency ledger cannot duplicate work.
   - Root GraphPatch v2 and scoped GraphPatch v3 may create, update, connect, disconnect, remove, attach, and lay out arbitrary acyclic branches with fan-in, fan-out, multiple sinks, dynamic inputs, and explicit widget-to-input conversion.
   - It pins workflow, graph, catalog, and schemas; preserves unrelated state; visibly applies mutations in deterministic order; verifies the exact final graph; fully rolls back on failure; and never queues.
   - Do not fall back to `create_nodes`, `set_node_values`, `connect_nodes_batch`, `remove_nodes`, or legacy workflow planners after a GraphPatch validation/application failure.
4. Trust exact verification.
   - A successful application already verifies the graph, node facts, attachments, layout, and unrelated workflow envelope. Do not add redundant overview/JSON/slot/value/layout reads.
   - Inspect again only when the result reports a mismatch, the user explicitly asks for a visual check, or a later execution/output review requires it.
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

- Any normal graph change: `compile_workflow_refinement_spec`, then pass its valid `apply_request` unchanged to `apply_workflow_graph_patch`.
- Find/list a branch: `workflow_branches_discover`; stop on ambiguity.
- Jump/focus a branch: discover it, then pass its exact pins and `branch_id` to `workflow_branch_navigate`.
- Compare branches: resolve two exact IDs under the same catalog pins, then call `workflow_branch_compare`.
- Clone/replace/remove a whole branch: discover, call `compile_workflow_branch_operation`, apply its unchanged GraphPatch envelope, then call `resolve_workflow_branch_successor` with the exact successful result facts.
- Nested mutation: unique definitions may be edited directly; reused definitions require explicit `shared_definition` acknowledgement of every affected instance. Instance-only copy-on-write detach remains unsupported and must fail closed.
- New empty-canvas workflow: use the same two tools; no preliminary inspection is necessary.
- Selected/deictic edit: declare an existing selector with `selected=true`; do not separately read selection.
- Parameter or layout change: express an existing-node update in the semantic request; GraphPatch applies it atomically with structural changes.
- Add or rewire part of a graph: describe all desired edges in one semantic refinement request. Whole-branch clone/replace/remove uses the branch-specific flow above. Both routes preserve every undeclared sibling edge.
- Attach a chat image: include the trusted attachment binding in the compiler request; never separately place the image.
- Adjust prompt text: call `update_connected_prompt` exactly once with a new stable opaque `operation_id`. Use `replace` when supplying a complete desired prompt. Treat an exclusive-subject or focus correction as a rewrite, never another appended clause: if the user supplies a complete replacement, send it with `replace` instead of accumulating it onto the old mixed prompt. Translate exclusion wording into a positive description of only the intended subject, never repeat or name the negated subject, and express the boundary neutrally as “Preserve all unmasked pixels.” Use `append`, `prepend`, or literal `remove_exact` for ordinary relative edits so untouched existing text stays private and preserved. Call `view_prompt_reference_image` once when the current request cites `image2`, `image_2`, or a reference character, then set `reference_image_used=true` and pass its server-issued `prompt_context_token` unchanged into the update. If that inspection does not explicitly return the current prompt, preserve it by appending or prepending a strong character-identity clause rather than fabricating a full replacement, except for the exclusive-subject rewrite above. Never invent, shorten, omit, or reuse the token for another context. A corrective follow-up that does not itself cite `image2` or a reference uses only `update_connected_prompt`; a literal retry may reuse the immediately prior prompt-reference lane. These tools resolve the connected image/prompt producers; never use an image socket label as a node ID or invoke a workflow compiler/planner for a prompt-only edit. Only retry an unknown transport outcome with the exact same arguments and operation ID; never reuse it for changed arguments or a classified failure.
- Draw or revise a mask: `view_node_mask` inspects the exact source image and its current alpha/mask state; it does not assume a mask already exists, and an empty mask is a valid first-paint state. When the correct source is already bound, use `view_node_mask`, `edit_node_mask`, then `confirm_mask_review`, giving each mutation a distinct new stable opaque `operation_id`. Prefer normalized coordinates; if deriving pixel coordinates from a transport-scaled preview, multiply them by the returned `originalSize/previewSize` ratio before editing. For a reference-driven prompt-and-mask request use this exact order: `view_prompt_reference_image`, `update_connected_prompt`, `view_node_mask`, `edit_node_mask`, `confirm_mask_review`; the prompt edit changes the graph hash, so mask inspection must happen afterward. Stop immediately on the first classified failed step and do not call remaining lane tools or invoke a planner/diagnostic detour. Only retry an unknown transport outcome with the exact same arguments and operation ID; recovery must reuse the same pending review token or approved receipt, never upload, commit, or queue twice. For an actual chat attachment, inspect/place the full-resolution attachment before inspecting the newly bound mask source. Never pass socket labels such as `image_1` or `image_2` as node IDs, never paint a stale source, and do not repeat a successful inspection while the graph is unchanged. A requested revision must inspect the latest pending mask before painting it with a new operation ID. Human mask confirmation remains mandatory.
- Diagnose a classified unsupported schema: in a later focused request, use local node details/schema tools. Do not mutate through low-level tools in that failed run.
- Read-only explanation with no requested change: `workflow_overview`, `workflow_diagram`, `workflow_get_current_json`, selection, values, or slots remain appropriate.
- Manual layout or mutation tools are legacy compatibility/focused diagnostics, not the default workflow-building path.
- Debug execution: `queue_workflow`, `wait`, `get_execution_history`, `get_execution_details`, and error-buffer tools. Give every intentional `queue_workflow` run a new stable opaque `operation_id`; reuse it only for an identical unknown/lost-reply recovery. On `queue_outcome_unknown`, stop and do not queue again under a new ID.
- Check assets/models: `comfy_models_list`, `comfy_list_folders`, `comfy_read_file`, `extract_workflow_from_image`.
- Find custom node packs: `manager_search_nodes`, `manager_v4_node_mappings`, `custom_nodes_search`.

## Workflow Rules

- Never assume the graph is valid because it looks connected. Verify required slots.
- Treat nodes as rectangles, not points, whenever an exact layout is requested.
- Never silently choose among equally valid local node classes; return the compiler's choice gate.
- Never use stale node knowledge, Registry metadata, or web results as build authority. Only the refreshed local `/object_info` catalog can authorize a node class/schema.
- Verified exact-schema lessons may rank a route internally, but they never authorize a stale class, port, value, or connection.
- Never replay a reconstructed or edited apply envelope. Pass the exact compiler result unchanged.
- Never reuse a branch ID after a mutation without the attested successor result or fresh discovery.
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
