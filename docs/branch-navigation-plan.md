# Branch navigation and scoped editing

## Objective

PR #35 teaches Ren to reason about workflow branches without adding a second
canvas mutation engine. Branch discovery, identity, comparison, and navigation
are new read-side capabilities. Clone, replace, and remove operations lower to
the existing catalog-, schema-, workflow-, and graph-pinned GraphPatch apply
path, which remains the only graph writer and never queues execution.

## Branch model

A branch is a deterministic graph region, not an arbitrary source-to-sink path.
The catalog exposes two related region kinds:

- **Segments** are maximal non-branching edge corridors between roots, sinks,
  splits, and merges. Every physical edge belongs to exactly one segment.
- **Arms** are the exclusive downstream region entered through one outgoing
  edge of a split, ending at the nearest shared reconvergence or at one or more
  terminals. Nested splits produce child arms; sibling arms never own the same
  node.

Shared split and merge nodes are boundaries rather than owned members. Every
record contains its exact entry, exit, internal, and cut edges. Regions with
multiple unexplained entries/exits, shared owned nodes, cycles, unresolved
endpoints, or unsupported scope facts remain discoverable but are not writable.

## Identity

Two hashes serve different purposes:

1. `branch_id` identifies one exact branch instance. It includes the workflow
   identity, typed recursive scope path, exact typed member and boundary node
   IDs, and exact endpoint topology. It excludes titles, layout, widget values,
   and unrelated sibling topology, so those changes do not rename the branch.
2. `branch_fingerprint` compares structure without relying on node IDs. It uses
   canonical local topology, node classes, port roles, and data types, while
   deliberately excluding widget values and schema-generation state. A clone
   therefore gets a new `branch_id` while retaining the same structural
   fingerprint. The compare API computes a separate, schema-scoped content
   digest from redacted value hashes and dynamic-input facts; that digest is
   never mutation authority and never exposes credentials or raw values.

Using a branch is still snapshot-safe: discovery returns the current workflow
identity and full graph hash, and every navigation or edit revalidates them.
A stable branch ID is not permission to apply against a stale canvas.

Numeric and string node IDs remain distinct in every identity and endpoint.
Titles, positions, serialized order, and generated labels are search evidence
only; they never authorize an edit. Symmetric semantic matches return bounded
candidates and perform no selection or mutation.

## Scope model

Root scope has an empty path. A nested path is an ordered list of exact
`{container_node_id, subgraph_id}` steps. Local node IDs are resolved only
inside that scope. Recursive definitions, missing instances, reused-definition
ambiguity, excessive depth, and cross-scope edges fail closed.

Discovery and navigation resolve nested instance paths read-only. Nested
mutation uses scoped GraphPatch v3: the full root workflow remains
the rollback/hash/idempotency envelope, while all node and edge operations are
bound to one resolved scope graph. Editing a shared definition must be explicit
because it can affect every instance. A definition with one reachable instance
can be edited directly. A reused definition requires explicit
`shared_definition` acknowledgement of every affected instance path; an
instance-only request fails closed until copy-on-write definition detachment is
implemented and independently validated. Virtual subgraph input/output
endpoints are typed boundaries, not ordinary nodes, and no connection is
synthesized across a scope boundary.

## Public behavior

- **Discover** returns bounded branch candidates, relationships, exact
  boundaries, writable status, and deterministic diagnostics.
- **Navigate** resolves exactly one branch, verifies workflow/hash/scope before
  UI effects, then atomically selects and focuses its exact local nodes.
- **Compare** is pure and reports topology, class/schema, safe value-hash,
  dynamic-selector/cardinality, and boundary differences.
- **Clone** uses fresh aliases/IDs, shares external sources by default, never
  duplicates risky partner/output/attachment behavior implicitly, and is
  limited to private regions with exactly reconstructable widgets and
  boundaries. External exits of a non-terminal/reconvergent clone are reported
  and left detached, so the shared merge target and siblings stay exact.
- **Replace** requires an explicit complete entry/exit boundary map and lowers
  every removal, creation, and reconnection to GraphPatch.
- **Remove** distinguishes terminal deletion from bypass. Bypass is automatic
  only for one unique compatible entry-to-exit mapping; otherwise it requires
  an explicit mapping or returns a choice.

Every branch edit asserts the complete incident-edge boundary. Shared members
cannot be removed through a child arm. Missing or ambiguous mappings fail
before the frontend is called.

After a successful apply, `resolve_workflow_branch_successor` re-attests the
exact application ledger, patch hash, aliases, workflow identity, final graph
hash, and current postconditions before rediscovering branches. Its deterministic
per-scope lineage maps each predecessor to zero, one, or several exact successor
IDs. A singular convenience ID is present only when the global result contains
exactly one successor. A verified removal returns an empty list only after the
predecessor is absent; incomplete or ambiguous coverage is a classified failure.

## Delivery order

1. Pure bounded branch catalog, stable IDs/fingerprints, relationships, and
   deterministic comparison for root and nested read-only scopes.
2. One exact frontend navigation primitive for select-and-focus under the
   existing canvas lock.
3. Root-scope clone, replace, and remove compilers that emit ordinary
   GraphPatch envelopes.
4. Scoped GraphPatch support for unique definitions and explicitly acknowledged
   shared-definition edits, with full-root rollback and outside-scope exact
   verification. Copy-on-write instance detachment remains a separate hardening
   phase rather than being approximated.
5. Post-apply successor lineage, natural-language routing, UI summaries,
   documentation, and live E2E.

## Acceptance gates

- Diamonds, split/merge arms, non-reconvergent terminals, nested diamonds,
  disconnected components, duplicate node types, and parallel slots produce
  deterministic regions independent of serialization order.
- Moving, resizing, retitling, changing a widget, or changing an unrelated
  sibling leaves the affected branch ID unchanged. Editing its topology changes
  the ID. Equivalent clones have different IDs and equal fingerprints.
- Ambiguous navigation selects nothing. Stale workflow, graph, or scope selects
  nothing. Successful navigation selects and focuses exactly the branch nodes.
- Replacing one branch changes only its declared nodes and edges. Every sibling
  node, edge, widget, rectangle, group, reroute, definition, and workflow field
  remains exact.
- Nested paths never confuse equal local IDs in different scopes. Shared
  definition edits disclose and attest all affected instances; an unsupported
  instance-only edit of a reused definition mutates nothing.
- Every failed mutation restores the complete root snapshot. Retry is
  idempotent. No branch tool searches the web, runs, or queues the workflow.
