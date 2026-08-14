# Contributing to ComfyUI FL-MCP

Keep the project focused on being a reliable MCP server and ComfyUI bridge.

## Guidelines

- Prefer deterministic tool behavior over product-specific assistant behavior.
- Keep tools scoped and explicit about whether they are read-only or mutating.
- Guard file, git, Manager, workflow, and process mutations behind config flags.
- Keep the embedded assistant workflow-first, provider-neutral, and local by default; avoid product personas and required hosted services.
- Never store provider credentials in conversation or settings files.
- Add tests for new tool schemas, routing behavior, and safety gates.
- Any new third-party import lands in both `pyproject.toml` and `requirements.txt` in the same commit that introduces it. A dependency that's merely present transitively (e.g. because ComfyUI's own environment happens to have it) is not a substitute for declaring it.

## Running the test suite

Use this repo's own `.venv` (not ComfyUI's root environment) for development and testing — it is the canonical, isolated environment for this plugin's own test suite and should have every declared dependency installed via `uv pip install -r requirements.txt` (or `uv sync`). Running tests from ComfyUI's root environment instead can mask missing dependencies this plugin never actually declared, since ComfyUI's own dependencies are present there incidentally.

```
.venv/bin/python3 -m pytest tests/ -q
node --test tests/js/*.test.mjs
```

Both suites must be fully green (no collection errors, no skipped failures) before opening a PR.
