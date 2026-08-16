# Ren intelligence regression — forensic report and recovery record

**Date:** 2026-08-14
**Trigger:** user-reported degradation in Ren's reliability, specifically that
tool calls were failing when asking it to add/update a prompt or to
analyze/inspect an image, plus a working checkout that had drifted far enough
from `origin/main` that `git status` alone made the situation look like a
much bigger emergency than it actually was.

This document is the durable record of what was actually wrong, what was
fixed, and what changes so it doesn't happen the same way again. It is
committed into the repository deliberately, rather than left in a private
planning note, so the acknowledgment stays attached to the project.

## Summary

Two independent things had gone wrong at the same time, and they compounded
each other:

1. **A real, shipped architectural defect**: three separate generations of
   the tool Ren uses to mutate a workflow graph were all registered and
   offered to the model simultaneously, with no reliable way for it to know
   which one was current.
2. **A real, unreleased regression in the newest local feature work**: two
   concrete bugs in brand-new (uncommitted, never-reviewed) code broke the
   exact two things the user reported — editing a prompt, and inspecting a
   reference image — on ordinary phrasing and ordinary workflows.

Separately, the checkout itself had accumulated ~24,000 lines of uncommitted
changes that looked, at first glance, like a second, competing rewrite of
work already merged upstream. It wasn't. Investigation showed the
overwhelming majority of that diff was byte-identical to content already
reviewed and merged on `origin/main`; the checkout's branch pointer had
simply never been advanced. The real local-only delta, once measured against
the correct baseline, was 47 files and net +17,230/-644 lines — a genuine,
coherent feature (mask targeting, narrow-edit idempotency, execution
provenance, queue-operation tracking), not chaos.

## Findings

### Finding 1 — Three live generations of the graph-mutation tool (already on `origin/main`)

Across three merged waves of work, three separate tool-pairs for mutating the
workflow graph ended up registered in `backend/mcp_server.py` at once:

- **Generation 1** (oldest, still legitimately needed elsewhere):
  `compile_workflow_spec` / `resolve_workflow_spec` / `plan_workflow` /
  `apply_workflow_plan` (`backend/workflow_planner.py`). Not dead code —
  `workflow_compiler.py` calls into it directly, and it's the documented
  fallback path for a classified unsupported schema.
- **Generation 2** (`plan_workflow_refinement` / `apply_workflow_refinement`,
  `backend/workflow_refinement.py`) — fully superseded by Generation 3, with
  zero callers outside its own pair, and named nowhere in the system prompt.
  Pure dead weight sitting in the default tool set.
- **Generation 3** (current): `compile_workflow_refinement_spec` /
  `apply_workflow_graph_patch`, plus the branch-navigation tool family.

`backend/chat_runtime.py`'s `CORE_CHAT_TOOLS` set contained all three
generations at once, including the fully-dead Generation 2 pair. A regex
heuristic (`workflow_graph_change_requested`) only narrowed the candidate set
down to Generation 3 when it fired unambiguously; on any message where it
didn't, Ren was handed up to 8 overlapping graph-mutation tools spanning all
three generations — a genuine, mechanistic source of tool-selection
confusion, confirmed by direct execution, not speculation.

The most telling piece of evidence: `backend/chat_prompt.md` already
contained an author-written instruction — *"do not call ... legacy
compiler/planner ... tools before or after them"* — that never named which
tools were legacy. Someone already knew Generation 1/2 were a hazard and
tried to route around them with prose instead of removing them from the
default toolset. The fix was left half-done, and it shipped to `origin/main`
in that state.

### Finding 2 — A missing dependency silently zeroed out the test suite

`backend/mask_compositor.py` imports `aiohttp`, which was declared in neither
`pyproject.toml` nor as a direct dependency this plugin's own test venv had
installed (it was present in `requirements.txt`'s dev section only as
`pytest`/`pytest-asyncio`/`pytest-cov`, never as `aiohttp`, and `aiohttp`
itself wasn't declared anywhere at all). Because `__init__.py` imports
`mask_compositor` unconditionally, and pytest's package-collection step
imports `__init__.py` before any test runs, this one undeclared dependency
took the entire suite down at collection — every one of 1,145 tests errored,
not just mask-related ones. Running the identical code from inside ComfyUI's
own Python environment (which happens to already have `aiohttp`, since
ComfyUI's core web server depends on it) showed a clean 1155/1155 pass. Same
code, two completely different verdicts, purely depending on which
environment ran the tests. That divergence — no single canonical, fully
declared test environment — is itself part of why this went unnoticed.

### Finding 3 — Two confirmed bugs directly matching the user's reported symptoms

These were the direct cause of "tool calls fail when I try to add a prompt or
inspect an image," found by targeted follow-up investigation after the
initial report, and confirmed by actually executing the code, not by
inspection alone. Both lived in genuinely new, **local-only, never-merged**
code — unlike Finding 1, neither of these was on `origin/main`.

**3a — Reference-image tracing broke on ComfyUI's own native resize nodes.**
`resolve_reference_image_producer` (`backend/mask_targeting.py`) only walked
upstream through an intermediate node if it recognized the node's class as a
safe passthrough, checked via the substring `"scaleimage"`. ComfyUI's actual
built-in resize nodes are named `ImageScale` and `ImageScaleBy`, which
normalize to `"imagescale"`/`"imagescaleby"` — the substring check had the
words in the wrong order and never matched. Any workflow with the extremely
common `LoadImage → Upscale Image → reference input` chain silently failed
reference-image resolution, breaking both "look at/use that reference image"
requests and any prompt update based on a reference image. The existing test
for this code path only covered one custom node whose name happened to
contain "resize" — never the native ComfyUI classes.

**3b — A regex heuristic misread "add a prompt to this node" as node
construction.** `update_connected_prompt` is only offered to the model when
`prompt_value_edit_requested()` returns true, which it doesn't if
`explicit_topology_change_requested()` also returns true for the same
message. That second check matched a mutation verb (`add`, `create`, ...)
followed by a graph noun (`node`, `edge`, ...) anywhere within 100
characters — with no way to distinguish "add a **node**" (construction) from
"add a prompt **to this node**" (a value edit referencing an existing node
as its target). The entire tool set collapsed to just the graph-compiler
pair, and `update_connected_prompt` was never offered to the model at all —
directly contradicting `chat_prompt.md`'s own explicit rule that a prompt
edit is a value edit, not a topology edit.

Two smaller issues were found in the same pass: `_graph_compiler_optional_tools`'s
inspection-verb list didn't recognize "check" or "look at," so a combined
request like "add coverage to the mask on this node and check the output"
silently dropped `view_output_image`; and `_derive_prompt_update` raised a
bare exception for an ambiguous `remove_exact` match or a removal that would
leave the prompt empty, instead of the same graceful structured response
already used one function up for ambiguous prompt-widget selection.

### Finding 4 — A real bug in the newest frontend feature, and a test-harness gap that hid it

Four tests in `tests/js/workflow_graph_patch_scoped_apply.test.mjs` failed
with `frontend_normalization_timeout`, each waiting out a real 5-10 second
timeout. The actual cause was in the *test harness*: `web/js/fl_api.js`'s
node-normalization polling loop checks whether
`globalThis.requestAnimationFrame` exists to decide if a frame was
observable, and this particular test's sandboxed harness never mocked
`requestAnimationFrame`/`document`/`cancelAnimationFrame` — unlike the
sibling root-level test, which already did. Every polling turn reported "no
frame observed," the loop's stability counter reset every time, and it never
converged before the timeout. This specific failure mode cannot happen in an
actual browser, so it was a test-infrastructure gap, not a shipped defect —
but it's exactly the kind of gap that made it easy to miss a real, separate
problem (the newest scoped-apply feature had never actually been verified
green).

### Finding 5 — Process: no size discipline, and the rulebook was itself mid-edit

The project has been built almost entirely by autonomous coding-agent
sessions. Individual merged commits were already enormous before this
investigation — some over 25,000 lines in a single commit, larger than
several entire prior PRs combined — meaning whole features were generated
and landed in one shot rather than incrementally, in a way no human review
could realistically keep up with. `CONTRIBUTING.md` had no size, ownership,
or merge-gate rules at all. `skills/workflow-assistant/SKILL.md` — the
document meant to govern the assistant's own operating discipline — was
itself sitting mid-edit and uncommitted in the same working tree it was
supposed to constrain. Two separately-written planning documents
(`docs/branch-navigation-plan.md`, `docs/semantic-workflow-intelligence-plan.md`)
describe two structurally different vocabularies for adjacent graph-editing
problems with no shared glossary between them — a sign that successive
sessions weren't cross-checking each other's design decisions before
building on top of them.

CI (`.github/workflows/ci.yml`) already runs the full Python and JS suites
on every push and PR, and would already have caught the missing-`aiohttp`
collection failure and the README tool-count drift the moment this code was
actually pushed through it. The gap wasn't inadequate CI — it was that this
entire body of work sat uncommitted, locally, and never reached CI at all.

## What was done about it

All of the following is recorded in git history on
`recovery/sync-with-origin-main`; commit messages there carry the full
technical detail.

1. **Preserve, then resync.** Before any other change, the complete
   uncommitted working tree was committed to a rescue branch and
   permanently tagged (`pre-recovery-snapshot-2026-08-14`) so nothing could
   be lost. The real local-only delta was then rebased directly onto
   `origin/main`. Two silent merge-duplication artifacts (git's 3-way merge
   inserting the same convergent addition twice without flagging a
   conflict) were caught by full-suite verification and removed.
2. **Closed the dependency/environment gap.** `aiohttp` is now declared in
   `pyproject.toml` and `requirements.txt`; `pytest-asyncio`/`pytest-cov`
   are declared in a `[dependency-groups] dev` group; this repo's own
   `.venv` is documented as the canonical test environment.
3. **Fixed the two bugs behind the actual reported symptoms** (Finding 3a
   and 3b above), with regression tests covering the native ComfyUI resize
   node classes and the "add a prompt to this node" phrasing.
4. **Consolidated the tool generations.** Generation 2
   (`plan_workflow_refinement`/`apply_workflow_refinement`) was removed from
   the default tool set. Generation 1 stays live (it's still needed
   elsewhere), and the vague "legacy compiler/planner" prompt language was
   replaced with the actual tool names, in both the system prompt and the
   assistant skill file. Full deletion of Generation 2's registration and
   its dead helper code is intentionally deferred to a separate, later
   commit, since `workflow_refinement.py` still exports shared
   infrastructure other modules import.
5. **Fixed the scoped-apply test-harness gap** (Finding 4), bringing the JS
   suite to 328/328.
6. **This document**, plus guardrails added to `CONTRIBUTING.md`: dependency
   declaration discipline, running `git fetch && git status` before starting
   new work, a rough commit-size ceiling, and — aimed directly at Finding
   1 — never shipping a new generation of an existing tool without retiring
   the old one in the same change.

Final verification: Python test suite 1166/1166 passing (was 1155, plus 11
new regression tests); JS test suite 328/328 passing (was 323/328).

## What's intentionally not done here

- Generation 2's dead code (`workflow_refinement.py`'s
  `plan_workflow_refinement`/`apply_workflow_refinement` registration and
  their exclusive helpers) is still registered in `mcp_server.py`. It's
  unreachable from the chat UI now, but the shared infrastructure it also
  exports (`NormalizedGraphSnapshot`, `normalize_workflow_graph`, etc.) is
  still imported by `workflow_graph_patch.py`, `workflow_scope.py`, and
  `workflow_branch_operations.py`. Extracting the shared parts and deleting
  the rest deserves its own reviewable commit rather than being folded into
  this recovery.
- The two independently-written architecture docs (Finding 5) were not
  reconciled into one shared vocabulary. That's a real gap, but rewriting
  either document risks losing detail neither author of the other document
  had access to; it should be done deliberately, not as a side effect of an
  unrelated recovery.
