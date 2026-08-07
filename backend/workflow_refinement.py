"""Deterministic planning for atomic refinement of one existing workflow chain."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypeAlias

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

NodeId: TypeAlias = StrictInt | StrictStr


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


class NormalizedGraphEdge(BaseModel):
    """One exact existing LiteGraph edge enriched with stable slot names and type."""

    model_config = ConfigDict(extra="forbid")

    source_node_id: NodeId
    source_output: str = Field(..., min_length=1, max_length=256)
    source_output_index: int = Field(..., ge=0)
    target_node_id: NodeId
    target_input: str = Field(..., min_length=1, max_length=256)
    target_input_index: int = Field(..., ge=0)
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
    nodes: list[NormalizedGraphNode] = Field(..., min_length=1, max_length=5_000)
    edges: list[NormalizedGraphEdge] = Field(default_factory=list, max_length=20_000)

    @property
    def schema(self) -> Literal[NORMALIZED_GRAPH_SCHEMA]:
        """Expose the public schema name without shadowing Pydantic's API."""

        return self.schema_


class WorkflowRefinementPath(BaseModel):
    """Ordered exact connections spanning two retained boundary nodes."""

    model_config = ConfigDict(extra="forbid")

    edges: list[NormalizedGraphEdge] = Field(..., min_length=1, max_length=200)


class WorkflowRefinementNode(BaseModel):
    """One replacement-chain node with its exact through-path slots."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, max_length=64)
    node_type: str = Field(..., min_length=1, max_length=256)
    values: dict[str, Any] = Field(default_factory=dict)
    chain_input: str = Field(..., min_length=1, max_length=256)
    chain_output: str = Field(..., min_length=1, max_length=256)
    chain_output_index: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_alias(self) -> WorkflowRefinementNode:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only lowercase "
                "letters, digits, and underscores"
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
    source_output_index: int = Field(..., ge=0)
    source_output: str
    target_alias: str
    target_input_index: int = Field(..., ge=0)
    target_input: str
    type: str


class CanonicalReplacementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alias: str
    target_input_index: int = Field(..., ge=0)
    target_input: str
    type: str


class CanonicalReplacementOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_alias: str
    source_output_index: int = Field(..., ge=0)
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


class CanonicalExpectedPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[NormalizedGraphNode] = Field(default_factory=list, max_length=199)
    connections: list[NormalizedGraphEdge] = Field(
        ...,
        min_length=1,
        max_length=200,
    )

    @model_validator(mode="after")
    def validate_linear_shape(self) -> CanonicalExpectedPath:
        if len(self.connections) != len(self.nodes) + 1:
            raise ValueError("expected path requires one more connection than internal nodes")
        return self


class WorkflowRefinementPlan(BaseModel):
    """Exact canonical payload consumed by the frontend atomic splice engine."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["insert", "replace", "delete"]
    expected_workflow_identity: str = Field(..., min_length=8, max_length=256)
    expected_graph_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_path: CanonicalExpectedPath
    replacement: CanonicalReplacement | None

    @model_validator(mode="after")
    def validate_operation(self) -> WorkflowRefinementPlan:
        internal_count = len(self.expected_path.nodes)
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


def normalize_workflow_graph(workflow: Mapping[str, Any]) -> NormalizedGraphSnapshot:
    """Normalize editable Comfy workflow JSON or fail if any edge is incomplete.

    Both the classic six-item link arrays and mapping-shaped LiteGraph links are
    accepted. Slot names and types always come from the serialized endpoint nodes;
    a caller never gets a misleading ``complete=true`` snapshot with unresolved
    endpoints.
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
    node_slots: dict[tuple[str, str], tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]] = {}
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
        inputs = _slot_sequence(raw_node.get("inputs", []), label=f"node {node_id!r} inputs")
        outputs = _slot_sequence(
            raw_node.get("outputs", []),
            label=f"node {node_id!r} outputs",
        )
        node_slots[key] = (inputs, outputs)
        normalized_nodes.append(NormalizedGraphNode(node_id=node_id, node_type=node_type))

    raw_links = workflow.get("links", [])
    if isinstance(raw_links, list):
        link_items = raw_links
    elif isinstance(raw_links, Mapping):
        link_items = list(raw_links.values())
    else:
        raise ValueError("workflow links must be an array or mapping")

    normalized_edges: list[NormalizedGraphEdge] = []
    seen_edges: set[tuple[Any, ...]] = set()
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
    normalized_edges.sort(key=_edge_exact_key)
    return NormalizedGraphSnapshot(
        schema=NORMALIZED_GRAPH_SCHEMA,
        complete=True,
        nodes=normalized_nodes,
        edges=normalized_edges,
    )


def _canonical_graph_facts(
    graph: NormalizedGraphSnapshot,
) -> tuple[
    dict[tuple[str, str], NormalizedGraphNode],
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
    return nodes, edges, issues


def _resolve_expected_path(
    request: PlanWorkflowRefinementRequest,
    graph_nodes: Mapping[tuple[str, str], NormalizedGraphNode],
    graph_edges: Mapping[tuple[Any, ...], NormalizedGraphEdge],
) -> tuple[list[NormalizedGraphEdge], list[NormalizedGraphNode], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
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
        {node.chain_input},
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
        if name == node.chain_input:
            issues.append(
                _issue(
                    "value_for_connection_input",
                    f"{path}.values.{name}",
                    f"Chain input {name!r} is supplied by a connection and cannot "
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
                if group == "required" and name != node.chain_input:
                    issues.append(
                        _issue(
                            "required_side_input_unsupported",
                            f"{path}.inputs.{name}",
                            f"Required connection input {name!r} is outside the linear replacement chain.",
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

    output_matches = [
        output
        for output in _output_slots(raw_info)
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

    if issues or len(chain_matches) != 1 or len(output_matches) != 1:
        return None, issues
    input_index, input_name, input_spec = chain_matches[0]
    input_type, _ = _input_parts(input_spec)
    output = output_matches[0]
    return {
        "alias": node.alias,
        "node_type": node.node_type,
        "schema_hash": node_schema_hash(node.node_type, raw_info),
        "schema_hash_schema": NODE_SCHEMA_HASH_SCHEMA,
        "values": accepted_values,
        "input": {"index": input_index, "name": input_name, "type": input_type},
        "output": output,
    }, issues


def compile_workflow_refinement(
    request: PlanWorkflowRefinementRequest,
    catalog: Mapping[str, Any],
    *,
    catalog_hash: str,
    source: str,
) -> dict[str, Any]:
    """Compile a graph-hash-pinned linear splice against one immutable catalog."""

    issues: list[dict[str, str]] = []
    graph_nodes, graph_edges, graph_issues = _canonical_graph_facts(request.graph)
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
        )
        issues.extend(node_issues)
        if resolved is not None:
            resolved_nodes.append(resolved)

    if internal_nodes:
        operation: Literal["replace", "delete"] = (
            "replace" if request.replacement_nodes else "delete"
        )
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

    replacement_plan: dict[str, Any] | None = None
    if request.replacement_nodes and len(resolved_nodes) == len(request.replacement_nodes):
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

        if path_edges:
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
                "nodes": [
                    {
                        "alias": node["alias"],
                        "node_type": node["node_type"],
                        "schema_hash": node["schema_hash"],
                        "values": node["values"],
                    }
                    for node in resolved_nodes
                ],
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

    canonical_plan = {
        "operation": operation,
        "expected_workflow_identity": request.expected_workflow_identity,
        "expected_graph_hash": request.expected_graph_hash,
        "expected_path": {
            "nodes": [node.model_dump(mode="json") for node in internal_nodes],
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
