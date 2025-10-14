"""Pydantic models for message validation and query DSL."""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field
from datetime import datetime


# ============================================================================
# Message Protocol Models
# ============================================================================

class BaseMessage(BaseModel):
    """Base message structure - all messages must include session_id."""

    session_id: str = Field(..., description="Session ID for routing")
    type: str = Field(..., description="Message type")
    timestamp: Optional[str] = Field(
        default=None, description="ISO timestamp"
    )


# Client -> Server Messages

class Handshake(BaseMessage):
    """Initial handshake message from client."""

    type: Literal["handshake"] = "handshake"
    client_version: Optional[str] = Field(None, description="Client version")


class UserMessage(BaseMessage):
    """User message to agent."""

    type: Literal["user_message"] = "user_message"
    message: str = Field(..., description="User message content")


class ToolResult(BaseMessage):
    """Tool execution result from client."""

    type: Literal["tool_result"] = "tool_result"
    request_id: str = Field(..., description="Tool request ID")
    success: bool = Field(..., description="Whether tool executed successfully")
    data: Optional[Any] = Field(None, description="Tool result data")
    error: Optional[str] = Field(None, description="Error message if failed")
    execution_time_ms: float = Field(..., description="Execution time in milliseconds")


# Server -> Client Messages

class HandshakeAck(BaseMessage):
    """Handshake acknowledgment from server."""

    type: Literal["handshake_ack"] = "handshake_ack"
    status: Literal["ready", "reconnected"] = Field(
        ..., description="Connection status"
    )
    agent_context: Optional[Dict[str, Any]] = Field(
        None, description="Agent context for reconnection"
    )


class AgentResponse(BaseMessage):
    """Agent response message."""

    type: Literal["agent_response"] = "agent_response"
    content: str = Field(..., description="Agent response content")
    is_final: bool = Field(True, description="Whether this is the final response")
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Additional metadata"
    )


class ToolRequest(BaseMessage):
    """Tool execution request to client."""

    type: Literal["tool_request"] = "tool_request"
    request_id: str = Field(..., description="Unique request ID")
    tool_name: str = Field(..., description="Tool name to execute")
    parameters: Dict[str, Any] = Field(..., description="Tool parameters")
    timeout_ms: int = Field(30000, description="Timeout in milliseconds")


class TypingIndicator(BaseMessage):
    """Typing indicator message."""

    type: Literal["typing_indicator"] = "typing_indicator"
    is_typing: bool = Field(..., description="Whether agent is typing")


class ErrorMessage(BaseMessage):
    """Error message."""

    type: Literal["error"] = "error"
    error_code: str = Field(..., description="Error code")
    message: str = Field(..., description="Error message")
    details: Optional[Any] = Field(None, description="Additional error details")


# ============================================================================
# Query DSL Models
# ============================================================================

class FilterCondition(BaseModel):
    """Single filter condition."""

    field: str = Field(..., description="Field path (supports dot notation)")
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
    """Logical combination of filters."""

    operator: Literal["and", "or", "not"] = Field(
        ..., description="Logical operator"
    )
    filters: List[Union["LogicalFilter", FilterCondition]] = Field(
        ..., description="Nested filters"
    )


# Enable forward references
LogicalFilter.model_rebuild()


class TraversalConfig(BaseModel):
    """Graph traversal configuration."""

    direction: Literal["upstream", "downstream", "both"] = Field(
        ..., description="Traversal direction"
    )
    max_depth: Optional[int] = Field(
        None, description="Maximum traversal depth (None = unlimited)"
    )
    include_start_nodes: bool = Field(
        True, description="Whether to include starting nodes in result"
    )


class AggregationConfig(BaseModel):
    """Aggregation configuration."""

    operation: Literal["count", "sum", "avg", "min", "max", "list"] = Field(
        ..., description="Aggregation operation"
    )
    field: Optional[str] = Field(
        None, description="Field to aggregate (required for sum/avg/min/max)"
    )
    group_by: Optional[str] = Field(
        None, description="Field to group by"
    )


class WorkflowQuery(BaseModel):
    """Complete workflow query specification."""

    filters: Optional[Union[LogicalFilter, FilterCondition]] = Field(
        None, description="Filter conditions"
    )
    traversal: Optional[TraversalConfig] = Field(
        None, description="Graph traversal configuration"
    )
    aggregation: Optional[AggregationConfig] = Field(
        None, description="Aggregation configuration"
    )
    result_format: Literal["full", "summary", "ids", "scalar", "diagram"] = Field(
        "full", description="Result format"
    )
    limit: Optional[int] = Field(
        None, description="Maximum number of results"
    )
    offset: Optional[int] = Field(
        0, description="Result offset for pagination"
    )


# ============================================================================
# Agent Context Models
# ============================================================================

class ConversationMessage(BaseModel):
    """Single message in conversation history."""

    role: Literal["user", "assistant", "tool", "system"] = Field(
        ..., description="Message role"
    )
    content: str = Field(..., description="Message content")
    tool_name: Optional[str] = Field(None, description="Tool name if role is 'tool'")
    timestamp: datetime = Field(
        default_factory=datetime.now, description="Message timestamp"
    )


class SessionContext(BaseModel):
    """Session context data."""

    session_id: str = Field(..., description="Session ID")
    conversation_history: List[ConversationMessage] = Field(
        default_factory=list, description="Conversation history"
    )
    workflow_state: Dict[str, Any] = Field(
        default_factory=dict, description="Workflow state cache"
    )
    last_activity: datetime = Field(
        default_factory=datetime.now, description="Last activity timestamp"
    )
