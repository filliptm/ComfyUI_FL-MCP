"""Strict public request/result contracts for PR35 branch tools.

The topology, query, comparison, and mutation compilers remain pure in their
own modules.  This small adapter gives the MCP layer one bounded discovery
surface and one exact navigation request without exposing raw workflow values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)
from workflow_branch_queries import (
    ExactBranchEndpointAnchor,
    ResolveWorkflowBranchRequest,
    WorkflowBranchResolutionResult,
    resolve_workflow_branch,
)
from workflow_branches import (
    BranchScopeRef,
    BranchScopeStep,
    WorkflowBranchCatalog,
    WorkflowBranchRecord,
    WorkflowBranchSummary,
    discover_workflow_branches,
)

WORKFLOW_BRANCH_DISCOVERY_REQUEST_SCHEMA = (
    "fl-mcp.workflow-branch-discovery-request.v1"
)
WORKFLOW_BRANCH_DISCOVERY_RESULT_SCHEMA = (
    "fl-mcp.workflow-branch-discovery-result.v1"
)

NodeId = StrictInt | StrictStr
BranchKind = Literal["segment", "split_arm", "isolated"]


class DiscoverWorkflowBranchesRequest(BaseModel):
    """Bounded read-only discovery against the currently active workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal[WORKFLOW_BRANCH_DISCOVERY_REQUEST_SCHEMA] = Field(
        WORKFLOW_BRANCH_DISCOVERY_REQUEST_SCHEMA,
        alias="schema",
    )
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

    @model_validator(mode="after")
    def validate_filters(self) -> DiscoverWorkflowBranchesRequest:
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("kinds cannot contain duplicates")
        if self.endpoint_anchor is None and self.direction != "containing":
            raise ValueError(
                "upstream/downstream direction requires an exact endpoint_anchor"
            )
        return self


class WorkflowBranchDiscoveryResult(BaseModel):
    """A compact listing plus exact details for only one resolved branch."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal[WORKFLOW_BRANCH_DISCOVERY_RESULT_SCHEMA] = Field(
        WORKFLOW_BRANCH_DISCOVERY_RESULT_SCHEMA,
        alias="schema",
    )
    valid: bool
    workflow_identity: str = Field(..., min_length=1, max_length=512)
    graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    summary: WorkflowBranchSummary
    resolution: WorkflowBranchResolutionResult
    selected_scope: BranchScopeRef | None = None
    selected_branch: WorkflowBranchRecord | None = None
    read_only: Literal[True] = True
    queued: Literal[False] = False

    @model_validator(mode="after")
    def validate_selected_details(self) -> WorkflowBranchDiscoveryResult:
        resolved = self.resolution.status == "resolved"
        if resolved != (self.selected_scope is not None):
            raise ValueError("selected_scope must be present exactly for resolved status")
        if resolved != (self.selected_branch is not None):
            raise ValueError("selected_branch must be present exactly for resolved status")
        if self.valid != (self.resolution.status not in {"invalid_catalog", "stale"}):
            raise ValueError("valid must reflect catalog and pin validity")
        if self.resolution.workflow_identity != self.workflow_identity:
            raise ValueError("resolution workflow identity is inconsistent")
        if self.resolution.graph_hash != self.graph_hash:
            raise ValueError("resolution graph hash is inconsistent")
        if self.resolution.branch_catalog_hash != self.branch_catalog_hash:
            raise ValueError("resolution catalog hash is inconsistent")
        return self


class NavigateWorkflowBranchRequest(BaseModel):
    """Exact branch authority for a single atomic selection/focus operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_workflow_identity: str = Field(..., min_length=1, max_length=512)
    expected_graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")


def discover_workflow_branch_selection(
    request: DiscoverWorkflowBranchesRequest | Mapping[str, Any],
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
) -> tuple[WorkflowBranchCatalog, WorkflowBranchDiscoveryResult]:
    """Discover, filter, and expose exact details for one unique branch only."""

    active_request = (
        request
        if isinstance(request, DiscoverWorkflowBranchesRequest)
        else DiscoverWorkflowBranchesRequest.model_validate(request)
    )
    catalog = discover_workflow_branches(
        workflow,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
    )
    resolution = resolve_workflow_branch(
        ResolveWorkflowBranchRequest(
            expected_workflow_identity=workflow_identity,
            expected_graph_hash=graph_hash,
            expected_branch_catalog_hash=catalog.branch_catalog_hash,
            branch_id=active_request.branch_id,
            scope_path=active_request.scope_path,
            endpoint_anchor=active_request.endpoint_anchor,
            direction=active_request.direction,
            kinds=active_request.kinds,
            writable=active_request.writable,
            query=active_request.query,
            max_candidates=active_request.max_candidates,
            max_selectable_node_ids=active_request.max_selectable_node_ids,
            max_label_chars=active_request.max_label_chars,
            max_reachability_steps=active_request.max_reachability_steps,
        ),
        catalog,
        workflow=workflow,
        workflow_identity_attestation=workflow_identity,
        workflow_graph_hash=graph_hash,
    )
    selected_scope = None
    selected_branch = None
    if resolution.selected is not None:
        matches = [
            (scope, branch)
            for scope in catalog.scopes
            for branch in scope.branches
            if branch.branch_id == resolution.selected.branch_id
        ]
        if len(matches) == 1:
            scope, selected_branch = matches[0]
            selected_scope = scope.scope
    result = WorkflowBranchDiscoveryResult(
        schema=WORKFLOW_BRANCH_DISCOVERY_RESULT_SCHEMA,
        valid=resolution.status not in {"invalid_catalog", "stale"},
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        branch_catalog_hash=catalog.branch_catalog_hash,
        summary=catalog.summary,
        resolution=resolution,
        selected_scope=selected_scope,
        selected_branch=selected_branch,
    )
    return catalog, result


__all__ = [
    "DiscoverWorkflowBranchesRequest",
    "NavigateWorkflowBranchRequest",
    "WORKFLOW_BRANCH_DISCOVERY_REQUEST_SCHEMA",
    "WORKFLOW_BRANCH_DISCOVERY_RESULT_SCHEMA",
    "WorkflowBranchDiscoveryResult",
    "discover_workflow_branch_selection",
]
