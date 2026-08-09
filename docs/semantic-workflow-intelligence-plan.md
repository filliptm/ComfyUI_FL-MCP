# Semantic workflow intelligence plan

## Objective

Ren should turn a human workflow request into one catalog-, schema-, graph-, and
workflow-pinned GraphPatch without guessing node classes or dynamic port names.
When a direct connection is impossible, it may synthesize a short, locally
loaded conversion route only when that route is unique, supported, review-safe,
and allowed by the request.

The live ComfyUI `/object_info` schema remains the mutation authority. Registry
metadata, package documentation, generated summaries, and verified lessons may
improve discovery and ranking, but may never override the loaded schema.

All production decisions are derived from normalized schema facts and explicit
policy. Named nodes such as Seedance, CreateVideo, or GetVideoComponents are
acceptance fixtures only; production code must not contain class-name-specific
routing rules.

## Architecture

1. **Schema capabilities** normalize exact loaded inputs, outputs, widgets,
   dynamic activation rules, match types, list behavior, origin, authentication,
   cost, and side effects.
2. **Semantic endpoint resolution** maps bounded intent such as
   `reference image`, `IMAGE`, ordinal `0` to an exact active input such as
   `model.reference_images.image_1`.
3. **Capability profiles** describe source, sink, transform, splitter, combiner,
   loader, saver, and converter shapes from the normalized schema.
4. **Conversion hypergraph planning** searches at most two inferred local
   intermediaries. Multi-input converters such as `CreateVideo` retain their
   required supporting inputs instead of being treated as a simple type cast.
   Finite dynamic-selector branches are bounded, schema-derived variants whose
   exact selector values travel with the route.
5. **GraphPatch compilation** solves all selectors, dynamic slots, match types,
   widget conversions, correlated outputs, and final topology before mutation.
6. **Verified pattern ranking** uses only lessons whose exact source and target
   schema hashes remain active. Lessons are ranking priors, never authority.
7. **Atomic application** creates the canonical graph visibly, verifies every
   declared and preserved fact, never queues implicitly, and restores the exact
   snapshot on failure.

## Route policy

Routes are ranked lexicographically:

1. Direct compatible connection.
2. Previously verified exact-schema pattern.
3. One supported native converter.
4. One supported custom converter.
5. Two supported local converters.

Partner/API, deprecated, experimental, output-side-effect, or heavy generator
nodes are never inferred without explicit user intent and review. A tie produces
`needs_choice`; it never falls back to alphabetical selection. `exact`, `only`,
`no extra nodes`, or equivalent wording disables inferred nodes.

An implicit source may bind one required converter input. Optional ports are
left unconnected, and a second required port needs an explicit source mapping.
Independent sibling converters are canonicalized as one DAG rather than two
artificial sequence choices. Dynamic selector expansion is capped at 64 viable
variants per class and target-type combinations are capped at 64; overflow
fails with a classified diagnostic.

## Corrective diagnostics

Unknown or ambiguous ports return a bounded candidate list containing canonical
path, concrete type, dynamic group, ordinal, and selector prerequisites. The
compiler should not return a full catalog dump or invite repeated schema guesses.

## Acceptance gates

- Wavelet `IMAGE` resolves directly to Seedance
  `model.reference_images.image_1`; no `CreateVideo` is inserted.
- Two Seedance references allocate `image_1` then `image_2` deterministically.
- `VIDEO` to VHS resolves `GetVideoComponents` and preserves correlated images,
  audio, FPS, and bit depth.
- `IMAGE` frames plus FPS to a native `VIDEO` target may infer `CreateVideo`.
- Equal conversion routes return `needs_choice` and no apply request.
- Exact/no-extra requests never infer intermediaries.
- Verified lessons alter ranking only while both exact schemas remain active.
- Normal workflow changes use exactly compile plus apply, with no web, schema
  probing, queue, or post-build verification tool calls.
- The complete local catalog normalizes without crashing and reports supported,
  adapter-required, and unsupported coverage honestly.

## Implemented validation snapshot

- 612 Python tests and 185 JavaScript tests pass.
- A saved 1,701-class local catalog produces 1,980 bounded profiles with zero
  derivation issues in about 0.55 seconds.
- Direct `IMAGE` to `IMAGE` remains a zero-node route.
- The same catalog uniquely resolves `IMAGE` to `VIDEO` through native
  `CreateVideo` and `VIDEO` to the `IMAGE`/`AUDIO`/`FLOAT` bundle through native
  `GetVideoComponents`, each in about 0.01 seconds.
- Synthetic non-media fixtures cover one-hop, two-hop, multi-output, union-type,
  nested dynamic-selector, ambiguity, catalog-order, optional-input, repeated-
  input, scalar/list mapping, graph-wide effective cardinality, and exact/no-
  extra behavior without production node-name conditions.
