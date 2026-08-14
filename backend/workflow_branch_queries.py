"""Read-only resolution and comparison over canonical workflow branch catalogs.

This module intentionally owns no graph mutation, browser command, or queue path.
It consumes :mod:`backend.workflow_branches` catalogs, validates all three live
pins, and returns bounded deterministic evidence.  Serialized widget and schema
content is represented only by SHA-256 digests; raw values are never copied into
the public models.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)
from workflow_branches import (
    WORKFLOW_BRANCH_CATALOG_HASH_SCHEMA,
    BranchEdgeFact,
    BranchScopeStep,
    WorkflowBranchCatalog,
    WorkflowBranchRecord,
    WorkflowBranchScope,
    discover_workflow_branches,
)
from workflow_schema_capabilities import (
    MaterializedInput,
    NodeSchemaCapabilities,
    infer_dynamic_selector_values,
    materialize_inputs,
    normalize_node_schema,
)

WORKFLOW_BRANCH_QUERY_SCHEMA = "fl-mcp.workflow-branch-query.v1"
WORKFLOW_BRANCH_RESOLUTION_SCHEMA = "fl-mcp.workflow-branch-resolution.v1"
WORKFLOW_BRANCH_COMPARISON_SCHEMA = "fl-mcp.workflow-branch-comparison.v1"
WORKFLOW_BRANCH_CONTENT_DIGEST_SCHEMA = "fl-mcp.workflow-branch-content-digest.v1"
WORKFLOW_BRANCH_SCHEMA_DIGEST_SCHEMA = "fl-mcp.workflow-branch-schema-digest.v1"

NodeId: TypeAlias = StrictInt | StrictStr
BranchKind: TypeAlias = Literal["segment", "split_arm", "isolated"]
ResolutionStatus: TypeAlias = Literal[
    "resolved",
    "needs_choice",
    "listed",
    "not_found",
    "stale",
    "invalid_catalog",
]

_GENERIC_QUERY_TERMS = frozenset({"a", "an", "branch", "chain", "path", "the"})
_QUERY_TOKEN = re.compile(r"[a-z0-9]+")
_SENSITIVE_FIELD = re.compile(
    r"(?:access[\s_.-]*key|api[\s_.-]*key|auth|bearer|client[\s_.-]*secret|"
    r"credential|password|private[\s_.-]*key|secret|token)",
    re.IGNORECASE,
)
_REDACTED_VALUE_MARKER = "__fl_mcp_redacted_unclassified_widget_value__"
_REDACTED_SCHEMA_MARKER = "__fl_mcp_redacted_sensitive_schema_field__"


def _canonical_value(value: Any) -> Any:
    """Normalize JSON numbers without ever converting values to display text."""

    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(nested) for nested in value]
    return value


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _typed_id(value: int | str) -> dict[str, Any]:
    return {
        "kind": "int" if isinstance(value, int) and not isinstance(value, bool) else "str",
        "value": value,
    }


def _id_key(value: int | str) -> tuple[str, str]:
    typed = _typed_id(value)
    return typed["kind"], json.dumps(typed["value"], ensure_ascii=False, separators=(",", ":"))


def _scope_key(scope_path: Sequence[BranchScopeStep]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (*_id_key(step.container_node_id), step.subgraph_id)
        for step in scope_path
    )


class BranchQueryIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Literal["error"] = "error"
    code: str = Field(..., min_length=1, max_length=128)
    path: str = Field(..., min_length=1, max_length=512)
    message: str = Field(..., min_length=1, max_length=1_024)


def _issue(code: str, path: str, message: str) -> BranchQueryIssue:
    return BranchQueryIssue(code=code, path=path, message=message)


class ExactBranchEndpointAnchor(BaseModel):
    """One typed node and, optionally, one complete physical slot identity.

    A node-only anchor supports containing/upstream/downstream discovery for
    interior nodes.  Slot anchoring is deliberately all-or-nothing: a partial
    slot description is not exact enough to authorize selection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: NodeId
    slot_index: StrictInt | None = Field(None, ge=0)
    slot_name: str | None = Field(None, min_length=1, max_length=256)
    type: str | None = Field(None, min_length=1, max_length=256)
    endpoint_role: Literal["source", "target"] | None = None

    @model_validator(mode="after")
    def validate_slot_identity(self) -> ExactBranchEndpointAnchor:
        values = (self.slot_index, self.slot_name, self.type)
        if any(item is not None for item in values) != all(item is not None for item in values):
            raise ValueError("slot_index, slot_name, and type must be supplied together")
        if self.endpoint_role is not None and self.slot_index is None:
            raise ValueError("endpoint_role requires an exact slot identity")
        return self

    @property
    def has_slot(self) -> bool:
        return self.slot_index is not None


class ResolveWorkflowBranchRequest(BaseModel):
    """Bounded conjunctive filters for one pinned branch catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal[WORKFLOW_BRANCH_QUERY_SCHEMA] = Field(
        WORKFLOW_BRANCH_QUERY_SCHEMA,
        alias="schema",
    )
    expected_workflow_identity: str = Field(..., min_length=1, max_length=512)
    expected_graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_id: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    scope_path: list[BranchScopeStep] | None = Field(None, max_length=32)
    endpoint_anchor: ExactBranchEndpointAnchor | None = None
    direction: Literal["containing", "upstream", "downstream"] = "containing"
    kinds: list[BranchKind] = Field(default_factory=list, max_length=3)
    writable: StrictBool | None = None
    query: str | None = Field(None, min_length=1, max_length=256)
    max_candidates: StrictInt = Field(20, ge=1, le=100)
    max_selectable_node_ids: StrictInt = Field(64, ge=1, le=512)
    max_label_chars: StrictInt = Field(160, ge=32, le=512)
    max_reachability_steps: StrictInt = Field(50_000, ge=1, le=500_000)

    @property
    def schema(self) -> Literal[WORKFLOW_BRANCH_QUERY_SCHEMA]:
        return self.schema_

    @model_validator(mode="after")
    def validate_filters(self) -> ResolveWorkflowBranchRequest:
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("kinds cannot contain duplicates")
        if self.endpoint_anchor is None and self.direction != "containing":
            raise ValueError("upstream/downstream direction requires an exact endpoint_anchor")
        return self


class BranchResolutionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    scope_path: list[BranchScopeStep] = Field(default_factory=list, max_length=32)
    kind: BranchKind
    label: str = Field(..., max_length=512)
    label_truncated: bool = False
    writable: bool
    reasons: list[str] = Field(default_factory=list, max_length=16)
    match_score: StrictInt = Field(..., ge=0)
    evidence: list[
        Literal[
            "exact_branch_id",
            "exact_label",
            "exact_node_class",
            "label_phrase",
            "node_class_phrase",
            "exact_node_title",
            "exact_workflow_alias",
            "node_title_phrase",
            "workflow_alias_phrase",
            "query_term",
            "exact_endpoint",
            "exact_node_anchor",
            "exact_scope",
            "kind_filter",
            "writable_filter",
        ]
    ] = Field(default_factory=list, max_length=16)
    selectable_node_ids: list[NodeId] = Field(default_factory=list, max_length=512)
    selectable_node_count: StrictInt = Field(..., ge=0)
    selectable_node_ids_truncated: bool = False


class WorkflowBranchResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal[WORKFLOW_BRANCH_RESOLUTION_SCHEMA] = Field(
        WORKFLOW_BRANCH_RESOLUTION_SCHEMA,
        alias="schema",
    )
    status: ResolutionStatus
    needs_choice: bool
    workflow_identity: str = Field(..., min_length=1, max_length=512)
    graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    selected: BranchResolutionCandidate | None = None
    candidates: list[BranchResolutionCandidate] = Field(default_factory=list, max_length=100)
    candidate_count: StrictInt = Field(..., ge=0)
    returned_candidate_count: StrictInt = Field(..., ge=0)
    omitted_candidate_count: StrictInt = Field(..., ge=0)
    candidates_truncated: bool
    issues: list[BranchQueryIssue] = Field(default_factory=list, max_length=32)
    read_only: Literal[True] = True
    queued: Literal[False] = False

    @property
    def schema(self) -> Literal[WORKFLOW_BRANCH_RESOLUTION_SCHEMA]:
        return self.schema_

    @model_validator(mode="after")
    def validate_result(self) -> WorkflowBranchResolutionResult:
        if self.needs_choice != (self.status == "needs_choice"):
            raise ValueError("needs_choice must match status")
        if (self.status == "resolved") != (self.selected is not None):
            raise ValueError("selected must be present exactly for resolved status")
        if self.returned_candidate_count != len(self.candidates):
            raise ValueError("returned_candidate_count must equal candidates length")
        if self.omitted_candidate_count != self.candidate_count - self.returned_candidate_count:
            raise ValueError("candidate truncation counts are inconsistent")
        if self.candidates_truncated != (self.omitted_candidate_count > 0):
            raise ValueError("candidates_truncated must match omitted_candidate_count")
        return self


def _catalog_hash(catalog: WorkflowBranchCatalog) -> str:
    scopes = sorted(
        catalog.scopes,
        key=lambda item: (len(item.scope.scope_path), item.scope_id),
    )
    return _canonical_hash(
        {
            "schema": WORKFLOW_BRANCH_CATALOG_HASH_SCHEMA,
            "workflow_identity": catalog.workflow_identity,
            "scopes": [
                {
                    "scope_id": scope.scope_id,
                    "branch_ids": sorted(branch.branch_id for branch in scope.branches),
                }
                for scope in scopes
            ],
        }
    )


def _catalog_integrity_issues(catalog: WorkflowBranchCatalog) -> list[BranchQueryIssue]:
    issues: list[BranchQueryIssue] = []
    if not catalog.valid:
        issues.append(
            _issue(
                "invalid_branch_catalog",
                "catalog.valid",
                "Branch discovery reported an invalid catalog.",
            )
        )
    if _catalog_hash(catalog) != catalog.branch_catalog_hash:
        issues.append(
            _issue(
                "branch_catalog_hash_mismatch",
                "catalog.branch_catalog_hash",
                "The catalog payload does not match its canonical branch catalog hash.",
            )
        )
    scope_ids: set[str] = set()
    scope_paths: set[tuple[tuple[str, str, str], ...]] = set()
    branch_ids: set[str] = set()
    for scope_index, scope in enumerate(catalog.scopes):
        if scope.scope_id in scope_ids:
            issues.append(
                _issue(
                    "duplicate_scope_id",
                    f"catalog.scopes[{scope_index}].scope_id",
                    "The catalog repeats one exact scope ID.",
                )
            )
        scope_ids.add(scope.scope_id)
        path_key = _scope_key(scope.scope.scope_path)
        if path_key in scope_paths:
            issues.append(
                _issue(
                    "duplicate_scope_path",
                    f"catalog.scopes[{scope_index}].scope.scope_path",
                    "The catalog repeats one exact typed scope path.",
                )
            )
        scope_paths.add(path_key)
        local_ids: set[str] = set()
        for branch_index, branch in enumerate(scope.branches):
            path = f"catalog.scopes[{scope_index}].branches[{branch_index}]"
            if branch.scope_id != scope.scope_id:
                issues.append(
                    _issue(
                        "branch_scope_mismatch",
                        f"{path}.scope_id",
                        "A branch scope ID differs from its enclosing scope.",
                    )
                )
            if branch.branch_id in branch_ids or branch.branch_id in local_ids:
                issues.append(
                    _issue(
                        "duplicate_branch_id",
                        f"{path}.branch_id",
                        "The catalog repeats one exact branch ID.",
                    )
                )
            branch_ids.add(branch.branch_id)
            local_ids.add(branch.branch_id)
        _, topology_issues = _scope_topology(scope)
        issues.extend(topology_issues)
    return issues


def _pin_issues(
    request: ResolveWorkflowBranchRequest | CompareWorkflowBranchesRequest,
    catalog: WorkflowBranchCatalog,
) -> list[BranchQueryIssue]:
    issues: list[BranchQueryIssue] = []
    if request.expected_workflow_identity != catalog.workflow_identity:
        issues.append(
            _issue(
                "stale_workflow_identity",
                "expected_workflow_identity",
                "The active workflow identity differs from the requested pin.",
            )
        )
    if request.expected_graph_hash != catalog.graph_hash:
        issues.append(
            _issue(
                "stale_graph_hash",
                "expected_graph_hash",
                "The active workflow graph differs from the requested pin.",
            )
        )
    if request.expected_branch_catalog_hash != catalog.branch_catalog_hash:
        issues.append(
            _issue(
                "stale_branch_catalog_hash",
                "expected_branch_catalog_hash",
                "The active branch catalog differs from the requested pin.",
            )
        )
    return issues


def _normalize_query(value: str) -> tuple[str, list[str]]:
    tokens = _QUERY_TOKEN.findall(value.casefold())
    terms = [item for item in tokens if item not in _GENERIC_QUERY_TERMS]
    return " ".join(terms), terms


def _query_score(
    branch: WorkflowBranchRecord,
    query: str,
    safe_labels: Sequence[tuple[Literal["title", "alias"], str]] = (),
) -> tuple[int, list[str]]:
    phrase, terms = _normalize_query(query)
    if not terms:
        return 0, []
    label = " ".join(_QUERY_TOKEN.findall(branch.label.casefold()))
    classes = sorted({" ".join(_QUERY_TOKEN.findall(node.node_type.casefold())) for node in branch.nodes})
    normalized_safe = [
        (kind, " ".join(_QUERY_TOKEN.findall(value.casefold())))
        for kind, value in safe_labels
        if value
    ]
    compact_phrase = phrase.replace(" ", "")
    score = 0
    evidence: list[str] = []
    if phrase == label:
        score += 1_000
        evidence.append("exact_label")
    if phrase in classes:
        score += 1_000
        evidence.append("exact_node_class")
    if phrase and (phrase in label or compact_phrase in label.replace(" ", "")):
        score += 500
        evidence.append("label_phrase")
    if any(
        phrase in node_class or compact_phrase in node_class.replace(" ", "")
        for node_class in classes
    ):
        score += 500
        evidence.append("node_class_phrase")
    for kind, value in normalized_safe:
        if phrase == value:
            score += 900
            evidence.append("exact_node_title" if kind == "title" else "exact_workflow_alias")
        elif phrase and (phrase in value or compact_phrase in value.replace(" ", "")):
            score += 450
            evidence.append("node_title_phrase" if kind == "title" else "workflow_alias_phrase")
    matched_terms = sum(
        any(
            term in field.replace(" ", "")
            for field in [label, *classes, *(value for _, value in normalized_safe)]
        )
        for term in terms
    )
    if matched_terms:
        score += 100 * matched_terms
        evidence.append("query_term")
    return score, sorted(set(evidence))


def _semantic_workflow_alias(node: Mapping[str, Any]) -> str | None:
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return None
    for key in (
        "fl_mcp_workflow_graph_patch",
        "fl_mcp_workflow_refinement",
        "fl_mcp_workflow_application",
    ):
        metadata = properties.get(key)
        if isinstance(metadata, Mapping):
            alias = metadata.get("alias")
            if isinstance(alias, str) and 0 < len(alias) <= 128:
                return alias
    return None


def _safe_branch_labels(
    workflow: Mapping[str, Any] | None,
    scope: WorkflowBranchScope,
    branch: WorkflowBranchRecord,
) -> list[tuple[Literal["title", "alias"], str]]:
    if workflow is None:
        return []
    payload, _ = _scope_payload_from_workflow(workflow, scope)
    if payload is None:
        return []
    nodes = _raw_nodes(payload)
    labels: set[tuple[Literal["title", "alias"], str]] = set()
    for node in branch.nodes:
        raw = nodes.get(_id_key(node.node_id))
        if raw is None:
            continue
        title = raw.get("title")
        if isinstance(title, str) and 0 < len(title) <= 512:
            labels.add(("title", title))
        alias = _semantic_workflow_alias(raw)
        if alias is not None:
            labels.add(("alias", alias))
    return sorted(labels)


def _source_matches(edge: BranchEdgeFact, anchor: ExactBranchEndpointAnchor) -> bool:
    if not anchor.has_slot:
        return _id_key(edge.source.node_id) == _id_key(anchor.node_id)
    assert anchor.slot_index is not None and anchor.slot_name is not None and anchor.type is not None
    return (
        _id_key(edge.source.node_id) == _id_key(anchor.node_id)
        and edge.source.output_index == anchor.slot_index
        and edge.source.output == anchor.slot_name
        and edge.source.type == anchor.type
    )


def _target_matches(edge: BranchEdgeFact, anchor: ExactBranchEndpointAnchor) -> bool:
    if not anchor.has_slot:
        return _id_key(edge.target.node_id) == _id_key(anchor.node_id)
    assert anchor.slot_index is not None and anchor.slot_name is not None and anchor.type is not None
    return (
        _id_key(edge.target.node_id) == _id_key(anchor.node_id)
        and edge.target.live_socket_index == anchor.slot_index
        and edge.target.input == anchor.slot_name
        and edge.target.type == anchor.type
    )


@dataclass(frozen=True)
class _ScopeTopology:
    nodes: frozenset[tuple[str, str]]
    edges: tuple[BranchEdgeFact, ...]
    edge_by_id: Mapping[str, BranchEdgeFact]
    incoming: Mapping[tuple[str, str], tuple[BranchEdgeFact, ...]]
    outgoing: Mapping[tuple[str, str], tuple[BranchEdgeFact, ...]]


def _edge_key(edge: BranchEdgeFact) -> tuple[Any, ...]:
    return (
        _id_key(edge.source.node_id),
        edge.source.output_index,
        edge.source.output,
        edge.source.type,
        _id_key(edge.target.node_id),
        edge.target.live_socket_index,
        edge.target.input,
        edge.target.type,
        edge.link_type,
        edge.edge_id,
    )


def _scope_topology(
    scope: WorkflowBranchScope,
) -> tuple[_ScopeTopology | None, list[BranchQueryIssue]]:
    issues: list[BranchQueryIssue] = []
    edge_by_id: dict[str, BranchEdgeFact] = {}
    edge_owner: dict[str, str] = {}
    nodes: set[tuple[str, str]] = set()
    node_facts: dict[tuple[str, str], tuple[str, str | None, bool]] = {}
    for branch_index, branch in enumerate(scope.branches):
        local_nodes: set[tuple[str, str]] = set()
        for node_index, node in enumerate(branch.nodes):
            key = _id_key(node.node_id)
            if key in local_nodes:
                issues.append(
                    _issue(
                        "duplicate_typed_node_fact",
                        f"scope.branches[{branch_index}].nodes[{node_index}]",
                        "A branch repeats one exact typed node ID.",
                    )
                )
            local_nodes.add(key)
            nodes.add(key)
            fact = (node.node_type, node.boundary_kind, node.selectable)
            if key in node_facts and node_facts[key] != fact:
                issues.append(
                    _issue(
                        "conflicting_typed_node_fact",
                        f"scope.branches[{branch_index}].nodes[{node_index}]",
                        "One typed node ID has conflicting class or boundary facts in its scope.",
                    )
                )
            node_facts[key] = fact
        if len({_id_key(item) for item in branch.selectable_node_ids}) != len(
            branch.selectable_node_ids
        ):
            issues.append(
                _issue(
                    "duplicate_selectable_node_id",
                    f"scope.branches[{branch_index}].selectable_node_ids",
                    "A branch repeats one exact selectable typed node ID.",
                )
            )
        if branch.kind != "segment":
            continue
        facts = {
            edge.edge_id: edge
            for edge in [
                *branch.entry_edges,
                *branch.exit_edges,
                *branch.internal_edges,
                *branch.cut_edges,
            ]
        }
        for edge_id in branch.edge_ids:
            edge = facts.get(edge_id)
            if edge is None:
                issues.append(
                    _issue(
                        "missing_segment_edge_fact",
                        f"scope.branches[{branch_index}].edge_ids",
                        "A primitive segment lacks a public fact for one member edge.",
                    )
                )
                continue
            if edge_id in edge_owner and edge_owner[edge_id] != branch.branch_id:
                issues.append(
                    _issue(
                        "segment_edge_partition_overlap",
                        f"scope.branches[{branch_index}].edge_ids",
                        "A physical edge belongs to more than one primitive segment.",
                    )
                )
            edge_owner[edge_id] = branch.branch_id
            existing = edge_by_id.get(edge_id)
            if existing is not None and _edge_key(existing) != _edge_key(edge):
                issues.append(
                    _issue(
                        "conflicting_edge_fact",
                        f"scope.branches[{branch_index}].edge_ids",
                        "One edge ID has conflicting physical endpoint facts.",
                    )
                )
            edge_by_id[edge_id] = edge
            nodes.add(_id_key(edge.source.node_id))
            nodes.add(_id_key(edge.target.node_id))
    if len(nodes) != scope.node_count:
        issues.append(
            _issue(
                "scope_node_count_mismatch",
                "scope.node_count",
                "The scope node count differs from its exact typed node facts.",
            )
        )
    if len(edge_by_id) != scope.edge_count:
        issues.append(
            _issue(
                "scope_edge_count_mismatch",
                "scope.edge_count",
                "The scope edge count differs from its primitive segment partition.",
            )
        )
    for branch_index, branch in enumerate(scope.branches):
        if len(branch.edge_ids) != len(set(branch.edge_ids)):
            issues.append(
                _issue(
                    "duplicate_branch_edge_id",
                    f"scope.branches[{branch_index}].edge_ids",
                    "A branch repeats one exact member edge ID.",
                )
            )
        missing = set(branch.edge_ids) - set(edge_by_id)
        if missing:
            issues.append(
                _issue(
                    "unknown_branch_edge_id",
                    f"scope.branches[{branch_index}].edge_ids",
                    "A branch references an edge absent from the primitive segment partition.",
                )
            )
        if branch.primary_entry_edge_id is not None and not any(
            edge.edge_id == branch.primary_entry_edge_id for edge in branch.entry_edges
        ):
            issues.append(
                _issue(
                    "primary_entry_edge_mismatch",
                    f"scope.branches[{branch_index}].primary_entry_edge_id",
                    "The primary entry edge is absent from the branch entry boundary.",
                )
            )
    if issues:
        return None, issues
    edges = tuple(sorted(edge_by_id.values(), key=_edge_key))
    incoming_lists: dict[tuple[str, str], list[BranchEdgeFact]] = {key: [] for key in nodes}
    outgoing_lists: dict[tuple[str, str], list[BranchEdgeFact]] = {key: [] for key in nodes}
    for edge in edges:
        outgoing_lists[_id_key(edge.source.node_id)].append(edge)
        incoming_lists[_id_key(edge.target.node_id)].append(edge)
    return (
        _ScopeTopology(
            nodes=frozenset(nodes),
            edges=edges,
            edge_by_id=edge_by_id,
            incoming={key: tuple(sorted(value, key=_edge_key)) for key, value in incoming_lists.items()},
            outgoing={key: tuple(sorted(value, key=_edge_key)) for key, value in outgoing_lists.items()},
        ),
        [],
    )


def _anchored_edges(
    topology: _ScopeTopology,
    anchor: ExactBranchEndpointAnchor,
) -> tuple[Literal["source", "target"] | None, tuple[BranchEdgeFact, ...], BranchQueryIssue | None]:
    source_edges = tuple(edge for edge in topology.edges if _source_matches(edge, anchor))
    target_edges = tuple(edge for edge in topology.edges if _target_matches(edge, anchor))
    if anchor.endpoint_role == "source":
        target_edges = ()
    elif anchor.endpoint_role == "target":
        source_edges = ()
    roles = int(bool(source_edges)) + int(bool(target_edges))
    if roles == 0:
        return None, (), None
    if roles > 1:
        return (
            None,
            (),
            _issue(
                "ambiguous_anchor_endpoint",
                "endpoint_anchor",
                "The exact slot identifies both an input and output endpoint; set endpoint_role.",
            ),
        )
    if source_edges:
        return "source", source_edges, None
    return "target", target_edges, None


def _reachable(
    topology: _ScopeTopology,
    initial: Sequence[tuple[str, str]],
    *,
    direction: Literal["upstream", "downstream"],
    max_steps: int,
) -> tuple[set[tuple[str, str]] | None, BranchQueryIssue | None]:
    reached: set[tuple[str, str]] = set()
    pending = sorted(set(initial))
    steps = 0
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        reached.add(key)
        steps += 1
        if steps > max_steps:
            return None, _issue(
                "reachability_limit_exceeded",
                "max_reachability_steps",
                "Branch reachability exceeded its explicit bounded work limit.",
            )
        adjacent = topology.incoming.get(key, ()) if direction == "upstream" else topology.outgoing.get(key, ())
        for edge in adjacent:
            steps += 1
            if steps > max_steps:
                return None, _issue(
                    "reachability_limit_exceeded",
                    "max_reachability_steps",
                    "Branch reachability exceeded its explicit bounded work limit.",
                )
            next_key = (
                _id_key(edge.source.node_id)
                if direction == "upstream"
                else _id_key(edge.target.node_id)
            )
            if next_key not in reached:
                pending.append(next_key)
    return reached, None


def _branch_direction_matches(
    branch: WorkflowBranchRecord,
    topology: _ScopeTopology,
    anchor_key: tuple[str, str],
    reached: set[tuple[str, str]],
    *,
    direction: Literal["upstream", "downstream"],
) -> bool:
    allowed = {*reached, anchor_key}
    owned = {_id_key(item) for item in branch.owned_node_ids}
    if owned & reached:
        return True
    for edge_id in branch.edge_ids:
        edge = topology.edge_by_id.get(edge_id)
        if edge is None:
            continue
        source = _id_key(edge.source.node_id)
        target = _id_key(edge.target.node_id)
        if source not in allowed or target not in allowed:
            continue
        if direction == "downstream" and target in reached:
            return True
        if direction == "upstream" and source in reached:
            return True
    return False


def _anchor_branch_ids(
    scope: WorkflowBranchScope,
    topology: _ScopeTopology,
    anchor: ExactBranchEndpointAnchor,
    *,
    direction: Literal["containing", "upstream", "downstream"],
    max_steps: int,
) -> tuple[set[str], BranchQueryIssue | None]:
    anchor_key = _id_key(anchor.node_id)
    if anchor_key not in topology.nodes:
        return set(), None
    role: Literal["source", "target"] | None = None
    anchored_edges: tuple[BranchEdgeFact, ...] = ()
    if anchor.has_slot:
        role, anchored_edges, issue = _anchored_edges(topology, anchor)
        if issue is not None or role is None:
            return set(), issue
    if direction == "containing":
        if anchored_edges:
            edge_ids = {edge.edge_id for edge in anchored_edges}
            return {
                branch.branch_id
                for branch in scope.branches
                if edge_ids.intersection(branch.edge_ids)
            }, None
        return {
            branch.branch_id
            for branch in scope.branches
            if any(_id_key(node.node_id) == anchor_key for node in branch.nodes)
        }, None

    initial: list[tuple[str, str]] = []
    if direction == "downstream":
        if role == "source":
            initial.extend(_id_key(edge.target.node_id) for edge in anchored_edges)
        else:
            initial.extend(
                _id_key(edge.target.node_id)
                for edge in topology.outgoing.get(anchor_key, ())
            )
            if role == "target":
                initial.append(anchor_key)
    else:
        if role == "target":
            initial.extend(_id_key(edge.source.node_id) for edge in anchored_edges)
        else:
            initial.extend(
                _id_key(edge.source.node_id)
                for edge in topology.incoming.get(anchor_key, ())
            )
            if role == "source":
                initial.append(anchor_key)
    reached, issue = _reachable(
        topology,
        initial,
        direction=direction,
        max_steps=max_steps,
    )
    if issue is not None or reached is None:
        return set(), issue
    return {
        branch.branch_id
        for branch in scope.branches
        if _branch_direction_matches(
            branch,
            topology,
            anchor_key,
            reached,
            direction=direction,
        )
    }, None


def _candidate_sort_key(
    item: tuple[WorkflowBranchScope, WorkflowBranchRecord, int, list[str]],
) -> tuple[Any, ...]:
    scope, branch, score, _ = item
    return (
        -score,
        len(scope.scope.scope_path),
        _scope_key(scope.scope.scope_path),
        {"split_arm": 0, "segment": 1, "isolated": 2}[branch.kind],
        branch.branch_id,
    )


def _public_candidate(
    scope: WorkflowBranchScope,
    branch: WorkflowBranchRecord,
    score: int,
    evidence: Sequence[str],
    request: ResolveWorkflowBranchRequest,
) -> BranchResolutionCandidate:
    node_ids = branch.selectable_node_ids[: request.max_selectable_node_ids]
    label = branch.label[: request.max_label_chars]
    return BranchResolutionCandidate(
        branch_id=branch.branch_id,
        branch_fingerprint=branch.branch_fingerprint,
        scope_id=branch.scope_id,
        scope_path=scope.scope.scope_path,
        kind=branch.kind,
        label=label,
        label_truncated=len(label) < len(branch.label),
        writable=branch.writable,
        reasons=branch.reasons[:16],
        match_score=score,
        evidence=list(evidence)[:16],
        selectable_node_ids=node_ids,
        selectable_node_count=len(branch.selectable_node_ids),
        selectable_node_ids_truncated=len(node_ids) < len(branch.selectable_node_ids),
    )


def _resolution_result(
    catalog: WorkflowBranchCatalog,
    *,
    status: ResolutionStatus,
    selected: BranchResolutionCandidate | None = None,
    candidates: Sequence[BranchResolutionCandidate] = (),
    candidate_count: int = 0,
    issues: Sequence[BranchQueryIssue] = (),
) -> WorkflowBranchResolutionResult:
    returned = len(candidates)
    return WorkflowBranchResolutionResult(
        schema=WORKFLOW_BRANCH_RESOLUTION_SCHEMA,
        status=status,
        needs_choice=status == "needs_choice",
        workflow_identity=catalog.workflow_identity,
        graph_hash=catalog.graph_hash,
        branch_catalog_hash=catalog.branch_catalog_hash,
        selected=selected,
        candidates=list(candidates),
        candidate_count=candidate_count,
        returned_candidate_count=returned,
        omitted_candidate_count=candidate_count - returned,
        candidates_truncated=candidate_count > returned,
        issues=list(issues),
    )


def resolve_workflow_branch(
    request: ResolveWorkflowBranchRequest | Mapping[str, Any],
    catalog: WorkflowBranchCatalog | Mapping[str, Any],
    *,
    workflow: Mapping[str, Any] | None = None,
    workflow_identity_attestation: str | None = None,
    workflow_graph_hash: str | None = None,
) -> WorkflowBranchResolutionResult:
    """Resolve at most one branch using exact filters and bounded positive evidence."""

    active_request = (
        request
        if isinstance(request, ResolveWorkflowBranchRequest)
        else ResolveWorkflowBranchRequest.model_validate(request)
    )
    active_catalog = (
        catalog
        if isinstance(catalog, WorkflowBranchCatalog)
        else WorkflowBranchCatalog.model_validate(catalog)
    )
    pin_issues = _pin_issues(active_request, active_catalog)
    if pin_issues:
        return _resolution_result(active_catalog, status="stale", issues=pin_issues)
    integrity_issues = _catalog_integrity_issues(active_catalog)
    if integrity_issues:
        return _resolution_result(
            active_catalog,
            status="invalid_catalog",
            issues=integrity_issues[:32],
        )
    if workflow is not None:
        if workflow_identity_attestation != active_catalog.workflow_identity:
            return _resolution_result(
                active_catalog,
                status="stale",
                issues=[
                    _issue(
                        "workflow_identity_unattested",
                        "workflow_identity_attestation",
                        "The optional workflow bytes are not bound to the pinned workflow identity.",
                    )
                ],
            )
        if workflow_graph_hash != active_catalog.graph_hash:
            return _resolution_result(
                active_catalog,
                status="stale",
                issues=[
                    _issue(
                        "workflow_graph_hash_unattested",
                        "workflow_graph_hash",
                        "The optional workflow bytes are not attested by the pinned graph hash.",
                    )
                ],
            )
        workflow_catalog = discover_workflow_branches(
            workflow,
            workflow_identity=active_catalog.workflow_identity,
            graph_hash=active_catalog.graph_hash,
        )
        if (
            not workflow_catalog.valid
            or workflow_catalog.branch_catalog_hash != active_catalog.branch_catalog_hash
        ):
            return _resolution_result(
                active_catalog,
                status="invalid_catalog",
                issues=[
                    _issue(
                        "workflow_catalog_mismatch",
                        "workflow",
                        "The optional workflow topology differs from the pinned branch catalog.",
                    )
                ],
            )

    requested_scope = (
        _scope_key(active_request.scope_path)
        if active_request.scope_path is not None
        else None
    )
    searched_scopes = [
        scope
        for scope in active_catalog.scopes
        if requested_scope is None or _scope_key(scope.scope.scope_path) == requested_scope
    ]
    anchor_branch_ids: dict[str, set[str]] = {}
    anchor_scope_ambiguous = False
    if active_request.endpoint_anchor is not None:
        for scope in searched_scopes:
            topology, topology_issues = _scope_topology(scope)
            if topology_issues or topology is None:
                return _resolution_result(
                    active_catalog,
                    status="invalid_catalog",
                    issues=topology_issues[:32],
                )
            branch_ids, anchor_issue = _anchor_branch_ids(
                scope,
                topology,
                active_request.endpoint_anchor,
                direction=active_request.direction,
                max_steps=active_request.max_reachability_steps,
            )
            if anchor_issue is not None:
                status: ResolutionStatus = (
                    "needs_choice"
                    if anchor_issue.code == "ambiguous_anchor_endpoint"
                    else "not_found"
                )
                return _resolution_result(
                    active_catalog,
                    status=status,
                    issues=[anchor_issue],
                )
            if branch_ids:
                anchor_branch_ids[scope.scope_id] = branch_ids
        if not anchor_branch_ids:
            return _resolution_result(
                active_catalog,
                status="not_found",
                issues=[
                    _issue(
                        "anchor_not_found",
                        "endpoint_anchor",
                        "No branch in the requested scope contains or is reachable from the exact typed anchor.",
                    )
                ],
            )
        anchor_scope_ambiguous = (
            active_request.scope_path is None and len(anchor_branch_ids) > 1
        )

    matches: list[tuple[WorkflowBranchScope, WorkflowBranchRecord, int, list[str]]] = []
    for scope in searched_scopes:
        for branch in scope.branches:
            evidence: list[str] = []
            score = 0
            if active_request.branch_id is not None:
                if branch.branch_id != active_request.branch_id:
                    continue
                score += 10_000
                evidence.append("exact_branch_id")
            if active_request.kinds:
                if branch.kind not in active_request.kinds:
                    continue
                evidence.append("kind_filter")
            if active_request.writable is not None:
                if branch.writable is not active_request.writable:
                    continue
                evidence.append("writable_filter")
            if requested_scope is not None:
                evidence.append("exact_scope")
            if active_request.endpoint_anchor is not None:
                if branch.branch_id not in anchor_branch_ids.get(scope.scope_id, set()):
                    continue
                score += 2_000
                evidence.append(
                    "exact_endpoint"
                    if active_request.endpoint_anchor.has_slot
                    else "exact_node_anchor"
                )
            if active_request.query is not None:
                query_score, query_evidence = _query_score(
                    branch,
                    active_request.query,
                    _safe_branch_labels(workflow, scope, branch),
                )
                if query_score <= 0:
                    continue
                score += query_score
                evidence.extend(query_evidence)
            matches.append((scope, branch, score, sorted(set(evidence))))

    matches.sort(key=_candidate_sort_key)
    if not matches:
        return _resolution_result(
            active_catalog,
            status="not_found",
            issues=[
                _issue(
                    "branch_not_found",
                    "query",
                    "No branch satisfies every exact filter with positive query evidence.",
                )
            ],
        )

    public_all = [
        _public_candidate(scope, branch, score, evidence, active_request)
        for scope, branch, score, evidence in matches
    ]
    public_returned = public_all[: active_request.max_candidates]
    has_semantic_evidence = any(
        (
            active_request.branch_id is not None,
            active_request.endpoint_anchor is not None,
            active_request.query is not None,
        )
    )
    if not has_semantic_evidence:
        return _resolution_result(
            active_catalog,
            status="listed",
            candidates=public_returned,
            candidate_count=len(public_all),
        )
    if anchor_scope_ambiguous:
        return _resolution_result(
            active_catalog,
            status="needs_choice",
            candidates=public_returned,
            candidate_count=len(public_all),
            issues=[
                _issue(
                    "ambiguous_anchor_scope",
                    "scope_path",
                    "The typed anchor exists in multiple graph scopes; choose one exact scope path.",
                )
            ],
        )
    top_score = matches[0][2]
    top_count = sum(score == top_score for _, _, score, _ in matches)
    if len(matches) == 1 or top_count == 1:
        return _resolution_result(
            active_catalog,
            status="resolved",
            selected=public_all[0],
            candidates=public_returned,
            candidate_count=len(public_all),
        )
    return _resolution_result(
        active_catalog,
        status="needs_choice",
        candidates=public_returned,
        candidate_count=len(public_all),
        issues=[
            _issue(
                "ambiguous_branch",
                "query",
                "Multiple branches have the same strongest evidence; choose an exact branch ID.",
            )
        ],
    )


class CompareWorkflowBranchesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_workflow_identity: str = Field(..., min_length=1, max_length=512)
    expected_graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    left_branch_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    right_branch_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    max_hashed_items: StrictInt = Field(64, ge=1, le=256)


class BranchClassCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["member", "owned", "boundary"]
    node_type: str = Field(..., min_length=1, max_length=256)
    count: StrictInt = Field(..., ge=1)


class BranchBoundaryFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: Literal["entry", "exit"]
    link_type: str = Field(..., min_length=1, max_length=256)
    source_node_type: str = Field(..., min_length=1, max_length=256)
    source_output_index: StrictInt = Field(..., ge=0)
    source_output: str = Field(..., min_length=1, max_length=256)
    source_type: str = Field(..., min_length=1, max_length=256)
    target_node_type: str = Field(..., min_length=1, max_length=256)
    target_input_index: StrictInt = Field(..., ge=0)
    target_input: str = Field(..., min_length=1, max_length=256)
    target_type: str = Field(..., min_length=1, max_length=256)
    count: StrictInt = Field(..., ge=1)


class HashedBranchItem(BaseModel):
    """A repeat-counted digest; it cannot serialize the hashed source value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_type: str = Field(..., min_length=1, max_length=256)
    fact_kind: Literal[
        "node_values",
        "node_schema",
        "dynamic_selector",
        "dynamic_group",
        "dynamic_input",
    ] = "node_values"
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    count: StrictInt = Field(..., ge=1)
    source_item_count: StrictInt = Field(..., ge=0)
    path_digest: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    value_digest: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    type_digest: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    cardinality: Literal["scalar", "list"] | None = None
    activation_state: Literal["active", "inactive", "conditional"] | None = None
    connected: bool | None = None


class BranchHashedFactSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["values", "schema", "dynamic"]
    status: Literal["available", "partial", "unavailable"]
    digest: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    items: list[HashedBranchItem] = Field(default_factory=list, max_length=256)
    item_count: StrictInt = Field(..., ge=0)
    returned_item_count: StrictInt = Field(..., ge=0)
    omitted_item_count: StrictInt = Field(..., ge=0)
    items_truncated: bool
    unavailable_reason: str | None = Field(None, max_length=256)

    @model_validator(mode="after")
    def validate_facts(self) -> BranchHashedFactSet:
        if self.returned_item_count != len(self.items):
            raise ValueError("returned_item_count must equal items length")
        if self.omitted_item_count != self.item_count - self.returned_item_count:
            raise ValueError("hashed fact truncation counts are inconsistent")
        if self.items_truncated != (self.omitted_item_count > 0):
            raise ValueError("items_truncated must match omitted_item_count")
        if self.status == "unavailable" and self.digest is not None:
            raise ValueError("unavailable facts cannot claim a digest")
        if self.status != "unavailable" and self.digest is None:
            raise ValueError("available or partial facts require a digest")
        return self


class BranchComparisonSide(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    scope_path: list[BranchScopeStep] = Field(default_factory=list, max_length=32)
    kind: BranchKind
    writable: bool
    member_node_count: StrictInt = Field(..., ge=0)
    owned_node_count: StrictInt = Field(..., ge=0)
    boundary_node_count: StrictInt = Field(..., ge=0)
    edge_count: StrictInt = Field(..., ge=0)
    classes: list[BranchClassCount] = Field(default_factory=list)
    boundaries: list[BranchBoundaryFact] = Field(default_factory=list)
    class_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    boundary_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    values: BranchHashedFactSet
    schema_facts: BranchHashedFactSet
    dynamic_facts: BranchHashedFactSet


class BranchComparisonDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: Literal[
        "kind",
        "topology",
        "classes",
        "boundary",
        "scope",
        "writable",
        "values",
        "schema",
        "dynamic",
    ]
    status: Literal["available", "unavailable"]
    equal: bool | None = None

    @model_validator(mode="after")
    def validate_dimension(self) -> BranchComparisonDimension:
        if self.status == "available" and self.equal is None:
            raise ValueError("available dimensions require an equality result")
        if self.status == "unavailable" and self.equal is not None:
            raise ValueError("unavailable dimensions cannot claim equality")
        return self


class WorkflowBranchComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal[WORKFLOW_BRANCH_COMPARISON_SCHEMA] = Field(
        WORKFLOW_BRANCH_COMPARISON_SCHEMA,
        alias="schema",
    )
    status: Literal["compared", "not_found", "stale", "invalid_catalog"]
    workflow_identity: str = Field(..., min_length=1, max_length=512)
    graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    left: BranchComparisonSide | None = None
    right: BranchComparisonSide | None = None
    dimensions: list[BranchComparisonDimension] = Field(default_factory=list, max_length=9)
    structurally_equal: bool | None = None
    value_equal: bool | None = None
    issues: list[BranchQueryIssue] = Field(default_factory=list, max_length=32)
    read_only: Literal[True] = True
    queued: Literal[False] = False

    @property
    def schema(self) -> Literal[WORKFLOW_BRANCH_COMPARISON_SCHEMA]:
        return self.schema_

    @model_validator(mode="after")
    def validate_result(self) -> WorkflowBranchComparisonResult:
        compared = self.status == "compared"
        if compared != (self.left is not None and self.right is not None):
            raise ValueError("comparison sides must be present exactly for compared status")
        if compared != (self.structurally_equal is not None):
            raise ValueError("structurally_equal must be present exactly for compared status")
        return self


def _node_class_counts(branch: WorkflowBranchRecord) -> list[BranchClassCount]:
    owned = {_id_key(item) for item in branch.owned_node_ids}
    boundary = {_id_key(item) for item in branch.boundary_node_ids}
    counts: Counter[tuple[str, str]] = Counter()
    for node in branch.nodes:
        key = _id_key(node.node_id)
        counts[("member", node.node_type)] += 1
        if key in owned:
            counts[("owned", node.node_type)] += 1
        if key in boundary:
            counts[("boundary", node.node_type)] += 1
    role_order = {"member": 0, "owned": 1, "boundary": 2}
    return [
        BranchClassCount(role=role, node_type=node_type, count=count)
        for (role, node_type), count in sorted(
            counts.items(),
            key=lambda item: (role_order[item[0][0]], item[0][1]),
        )
    ]


def _boundary_facts(branch: WorkflowBranchRecord) -> list[BranchBoundaryFact]:
    node_types = {_id_key(node.node_id): node.node_type for node in branch.nodes}
    counts: Counter[tuple[Any, ...]] = Counter()
    for direction, edges in (("entry", branch.entry_edges), ("exit", branch.exit_edges)):
        for edge in edges:
            key = (
                direction,
                edge.link_type,
                node_types.get(_id_key(edge.source.node_id), "__unknown__"),
                edge.source.output_index,
                edge.source.output,
                edge.source.type,
                node_types.get(_id_key(edge.target.node_id), "__unknown__"),
                edge.target.live_socket_index,
                edge.target.input,
                edge.target.type,
            )
            counts[key] += 1
    return [
        BranchBoundaryFact(
            direction=key[0],
            link_type=key[1],
            source_node_type=key[2],
            source_output_index=key[3],
            source_output=key[4],
            source_type=key[5],
            target_node_type=key[6],
            target_input_index=key[7],
            target_input=key[8],
            target_type=key[9],
            count=count,
        )
        for key, count in sorted(counts.items(), key=lambda item: item[0])
    ]


def _unavailable_facts(category: Literal["values", "schema", "dynamic"], reason: str) -> BranchHashedFactSet:
    return BranchHashedFactSet(
        category=category,
        status="unavailable",
        item_count=0,
        returned_item_count=0,
        omitted_item_count=0,
        items_truncated=False,
        unavailable_reason=reason,
    )


def _hashed_facts(
    category: Literal["values", "schema"],
    raw_items: Sequence[tuple[str, str, int]],
    *,
    complete: bool,
    max_items: int,
    reason: str | None = None,
) -> BranchHashedFactSet:
    counts: Counter[tuple[str, str, int]] = Counter(raw_items)
    items_all = [
        HashedBranchItem(
            node_type=node_type,
            fact_kind="node_values" if category == "values" else "node_schema",
            digest=digest,
            source_item_count=source_count,
            count=count,
        )
        for (node_type, digest, source_count), count in sorted(counts.items())
    ]
    digest = _canonical_hash(
        {
            "schema": (
                WORKFLOW_BRANCH_CONTENT_DIGEST_SCHEMA
                if category == "values"
                else WORKFLOW_BRANCH_SCHEMA_DIGEST_SCHEMA
            ),
            "items": [item.model_dump(mode="json") for item in items_all],
            "complete": complete,
        }
    )
    returned = items_all[:max_items]
    return BranchHashedFactSet(
        category=category,
        status="available" if complete else "partial",
        digest=digest,
        items=returned,
        item_count=len(items_all),
        returned_item_count=len(returned),
        omitted_item_count=len(items_all) - len(returned),
        items_truncated=len(returned) < len(items_all),
        unavailable_reason=reason,
    )


def _definition_map(workflow: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    definitions = workflow.get("definitions")
    if not isinstance(definitions, Mapping):
        return {}
    raw = definitions.get("subgraphs", [])
    if isinstance(raw, Mapping):
        iterable = raw.items()
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        iterable = ((None, item) for item in raw)
    else:
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for mapping_id, item in iterable:
        if not isinstance(item, Mapping):
            continue
        definition_id = item.get("id", mapping_id)
        if isinstance(definition_id, str) and definition_id:
            result[definition_id] = item
    return result


def _raw_nodes(payload: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    raw = payload.get("nodes", [])
    if isinstance(raw, Mapping):
        iterable = raw.items()
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        iterable = ((None, item) for item in raw)
    else:
        return {}
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for mapping_id, item in iterable:
        if not isinstance(item, Mapping):
            continue
        node_id = item.get("id", mapping_id)
        if isinstance(node_id, bool) or not isinstance(node_id, (int, str)):
            continue
        result[_id_key(node_id)] = item
    return result


def _scope_payload_from_workflow(
    workflow: Mapping[str, Any],
    scope: WorkflowBranchScope,
) -> tuple[Mapping[str, Any] | None, str | None]:
    payload: Mapping[str, Any] = workflow
    definitions = _definition_map(workflow)
    for step in scope.scope.scope_path:
        raw_node = _raw_nodes(payload).get(_id_key(step.container_node_id))
        if raw_node is None:
            return None, "scope_container_missing"
        node_type = raw_node.get("type") or raw_node.get("comfyClass") or raw_node.get("class_type")
        if node_type != step.subgraph_id:
            return None, "scope_container_type_mismatch"
        definition = definitions.get(step.subgraph_id)
        if definition is None:
            return None, "subgraph_definition_missing"
        payload = definition
    return payload, None


def _connected_inputs(raw_node: Mapping[str, Any]) -> set[str]:
    raw = raw_node.get("inputs", [])
    values = raw.values() if isinstance(raw, Mapping) else raw if isinstance(raw, list) else []
    result: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        links = item.get("links")
        if item.get("link") is not None or (
            isinstance(links, Sequence)
            and not isinstance(links, (str, bytes, bytearray))
            and bool(links)
        ):
            result.add(item["name"])
    return result


def _sensitive_capability(capability: Any) -> bool:
    if capability.hidden_kind == "auth":
        return True
    if _SENSITIVE_FIELD.search(f"{capability.name} {capability.path}"):
        return True
    if _SENSITIVE_FIELD.search(json.dumps(capability.declared_type, default=str)):
        return True
    return _metadata_contains_sensitive_evidence(capability.metadata)


def _metadata_contains_sensitive_evidence(
    value: Any,
    *,
    max_items: int = 10_000,
) -> bool:
    """Fail closed when schema metadata names or describes credential state."""

    stack = [value]
    visited: set[int] = set()
    inspected = 0
    while stack:
        current = stack.pop()
        if isinstance(current, (Mapping, list, tuple)):
            marker = id(current)
            if marker in visited:
                continue
            visited.add(marker)
        inspected += 1
        if inspected > max_items:
            return True
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if _SENSITIVE_FIELD.search(str(key)):
                    return True
                stack.append(nested)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str) and _SENSITIVE_FIELD.search(current):
            return True
    return False


def _value_contains_sensitive_key(value: Any) -> bool:
    """Detect credential-shaped keys before any nested value can be hashed."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_FIELD.search(str(key)):
                return True
            if _value_contains_sensitive_key(nested):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_value_contains_sensitive_key(item) for item in value)
    return False


def _schema_valid_widget_value(capability: Any, value: Any) -> bool:
    """Validate only schema shapes whose value domain is deterministic."""

    if capability.enum_options:
        if capability.metadata.get("multiselect") is True:
            return isinstance(value, list) and all(
                item in capability.enum_options for item in value
            )
        return value in capability.enum_options
    declared = capability.declared_type
    if declared == "BOOLEAN":
        return isinstance(value, bool)
    if declared == "FLOAT":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if declared == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "STRING":
        return isinstance(value, str)
    if capability.kind == "dynamic_selector":
        return bool(capability.enum_options) and value in capability.enum_options
    if capability.default.available:
        default = capability.default.value
        if isinstance(default, bool):
            return isinstance(value, bool)
        if isinstance(default, int) and not isinstance(default, bool):
            return isinstance(value, int) and not isinstance(value, bool)
        if isinstance(default, float):
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        if isinstance(default, str):
            return isinstance(value, str)
    return False


def _capability_context(
    node_type: str,
    raw_node: Mapping[str, Any],
    schema_mapping: Mapping[str, Any],
) -> tuple[
    NodeSchemaCapabilities | None,
    tuple[MaterializedInput, ...],
    tuple[MaterializedInput, ...],
    dict[str, Any],
    list[Any],
    set[str],
    bool,
]:
    schema = schema_mapping.get(node_type)
    raw_values = raw_node.get("widgets_values", [])
    if not isinstance(schema, Mapping) or not isinstance(raw_values, list):
        return None, (), (), {}, [], set(), False
    try:
        capabilities = normalize_node_schema(node_type, schema)
        connected = _connected_inputs(raw_node)
        selectors = infer_dynamic_selector_values(
            capabilities,
            raw_values,
            connected_inputs=connected,
        )
        materialized = materialize_inputs(
            capabilities,
            values=selectors,
            connected_inputs=connected,
        )
    except (TypeError, ValueError):
        return None, (), (), {}, [], set(), False
    widgets = [
        item
        for item in materialized
        if item.capability.widget and not item.capability.hidden
    ]
    exact = len(widgets) == len(raw_values) and capabilities.classification.status == "supported"
    return capabilities, materialized, tuple(widgets), selectors, raw_values, connected, exact


def _value_facts(
    workflow: Mapping[str, Any] | None,
    scope: WorkflowBranchScope,
    branch: WorkflowBranchRecord,
    *,
    schema_mapping: Mapping[str, Any] | None,
    max_items: int,
) -> BranchHashedFactSet:
    if workflow is None:
        return _unavailable_facts("values", "workflow_not_provided")
    if schema_mapping is None:
        return _unavailable_facts("values", "schema_mapping_not_provided")
    payload, reason = _scope_payload_from_workflow(workflow, scope)
    if payload is None:
        return _unavailable_facts("values", reason or "scope_unavailable")
    nodes = _raw_nodes(payload)
    node_facts = {_id_key(node.node_id): node for node in branch.nodes}
    raw_items: list[tuple[str, str, int]] = []
    complete = True
    for node_id in branch.owned_node_ids:
        node_fact = node_facts.get(_id_key(node_id))
        if node_fact is not None and node_fact.boundary_kind is not None:
            continue
        raw_node = nodes.get(_id_key(node_id))
        if raw_node is None or node_fact is None:
            complete = False
            continue
        capabilities, _, widgets, _, raw_values, _, exact = _capability_context(
            node_fact.node_type,
            raw_node,
            schema_mapping,
        )
        if capabilities is None:
            complete = False
            continue
        facts: list[dict[str, Any]] = []
        for index, value in enumerate(raw_values):
            if index >= len(widgets):
                complete = False
                facts.append({"value": _REDACTED_VALUE_MARKER, "reason": "unaligned"})
                continue
            capability = widgets[index].capability
            if _sensitive_capability(capability) or _value_contains_sensitive_key(value):
                complete = False
                facts.append({"value": _REDACTED_VALUE_MARKER, "reason": "sensitive"})
                continue
            if not _schema_valid_widget_value(capability, value):
                complete = False
                facts.append({"value": _REDACTED_VALUE_MARKER, "reason": "invalid"})
                continue
            facts.append(
                {
                    "path": capability.path,
                    "kind": capability.kind,
                    "value": value,
                }
            )
        complete = complete and exact
        raw_items.append(
            (
                node_fact.node_type,
                _canonical_hash({"schema": WORKFLOW_BRANCH_CONTENT_DIGEST_SCHEMA, "facts": facts}),
                len(raw_values),
            )
        )
    return _hashed_facts(
        "values",
        raw_items,
        complete=complete,
        max_items=max_items,
        reason=None if complete else "sensitive_or_unaligned_widget_values_redacted",
    )


def _dynamic_facts(
    workflow: Mapping[str, Any] | None,
    scope: WorkflowBranchScope,
    branch: WorkflowBranchRecord,
    *,
    schema_mapping: Mapping[str, Any] | None,
    max_items: int,
) -> BranchHashedFactSet:
    if workflow is None:
        return _unavailable_facts("dynamic", "workflow_not_provided")
    if schema_mapping is None:
        return _unavailable_facts("dynamic", "schema_mapping_not_provided")
    payload, reason = _scope_payload_from_workflow(workflow, scope)
    if payload is None:
        return _unavailable_facts("dynamic", reason or "scope_unavailable")
    nodes = _raw_nodes(payload)
    node_facts = {_id_key(node.node_id): node for node in branch.nodes}
    items: list[HashedBranchItem] = []
    complete = True
    for node_id in branch.owned_node_ids:
        node_fact = node_facts.get(_id_key(node_id))
        if node_fact is None or node_fact.boundary_kind is not None:
            continue
        raw_node = nodes.get(_id_key(node_id))
        if raw_node is None:
            complete = False
            continue
        capabilities, materialized, _, selectors, _, connected, exact = _capability_context(
            node_fact.node_type,
            raw_node,
            schema_mapping,
        )
        if capabilities is None:
            complete = False
            continue
        complete = complete and exact
        for capability_path, value in sorted(selectors.items()):
            capability = next(
                (item for item in capabilities.inputs if item.path == capability_path),
                None,
            )
            sensitive = capability is None or _sensitive_capability(capability)
            complete = complete and not sensitive
            path_value = _REDACTED_SCHEMA_MARKER if sensitive else capability_path
            value_payload = _REDACTED_VALUE_MARKER if sensitive else value
            payload_fact = {
                "kind": "dynamic_selector",
                "path": path_value,
                "value": value_payload,
            }
            items.append(
                HashedBranchItem(
                    node_type=node_fact.node_type,
                    fact_kind="dynamic_selector",
                    digest=_canonical_hash(payload_fact),
                    count=1,
                    source_item_count=1,
                    path_digest=_canonical_hash({"path": path_value}),
                    value_digest=_canonical_hash({"path": path_value, "value": value_payload}),
                )
            )
        for group in capabilities.dynamic_groups:
            sensitive = bool(_SENSITIVE_FIELD.search(group.path))
            complete = complete and not sensitive
            path_value = _REDACTED_SCHEMA_MARKER if sensitive else group.path
            payload_fact = {
                "kind": group.kind,
                "path": path_value,
                "slot_type": _schema_shape(group.slot_type),
                "minimum": group.minimum,
                "maximum": group.maximum,
            }
            items.append(
                HashedBranchItem(
                    node_type=node_fact.node_type,
                    fact_kind="dynamic_group",
                    digest=_canonical_hash(payload_fact),
                    count=1,
                    source_item_count=len(group.generated_inputs) + len(group.options),
                    path_digest=_canonical_hash({"path": path_value}),
                    type_digest=_canonical_hash({"type": _schema_shape(group.slot_type)}),
                )
            )
        for materialized_input in materialized:
            capability = materialized_input.capability
            if not capability.activation and capability.kind not in {"dynamic_selector", "dynamic_slot"}:
                continue
            sensitive = _sensitive_capability(capability)
            complete = complete and not sensitive
            path_value = _REDACTED_SCHEMA_MARKER if sensitive else capability.path
            payload_fact = {
                "kind": capability.kind,
                "path": path_value,
                "type": _schema_shape(capability.declared_type),
                "cardinality": capability.cardinality,
                "activation": materialized_input.activation_state,
                "connected": capability.path in connected or capability.name in connected,
            }
            items.append(
                HashedBranchItem(
                    node_type=node_fact.node_type,
                    fact_kind="dynamic_input",
                    digest=_canonical_hash(payload_fact),
                    count=1,
                    source_item_count=1,
                    path_digest=_canonical_hash({"path": path_value}),
                    type_digest=_canonical_hash({"type": _schema_shape(capability.declared_type)}),
                    cardinality=capability.cardinality,
                    activation_state=materialized_input.activation_state,
                    connected=capability.path in connected or capability.name in connected,
                )
            )
    ordered = sorted(items, key=lambda item: json.dumps(item.model_dump(mode="json"), sort_keys=True))
    returned = ordered[:max_items]
    digest = _canonical_hash(
        {
            "schema": WORKFLOW_BRANCH_CONTENT_DIGEST_SCHEMA,
            "category": "dynamic",
            "items": [item.model_dump(mode="json") for item in ordered],
            "complete": complete,
        }
    )
    return BranchHashedFactSet(
        category="dynamic",
        status="available" if complete else "partial",
        digest=digest,
        items=returned,
        item_count=len(ordered),
        returned_item_count=len(returned),
        omitted_item_count=len(ordered) - len(returned),
        items_truncated=len(returned) < len(ordered),
        unavailable_reason=None if complete else "dynamic_alignment_or_sensitive_fact_redacted",
    )


def _schema_shape(value: Any) -> Any:
    """Return deterministic schema shape without defaults, options, or credentials."""

    if isinstance(value, Mapping):
        shaped: dict[str, Any] = {}
        sensitive_present = False
        for raw_key, nested in value.items():
            key = str(raw_key)
            if _SENSITIVE_FIELD.search(key):
                sensitive_present = True
                continue
            shaped[key] = _schema_shape(nested)
        if sensitive_present:
            shaped[_REDACTED_SCHEMA_MARKER] = True
        return shaped
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_schema_shape(item) for item in value]
    if value is None:
        return {"scalar_type": "null"}
    if isinstance(value, bool):
        return {"scalar_type": "bool"}
    if isinstance(value, (int, float)):
        return {"scalar_type": "number"}
    if isinstance(value, str):
        return {"scalar_type": "string"}
    return {"scalar_type": type(value).__name__}


def _schema_facts(
    schema_mapping: Mapping[str, Any] | None,
    branch: WorkflowBranchRecord,
    *,
    max_items: int,
) -> BranchHashedFactSet:
    if schema_mapping is None:
        return _unavailable_facts("schema", "schema_mapping_not_provided")
    owned = {_id_key(item) for item in branch.owned_node_ids}
    node_types = sorted(
        {
            node.node_type
            for node in branch.nodes
            if _id_key(node.node_id) in owned and node.boundary_kind is None
        }
    )
    raw_items: list[tuple[str, str, int]] = []
    complete = True
    for node_type in node_types:
        if node_type not in schema_mapping:
            complete = False
            continue
        try:
            digest = _canonical_hash(_schema_shape(schema_mapping[node_type]))
        except (TypeError, ValueError):
            complete = False
            continue
        raw_items.append((node_type, digest, 1))
    return _hashed_facts(
        "schema",
        raw_items,
        complete=complete,
        max_items=max_items,
        reason=None if complete else "one_or_more_node_schemas_unavailable",
    )


def _comparison_side(
    scope: WorkflowBranchScope,
    branch: WorkflowBranchRecord,
    *,
    workflow: Mapping[str, Any] | None,
    schema_mapping: Mapping[str, Any] | None,
    max_items: int,
) -> BranchComparisonSide:
    classes = _node_class_counts(branch)
    boundaries = _boundary_facts(branch)
    values = _value_facts(
        workflow,
        scope,
        branch,
        schema_mapping=schema_mapping,
        max_items=max_items,
    )
    schemas = _schema_facts(schema_mapping, branch, max_items=max_items)
    dynamics = _dynamic_facts(
        workflow,
        scope,
        branch,
        schema_mapping=schema_mapping,
        max_items=max_items,
    )
    return BranchComparisonSide(
        branch_id=branch.branch_id,
        branch_fingerprint=branch.branch_fingerprint,
        scope_id=branch.scope_id,
        scope_path=scope.scope.scope_path,
        kind=branch.kind,
        writable=branch.writable,
        member_node_count=len(branch.nodes),
        owned_node_count=len(branch.owned_node_ids),
        boundary_node_count=len(branch.boundary_node_ids),
        edge_count=len(branch.edge_ids),
        classes=classes,
        boundaries=boundaries,
        class_digest=_canonical_hash([item.model_dump(mode="json") for item in classes]),
        boundary_digest=_canonical_hash([item.model_dump(mode="json") for item in boundaries]),
        values=values,
        schema_facts=schemas,
        dynamic_facts=dynamics,
    )


def _comparison_result(
    catalog: WorkflowBranchCatalog,
    *,
    status: Literal["compared", "not_found", "stale", "invalid_catalog"],
    left: BranchComparisonSide | None = None,
    right: BranchComparisonSide | None = None,
    dimensions: Sequence[BranchComparisonDimension] = (),
    structurally_equal: bool | None = None,
    value_equal: bool | None = None,
    issues: Sequence[BranchQueryIssue] = (),
) -> WorkflowBranchComparisonResult:
    return WorkflowBranchComparisonResult(
        schema=WORKFLOW_BRANCH_COMPARISON_SCHEMA,
        status=status,
        workflow_identity=catalog.workflow_identity,
        graph_hash=catalog.graph_hash,
        branch_catalog_hash=catalog.branch_catalog_hash,
        left=left,
        right=right,
        dimensions=list(dimensions),
        structurally_equal=structurally_equal,
        value_equal=value_equal,
        issues=list(issues),
    )


def compare_workflow_branches(
    request: CompareWorkflowBranchesRequest | Mapping[str, Any],
    *,
    catalog: WorkflowBranchCatalog | Mapping[str, Any] | None = None,
    workflow: Mapping[str, Any] | None = None,
    workflow_identity_attestation: str | None = None,
    workflow_graph_hash: str | None = None,
    schema_mapping: Mapping[str, Any] | None = None,
) -> WorkflowBranchComparisonResult:
    """Compare two exact branch IDs without exposing serialized values or secrets.

    A caller may provide a previously discovered catalog or a raw workflow.  If
    no catalog is supplied, discovery is run with the request's exact identity
    and graph pins, then its canonical catalog hash must still match the request.
    """

    active_request = (
        request
        if isinstance(request, CompareWorkflowBranchesRequest)
        else CompareWorkflowBranchesRequest.model_validate(request)
    )
    if catalog is None:
        if workflow is None:
            raise ValueError("catalog or workflow is required")
        active_catalog = discover_workflow_branches(
            workflow,
            workflow_identity=active_request.expected_workflow_identity,
            graph_hash=active_request.expected_graph_hash,
        )
    else:
        active_catalog = (
            catalog
            if isinstance(catalog, WorkflowBranchCatalog)
            else WorkflowBranchCatalog.model_validate(catalog)
        )

    pin_issues = _pin_issues(active_request, active_catalog)
    if pin_issues:
        return _comparison_result(active_catalog, status="stale", issues=pin_issues)
    integrity_issues = _catalog_integrity_issues(active_catalog)
    if integrity_issues:
        return _comparison_result(
            active_catalog,
            status="invalid_catalog",
            issues=integrity_issues[:32],
        )
    if workflow is not None:
        if workflow_identity_attestation != active_catalog.workflow_identity:
            return _comparison_result(
                active_catalog,
                status="stale",
                issues=[
                    _issue(
                        "workflow_identity_unattested",
                        "workflow_identity_attestation",
                        "The optional workflow bytes are not bound to the pinned workflow identity.",
                    )
                ],
            )
        if workflow_graph_hash != active_catalog.graph_hash:
            return _comparison_result(
                active_catalog,
                status="stale",
                issues=[
                    _issue(
                        "workflow_graph_hash_unattested",
                        "workflow_graph_hash",
                        "The optional workflow bytes are not attested by the pinned graph hash.",
                    )
                ],
            )
        workflow_catalog = discover_workflow_branches(
            workflow,
            workflow_identity=active_catalog.workflow_identity,
            graph_hash=active_catalog.graph_hash,
        )
        if (
            not workflow_catalog.valid
            or workflow_catalog.branch_catalog_hash != active_catalog.branch_catalog_hash
        ):
            return _comparison_result(
                active_catalog,
                status="invalid_catalog",
                issues=[
                    _issue(
                        "workflow_catalog_mismatch",
                        "workflow",
                        "The optional workflow topology differs from the pinned branch catalog.",
                    )
                ],
            )

    by_id: dict[str, tuple[WorkflowBranchScope, WorkflowBranchRecord]] = {
        branch.branch_id: (scope, branch)
        for scope in active_catalog.scopes
        for branch in scope.branches
    }
    missing: list[str] = []
    if active_request.left_branch_id not in by_id:
        missing.append("left_branch_id")
    if active_request.right_branch_id not in by_id:
        missing.append("right_branch_id")
    if missing:
        return _comparison_result(
            active_catalog,
            status="not_found",
            issues=[
                _issue(
                    "branch_not_found",
                    path,
                    "The exact branch ID is absent from the pinned catalog.",
                )
                for path in missing
            ],
        )

    left_scope, left_branch = by_id[active_request.left_branch_id]
    right_scope, right_branch = by_id[active_request.right_branch_id]
    left = _comparison_side(
        left_scope,
        left_branch,
        workflow=workflow,
        schema_mapping=schema_mapping,
        max_items=active_request.max_hashed_items,
    )
    right = _comparison_side(
        right_scope,
        right_branch,
        workflow=workflow,
        schema_mapping=schema_mapping,
        max_items=active_request.max_hashed_items,
    )
    value_equal = (
        left.values.digest == right.values.digest
        if left.values.status == "available" and right.values.status == "available"
        else None
    )
    schema_equal = (
        left.schema_facts.digest == right.schema_facts.digest
        if left.schema_facts.status == "available" and right.schema_facts.status == "available"
        else None
    )
    dynamic_equal = (
        left.dynamic_facts.digest == right.dynamic_facts.digest
        if left.dynamic_facts.status == "available"
        and right.dynamic_facts.status == "available"
        else None
    )
    dimensions = [
        BranchComparisonDimension(dimension="kind", status="available", equal=left.kind == right.kind),
        BranchComparisonDimension(
            dimension="topology",
            status="available",
            equal=left.branch_fingerprint == right.branch_fingerprint,
        ),
        BranchComparisonDimension(
            dimension="classes",
            status="available",
            equal=left.class_digest == right.class_digest,
        ),
        BranchComparisonDimension(
            dimension="boundary",
            status="available",
            equal=left.boundary_digest == right.boundary_digest,
        ),
        BranchComparisonDimension(
            dimension="scope",
            status="available",
            equal=_scope_key(left.scope_path) == _scope_key(right.scope_path),
        ),
        BranchComparisonDimension(
            dimension="writable",
            status="available",
            equal=left.writable == right.writable,
        ),
        BranchComparisonDimension(
            dimension="values",
            status="available" if value_equal is not None else "unavailable",
            equal=value_equal,
        ),
        BranchComparisonDimension(
            dimension="schema",
            status="available" if schema_equal is not None else "unavailable",
            equal=schema_equal,
        ),
        BranchComparisonDimension(
            dimension="dynamic",
            status="available" if dynamic_equal is not None else "unavailable",
            equal=dynamic_equal,
        ),
    ]
    structural_equal = all(
        dimension.equal is True
        for dimension in dimensions
        if dimension.dimension in {"kind", "topology", "classes", "boundary"}
    )
    return _comparison_result(
        active_catalog,
        status="compared",
        left=left,
        right=right,
        dimensions=dimensions,
        structurally_equal=structural_equal,
        value_equal=value_equal,
    )


__all__ = [
    "BranchBoundaryFact",
    "BranchClassCount",
    "BranchComparisonDimension",
    "BranchComparisonSide",
    "BranchHashedFactSet",
    "BranchQueryIssue",
    "BranchResolutionCandidate",
    "CompareWorkflowBranchesRequest",
    "ExactBranchEndpointAnchor",
    "HashedBranchItem",
    "ResolveWorkflowBranchRequest",
    "WORKFLOW_BRANCH_COMPARISON_SCHEMA",
    "WORKFLOW_BRANCH_CONTENT_DIGEST_SCHEMA",
    "WORKFLOW_BRANCH_QUERY_SCHEMA",
    "WORKFLOW_BRANCH_RESOLUTION_SCHEMA",
    "WORKFLOW_BRANCH_SCHEMA_DIGEST_SCHEMA",
    "WorkflowBranchComparisonResult",
    "WorkflowBranchResolutionResult",
    "compare_workflow_branches",
    "resolve_workflow_branch",
]
