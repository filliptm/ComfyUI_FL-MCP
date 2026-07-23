"""Pydantic models for bridge messages and workflow query DSL."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


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
