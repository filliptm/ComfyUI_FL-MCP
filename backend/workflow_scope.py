"""Pure scope resolution for mutation-safe ComfyUI subgraph edits.

This module is deliberately read-only.  It resolves exact recursive subgraph
instance paths against one serialized root workflow, inventories every
reachable instance of a definition, and projects one definition into the
existing normalized-graph format for compiler use.  ComfyUI's virtual
subgraph input/output nodes (``-10``/``-20``) exist only inside that compiler
projection; public scope identity uses stable boundary-slot UUIDs instead.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)
from workflow_refinement import NormalizedGraphSnapshot, normalize_workflow_graph

WORKFLOW_SCOPE_SCHEMA = "fl-mcp.workflow-scope.v1"
WORKFLOW_SCOPE_DEFINITION_HASH_SCHEMA = "fl-mcp.workflow-scope-definition-hash.v2"
WORKFLOW_SCOPE_PROJECTION_SCHEMA = "fl-mcp.workflow-scope-projection.v1"

SCOPE_INPUT_NODE_ID = -10
SCOPE_OUTPUT_NODE_ID = -20
SCOPE_INPUT_NODE_TYPE = "__fl_mcp_scope_input__"
SCOPE_OUTPUT_NODE_TYPE = "__fl_mcp_scope_output__"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

NodeId: TypeAlias = StrictInt | StrictStr
LinkId: TypeAlias = StrictInt | StrictStr


def _canonical_hash(
    schema: str,
    value: Any,
    *,
    limits: WorkflowScopeLimits,
) -> str:
    _validate_strict_json(
        value,
        path="workflow.definitions.subgraphs",
        limits=limits,
    )
    payload = _canonical_typed_json({"schema": schema, "value": value})
    if len(payload) > limits.max_definition_json_bytes:
        raise WorkflowScopeError(
            "definition_json_size_exceeded",
            "workflow.definitions.subgraphs",
            "The canonical subgraph definition exceeds the configured byte limit of "
            f"{limits.max_definition_json_bytes}.",
        )
    return hashlib.sha256(payload).hexdigest()


def _canonical_typed_json(value: Any) -> bytes:
    """Encode exact JSON facts identically in Python and JavaScript.

    JSON's textual number spelling and object-key ordering differ between the
    two runtimes.  This ASCII typed encoding canonicalizes every accepted
    number to one IEEE-754 binary64 bit pattern (normalizing negative zero),
    sorts keys by Unicode scalar value, and length-prefixes UTF-8 strings.
    """

    item_type = type(value)
    if value is None:
        return b"n;"
    if item_type is bool:
        return b"b1;" if value else b"b0;"
    if item_type in {int, float}:
        try:
            number = float(value)
        except OverflowError as exc:
            raise WorkflowScopeError(
                "non_json_scope_value",
                "workflow.definitions.subgraphs",
                "JSON numbers must fit the browser's finite IEEE-754 Number domain.",
            ) from exc
        if not math.isfinite(number):
            raise WorkflowScopeError(
                "non_json_scope_value",
                "workflow.definitions.subgraphs",
                "JSON numbers must be finite.",
            )
        if number == 0:
            number = 0.0
        return b"d" + struct.pack(">d", number).hex().encode("ascii") + b";"
    if item_type is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkflowScopeError(
                "non_json_scope_value",
                "workflow.definitions.subgraphs",
                "JSON strings must be valid UTF-8.",
            ) from exc
        return (
            b"s"
            + str(len(encoded)).encode("ascii")
            + b":"
            + encoded.hex().encode("ascii")
            + b";"
        )
    if item_type is list:
        return (
            b"a"
            + str(len(value)).encode("ascii")
            + b":["
            + b"".join(_canonical_typed_json(item) for item in value)
            + b"];"
        )
    if item_type is dict:
        encoded_items = []
        for key in sorted(value):
            encoded_items.append(_canonical_typed_json(key))
            encoded_items.append(_canonical_typed_json(value[key]))
        return (
            b"o"
            + str(len(value)).encode("ascii")
            + b":{"
            + b"".join(encoded_items)
            + b"};"
        )
    raise WorkflowScopeError(
        "non_json_scope_value",
        "workflow.definitions.subgraphs",
        "A subgraph definition contains a value outside the exact JSON data model.",
    )


def _typed_id(value: int | str) -> dict[str, Any]:
    return {
        "kind": "int" if isinstance(value, int) and not isinstance(value, bool) else "str",
        "value": value,
    }


def _id_key(value: int | str) -> tuple[str, str]:
    return (
        "int" if isinstance(value, int) and not isinstance(value, bool) else "str",
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _id_sort_key(value: int | str) -> tuple[str, str]:
    return _id_key(value)


def _validate_id(value: Any, *, path: str, code: str) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise WorkflowScopeError(code, path, "The identifier must be an exact integer or string.")
    if isinstance(value, str) and not value:
        raise WorkflowScopeError(code, path, "String identifiers must not be empty.")
    return value


def _exact_text(value: Any, *, path: str, label: str, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowScopeError(
            "unresolved_scope_fact",
            path,
            f"{label} must be a non-empty string.",
        )
    if len(value) > max_length:
        raise WorkflowScopeError(
            "scope_fact_too_long",
            path,
            f"{label} exceeds {max_length} characters.",
        )
    return value


def _canonical_uuid(value: Any, *, path: str) -> str:
    value = _exact_text(value, path=path, label="Boundary slot ID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowScopeError(
            "invalid_boundary_slot_id",
            path,
            "Boundary slot IDs must be canonical UUIDs.",
        ) from exc
    canonical = str(parsed)
    if value != canonical:
        raise WorkflowScopeError(
            "invalid_boundary_slot_id",
            path,
            "Boundary slot IDs must use canonical lowercase UUID spelling.",
        )
    return canonical


class WorkflowScopeError(ValueError):
    """One classified, bounded scope-resolution failure."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message
        self.details = dict(details or {})

    def as_issue(self) -> dict[str, Any]:
        return {
            "severity": "error",
            "code": self.code,
            "path": self.path,
            "message": self.message,
            **({"details": self.details} if self.details else {}),
        }


class WorkflowScopeLimits(BaseModel):
    """Hard bounds for recursive instance expansion and graph projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_depth: StrictInt = Field(8, ge=1, le=32)
    max_definitions: StrictInt = Field(512, ge=1, le=4_096)
    max_instances: StrictInt = Field(512, ge=1, le=8_192)
    max_expanded_nodes: StrictInt = Field(20_000, ge=1, le=200_000)
    max_expanded_edges: StrictInt = Field(80_000, ge=0, le=800_000)
    max_scope_nodes: StrictInt = Field(5_000, ge=1, le=50_000)
    max_scope_edges: StrictInt = Field(20_000, ge=0, le=200_000)
    max_scope_ports: StrictInt = Field(2_000, ge=0, le=20_000)
    max_slots_per_node: StrictInt = Field(2_000, ge=0, le=20_000)
    max_widgets_per_node: StrictInt = Field(2_000, ge=0, le=20_000)
    max_definition_json_depth: StrictInt = Field(64, ge=1, le=256)
    max_definition_json_items: StrictInt = Field(200_000, ge=1, le=2_000_000)
    max_definition_json_bytes: StrictInt = Field(
        8_388_608,
        ge=1_024,
        le=67_108_864,
    )


WORKFLOW_SCOPE_LIMITS = WorkflowScopeLimits()


def _validate_strict_json(
    value: Any,
    *,
    path: str,
    limits: WorkflowScopeLimits,
) -> None:
    """Validate an exact, acyclic JSON tree within a bounded work budget."""

    item_count = 0
    text_bytes = 0
    active_containers: set[int] = set()

    def consume_item(item_path: str) -> None:
        nonlocal item_count
        item_count += 1
        if item_count > limits.max_definition_json_items:
            raise WorkflowScopeError(
                "definition_json_item_limit_exceeded",
                item_path,
                "The subgraph definition exceeds the configured JSON fact limit of "
                f"{limits.max_definition_json_items}.",
            )

    def consume_text(text: str, item_path: str) -> None:
        nonlocal text_bytes
        remaining = limits.max_definition_json_bytes - text_bytes
        if len(text) > remaining:
            raise WorkflowScopeError(
                "definition_json_size_exceeded",
                item_path,
                "The subgraph definition exceeds the configured JSON byte limit of "
                f"{limits.max_definition_json_bytes}.",
            )
        try:
            encoded_size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise WorkflowScopeError(
                "non_json_scope_value",
                item_path,
                "The subgraph definition contains a string that is not valid UTF-8.",
            ) from exc
        text_bytes += encoded_size
        if text_bytes > limits.max_definition_json_bytes:
            raise WorkflowScopeError(
                "definition_json_size_exceeded",
                item_path,
                "The subgraph definition exceeds the configured JSON byte limit of "
                f"{limits.max_definition_json_bytes}.",
            )

    def visit(item: Any, item_path: str, depth: int) -> None:
        if depth > limits.max_definition_json_depth:
            raise WorkflowScopeError(
                "definition_json_depth_exceeded",
                item_path,
                "The subgraph definition exceeds the configured JSON depth limit of "
                f"{limits.max_definition_json_depth}.",
            )
        consume_item(item_path)
        item_type = type(item)
        if item_type is dict:
            identity = id(item)
            if identity in active_containers:
                raise WorkflowScopeError(
                    "non_json_scope_value",
                    item_path,
                    "The subgraph definition contains a cyclic object.",
                )
            if item_count + (2 * len(item)) > limits.max_definition_json_items:
                raise WorkflowScopeError(
                    "definition_json_item_limit_exceeded",
                    item_path,
                    "The subgraph definition exceeds the configured JSON fact limit of "
                    f"{limits.max_definition_json_items}.",
                )
            active_containers.add(identity)
            try:
                for key, child in item.items():
                    if type(key) is not str:
                        raise WorkflowScopeError(
                            "non_json_scope_value",
                            item_path,
                            "JSON object keys must be exact strings.",
                        )
                    key_path = f"{item_path}.{key}" if key else f"{item_path}['']"
                    consume_item(key_path)
                    consume_text(key, key_path)
                    visit(child, key_path, depth + 1)
            finally:
                active_containers.remove(identity)
            return
        if item_type is list:
            identity = id(item)
            if identity in active_containers:
                raise WorkflowScopeError(
                    "non_json_scope_value",
                    item_path,
                    "The subgraph definition contains a cyclic array.",
                )
            if item_count + len(item) > limits.max_definition_json_items:
                raise WorkflowScopeError(
                    "definition_json_item_limit_exceeded",
                    item_path,
                    "The subgraph definition exceeds the configured JSON fact limit of "
                    f"{limits.max_definition_json_items}.",
                )
            active_containers.add(identity)
            try:
                for index, child in enumerate(item):
                    visit(child, f"{item_path}[{index}]", depth + 1)
            finally:
                active_containers.remove(identity)
            return
        if item_type is str:
            consume_text(item, item_path)
            return
        if item_type in {int, float}:
            try:
                number = float(item)
            except OverflowError as exc:
                raise WorkflowScopeError(
                    "non_json_scope_value",
                    item_path,
                    "JSON numbers must fit the browser's finite IEEE-754 Number domain.",
                ) from exc
            if not math.isfinite(number):
                raise WorkflowScopeError(
                    "non_json_scope_value",
                    item_path,
                    "JSON numbers must fit the browser's finite IEEE-754 Number domain.",
                )
            return
        if item_type is bool or item is None:
            return
        raise WorkflowScopeError(
            "non_json_scope_value",
            item_path,
            "The subgraph definition contains a value outside the exact JSON data model.",
        )

    visit(value, path, 0)


class WorkflowScopeStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    container_node_id: NodeId
    subgraph_id: str = Field(..., min_length=1, max_length=256)


_SCOPE_PATH_ADAPTER = TypeAdapter(list[WorkflowScopeStep])


class WorkflowBoundaryPortFact(BaseModel):
    """One exact immutable subgraph boundary endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scope_input", "scope_output"]
    slot_id: str = Field(..., min_length=36, max_length=36)
    slot_index: StrictInt = Field(..., ge=0)
    name: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=256)
    link_ids: list[LinkId] = Field(default_factory=list, max_length=200_000)


class WorkflowScopeResolution(BaseModel):
    """Exact public facts for one resolved subgraph instance path."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal[WORKFLOW_SCOPE_SCHEMA] = Field(WORKFLOW_SCOPE_SCHEMA, alias="schema")
    scope_path: list[WorkflowScopeStep] = Field(..., min_length=1, max_length=32)
    definition_id: str = Field(..., min_length=1, max_length=256)
    definition_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    definition_name: str | None = Field(None, max_length=512)
    node_count: StrictInt = Field(..., ge=0)
    edge_count: StrictInt = Field(..., ge=0)
    boundary_inputs: list[WorkflowBoundaryPortFact] = Field(default_factory=list)
    boundary_outputs: list[WorkflowBoundaryPortFact] = Field(default_factory=list)


class WorkflowScopeInstance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_path: list[WorkflowScopeStep] = Field(..., min_length=1, max_length=32)
    definition_id: str = Field(..., min_length=1, max_length=256)
    definition_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class WorkflowScopeInventory(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal[WORKFLOW_SCOPE_SCHEMA] = Field(WORKFLOW_SCOPE_SCHEMA, alias="schema")
    instances: list[WorkflowScopeInstance] = Field(default_factory=list)
    instance_count: StrictInt = Field(..., ge=0)
    definition_count: StrictInt = Field(..., ge=0)


class WorkflowScopeEditPolicy(BaseModel):
    """Safe edit effect for a selected instance path."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal[WORKFLOW_SCOPE_SCHEMA] = Field(WORKFLOW_SCOPE_SCHEMA, alias="schema")
    selected: WorkflowScopeResolution
    requested_mode: Literal["instance", "shared_definition"]
    status: Literal[
        "unique_definition",
        "shared_definition_requires_acknowledgement",
        "shared_definition",
    ]
    allowed: bool
    instance_count: StrictInt = Field(..., ge=1)
    affected_scope_paths: list[list[WorkflowScopeStep]] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_status(self) -> WorkflowScopeEditPolicy:
        if self.status == "shared_definition_requires_acknowledgement" and self.allowed:
            raise ValueError("an unacknowledged shared definition cannot be writable")
        if self.status != "shared_definition_requires_acknowledgement" and not self.allowed:
            raise ValueError("an acknowledged edit policy must be writable")
        if self.status == "unique_definition" and self.instance_count != 1:
            raise ValueError("unique_definition requires exactly one instance")
        if self.status.startswith("shared_definition") and self.instance_count < 2:
            raise ValueError("shared_definition requires multiple instances")
        return self


class WorkflowScopeProjection(BaseModel):
    """Public scope facts plus private compiler-only normalized graph material."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        arbitrary_types_allowed=True,
    )

    schema_: Literal[WORKFLOW_SCOPE_PROJECTION_SCHEMA] = Field(
        WORKFLOW_SCOPE_PROJECTION_SCHEMA,
        alias="schema",
    )
    resolution: WorkflowScopeResolution
    # Both fields are backend-only.  In particular, the compiler-added virtual
    # -10/-20 node identities never enter a public branch or GraphPatch identity.
    graph: NormalizedGraphSnapshot = Field(exclude=True, repr=False)
    compiler_workflow: dict[str, Any] = Field(exclude=True, repr=False)


@dataclass(frozen=True)
class _ValidatedDefinition:
    definition_hash: str
    definition_name: str | None
    boundary_inputs: list[WorkflowBoundaryPortFact]
    boundary_outputs: list[WorkflowBoundaryPortFact]
    graph: NormalizedGraphSnapshot
    compiler_workflow: dict[str, Any]


def _coerce_limits(
    limits: WorkflowScopeLimits | Mapping[str, Any] | None,
) -> WorkflowScopeLimits:
    return (
        limits
        if isinstance(limits, WorkflowScopeLimits)
        else WorkflowScopeLimits.model_validate(limits or {})
    )


def _coerce_scope_path(
    value: Sequence[WorkflowScopeStep | Mapping[str, Any]],
    *,
    limits: WorkflowScopeLimits,
) -> list[WorkflowScopeStep]:
    bounded = _bounded_sequence(
        value,
        path="scope_path",
        max_items=limits.max_depth,
        invalid_code="invalid_scope_path",
        limit_code="scope_depth_limit_exceeded",
        label="scope path",
    )
    try:
        path = _SCOPE_PATH_ADAPTER.validate_python(bounded)
    except Exception as exc:
        raise WorkflowScopeError(
            "invalid_scope_path",
            "scope_path",
            "The scope path must contain exact container-node and subgraph IDs.",
        ) from exc
    if not path:
        raise WorkflowScopeError(
            "scope_path_required",
            "scope_path",
            "A nested subgraph scope path is required.",
        )
    return path


def _bounded_sequence(
    value: Any,
    *,
    path: str,
    max_items: int,
    invalid_code: str,
    limit_code: str,
    label: str,
) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise WorkflowScopeError(
            invalid_code,
            path,
            f"The {label} must be an exact bounded sequence.",
        )
    if len(value) > max_items:
        raise WorkflowScopeError(
            limit_code,
            path,
            f"The {label} exceeds the configured limit of {max_items}.",
        )
    result: list[Any] = []
    for item in value:
        if len(result) >= max_items:
            raise WorkflowScopeError(
                limit_code,
                path,
                f"The {label} exceeds the configured limit of {max_items}.",
            )
        result.append(item)
    return result


def _path_payload(path: Sequence[WorkflowScopeStep]) -> list[dict[str, Any]]:
    return [
        {
            "container_node_id": _typed_id(step.container_node_id),
            "subgraph_id": step.subgraph_id,
        }
        for step in path
    ]


def _path_key(path: Sequence[WorkflowScopeStep]) -> tuple[tuple[str, str, str], ...]:
    return tuple((*_id_key(step.container_node_id), step.subgraph_id) for step in path)


def _path_sort_key(path: Sequence[WorkflowScopeStep]) -> tuple[Any, ...]:
    return len(path), _path_key(path)


def _definition_map(
    workflow: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits,
) -> dict[str, Mapping[str, Any]]:
    definitions = workflow.get("definitions")
    if not isinstance(definitions, Mapping):
        raise WorkflowScopeError(
            "invalid_definitions",
            "workflow.definitions",
            "Workflow definitions must be an object.",
        )
    raw = definitions.get("subgraphs", [])
    if isinstance(raw, Mapping):
        raw_length = len(raw)
        is_mapping = True
    elif isinstance(raw, list):
        raw_length = len(raw)
        is_mapping = False
    else:
        raise WorkflowScopeError(
            "invalid_subgraph_definitions",
            "workflow.definitions.subgraphs",
            "Subgraph definitions must be an array or mapping.",
        )
    if raw_length > limits.max_definitions:
        raise WorkflowScopeError(
            "definition_limit_exceeded",
            "workflow.definitions.subgraphs",
            f"The workflow exceeds the definition limit of {limits.max_definitions}.",
        )
    items = raw.items() if is_mapping else ((None, item) for item in raw)
    result: dict[str, Mapping[str, Any]] = {}
    for index, (mapping_id, item) in enumerate(items):
        if index >= limits.max_definitions:
            raise WorkflowScopeError(
                "definition_limit_exceeded",
                "workflow.definitions.subgraphs",
                f"The workflow exceeds the definition limit of {limits.max_definitions}.",
            )
        path = f"workflow.definitions.subgraphs[{index}]"
        if not isinstance(item, Mapping):
            raise WorkflowScopeError(
                "invalid_subgraph_definition",
                path,
                "Every subgraph definition must be an object.",
            )
        definition_id = item.get("id", mapping_id)
        definition_id = _exact_text(
            definition_id,
            path=f"{path}.id",
            label="Subgraph definition ID",
        )
        if definition_id in result:
            raise WorkflowScopeError(
                "duplicate_subgraph_definition_id",
                f"{path}.id",
                f"Subgraph definition {definition_id!r} is repeated.",
            )
        result[definition_id] = item
    return result


def _collection_items(
    value: Any,
    *,
    path: str,
    label: str,
    max_items: int,
    limit_code: str,
) -> list[tuple[Any, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        raw_length = len(value)
        is_mapping = True
    elif isinstance(value, list):
        raw_length = len(value)
        is_mapping = False
    else:
        raise WorkflowScopeError(
            f"invalid_scope_{label}",
            path,
            f"Scope {label} must be an array or mapping.",
        )
    if raw_length > max_items:
        raise WorkflowScopeError(
            limit_code,
            path,
            f"Scope {label} exceeds the configured limit of {max_items}.",
        )
    raw_items = value.items() if is_mapping else ((None, item) for item in value)
    result: list[tuple[Any, Mapping[str, Any]]] = []
    for index, (mapping_id, item) in enumerate(raw_items):
        if index >= max_items:
            raise WorkflowScopeError(
                limit_code,
                path,
                f"Scope {label} exceeds the configured limit of {max_items}.",
            )
        if not isinstance(item, Mapping):
            raise WorkflowScopeError(
                f"invalid_scope_{label[:-1]}",
                f"{path}[{index}]",
                f"Every scope {label[:-1]} must be an object.",
            )
        result.append((mapping_id, item))
    return result


def _scope_nodes(
    payload: Mapping[str, Any],
    *,
    path: str,
    definition: bool,
    max_items: int,
    limit_code: str,
) -> list[tuple[int | str, str, Mapping[str, Any]]]:
    result: list[tuple[int | str, str, Mapping[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for index, (mapping_id, item) in enumerate(
        _collection_items(
            payload.get("nodes", []),
            path=f"{path}.nodes",
            label="nodes",
            max_items=max_items,
            limit_code=limit_code,
        )
    ):
        node_id = _validate_id(
            item.get("id", mapping_id),
            path=f"{path}.nodes[{index}].id",
            code="invalid_scope_node_id",
        )
        key = _id_key(node_id)
        if key in seen:
            raise WorkflowScopeError(
                "duplicate_scope_node_id",
                f"{path}.nodes[{index}].id",
                f"Scope repeats typed node ID {node_id!r}.",
            )
        if definition and node_id in {SCOPE_INPUT_NODE_ID, SCOPE_OUTPUT_NODE_ID}:
            raise WorkflowScopeError(
                "virtual_node_id_collision",
                f"{path}.nodes[{index}].id",
                f"Node ID {node_id} is reserved for a virtual scope boundary.",
            )
        node_type = _exact_text(
            item.get("type") or item.get("comfyClass") or item.get("class_type"),
            path=f"{path}.nodes[{index}].type",
            label="Node type",
        )
        seen.add(key)
        result.append((node_id, node_type, item))
    return result


def _find_exact_scope_node(
    payload: Mapping[str, Any],
    node_id: int | str,
    *,
    path: str,
    definition: bool,
    limits: WorkflowScopeLimits,
) -> tuple[str, Mapping[str, Any]]:
    matches = [
        (node_type, item)
        for candidate_id, node_type, item in _scope_nodes(
            payload,
            path=path,
            definition=definition,
            max_items=(limits.max_scope_nodes if definition else limits.max_expanded_nodes),
            limit_code=(
                "scope_node_limit_exceeded" if definition else "expanded_node_limit_exceeded"
            ),
        )
        if _id_key(candidate_id) == _id_key(node_id)
    ]
    if not matches:
        raise WorkflowScopeError(
            "scope_container_missing",
            path,
            f"Scope container node {node_id!r} is absent.",
        )
    if len(matches) != 1:
        # _scope_nodes already rejects this, but keep the authority local.
        raise WorkflowScopeError(
            "scope_container_ambiguous",
            path,
            f"Scope container node {node_id!r} is ambiguous.",
        )
    return matches[0]


def workflow_definition_hash(
    definition: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits | Mapping[str, Any] | None = None,
) -> str:
    """Hash every JSON fact of one exact serialized subgraph definition."""

    if not isinstance(definition, Mapping):
        raise WorkflowScopeError(
            "invalid_subgraph_definition",
            "definition",
            "A subgraph definition object is required.",
        )
    return _canonical_hash(
        WORKFLOW_SCOPE_DEFINITION_HASH_SCHEMA,
        definition,
        limits=_coerce_limits(limits),
    )


def _validate_container_interface(
    container: Mapping[str, Any],
    boundary_inputs: Sequence[WorkflowBoundaryPortFact],
    boundary_outputs: Sequence[WorkflowBoundaryPortFact],
    *,
    path: str,
    limits: WorkflowScopeLimits,
) -> None:
    actual_inputs = _slot_manifest(
        container.get("inputs", []),
        path=f"{path}.inputs",
        limits=limits,
    )
    actual_outputs = _slot_manifest(
        container.get("outputs", []),
        path=f"{path}.outputs",
        limits=limits,
    )
    expected_inputs = [(item.name, item.type) for item in boundary_inputs]
    expected_outputs = [(item.name, item.type) for item in boundary_outputs]
    for direction, actual, expected in (
        ("inputs", actual_inputs, expected_inputs),
        ("outputs", actual_outputs, expected_outputs),
    ):
        if actual != expected:
            raise WorkflowScopeError(
                "scope_container_interface_mismatch",
                f"{path}.{direction}",
                f"The container's {direction} do not exactly attest the referenced boundary.",
                details={
                    "direction": direction,
                    "expected_count": len(expected),
                    "actual_count": len(actual),
                },
            )


def _resolve_payload(
    workflow: Mapping[str, Any],
    path: Sequence[WorkflowScopeStep],
    definitions: Mapping[str, Mapping[str, Any]],
    *,
    limits: WorkflowScopeLimits,
) -> tuple[Mapping[str, Any], _ValidatedDefinition]:
    payload: Mapping[str, Any] = workflow
    ancestry: set[str] = set()
    selected_validation: _ValidatedDefinition | None = None
    for index, step in enumerate(path):
        scope_label = "workflow" if index == 0 else f"scope_path[{index - 1}]"
        node_type, container = _find_exact_scope_node(
            payload,
            step.container_node_id,
            path=f"scope_path[{index}].container_node_id",
            definition=index > 0,
            limits=limits,
        )
        if node_type != step.subgraph_id:
            raise WorkflowScopeError(
                "scope_container_type_mismatch",
                f"scope_path[{index}].subgraph_id",
                f"Container {step.container_node_id!r} is {node_type!r}, not "
                f"{step.subgraph_id!r} in {scope_label}.",
            )
        if step.subgraph_id in ancestry:
            raise WorkflowScopeError(
                "recursive_subgraph_definition",
                f"scope_path[{index}].subgraph_id",
                f"Subgraph definition {step.subgraph_id!r} recursively references its ancestry.",
                details={"scope_path": _path_payload(path[: index + 1])},
            )
        definition = definitions.get(step.subgraph_id)
        if definition is None:
            raise WorkflowScopeError(
                "subgraph_definition_missing",
                f"scope_path[{index}].subgraph_id",
                f"Subgraph definition {step.subgraph_id!r} is absent.",
            )
        selected_validation = _validated_definition(definition, limits=limits)
        _validate_container_interface(
            container,
            selected_validation.boundary_inputs,
            selected_validation.boundary_outputs,
            path=f"scope_path[{index}].container",
            limits=limits,
        )
        ancestry.add(step.subgraph_id)
        payload = definition
    if selected_validation is None:
        raise WorkflowScopeError(
            "scope_path_required",
            "scope_path",
            "A nested subgraph scope path is required.",
        )
    return payload, selected_validation


def _slot_items(
    value: Any,
    *,
    path: str,
    max_items: int,
    limit_code: str,
) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        if len(value) > max_items:
            raise WorkflowScopeError(
                limit_code,
                path,
                f"Slots exceed the configured limit of {max_items}.",
            )
        slots: list[Any] = []
        for item in value:
            if len(slots) >= max_items:
                raise WorkflowScopeError(
                    limit_code,
                    path,
                    f"Slots exceed the configured limit of {max_items}.",
                )
            slots.append(item)
    elif isinstance(value, Mapping):
        if len(value) > max_items:
            raise WorkflowScopeError(
                limit_code,
                path,
                f"Slots exceed the configured limit of {max_items}.",
            )
        numeric: dict[int, Any] = {}
        named: dict[str, Any] = {}
        for position, (key, item) in enumerate(value.items()):
            if position >= max_items:
                raise WorkflowScopeError(
                    limit_code,
                    path,
                    f"Slots exceed the configured limit of {max_items}.",
                )
            try:
                index = int(key)
            except (TypeError, ValueError):
                if not isinstance(key, str) or not key:
                    raise WorkflowScopeError(
                        "invalid_scope_slots",
                        path,
                        "Slot mappings contain an invalid named key.",
                    ) from None
                named[key] = item
                continue
            if index < 0 or index in numeric:
                raise WorkflowScopeError(
                    "invalid_scope_slots",
                    path,
                    "Slot mappings contain an invalid or duplicate numeric index.",
                )
            numeric[index] = item
        if numeric and named:
            raise WorkflowScopeError(
                "mixed_scope_slot_keys",
                path,
                "Slot mappings cannot mix numeric and named keys.",
            )
        if numeric:
            if sorted(numeric) != list(range(len(numeric))):
                raise WorkflowScopeError(
                    "noncontiguous_scope_slot_keys",
                    path,
                    "Numeric slot mapping indexes must be contiguous.",
                )
            slots = [numeric[index] for index in range(len(numeric))]
        else:
            slots = [named[key] for key in sorted(named)]
    else:
        raise WorkflowScopeError(
            "invalid_scope_slots",
            path,
            "Slots must be an array or mapping.",
        )
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(slots):
        if not isinstance(item, Mapping):
            raise WorkflowScopeError(
                "invalid_scope_slot",
                f"{path}[{index}]",
                "Every slot must be an object.",
            )
        result.append(item)
    return result


def _link_id_list(
    value: Any,
    *,
    path: str,
    max_items: int,
) -> list[int | str]:
    if not isinstance(value, list):
        raise WorkflowScopeError(
            "invalid_boundary_link_ids",
            path,
            "Boundary linkIds must be an exact array.",
        )
    if len(value) > max_items:
        raise WorkflowScopeError(
            "boundary_link_limit_exceeded",
            path,
            f"Boundary linkIds exceeds the configured edge limit of {max_items}.",
        )
    result: list[int | str] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if index >= max_items:
            raise WorkflowScopeError(
                "boundary_link_limit_exceeded",
                path,
                f"Boundary linkIds exceed the configured edge limit of {max_items}.",
            )
        link_id = _validate_id(
            item,
            path=f"{path}[{index}]",
            code="invalid_scope_link_id",
        )
        key = _id_key(link_id)
        if key in seen:
            raise WorkflowScopeError(
                "duplicate_boundary_link_id",
                f"{path}[{index}]",
                f"Boundary link ID {link_id!r} is repeated.",
            )
        seen.add(key)
        result.append(link_id)
    return result


def _boundary_ports(
    definition: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits,
) -> tuple[list[WorkflowBoundaryPortFact], list[WorkflowBoundaryPortFact]]:
    input_node = definition.get("inputNode")
    output_node = definition.get("outputNode")
    if (
        not isinstance(input_node, Mapping)
        or type(input_node.get("id")) is not int
        or input_node.get("id") != SCOPE_INPUT_NODE_ID
    ):
        raise WorkflowScopeError(
            "virtual_boundary_id_mismatch",
            "definition.inputNode.id",
            f"The subgraph input boundary must have ID {SCOPE_INPUT_NODE_ID}.",
        )
    if (
        not isinstance(output_node, Mapping)
        or type(output_node.get("id")) is not int
        or output_node.get("id") != SCOPE_OUTPUT_NODE_ID
    ):
        raise WorkflowScopeError(
            "virtual_boundary_id_mismatch",
            "definition.outputNode.id",
            f"The subgraph output boundary must have ID {SCOPE_OUTPUT_NODE_ID}.",
        )
    result: dict[str, list[WorkflowBoundaryPortFact]] = {
        "scope_input": [],
        "scope_output": [],
    }
    seen_ids: set[str] = set()
    for kind, field in (("scope_input", "inputs"), ("scope_output", "outputs")):
        items = _slot_items(
            definition.get(field, []),
            path=f"definition.{field}",
            max_items=limits.max_scope_ports,
            limit_code="scope_port_limit_exceeded",
        )
        for index, item in enumerate(items):
            path = f"definition.{field}[{index}]"
            slot_id = _canonical_uuid(item.get("id"), path=f"{path}.id")
            if slot_id in seen_ids:
                raise WorkflowScopeError(
                    "duplicate_boundary_slot_id",
                    f"{path}.id",
                    f"Boundary slot UUID {slot_id!r} is repeated.",
                )
            seen_ids.add(slot_id)
            result[kind].append(
                WorkflowBoundaryPortFact(
                    kind=kind,
                    slot_id=slot_id,
                    slot_index=index,
                    name=_exact_text(item.get("name"), path=f"{path}.name", label="Slot name"),
                    type=_exact_text(item.get("type"), path=f"{path}.type", label="Slot type"),
                    link_ids=_link_id_list(
                        item.get("linkIds"),
                        path=f"{path}.linkIds",
                        max_items=limits.max_scope_edges,
                    ),
                )
            )
    return result["scope_input"], result["scope_output"]


def _alias_value(
    mapping: Mapping[str, Any],
    names: Sequence[str],
    *,
    path: str,
    field: str,
) -> Any:
    present = [(name, mapping[name]) for name in names if name in mapping]
    if not present:
        return None
    first_name, first_value = present[0]
    for _, value in present[1:]:
        if type(value) is not type(first_value) or value != first_value:
            raise WorkflowScopeError(
                "conflicting_scope_link_aliases",
                f"{path}.{field}",
                f"Link aliases for {field} must contain one exact typed value.",
                details={"aliases": [item[0] for item in present]},
            )
    return mapping[first_name]


def _link_parts(
    value: Any,
    *,
    path: str,
) -> tuple[int | str, int | str, int, int | str, int, str | None]:
    if isinstance(value, list):
        if not 5 <= len(value) <= 6:
            raise WorkflowScopeError(
                "invalid_scope_link",
                path,
                "Link arrays need five or six exact fields.",
            )
        link_id, source_id, source_slot, target_id, target_slot = value[:5]
        link_type = value[5] if len(value) > 5 else None
    elif isinstance(value, Mapping):
        link_id = _alias_value(value, ("id", "link_id"), path=path, field="id")
        source_id = _alias_value(
            value,
            ("origin_id", "source_id", "source_node_id", "from_node_id"),
            path=path,
            field="source_id",
        )
        source_slot = _alias_value(
            value,
            ("origin_slot", "source_slot", "source_output_index"),
            path=path,
            field="source_slot",
        )
        target_id = _alias_value(
            value,
            ("target_id", "target_node_id", "to_node_id"),
            path=path,
            field="target_id",
        )
        target_slot = _alias_value(
            value,
            ("target_slot", "target_input_index"),
            path=path,
            field="target_slot",
        )
        link_type = _alias_value(
            value,
            ("type", "link_type"),
            path=path,
            field="type",
        )
    else:
        raise WorkflowScopeError("invalid_scope_link", path, "Links must be arrays or objects.")
    link_id = _validate_id(link_id, path=f"{path}.id", code="invalid_scope_link_id")
    source_id = _validate_id(source_id, path=f"{path}.source", code="invalid_scope_link_source")
    target_id = _validate_id(target_id, path=f"{path}.target", code="invalid_scope_link_target")
    if isinstance(source_slot, bool) or not isinstance(source_slot, int) or source_slot < 0:
        raise WorkflowScopeError(
            "invalid_scope_link_source_slot",
            f"{path}.source_slot",
            "Link source slots must be non-negative integers.",
        )
    if isinstance(target_slot, bool) or not isinstance(target_slot, int) or target_slot < 0:
        raise WorkflowScopeError(
            "invalid_scope_link_target_slot",
            f"{path}.target_slot",
            "Link target slots must be non-negative integers.",
        )
    if link_type is not None:
        link_type = _exact_text(link_type, path=f"{path}.type", label="Link type")
    return link_id, source_id, source_slot, target_id, target_slot, link_type


def _raw_links(
    payload: Mapping[str, Any],
    *,
    path: str,
    max_items: int,
    limit_code: str,
) -> list[Any]:
    value = payload.get("links", [])
    if isinstance(value, Mapping):
        raw_length = len(value)
        is_mapping = True
    elif isinstance(value, list):
        raw_length = len(value)
        is_mapping = False
    else:
        raise WorkflowScopeError(
            "invalid_scope_links",
            path,
            "Scope links must be an array or mapping.",
        )
    if raw_length > max_items:
        raise WorkflowScopeError(
            limit_code,
            path,
            f"Scope links exceed the configured limit of {max_items}.",
        )
    iterator = value.values() if is_mapping else iter(value)
    result: list[Any] = []
    for item in iterator:
        if len(result) >= max_items:
            raise WorkflowScopeError(
                limit_code,
                path,
                f"Scope links exceed the configured limit of {max_items}.",
            )
        result.append(item)
    return result


def _slot_manifest(
    value: Any,
    *,
    path: str,
    limits: WorkflowScopeLimits,
) -> list[tuple[str, str]]:
    items = _slot_items(
        value,
        path=path,
        max_items=limits.max_slots_per_node,
        limit_code="node_slot_limit_exceeded",
    )
    result: list[tuple[str, str]] = []
    for index, item in enumerate(items):
        result.append(
            (
                _exact_text(item.get("name"), path=f"{path}[{index}].name", label="Slot name"),
                _exact_text(item.get("type"), path=f"{path}[{index}].type", label="Slot type"),
            )
        )
    return result


def _reroute_slot_declaration(
    raw: Mapping[str, Any],
    *,
    node_id: int | str,
    limits: WorkflowScopeLimits,
) -> tuple[str, str]:
    path = f"definition.nodes[{node_id!r}]"
    inputs = _slot_items(
        raw.get("inputs", []),
        path=f"{path}.inputs",
        max_items=limits.max_slots_per_node,
        limit_code="node_slot_limit_exceeded",
    )
    outputs = _slot_items(
        raw.get("outputs", []),
        path=f"{path}.outputs",
        max_items=limits.max_slots_per_node,
        limit_code="node_slot_limit_exceeded",
    )
    if len(inputs) != 1 or len(outputs) != 1:
        raise WorkflowScopeError(
            "invalid_reroute_shape",
            path,
            "A serialized Reroute must expose exactly one input and one output.",
        )
    input_name = inputs[0].get("name")
    output_name = outputs[0].get("name")
    input_type = inputs[0].get("type")
    output_type = outputs[0].get("type")
    if input_name != "" or output_name != "" or input_type != "*":
        raise WorkflowScopeError(
            "invalid_reroute_shape",
            path,
            "Only the exact ComfyUI blank-name, wildcard-input Reroute shape is supported.",
        )
    output_type = _exact_text(
        output_type,
        path=f"{path}.outputs[0].type",
        label="Reroute output type",
    )
    return input_type, output_type


def _widget_values(
    raw: Mapping[str, Any],
    *,
    node_id: int | str,
    limits: WorkflowScopeLimits,
) -> list[Any]:
    value = _validate_widget_values_shape(raw, node_id=node_id, limits=limits)
    return [deepcopy(item) for item in value]


def _validate_widget_values_shape(
    raw: Mapping[str, Any],
    *,
    node_id: int | str,
    limits: WorkflowScopeLimits,
) -> list[Any]:
    value = raw.get("widgets_values", [])
    path = f"definition.nodes[{node_id!r}].widgets_values"
    if not isinstance(value, list):
        raise WorkflowScopeError(
            "invalid_scope_widget_values",
            path,
            "Node widgets_values must be an exact array when present.",
        )
    if len(value) > limits.max_widgets_per_node:
        raise WorkflowScopeError(
            "node_widget_limit_exceeded",
            path,
            f"Node widgets exceed the configured limit of {limits.max_widgets_per_node}.",
        )
    return value


def _preflight_definition_bounds(
    definition: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits,
) -> None:
    """Reject oversized graph collections before hashing or copying metadata."""

    raw_nodes = _scope_nodes(
        definition,
        path="definition",
        definition=True,
        max_items=limits.max_scope_nodes,
        limit_code="scope_node_limit_exceeded",
    )
    _raw_links(
        definition,
        path="definition.links",
        max_items=limits.max_scope_edges,
        limit_code="scope_edge_limit_exceeded",
    )
    for field in ("inputs", "outputs"):
        ports = _slot_items(
            definition.get(field, []),
            path=f"definition.{field}",
            max_items=limits.max_scope_ports,
            limit_code="scope_port_limit_exceeded",
        )
        for index, port in enumerate(ports):
            _link_id_list(
                port.get("linkIds"),
                path=f"definition.{field}[{index}].linkIds",
                max_items=limits.max_scope_edges,
            )
    for node_id, _, raw in raw_nodes:
        for field in ("inputs", "outputs"):
            _slot_items(
                raw.get(field, []),
                path=f"definition.nodes[{node_id!r}].{field}",
                max_items=limits.max_slots_per_node,
                limit_code="node_slot_limit_exceeded",
            )
        _validate_widget_values_shape(raw, node_id=node_id, limits=limits)


def _validate_scope_graph(
    definition: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits,
) -> tuple[
    list[WorkflowBoundaryPortFact],
    list[WorkflowBoundaryPortFact],
    NormalizedGraphSnapshot,
    dict[str, Any],
]:
    raw_nodes = _scope_nodes(
        definition,
        path="definition",
        definition=True,
        max_items=limits.max_scope_nodes,
        limit_code="scope_node_limit_exceeded",
    )
    raw_links = _raw_links(
        definition,
        path="definition.links",
        max_items=limits.max_scope_edges,
        limit_code="scope_edge_limit_exceeded",
    )
    boundary_inputs, boundary_outputs = _boundary_ports(definition, limits=limits)
    node_slots: dict[tuple[str, str], tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {
        _id_key(SCOPE_INPUT_NODE_ID): (
            [],
            [(item.name, item.type) for item in boundary_inputs],
        ),
        _id_key(SCOPE_OUTPUT_NODE_ID): (
            [(item.name, item.type) for item in boundary_outputs],
            [],
        ),
    }
    compiler_nodes: list[dict[str, Any]] = []
    compiler_nodes_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    reroutes: dict[tuple[str, str], tuple[int | str, str, str]] = {}
    for node_id, node_type, raw in raw_nodes:
        node_key = _id_key(node_id)
        widgets = _widget_values(raw, node_id=node_id, limits=limits)
        if node_type == "Reroute":
            input_type, output_type = _reroute_slot_declaration(
                raw,
                node_id=node_id,
                limits=limits,
            )
            inputs = [("__fl_mcp_reroute_input_0__", input_type)]
            outputs = [("__fl_mcp_reroute_output_0__", output_type)]
            reroutes[node_key] = (node_id, input_type, output_type)
        else:
            inputs = _slot_manifest(
                raw.get("inputs", []),
                path=f"definition.nodes[{node_id!r}].inputs",
                limits=limits,
            )
            outputs = _slot_manifest(
                raw.get("outputs", []),
                path=f"definition.nodes[{node_id!r}].outputs",
                limits=limits,
            )
        node_slots[node_key] = (inputs, outputs)
        cloned = deepcopy(dict(raw))
        cloned["id"] = node_id
        cloned["widgets_values"] = widgets
        cloned["inputs"] = [{"name": name, "type": slot_type} for name, slot_type in inputs]
        cloned["outputs"] = [{"name": name, "type": slot_type} for name, slot_type in outputs]
        compiler_nodes.append(cloned)
        compiler_nodes_by_key[node_key] = cloned

    link_ids: set[tuple[str, str]] = set()
    endpoint_keys: set[tuple[Any, ...]] = set()
    occupied_target_slots: set[tuple[tuple[str, str], int]] = set()
    incoming: dict[tuple[str, str], set[tuple[str, str]]] = {key: set() for key in node_slots}
    outgoing: dict[tuple[str, str], set[tuple[str, str]]] = {key: set() for key in node_slots}
    observed_boundary_inputs: dict[int, list[int | str]] = {
        item.slot_index: [] for item in boundary_inputs
    }
    observed_boundary_outputs: dict[int, list[int | str]] = {
        item.slot_index: [] for item in boundary_outputs
    }
    parsed_links: list[tuple[int | str, int | str, int, int | str, int, str | None]] = []
    for index, raw_link in enumerate(raw_links):
        link_id, source_id, source_slot, target_id, target_slot, link_type = _link_parts(
            raw_link,
            path=f"definition.links[{index}]",
        )
        parsed_links.append((link_id, source_id, source_slot, target_id, target_slot, link_type))
        link_key = _id_key(link_id)
        if link_key in link_ids:
            raise WorkflowScopeError(
                "duplicate_scope_link_id",
                f"definition.links[{index}].id",
                f"Scope link ID {link_id!r} is repeated.",
            )
        link_ids.add(link_key)
        source_key = _id_key(source_id)
        target_key = _id_key(target_id)
        if source_key not in node_slots or target_key not in node_slots:
            raise WorkflowScopeError(
                "scope_link_endpoint_missing",
                f"definition.links[{index}]",
                "A scope link references a node absent from its definition.",
            )
        if target_id == SCOPE_INPUT_NODE_ID or source_id == SCOPE_OUTPUT_NODE_ID:
            raise WorkflowScopeError(
                "invalid_boundary_edge_direction",
                f"definition.links[{index}]",
                "Virtual input boundaries are sources and output boundaries are targets.",
            )
        source_outputs = node_slots[source_key][1]
        target_inputs = node_slots[target_key][0]
        if source_slot >= len(source_outputs) or target_slot >= len(target_inputs):
            raise WorkflowScopeError(
                "scope_link_slot_missing",
                f"definition.links[{index}]",
                "A scope link references a slot absent from its endpoint.",
            )
        target_slot_key = (target_key, target_slot)
        if target_slot_key in occupied_target_slots:
            is_scope_output = target_id == SCOPE_OUTPUT_NODE_ID
            raise WorkflowScopeError(
                "occupied_scope_output" if is_scope_output else "occupied_scope_input",
                f"definition.links[{index}]",
                (
                    "A subgraph output boundary may have at most one incoming link."
                    if is_scope_output
                    else "A node input may have at most one incoming physical link."
                ),
            )
        occupied_target_slots.add(target_slot_key)
        endpoint_key = (source_key, source_slot, target_key, target_slot)
        if endpoint_key in endpoint_keys:
            raise WorkflowScopeError(
                "duplicate_scope_edge",
                f"definition.links[{index}]",
                "The scope repeats one exact physical edge.",
            )
        endpoint_keys.add(endpoint_key)
        if source_id == SCOPE_INPUT_NODE_ID:
            observed_boundary_inputs[source_slot].append(link_id)
        if target_id == SCOPE_OUTPUT_NODE_ID:
            observed_boundary_outputs[target_slot].append(link_id)
        outgoing[source_key].add(target_key)
        incoming[target_key].add(source_key)

    for reroute_key, (node_id, input_type, output_type) in reroutes.items():
        incident_types: set[str] = set()
        incident_count = 0
        for _, source_id, _, target_id, _, link_type in parsed_links:
            if _id_key(source_id) != reroute_key and _id_key(target_id) != reroute_key:
                continue
            incident_count += 1
            if link_type is None or link_type == "*":
                raise WorkflowScopeError(
                    "unresolved_reroute_type",
                    f"definition.nodes[{node_id!r}]",
                    "Every physical Reroute link must attest one concrete type.",
                )
            incident_types.add(link_type)
        if incident_count == 0 or len(incident_types) != 1:
            raise WorkflowScopeError(
                "unresolved_reroute_type" if not incident_types else "reroute_type_mismatch",
                f"definition.nodes[{node_id!r}]",
                "A Reroute must resolve to exactly one concrete physical link type.",
            )
        resolved_type = next(iter(incident_types))
        if input_type != "*" or output_type not in {"*", resolved_type}:
            raise WorkflowScopeError(
                "reroute_type_mismatch",
                f"definition.nodes[{node_id!r}]",
                "The Reroute declaration disagrees with its physical link type.",
            )
        resolved_inputs = [("__fl_mcp_reroute_input_0__", resolved_type)]
        resolved_outputs = [("__fl_mcp_reroute_output_0__", resolved_type)]
        node_slots[reroute_key] = (resolved_inputs, resolved_outputs)
        compiler_node = compiler_nodes_by_key[reroute_key]
        compiler_node["inputs"] = [
            {"name": name, "type": slot_type} for name, slot_type in resolved_inputs
        ]
        compiler_node["outputs"] = [
            {"name": name, "type": slot_type} for name, slot_type in resolved_outputs
        ]

    compiler_links: list[list[Any]] = []
    for link_id, source_id, source_slot, target_id, target_slot, link_type in parsed_links:
        effective_type = link_type or node_slots[_id_key(source_id)][1][source_slot][1]
        compiler_links.append(
            [link_id, source_id, source_slot, target_id, target_slot, effective_type]
        )

    for fact in [*boundary_inputs, *boundary_outputs]:
        observed = (
            observed_boundary_inputs[fact.slot_index]
            if fact.kind == "scope_input"
            else observed_boundary_outputs[fact.slot_index]
        )
        if Counter(_id_key(item) for item in observed) != Counter(
            _id_key(item) for item in fact.link_ids
        ):
            field = "inputs" if fact.kind == "scope_input" else "outputs"
            raise WorkflowScopeError(
                "boundary_link_ids_mismatch",
                f"definition.{field}[{fact.slot_index}].linkIds",
                "Boundary linkIds do not match the exact physical scope links.",
                details={"slot_id": fact.slot_id},
            )
    indegree = {key: len(values) for key, values in incoming.items()}
    ready = deque(sorted((key for key, degree in indegree.items() if degree == 0)))
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in sorted(outgoing[source]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(node_slots):
        raise WorkflowScopeError(
            "scope_graph_cycle",
            "definition.links",
            "The scoped subgraph contains a directed cycle.",
        )

    compiler_workflow = {
        "nodes": [
            {
                "id": SCOPE_INPUT_NODE_ID,
                "type": SCOPE_INPUT_NODE_TYPE,
                "inputs": [],
                "outputs": [{"name": item.name, "type": item.type} for item in boundary_inputs],
                "widgets_values": [],
            },
            *compiler_nodes,
            {
                "id": SCOPE_OUTPUT_NODE_ID,
                "type": SCOPE_OUTPUT_NODE_TYPE,
                "inputs": [{"name": item.name, "type": item.type} for item in boundary_outputs],
                "outputs": [],
                "widgets_values": [],
            },
        ],
        "links": compiler_links,
    }
    try:
        graph = normalize_workflow_graph(compiler_workflow)
    except (TypeError, ValueError) as exc:
        raise WorkflowScopeError(
            "scope_graph_invalid",
            "definition",
            str(exc),
        ) from exc
    return boundary_inputs, boundary_outputs, graph, compiler_workflow


def _validated_definition(
    definition: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits,
) -> _ValidatedDefinition:
    _preflight_definition_bounds(definition, limits=limits)
    definition_hash = workflow_definition_hash(definition, limits=limits)
    name = definition.get("name")
    if name is not None:
        name = _exact_text(
            name,
            path="definition.name",
            label="Subgraph definition name",
            max_length=512,
        )
    boundary_inputs, boundary_outputs, graph, compiler_workflow = _validate_scope_graph(
        definition,
        limits=limits,
    )
    return _ValidatedDefinition(
        definition_hash=definition_hash,
        definition_name=name,
        boundary_inputs=boundary_inputs,
        boundary_outputs=boundary_outputs,
        graph=graph,
        compiler_workflow=compiler_workflow,
    )


def _resolution(
    path: Sequence[WorkflowScopeStep],
    validated: _ValidatedDefinition,
    *,
    expected_definition_hash: str | None,
) -> tuple[WorkflowScopeResolution, NormalizedGraphSnapshot, dict[str, Any]]:
    definition_hash = validated.definition_hash
    if expected_definition_hash is not None:
        if not isinstance(expected_definition_hash, str) or not _SHA256_PATTERN.fullmatch(
            expected_definition_hash
        ):
            raise WorkflowScopeError(
                "invalid_definition_hash",
                "expected_definition_hash",
                "Expected definition hashes must be lowercase SHA-256 digests.",
            )
        if definition_hash != expected_definition_hash:
            raise WorkflowScopeError(
                "scope_definition_changed",
                "expected_definition_hash",
                "The resolved subgraph definition changed after scope discovery.",
                details={
                    "expected_definition_hash": expected_definition_hash,
                    "actual_definition_hash": definition_hash,
                },
            )
    return (
        WorkflowScopeResolution(
            scope_path=list(path),
            definition_id=path[-1].subgraph_id,
            definition_hash=definition_hash,
            definition_name=validated.definition_name,
            node_count=len(validated.graph.nodes) - 2,
            edge_count=len(validated.graph.edges),
            boundary_inputs=validated.boundary_inputs,
            boundary_outputs=validated.boundary_outputs,
        ),
        validated.graph,
        validated.compiler_workflow,
    )


def resolve_workflow_scope(
    workflow: Mapping[str, Any],
    scope_path: Sequence[WorkflowScopeStep | Mapping[str, Any]],
    *,
    expected_definition_hash: str | None = None,
    limits: WorkflowScopeLimits | Mapping[str, Any] | None = None,
) -> WorkflowScopeResolution:
    """Resolve and fully validate one exact nested subgraph instance path."""

    if not isinstance(workflow, Mapping):
        raise WorkflowScopeError("invalid_workflow", "workflow", "Workflow must be an object.")
    active_limits = _coerce_limits(limits)
    path = _coerce_scope_path(scope_path, limits=active_limits)
    definitions = _definition_map(workflow, limits=active_limits)
    _, validated = _resolve_payload(
        workflow,
        path,
        definitions,
        limits=active_limits,
    )
    resolution, _, _ = _resolution(
        path,
        validated,
        expected_definition_hash=expected_definition_hash,
    )
    return resolution


def project_workflow_scope(
    workflow: Mapping[str, Any],
    scope_path: Sequence[WorkflowScopeStep | Mapping[str, Any]],
    *,
    expected_definition_hash: str | None = None,
    limits: WorkflowScopeLimits | Mapping[str, Any] | None = None,
) -> WorkflowScopeProjection:
    """Return one validated compiler projection for an exact nested scope."""

    if not isinstance(workflow, Mapping):
        raise WorkflowScopeError("invalid_workflow", "workflow", "Workflow must be an object.")
    active_limits = _coerce_limits(limits)
    path = _coerce_scope_path(scope_path, limits=active_limits)
    definitions = _definition_map(workflow, limits=active_limits)
    _, validated = _resolve_payload(
        workflow,
        path,
        definitions,
        limits=active_limits,
    )
    resolution, graph, compiler_workflow = _resolution(
        path,
        validated,
        expected_definition_hash=expected_definition_hash,
    )
    return WorkflowScopeProjection(
        resolution=resolution,
        graph=graph,
        compiler_workflow=compiler_workflow,
    )


def enumerate_workflow_scope_instances(
    workflow: Mapping[str, Any],
    *,
    limits: WorkflowScopeLimits | Mapping[str, Any] | None = None,
) -> WorkflowScopeInventory:
    """Enumerate every reachable subgraph instance without flattening scopes."""

    if not isinstance(workflow, Mapping):
        raise WorkflowScopeError("invalid_workflow", "workflow", "Workflow must be an object.")
    active_limits = _coerce_limits(limits)
    definitions = _definition_map(workflow, limits=active_limits)
    validated_definition_cache: dict[str, _ValidatedDefinition] = {}
    queue: deque[tuple[Mapping[str, Any], list[WorkflowScopeStep], tuple[str, ...], str]] = deque(
        [(workflow, [], (), "workflow")]
    )
    instances: list[WorkflowScopeInstance] = []
    expanded_nodes = 0
    expanded_edges = 0
    validated_definitions: set[str] = set()
    while queue:
        payload, parent_path, ancestry, payload_path = queue.popleft()
        remaining_nodes = active_limits.max_expanded_nodes - expanded_nodes
        node_limit = remaining_nodes
        node_limit_code = "expanded_node_limit_exceeded"
        if parent_path and active_limits.max_scope_nodes <= node_limit:
            node_limit = active_limits.max_scope_nodes
            node_limit_code = "scope_node_limit_exceeded"
        nodes = _scope_nodes(
            payload,
            path=payload_path,
            definition=bool(parent_path),
            max_items=node_limit,
            limit_code=node_limit_code,
        )
        remaining_edges = active_limits.max_expanded_edges - expanded_edges
        edge_limit = remaining_edges
        edge_limit_code = "expanded_edge_limit_exceeded"
        if parent_path and active_limits.max_scope_edges <= edge_limit:
            edge_limit = active_limits.max_scope_edges
            edge_limit_code = "scope_edge_limit_exceeded"
        links = _raw_links(
            payload,
            path=f"{payload_path}.links",
            max_items=edge_limit,
            limit_code=edge_limit_code,
        )
        expanded_nodes += len(nodes)
        expanded_edges += len(links)
        for node_id, node_type, raw_node in sorted(
            nodes,
            key=lambda item: _id_sort_key(item[0]),
        ):
            definition = definitions.get(node_type)
            if definition is None:
                continue
            child_path = [
                *parent_path,
                WorkflowScopeStep(container_node_id=node_id, subgraph_id=node_type),
            ]
            if len(child_path) > active_limits.max_depth:
                raise WorkflowScopeError(
                    "scope_depth_limit_exceeded",
                    payload_path,
                    f"Recursive scope expansion exceeds depth {active_limits.max_depth}.",
                )
            if node_type in ancestry:
                raise WorkflowScopeError(
                    "recursive_subgraph_definition",
                    payload_path,
                    f"Subgraph definition {node_type!r} recursively references its ancestry.",
                    details={"scope_path": _path_payload(child_path)},
                )
            if node_type not in validated_definitions:
                validated_definition_cache[node_type] = _validated_definition(
                    definition,
                    limits=active_limits,
                )
                validated_definitions.add(node_type)
            validated = validated_definition_cache[node_type]
            _validate_container_interface(
                raw_node,
                validated.boundary_inputs,
                validated.boundary_outputs,
                path=f"{payload_path}.container[{node_id!r}]",
                limits=active_limits,
            )
            if len(instances) >= active_limits.max_instances:
                raise WorkflowScopeError(
                    "instance_limit_exceeded",
                    payload_path,
                    f"Recursive scope expansion exceeds {active_limits.max_instances} instances.",
                )
            instances.append(
                WorkflowScopeInstance(
                    scope_path=child_path,
                    definition_id=node_type,
                    definition_hash=validated.definition_hash,
                )
            )
            queue.append(
                (
                    definition,
                    child_path,
                    (*ancestry, node_type),
                    f"scope[{len(child_path)}:{node_type}]",
                )
            )
    instances.sort(key=lambda item: _path_sort_key(item.scope_path))
    return WorkflowScopeInventory(
        instances=instances,
        instance_count=len(instances),
        definition_count=len(definitions),
    )


def resolve_workflow_scope_edit(
    workflow: Mapping[str, Any],
    scope_path: Sequence[WorkflowScopeStep | Mapping[str, Any]],
    *,
    requested_mode: Literal["instance", "shared_definition"] = "instance",
    acknowledged_scope_paths: Sequence[Sequence[WorkflowScopeStep | Mapping[str, Any]]]
    | None = None,
    expected_definition_hash: str | None = None,
    limits: WorkflowScopeLimits | Mapping[str, Any] | None = None,
) -> WorkflowScopeEditPolicy:
    """Resolve unique-vs-shared definition effects before any mutation.

    A reused definition is never silently treated as an instance-local graph.
    ``requested_mode='instance'`` returns a non-writable choice result.  An
    explicit shared-definition edit is writable only when the caller echoes
    the complete current set of affected instance paths.
    """

    active_limits = _coerce_limits(limits)
    bounded_acknowledgements = (
        None
        if acknowledged_scope_paths is None
        else _bounded_sequence(
            acknowledged_scope_paths,
            path="acknowledged_scope_paths",
            max_items=active_limits.max_instances,
            invalid_code="invalid_acknowledged_scope_paths",
            limit_code="acknowledgement_limit_exceeded",
            label="acknowledged scope path list",
        )
    )
    selected = resolve_workflow_scope(
        workflow,
        scope_path,
        expected_definition_hash=expected_definition_hash,
        limits=active_limits,
    )
    inventory = enumerate_workflow_scope_instances(workflow, limits=active_limits)
    affected = [
        item.scope_path
        for item in inventory.instances
        if item.definition_id == selected.definition_id
    ]
    affected.sort(key=_path_sort_key)
    selected_key = _path_key(selected.scope_path)
    if sum(_path_key(path) == selected_key for path in affected) != 1:
        raise WorkflowScopeError(
            "scope_inventory_mismatch",
            "scope_path",
            "The selected scope does not occur exactly once in the reachable inventory.",
        )
    if len(affected) == 1:
        if bounded_acknowledgements is not None:
            acknowledged = [
                _coerce_scope_path(path, limits=active_limits) for path in bounded_acknowledgements
            ]
            if Counter(_path_key(path) for path in acknowledged) != Counter(
                {_path_key(affected[0]): 1}
            ):
                raise WorkflowScopeError(
                    "affected_scope_paths_mismatch",
                    "acknowledged_scope_paths",
                    "Acknowledged scope paths do not match the unique affected scope.",
                )
        return WorkflowScopeEditPolicy(
            selected=selected,
            requested_mode=requested_mode,
            status="unique_definition",
            allowed=True,
            instance_count=1,
            affected_scope_paths=affected,
        )
    if requested_mode == "instance":
        return WorkflowScopeEditPolicy(
            selected=selected,
            requested_mode=requested_mode,
            status="shared_definition_requires_acknowledgement",
            allowed=False,
            instance_count=len(affected),
            affected_scope_paths=affected,
        )
    if bounded_acknowledgements is None:
        raise WorkflowScopeError(
            "affected_scope_paths_required",
            "acknowledged_scope_paths",
            "Shared-definition edits require every affected instance path.",
            details={"affected_scope_paths": [_path_payload(path) for path in affected]},
        )
    acknowledged = [
        _coerce_scope_path(path, limits=active_limits) for path in bounded_acknowledgements
    ]
    expected_keys = Counter(_path_key(path) for path in affected)
    acknowledged_keys = Counter(_path_key(path) for path in acknowledged)
    if acknowledged_keys != expected_keys:
        raise WorkflowScopeError(
            "affected_scope_paths_mismatch",
            "acknowledged_scope_paths",
            "Shared-definition acknowledgement must equal the complete affected path set.",
            details={
                "expected_scope_count": len(affected),
                "acknowledged_scope_count": len(acknowledged),
            },
        )
    return WorkflowScopeEditPolicy(
        selected=selected,
        requested_mode=requested_mode,
        status="shared_definition",
        allowed=True,
        instance_count=len(affected),
        affected_scope_paths=affected,
    )


__all__ = [
    "SCOPE_INPUT_NODE_ID",
    "SCOPE_INPUT_NODE_TYPE",
    "SCOPE_OUTPUT_NODE_ID",
    "SCOPE_OUTPUT_NODE_TYPE",
    "WORKFLOW_SCOPE_DEFINITION_HASH_SCHEMA",
    "WORKFLOW_SCOPE_LIMITS",
    "WORKFLOW_SCOPE_PROJECTION_SCHEMA",
    "WORKFLOW_SCOPE_SCHEMA",
    "WorkflowBoundaryPortFact",
    "WorkflowScopeEditPolicy",
    "WorkflowScopeError",
    "WorkflowScopeInstance",
    "WorkflowScopeInventory",
    "WorkflowScopeLimits",
    "WorkflowScopeProjection",
    "WorkflowScopeResolution",
    "WorkflowScopeStep",
    "enumerate_workflow_scope_instances",
    "project_workflow_scope",
    "resolve_workflow_scope",
    "resolve_workflow_scope_edit",
    "workflow_definition_hash",
]
