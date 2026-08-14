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
- Run `git fetch && git status` before starting new work, and confirm your branch isn't behind the remote's default branch. Building on a stale base risks silently re-implementing something another session already finished and merged.
- Keep individual commits reviewable — a rough ceiling of 500-1000 changed lines without an explicit justification in the commit message. A feature that's naturally larger than that should land as a sequence of small, single-purpose commits rather than one unreviewable diff.
- Before adding a new "generation" of an existing tool, chat lane, or compiler (a second implementation of something a tool already does), retire or explicitly deprecate the old one in the same change. Never leave two live, undocumented, overlapping implementations of the same capability both reachable by the model at once — that is a direct, recurring cause of tool-selection confusion in this project's history (see `docs/2026-08-ren-regression-forensic-report.md`).

## Running the test suite

Use this repo's own `.venv` (not ComfyUI's root environment) for development and testing — it is the canonical, isolated environment for this plugin's own test suite and should have every declared dependency installed via `uv pip install -r requirements.txt` (or `uv sync`). Running tests from ComfyUI's root environment instead can mask missing dependencies this plugin never actually declared, since ComfyUI's own dependencies are present there incidentally.

```
.venv/bin/python3 -m pytest tests/ -q
node --test tests/js/*.test.mjs
```

Both suites must be fully green (no collection errors, no skipped failures) before opening a PR.
