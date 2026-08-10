"""Deterministic planning for atomic refinement of an existing workflow graph."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias

from node_library import (
    CATALOG_HASH_SCHEMA,
    NODE_SCHEMA_HASH_SCHEMA,
    node_schema_hash,
)
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from workflow_planner import (
    _canonical_widget_value,
    _expand_input_groups,
    _input_parts,
    _is_connectable,
    _output_slots,
    _types_compatible,
    _validate_widget_value,
)

NORMALIZED_GRAPH_SCHEMA = "fl-mcp.normalized-workflow-graph.v1"
WORKFLOW_REFINEMENT_SCHEMA = "fl-mcp.workflow-refinement.v1"
GRAPH_PRECONDITION_HASH_SCHEMA = "fl-mcp.graph-precondition.v1"
WORKFLOW_IDENTITY_SCHEMA = "fl-mcp.workflow-instance.v1"

_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_REROUTE_INPUT_NAME = "__fl_mcp_reroute_input_0__"
_REROUTE_OUTPUT_NAME = "__fl_mcp_reroute_output_0__"

NodeId: TypeAlias = StrictInt | StrictStr
SlotIndex: TypeAlias = Annotated[StrictInt, Field(ge=0)]


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _id_key(value: NodeId) -> tuple[str, str]:
    """Keep frontend-exact numeric and string IDs distinct."""

    return type(value).__name__, json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _edge_key(edge: NormalizedGraphEdge) -> tuple[Any, ...]:
    return (
        _id_key(edge.source_node_id),
        edge.source_output_index,
        _id_key(edge.target_node_id),
        edge.target_input_index,
    )


def _edge_exact_key(edge: NormalizedGraphEdge) -> tuple[Any, ...]:
    return (*_edge_key(edge), edge.source_output, edge.target_input, edge.type)


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"severity": "error", "code": code, "path": path, "message": message}


class NormalizedGraphNode(BaseModel):
    """One existing node identity from a complete editable workflow snapshot."""

    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    node_type: str = Field(..., min_length=1, max_length=256)
    widget_values: list[Any] = Field(default_factory=list, max_length=2_000)


class NormalizedGraphEdge(BaseModel):
    """One exact existing LiteGraph edge enriched with stable slot names and type."""

    model_config = ConfigDict(extra="forbid")

    source_node_id: NodeId
    source_output: str = Field(..., min_length=1, max_length=256)
    source_output_index: SlotIndex
    target_node_id: NodeId
    target_input: str = Field(..., min_length=1, max_length=256)
    target_input_index: SlotIndex
    type: str = Field(..., min_length=1, max_length=256)


class NormalizedGraphOutput(BaseModel):
    """One exact serialized output slot, including currently unconnected outputs."""

    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    output: str = Field(..., min_length=1, max_length=256)
    output_index: SlotIndex
    type: str = Field(..., min_length=1, max_length=256)


class NormalizedGraphSnapshot(BaseModel):
    """Complete, normalized graph facts used only for dry-run refinement planning."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal[NORMALIZED_GRAPH_SCHEMA] = Field(
        NORMALIZED_GRAPH_SCHEMA,
        alias="schema",
    )
    complete: Literal[True] = True
    nodes: list[NormalizedGraphNode] = Field(default_factory=list, max_length=5_000)
    outputs: list[NormalizedGraphOutput] = Field(default_factory=list, max_length=20_000)
    edges: list[NormalizedGraphEdge] = Field(default_factory=list, max_length=20_000)

    @property
    def schema(self) -> Literal[NORMALIZED_GRAPH_SCHEMA]:
        """Expose the public schema name without shadowing Pydantic's API."""

        return self.schema_


class WorkflowRefinementPath(BaseModel):
    """Ordered exact connections spanning two retained boundary nodes."""

    model_config = ConfigDict(extra="forbid")

    edges: list[NormalizedGraphEdge] = Field(default_factory=list, max_length=200)


class WorkflowRefinementExistingOutput(BaseModel):
    """One exact output on an existing, retained workflow node."""

    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    source_output: str | None = Field(None, min_length=1, max_length=256)
    source_output_index: SlotIndex | None = None

    @model_validator(mode="after")
    def validate_output_reference(self) -> WorkflowRefinementExistingOutput:
        if self.source_output is None and self.source_output_index is None:
            raise ValueError("source_output or source_output_index is required")
        return self


class WorkflowRefinementSideInputMapping(BaseModel):
    """Connect one retained-node output to an exact new-node side input."""

    model_config = ConfigDict(extra="forbid")

    source_node_id: NodeId
    source_output: str | None = Field(None, min_length=1, max_length=256)
    source_output_index: SlotIndex | None = None
    target_alias: str = Field(..., min_length=1, max_length=64)
    target_input: str = Field(..., min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_target_alias(self) -> WorkflowRefinementSideInputMapping:
        if self.source_output is None and self.source_output_index is None:
            raise ValueError("source_output or source_output_index is required")
        if not _ALIAS_PATTERN.fullmatch(self.target_alias):
            raise ValueError(
                "target_alias must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores"
            )
        return self


class WorkflowRefinementNode(BaseModel):
    """One replacement-chain node with its exact through-path slots."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, max_length=64)
    node_type: str = Field(..., min_length=1, max_length=256)
    values: dict[str, Any] = Field(default_factory=dict)
    chain_input: str = Field(..., min_length=1, max_length=256)
    chain_output: str | None = Field(None, min_length=1, max_length=256)
    chain_output_index: SlotIndex | None = None

    @model_validator(mode="after")
    def validate_alias(self) -> WorkflowRefinementNode:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only lowercase "
                "letters, digits, and underscores"
            )
        if self.chain_output is None and self.chain_output_index is not None:
            raise ValueError(
                "chain_output_index cannot be supplied without chain_output"
            )
        return self


class PlanWorkflowRefinementRequest(BaseModel):
    """Validate one exact chain splice without mutating the live workflow."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(..., min_length=8, max_length=128)
    expected_workflow_identity: str = Field(..., min_length=8, max_length=256)
    expected_graph_hash: str = Field(..., min_length=64, max_length=64)
    graph: NormalizedGraphSnapshot
    expected_path: WorkflowRefinementPath
    terminal_source: WorkflowRefinementExistingOutput | None = None
    side_input_mappings: list[WorkflowRefinementSideInputMapping] = Field(
        default_factory=list,
        max_length=100,
    )
    replacement_nodes: list[WorkflowRefinementNode] = Field(
        default_factory=list,
        max_length=100,
    )
    expected_catalog_hash: str | None = Field(None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_contract(self) -> PlanWorkflowRefinementRequest:
        if not _APPLICATION_ID_PATTERN.fullmatch(self.application_id):
            raise ValueError("application_id has an invalid format")
        if not _SHA256_PATTERN.fullmatch(self.expected_graph_hash):
            raise ValueError("expected_graph_hash must be a lowercase SHA-256 digest")
        if self.expected_catalog_hash and not _SHA256_PATTERN.fullmatch(
            self.expected_catalog_hash
        ):
            raise ValueError("expected_catalog_hash must be a lowercase SHA-256 digest")
        if self.terminal_source is not None:
            if self.expected_path.edges:
                raise ValueError(
                    "terminal_source append refinements must not define an expected path"
                )
            if not self.replacement_nodes:
                raise ValueError(
                    "terminal_source append refinements require replacement nodes"
                )
        else:
            if not self.expected_path.edges:
                raise ValueError(
                    "linear refinements require at least one expected path edge"
                )
        if self.side_input_mappings and not self.replacement_nodes:
            raise ValueError(
                "side_input_mappings require replacement nodes and cannot accompany deletion"
            )
        payload = self.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_048_576:
            raise ValueError("workflow refinement request must not exceed 1 MiB")
        return self


class CanonicalReplacementNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    node_type: str
    schema_hash: str
    values: dict[str, Any]


class CanonicalReplacementConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_alias: str
    source_output_index: SlotIndex
    source_output: str
    target_alias: str
    target_input_index: SlotIndex
    target_input: str
    type: str


class CanonicalReplacementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alias: str
    target_input_index: SlotIndex
    target_input: str
    type: str


class CanonicalReplacementOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_alias: str
    source_output_index: SlotIndex
    source_output: str
    type: str


class CanonicalReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[CanonicalReplacementNode] = Field(..., min_length=1, max_length=100)
    connections: list[CanonicalReplacementConnection] = Field(
        default_factory=list,
        max_length=99,
    )
    input: CanonicalReplacementInput
    output: CanonicalReplacementOutput

    @model_validator(mode="after")
    def validate_linear_shape(self) -> CanonicalReplacement:
        if len(self.connections) != len(self.nodes) - 1:
            raise ValueError("replacement must contain one fewer connection than nodes")
        for index, connection in enumerate(self.connections):
            if (
                connection.source_alias != self.nodes[index].alias
                or connection.target_alias != self.nodes[index + 1].alias
            ):
                raise ValueError("replacement connections must follow node order exactly")
        if self.input.target_alias != self.nodes[0].alias:
            raise ValueError("replacement input must target the first node")
        if self.output.source_alias != self.nodes[-1].alias:
            raise ValueError("replacement output must originate from the last node")
        return self


class CanonicalExistingInput(BaseModel):
    """One schema-pinned connection from an existing node into a new node."""

    model_config = ConfigDict(extra="forbid")

    source_node_id: NodeId
    source_node_type: str
    source_schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_output_index: SlotIndex
    source_output: str
    target_alias: str
    target_input_index: SlotIndex
    target_input: str
    type: str


class CanonicalReplacementWithSideInputs(BaseModel):
    """A linear replacement with additional retained-source fan-in edges."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[CanonicalReplacementNode] = Field(..., min_length=1, max_length=100)
    connections: list[CanonicalReplacementConnection] = Field(
        default_factory=list,
        max_length=99,
    )
    input: CanonicalReplacementInput
    side_inputs: list[CanonicalExistingInput] = Field(..., min_length=1, max_length=100)
    output: CanonicalReplacementOutput

    @model_validator(mode="after")
    def validate_linear_shape(self) -> CanonicalReplacementWithSideInputs:
        aliases = [node.alias for node in self.nodes]
        if len(self.connections) != len(aliases) - 1:
            raise ValueError("replacement must contain one fewer connection than nodes")
        for index, connection in enumerate(self.connections):
            if (
                connection.source_alias != aliases[index]
                or connection.target_alias != aliases[index + 1]
            ):
                raise ValueError("replacement connections must follow node order exactly")
        if self.input.target_alias != aliases[0]:
            raise ValueError("replacement input must target the first node")
        if self.output.source_alias != aliases[-1]:
            raise ValueError("replacement output must originate from the last node")
        occupied = {(self.input.target_alias, self.input.target_input_index)}
        occupied.update(
            (connection.target_alias, connection.target_input_index)
            for connection in self.connections
        )
        for mapping in self.side_inputs:
            if mapping.target_alias not in aliases:
                raise ValueError("replacement side input targets an unknown alias")
            target = (mapping.target_alias, mapping.target_input_index)
            if target in occupied:
                raise ValueError("replacement inputs must not target the same slot twice")
            occupied.add(target)
        return self


class CanonicalAppendReplacement(BaseModel):
    """A retained-source fan-in followed by one ordered created-node spine."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[CanonicalReplacementNode] = Field(..., min_length=1, max_length=100)
    connections: list[CanonicalReplacementConnection] = Field(
        default_factory=list,
        max_length=99,
    )
    input: None = None
    primary_input: CanonicalExistingInput
    side_inputs: list[CanonicalExistingInput] = Field(default_factory=list, max_length=100)
    output: CanonicalReplacementOutput | None = None

    @model_validator(mode="after")
    def validate_append_shape(self) -> CanonicalAppendReplacement:
        if len(self.connections) != len(self.nodes) - 1:
            raise ValueError("append replacement must form one ordered created-node spine")
        aliases = [node.alias for node in self.nodes]
        if len(set(aliases)) != len(aliases):
            raise ValueError("append replacement aliases must be unique")
        for index, connection in enumerate(self.connections):
            if (
                connection.source_alias != aliases[index]
                or connection.target_alias != aliases[index + 1]
            ):
                raise ValueError(
                    "append replacement connections must follow node order exactly"
                )
        if self.primary_input.target_alias != aliases[0]:
            raise ValueError("append primary input must target the first created node")
        target_slots = {
            (self.primary_input.target_alias, self.primary_input.target_input_index)
        }
        target_slots.update(
            (connection.target_alias, connection.target_input_index)
            for connection in self.connections
        )
        for mapping in self.side_inputs:
            if mapping.target_alias not in aliases:
                raise ValueError("append side input targets an unknown replacement alias")
            target = (mapping.target_alias, mapping.target_input_index)
            if target in target_slots:
                raise ValueError("append inputs must not target the same slot more than once")
            target_slots.add(target)
        if self.output is not None and self.output.source_alias != aliases[-1]:
            raise ValueError("append output must originate from the last created node")
        return self


class CanonicalExpectedNode(BaseModel):
    """Stable path identity without graph-only dynamic widget observations."""

    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    node_type: str = Field(..., min_length=1, max_length=256)


class CanonicalExpectedPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[CanonicalExpectedNode] = Field(default_factory=list, max_length=199)
    connections: list[NormalizedGraphEdge] = Field(
        default_factory=list,
        max_length=200,
    )

    @model_validator(mode="after")
    def validate_linear_shape(self) -> CanonicalExpectedPath:
        if not self.connections and not self.nodes:
            return self
        if len(self.connections) != len(self.nodes) + 1:
            raise ValueError("expected path requires one more connection than internal nodes")
        return self


class WorkflowRefinementPlan(BaseModel):
    """Exact canonical payload consumed by the frontend atomic splice engine."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["insert", "replace", "delete", "append"]
    expected_workflow_identity: str = Field(..., min_length=8, max_length=256)
    expected_graph_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_path: CanonicalExpectedPath
    replacement: (
        CanonicalReplacement
        | CanonicalReplacementWithSideInputs
        | CanonicalAppendReplacement
        | None
    )

    @model_validator(mode="after")
    def validate_operation(self) -> WorkflowRefinementPlan:
        internal_count = len(self.expected_path.nodes)
        connection_count = len(self.expected_path.connections)
        if self.operation == "append":
            if internal_count != 0 or connection_count != 0:
                raise ValueError("append requires an empty expected path")
            if not isinstance(self.replacement, CanonicalAppendReplacement):
                raise ValueError("append requires a canonical append replacement")
            return self
        if isinstance(self.replacement, CanonicalAppendReplacement):
            raise ValueError("only append may define a canonical append replacement")
        if self.operation == "insert" and internal_count != 0:
            raise ValueError("insert requires no internal expected-path nodes")
        if self.operation != "insert" and internal_count == 0:
            raise ValueError("replace and delete require internal expected-path nodes")
        if self.operation == "delete" and self.replacement is not None:
            raise ValueError("delete must not define a replacement")
        if self.operation != "delete" and self.replacement is None:
            raise ValueError("insert and replace require a replacement")
        return self


class ApplyWorkflowRefinementRequest(BaseModel):
    """Unchanged planner output accepted by the frontend atomic splice engine."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(..., min_length=8, max_length=128)
    expected_catalog_hash: str = Field(..., min_length=64, max_length=64)
    refinement_hash: str = Field(..., min_length=64, max_length=64)
    plan: WorkflowRefinementPlan

    @model_validator(mode="after")
    def validate_hashes_and_id(self) -> ApplyWorkflowRefinementRequest:
        if not _APPLICATION_ID_PATTERN.fullmatch(self.application_id):
            raise ValueError("application_id has an invalid format")
        if not _SHA256_PATTERN.fullmatch(self.expected_catalog_hash):
            raise ValueError("expected_catalog_hash must be a lowercase SHA-256 digest")
        if not _SHA256_PATTERN.fullmatch(self.refinement_hash):
            raise ValueError("refinement_hash must be a lowercase SHA-256 digest")
        canonical_hash = refinement_plan_hash(
            self.plan.model_dump(mode="json"),
            self.expected_catalog_hash,
        )
        if self.refinement_hash != canonical_hash:
            raise ValueError(
                "refinement_hash does not match the canonical plan and catalog hash"
            )
        payload = self.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_048_576:
            raise ValueError("workflow refinement apply request must not exceed 1 MiB")
        return self


def refinement_plan_hash(plan: Mapping[str, Any] | BaseModel, catalog_hash: str) -> str:
    """Bind one canonical apply plan to the exact loaded-node catalog generation."""

    canonical_plan = (
        plan.model_dump(mode="json") if isinstance(plan, BaseModel) else dict(plan)
    )
    return _canonical_hash(
        {
            "schema": WORKFLOW_REFINEMENT_SCHEMA,
            "catalog_hash": catalog_hash,
            "plan": canonical_plan,
        }
    )


def _mapping_value(value: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in value:
            return value[name]
    raise ValueError(f"missing required field; expected one of: {', '.join(names)}")


def _slot_sequence(value: Any, *, label: str) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        slots = value
    elif isinstance(value, Mapping):
        numeric: dict[int, Any] = {}
        named: dict[str, Any] = {}
        for key, item in value.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                if not isinstance(key, str) or not key:
                    raise ValueError(
                        f"{label} contains an invalid named slot key"
                    ) from None
                named[key] = item
                continue
            if index < 0 or index in numeric:
                raise ValueError(f"{label} contains an invalid slot index")
            numeric[index] = item
        if numeric and named:
            raise ValueError(f"{label} cannot mix numeric and named slot keys")
        if numeric:
            if sorted(numeric) != list(range(len(numeric))):
                raise ValueError(f"{label} slot indexes must be contiguous")
            slots = [numeric[index] for index in range(len(numeric))]
        else:
            # The graph precondition hash canonicalizes mapping keys. Sort named
            # slots identically so insertion order can never change slot indexes
            # beneath an otherwise identical graph hash.
            slots = [named[key] for key in sorted(named)]
    else:
        raise ValueError(f"{label} must be an array or mapping")
    if any(not isinstance(slot, Mapping) for slot in slots):
        raise ValueError(f"{label} contains a malformed slot")
    return list(slots)


def _is_concrete_reroute_link_type(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value != "*" and len(value) <= 256


def _reroute_slot_declaration(
    raw_node: Mapping[str, Any],
    *,
    node_id: NodeId,
    allow_private_slots: bool,
) -> tuple[str, str, bool]:
    """Validate the one native ComfyUI Reroute shape we can normalize safely."""

    inputs = _slot_sequence(
        raw_node.get("inputs", []),
        label=f"Reroute node {node_id!r} inputs",
    )
    outputs = _slot_sequence(
        raw_node.get("outputs", []),
        label=f"Reroute node {node_id!r} outputs",
    )
    if len(inputs) != 1 or len(outputs) != 1:
        raise ValueError(
            f"workflow Reroute node {node_id!r} must expose exactly one input and one output"
        )
    input_name = inputs[0].get("name")
    output_name = outputs[0].get("name")
    input_type = inputs[0].get("type")
    output_type = outputs[0].get("type")
    is_native = input_name == "" and output_name == "" and input_type == "*"
    is_private_resolved = (
        allow_private_slots
        and input_name == _REROUTE_INPUT_NAME
        and output_name == _REROUTE_OUTPUT_NAME
        and _is_concrete_reroute_link_type(input_type)
        and input_type == output_type
    )
    if not is_native and not is_private_resolved:
        raise ValueError(
            f"workflow Reroute node {node_id!r} is not the exact ComfyUI "
            "blank-name, wildcard-input shape"
        )
    if (
        not isinstance(output_type, str)
        or not output_type
        or len(output_type) > 256
    ):
        raise ValueError(
            f"workflow Reroute node {node_id!r} has no exact output type declaration"
        )
    return input_type, output_type, is_private_resolved


def _link_parts(raw: Any) -> tuple[NodeId, int, NodeId, int, Any]:
    if isinstance(raw, (list, tuple)):
        if len(raw) < 6:
            raise ValueError("serialized link arrays require six fields")
        _, source_id, source_slot, target_id, target_slot, link_type = raw[:6]
    elif isinstance(raw, Mapping):
        source_id = _mapping_value(raw, ("origin_id", "source_node_id", "source_id"))
        source_slot = _mapping_value(
            raw,
            ("origin_slot", "source_output_index", "source_slot"),
        )
        target_id = _mapping_value(raw, ("target_id", "target_node_id"))
        target_slot = _mapping_value(
            raw,
            ("target_slot", "target_input_index"),
        )
        link_type = raw.get("type")
    else:
        raise ValueError("serialized link must be an array or mapping")

    if isinstance(source_id, bool) or not isinstance(source_id, (int, str)):
        raise ValueError("serialized link has an invalid source node ID")
    if isinstance(target_id, bool) or not isinstance(target_id, (int, str)):
        raise ValueError("serialized link has an invalid target node ID")
    if isinstance(source_slot, bool) or not isinstance(source_slot, int) or source_slot < 0:
        raise ValueError("serialized link has an invalid source slot")
    if isinstance(target_slot, bool) or not isinstance(target_slot, int) or target_slot < 0:
        raise ValueError("serialized link has an invalid target slot")
    return source_id, source_slot, target_id, target_slot, link_type


def normalize_workflow_graph(
    workflow: Mapping[str, Any],
    *,
    allow_private_reroute_slots: bool = False,
) -> NormalizedGraphSnapshot:
    """Normalize editable Comfy workflow JSON or fail if any edge is incomplete.

    Both the classic six-item link arrays and mapping-shaped LiteGraph links are
    accepted. Slot names and types always come from the serialized endpoint nodes;
    a caller never gets a misleading ``complete=true`` snapshot with unresolved
    endpoints. ``allow_private_reroute_slots`` exists only for compiler-owned
    scope projections that have already replaced an attested native Reroute's
    blank slots with the private stable names used by GraphPatch.
    """

    if not isinstance(workflow, Mapping):
        raise ValueError("workflow must be an editable workflow JSON mapping")
    raw_nodes = workflow.get("nodes")
    if isinstance(raw_nodes, list):
        node_items = [(None, value) for value in raw_nodes]
    elif isinstance(raw_nodes, Mapping):
        node_items = list(raw_nodes.items())
    else:
        raise ValueError("workflow nodes must be an array or mapping")

    normalized_nodes: list[NormalizedGraphNode] = []
    normalized_outputs: list[NormalizedGraphOutput] = []
    node_slots: dict[tuple[str, str], tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]] = {}
    reroutes: dict[tuple[str, str], tuple[NodeId, str, str, bool]] = {}
    for index, (mapping_key, raw_node) in enumerate(node_items):
        if not isinstance(raw_node, Mapping):
            raise ValueError(f"workflow node {index} is malformed")
        node_id = raw_node.get("id", mapping_key)
        node_type = raw_node.get("type") or raw_node.get("comfyClass") or raw_node.get(
            "class_type"
        )
        if isinstance(node_id, bool) or not isinstance(node_id, (int, str)):
            raise ValueError(f"workflow node {index} has an invalid ID")
        if not isinstance(node_type, str) or not node_type:
            raise ValueError(f"workflow node {node_id!r} has no exact type")
        key = _id_key(node_id)
        if key in node_slots:
            raise ValueError(f"workflow contains duplicate node ID {node_id!r}")
        if node_type == "Reroute":
            input_type, output_type, is_private_resolved = _reroute_slot_declaration(
                raw_node,
                node_id=node_id,
                allow_private_slots=allow_private_reroute_slots,
            )
            inputs = [{"name": _REROUTE_INPUT_NAME, "type": input_type}]
            outputs = [{"name": _REROUTE_OUTPUT_NAME, "type": output_type}]
            reroutes[key] = (
                node_id,
                input_type,
                output_type,
                is_private_resolved,
            )
        else:
            inputs = _slot_sequence(
                raw_node.get("inputs", []),
                label=f"node {node_id!r} inputs",
            )
            outputs = _slot_sequence(
                raw_node.get("outputs", []),
                label=f"node {node_id!r} outputs",
            )
            for output_index, output in enumerate(outputs):
                output_name = output.get("name")
                output_type = output.get("type")
                if not all(
                    isinstance(item, str) and item
                    for item in (output_name, output_type)
                ):
                    raise ValueError(
                        f"workflow node {node_id!r} output {output_index} is unnamed or untyped"
                    )
                normalized_outputs.append(
                    NormalizedGraphOutput(
                        node_id=node_id,
                        output=output_name,
                        output_index=output_index,
                        type=output_type,
                    )
                )
        node_slots[key] = (inputs, outputs)
        raw_widget_values = raw_node.get("widgets_values", [])
        widget_values = list(raw_widget_values) if isinstance(raw_widget_values, list) else []
        normalized_nodes.append(
            NormalizedGraphNode(
                node_id=node_id,
                node_type=node_type,
                widget_values=widget_values,
            )
        )

    raw_links = workflow.get("links", [])
    if isinstance(raw_links, list):
        link_items = raw_links
    elif isinstance(raw_links, Mapping):
        link_items = list(raw_links.values())
    else:
        raise ValueError("workflow links must be an array or mapping")

    parsed_links: list[tuple[int, NodeId, int, NodeId, int, Any]] = []
    reroute_incident_counts = dict.fromkeys(reroutes, 0)
    reroute_incident_types: dict[tuple[str, str], set[str]] = {
        key: set() for key in reroutes
    }
    reroute_has_untyped_incident = dict.fromkeys(reroutes, False)
    for index, raw_link in enumerate(link_items):
        try:
            source_id, source_index, target_id, target_index, link_type = _link_parts(raw_link)
        except ValueError as exc:
            raise ValueError(f"workflow link {index} is malformed: {exc}") from exc
        source_slots = node_slots.get(_id_key(source_id))
        target_slots = node_slots.get(_id_key(target_id))
        if source_slots is None or target_slots is None:
            raise ValueError(f"workflow link {index} references a missing endpoint node")
        outputs = source_slots[1]
        inputs = target_slots[0]
        if source_index >= len(outputs) or target_index >= len(inputs):
            raise ValueError(f"workflow link {index} references a missing endpoint slot")
        parsed_links.append(
            (index, source_id, source_index, target_id, target_index, link_type)
        )
        source_key = _id_key(source_id)
        target_key = _id_key(target_id)
        for reroute_key in {source_key, target_key}.intersection(reroutes):
            reroute_incident_counts[reroute_key] += 1
            if not _is_concrete_reroute_link_type(link_type):
                reroute_has_untyped_incident[reroute_key] = True
            else:
                reroute_incident_types[reroute_key].add(link_type)

    for reroute_key in sorted(reroutes):
        node_id, input_type, output_type, is_private_resolved = reroutes[reroute_key]
        incident_types = reroute_incident_types[reroute_key]
        if (
            reroute_incident_counts[reroute_key] == 0
            or reroute_has_untyped_incident[reroute_key]
        ):
            raise ValueError(
                f"workflow Reroute node {node_id!r} requires every physical link "
                "to attest one concrete type"
            )
        if len(incident_types) != 1:
            raise ValueError(
                f"workflow Reroute node {node_id!r} must resolve to exactly one "
                "concrete physical link type"
            )
        resolved_type = next(iter(incident_types))
        declaration_matches = (
            input_type == output_type == resolved_type
            if is_private_resolved
            else input_type == "*" and output_type in {"*", resolved_type}
        )
        if not declaration_matches:
            raise ValueError(
                f"workflow Reroute node {node_id!r} declaration disagrees with its "
                "physical link type"
            )
        resolved_inputs = [{"name": _REROUTE_INPUT_NAME, "type": resolved_type}]
        resolved_outputs = [{"name": _REROUTE_OUTPUT_NAME, "type": resolved_type}]
        node_slots[reroute_key] = (resolved_inputs, resolved_outputs)
        normalized_outputs.append(
            NormalizedGraphOutput(
                node_id=node_id,
                output=_REROUTE_OUTPUT_NAME,
                output_index=0,
                type=resolved_type,
            )
        )

    normalized_edges: list[NormalizedGraphEdge] = []
    seen_edges: set[tuple[Any, ...]] = set()
    for index, source_id, source_index, target_id, target_index, link_type in parsed_links:
        outputs = node_slots[_id_key(source_id)][1]
        inputs = node_slots[_id_key(target_id)][0]
        output = outputs[source_index]
        target_input = inputs[target_index]
        output_name = output.get("name")
        input_name = target_input.get("name")
        output_type = output.get("type")
        input_type = target_input.get("type")
        if not all(
            isinstance(item, str) and item
            for item in (output_name, input_name, output_type, input_type)
        ):
            raise ValueError(f"workflow link {index} has an unnamed or untyped endpoint slot")
        if not _types_compatible(output_type, input_type):
            raise ValueError(f"workflow link {index} connects incompatible endpoint types")
        effective_type = link_type if isinstance(link_type, str) and link_type else output_type
        if not _types_compatible(effective_type, output_type) or not _types_compatible(
            effective_type,
            input_type,
        ):
            raise ValueError(f"workflow link {index} type disagrees with its endpoint slots")
        edge = NormalizedGraphEdge(
            source_node_id=source_id,
            source_output=output_name,
            source_output_index=source_index,
            target_node_id=target_id,
            target_input=input_name,
            target_input_index=target_index,
            type=effective_type,
        )
        key = _edge_key(edge)
        if key in seen_edges:
            raise ValueError(f"workflow contains duplicate edge endpoints at link {index}")
        seen_edges.add(key)
        normalized_edges.append(edge)

    normalized_nodes.sort(key=lambda item: _id_key(item.node_id))
    normalized_outputs.sort(
        key=lambda item: (_id_key(item.node_id), item.output_index, item.output, item.type)
    )
    normalized_edges.sort(key=_edge_exact_key)
    return NormalizedGraphSnapshot(
        schema=NORMALIZED_GRAPH_SCHEMA,
        complete=True,
        nodes=normalized_nodes,
        outputs=normalized_outputs,
        edges=normalized_edges,
    )


def _canonical_graph_facts(
    graph: NormalizedGraphSnapshot,
) -> tuple[
    dict[tuple[str, str], NormalizedGraphNode],
    dict[tuple[Any, ...], NormalizedGraphOutput],
    dict[tuple[Any, ...], NormalizedGraphEdge],
    list[dict[str, str]],
]:
    issues: list[dict[str, str]] = []
    nodes: dict[tuple[str, str], NormalizedGraphNode] = {}
    for index, node in enumerate(graph.nodes):
        key = _id_key(node.node_id)
        if key in nodes:
            issues.append(
                _issue(
                    "duplicate_graph_node_id",
                    f"graph.nodes[{index}].node_id",
                    f"Node ID {node.node_id!r} appears more than once.",
                )
            )
        else:
            nodes[key] = node

    outputs: dict[tuple[Any, ...], NormalizedGraphOutput] = {}
    for index, output in enumerate(graph.outputs):
        node_key = _id_key(output.node_id)
        key = (node_key, output.output_index)
        if node_key not in nodes:
            issues.append(
                _issue(
                    "graph_output_node_missing",
                    f"graph.outputs[{index}].node_id",
                    "An output slot references a node absent from the graph snapshot.",
                )
            )
        if key in outputs:
            issues.append(
                _issue(
                    "duplicate_graph_output",
                    f"graph.outputs[{index}]",
                    "The graph contains duplicate output indexes for one node.",
                )
            )
        else:
            outputs[key] = output

    edges: dict[tuple[Any, ...], NormalizedGraphEdge] = {}
    for index, edge in enumerate(graph.edges):
        key = _edge_key(edge)
        if _id_key(edge.source_node_id) not in nodes or _id_key(edge.target_node_id) not in nodes:
            issues.append(
                _issue(
                    "graph_edge_endpoint_missing",
                    f"graph.edges[{index}]",
                    "An edge references a node absent from the complete graph snapshot.",
                )
            )
        if key in edges:
            issues.append(
                _issue(
                    "duplicate_graph_edge",
                    f"graph.edges[{index}]",
                    "The graph contains duplicate edge endpoints.",
                )
            )
        else:
            edges[key] = edge
    return nodes, outputs, edges, issues


def _resolve_expected_path(
    request: PlanWorkflowRefinementRequest,
    graph_nodes: Mapping[tuple[str, str], NormalizedGraphNode],
    graph_edges: Mapping[tuple[Any, ...], NormalizedGraphEdge],
) -> tuple[list[NormalizedGraphEdge], list[NormalizedGraphNode], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    if not request.expected_path.edges:
        return [], [], issues
    path_edges: list[NormalizedGraphEdge] = []
    for index, expected in enumerate(request.expected_path.edges):
        observed = graph_edges.get(_edge_key(expected))
        if observed is None:
            issues.append(
                _issue(
                    "path_edge_missing",
                    f"expected_path.edges[{index}]",
                    "The exact expected path edge is absent from the graph snapshot.",
                )
            )
            continue
        if _edge_exact_key(observed) != _edge_exact_key(expected):
            issues.append(
                _issue(
                    "path_edge_changed",
                    f"expected_path.edges[{index}]",
                    "The edge endpoints exist, but its slot name or type changed.",
                )
            )
            continue
        path_edges.append(observed)

    for index in range(len(request.expected_path.edges) - 1):
        left = request.expected_path.edges[index]
        right = request.expected_path.edges[index + 1]
        if _id_key(left.target_node_id) != _id_key(right.source_node_id):
            issues.append(
                _issue(
                    "path_not_contiguous",
                    f"expected_path.edges[{index + 1}]",
                    "Expected path edges must form one ordered contiguous chain.",
                )
            )

    declared = request.expected_path.edges
    sequence = [declared[0].source_node_id, *[edge.target_node_id for edge in declared]]
    if len({_id_key(node_id) for node_id in sequence}) != len(sequence):
        issues.append(
            _issue(
                "path_cycle",
                "expected_path.edges",
                "Expected refinement path must not repeat a node or form a cycle.",
            )
        )

    internal_nodes: list[NormalizedGraphNode] = []
    for position, node_id in enumerate(sequence[1:-1]):
        node = graph_nodes.get(_id_key(node_id))
        if node is None:
            issues.append(
                _issue(
                    "path_node_missing",
                    f"expected_path.nodes[{position}]",
                    f"Internal path node {node_id!r} is absent from the graph snapshot.",
                )
            )
        else:
            internal_nodes.append(node)

    path_keys = {_edge_key(edge) for edge in request.expected_path.edges}
    for position, node in enumerate(internal_nodes):
        node_key = _id_key(node.node_id)
        touching = [
            edge
            for edge in graph_edges.values()
            if _id_key(edge.source_node_id) == node_key
            or _id_key(edge.target_node_id) == node_key
        ]
        unexpected = [edge for edge in touching if _edge_key(edge) not in path_keys]
        incoming = [edge for edge in touching if _id_key(edge.target_node_id) == node_key]
        outgoing = [edge for edge in touching if _id_key(edge.source_node_id) == node_key]
        if len(incoming) != 1 or len(outgoing) != 1 or unexpected:
            issues.append(
                _issue(
                    "non_linear_target",
                    f"expected_path.nodes[{position}]",
                    f"Node {node.node_id!r} has undeclared incident edges; removing it "
                    "could lose a sibling branch.",
                )
            )
    return path_edges, internal_nodes, issues


def _resolve_replacement_node(
    node: WorkflowRefinementNode,
    catalog: Mapping[str, Any],
    *,
    index: int,
    connected_inputs: set[str],
    output_required: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    path = f"replacement_nodes[{index}]"
    issues: list[dict[str, str]] = []
    raw_info = catalog.get(node.node_type)
    if not isinstance(raw_info, Mapping):
        return None, [
            _issue(
                "node_type_not_loaded",
                f"{path}.node_type",
                f"Node type {node.node_type!r} is not loaded in this ComfyUI instance.",
            )
        ]

    groups, dynamic_issues = _expand_input_groups(
        raw_info.get("input") if isinstance(raw_info.get("input"), dict) else {},
        node.values,
        connected_inputs,
    )
    for dynamic_issue in dynamic_issues:
        issues.append(
            _issue(
                dynamic_issue["code"],
                f"{path}.{dynamic_issue['path']}",
                dynamic_issue["message"],
            )
        )
    all_inputs = {**groups["required"], **groups["optional"]}
    ordered_connectable_inputs: list[tuple[str, Any]] = [
        (name, spec)
        for group in ("required", "optional")
        for name, spec in groups[group].items()
        if _is_connectable(spec)
    ]
    chain_matches = [
        (slot_index, name, spec)
        for slot_index, (name, spec) in enumerate(ordered_connectable_inputs)
        if name == node.chain_input
    ]
    if len(chain_matches) != 1:
        issues.append(
            _issue(
                "unknown_chain_input",
                f"{path}.chain_input",
                f"{node.node_type} has no unique connectable input named {node.chain_input!r}.",
            )
        )

    accepted_values: dict[str, Any] = {}
    for name, value in node.values.items():
        if name in connected_inputs:
            issues.append(
                _issue(
                    "value_for_connection_input",
                    f"{path}.values.{name}",
                    f"Connected input {name!r} is supplied by a connection and cannot "
                    "also have a value.",
                )
            )
            continue
        spec = all_inputs.get(name)
        if spec is None:
            issues.append(
                _issue(
                    "unknown_widget",
                    f"{path}.values.{name}",
                    f"{node.node_type} has no active runtime input named {name!r}.",
                )
            )
            continue
        value_issues = _validate_widget_value(name, spec, value)
        for value_issue in value_issues:
            issues.append(
                _issue(
                    value_issue["code"],
                    f"{path}.{value_issue['path']}",
                    value_issue["message"],
                )
            )
        if not value_issues:
            accepted_values[name] = _canonical_widget_value(spec, value)

    for group in ("required", "optional"):
        for name, spec in groups[group].items():
            if _is_connectable(spec):
                if group == "required" and name not in connected_inputs:
                    issues.append(
                        _issue(
                            "missing_required_connection",
                            f"{path}.inputs.{name}",
                            f"Required connection input {name!r} has no exact incoming mapping.",
                        )
                    )
                continue
            if name not in node.values:
                issues.append(
                    _issue(
                        "missing_widget_value",
                        f"{path}.values.{name}",
                        f"{node.node_type} needs an explicit value for active widget {name!r}.",
                    )
                )

    output_slots = _output_slots(raw_info)
    output_matches: list[dict[str, Any]] = []
    if node.chain_output is None:
        if output_required:
            issues.append(
                _issue(
                    "missing_chain_output",
                    f"{path}.chain_output",
                    f"{node.node_type} needs an exact chain output before another "
                    "created node or retained downstream boundary.",
                )
            )
    else:
        output_matches = [
            output
            for output in output_slots
            if output["name"] == node.chain_output
            and (
                node.chain_output_index is None
                or output["index"] == node.chain_output_index
            )
        ]
        if not output_matches:
            issues.append(
                _issue(
                    "unknown_chain_output",
                    f"{path}.chain_output",
                    f"{node.node_type} has no matching output named {node.chain_output!r}.",
                )
            )
        elif len(output_matches) > 1:
            issues.append(
                _issue(
                    "ambiguous_chain_output",
                    f"{path}.chain_output_index",
                    "The output name is not unique; provide chain_output_index.",
                )
            )

    output_valid = (
        node.chain_output is None and not output_required
    ) or len(output_matches) == 1
    if issues or len(chain_matches) != 1 or not output_valid:
        return None, issues
    input_index, input_name, input_spec = chain_matches[0]
    input_type, _ = _input_parts(input_spec)
    connectable_inputs: dict[str, dict[str, Any]] = {}
    for slot_index, (name, spec) in enumerate(ordered_connectable_inputs):
        input_type_for_slot, _ = _input_parts(spec)
        connectable_inputs[name] = {
            "index": slot_index,
            "name": name,
            "type": input_type_for_slot,
        }
    output = output_matches[0] if output_matches else None
    return {
        "alias": node.alias,
        "node_type": node.node_type,
        "schema_hash": node_schema_hash(node.node_type, raw_info),
        "schema_hash_schema": NODE_SCHEMA_HASH_SCHEMA,
        "values": accepted_values,
        "input": {"index": input_index, "name": input_name, "type": input_type},
        "inputs": connectable_inputs,
        "output": output,
    }, issues


def _resolve_existing_output(
    reference: WorkflowRefinementExistingOutput | WorkflowRefinementSideInputMapping,
    graph_nodes: Mapping[tuple[str, str], NormalizedGraphNode],
    graph_outputs: Mapping[tuple[Any, ...], NormalizedGraphOutput],
    catalog: Mapping[str, Any],
    *,
    path: str,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Resolve one retained existing-node output against the pinned live schema."""

    node_id = (
        reference.node_id
        if isinstance(reference, WorkflowRefinementExistingOutput)
        else reference.source_node_id
    )
    source_id_field = (
        "node_id"
        if isinstance(reference, WorkflowRefinementExistingOutput)
        else "source_node_id"
    )
    graph_node = graph_nodes.get(_id_key(node_id))
    if graph_node is None:
        return None, [
            _issue(
                "existing_source_node_missing",
                f"{path}.{source_id_field}",
                f"Existing source node {node_id!r} is absent from the graph snapshot.",
            )
        ]
    canvas_matches = [
        output
        for (output_node_key, _), output in graph_outputs.items()
        if output_node_key == _id_key(node_id)
        and (
            reference.source_output is None
            or output.output == reference.source_output
        )
        and (
            reference.source_output_index is None
            or output.output_index == reference.source_output_index
        )
    ]
    if not canvas_matches:
        return None, [
            _issue(
                "active_source_output_missing",
                path,
                "The active canvas source node has no output matching the supplied "
                "name/index.",
            )
        ]
    if len(canvas_matches) > 1:
        return None, [
            _issue(
                "active_source_output_ambiguous",
                path,
                "The active canvas output name is not unique; provide its exact index.",
            )
        ]
    canvas_output = canvas_matches[0]
    raw_info = catalog.get(graph_node.node_type)
    if not isinstance(raw_info, Mapping):
        return None, [
            _issue(
                "existing_source_type_not_loaded",
                f"{path}.{source_id_field}",
                f"Existing source node {node_id!r} uses unloaded type "
                f"{graph_node.node_type!r}.",
            )
        ]
    matches = [
        output
        for output in _output_slots(raw_info)
        if output["name"] == canvas_output.output
        and output["index"] == canvas_output.output_index
        and output["type"] == canvas_output.type
    ]
    if not matches:
        return None, [
            _issue(
                "existing_source_schema_mismatch",
                path,
                f"The active {graph_node.node_type} output no longer matches its "
                "loaded catalog name, index, and type.",
            )
        ]
    if len(matches) > 1:
        return None, [
            _issue(
                "existing_source_output_ambiguous",
                path,
                "The existing source output is not unique; provide its exact index.",
            )
        ]
    return {
        "source_node_id": node_id,
        "source_node_type": graph_node.node_type,
        "source_schema_hash": node_schema_hash(graph_node.node_type, raw_info),
        "source_output_index": canvas_output.output_index,
        "source_output": canvas_output.output,
        "type": canvas_output.type,
    }, []


def _refinement_has_cycle(
    graph_edges: Mapping[tuple[Any, ...], NormalizedGraphEdge],
    *,
    removed_path_edges: Sequence[NormalizedGraphEdge],
    removed_nodes: Sequence[NormalizedGraphNode],
    planned_edges: Sequence[tuple[tuple[Any, ...], tuple[Any, ...]]],
) -> bool:
    """Return whether retained graph edges plus planned edges contain a cycle."""

    removed_edge_keys = {_edge_key(edge) for edge in removed_path_edges}
    removed_node_keys = {_id_key(node.node_id) for node in removed_nodes}
    adjacency: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {}
    indegree: dict[tuple[Any, ...], int] = {}

    def existing(node_id: NodeId) -> tuple[Any, ...]:
        return ("existing", *_id_key(node_id))

    def add_edge(source: tuple[Any, ...], target: tuple[Any, ...]) -> None:
        targets = adjacency.setdefault(source, set())
        indegree.setdefault(source, 0)
        indegree.setdefault(target, 0)
        if target not in targets:
            targets.add(target)
            indegree[target] += 1

    for edge in graph_edges.values():
        if _edge_key(edge) in removed_edge_keys:
            continue
        if (
            _id_key(edge.source_node_id) in removed_node_keys
            or _id_key(edge.target_node_id) in removed_node_keys
        ):
            continue
        add_edge(existing(edge.source_node_id), existing(edge.target_node_id))
    for source, target in planned_edges:
        add_edge(source, target)

    ready = deque(vertex for vertex, degree in indegree.items() if degree == 0)
    visited_count = 0
    while ready:
        vertex = ready.popleft()
        visited_count += 1
        for target in adjacency.get(vertex, ()):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited_count != len(indegree)


def compile_workflow_refinement(
    request: PlanWorkflowRefinementRequest,
    catalog: Mapping[str, Any],
    *,
    catalog_hash: str,
    source: str,
) -> dict[str, Any]:
    """Compile one graph-hash-pinned splice or retained-source append."""

    issues: list[dict[str, str]] = []
    graph_nodes, graph_outputs, graph_edges, graph_issues = _canonical_graph_facts(
        request.graph
    )
    issues.extend(graph_issues)
    path_edges, internal_nodes, path_issues = _resolve_expected_path(
        request,
        graph_nodes,
        graph_edges,
    )
    issues.extend(path_issues)

    if request.expected_catalog_hash and request.expected_catalog_hash != catalog_hash:
        issues.append(
            _issue(
                "catalog_changed",
                "expected_catalog_hash",
                "The loaded-node catalog changed after discovery.",
            )
        )

    if request.terminal_source is not None:
        operation: Literal["append", "insert", "replace", "delete"] = "append"
    elif internal_nodes:
        operation = "replace" if request.replacement_nodes else "delete"
    elif request.replacement_nodes:
        operation = "insert"
    else:
        operation = "delete"
        issues.append(
            _issue(
                "no_op_refinement",
                "replacement_nodes",
                "A direct edge with no internal or replacement nodes would not change the graph.",
            )
        )

    declared_side_inputs: dict[str, set[str]] = {}
    for mapping in request.side_input_mappings:
        declared_side_inputs.setdefault(mapping.target_alias, set()).add(
            mapping.target_input
        )

    aliases: set[str] = set()
    resolved_nodes: list[dict[str, Any]] = []
    for index, replacement in enumerate(request.replacement_nodes):
        if replacement.alias in aliases:
            issues.append(
                _issue(
                    "duplicate_alias",
                    f"replacement_nodes[{index}].alias",
                    f"Replacement alias {replacement.alias!r} is used more than once.",
                )
            )
            continue
        aliases.add(replacement.alias)
        resolved, node_issues = _resolve_replacement_node(
            replacement,
            catalog,
            index=index,
            connected_inputs={replacement.chain_input}
            | declared_side_inputs.get(replacement.alias, set()),
            output_required=(
                index < len(request.replacement_nodes) - 1
                or operation != "append"
            ),
        )
        issues.extend(node_issues)
        if resolved is not None:
            resolved_nodes.append(resolved)

    replacement_plan: dict[str, Any] | None = None
    if request.replacement_nodes and len(resolved_nodes) == len(request.replacement_nodes):
        resolved_by_alias = {node["alias"]: node for node in resolved_nodes}
        connection_plan: list[dict[str, Any]] = []
        for index in range(len(resolved_nodes) - 1):
            source_node = resolved_nodes[index]
            target_node = resolved_nodes[index + 1]
            if not _types_compatible(
                source_node["output"]["type"],
                target_node["input"]["type"],
            ):
                issues.append(
                    _issue(
                        "incompatible_replacement_slots",
                        f"replacement_nodes[{index + 1}].chain_input",
                        f"Cannot connect {source_node['output']['type']} to "
                        f"{target_node['input']['type']}.",
                    )
                )
            connection_plan.append(
                {
                    "source_alias": source_node["alias"],
                    "source_output_index": source_node["output"]["index"],
                    "source_output": source_node["output"]["name"],
                    "target_alias": target_node["alias"],
                    "target_input_index": target_node["input"]["index"],
                    "target_input": target_node["input"]["name"],
                    "type": source_node["output"]["type"],
                }
            )

        canonical_side_inputs: list[dict[str, Any]] = []
        occupied_targets = {
            (node["alias"], node["input"]["index"])
            for node in resolved_nodes
        }
        removed_node_keys = {_id_key(node.node_id) for node in internal_nodes}
        for index, mapping in enumerate(request.side_input_mappings):
            mapping_path = f"side_input_mappings[{index}]"
            target_node = resolved_by_alias.get(mapping.target_alias)
            if target_node is None:
                issues.append(
                    _issue(
                        "side_input_target_alias_missing",
                        f"{mapping_path}.target_alias",
                        f"Replacement alias {mapping.target_alias!r} does not exist.",
                    )
                )
                continue
            target_input = target_node["inputs"].get(mapping.target_input)
            if target_input is None:
                issues.append(
                    _issue(
                        "side_input_target_missing",
                        f"{mapping_path}.target_input",
                        f"{target_node['node_type']} has no unique connectable input "
                        f"named {mapping.target_input!r}.",
                    )
                )
                continue
            target_key = (target_node["alias"], target_input["index"])
            if target_key in occupied_targets:
                issues.append(
                    _issue(
                        "side_input_target_occupied",
                        f"{mapping_path}.target_input",
                        "The requested target input is already supplied by the primary "
                        "or sequential chain connection.",
                    )
                )
                continue
            occupied_targets.add(target_key)
            source_output, source_issues = _resolve_existing_output(
                mapping,
                graph_nodes,
                graph_outputs,
                catalog,
                path=mapping_path,
            )
            issues.extend(source_issues)
            if source_output is None:
                continue
            if _id_key(source_output["source_node_id"]) in removed_node_keys:
                issues.append(
                    _issue(
                        "side_input_source_removed",
                        f"{mapping_path}.source_node_id",
                        "A side input cannot originate from an internal path node that "
                        "this refinement removes.",
                    )
                )
                continue
            if not _types_compatible(source_output["type"], target_input["type"]):
                issues.append(
                    _issue(
                        "incompatible_side_input",
                        f"{mapping_path}.target_input",
                        f"Cannot connect {source_output['type']} to "
                        f"{target_input['type']}.",
                    )
                )
            canonical_side_inputs.append(
                {
                    **source_output,
                    "target_alias": target_node["alias"],
                    "target_input_index": target_input["index"],
                    "target_input": target_input["name"],
                    "type": source_output["type"],
                }
            )

        canonical_side_inputs.sort(
            key=lambda item: (
                item["target_alias"],
                item["target_input_index"],
                _id_key(item["source_node_id"]),
                item["source_output_index"],
            )
        )

        canonical_nodes = [
            {
                "alias": node["alias"],
                "node_type": node["node_type"],
                "schema_hash": node["schema_hash"],
                "values": node["values"],
            }
            for node in resolved_nodes
        ]

        primary_input: dict[str, Any] | None = None
        if operation == "append" and request.terminal_source is not None:
            source_output, source_issues = _resolve_existing_output(
                request.terminal_source,
                graph_nodes,
                graph_outputs,
                catalog,
                path="terminal_source",
            )
            issues.extend(source_issues)
            if source_output is not None:
                target_input = resolved_nodes[0]["input"]
                if not _types_compatible(source_output["type"], target_input["type"]):
                    issues.append(
                        _issue(
                            "incompatible_primary_input",
                            "terminal_source",
                            f"Cannot connect {source_output['type']} to "
                            f"{target_input['type']}.",
                        )
                    )
                primary_input = {
                    **source_output,
                    "target_alias": resolved_nodes[0]["alias"],
                    "target_input_index": target_input["index"],
                    "target_input": target_input["name"],
                    "type": source_output["type"],
                }
            final_output = resolved_nodes[-1]["output"]
            if primary_input is not None:
                replacement_plan = {
                    "nodes": canonical_nodes,
                    "connections": connection_plan,
                    "input": None,
                    "primary_input": primary_input,
                    "side_inputs": canonical_side_inputs,
                    "output": (
                        {
                            "source_alias": resolved_nodes[-1]["alias"],
                            "source_output_index": final_output["index"],
                            "source_output": final_output["name"],
                            "type": final_output["type"],
                        }
                        if final_output is not None
                        else None
                    ),
                }
        elif path_edges:
            first_edge = path_edges[0]
            last_edge = path_edges[-1]
            if not _types_compatible(first_edge.type, resolved_nodes[0]["input"]["type"]):
                issues.append(
                    _issue(
                        "incompatible_boundary_input",
                        "replacement_nodes[0].chain_input",
                        f"Cannot connect boundary type {first_edge.type} to "
                        f"{resolved_nodes[0]['input']['type']}.",
                    )
                )
            if not _types_compatible(resolved_nodes[-1]["output"]["type"], last_edge.type):
                issues.append(
                    _issue(
                        "incompatible_boundary_output",
                        f"replacement_nodes[{len(resolved_nodes) - 1}].chain_output",
                        f"Cannot connect {resolved_nodes[-1]['output']['type']} to "
                        f"boundary type {last_edge.type}.",
                    )
                )
            replacement_plan = {
                "nodes": canonical_nodes,
                "connections": connection_plan,
                "input": {
                    "target_alias": resolved_nodes[0]["alias"],
                    "target_input_index": resolved_nodes[0]["input"]["index"],
                    "target_input": resolved_nodes[0]["input"]["name"],
                    "type": first_edge.type,
                },
                "output": {
                    "source_alias": resolved_nodes[-1]["alias"],
                    "source_output_index": resolved_nodes[-1]["output"]["index"],
                    "source_output": resolved_nodes[-1]["output"]["name"],
                    "type": resolved_nodes[-1]["output"]["type"],
                },
            }
            if canonical_side_inputs:
                replacement_plan["side_inputs"] = canonical_side_inputs
    elif operation == "delete" and path_edges:
        if not _types_compatible(path_edges[0].type, path_edges[-1].type):
            issues.append(
                _issue(
                    "incompatible_delete_boundaries",
                    "expected_path.edges",
                    f"Deleting this chain would connect incompatible boundary types "
                    f"{path_edges[0].type} and {path_edges[-1].type}.",
                )
            )

    if replacement_plan is not None:
        def existing_vertex(node_id: NodeId) -> tuple[Any, ...]:
            return ("existing", *_id_key(node_id))

        def alias_vertex(alias: str) -> tuple[Any, ...]:
            return ("alias", alias)

        planned_edges: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = []
        if operation == "append":
            primary = replacement_plan["primary_input"]
            planned_edges.append(
                (
                    existing_vertex(primary["source_node_id"]),
                    alias_vertex(primary["target_alias"]),
                )
            )
        else:
            planned_edges.append(
                (
                    existing_vertex(path_edges[0].source_node_id),
                    alias_vertex(replacement_plan["input"]["target_alias"]),
                )
            )
        planned_edges.extend(
            (
                alias_vertex(connection["source_alias"]),
                alias_vertex(connection["target_alias"]),
            )
            for connection in replacement_plan["connections"]
        )
        planned_edges.extend(
            (
                existing_vertex(mapping["source_node_id"]),
                alias_vertex(mapping["target_alias"]),
            )
            for mapping in replacement_plan.get("side_inputs", [])
        )
        if operation != "append":
            planned_edges.append(
                (
                    alias_vertex(replacement_plan["output"]["source_alias"]),
                    existing_vertex(path_edges[-1].target_node_id),
                )
            )
        if _refinement_has_cycle(
            graph_edges,
            removed_path_edges=path_edges,
            removed_nodes=internal_nodes,
            planned_edges=planned_edges,
        ):
            issues.append(
                _issue(
                    "refinement_cycle",
                    "side_input_mappings",
                    "The requested refinement would create a directed workflow cycle.",
                )
            )
    elif operation == "delete" and path_edges and not issues:
        delete_edge = (
            ("existing", *_id_key(path_edges[0].source_node_id)),
            ("existing", *_id_key(path_edges[-1].target_node_id)),
        )
        if _refinement_has_cycle(
            graph_edges,
            removed_path_edges=path_edges,
            removed_nodes=internal_nodes,
            planned_edges=[delete_edge],
        ):
            issues.append(
                _issue(
                    "refinement_cycle",
                    "expected_path.edges",
                    "Deleting this path would create a directed workflow cycle.",
                )
            )

    canonical_plan = {
        "operation": operation,
        "expected_workflow_identity": request.expected_workflow_identity,
        "expected_graph_hash": request.expected_graph_hash,
        "expected_path": {
            "nodes": [
                {"node_id": node.node_id, "node_type": node.node_type}
                for node in internal_nodes
            ],
            "connections": [edge.model_dump(mode="json") for edge in path_edges],
        },
        "replacement": replacement_plan,
    }
    error_count = len(issues)
    refinement_hash = (
        refinement_plan_hash(canonical_plan, catalog_hash)
        if error_count == 0
        else None
    )
    apply_request = (
        ApplyWorkflowRefinementRequest.model_validate(
            {
                "application_id": request.application_id,
                "expected_catalog_hash": catalog_hash,
                "refinement_hash": refinement_hash,
                "plan": canonical_plan,
            }
        ).model_dump(mode="json")
        if refinement_hash is not None
        else None
    )
    return {
        "valid": error_count == 0,
        "operation": operation,
        "refinement_hash": refinement_hash,
        "refinement_hash_schema": WORKFLOW_REFINEMENT_SCHEMA,
        "graph": {
            "state": "pinned",
            "workflow_identity": request.expected_workflow_identity,
            "workflow_identity_schema": WORKFLOW_IDENTITY_SCHEMA,
            "graph_hash": request.expected_graph_hash,
            "graph_hash_schema": GRAPH_PRECONDITION_HASH_SCHEMA,
            "node_count": len(request.graph.nodes),
            "edge_count": len(request.graph.edges),
        },
        "catalog": {
            "state": "pinned",
            "source": source,
            "catalog_hash": catalog_hash,
            "catalog_hash_schema": CATALOG_HASH_SCHEMA,
            "node_count": len(catalog),
        },
        "plan": canonical_plan,
        "apply_request": apply_request,
        "issues": issues,
        "error_count": error_count,
    }
