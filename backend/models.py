"""Pydantic models for bridge messages and workflow query DSL."""

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
REQUIRED_FRONTEND_TOOL_CONTRACT_REVISIONS = {
    "apply_workflow_graph_patch": 3,
    "confirm_mask_review": 3,
    "edit_node_mask": 5,
    "get_node_image_ref": 2,
    "get_canvas_image_refs": 1,
    "get_selected_nodes": 2,
    "queue_workflow": 3,
    "recover_narrow_operation": 1,
    "set_node_values_exact": 4,
}
ToolContractRevision = Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]


def canonical_tool_manifest_hash(supported_tools: List[str]) -> str:
    """Hash the exact sorted frontend tool manifest shared with JavaScript."""

    canonical = json.dumps(
        supported_tools,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_tool_contract_manifest_hash(
    tool_contract_revisions: Dict[str, int],
) -> str:
    """Hash the exact sorted per-tool implementation contract manifest."""

    canonical = json.dumps(
        {
            name: tool_contract_revisions[name]
            for name in sorted(tool_contract_revisions)
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class BaseMessage(BaseModel):
    """Base message structure. All bridge messages include a session ID."""

    session_id: str = Field(..., description="Session ID for routing")
    type: str = Field(..., description="Message type")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp")


class Handshake(BaseMessage):
    """Initial WebSocket handshake message."""

    type: Literal["handshake"] = "handshake"
    client_version: Optional[str] = Field(None, description="Client version")
    connection_type: Optional[Literal["frontend", "mcp"]] = Field(
        None,
        description="Explicit client role. Older clients may omit this field.",
    )
    client_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=128,
        description="Stable identity used to reconnect one client without replacing others.",
    )
    supported_tools: Optional[List[StrictStr]] = Field(
        None,
        max_length=512,
        description=(
            "Exact lexicographically sorted frontend handler names. Browser clients "
            "that omit this legacy-compatible field cannot receive tool requests."
        ),
    )
    tool_manifest_hash: Optional[StrictStr] = Field(
        None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Optional SHA-256 of compact canonical JSON for supported_tools."
        ),
    )
    tool_contract_revisions: Optional[Dict[StrictStr, ToolContractRevision]] = Field(
        None,
        max_length=512,
        description=(
            "Exact sorted frontend implementation-contract revision for every "
            "advertised tool handler."
        ),
    )
    tool_contract_manifest_hash: Optional[StrictStr] = Field(
        None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Optional SHA-256 of compact sorted-key JSON for tool_contract_revisions."
        ),
    )

    @model_validator(mode="after")
    def validate_tool_manifest(self) -> "Handshake":
        if self.supported_tools is None:
            if self.tool_manifest_hash is not None:
                raise ValueError("tool_manifest_hash requires supported_tools")
            if self.tool_contract_revisions is not None:
                raise ValueError("tool_contract_revisions requires supported_tools")
            if self.tool_contract_manifest_hash is not None:
                raise ValueError(
                    "tool_contract_manifest_hash requires tool_contract_revisions"
                )
            return self

        if self.connection_type == "mcp":
            raise ValueError("tool manifests are only valid for frontend clients")

        if any(_TOOL_NAME_RE.fullmatch(name) is None for name in self.supported_tools):
            raise ValueError(
                "supported_tools entries must be 1-128 character tool identifiers"
            )
        if self.supported_tools != sorted(set(self.supported_tools)):
            raise ValueError("supported_tools must be unique and lexicographically sorted")

        expected_hash = canonical_tool_manifest_hash(self.supported_tools)
        if (
            self.tool_manifest_hash is not None
            and self.tool_manifest_hash != expected_hash
        ):
            raise ValueError("tool_manifest_hash does not match supported_tools")

        if self.tool_contract_revisions is None:
            if self.tool_contract_manifest_hash is not None:
                raise ValueError(
                    "tool_contract_manifest_hash requires tool_contract_revisions"
                )
            return self
        revision_names = list(self.tool_contract_revisions)
        if any(_TOOL_NAME_RE.fullmatch(name) is None for name in revision_names):
            raise ValueError(
                "tool_contract_revisions keys must be 1-128 character tool identifiers"
            )
        if revision_names != sorted(revision_names):
            raise ValueError("tool_contract_revisions keys must be lexicographically sorted")
        if revision_names != self.supported_tools:
            raise ValueError(
                "tool_contract_revisions must contain every supported tool exactly once"
            )
        expected_contract_hash = canonical_tool_contract_manifest_hash(
            self.tool_contract_revisions
        )
        if (
            self.tool_contract_manifest_hash is not None
            and self.tool_contract_manifest_hash != expected_contract_hash
        ):
            raise ValueError(
                "tool_contract_manifest_hash does not match tool_contract_revisions"
            )
        return self


class ToolResult(BaseMessage):
    """Tool execution result from the browser bridge."""

    type: Literal["tool_result"] = "tool_result"
    request_id: str = Field(..., description="Tool request ID")
    success: bool = Field(..., description="Whether tool executed successfully")
    data: Optional[Any] = Field(None, description="Tool result data")
    error: Optional[str] = Field(None, description="Error message if failed")
    execution_time_ms: float = Field(..., description="Execution time in milliseconds")


class ScreenshotMessage(BaseMessage):
    """Screenshot data from the browser bridge."""

    type: Literal["screenshot"] = "screenshot"
    screenshot_id: str = Field(..., description="Unique screenshot ID")
    format: Literal["jpeg", "png"] = Field(..., description="Image format")
    size_bytes: int = Field(..., description="Image size in bytes")
    base64_data: str = Field(..., description="Base64 encoded image data")


class HandshakeAck(BaseMessage):
    """Handshake acknowledgment."""

    type: Literal["handshake_ack"] = "handshake_ack"
    status: Literal["ready", "reconnected"] = Field(..., description="Connection status")
    bridge_context: Optional[Dict[str, Any]] = Field(None, description="Bridge context")


class ErrorMessage(BaseMessage):
    """Error message."""

    type: Literal["error"] = "error"
    error_code: str = Field(..., description="Error code")
    message: str = Field(..., description="Error message")
    details: Optional[Any] = Field(None, description="Additional error details")


class FilterCondition(BaseModel):
    """Single workflow query filter condition."""

    field: str = Field(..., description="Field path, with dot notation support")
    operator: Literal[
        "equals",
        "not_equals",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "exists",
        "not_exists",
        "regex",
    ] = Field(..., description="Comparison operator")
    value: Optional[Any] = Field(None, description="Value to compare against")


class LogicalFilter(BaseModel):
    """Logical combination of workflow query filters."""

    operator: Literal["and", "or", "not"] = Field(..., description="Logical operator")
    filters: List[FilterCondition] = Field(..., description="Filter conditions")


class TraversalConfig(BaseModel):
    """Graph traversal configuration."""

    direction: Literal["upstream", "downstream", "both"] = Field(
        ..., description="Traversal direction"
    )
    max_depth: Optional[int] = Field(None, description="Maximum traversal depth")
    include_start_nodes: bool = Field(True, description="Include starting nodes")


class AggregationConfig(BaseModel):
    """Workflow query aggregation configuration."""

    operation: Literal["count", "sum", "avg", "min", "max", "list"] = Field(
        ..., description="Aggregation operation"
    )
    field: Optional[str] = Field(None, description="Field to aggregate")
    group_by: Optional[str] = Field(None, description="Field to group by")


class WorkflowQuery(BaseModel):
    """Workflow query specification."""

    filters: Optional[Union[LogicalFilter, FilterCondition]] = Field(
        None, description="Filter conditions"
    )
    traversal: Optional[TraversalConfig] = Field(None, description="Graph traversal")
    aggregation: Optional[AggregationConfig] = Field(None, description="Aggregation")
    result_format: Literal["full", "summary", "ids", "scalar", "diagram"] = Field(
        "full", description="Result format"
    )
    limit: Optional[int] = Field(None, description="Maximum results")
    offset: Optional[int] = Field(0, description="Result offset")


class SessionContext(BaseModel):
    """WebSocket session context."""

    session_id: str = Field(..., description="Session ID")
    workflow_state: Dict[str, Any] = Field(default_factory=dict, description="State cache")
    last_activity: datetime = Field(default_factory=datetime.now, description="Last activity")
