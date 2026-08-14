"""Pure root-scope branch operations lowered to canonical GraphPatch.

This module is intentionally only a compiler.  It re-discovers the branch from
one pinned workflow snapshot, checks every branch/catalog/workflow precondition,
and delegates graph validation to the existing semantic compiler and GraphPatch
kernel.  It never calls the frontend and never queues or executes a workflow.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias

from node_library import classify_node_origin
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)
from workflow_branches import (
    BranchEdgeFact,
    BranchNodeFact,
    BranchScopeStep,
    WorkflowBranchCatalog,
    WorkflowBranchRecord,
    WorkflowBranchScope,
    discover_workflow_branches,
)
from workflow_capability_graph import build_capability_graph
from workflow_compiler import WorkflowSpecNode
from workflow_graph_patch import (
    GRAPH_PATCH_SCHEMA,
    SCOPED_GRAPH_PATCH_SCHEMA,
    ApplyGraphPatchRequest,
    ApplyScopedGraphPatchRequest,
    GraphPatchLayoutHint,
    GraphPatchPlan,
    GraphPatchScopeAuthority,
    NewNodeRef,
    PlanGraphPatchRequest,
    ScopedGraphPatchPlan,
    WorkflowGraphPatchApplyRequest,
    compile_graph_patch,
    compile_scoped_graph_patch,
    graph_patch_assertions_for_existing_region,
    scoped_graph_patch_request_from_private_plan,
    verify_completed_graph_patch_state,
)
from workflow_refinement import (
    NormalizedGraphEdge,
    NormalizedGraphSnapshot,
    normalize_workflow_graph,
)
from workflow_refinement_compiler import (
    CompileWorkflowRefinementSpecRequest,
    ExistingNodeSelector,
    RefinementSpecEdge,
    compile_workflow_refinement_spec,
)
from workflow_schema_capabilities import (
    infer_dynamic_selector_values,
    materialize_inputs,
    normalize_node_schema,
)
from workflow_scope import (
    SCOPE_INPUT_NODE_ID,
    SCOPE_INPUT_NODE_TYPE,
    SCOPE_OUTPUT_NODE_ID,
    SCOPE_OUTPUT_NODE_TYPE,
    WorkflowScopeError,
    WorkflowScopeProjection,
    project_workflow_scope,
    resolve_workflow_scope_edit,
)

WORKFLOW_BRANCH_OPERATION_SCHEMA = "fl-mcp.workflow-branch-operation.v1"
WORKFLOW_BRANCH_SUCCESSOR_SCHEMA = "fl-mcp.workflow-branch-successor.v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_APPLICATION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_BRANCH_OPERATION_BYTES = 1_048_576
_MAX_BRANCH_OPERATION_ITEMS = 100_000
_MAX_BRANCH_OPERATION_JSON_DEPTH = 64
_MAX_BRANCH_SCOPE_DEPTH = 32
_MAX_LINEAGE_PREDECESSOR_EDGES = 2_000
_SECRET_IDENTIFIER_TOKENS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_SECRET_COLLAPSED_IDENTIFIERS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "privatekey",
        "refreshtoken",
        "secretkey",
    }
)
_SECRET_TEXT_EVIDENCE = re.compile(
    r"(?:access[\s_.-]*key|api[\s_.-]*key|auth(?:entication|orization)?|"
    r"bearer|client[\s_.-]*secret|credential|password|private[\s_.-]*key|"
    r"secret|token)",
    re.IGNORECASE,
)
_CLONE_COSMETIC_PROPERTY_KEYS = frozenset(
    {
        "aux_id",
        "cnr_id",
        "ver",
    }
)
_GRAPH_PATCH_PROVENANCE_PROPERTY = "fl_mcp_workflow_graph_patch"
_GRAPH_PATCH_PROVENANCE_FIELDS = frozenset(
    {"schema", "application_id", "patch_hash", "alias", "schema_hash"}
)

NodeId: TypeAlias = StrictInt | StrictStr
Coordinate: TypeAlias = StrictInt | StrictFloat
CloneRisk = Literal["partner", "api", "output", "heavy", "opaque"]
BranchOperationScopePath: TypeAlias = Annotated[
    list[BranchScopeStep],
    Field(min_length=1, max_length=_MAX_BRANCH_SCOPE_DEPTH),
]


def _validate_branch_operation_request_budget(value: Any) -> None:
    """Bound an untrusted operation request before graph discovery or copying."""

    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    item_count = 0
    text_bytes = 0
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.discard(id(current))
            continue
        if depth > _MAX_BRANCH_OPERATION_JSON_DEPTH:
            raise ValueError("branch_operation_request_depth_exceeded")
        item_count += 1
        if item_count > _MAX_BRANCH_OPERATION_ITEMS:
            raise ValueError("branch_operation_request_item_limit_exceeded")
        if isinstance(current, Mapping):
            marker = id(current)
            if marker in active:
                raise ValueError("branch_operation_request_contains_cycle")
            active.add(marker)
            stack.append((current, depth, True))
            item_count += 2 * len(current)
            if item_count > _MAX_BRANCH_OPERATION_ITEMS:
                raise ValueError("branch_operation_request_item_limit_exceeded")
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise ValueError("branch_operation_request_has_non_string_key")
                try:
                    text_bytes += len(key.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise ValueError("branch_operation_request_has_invalid_utf8") from exc
                if text_bytes > _MAX_BRANCH_OPERATION_BYTES:
                    raise ValueError("branch_operation_request_too_large")
                stack.append((nested, depth + 1, False))
        elif isinstance(current, (list, tuple)):
            marker = id(current)
            if marker in active:
                raise ValueError("branch_operation_request_contains_cycle")
            active.add(marker)
            stack.append((current, depth, True))
            item_count += len(current)
            if item_count > _MAX_BRANCH_OPERATION_ITEMS:
                raise ValueError("branch_operation_request_item_limit_exceeded")
            stack.extend((nested, depth + 1, False) for nested in current)
        elif isinstance(current, str):
            try:
                text_bytes += len(current.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("branch_operation_request_has_invalid_utf8") from exc
            if text_bytes > _MAX_BRANCH_OPERATION_BYTES:
                raise ValueError("branch_operation_request_too_large")
        elif current is None or type(current) in {bool, int}:
            continue
        elif type(current) is float and math.isfinite(current):
            continue
        else:
            raise ValueError("branch_operation_request_contains_non_json_value")

    try:
        payload_size = len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("branch_operation_request_contains_non_json_value") from exc
    if payload_size > _MAX_BRANCH_OPERATION_BYTES:
        raise ValueError("branch_operation_request_too_large")


def _typed_key(value: int | str) -> tuple[str, str]:
    return (
        "int" if isinstance(value, int) and not isinstance(value, bool) else "str",
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _secret_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    tokens = re.findall(r"[A-Za-z0-9]+", camel_split.casefold())
    return bool(_SECRET_IDENTIFIER_TOKENS.intersection(tokens)) or "".join(
        tokens
    ) in _SECRET_COLLAPSED_IDENTIFIERS


def _contains_secret_mapping_key(
    value: Any,
    *,
    inspect_string_values: bool = False,
    max_items: int = 10_000,
) -> bool:
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
            for key, item in current.items():
                if _secret_identifier(key):
                    return True
                stack.append(item)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif (
            inspect_string_values
            and isinstance(current, str)
            and _SECRET_TEXT_EVIDENCE.search(current)
        ):
            return True
    return False


def _safe_empty_secret_default(value: Any) -> bool:
    if value is None or value == "" or value is False:
        return True
    if type(value) in {int, float}:
        return value == 0
    if isinstance(value, (Mapping, list, tuple)):
        return len(value) == 0
    return False


def _metadata_declares_attachment(value: Any, *, max_items: int = 10_000) -> bool:
    """Recognize schema-declared upload widgets without copying local file refs."""

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
            for key, item in current.items():
                collapsed = re.sub(r"[^a-z0-9]+", "", str(key).casefold())
                if item and (
                    collapsed == "upload"
                    or collapsed.endswith("upload")
                    or collapsed.startswith("upload")
                ):
                    return True
                stack.append(item)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return False


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    severity: Literal["error", "warning"] = "error",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }
    if details:
        result["details"] = dict(details)
    return result


def _compiler_issue(value: Mapping[str, Any]) -> dict[str, Any]:
    details = {
        key: item
        for key, item in value.items()
        if key not in {"severity", "code", "path", "message"}
    }
    return _issue(
        str(value.get("code") or "branch_lowering_failed"),
        str(value.get("path") or "compiler"),
        str(value.get("message") or "The branch operation could not be compiled."),
        severity="warning" if value.get("severity") == "warning" else "error",
        details=details,
    )


class BranchOperationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"]
    code: str = Field(..., min_length=1, max_length=128)
    path: str = Field(..., min_length=1, max_length=1_024)
    message: str = Field(..., min_length=1, max_length=4_096)
    details: dict[str, Any] = Field(default_factory=dict)


class BranchOperationPins(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(..., min_length=8, max_length=128, pattern=_APPLICATION_PATTERN)
    branch_id: str = Field(..., pattern=_SHA256_PATTERN)
    expected_workflow_identity: str = Field(..., min_length=1, max_length=512)
    expected_graph_hash: str = Field(..., pattern=_SHA256_PATTERN)
    expected_branch_catalog_hash: str = Field(..., pattern=_SHA256_PATTERN)
    scope_edit_mode: Literal["instance", "shared_definition"] = "instance"
    affected_scope_paths: list[BranchOperationScopePath] = Field(
        default_factory=list,
        max_length=8_192,
    )

    @model_validator(mode="before")
    @classmethod
    def validate_request_budget(cls, value: Any) -> Any:
        _validate_branch_operation_request_budget(value)
        return value

    @model_validator(mode="after")
    def validate_scope_edit_authority(self) -> BranchOperationPins:
        keys = [
            tuple(
                (*_typed_key(step.container_node_id), step.subgraph_id)
                for step in path
            )
            for path in self.affected_scope_paths
        ]
        if any(not path for path in self.affected_scope_paths):
            raise ValueError("affected_scope_paths cannot contain an empty path")
        if len(keys) != len(set(keys)):
            raise ValueError("affected_scope_paths must be duplicate-free")
        self.affected_scope_paths = [
            path
            for _, path in sorted(
                zip(keys, self.affected_scope_paths, strict=True),
                key=lambda item: (len(item[0]), item[0]),
            )
        ]
        if self.scope_edit_mode == "instance" and self.affected_scope_paths:
            raise ValueError(
                "affected_scope_paths are supplied only for shared_definition edits"
            )
        if self.scope_edit_mode == "shared_definition" and not self.affected_scope_paths:
            raise ValueError(
                "shared_definition edits require the complete affected_scope_paths set"
            )
        return self


class BranchLayoutOffset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: Coordinate = 80
    y: Coordinate = 80

    @model_validator(mode="after")
    def validate_offset(self) -> BranchLayoutOffset:
        if not all(math.isfinite(float(item)) for item in (self.x, self.y)):
            raise ValueError("layout offset must be finite")
        if abs(float(self.x)) > 100_000 or abs(float(self.y)) > 100_000:
            raise ValueError("layout offset must not exceed 100000")
        return self


class CloneBranchRequest(BranchOperationPins):
    operation: Literal["clone"] = "clone"
    layout_offset: BranchLayoutOffset = Field(default_factory=BranchLayoutOffset)
    acknowledged_risks: list[CloneRisk] = Field(default_factory=list, max_length=5)

    @field_validator("acknowledged_risks", mode="before")
    @classmethod
    def canonicalize_risks(cls, value: Any) -> Any:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return sorted(set(value))
        return value


class BranchEntryMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_edge_id: str = Field(..., pattern=_SHA256_PATTERN)
    target_alias: str = Field(..., min_length=1, max_length=64)
    target_input: str = Field(..., min_length=1, max_length=256)
    target_mode: Literal["auto", "slot", "convert_widget"] = "auto"


class BranchExitMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_edge_id: str = Field(..., pattern=_SHA256_PATTERN)
    source_alias: str = Field(..., min_length=1, max_length=64)
    source_output: str = Field(..., min_length=1, max_length=256)
    source_output_index: StrictInt | None = Field(None, ge=0)


class ReplaceBranchRequest(BranchOperationPins):
    operation: Literal["replace"] = "replace"
    replacement_nodes: list[WorkflowSpecNode] = Field(..., min_length=1, max_length=100)
    replacement_edges: list[RefinementSpecEdge] = Field(default_factory=list, max_length=2_000)
    entry_mappings: list[BranchEntryMapping] = Field(default_factory=list, max_length=2_000)
    exit_mappings: list[BranchExitMapping] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def validate_replacement(self) -> ReplaceBranchRequest:
        aliases = [item.alias for item in self.replacement_nodes]
        if len(aliases) != len(set(aliases)):
            raise ValueError("replacement node aliases must be unique")
        known = set(aliases)
        for edge in self.replacement_edges:
            if edge.source_alias not in known or edge.target_alias not in known:
                raise ValueError("replacement_edges may reference only replacement aliases")
        if any(item.target_alias not in known for item in self.entry_mappings):
            raise ValueError("entry mappings must target a replacement alias")
        if any(item.source_alias not in known for item in self.exit_mappings):
            raise ValueError("exit mappings must source a replacement alias")
        entry_ids = [item.entry_edge_id for item in self.entry_mappings]
        exit_ids = [item.exit_edge_id for item in self.exit_mappings]
        if len(entry_ids) != len(set(entry_ids)) or len(exit_ids) != len(set(exit_ids)):
            raise ValueError("every replacement boundary edge may be mapped only once")
        return self


class BranchBypassMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_edge_id: str = Field(..., pattern=_SHA256_PATTERN)
    exit_edge_id: str = Field(..., pattern=_SHA256_PATTERN)


class RemoveBranchRequest(BranchOperationPins):
    operation: Literal["remove"] = "remove"
    mode: Literal["delete", "bypass"] = "delete"
    bypass_mappings: list[BranchBypassMapping] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def validate_mode(self) -> RemoveBranchRequest:
        if self.mode == "delete" and self.bypass_mappings:
            raise ValueError("delete mode cannot contain bypass mappings")
        pairs = [
            (item.entry_edge_id, item.exit_edge_id)
            for item in self.bypass_mappings
        ]
        if len(pairs) != len(set(pairs)):
            raise ValueError("bypass mappings cannot repeat an exact edge pair")
        return self


BranchOperationRequest = Annotated[
    CloneBranchRequest | ReplaceBranchRequest | RemoveBranchRequest,
    Field(discriminator="operation"),
]
_REQUEST_ADAPTER = TypeAdapter(BranchOperationRequest)


class PendingSuccessorScopeLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_path: list[BranchScopeStep] = Field(default_factory=list, max_length=32)
    predecessor_branch_id: str = Field(..., pattern=_SHA256_PATTERN)
    predecessor_expectation: Literal["removed", "may_reclassify"]


class PendingSuccessorLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=_APPLICATION_PATTERN,
    )
    patch_hash: str = Field(..., pattern=_SHA256_PATTERN)
    expected_workflow_identity: str = Field(..., min_length=1, max_length=512)
    expected_branch_catalog_hash: str = Field(..., pattern=_SHA256_PATTERN)
    predecessor_branch_id: str = Field(..., pattern=_SHA256_PATTERN)
    scope_path: list[BranchScopeStep] = Field(default_factory=list, max_length=32)
    expected_state: Literal["created_region", "branch_removed"]
    created_aliases: list[str] = Field(default_factory=list, max_length=100)
    predecessor_edges: list[BranchEdgeFact] = Field(
        default_factory=list,
        max_length=_MAX_LINEAGE_PREDECESSOR_EDGES,
    )
    predecessor_owned_node_ids: list[NodeId] = Field(default_factory=list, max_length=5_000)
    scope_locators: list[PendingSuccessorScopeLocator] = Field(
        ...,
        min_length=1,
        max_length=8_192,
    )

    @model_validator(mode="after")
    def validate_locator(self) -> PendingSuccessorLocator:
        if len(self.created_aliases) != len(set(self.created_aliases)):
            raise ValueError("created_aliases must be duplicate-free")
        if any(_ALIAS_PATTERN.fullmatch(alias) is None for alias in self.created_aliases):
            raise ValueError("created_aliases contain an invalid alias")
        edge_ids = [edge.edge_id for edge in self.predecessor_edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("predecessor_edges must be duplicate-free")
        self.created_aliases.sort()
        self.predecessor_edges.sort(key=lambda edge: edge.edge_id)
        if len({_typed_key(item) for item in self.predecessor_owned_node_ids}) != len(
            self.predecessor_owned_node_ids
        ):
            raise ValueError("predecessor_owned_node_ids must be duplicate-free")
        self.predecessor_owned_node_ids.sort(key=_typed_key)
        self.scope_locators.sort(
            key=lambda item: (
                len(item.scope_path),
                tuple(
                    (*_typed_key(step.container_node_id), step.subgraph_id)
                    for step in item.scope_path
                ),
            )
        )
        return self


class BranchOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    schema_: Literal[WORKFLOW_BRANCH_OPERATION_SCHEMA] = Field(
        WORKFLOW_BRANCH_OPERATION_SCHEMA,
        alias="schema",
    )
    valid: bool
    operation: Literal["clone", "replace", "remove"]
    needs_choice: bool = False
    predecessor_branch_id: str = Field(..., pattern=_SHA256_PATTERN)
    successor_branch_id: None = None
    pending_successor_locator: PendingSuccessorLocator | None = None
    branch_catalog_hash: str | None = Field(None, pattern=_SHA256_PATTERN)
    patch_hash: str | None = Field(None, pattern=_SHA256_PATTERN)
    plan: GraphPatchPlan | ScopedGraphPatchPlan | None = None
    apply_request: ApplyGraphPatchRequest | ApplyScopedGraphPatchRequest | None = None
    expected_final: dict[str, Any] | None = None
    issues: list[BranchOperationIssue] = Field(default_factory=list)
    error_count: StrictInt = Field(..., ge=0)
    warning_count: StrictInt = Field(..., ge=0)
    queued: Literal[False] = False


class ResolveBranchSuccessorsRequest(BaseModel):
    """Read-only authority for resolving post-apply branch lineage."""

    model_config = ConfigDict(extra="forbid")

    apply_request: WorkflowGraphPatchApplyRequest
    pending_successor_locator: PendingSuccessorLocator
    expected_workflow_identity: str = Field(..., min_length=1, max_length=512)
    expected_graph_hash: str = Field(..., pattern=_SHA256_PATTERN)
    aliases: dict[str, NodeId] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def validate_request_budget(cls, value: Any) -> Any:
        _validate_branch_operation_request_budget(value)
        return value

    @model_validator(mode="after")
    def validate_authority(self) -> ResolveBranchSuccessorsRequest:
        locator = self.pending_successor_locator
        apply_request = self.apply_request
        if locator.application_id != apply_request.application_id:
            raise ValueError("pending locator application_id differs from apply_request")
        if locator.patch_hash != apply_request.patch_hash:
            raise ValueError("pending locator patch_hash differs from apply_request")
        if locator.expected_workflow_identity != self.expected_workflow_identity:
            raise ValueError("pending locator workflow identity differs from the result pin")
        if apply_request.plan.expected_workflow_identity != self.expected_workflow_identity:
            raise ValueError("apply_request workflow identity differs from the result pin")
        planned_aliases = {item.alias for item in apply_request.plan.create_nodes}
        if set(self.aliases) != planned_aliases:
            raise ValueError("aliases must exactly match the GraphPatch create aliases")
        if set(locator.created_aliases) != planned_aliases:
            raise ValueError("pending locator aliases must exactly match the GraphPatch plan")
        if any(_ALIAS_PATTERN.fullmatch(alias) is None for alias in self.aliases):
            raise ValueError("aliases contain an invalid alias")
        if any(type(node_id) not in {int, str} for node_id in self.aliases.values()):
            raise ValueError("aliases contain an invalid exact node ID")
        typed_ids = {_typed_key(node_id) for node_id in self.aliases.values()}
        if len(typed_ids) != len(self.aliases):
            raise ValueError("aliases must map to unique exact node IDs")
        scope_keys = [
            tuple(
                (*_typed_key(step.container_node_id), step.subgraph_id)
                for step in item.scope_path
            )
            for item in locator.scope_locators
        ]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("pending scope locators must be duplicate-free")
        selected_key = tuple(
            (*_typed_key(step.container_node_id), step.subgraph_id)
            for step in locator.scope_path
        )
        selected = [
            item
            for key, item in zip(scope_keys, locator.scope_locators, strict=True)
            if key == selected_key
        ]
        if len(selected) != 1 or selected[0].predecessor_branch_id != locator.predecessor_branch_id:
            raise ValueError("pending locator selected-scope facts are inconsistent")
        return self


class BranchSuccessorScopeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_path: list[BranchScopeStep] = Field(default_factory=list, max_length=32)
    predecessor_branch_id: str = Field(..., pattern=_SHA256_PATTERN)
    predecessor_present: bool
    successor_branch_ids: list[str] = Field(default_factory=list, max_length=1_000)


class BranchSuccessorResolutionResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal[WORKFLOW_BRANCH_SUCCESSOR_SCHEMA] = Field(
        WORKFLOW_BRANCH_SUCCESSOR_SCHEMA,
        alias="schema",
    )
    valid: bool
    application_id: str = Field(..., min_length=8, max_length=128)
    patch_hash: str = Field(..., pattern=_SHA256_PATTERN)
    workflow_identity: str = Field(..., min_length=1, max_length=512)
    graph_hash: str = Field(..., pattern=_SHA256_PATTERN)
    branch_catalog_hash: str | None = Field(None, pattern=_SHA256_PATTERN)
    lineage: list[BranchSuccessorScopeResult] = Field(default_factory=list, max_length=8_192)
    successor_branch_ids: list[str] = Field(default_factory=list, max_length=1_000)
    successor_branch_id: str | None = Field(None, pattern=_SHA256_PATTERN)
    issues: list[BranchOperationIssue] = Field(default_factory=list)
    error_count: StrictInt = Field(..., ge=0)
    queued: Literal[False] = False


_MAX_SUCCESSOR_IDS = 1_000
_MAX_LINEAGE_SCAN_WORK = 200_000


def _successor_result(
    request: ResolveBranchSuccessorsRequest,
    *,
    valid: bool,
    workflow_identity: str,
    graph_hash: str,
    branch_catalog_hash: str | None,
    lineage: Sequence[BranchSuccessorScopeResult] = (),
    issues: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    normalized_issues = [BranchOperationIssue.model_validate(item) for item in issues]
    successor_ids = sorted(
        {
            branch_id
            for item in lineage
            for branch_id in item.successor_branch_ids
        }
    )
    if len(successor_ids) > _MAX_SUCCESSOR_IDS:
        normalized_issues.append(
            BranchOperationIssue.model_validate(
                _issue(
                    "branch_successor_result_limit_exceeded",
                    "lineage",
                    "Successor discovery exceeded the bounded result limit.",
                    details={"limit": _MAX_SUCCESSOR_IDS},
                )
            )
        )
        valid = False
        lineage = ()
        successor_ids = []
    result = BranchSuccessorResolutionResult(
        valid=valid and not any(item.severity == "error" for item in normalized_issues),
        application_id=request.apply_request.application_id,
        patch_hash=request.apply_request.patch_hash,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        branch_catalog_hash=branch_catalog_hash,
        lineage=list(lineage) if valid else [],
        successor_branch_ids=successor_ids if valid else [],
        successor_branch_id=(
            successor_ids[0] if valid and len(successor_ids) == 1 else None
        ),
        issues=normalized_issues,
        error_count=sum(item.severity == "error" for item in normalized_issues),
    )
    return result.model_dump(mode="json", by_alias=True)


def _result(
    request: BranchOperationPins,
    *,
    valid: bool,
    issues: Sequence[Mapping[str, Any]],
    branch_catalog_hash: str | None,
    needs_choice: bool = False,
    pending: PendingSuccessorLocator | None = None,
    compiled: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_issues = [BranchOperationIssue.model_validate(item) for item in issues]
    compiled = compiled or {}
    result = BranchOperationResult(
        valid=valid,
        operation=request.operation,
        needs_choice=needs_choice,
        predecessor_branch_id=request.branch_id,
        pending_successor_locator=pending,
        branch_catalog_hash=branch_catalog_hash,
        patch_hash=compiled.get("patch_hash") if valid else None,
        plan=compiled.get("plan") if valid else None,
        apply_request=compiled.get("apply_request") if valid else None,
        expected_final=compiled.get("expected_final") if valid else None,
        issues=normalized_issues,
        error_count=sum(item.severity == "error" for item in normalized_issues),
        warning_count=sum(item.severity == "warning" for item in normalized_issues),
    )
    return result.model_dump(mode="json", by_alias=True)


def _raw_nodes(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = workflow.get("nodes")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, Mapping):
        return [item for item in raw.values() if isinstance(item, Mapping)]
    return []


def _edge_fact_key(edge: BranchEdgeFact) -> tuple[Any, ...]:
    return (
        _typed_key(edge.source.node_id),
        edge.source.output_index,
        edge.source.output,
        _typed_key(edge.target.node_id),
        edge.target.live_socket_index,
        edge.target.input,
        edge.link_type,
    )


def _graph_edge_key(edge: NormalizedGraphEdge) -> tuple[Any, ...]:
    return (
        _typed_key(edge.source_node_id),
        edge.source_output_index,
        edge.source_output,
        _typed_key(edge.target_node_id),
        edge.target_input_index,
        edge.target_input,
        edge.type,
    )


def _branch_edge_facts(branch: WorkflowBranchRecord) -> dict[str, BranchEdgeFact]:
    result: dict[str, BranchEdgeFact] = {}
    for edge in [
        *branch.entry_edges,
        *branch.exit_edges,
        *branch.internal_edges,
        *branch.cut_edges,
    ]:
        result[edge.edge_id] = edge
    return result


def _incident_edges(
    graph: NormalizedGraphSnapshot,
    node_ids: set[tuple[str, str]],
) -> list[NormalizedGraphEdge]:
    return [
        edge
        for edge in graph.edges
        if _typed_key(edge.source_node_id) in node_ids
        or _typed_key(edge.target_node_id) in node_ids
    ]


def _lookup_branch(
    catalog: WorkflowBranchCatalog,
    branch_id: str,
) -> tuple[WorkflowBranchScope, WorkflowBranchRecord] | None:
    matches = [
        (scope, branch)
        for scope in catalog.scopes
        for branch in scope.branches
        if branch.branch_id == branch_id
    ]
    return matches[0] if len(matches) == 1 else None


def _scope_path_key(path: Sequence[BranchScopeStep]) -> tuple[Any, ...]:
    return tuple(
        (*_typed_key(step.container_node_id), step.subgraph_id) for step in path
    )


def _branch_structural_key(branch: WorkflowBranchRecord) -> tuple[Any, ...]:
    """Scope-independent exact predecessor identity for reused definitions."""

    facts = _branch_edge_facts(branch)
    member_facts = [facts[edge_id] for edge_id in branch.edge_ids if edge_id in facts]
    primary = facts.get(branch.primary_entry_edge_id or "")
    return (
        branch.kind,
        tuple(sorted(_typed_key(item) for item in branch.owned_node_ids)),
        tuple(sorted(_typed_key(item) for item in branch.interior_node_ids)),
        tuple(sorted(_typed_key(item) for item in branch.boundary_node_ids)),
        _edge_fact_key(primary) if primary is not None else None,
        tuple(sorted(_edge_fact_key(edge) for edge in member_facts)),
    )


def _pending_scope_locators(
    request: BranchOperationPins,
    branch_catalog: WorkflowBranchCatalog,
    selected_scope: WorkflowBranchScope,
    selected_branch: WorkflowBranchRecord,
    compiled: Mapping[str, Any],
) -> tuple[list[PendingSuccessorScopeLocator], list[dict[str, Any]]]:
    if isinstance(compiled.get("plan"), Mapping) and compiled["plan"].get(
        "operation"
    ) == "scoped_patch":
        raw_paths = compiled["plan"].get("scope", {}).get("affected_scope_paths", [])
        affected_paths = [
            [BranchScopeStep.model_validate(step) for step in path]
            for path in raw_paths
        ]
    else:
        affected_paths = [[]]
    scope_by_path = {
        _scope_path_key(scope.scope.scope_path): scope for scope in branch_catalog.scopes
    }
    selected_key = _branch_structural_key(selected_branch)
    expectation: Literal["removed", "may_reclassify"] = (
        "may_reclassify" if isinstance(request, CloneBranchRequest) else "removed"
    )
    result: list[PendingSuccessorScopeLocator] = []
    issues: list[dict[str, Any]] = []
    for index, path in enumerate(affected_paths):
        scope = scope_by_path.get(_scope_path_key(path))
        if scope is None:
            issues.append(
                _issue(
                    "branch_lineage_scope_missing",
                    f"pending_successor.scope_locators[{index}].scope_path",
                    "An affected scope is absent from the pinned branch catalog.",
                )
            )
            continue
        if scope.scope_id == selected_scope.scope_id:
            matches = [selected_branch]
        else:
            matches = [
                branch
                for branch in scope.branches
                if _branch_structural_key(branch) == selected_key
            ]
        if len(matches) != 1:
            issues.append(
                _issue(
                    "branch_lineage_predecessor_ambiguous",
                    f"pending_successor.scope_locators[{index}]",
                    "The exact predecessor branch is not unique in an affected scope.",
                    details={"candidate_count": len(matches)},
                )
            )
            continue
        result.append(
            PendingSuccessorScopeLocator(
                scope_path=path,
                predecessor_branch_id=matches[0].branch_id,
                predecessor_expectation=expectation,
            )
        )
    return result, issues


def _plan_scope_paths(
    plan: GraphPatchPlan | ScopedGraphPatchPlan,
) -> list[list[BranchScopeStep]]:
    if isinstance(plan, ScopedGraphPatchPlan):
        return [
            [BranchScopeStep.model_validate(step.model_dump(mode="json")) for step in path]
            for path in plan.scope.affected_scope_paths
        ]
    return [[]]


def _plan_endpoint_node_id(ref: Mapping[str, Any]) -> int | str | None:
    if "node_id" in ref and type(ref["node_id"]) in {int, str}:
        return ref["node_id"]
    if "scope_input" in ref:
        return SCOPE_INPUT_NODE_ID
    if "scope_output" in ref:
        return SCOPE_OUTPUT_NODE_ID
    return None


def _plan_edge_fact_key(edge: Any) -> tuple[Any, ...] | None:
    raw = edge.model_dump(mode="json") if isinstance(edge, BaseModel) else edge
    if not isinstance(raw, Mapping):
        return None
    source = raw.get("source")
    target = raw.get("target")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return None
    source_ref = source.get("ref")
    target_ref = target.get("ref")
    if not isinstance(source_ref, Mapping) or not isinstance(target_ref, Mapping):
        return None
    source_id = _plan_endpoint_node_id(source_ref)
    target_id = _plan_endpoint_node_id(target_ref)
    if source_id is None or target_id is None:
        return None
    return (
        _typed_key(source_id),
        source.get("output_index"),
        source.get("output"),
        _typed_key(target_id),
        target.get("socket_index"),
        target.get("input"),
        source.get("type"),
    )


def _plan_ref_node_id(
    ref: Mapping[str, Any],
    aliases: Mapping[str, NodeId],
) -> NodeId | None:
    node_id = _plan_endpoint_node_id(ref)
    if node_id is not None:
        return node_id
    alias = ref.get("alias")
    if isinstance(alias, str):
        return aliases.get(alias)
    return None


def _plan_edge_matches_branch_fact(
    edge: Any,
    fact: BranchEdgeFact,
    aliases: Mapping[str, NodeId],
    *,
    allow_attested_socket_reprojection: bool = False,
) -> bool:
    raw = edge.model_dump(mode="json") if isinstance(edge, BaseModel) else edge
    if not isinstance(raw, Mapping):
        return False
    source = raw.get("source")
    target = raw.get("target")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return False
    source_ref = source.get("ref")
    target_ref = target.get("ref")
    if not isinstance(source_ref, Mapping) or not isinstance(target_ref, Mapping):
        return False
    source_id = _plan_ref_node_id(source_ref, aliases)
    target_id = _plan_ref_node_id(target_ref, aliases)
    if source_id is None or target_id is None:
        return False
    socket_index = target.get("socket_index")
    return (
        _typed_key(source_id) == _typed_key(fact.source.node_id)
        and source.get("output_index") == fact.source.output_index
        and source.get("output") == fact.source.output
        and source.get("type") == fact.link_type
        and _typed_key(target_id) == _typed_key(fact.target.node_id)
        and target.get("input") == fact.target.input
        and target.get("type") == fact.link_type
        and (
            socket_index is None
            or socket_index == fact.target.live_socket_index
            or allow_attested_socket_reprojection
        )
    )


def _scope_facts(
    scope: WorkflowBranchScope,
) -> tuple[
    dict[tuple[str, str], BranchNodeFact],
    dict[str, BranchEdgeFact],
]:
    nodes: dict[tuple[str, str], BranchNodeFact] = {}
    edges: dict[str, BranchEdgeFact] = {}
    for branch in scope.branches:
        for node in branch.nodes:
            nodes[_typed_key(node.node_id)] = node
        edges.update(_branch_edge_facts(branch))
    return nodes, edges


def _attest_created_region(
    scope: WorkflowBranchScope,
    plan: GraphPatchPlan | ScopedGraphPatchPlan,
    aliases: Mapping[str, NodeId],
    *,
    path: str,
    allow_attested_socket_reprojection: bool = False,
) -> tuple[set[tuple[Any, ...]], list[dict[str, Any]]]:
    """Attest exact created node types and their complete current incidents."""

    issues: list[dict[str, Any]] = []
    nodes, edge_by_id = _scope_facts(scope)
    created_keys = {_typed_key(node_id) for node_id in aliases.values()}
    for create in plan.create_nodes:
        node_id = aliases.get(create.alias)
        node = nodes.get(_typed_key(node_id)) if node_id is not None else None
        if node is None:
            issues.append(
                _issue(
                    "branch_successor_created_node_missing",
                    path,
                    f"Created alias {create.alias!r} is absent from the affected scope.",
                )
            )
        elif node.node_type != create.node_type:
            issues.append(
                _issue(
                    "branch_successor_created_node_type_mismatch",
                    path,
                    f"Created alias {create.alias!r} has a different node type in the affected scope.",
                )
            )

    actual_incident = {
        edge_id: fact
        for edge_id, fact in edge_by_id.items()
        if _typed_key(fact.source.node_id) in created_keys
        or _typed_key(fact.target.node_id) in created_keys
    }
    planned_incident = [
        edge
        for edge in plan.add_edges
        if (
            isinstance(edge.source.ref, NewNodeRef)
            or isinstance(edge.target.ref, NewNodeRef)
        )
    ]
    matched_ids: set[str] = set()
    for edge in planned_incident:
        matches = [
            edge_id
            for edge_id, fact in actual_incident.items()
            if edge_id not in matched_ids
            and _plan_edge_matches_branch_fact(
                edge,
                fact,
                aliases,
                allow_attested_socket_reprojection=(
                    allow_attested_socket_reprojection
                ),
            )
        ]
        if len(matches) != 1:
            issues.append(
                _issue(
                    "branch_successor_created_incident_edge_mismatch",
                    path,
                    "A planned created-node incident edge does not have one exact current match.",
                    details={"candidate_count": len(matches)},
                )
            )
            continue
        matched_ids.add(matches[0])
    if len(matched_ids) != len(actual_incident) or len(matched_ids) != len(
        planned_incident
    ):
        issues.append(
            _issue(
                "branch_successor_created_incident_edge_mismatch",
                path,
                "Created nodes have an unplanned, missing, or duplicate current incident edge.",
                details={
                    "planned_count": len(planned_incident),
                    "observed_count": len(actual_incident),
                    "matched_count": len(matched_ids),
                },
            )
        )
    return {
        _edge_fact_key(actual_incident[edge_id])
        for edge_id in matched_ids
    }, issues


def _validate_pending_locator_against_plan(
    request: ResolveBranchSuccessorsRequest,
) -> list[dict[str, Any]]:
    """Re-derive every locator fact represented by the hashed GraphPatch plan.

    Clone predecessor topology is hashed into the plan's full preserved-region
    assertions and is independently re-attested against the fresh post-apply
    branch catalog before any lineage is returned.
    """

    locator = request.pending_successor_locator
    plan = request.apply_request.plan
    issues: list[dict[str, Any]] = []
    planned_paths = {_scope_path_key(path) for path in _plan_scope_paths(plan)}
    locator_paths = {
        _scope_path_key(item.scope_path) for item in locator.scope_locators
    }
    if locator_paths != planned_paths:
        issues.append(
            _issue(
                "branch_lineage_scope_authority_mismatch",
                "pending_successor_locator.scope_locators",
                "Pending lineage scopes do not exactly match the hashed GraphPatch authority.",
            )
        )

    planned_aliases = sorted(item.alias for item in plan.create_nodes)
    if locator.created_aliases != planned_aliases:
        issues.append(
            _issue(
                "branch_lineage_created_alias_mismatch",
                "pending_successor_locator.created_aliases",
                "Pending lineage aliases do not exactly match the hashed GraphPatch plan.",
            )
        )

    removed_ids = sorted(
        (item.ref.node_id for item in plan.remove_nodes),
        key=_typed_key,
    )
    removed = bool(removed_ids)
    expected_state = "created_region" if planned_aliases else "branch_removed"
    if locator.expected_state != expected_state:
        issues.append(
            _issue(
                "branch_lineage_state_mismatch",
                "pending_successor_locator.expected_state",
                "Pending lineage state does not match the hashed GraphPatch operations.",
            )
        )
    expected_predecessor_state = "removed" if removed else "may_reclassify"
    if any(
        item.predecessor_expectation != expected_predecessor_state
        for item in locator.scope_locators
    ):
        issues.append(
            _issue(
                "branch_lineage_predecessor_expectation_mismatch",
                "pending_successor_locator.scope_locators",
                "Pending predecessor expectations do not match the hashed GraphPatch operations.",
            )
        )

    if removed:
        if sorted(locator.predecessor_owned_node_ids, key=_typed_key) != removed_ids:
            issues.append(
                _issue(
                    "branch_lineage_removed_node_mismatch",
                    "pending_successor_locator.predecessor_owned_node_ids",
                    "Pending predecessor nodes do not equal the exact hashed removal set.",
                )
            )
        planned_edge_keys = {
            key
            for edge in plan.remove_edges
            if (key := _plan_edge_fact_key(edge)) is not None
        }
        locator_edge_keys = {_edge_fact_key(edge) for edge in locator.predecessor_edges}
        if len(planned_edge_keys) != len(plan.remove_edges) or (
            planned_edge_keys != locator_edge_keys
        ):
            issues.append(
                _issue(
                    "branch_lineage_removed_edge_mismatch",
                    "pending_successor_locator.predecessor_edges",
                    "Pending predecessor edges do not equal the exact hashed removal set.",
                )
            )
    else:
        if plan.remove_edges or locator.expected_state != "created_region":
            issues.append(
                _issue(
                    "branch_lineage_clone_plan_mismatch",
                    "apply_request.plan",
                    "Clone lineage requires a create-only branch patch with no removals.",
                )
            )
        asserted_edge_keys = {
            key
            for edge in plan.assertions.edges
            if (key := _plan_edge_fact_key(edge)) is not None
        }
        locator_edge_keys = {_edge_fact_key(edge) for edge in locator.predecessor_edges}
        if len(asserted_edge_keys) != len(plan.assertions.edges) or (
            asserted_edge_keys != locator_edge_keys
        ):
            issues.append(
                _issue(
                    "branch_lineage_clone_assertion_mismatch",
                    "apply_request.plan.assertions.edges",
                    "Clone assertions do not equal the exact predecessor edge facts.",
                )
            )
        asserted_node_keys = {
            _typed_key(item.ref.node_id) for item in plan.assertions.nodes
        }
        if not {
            _typed_key(item) for item in locator.predecessor_owned_node_ids
        }.issubset(asserted_node_keys):
            issues.append(
                _issue(
                    "branch_lineage_clone_assertion_mismatch",
                    "apply_request.plan.assertions.nodes",
                    "Clone assertions do not cover every predecessor-owned node.",
                )
            )
    return issues


def _branch_node_keys(branch: WorkflowBranchRecord) -> set[tuple[str, str]]:
    return {_typed_key(item.node_id) for item in branch.nodes}


def _branch_member_edge_keys(branch: WorkflowBranchRecord) -> set[tuple[Any, ...]]:
    facts = _branch_edge_facts(branch)
    return {
        _edge_fact_key(facts[edge_id])
        for edge_id in branch.edge_ids
        if edge_id in facts
    }


def _maximal_branch_candidates(
    candidates: Sequence[WorkflowBranchRecord],
) -> list[WorkflowBranchRecord]:
    candidate_ids = {branch.branch_id for branch in candidates}
    return sorted(
        (
            branch
            for branch in candidates
            if not candidate_ids.intersection(branch.parent_branch_ids)
        ),
        key=lambda branch: branch.branch_id,
    )


def _minimal_branch_candidates(
    candidates: Sequence[WorkflowBranchRecord],
) -> list[WorkflowBranchRecord]:
    """Return the most-specific non-overlapping branch records in a candidate set."""

    candidate_ids = {branch.branch_id for branch in candidates}
    return sorted(
        (
            branch
            for branch in candidates
            if not candidate_ids.intersection(branch.child_branch_ids)
        ),
        key=lambda branch: branch.branch_id,
    )


def _scope_successor_candidates(
    scope: WorkflowBranchScope,
    *,
    required_node_keys: set[tuple[str, str]],
    required_edge_keys: set[tuple[Any, ...]],
    work: list[int],
    exact_branch_id: str | None = None,
    trusted_profile: tuple[
        Literal["segment", "split_arm", "isolated"],
        str | None,
    ]
    | None = None,
    require_preferred_profile: bool = False,
    specificity: Literal["maximal", "minimal"] = "minimal",
    allowed_owned_node_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[WorkflowBranchRecord], bool]:
    candidates: list[WorkflowBranchRecord] = []
    for branch in scope.branches:
        work[0] += 1 + len(branch.nodes) + len(branch.edge_ids)
        if work[0] > _MAX_LINEAGE_SCAN_WORK:
            return [], False
        node_keys = _branch_node_keys(branch)
        edge_keys = _branch_member_edge_keys(branch)
        if allowed_owned_node_keys is not None and not {
            _typed_key(item) for item in branch.owned_node_ids
        }.issubset(allowed_owned_node_keys):
            continue
        if node_keys.intersection(required_node_keys) or edge_keys.intersection(
            required_edge_keys
        ):
            candidates.append(branch)

    preferred = candidates
    if exact_branch_id is not None:
        preferred = [
            branch for branch in candidates if branch.branch_id == exact_branch_id
        ]
    elif trusted_profile is not None:
        kind, fingerprint = trusted_profile
        preferred = [
            branch
            for branch in candidates
            if branch.kind == kind
            and (
                fingerprint is None
                or branch.branch_fingerprint == fingerprint
            )
        ]

    order = (
        _maximal_branch_candidates
        if specificity == "maximal"
        else _minimal_branch_candidates
    )

    def covered(selected: Sequence[WorkflowBranchRecord]) -> bool:
        covered_nodes: set[tuple[str, str]] = set()
        covered_edges: set[tuple[Any, ...]] = set()
        for branch in selected:
            covered_nodes.update(_branch_node_keys(branch))
            covered_edges.update(_branch_member_edge_keys(branch))
        return required_node_keys.issubset(
            covered_nodes
        ) and required_edge_keys.issubset(covered_edges)

    selected = order(preferred)
    if not covered(selected) and not require_preferred_profile:
        selected = order(candidates)
    return selected, covered(selected)


def resolve_workflow_branch_successors(
    request: ResolveBranchSuccessorsRequest | Mapping[str, Any],
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve exact post-apply branch lineage without mutating or queueing.

    The GraphPatch apply envelope remains the sole mutation authority.  The
    pending locator is only bounded provenance and is revalidated against the
    hashed plan plus the fresh, graph-pinned branch catalog before use.
    """

    validated = (
        request
        if isinstance(request, ResolveBranchSuccessorsRequest)
        else ResolveBranchSuccessorsRequest.model_validate(request)
    )
    issues = _validate_pending_locator_against_plan(validated)
    result_definition_hash: str | None = None
    try:
        if isinstance(validated.apply_request, ApplyScopedGraphPatchRequest):
            result_definition_hash = project_workflow_scope(
                workflow,
                validated.apply_request.plan.scope.scope_path,
            ).resolution.definition_hash
        state_issues = verify_completed_graph_patch_state(
            validated.apply_request,
            workflow,
            catalog,
            validated.aliases,
            result_definition_hash=result_definition_hash,
        )
    except (TypeError, ValueError, WorkflowScopeError) as exc:
        state_issues = [
            _issue(
                "branch_successor_postcondition_unavailable",
                "workflow",
                f"The current GraphPatch postconditions cannot be attested: {exc}",
            )
        ]
    issues.extend(state_issues)
    postconditions_attested = not state_issues
    if validated.expected_workflow_identity != workflow_identity:
        issues.append(
            _issue(
                "workflow_identity_changed",
                "expected_workflow_identity",
                "The active workflow identity differs from the post-apply lineage pin.",
            )
        )
    if validated.expected_graph_hash != graph_hash:
        issues.append(
            _issue(
                "graph_changed",
                "expected_graph_hash",
                "The active workflow graph differs from the attested post-apply hash.",
            )
        )

    catalog = discover_workflow_branches(
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
    )
    if not catalog.valid:
        issues.append(
            _issue(
                "branch_catalog_invalid",
                "workflow",
                "The post-apply workflow cannot produce a complete safe branch catalog.",
                details={"issue_codes": [item.code for item in catalog.issues]},
            )
        )
    if issues:
        return _successor_result(
            validated,
            valid=False,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            branch_catalog_hash=catalog.branch_catalog_hash,
            issues=issues,
        )

    locator = validated.pending_successor_locator
    scopes = {
        _scope_path_key(scope.scope.scope_path): scope for scope in catalog.scopes
    }
    created_node_keys = {_typed_key(item) for item in validated.aliases.values()}
    predecessor_node_keys = {
        _typed_key(item) for item in locator.predecessor_owned_node_ids
    }
    predecessor_edge_keys = {
        _edge_fact_key(item) for item in locator.predecessor_edges
    }
    lineage: list[BranchSuccessorScopeResult] = []
    work = [0]
    for index, scope_locator in enumerate(locator.scope_locators):
        scope = scopes.get(_scope_path_key(scope_locator.scope_path))
        if scope is None:
            issues.append(
                _issue(
                    "branch_lineage_scope_missing",
                    f"pending_successor_locator.scope_locators[{index}].scope_path",
                    "An affected scope is absent from the post-apply branch catalog.",
                )
            )
            continue
        predecessor_matches = [
            branch
            for branch in scope.branches
            if branch.branch_id == scope_locator.predecessor_branch_id
        ]
        predecessor_present = len(predecessor_matches) == 1
        scope_node_keys = {
            key
            for branch in scope.branches
            for key in _branch_node_keys(branch)
        }
        if scope_locator.predecessor_expectation == "removed" and (
            predecessor_present
            or predecessor_node_keys.intersection(scope_node_keys)
        ):
            issues.append(
                _issue(
                    "branch_predecessor_still_present",
                    f"lineage[{index}]",
                    "The predecessor branch region is still present after the attested apply.",
                )
            )
            continue
        if (
            scope_locator.predecessor_expectation == "may_reclassify"
            and not predecessor_present
        ):
            issues.append(
                _issue(
                    "branch_clone_predecessor_missing",
                    f"lineage[{index}]",
                    "The exact predecessor branch is absent after the clone; lineage cannot be proven.",
                )
            )
            continue

        successors: list[WorkflowBranchRecord] = []
        if locator.expected_state == "created_region":
            created_edge_keys, created_issues = _attest_created_region(
                scope,
                validated.apply_request.plan,
                validated.aliases,
                path=f"lineage[{index}]",
                allow_attested_socket_reprojection=postconditions_attested,
            )
            if created_issues:
                issues.extend(created_issues)
                continue
            trusted_predecessor = (
                predecessor_matches[0]
                if predecessor_present
                and scope_locator.predecessor_expectation == "may_reclassify"
                else None
            )
            created, covered = _scope_successor_candidates(
                scope,
                required_node_keys=created_node_keys,
                required_edge_keys=created_edge_keys,
                work=work,
                trusted_profile=(
                    (
                        trusted_predecessor.kind,
                        trusted_predecessor.branch_fingerprint,
                    )
                    if trusted_predecessor is not None
                    else None
                ),
                require_preferred_profile=trusted_predecessor is not None,
                specificity="minimal",
                allowed_owned_node_keys=created_node_keys,
            )
            if not covered and trusted_predecessor is not None:
                created, covered = _scope_successor_candidates(
                    scope,
                    required_node_keys=created_node_keys,
                    required_edge_keys=created_edge_keys,
                    work=work,
                    trusted_profile=(trusted_predecessor.kind, None),
                    require_preferred_profile=True,
                    specificity="minimal",
                    allowed_owned_node_keys=created_node_keys,
                )
            if not covered and trusted_predecessor is not None:
                created, covered = _scope_successor_candidates(
                    scope,
                    required_node_keys=created_node_keys,
                    required_edge_keys=created_edge_keys,
                    work=work,
                    specificity="minimal",
                    allowed_owned_node_keys=created_node_keys,
                )
            if not covered:
                issues.append(
                    _issue(
                        "branch_successor_created_region_unresolved",
                        f"lineage[{index}]",
                        "Fresh branches do not exactly cover every created alias and planned incident edge.",
                    )
                )
                continue
            successors.extend(created)
            if scope_locator.predecessor_expectation == "may_reclassify":
                continuity, continuity_covered = _scope_successor_candidates(
                    scope,
                    required_node_keys=predecessor_node_keys,
                    required_edge_keys=predecessor_edge_keys,
                    work=work,
                    exact_branch_id=(
                        scope_locator.predecessor_branch_id
                        if predecessor_present
                        else None
                    ),
                    require_preferred_profile=True,
                    specificity="minimal",
                )
                if not continuity_covered:
                    issues.append(
                        _issue(
                            "branch_clone_continuity_unresolved",
                            f"lineage[{index}]",
                            "Fresh branches do not prove complete predecessor continuity for the clone.",
                        )
                    )
                    continue
                successors.extend(continuity)
        unique_successors = sorted(
            {item.branch_id: item for item in successors}.values(),
            key=lambda item: item.branch_id,
        )
        successor_ids = [item.branch_id for item in unique_successors]
        if len(successor_ids) > _MAX_SUCCESSOR_IDS:
            issues.append(
                _issue(
                    "branch_successor_result_limit_exceeded",
                    f"lineage[{index}].successor_branch_ids",
                    "Successor discovery exceeded the bounded per-scope result limit.",
                    details={"limit": _MAX_SUCCESSOR_IDS},
                )
            )
            continue
        lineage.append(
            BranchSuccessorScopeResult(
                scope_path=scope_locator.scope_path,
                predecessor_branch_id=scope_locator.predecessor_branch_id,
                predecessor_present=predecessor_present,
                successor_branch_ids=successor_ids,
            )
        )

    if work[0] > _MAX_LINEAGE_SCAN_WORK:
        issues.append(
            _issue(
                "branch_lineage_work_limit_exceeded",
                "lineage",
                "Successor discovery exceeded its bounded topology work budget.",
            )
        )
    if len(lineage) != len(locator.scope_locators):
        issues.append(
            _issue(
                "branch_lineage_incomplete",
                "lineage",
                "Every affected scope must resolve to one complete lineage record.",
            )
        )
    return _successor_result(
        validated,
        valid=not issues,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        branch_catalog_hash=catalog.branch_catalog_hash,
        lineage=lineage,
        issues=issues,
    )


def _preflight(
    request: BranchOperationPins,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
) -> tuple[
    WorkflowBranchCatalog,
    WorkflowBranchScope | None,
    WorkflowBranchRecord | None,
    NormalizedGraphSnapshot | None,
    list[dict[str, Any]],
]:
    issues: list[dict[str, Any]] = []
    if request.expected_workflow_identity != workflow_identity:
        issues.append(
            _issue(
                "workflow_identity_changed",
                "expected_workflow_identity",
                "The active workflow identity differs from the discovered branch.",
            )
        )
    if request.expected_graph_hash != graph_hash:
        issues.append(
            _issue(
                "graph_changed",
                "expected_graph_hash",
                "The active workflow graph changed after branch discovery.",
            )
        )
    branch_catalog = discover_workflow_branches(
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
    )
    if not branch_catalog.valid:
        issues.append(
            _issue(
                "branch_catalog_invalid",
                "workflow",
                "The active workflow cannot produce a complete safe branch catalog.",
                details={"issue_codes": [item.code for item in branch_catalog.issues]},
            )
        )
    if request.expected_branch_catalog_hash != branch_catalog.branch_catalog_hash:
        issues.append(
            _issue(
                "branch_catalog_changed",
                "expected_branch_catalog_hash",
                "The deterministic branch catalog changed after discovery.",
            )
        )
    match = _lookup_branch(branch_catalog, request.branch_id)
    scope: WorkflowBranchScope | None = None
    branch: WorkflowBranchRecord | None = None
    if match is None:
        issues.append(
            _issue(
                "branch_not_found",
                "branch_id",
                "The exact branch ID is absent from the active branch catalog.",
            )
        )
    else:
        scope, branch = match
        if scope.scope.kind == "root" and not branch.writable:
            issues.append(
                _issue(
                    "branch_not_writable",
                    "branch_id",
                    "The selected root branch is not safely writable.",
                )
            )
        if scope.scope.kind == "subgraph_instance":
            unsupported = sorted(
                {
                    *scope.reasons,
                    *branch.reasons,
                }
                - {"shared_definition_acknowledgement_required"}
            )
            if unsupported:
                issues.append(
                    _issue(
                        "nested_branch_not_writable",
                        "branch_id",
                        "The nested branch has safety restrictions beyond its scoped edit policy.",
                        details={"reasons": unsupported},
                    )
                )
    graph: NormalizedGraphSnapshot | None = None
    try:
        graph = normalize_workflow_graph(workflow)
    except (TypeError, ValueError) as exc:
        issues.append(_issue("workflow_graph_invalid", "workflow", str(exc)))
    if (
        branch is not None
        and graph is not None
        and branch.owned_node_ids
        and scope is not None
        and scope.scope.kind == "root"
    ):
        owned = {_typed_key(item) for item in branch.owned_node_ids}
        incident = _incident_edges(graph, owned)
        facts = _branch_edge_facts(branch)
        fact_keys = {_edge_fact_key(item) for item in facts.values()}
        missing = [edge for edge in incident if _graph_edge_key(edge) not in fact_keys]
        if missing:
            issues.append(
                _issue(
                    "branch_incident_edges_incomplete",
                    "branch_id",
                    "The branch does not assert every exact edge incident to its owned nodes.",
                    details={"missing_edge_count": len(missing)},
                )
            )
    return branch_catalog, scope, branch, graph, issues


def _mapping_completeness(
    actual: Sequence[str],
    expected: Sequence[str],
    *,
    path: str,
    kind: str,
) -> list[dict[str, Any]]:
    if Counter(actual) == Counter(expected):
        return []
    return [
        _issue(
            f"incomplete_{kind}_mapping",
            path,
            f"Every exact branch {kind} edge must be mapped exactly once.",
            details={
                "expected_edge_ids": sorted(expected),
                "provided_edge_ids": sorted(actual),
            },
        )
    ]


def _existing_aliases(
    node_ids: Sequence[int | str],
    *,
    reserved: set[str],
) -> tuple[dict[tuple[str, str], str], list[ExistingNodeSelector]]:
    result: dict[tuple[str, str], str] = {}
    selectors: list[ExistingNodeSelector] = []
    for index, node_id in enumerate(sorted(node_ids, key=_typed_key)):
        base = f"branch_existing_{index:03d}"
        alias = base
        suffix = 0
        while alias in reserved:
            suffix += 1
            alias = f"{base}_{suffix}"
        result[_typed_key(node_id)] = alias
        selectors.append(ExistingNodeSelector(alias=alias, node_id=node_id))
    return result, selectors


def _semantic_edge_from_graph(
    edge: NormalizedGraphEdge,
    aliases: Mapping[tuple[str, str], str],
) -> RefinementSpecEdge:
    return RefinementSpecEdge(
        source_alias=aliases[_typed_key(edge.source_node_id)],
        source_output=edge.source_output,
        source_output_index=edge.source_output_index,
        target_alias=aliases[_typed_key(edge.target_node_id)],
        target_input=edge.target_input,
        target_mode="slot",
    )


def _plan_ref_key(value: Mapping[str, Any]) -> tuple[str, Any]:
    if "node_id" in value:
        return "existing", _typed_key(value["node_id"])
    return "new", value.get("alias")


def _plan_edge_runtime_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    source = value["source"]
    target = value["target"]
    return (
        _plan_ref_key(source["ref"]),
        source["output_index"],
        source["output"],
        _plan_ref_key(target["ref"]),
        target.get("socket_index"),
        target["input"],
        source["type"],
    )


def _expected_incident_plan_keys(edges: Sequence[NormalizedGraphEdge]) -> set[tuple[Any, ...]]:
    return {
        (
            ("existing", _typed_key(edge.source_node_id)),
            edge.source_output_index,
            edge.source_output,
            ("existing", _typed_key(edge.target_node_id)),
            edge.target_input_index,
            edge.target_input,
            edge.type,
        )
        for edge in edges
    }


def _postcheck(
    compiled: Mapping[str, Any],
    *,
    removed_node_ids: Sequence[int | str],
    incident_edges: Sequence[NormalizedGraphEdge],
    created_aliases: set[str],
    allowed_additions: Sequence[tuple[set[tuple[str, Any]], set[tuple[str, Any]]]],
    asserted_node_ids: Sequence[int | str] | None = None,
    asserted_edges: Sequence[NormalizedGraphEdge] | None = None,
) -> list[dict[str, Any]]:
    plan = compiled.get("plan")
    if not isinstance(plan, Mapping):
        return [_issue("branch_lowering_missing_plan", "compiler.plan", "The compiler returned no GraphPatch plan.")]
    issues: list[dict[str, Any]] = []
    actual_removed = {
        _typed_key(item["ref"]["node_id"])
        for item in plan.get("remove_nodes", [])
    }
    expected_removed = {_typed_key(item) for item in removed_node_ids}
    if actual_removed != expected_removed:
        issues.append(
            _issue(
                "branch_postcheck_removed_nodes_changed",
                "plan.remove_nodes",
                "Lowering changed the exact branch-owned removal set.",
            )
        )
    actual_created = {item["alias"] for item in plan.get("create_nodes", [])}
    if actual_created != created_aliases:
        issues.append(
            _issue(
                "branch_postcheck_created_nodes_changed",
                "plan.create_nodes",
                "Lowering changed the declared replacement/clone aliases.",
            )
        )
    if plan.get("update_nodes") or plan.get("attachments"):
        issues.append(
            _issue(
                "branch_postcheck_unexpected_operation",
                "plan",
                "Branch lowering introduced an update or attachment operation.",
            )
        )
    if {
        _plan_edge_runtime_key(item) for item in plan.get("remove_edges", [])
    } != _expected_incident_plan_keys(incident_edges):
        issues.append(
            _issue(
                "branch_postcheck_removed_edges_changed",
                "plan.remove_edges",
                "Lowering did not remove exactly every incident edge of the owned region.",
            )
        )
    if asserted_node_ids is not None:
        actual_asserted_nodes = {
            _typed_key(item["ref"]["node_id"])
            for item in plan.get("assertions", {}).get("nodes", [])
        }
        expected_asserted_nodes = {_typed_key(item) for item in asserted_node_ids}
        if actual_asserted_nodes != expected_asserted_nodes:
            issues.append(
                _issue(
                    "branch_postcheck_asserted_nodes_changed",
                    "plan.assertions.nodes",
                    "Lowering did not assert exactly every node in the preserved branch boundary.",
                )
            )
    if asserted_edges is not None:
        actual_asserted_edges = {
            _plan_edge_runtime_key(item)
            for item in plan.get("assertions", {}).get("edges", [])
        }
        if actual_asserted_edges != _expected_incident_plan_keys(asserted_edges):
            issues.append(
                _issue(
                    "branch_postcheck_asserted_edges_changed",
                    "plan.assertions.edges",
                    "Lowering did not assert every exact edge incident to the preserved branch region.",
                )
            )
    for index, edge in enumerate(plan.get("add_edges", [])):
        source = _plan_ref_key(edge["source"]["ref"])
        target = _plan_ref_key(edge["target"]["ref"])
        if not any(source in sources and target in targets for sources, targets in allowed_additions):
            issues.append(
                _issue(
                    "branch_postcheck_addition_outside_region",
                    f"plan.add_edges[{index}]",
                    "Lowering introduced an edge outside the declared branch boundary.",
                )
            )
    return issues


def _compile_semantic(
    request: BranchOperationPins,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
    existing_nodes: list[ExistingNodeSelector],
    create_nodes: list[WorkflowSpecNode],
    add_edges: list[RefinementSpecEdge],
    remove_edges: list[RefinementSpecEdge],
    remove_nodes: list[str],
) -> dict[str, Any]:
    semantic_request = CompileWorkflowRefinementSpecRequest(
        application_id=request.application_id,
        existing_nodes=existing_nodes,
        create_nodes=create_nodes,
        add_edges=add_edges,
        remove_edges=remove_edges,
        remove_nodes=remove_nodes,
        allow_inferred_converters=False,
        expected_graph_hash=graph_hash,
        expected_catalog_hash=catalog_hash,
    )
    return compile_workflow_refinement_spec(
        semantic_request,
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        catalog=catalog,
        catalog_hash=catalog_hash,
        source=source,
    )


def _replace(
    request: ReplaceBranchRequest,
    branch: WorkflowBranchRecord,
    graph: NormalizedGraphSnapshot,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool, list[str]]:
    issues: list[dict[str, Any]] = []
    if not branch.owned_node_ids:
        return None, [_issue("branch_has_no_owned_nodes", "branch_id", "This branch has no private owned nodes to replace.")], False, []
    issues.extend(
        _mapping_completeness(
            [item.entry_edge_id for item in request.entry_mappings],
            [item.edge_id for item in branch.entry_edges],
            path="entry_mappings",
            kind="entry",
        )
    )
    issues.extend(
        _mapping_completeness(
            [item.exit_edge_id for item in request.exit_mappings],
            [item.edge_id for item in branch.exit_edges],
            path="exit_mappings",
            kind="exit",
        )
    )
    if issues:
        return None, issues, False, []
    edge_by_id = _branch_edge_facts(branch)
    incident = _incident_edges(graph, {_typed_key(item) for item in branch.owned_node_ids})
    node_ids = set(branch.owned_node_ids)
    node_ids.update(
        item
        for edge in incident
        for item in (edge.source_node_id, edge.target_node_id)
    )
    replacement_aliases = {item.alias for item in request.replacement_nodes}
    aliases, selectors = _existing_aliases(node_ids, reserved=replacement_aliases)
    remove_edges = [_semantic_edge_from_graph(edge, aliases) for edge in incident]
    add_edges = list(request.replacement_edges)
    for item in request.entry_mappings:
        edge = edge_by_id[item.entry_edge_id]
        add_edges.append(
            RefinementSpecEdge(
                source_alias=aliases[_typed_key(edge.source.node_id)],
                source_output=edge.source.output,
                source_output_index=edge.source.output_index,
                target_alias=item.target_alias,
                target_input=item.target_input,
                target_mode=item.target_mode,
            )
        )
    for item in request.exit_mappings:
        edge = edge_by_id[item.exit_edge_id]
        add_edges.append(
            RefinementSpecEdge(
                source_alias=item.source_alias,
                source_output=item.source_output,
                source_output_index=item.source_output_index,
                target_alias=aliases[_typed_key(edge.target.node_id)],
                target_input=edge.target.input,
                target_mode="slot",
            )
        )
    compiled = _compile_semantic(
        request,
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        catalog=catalog,
        catalog_hash=catalog_hash,
        source=source,
        existing_nodes=selectors,
        create_nodes=list(request.replacement_nodes),
        add_edges=add_edges,
        remove_edges=remove_edges,
        remove_nodes=[aliases[_typed_key(item)] for item in branch.owned_node_ids],
    )
    compiler_issues = [_compiler_issue(item) for item in compiled.get("issues", [])]
    if not compiled.get("valid"):
        return compiled, compiler_issues, bool(compiled.get("needs_choice")), sorted(replacement_aliases)
    allowed = [
        (
            {("new", alias) for alias in replacement_aliases},
            {("new", alias) for alias in replacement_aliases},
        ),
        (
            {("existing", _typed_key(item.source.node_id)) for item in branch.entry_edges},
            {("new", alias) for alias in replacement_aliases},
        ),
        (
            {("new", alias) for alias in replacement_aliases},
            {("existing", _typed_key(item.target.node_id)) for item in branch.exit_edges},
        ),
    ]
    post_issues = _postcheck(
        compiled,
        removed_node_ids=branch.owned_node_ids,
        incident_edges=incident,
        created_aliases=replacement_aliases,
        allowed_additions=allowed,
    )
    return compiled, [*compiler_issues, *post_issues], False, sorted(replacement_aliases)


def _remove(
    request: RemoveBranchRequest,
    branch: WorkflowBranchRecord,
    graph: NormalizedGraphSnapshot,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    if not branch.owned_node_ids:
        return None, [_issue("branch_has_no_owned_nodes", "branch_id", "This branch has no private owned nodes to remove.")], False
    if request.mode == "delete" and branch.exit_edges:
        return None, [_issue("terminal_delete_required", "mode", "Delete mode is allowed only when the branch has no outgoing cut edge.")], False
    mappings = list(request.bypass_mappings)
    if request.mode == "bypass" and not mappings:
        if len(branch.entry_edges) == 1 and len(branch.exit_edges) == 1:
            mappings = [
                BranchBypassMapping(
                    entry_edge_id=branch.entry_edges[0].edge_id,
                    exit_edge_id=branch.exit_edges[0].edge_id,
                )
            ]
        else:
            return (
                None,
                [
                    _issue(
                        "bypass_mapping_required",
                        "bypass_mappings",
                        "This branch has multiple possible bypass boundary mappings.",
                        details={
                            "entry_edge_ids": sorted(item.edge_id for item in branch.entry_edges),
                            "exit_edge_ids": sorted(item.edge_id for item in branch.exit_edges),
                        },
                    )
                ],
                True,
            )
    if request.mode == "bypass":
        entry_ids = {item.edge_id for item in branch.entry_edges}
        exit_ids = {item.edge_id for item in branch.exit_edges}
        if any(item.entry_edge_id not in entry_ids or item.exit_edge_id not in exit_ids for item in mappings):
            return None, [_issue("unknown_bypass_boundary_edge", "bypass_mappings", "A bypass mapping references an edge outside the exact branch boundary.")], False
        if {item.entry_edge_id for item in mappings} != entry_ids or Counter(
            item.exit_edge_id for item in mappings
        ) != Counter(dict.fromkeys(exit_ids, 1)):
            return None, [_issue("incomplete_bypass_mapping", "bypass_mappings", "Every entry must be used and every exit must be mapped exactly once.")], False
    edge_by_id = _branch_edge_facts(branch)
    incident = _incident_edges(graph, {_typed_key(item) for item in branch.owned_node_ids})
    node_ids = set(branch.owned_node_ids)
    node_ids.update(
        item
        for edge in incident
        for item in (edge.source_node_id, edge.target_node_id)
    )
    aliases, selectors = _existing_aliases(node_ids, reserved=set())
    remove_edges = [_semantic_edge_from_graph(edge, aliases) for edge in incident]
    additions: list[RefinementSpecEdge] = []
    for item in mappings:
        entry = edge_by_id[item.entry_edge_id]
        exit_edge = edge_by_id[item.exit_edge_id]
        additions.append(
            RefinementSpecEdge(
                source_alias=aliases[_typed_key(entry.source.node_id)],
                source_output=entry.source.output,
                source_output_index=entry.source.output_index,
                target_alias=aliases[_typed_key(exit_edge.target.node_id)],
                target_input=exit_edge.target.input,
                target_mode="slot",
            )
        )
    compiled = _compile_semantic(
        request,
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        catalog=catalog,
        catalog_hash=catalog_hash,
        source=source,
        existing_nodes=selectors,
        create_nodes=[],
        add_edges=additions,
        remove_edges=remove_edges,
        remove_nodes=[aliases[_typed_key(item)] for item in branch.owned_node_ids],
    )
    compiler_issues = [_compiler_issue(item) for item in compiled.get("issues", [])]
    if not compiled.get("valid"):
        return compiled, compiler_issues, bool(compiled.get("needs_choice"))
    allowed = [
        (
            {("existing", _typed_key(item.source.node_id)) for item in branch.entry_edges},
            {("existing", _typed_key(item.target.node_id)) for item in branch.exit_edges},
        )
    ] if additions else []
    post_issues = _postcheck(
        compiled,
        removed_node_ids=branch.owned_node_ids,
        incident_edges=incident,
        created_aliases=set(),
        allowed_additions=allowed,
    )
    return compiled, [*compiler_issues, *post_issues], False


def _clone_node_set(
    branch: WorkflowBranchRecord,
    graph: NormalizedGraphSnapshot,
) -> tuple[list[int | str], list[str], list[dict[str, Any]]]:
    if branch.kind == "isolated":
        return [], [], [_issue("clone_branch_kind_unsupported", "branch_id", "Isolated-node clone is outside the branch-clone v1 contract.")]
    clone_keys = {_typed_key(item) for item in branch.owned_node_ids}
    if branch.kind == "segment":
        outgoing_counts = Counter(_typed_key(edge.source_node_id) for edge in graph.edges)
        incoming_counts = Counter(_typed_key(edge.target_node_id) for edge in graph.edges)
        terminal_targets = {
            _typed_key(edge.target.node_id): edge.target.node_id
            for edge in branch.exit_edges
            if outgoing_counts[_typed_key(edge.target.node_id)] == 0
            and incoming_counts[_typed_key(edge.target.node_id)] == 1
        }
        if len(terminal_targets) == 1:
            clone_keys.update(terminal_targets)
    node_by_key = {_typed_key(node.node_id): node.node_id for node in graph.nodes}
    clone_ids = [node_by_key[key] for key in sorted(clone_keys) if key in node_by_key]
    if not clone_ids:
        return [], [], [_issue("branch_has_no_cloneable_nodes", "branch_id", "The branch has no private nodes to clone.")]
    outgoing = [
        edge
        for edge in graph.edges
        if _typed_key(edge.source_node_id) in clone_keys
        and _typed_key(edge.target_node_id) not in clone_keys
    ]
    edge_ids_by_key = {
        _edge_fact_key(fact): edge_id
        for edge_id, fact in _branch_edge_facts(branch).items()
    }
    detached_edge_ids = [
        edge_ids_by_key.get(_graph_edge_key(edge)) for edge in outgoing
    ]
    if any(edge_id is None for edge_id in detached_edge_ids):
        return [], [], [
            _issue(
                "clone_detached_exit_edge_unresolved",
                "branch_id",
                "An external clone exit lacks an exact branch edge identity.",
            )
        ]
    return clone_ids, sorted(str(edge_id) for edge_id in detached_edge_ids), []


def _layout_hint(raw: Mapping[str, Any], offset: BranchLayoutOffset) -> GraphPatchLayoutHint | None:
    pos = raw.get("pos")
    if not isinstance(pos, Sequence) or isinstance(pos, (str, bytes)) or len(pos) < 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in pos[:2]):
        return None
    kwargs: dict[str, Any] = {"x": pos[0] + offset.x, "y": pos[1] + offset.y}
    size = raw.get("size")
    if isinstance(size, Sequence) and not isinstance(size, (str, bytes)) and len(size) >= 2:
        if all(not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(float(item)) and float(item) > 0 for item in size[:2]):
            kwargs.update(width=size[0], height=size[1])
    try:
        return GraphPatchLayoutHint(**kwargs)
    except ValueError:
        return None


def _clone_values(
    raw: Mapping[str, Any],
    node_type: str,
    node_info: Mapping[str, Any],
    *,
    connected_inputs: set[str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    capabilities = normalize_node_schema(node_type, node_info)
    if capabilities.classification.status != "supported":
        return None, [_issue("clone_schema_opaque", "branch_id", f"{node_type} does not have a fully supported reconstructable schema.")]
    widget_values = raw.get("widgets_values", [])
    if not isinstance(widget_values, list):
        return None, [_issue("clone_widget_values_malformed", "branch_id", f"{node_type} has no exact positional widget-value list.")]
    selector_values = infer_dynamic_selector_values(
        capabilities,
        widget_values,
        connected_inputs=connected_inputs,
    )
    materialized = materialize_inputs(
        capabilities,
        values=selector_values,
        connected_inputs=connected_inputs,
    )
    if any(
        capability.hidden
        and (
            capability.hidden_kind == "auth"
            or _secret_identifier(capability.path)
            or _secret_identifier(capability.name)
            or _contains_secret_mapping_key(
                capability.metadata,
                inspect_string_values=True,
            )
        )
        for capability in capabilities.inputs
    ):
        return None, [
            _issue(
                "clone_secret_value_unsupported",
                "branch_id",
                f"{node_type} exposes hidden credential state that cannot be copied safely.",
            )
        ]
    if any(
        _metadata_declares_attachment(item.capability.metadata)
        for item in materialized
    ):
        return None, [
            _issue(
                "clone_attachment_mapping_required",
                "branch_id",
                f"{node_type} contains an upload-backed attachment widget.",
            )
        ]
    widgets = [
        item.capability
        for item in materialized
        if item.capability.widget
        and not item.capability.hidden
        and item.capability.path not in connected_inputs
    ]
    if len(widgets) != len(widget_values):
        return None, [_issue("clone_widget_reconstruction_incomplete", "branch_id", f"{node_type} exposes {len(widgets)} active named widgets but stores {len(widget_values)} positional values.")]
    values: dict[str, Any] = {}
    for capability, value in zip(widgets, widget_values, strict=True):
        secret_bearing = (
            _secret_identifier(capability.path)
            or _secret_identifier(capability.name)
            or _contains_secret_mapping_key(
                capability.metadata,
                inspect_string_values=True,
            )
            or _contains_secret_mapping_key(value)
        )
        if secret_bearing:
            if (
                capability.default.available
                and capability.default.value == value
                and _safe_empty_secret_default(value)
            ):
                continue
            return None, [
                _issue(
                    "clone_secret_value_unsupported",
                    "branch_id",
                    f"{node_type} contains credential-like widget state that will not be copied.",
                )
            ]
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None, [_issue("clone_widget_value_not_json", "branch_id", f"{node_type}.{capability.path} is not a JSON-safe widget value.")]
        values[capability.path] = value
    return values, []


def _clone_serialized_state_issues(
    raw: Mapping[str, Any],
    node_type: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    mode = raw.get("mode", 0)
    if isinstance(mode, bool) or not isinstance(mode, int) or mode != 0:
        issues.append(
            _issue(
                "clone_execution_state_unsupported",
                "branch_id",
                f"{node_type} has a non-default execution mode that GraphPatch cannot reproduce.",
            )
        )
    flags = raw.get("flags", {})
    if not isinstance(flags, Mapping) or flags:
        issues.append(
            _issue(
                "clone_execution_state_unsupported",
                "branch_id",
                f"{node_type} has serialized flags that GraphPatch cannot reproduce exactly.",
            )
        )
    properties = raw.get("properties", {})
    properties_supported = isinstance(properties, Mapping) and all(
        isinstance(key, str)
        and (
            key in _CLONE_COSMETIC_PROPERTY_KEYS
            or (
                key == _GRAPH_PATCH_PROVENANCE_PROPERTY
                and _valid_graph_patch_provenance(value)
            )
        )
        for key, value in properties.items()
    )
    if not properties_supported:
        issues.append(
            _issue(
                "clone_properties_unsupported",
                "branch_id",
                f"{node_type} has functional serialized properties that GraphPatch cannot reproduce.",
            )
        )
    return issues


def _valid_graph_patch_provenance(value: Any) -> bool:
    """Recognize only provenance GraphPatch itself recreates on a new node."""

    if not isinstance(value, Mapping) or set(value) != _GRAPH_PATCH_PROVENANCE_FIELDS:
        return False
    schema = value.get("schema")
    application_id = value.get("application_id")
    patch_hash = value.get("patch_hash")
    alias = value.get("alias")
    schema_hash = value.get("schema_hash")
    return bool(
        isinstance(schema, str)
        and schema in {GRAPH_PATCH_SCHEMA, SCOPED_GRAPH_PATCH_SCHEMA}
        and isinstance(application_id, str)
        and re.fullmatch(_APPLICATION_PATTERN, application_id)
        and isinstance(patch_hash, str)
        and re.fullmatch(_SHA256_PATTERN, patch_hash)
        and isinstance(alias, str)
        and _ALIAS_PATTERN.fullmatch(alias)
        and isinstance(schema_hash, str)
        and re.fullmatch(_SHA256_PATTERN, schema_hash)
    )


def _clone(
    request: CloneBranchRequest,
    branch: WorkflowBranchRecord,
    graph: NormalizedGraphSnapshot,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool, list[str]]:
    clone_ids, detached_exit_edge_ids, issues = _clone_node_set(branch, graph)
    if issues:
        return None, issues, False, []
    clone_warnings = (
        [
            _issue(
                "clone_outputs_detached",
                "branch_id",
                "The cloned private region has external outputs; those exits remain detached.",
                severity="warning",
                details={
                    "detached_exit_edge_ids": detached_exit_edge_ids,
                    "external_exit_connections_created": 0,
                },
            )
        ]
        if detached_exit_edge_ids
        else []
    )
    raw_by_id = {
        _typed_key(item["id"]): item
        for item in _raw_nodes(workflow)
        if isinstance(item.get("id"), (int, str)) and not isinstance(item.get("id"), bool)
    }
    clone_keys = {_typed_key(item) for item in clone_ids}
    internal = [
        edge
        for edge in graph.edges
        if _typed_key(edge.source_node_id) in clone_keys
        and _typed_key(edge.target_node_id) in clone_keys
    ]
    incoming = [
        edge
        for edge in graph.edges
        if _typed_key(edge.source_node_id) not in clone_keys
        and _typed_key(edge.target_node_id) in clone_keys
    ]
    incident = [
        edge
        for edge in graph.edges
        if _typed_key(edge.source_node_id) in clone_keys
        or _typed_key(edge.target_node_id) in clone_keys
    ]
    assertions, assertion_issues = graph_patch_assertions_for_existing_region(
        graph,
        catalog,
        clone_ids,
    )
    if assertion_issues:
        return None, [_compiler_issue(item) for item in assertion_issues], False, []
    graph_profiles = build_capability_graph(catalog)
    acknowledged = set(request.acknowledged_risks)
    specs: list[WorkflowSpecNode] = []
    layout_by_alias: dict[str, GraphPatchLayoutHint] = {}
    clone_aliases: dict[tuple[str, str], str] = {}
    for index, node_id in enumerate(sorted(clone_ids, key=_typed_key)):
        key = _typed_key(node_id)
        raw = raw_by_id.get(key)
        if raw is None:
            issues.append(_issue("clone_node_missing", "branch_id", f"Clone node {node_id!r} is absent from the serialized workflow."))
            continue
        node_type = raw.get("type") or raw.get("comfyClass") or raw.get("class_type")
        if isinstance(node_type, str):
            serialized_state_issues = _clone_serialized_state_issues(raw, node_type)
            issues.extend(serialized_state_issues)
            if serialized_state_issues:
                continue
        node_info = catalog.get(node_type) if isinstance(node_type, str) else None
        if not isinstance(node_type, str) or not isinstance(node_info, Mapping):
            issues.append(_issue("clone_node_schema_missing", "branch_id", f"Clone node {node_id!r} does not have a loaded exact schema."))
            continue
        profile = graph_profiles.profile(node_type)
        risks: set[CloneRisk] = set()
        origin = classify_node_origin(dict(node_info))
        if origin == "partner":
            risks.add("partner")
        if bool(node_info.get("api_node")):
            risks.add("api")
        if bool(node_info.get("output_node")):
            risks.add("output")
        if profile is None or profile.schema_status != "supported" or origin == "unknown":
            risks.add("opaque")
        if profile is not None and profile.heavy:
            risks.add("heavy")
        missing_risks = sorted(risks - acknowledged)
        if missing_risks:
            issues.append(
                _issue(
                    "clone_risk_acknowledgement_required",
                    "acknowledged_risks",
                    f"{node_type} requires explicit acknowledgement before cloning.",
                    details={"risks": missing_risks},
                )
            )
            continue
        connected = {
            edge.target_input
            for edge in graph.edges
            if _typed_key(edge.target_node_id) == key
        }
        values, value_issues = _clone_values(
            raw,
            node_type,
            node_info,
            connected_inputs=connected,
        )
        issues.extend(value_issues)
        layout = _layout_hint(raw, request.layout_offset)
        if layout is None:
            issues.append(_issue("clone_layout_unavailable", "layout_offset", f"{node_type} has no finite serialized position for deterministic offsetting."))
        if values is None or layout is None:
            continue
        alias = f"branch_clone_{index:03d}_{request.branch_id[:8]}"
        clone_aliases[key] = alias
        layout_by_alias[alias] = layout
        specs.append(
            WorkflowSpecNode(
                alias=alias,
                capability=f"exact locally loaded {node_type}",
                requested_node_type=node_type,
                preferred_node_types=[node_type],
                allowed_origins=[origin],
                values=values,
            )
        )
    if issues:
        return None, issues, False, sorted(clone_aliases.values())
    external_ids = {edge.source_node_id for edge in incoming}
    existing_aliases, selectors = _existing_aliases(external_ids, reserved=set(clone_aliases.values()))
    additions: list[RefinementSpecEdge] = []
    for edge in [*internal, *incoming]:
        source_key = _typed_key(edge.source_node_id)
        target_key = _typed_key(edge.target_node_id)
        additions.append(
            RefinementSpecEdge(
                source_alias=(
                    clone_aliases[source_key]
                    if source_key in clone_aliases
                    else existing_aliases[source_key]
                ),
                source_output=edge.source_output,
                source_output_index=edge.source_output_index,
                target_alias=clone_aliases[target_key],
                target_input=edge.target_input,
                target_mode="slot",
            )
        )
    semantic = _compile_semantic(
        request,
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        catalog=catalog,
        catalog_hash=catalog_hash,
        source=source,
        existing_nodes=selectors,
        create_nodes=specs,
        add_edges=additions,
        remove_edges=[],
        remove_nodes=[],
    )
    compiler_issues = [_compiler_issue(item) for item in semantic.get("issues", [])]
    if not semantic.get("valid"):
        return semantic, compiler_issues, bool(semantic.get("needs_choice")), sorted(clone_aliases.values())
    envelope = ApplyGraphPatchRequest.model_validate(semantic["apply_request"])
    plan = envelope.plan
    creates = [
        item.model_copy(update={"layout_hint": layout_by_alias[item.alias]})
        for item in plan.create_nodes
    ]
    graph_patch_request = PlanGraphPatchRequest(
        application_id=request.application_id,
        expected_workflow_identity=workflow_identity,
        expected_graph_hash=graph_hash,
        expected_catalog_hash=catalog_hash,
        graph=graph,
        assertions=assertions,
        create_nodes=creates,
        update_nodes=plan.update_nodes,
        remove_edges=plan.remove_edges,
        add_edges=plan.add_edges,
        remove_nodes=plan.remove_nodes,
        attachments=plan.attachments,
    )
    compiled = compile_graph_patch(
        graph_patch_request,
        catalog,
        catalog_hash=catalog_hash,
        source=source,
    )
    graph_issues = [_compiler_issue(item) for item in compiled.get("issues", [])]
    if not compiled.get("valid"):
        return compiled, [*compiler_issues, *graph_issues], False, sorted(clone_aliases.values())
    allowed = [
        (
            {("new", alias) for alias in clone_aliases.values()},
            {("new", alias) for alias in clone_aliases.values()},
        ),
        (
            {("existing", _typed_key(item)) for item in external_ids},
            {("new", alias) for alias in clone_aliases.values()},
        ),
    ]
    post_issues = _postcheck(
        compiled,
        removed_node_ids=[],
        incident_edges=[],
        created_aliases=set(clone_aliases.values()),
        allowed_additions=allowed,
        asserted_node_ids=[item.ref.node_id for item in assertions.nodes],
        asserted_edges=incident,
    )
    return compiled, [*clone_warnings, *compiler_issues, *graph_issues, *post_issues], False, sorted(clone_aliases.values())


def _scope_projection_catalog(
    catalog: Mapping[str, Any],
    projection: WorkflowScopeProjection,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Add compiler-only schemas for the projection's immutable boundaries."""

    collisions = sorted(
        node_type
        for node_type in (SCOPE_INPUT_NODE_TYPE, SCOPE_OUTPUT_NODE_TYPE)
        if node_type in catalog
    )
    if collisions:
        return None, [
            _issue(
                "scope_boundary_node_type_collision",
                "catalog",
                "The loaded catalog contains a class name reserved for private scoped "
                f"boundary projection: {', '.join(collisions)}.",
            )
        ]
    output_names = [item.name for item in projection.resolution.boundary_outputs]
    if len(output_names) != len(set(output_names)):
        return None, [
            _issue(
                "ambiguous_scope_boundary_name",
                "scope.boundary_outputs",
                "Nested branch lowering cannot address duplicate output-boundary names "
                "through the semantic edge contract; use a low-level scoped GraphPatch.",
            )
        ]
    result = dict(catalog)
    result[SCOPE_INPUT_NODE_TYPE] = {
        "input": {"required": {}},
        "output": [item.type for item in projection.resolution.boundary_inputs],
        "output_name": [item.name for item in projection.resolution.boundary_inputs],
        "python_module": "fl_mcp.workflow_scope",
    }
    result[SCOPE_OUTPUT_NODE_TYPE] = {
        "input": {
            "required": {
                item.name: [item.type, {"forceInput": True}]
                for item in projection.resolution.boundary_outputs
            }
        },
        "output": [],
        "output_name": [],
        "python_module": "fl_mcp.workflow_scope",
    }
    return result, []


def _nested_scope_context(
    request: BranchOperationPins,
    scope: WorkflowBranchScope,
    workflow: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> tuple[
    GraphPatchScopeAuthority | None,
    WorkflowScopeProjection | None,
    dict[str, Any] | None,
    list[dict[str, Any]],
]:
    path = [item.model_dump(mode="json") for item in scope.scope.scope_path]
    try:
        policy = resolve_workflow_scope_edit(
            workflow,
            path,
            requested_mode=request.scope_edit_mode,
            acknowledged_scope_paths=(
                [
                    [step.model_dump(mode="json") for step in item]
                    for item in request.affected_scope_paths
                ]
                if request.scope_edit_mode == "shared_definition"
                else None
            ),
        )
        if not policy.allowed:
            return None, None, None, [
                _issue(
                    "instance_detach_not_supported",
                    "scope_edit_mode",
                    "This definition is reused; instance-only detachment is not supported. "
                    "Choose shared_definition and acknowledge every affected scope path.",
                    details={
                        "affected_scope_paths": [
                            [step.model_dump(mode="json") for step in item]
                            for item in policy.affected_scope_paths
                        ]
                    },
                )
            ]
        projection = project_workflow_scope(
            workflow,
            path,
            expected_definition_hash=policy.selected.definition_hash,
        )
        authority = GraphPatchScopeAuthority(
            scope_path=policy.selected.scope_path,
            definition_id=policy.selected.definition_id,
            definition_hash=policy.selected.definition_hash,
            edit_mode=request.scope_edit_mode,
            affected_scope_paths=policy.affected_scope_paths,
        )
        scoped_catalog, catalog_issues = _scope_projection_catalog(catalog, projection)
        return authority, projection, scoped_catalog, catalog_issues
    except WorkflowScopeError as exc:
        return None, None, None, [exc.as_issue()]


def _compile_nested_branch_operation(
    request: BranchOperationRequest,
    scope: WorkflowBranchScope,
    branch: WorkflowBranchRecord,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
    bool,
    list[str],
]:
    authority, projection, scoped_catalog, context_issues = _nested_scope_context(
        request,
        scope,
        workflow,
        catalog,
    )
    if context_issues or authority is None or projection is None or scoped_catalog is None:
        return None, context_issues, False, []

    if isinstance(request, ReplaceBranchRequest):
        private_compiled, operation_issues, needs_choice, created_aliases = _replace(
            request,
            branch,
            projection.graph,
            projection.compiler_workflow,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            catalog=scoped_catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
    elif isinstance(request, RemoveBranchRequest):
        private_compiled, operation_issues, needs_choice = _remove(
            request,
            branch,
            projection.graph,
            projection.compiler_workflow,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            catalog=scoped_catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
        created_aliases = []
    else:
        private_compiled, operation_issues, needs_choice, created_aliases = _clone(
            request,
            branch,
            projection.graph,
            projection.compiler_workflow,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            catalog=scoped_catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
    if (
        not private_compiled
        or not private_compiled.get("valid")
        or any(item.get("severity") == "error" for item in operation_issues)
    ):
        return private_compiled, operation_issues, needs_choice, created_aliases
    try:
        public_request = scoped_graph_patch_request_from_private_plan(
            private_compiled["apply_request"],
            scope=authority,
            projection=projection,
        )
    except (TypeError, ValueError) as exc:
        return None, [
            _issue(
                "scoped_branch_lowering_failed",
                "compiler.apply_request",
                f"The projected branch plan could not be converted to GraphPatch v3: {exc}",
            )
        ], False, created_aliases
    compiled = compile_scoped_graph_patch(
        public_request,
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        catalog=catalog,
        catalog_hash=catalog_hash,
        source=source,
    )
    scoped_issues = [_compiler_issue(item) for item in compiled.get("issues", [])]
    return (
        compiled,
        [*operation_issues, *scoped_issues],
        bool(compiled.get("needs_choice")),
        created_aliases,
    )


def compile_workflow_branch_operation(
    request: BranchOperationRequest | Mapping[str, Any],
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
) -> dict[str, Any]:
    """Compile one exact root-scope branch operation without any side effect."""

    if isinstance(request, (CloneBranchRequest, ReplaceBranchRequest, RemoveBranchRequest)):
        _validate_branch_operation_request_budget(request.model_dump(mode="json"))
        validated = request
    else:
        _validate_branch_operation_request_budget(request)
        validated = _REQUEST_ADAPTER.validate_python(request)
    branch_catalog, scope, branch, graph, issues = _preflight(
        validated,
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
    )
    if issues or scope is None or branch is None or graph is None:
        return _result(
            validated,
            valid=False,
            issues=issues,
            branch_catalog_hash=branch_catalog.branch_catalog_hash,
        )
    compiled: dict[str, Any] | None
    needs_choice: bool
    created_aliases: list[str] = []
    if scope.scope.kind == "subgraph_instance":
        compiled, operation_issues, needs_choice, created_aliases = (
            _compile_nested_branch_operation(
                validated,
                scope,
                branch,
                workflow,
                workflow_identity=workflow_identity,
                graph_hash=graph_hash,
                catalog=catalog,
                catalog_hash=catalog_hash,
                source=source,
            )
        )
    elif isinstance(validated, ReplaceBranchRequest):
        compiled, operation_issues, needs_choice, created_aliases = _replace(
            validated,
            branch,
            graph,
            workflow,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            catalog=catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
    elif isinstance(validated, RemoveBranchRequest):
        compiled, operation_issues, needs_choice = _remove(
            validated,
            branch,
            graph,
            workflow,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            catalog=catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
    else:
        compiled, operation_issues, needs_choice, created_aliases = _clone(
            validated,
            branch,
            graph,
            workflow,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            catalog=catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
    all_issues = [*operation_issues]
    valid = bool(compiled and compiled.get("valid")) and not any(
        item.get("severity") == "error" for item in all_issues
    )
    scope_locators: list[PendingSuccessorScopeLocator] = []
    if valid and compiled is not None:
        scope_locators, lineage_issues = _pending_scope_locators(
            validated,
            branch_catalog,
            scope,
            branch,
            compiled,
        )
        all_issues.extend(lineage_issues)
        valid = not any(item.get("severity") == "error" for item in all_issues)
    predecessor_facts = _branch_edge_facts(branch)
    if valid and len(predecessor_facts) > _MAX_LINEAGE_PREDECESSOR_EDGES:
        all_issues.append(
            _issue(
                "branch_lineage_predecessor_edge_limit_exceeded",
                "branch_id",
                "The branch exceeds the bounded exact-edge lineage contract.",
                details={
                    "edge_count": len(predecessor_facts),
                    "limit": _MAX_LINEAGE_PREDECESSOR_EDGES,
                },
            )
        )
        valid = False
    compiled_plan = compiled.get("plan") if compiled is not None else None
    planned_created_aliases = (
        sorted(
            item["alias"]
            for item in compiled_plan.get("create_nodes", [])
            if isinstance(item, Mapping) and isinstance(item.get("alias"), str)
        )
        if isinstance(compiled_plan, Mapping)
        else []
    )
    pending = (
        PendingSuccessorLocator(
            application_id=validated.application_id,
            patch_hash=compiled["patch_hash"],
            expected_workflow_identity=workflow_identity,
            expected_branch_catalog_hash=branch_catalog.branch_catalog_hash,
            predecessor_branch_id=validated.branch_id,
            scope_path=scope.scope.scope_path,
            expected_state=(
                "branch_removed"
                if isinstance(validated, RemoveBranchRequest)
                else "created_region"
            ),
            created_aliases=planned_created_aliases,
            predecessor_edges=list(predecessor_facts.values()),
            predecessor_owned_node_ids=branch.owned_node_ids,
            scope_locators=scope_locators,
        )
        if valid
        else None
    )
    return _result(
        validated,
        valid=valid,
        issues=all_issues,
        branch_catalog_hash=branch_catalog.branch_catalog_hash,
        needs_choice=needs_choice,
        pending=pending,
        compiled=compiled,
    )


__all__ = [
    "BranchBypassMapping",
    "BranchEntryMapping",
    "BranchExitMapping",
    "BranchLayoutOffset",
    "BranchOperationIssue",
    "BranchOperationPins",
    "BranchOperationRequest",
    "BranchOperationResult",
    "BranchSuccessorResolutionResult",
    "BranchSuccessorScopeResult",
    "CloneBranchRequest",
    "PendingSuccessorLocator",
    "PendingSuccessorScopeLocator",
    "ResolveBranchSuccessorsRequest",
    "RemoveBranchRequest",
    "ReplaceBranchRequest",
    "WORKFLOW_BRANCH_OPERATION_SCHEMA",
    "WORKFLOW_BRANCH_SUCCESSOR_SCHEMA",
    "compile_workflow_branch_operation",
    "resolve_workflow_branch_successors",
]
