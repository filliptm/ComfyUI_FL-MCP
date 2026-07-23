# Contributing to ComfyUI FL-MCP

Keep the project focused on being a reliable MCP server and ComfyUI bridge.

## Guidelines

- Prefer deterministic tool behavior over product-specific assistant behavior.
- Keep tools scoped and explicit about whether they are read-only or mutating.
- Guard file, git, Manager, workflow, and process mutations behind config flags.
- Keep the embedded assistant workflow-first, provider-neutral, and local by default; avoid product personas and required hosted services.
- Never store provider credentials in conversation or settings files.
- Add tests for new tool schemas, routing behavior, and safety gates.
