"""Pure, deterministic discovery of workflow branches and split-arm regions.

The catalog is deliberately independent from GraphPatch mutation.  It reads one
serialized workflow, partitions every physical edge into exactly one maximal
non-branching segment, and derives bounded higher-level regions for each arm of
a split.  Nested ComfyUI subgraph definitions are discovered per instance path
and never flattened into their parent graph; unique definitions are directly
writable while reused definitions require explicit shared-edit authority.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

WORKFLOW_BRANCH_CATALOG_SCHEMA = "fl-mcp.workflow-branch-catalog.v1"
WORKFLOW_BRANCH_CATALOG_HASH_SCHEMA = "fl-mcp.workflow-branch-catalog-hash.v1"
WORKFLOW_BRANCH_ID_SCHEMA = "fl-mcp.workflow-branch-id.v1"
WORKFLOW_BRANCH_FINGERPRINT_SCHEMA = "fl-mcp.workflow-branch-fingerprint.v1"
WORKFLOW_BRANCH_EDGE_ID_SCHEMA = "fl-mcp.workflow-branch-edge-id.v1"
WORKFLOW_BRANCH_SCOPE_ID_SCHEMA = "fl-mcp.workflow-branch-scope-id.v1"

NodeId: TypeAlias = StrictInt | StrictStr


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
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
    return (
        "int" if isinstance(value, int) and not isinstance(value, bool) else "str",
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _validate_node_id(value: Any, *, path: str, code: str) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _DiscoveryFailure(code, path, "Node ID is invalid.")
    if isinstance(value, str) and len(value) > 256:
        raise _DiscoveryFailure(
            code,
            path,
            "String node ID exceeds the bounded maximum length of 256 characters.",
        )
    return value


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"severity": "error", "code": code, "path": path, "message": message}


class BranchDiscoveryLimits(BaseModel):
    """Hard bounds for recursive expansion and split-arm derivation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_depth: StrictInt = Field(8, ge=0, le=32)
    max_scopes: StrictInt = Field(256, ge=1, le=2_048)
    max_nodes: StrictInt = Field(5_000, ge=1, le=50_000)
    max_edges: StrictInt = Field(20_000, ge=0, le=200_000)
    max_split_arm_work: StrictInt = Field(2_000_000, ge=1, le=20_000_000)


class BranchIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error"] = "error"
    code: str
    path: str
    message: str


class BranchScopeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_node_id: NodeId
    subgraph_id: str = Field(..., min_length=1, max_length=256)


class BranchScopeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["root", "subgraph_instance"]
    scope_path: list[BranchScopeStep] = Field(default_factory=list, max_length=32)
    subgraph_id: str | None = Field(None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_scope(self) -> BranchScopeRef:
        if self.kind == "root" and (self.scope_path or self.subgraph_id is not None):
            raise ValueError("root scope cannot contain a subgraph instance path")
        if self.kind == "subgraph_instance":
            if not self.scope_path or self.subgraph_id is None:
                raise ValueError("subgraph scope needs a scope path and subgraph_id")
            if self.scope_path[-1].subgraph_id != self.subgraph_id:
                raise ValueError("scope subgraph_id must match the final scope-path step")
        return self


class BranchNodeFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    node_type: str = Field(..., min_length=1, max_length=256)
    boundary_kind: Literal["subgraph_input", "subgraph_output"] | None = None
    selectable: bool = True


class BranchSourceEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    output_index: StrictInt = Field(..., ge=0)
    output: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=256)


class BranchTargetEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    live_socket_index: StrictInt = Field(..., ge=0)
    input: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=256)


class BranchEdgeFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    link_type: str = Field(..., min_length=1, max_length=256)
    source: BranchSourceEndpoint
    target: BranchTargetEndpoint


class WorkflowBranchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    kind: Literal["segment", "split_arm", "isolated"]
    primary_entry_edge_id: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    entry_edges: list[BranchEdgeFact] = Field(default_factory=list)
    exit_edges: list[BranchEdgeFact] = Field(default_factory=list)
    cut_edges: list[BranchEdgeFact] = Field(default_factory=list)
    internal_edges: list[BranchEdgeFact] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    owned_node_ids: list[NodeId] = Field(default_factory=list)
    interior_node_ids: list[NodeId] = Field(default_factory=list)
    boundary_node_ids: list[NodeId] = Field(default_factory=list)
    selectable_node_ids: list[NodeId] = Field(default_factory=list)
    nodes: list[BranchNodeFact] = Field(default_factory=list)
    parent_branch_ids: list[str] = Field(default_factory=list)
    child_branch_ids: list[str] = Field(default_factory=list)
    sibling_branch_ids: list[str] = Field(default_factory=list)
    primitive_segment_ids: list[str] = Field(default_factory=list)
    writable: bool
    reasons: list[str] = Field(default_factory=list)
    label: str


class WorkflowBranchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    scope: BranchScopeRef
    parent_scope_id: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    child_scope_ids: list[str] = Field(default_factory=list)
    definition_name: str | None = Field(None, max_length=512)
    writable: bool
    reasons: list[str] = Field(default_factory=list)
    node_count: StrictInt = Field(..., ge=0)
    edge_count: StrictInt = Field(..., ge=0)
    branches: list[WorkflowBranchRecord] = Field(default_factory=list)


class WorkflowBranchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_count: StrictInt = Field(..., ge=0)
    node_count: StrictInt = Field(..., ge=0)
    edge_count: StrictInt = Field(..., ge=0)
    segment_count: StrictInt = Field(..., ge=0)
    split_arm_count: StrictInt = Field(..., ge=0)
    isolated_count: StrictInt = Field(..., ge=0)


class WorkflowBranchCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal[WORKFLOW_BRANCH_CATALOG_SCHEMA] = Field(
        WORKFLOW_BRANCH_CATALOG_SCHEMA,
        alias="schema",
    )
    branch_id_schema: Literal[WORKFLOW_BRANCH_ID_SCHEMA] = WORKFLOW_BRANCH_ID_SCHEMA
    branch_fingerprint_schema: Literal[WORKFLOW_BRANCH_FINGERPRINT_SCHEMA] = (
        WORKFLOW_BRANCH_FINGERPRINT_SCHEMA
    )
    valid: bool
    workflow_identity: str = Field(..., min_length=1, max_length=512)
    graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    branch_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    summary: WorkflowBranchSummary
    scopes: list[WorkflowBranchScope] = Field(default_factory=list)
    issues: list[BranchIssue] = Field(default_factory=list)

    @property
    def schema(self) -> Literal[WORKFLOW_BRANCH_CATALOG_SCHEMA]:
        return self.schema_


@dataclass(frozen=True)
class _Slot:
    name: str
    type: str


@dataclass(frozen=True)
class _Node:
    node_id: int | str
    node_type: str
    inputs: tuple[_Slot, ...]
    outputs: tuple[_Slot, ...]
    boundary_kind: Literal["subgraph_input", "subgraph_output"] | None = None

    @property
    def selectable(self) -> bool:
        return self.boundary_kind is None


@dataclass(frozen=True)
class _Edge:
    source_key: tuple[str, str]
    source_output_index: int
    source_output: str
    source_type: str
    target_key: tuple[str, str]
    target_input_index: int
    target_input: str
    target_type: str
    link_type: str
    edge_id: str


@dataclass
class _ScopeGraph:
    nodes: dict[tuple[str, str], _Node]
    edges: list[_Edge]
    incoming: dict[tuple[str, str], list[_Edge]]
    outgoing: dict[tuple[str, str], list[_Edge]]
    topological_order: list[tuple[str, str]]
    topological_rank: dict[tuple[str, str], int]


@dataclass
class _BranchDraft:
    kind: Literal["segment", "split_arm", "isolated"]
    member_edges: list[_Edge]
    entry_edges: list[_Edge]
    exit_edges: list[_Edge]
    cut_edges: list[_Edge]
    internal_edges: list[_Edge]
    owned_keys: set[tuple[str, str]]
    interior_keys: set[tuple[str, str]]
    boundary_keys: set[tuple[str, str]]
    selectable_keys: set[tuple[str, str]]
    primary_entry: _Edge | None = None
    split_key: tuple[str, str] | None = None
    path_keys: list[tuple[str, str]] = field(default_factory=list)
    branch_id: str = ""
    fingerprint: str = ""
    parent_ids: list[str] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)
    sibling_ids: list[str] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)


class _DiscoveryFailure(ValueError):
    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass
class _TopologyWorkBudget:
    remaining: int

    def consume(self, amount: int = 1, *, path: str = "branches") -> None:
        if amount < 0:
            raise ValueError("topology work amount cannot be negative")
        self.remaining -= amount
        if self.remaining < 0:
            raise _DiscoveryFailure(
                "split_arm_work_limit_exceeded",
                path,
                "Branch discovery exceeded its bounded topology work budget.",
            )


def _node_sort_key(key: tuple[str, str]) -> tuple[int, str]:
    return (0 if key[0] == "int" else 1, key[1])


def _edge_sort_key(edge: _Edge) -> tuple[Any, ...]:
    return (
        _node_sort_key(edge.source_key),
        edge.source_output_index,
        edge.source_output,
        edge.source_type,
        _node_sort_key(edge.target_key),
        edge.target_input_index,
        edge.target_input,
        edge.target_type,
        edge.link_type,
    )


def _scope_payload(scope: BranchScopeRef) -> list[dict[str, Any]]:
    return [
        {
            "container_node_id": _typed_id(step.container_node_id),
            "subgraph_id": step.subgraph_id,
        }
        for step in scope.scope_path
    ]


def _scope_id(workflow_identity: str, scope: BranchScopeRef) -> str:
    return _canonical_hash(
        {
            "schema": WORKFLOW_BRANCH_SCOPE_ID_SCHEMA,
            "workflow_identity": workflow_identity,
            "scope": {"kind": scope.kind, "instance_path": _scope_payload(scope)},
        }
    )


def _exact_text(
    value: Any,
    *,
    path: str,
    code: str,
    label: str,
    max_length: int,
) -> str:
    if not isinstance(value, str) or not value:
        raise _DiscoveryFailure(code, path, f"{label} must be a non-empty string.")
    if len(value) > max_length:
        raise _DiscoveryFailure(
            code,
            path,
            f"{label} exceeds the bounded maximum length of {max_length} characters.",
        )
    return value


def _slot_sequence(value: Any, *, path: str) -> tuple[_Slot, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        values = list(value.values())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = list(value)
    else:
        raise _DiscoveryFailure("invalid_slot_manifest", path, "Slot manifest must be an array or mapping.")
    result: list[_Slot] = []
    for index, item in enumerate(values):
        if not isinstance(item, Mapping):
            raise _DiscoveryFailure(
                "invalid_slot_manifest",
                f"{path}[{index}]",
                "Slot entry must be an object.",
            )
        slot_path = f"{path}[{index}]"
        name = _exact_text(
            item.get("name"),
            path=f"{slot_path}.name",
            code="unresolved_slot_fact",
            label="Physical slot name",
            max_length=256,
        )
        slot_type = _exact_text(
            item.get("type"),
            path=f"{slot_path}.type",
            code="unresolved_slot_fact",
            label="Physical slot type",
            max_length=256,
        )
        result.append(_Slot(name=name, type=slot_type))
    return tuple(result)


def _node_items(value: Any, *, path: str) -> list[tuple[Any, Mapping[str, Any]]]:
    if isinstance(value, list):
        items = [(None, item) for item in value]
    elif isinstance(value, Mapping):
        items = list(value.items())
    else:
        raise _DiscoveryFailure("invalid_nodes", path, "Nodes must be an array or mapping.")
    result: list[tuple[Any, Mapping[str, Any]]] = []
    for index, (mapping_id, item) in enumerate(items):
        if not isinstance(item, Mapping):
            raise _DiscoveryFailure("invalid_node", f"{path}[{index}]", "Node must be an object.")
        result.append((mapping_id, item))
    return result


def _link_items(value: Any, *, path: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, Mapping):
        return list(value.values())
    raise _DiscoveryFailure("invalid_links", path, "Links must be an array or mapping.")


def _first(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _link_parts(value: Any, *, path: str) -> tuple[int | str, int, int | str, int, str | None]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping)):
        if len(value) < 5:
            raise _DiscoveryFailure("invalid_link", path, "Link array is too short.")
        source_id, source_slot, target_id, target_slot = value[1:5]
        link_type = value[5] if len(value) > 5 else None
    elif isinstance(value, Mapping):
        source_id = _first(value, ("origin_id", "source_id", "source_node_id", "from_node_id"))
        source_slot = _first(value, ("origin_slot", "source_slot", "source_output_index"))
        target_id = _first(value, ("target_id", "target_node_id", "to_node_id"))
        target_slot = _first(value, ("target_slot", "target_input_index"))
        link_type = value.get("type")
    else:
        raise _DiscoveryFailure("invalid_link", path, "Link must be an array or object.")
    source_id = _validate_node_id(
        source_id,
        path=f"{path}.source",
        code="invalid_link_source",
    )
    target_id = _validate_node_id(
        target_id,
        path=f"{path}.target",
        code="invalid_link_target",
    )
    if isinstance(source_slot, bool) or not isinstance(source_slot, int) or source_slot < 0:
        raise _DiscoveryFailure("invalid_link_source_slot", path, "Link source slot is invalid.")
    if isinstance(target_slot, bool) or not isinstance(target_slot, int) or target_slot < 0:
        raise _DiscoveryFailure("invalid_link_target_slot", path, "Link target slot is invalid.")
    if link_type is not None:
        link_type = _exact_text(
            link_type,
            path=f"{path}.type",
            code="invalid_link_type",
            label="Link type",
            max_length=256,
        )
    return source_id, source_slot, target_id, target_slot, link_type


def _definition_ports(value: Any, *, path: str) -> tuple[_Slot, ...]:
    return _slot_sequence(value or [], path=path)


def _build_scope_graph(
    payload: Mapping[str, Any],
    *,
    workflow_identity: str,
    scope: BranchScopeRef,
    definition: bool,
    path: str,
) -> _ScopeGraph:
    nodes: dict[tuple[str, str], _Node] = {}
    for index, (mapping_id, raw) in enumerate(_node_items(payload.get("nodes", []), path=f"{path}.nodes")):
        node_id = raw.get("id", mapping_id)
        node_type = raw.get("type") or raw.get("comfyClass") or raw.get("class_type")
        node_id = _validate_node_id(
            node_id,
            path=f"{path}.nodes[{index}].id",
            code="invalid_node_id",
        )
        node_type = _exact_text(
            node_type,
            path=f"{path}.nodes[{index}].type",
            code="invalid_node_type",
            label="Node type",
            max_length=256,
        )
        key = _id_key(node_id)
        if key in nodes:
            raise _DiscoveryFailure(
                "duplicate_local_node_id",
                f"{path}.nodes[{index}].id",
                f"Scope repeats local node ID {node_id!r}.",
            )
        nodes[key] = _Node(
            node_id=node_id,
            node_type=node_type,
            inputs=_slot_sequence(raw.get("inputs", []), path=f"{path}.nodes[{index}].inputs"),
            outputs=_slot_sequence(raw.get("outputs", []), path=f"{path}.nodes[{index}].outputs"),
        )

    if definition:
        input_node = payload.get("inputNode")
        output_node = payload.get("outputNode")
        input_id = input_node.get("id", -10) if isinstance(input_node, Mapping) else -10
        output_id = output_node.get("id", -20) if isinstance(output_node, Mapping) else -20
        for boundary_id, node_type, inputs, outputs, boundary_kind, boundary_path in (
            (
                input_id,
                "__subgraph_input__",
                (),
                _definition_ports(payload.get("inputs"), path=f"{path}.inputs"),
                "subgraph_input",
                f"{path}.inputNode.id",
            ),
            (
                output_id,
                "__subgraph_output__",
                _definition_ports(payload.get("outputs"), path=f"{path}.outputs"),
                (),
                "subgraph_output",
                f"{path}.outputNode.id",
            ),
        ):
            boundary_id = _validate_node_id(
                boundary_id,
                path=boundary_path,
                code="invalid_virtual_node_id",
            )
            key = _id_key(boundary_id)
            if key in nodes:
                raise _DiscoveryFailure(
                    "duplicate_local_node_id",
                    boundary_path,
                    f"Virtual boundary ID {boundary_id!r} collides with a local node.",
                )
            nodes[key] = _Node(
                node_id=boundary_id,
                node_type=node_type,
                inputs=tuple(inputs),
                outputs=tuple(outputs),
                boundary_kind=boundary_kind,
            )

    edges: list[_Edge] = []
    seen: set[tuple[Any, ...]] = set()
    for index, raw_link in enumerate(_link_items(payload.get("links", []), path=f"{path}.links")):
        source_id, source_index, target_id, target_index, link_type = _link_parts(
            raw_link,
            path=f"{path}.links[{index}]",
        )
        source_key = _id_key(source_id)
        target_key = _id_key(target_id)
        source_node = nodes.get(source_key)
        target_node = nodes.get(target_key)
        if source_node is None or target_node is None:
            raise _DiscoveryFailure(
                "missing_link_endpoint",
                f"{path}.links[{index}]",
                "Link references a node absent from its graph scope.",
            )
        if source_index >= len(source_node.outputs) or target_index >= len(target_node.inputs):
            raise _DiscoveryFailure(
                "missing_link_slot",
                f"{path}.links[{index}]",
                "Link references a slot absent from its endpoint node.",
            )
        output = source_node.outputs[source_index]
        target_input = target_node.inputs[target_index]
        effective_type = link_type or output.type
        exact_key = (
            source_key,
            source_index,
            output.name,
            output.type,
            target_key,
            target_index,
            target_input.name,
            target_input.type,
            effective_type,
        )
        if exact_key in seen:
            raise _DiscoveryFailure(
                "duplicate_physical_edge",
                f"{path}.links[{index}]",
                "Scope contains the same exact physical edge more than once.",
            )
        seen.add(exact_key)
        edge_id = _canonical_hash(
            {
                "schema": WORKFLOW_BRANCH_EDGE_ID_SCHEMA,
                "workflow_identity": workflow_identity,
                "scope": _scope_payload(scope),
                "source": {
                    "node_id": _typed_id(source_id),
                    "output_index": source_index,
                    "output": output.name,
                    "type": output.type,
                },
                "target": {
                    "node_id": _typed_id(target_id),
                    "live_socket_index": target_index,
                    "input": target_input.name,
                    "type": target_input.type,
                },
                "link_type": effective_type,
            }
        )
        edges.append(
            _Edge(
                source_key=source_key,
                source_output_index=source_index,
                source_output=output.name,
                source_type=output.type,
                target_key=target_key,
                target_input_index=target_index,
                target_input=target_input.name,
                target_type=target_input.type,
                link_type=effective_type,
                edge_id=edge_id,
            )
        )

    edges.sort(key=_edge_sort_key)
    incoming: dict[tuple[str, str], list[_Edge]] = {key: [] for key in nodes}
    outgoing: dict[tuple[str, str], list[_Edge]] = {key: [] for key in nodes}
    for edge in edges:
        incoming[edge.target_key].append(edge)
        outgoing[edge.source_key].append(edge)
    for collection in (*incoming.values(), *outgoing.values()):
        collection.sort(key=_edge_sort_key)

    indegree = {key: len(incoming[key]) for key in nodes}
    queue = [(_node_sort_key(key), key) for key, degree in indegree.items() if degree == 0]
    heapq.heapify(queue)
    order: list[tuple[str, str]] = []
    while queue:
        _, key = heapq.heappop(queue)
        order.append(key)
        for edge in outgoing[key]:
            indegree[edge.target_key] -= 1
            if indegree[edge.target_key] == 0:
                heapq.heappush(queue, (_node_sort_key(edge.target_key), edge.target_key))
    if len(order) != len(nodes):
        raise _DiscoveryFailure(
            "graph_cycle",
            f"{path}.links",
            "Branch discovery requires an acyclic physical graph in every scope.",
        )
    return _ScopeGraph(
        nodes=nodes,
        edges=edges,
        incoming=incoming,
        outgoing=outgoing,
        topological_order=order,
        topological_rank={key: index for index, key in enumerate(order)},
    )


def _public_edge(edge: _Edge, graph: _ScopeGraph) -> BranchEdgeFact:
    source = graph.nodes[edge.source_key]
    target = graph.nodes[edge.target_key]
    return BranchEdgeFact(
        edge_id=edge.edge_id,
        link_type=edge.link_type,
        source=BranchSourceEndpoint(
            node_id=source.node_id,
            output_index=edge.source_output_index,
            output=edge.source_output,
            type=edge.source_type,
        ),
        target=BranchTargetEndpoint(
            node_id=target.node_id,
            live_socket_index=edge.target_input_index,
            input=edge.target_input,
            type=edge.target_type,
        ),
    )


def _unique_edges(edges: Sequence[_Edge]) -> list[_Edge]:
    return sorted({edge.edge_id: edge for edge in edges}.values(), key=_edge_sort_key)


def _node_fact_payload(key: tuple[str, str], graph: _ScopeGraph, roles: Sequence[str]) -> dict[str, Any]:
    node = graph.nodes[key]
    return {
        "node_id": _typed_id(node.node_id),
        "node_type": node.node_type,
        "boundary_kind": node.boundary_kind,
        "roles": sorted(set(roles)),
    }


def _edge_identity_payload(edge: _Edge, graph: _ScopeGraph) -> dict[str, Any]:
    source = graph.nodes[edge.source_key]
    target = graph.nodes[edge.target_key]
    return {
        "source": {
            "node_id": _typed_id(source.node_id),
            "node_type": source.node_type,
            "output_index": edge.source_output_index,
            "output": edge.source_output,
            "type": edge.source_type,
        },
        "target": {
            "node_id": _typed_id(target.node_id),
            "node_type": target.node_type,
            "live_socket_index": edge.target_input_index,
            "input": edge.target_input,
            "type": edge.target_type,
        },
        "link_type": edge.link_type,
    }


def _branch_fingerprint(
    draft: _BranchDraft,
    graph: _ScopeGraph,
    *,
    work: _TopologyWorkBudget,
) -> str:
    member_keys = {
        *draft.owned_keys,
        *draft.boundary_keys,
        *(edge.source_key for edge in draft.member_edges),
        *(edge.target_key for edge in draft.member_edges),
    }
    if draft.kind == "isolated":
        member_keys.update(draft.owned_keys)
    role_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key in draft.owned_keys:
        role_by_key[key].add("owned")
    for key in draft.interior_keys:
        role_by_key[key].add("interior")
    for key in draft.boundary_keys:
        role_by_key[key].add("boundary")
    if draft.split_key is not None:
        role_by_key[draft.split_key].add("split")
    member_edges = _unique_edges(draft.member_edges)
    work.consume(len(member_keys) + len(member_edges), path="branches.fingerprint")
    initial: dict[tuple[str, str], str] = {}
    local_incoming: dict[tuple[str, str], list[_Edge]] = {
        key: [] for key in member_keys
    }
    local_outgoing: dict[tuple[str, str], list[_Edge]] = {
        key: [] for key in member_keys
    }
    for key in member_keys:
        initial[key] = _canonical_hash(
            {
                "node_type": graph.nodes[key].node_type,
                "boundary_kind": graph.nodes[key].boundary_kind,
                "roles": sorted(role_by_key[key]),
            }
        )
    for edge in member_edges:
        local_incoming[edge.target_key].append(edge)
        local_outgoing[edge.source_key].append(edge)
    ordered_keys = sorted(member_keys, key=graph.topological_rank.__getitem__)

    # A forward and reverse DAG pass gives every node an ID-independent view of
    # its exact typed ancestry and descendants.  Unlike iterative whole-branch
    # refinement, this is linear in the materialized branch facts and bounded by
    # the shared topology-work budget.
    forward: dict[tuple[str, str], str] = {}
    for key in ordered_keys:
        incoming = local_incoming[key]
        work.consume(1 + len(incoming), path="branches.fingerprint.forward")
        forward[key] = _canonical_hash(
            {
                "base": initial[key],
                "incoming": sorted(
                    (
                        forward[edge.source_key],
                        edge.source_output_index,
                        edge.source_output,
                        edge.source_type,
                        edge.target_input_index,
                        edge.target_input,
                        edge.target_type,
                        edge.link_type,
                    )
                    for edge in incoming
                ),
            }
        )
    backward: dict[tuple[str, str], str] = {}
    for key in reversed(ordered_keys):
        outgoing = local_outgoing[key]
        work.consume(1 + len(outgoing), path="branches.fingerprint.backward")
        backward[key] = _canonical_hash(
            {
                "base": initial[key],
                "outgoing": sorted(
                    (
                        edge.source_output_index,
                        edge.source_output,
                        edge.source_type,
                        edge.target_input_index,
                        edge.target_input,
                        edge.target_type,
                        edge.link_type,
                        backward[edge.target_key],
                    )
                    for edge in outgoing
                ),
            }
        )
    labels = {
        key: _canonical_hash(
            {
                "base": initial[key],
                "forward": forward[key],
                "backward": backward[key],
            }
        )
        for key in member_keys
    }
    work.consume(len(member_keys) + len(member_edges), path="branches.fingerprint.final")
    topology_edges = sorted(
        (
            labels[edge.source_key],
            edge.source_output_index,
            edge.source_output,
            edge.source_type,
            edge.target_input_index,
            edge.target_input,
            edge.target_type,
            edge.link_type,
            labels[edge.target_key],
        )
        for edge in member_edges
    )
    return _canonical_hash(
        {
            "schema": WORKFLOW_BRANCH_FINGERPRINT_SCHEMA,
            "kind": draft.kind,
            "nodes": sorted(labels.values()),
            "edges": topology_edges,
        }
    )


def _assign_branch_identity(
    draft: _BranchDraft,
    *,
    graph: _ScopeGraph,
    workflow_identity: str,
    scope: BranchScopeRef,
    work: _TopologyWorkBudget,
) -> None:
    role_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key in draft.owned_keys:
        role_by_key[key].add("owned")
    for key in draft.interior_keys:
        role_by_key[key].add("interior")
    for key in draft.boundary_keys:
        role_by_key[key].add("boundary")
    if draft.split_key is not None:
        role_by_key[draft.split_key].add("split")
    member_keys = set(role_by_key)
    member_keys.update(edge.source_key for edge in draft.member_edges)
    member_keys.update(edge.target_key for edge in draft.member_edges)
    work.consume(len(member_keys) + len(draft.member_edges), path="branches.identity")
    draft.fingerprint = _branch_fingerprint(draft, graph, work=work)
    draft.branch_id = _canonical_hash(
        {
            "schema": WORKFLOW_BRANCH_ID_SCHEMA,
            "workflow_identity": workflow_identity,
            "scope": {"kind": scope.kind, "instance_path": _scope_payload(scope)},
            "kind": draft.kind,
            "nodes": [
                _node_fact_payload(key, graph, role_by_key[key])
                for key in sorted(member_keys, key=_node_sort_key)
            ],
            "edges": [
                _edge_identity_payload(edge, graph)
                for edge in _unique_edges(draft.member_edges)
            ],
        }
    )


def _primitive_segments(graph: _ScopeGraph) -> list[_BranchDraft]:
    boundaries = {
        key
        for key, node in graph.nodes.items()
        if node.boundary_kind is not None
        or len(graph.incoming[key]) != 1
        or len(graph.outgoing[key]) != 1
    }
    drafts: list[_BranchDraft] = []
    covered: set[str] = set()
    for start in sorted(boundaries, key=_node_sort_key):
        for first in graph.outgoing[start]:
            path_edges = [first]
            path_keys = [start, first.target_key]
            current = first.target_key
            while current not in boundaries:
                next_edge = graph.outgoing[current][0]
                path_edges.append(next_edge)
                path_keys.append(next_edge.target_key)
                current = next_edge.target_key
            for edge in path_edges:
                if edge.edge_id in covered:
                    raise _DiscoveryFailure(
                        "segment_edge_overlap",
                        "branches",
                        "A physical edge was assigned to more than one primitive segment.",
                    )
                covered.add(edge.edge_id)
            interior = set(path_keys[1:-1])
            entry = [path_edges[0]]
            exit_edges = [path_edges[-1]]
            internal = [
                edge
                for edge in path_edges
                if edge.source_key in interior and edge.target_key in interior
            ]
            drafts.append(
                _BranchDraft(
                    kind="segment",
                    member_edges=path_edges,
                    entry_edges=entry,
                    exit_edges=exit_edges,
                    cut_edges=_unique_edges([*entry, *exit_edges]),
                    internal_edges=internal,
                    owned_keys=interior,
                    interior_keys=interior,
                    boundary_keys={path_keys[0], path_keys[-1]},
                    selectable_keys={
                        key for key in path_keys if graph.nodes[key].selectable
                    },
                    primary_entry=first,
                    path_keys=path_keys,
                )
            )
    if covered != {edge.edge_id for edge in graph.edges}:
        raise _DiscoveryFailure(
            "segment_partition_incomplete",
            "branches",
            "Primitive segments do not cover every physical edge exactly once.",
        )
    for key in sorted(graph.nodes, key=_node_sort_key):
        if graph.incoming[key] or graph.outgoing[key]:
            continue
        drafts.append(
            _BranchDraft(
                kind="isolated",
                member_edges=[],
                entry_edges=[],
                exit_edges=[],
                cut_edges=[],
                internal_edges=[],
                owned_keys={key},
                interior_keys={key},
                boundary_keys=set(),
                selectable_keys={key} if graph.nodes[key].selectable else set(),
                primary_entry=None,
                path_keys=[key],
            )
        )
    return drafts


def _split_arms(
    graph: _ScopeGraph,
    *,
    work: _TopologyWorkBudget,
) -> list[_BranchDraft]:
    drafts: list[_BranchDraft] = []
    for split_key in graph.topological_order:
        outgoing = graph.outgoing[split_key]
        if len(outgoing) <= 1:
            continue
        reach_sets: list[set[tuple[str, str]]] = []
        for entry in outgoing:
            reached: set[tuple[str, str]] = set()
            queue: deque[tuple[str, str]] = deque([entry.target_key])
            while queue:
                key = queue.popleft()
                if key in reached:
                    continue
                reached.add(key)
                work.consume(path="branches.split_arms.reachability")
                for edge in graph.outgoing[key]:
                    work.consume(path="branches.split_arms.reachability")
                    queue.append(edge.target_key)
            reach_sets.append(reached)
        ownership: Counter[tuple[str, str]] = Counter()
        for reached in reach_sets:
            work.consume(len(reached), path="branches.split_arms.ownership")
            ownership.update(reached)
        for entry, reached in zip(outgoing, reach_sets, strict=True):
            owned = {key for key in reached if ownership[key] == 1}
            work.consume(len(reached), path="branches.split_arms.exclusive")
            # When multiple physical split edges immediately enter the same
            # shared successor, there is no exclusive arm region to select or
            # mutate.  Primitive segments still own those physical edges.
            if not owned:
                continue
            internal: list[_Edge] = []
            entries: list[_Edge] = []
            exits: list[_Edge] = []
            for key in owned:
                incoming = graph.incoming[key]
                outgoing_edges = graph.outgoing[key]
                work.consume(
                    len(incoming) + len(outgoing_edges),
                    path="branches.split_arms.boundaries",
                )
                for candidate in incoming:
                    if candidate.source_key in owned:
                        internal.append(candidate)
                    else:
                        entries.append(candidate)
                exits.extend(
                    candidate
                    for candidate in outgoing_edges
                    if candidate.target_key not in owned
                )
            if entry.edge_id not in {edge.edge_id for edge in entries}:
                entries.append(entry)
            entries = _unique_edges(entries)
            exits = _unique_edges(exits)
            boundary = {
                *(edge.source_key for edge in entries),
                *(edge.target_key for edge in exits),
            }
            interior = {
                key
                for key in owned
                if graph.nodes[key].boundary_kind is None
                and len(graph.incoming[key]) == 1
                and len(graph.outgoing[key]) == 1
            }
            member_edges = _unique_edges([*entries, *internal, *exits])
            drafts.append(
                _BranchDraft(
                    kind="split_arm",
                    member_edges=member_edges,
                    entry_edges=entries,
                    exit_edges=exits,
                    cut_edges=_unique_edges([*entries, *exits]),
                    internal_edges=_unique_edges(internal),
                    owned_keys=owned,
                    interior_keys=interior,
                    boundary_keys=boundary,
                    selectable_keys={
                        key
                        for key in {split_key, *owned, *boundary}
                        if graph.nodes[key].selectable
                    },
                    primary_entry=entry,
                    split_key=split_key,
                )
            )
    return drafts


def _assign_relations(
    drafts: list[_BranchDraft],
    *,
    work: _TopologyWorkBudget,
) -> None:
    arms = [draft for draft in drafts if draft.kind == "split_arm"]
    segments = [draft for draft in drafts if draft.kind in {"segment", "isolated"}]
    by_id = {draft.branch_id: draft for draft in drafts}
    arm_by_id = {arm.branch_id: arm for arm in arms}
    arm_edge_ids = {
        arm.branch_id: {edge.edge_id for edge in arm.member_edges} for arm in arms
    }
    segment_edge_ids = {
        segment.branch_id: {edge.edge_id for edge in segment.member_edges}
        for segment in segments
    }
    work.consume(
        len(drafts)
        + sum(len(edge_ids) for edge_ids in arm_edge_ids.values())
        + sum(len(edge_ids) for edge_ids in segment_edge_ids.values()),
        path="branches.relations.indexes",
    )

    arms_by_owned_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    for arm in arms:
        work.consume(len(arm.owned_keys), path="branches.relations.arm_ownership")
        for key in arm.owned_keys:
            arms_by_owned_key[key].append(arm.branch_id)
    for arm in arms:
        assert arm.split_key is not None
        candidate_ids = [
            branch_id
            for branch_id in arms_by_owned_key.get(arm.split_key, [])
            if branch_id != arm.branch_id
        ]
        work.consume(len(candidate_ids), path="branches.relations.arm_parents")
        candidates = [arm_by_id[branch_id] for branch_id in candidate_ids]
        if candidates:
            parent = min(candidates, key=lambda item: (len(item.owned_keys), item.branch_id))
            arm.parent_ids = [parent.branch_id]

    arms_by_edge: dict[str, set[str]] = defaultdict(set)
    for branch_id, edge_ids in arm_edge_ids.items():
        work.consume(len(edge_ids), path="branches.relations.edge_index")
        for edge_id in edge_ids:
            arms_by_edge[edge_id].add(branch_id)
    for segment in segments:
        segment_edges = segment_edge_ids[segment.branch_id]
        if not segment_edges:
            continue
        ordered_edges = sorted(segment_edges)
        candidate_ids = set(arms_by_edge.get(ordered_edges[0], set()))
        work.consume(len(candidate_ids), path="branches.relations.segment_parents")
        for edge_id in ordered_edges[1:]:
            work.consume(len(candidate_ids), path="branches.relations.segment_parents")
            candidate_ids.intersection_update(arms_by_edge.get(edge_id, set()))
        work.consume(len(candidate_ids), path="branches.relations.segment_parents")
        candidates = [arm_by_id[branch_id] for branch_id in candidate_ids]
        if candidates:
            parent = min(candidates, key=lambda item: (len(item.member_edges), item.branch_id))
            segment.parent_ids = [parent.branch_id]

    for draft in drafts:
        for parent_id in draft.parent_ids:
            work.consume(path="branches.relations.children")
            by_id[parent_id].child_ids.append(draft.branch_id)
    arms_by_split: dict[tuple[str, str], list[_BranchDraft]] = defaultdict(list)
    for arm in arms:
        assert arm.split_key is not None
        arms_by_split[arm.split_key].append(arm)
    for siblings in arms_by_split.values():
        work.consume(
            len(siblings) * max(1, len(siblings) - 1),
            path="branches.relations.arm_siblings",
        )
        ids = sorted(item.branch_id for item in siblings)
        for item in siblings:
            item.sibling_ids = [branch_id for branch_id in ids if branch_id != item.branch_id]

    non_arm_sibling_groups: dict[tuple[str, tuple[str, ...]], list[_BranchDraft]] = defaultdict(list)
    for draft in segments:
        if draft.parent_ids:
            non_arm_sibling_groups[(draft.kind, tuple(draft.parent_ids))].append(draft)
    for siblings in non_arm_sibling_groups.values():
        work.consume(
            len(siblings) * max(1, len(siblings) - 1),
            path="branches.relations.primitive_siblings",
        )
        ids = sorted(item.branch_id for item in siblings)
        for item in siblings:
            item.sibling_ids = [branch_id for branch_id in ids if branch_id != item.branch_id]

    for draft in drafts:
        draft.child_ids = sorted(set(draft.child_ids))
        draft.parent_ids = sorted(set(draft.parent_ids))
        draft.sibling_ids = sorted(set(draft.sibling_ids))

    primitive = [draft for draft in segments if draft.kind == "segment"]
    segments_by_edge: dict[str, set[str]] = defaultdict(set)
    for segment in primitive:
        edge_ids = segment_edge_ids[segment.branch_id]
        work.consume(len(edge_ids), path="branches.relations.segment_index")
        for edge_id in edge_ids:
            segments_by_edge[edge_id].add(segment.branch_id)
    for arm in arms:
        member_ids = arm_edge_ids[arm.branch_id]
        candidate_segment_ids: set[str] = set()
        for edge_id in member_ids:
            work.consume(path="branches.relations.primitive_membership")
            candidate_segment_ids.update(segments_by_edge.get(edge_id, set()))
        work.consume(
            len(candidate_segment_ids),
            path="branches.relations.primitive_membership",
        )
        arm.segment_ids = sorted(
            segment_id
            for segment_id in candidate_segment_ids
            if segment_edge_ids[segment_id].issubset(member_ids)
        )


def _node_ids(keys: set[tuple[str, str]], graph: _ScopeGraph) -> list[int | str]:
    return [graph.nodes[key].node_id for key in sorted(keys, key=_node_sort_key)]


def _public_record(
    draft: _BranchDraft,
    *,
    graph: _ScopeGraph,
    scope_id: str,
    writable: bool,
    reasons: list[str],
    work: _TopologyWorkBudget,
) -> WorkflowBranchRecord:
    member_keys = {
        *draft.owned_keys,
        *draft.boundary_keys,
        *(edge.source_key for edge in draft.member_edges),
        *(edge.target_key for edge in draft.member_edges),
    }
    work.consume(
        len(member_keys)
        + len(draft.member_edges)
        + len(draft.entry_edges)
        + len(draft.exit_edges),
        path="branches.public_record",
    )
    nodes = [
        BranchNodeFact(
            node_id=graph.nodes[key].node_id,
            node_type=graph.nodes[key].node_type,
            boundary_kind=graph.nodes[key].boundary_kind,
            selectable=graph.nodes[key].selectable,
        )
        for key in sorted(member_keys, key=_node_sort_key)
    ]
    if draft.kind == "isolated":
        only = graph.nodes[next(iter(draft.owned_keys))]
        label = only.node_type
    elif draft.kind == "segment":
        label = f"{graph.nodes[draft.path_keys[0]].node_type} → {graph.nodes[draft.path_keys[-1]].node_type}"
    else:
        assert draft.split_key is not None
        assert draft.primary_entry is not None
        target = draft.primary_entry.target_key
        label = f"{graph.nodes[draft.split_key].node_type} arm via {graph.nodes[target].node_type}"
    return WorkflowBranchRecord(
        branch_id=draft.branch_id,
        branch_fingerprint=draft.fingerprint,
        scope_id=scope_id,
        kind=draft.kind,
        primary_entry_edge_id=(
            draft.primary_entry.edge_id if draft.primary_entry is not None else None
        ),
        entry_edges=[_public_edge(edge, graph) for edge in _unique_edges(draft.entry_edges)],
        exit_edges=[_public_edge(edge, graph) for edge in _unique_edges(draft.exit_edges)],
        cut_edges=[_public_edge(edge, graph) for edge in _unique_edges(draft.cut_edges)],
        internal_edges=[_public_edge(edge, graph) for edge in _unique_edges(draft.internal_edges)],
        edge_ids=[edge.edge_id for edge in _unique_edges(draft.member_edges)],
        owned_node_ids=_node_ids(draft.owned_keys, graph),
        interior_node_ids=_node_ids(draft.interior_keys, graph),
        boundary_node_ids=_node_ids(draft.boundary_keys, graph),
        selectable_node_ids=_node_ids(draft.selectable_keys, graph),
        nodes=nodes,
        parent_branch_ids=draft.parent_ids,
        child_branch_ids=draft.child_ids,
        sibling_branch_ids=draft.sibling_ids,
        primitive_segment_ids=draft.segment_ids,
        writable=writable,
        reasons=reasons,
        label=label,
    )


def _definition_map(workflow: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    definitions = workflow.get("definitions")
    if definitions is None:
        return {}
    if not isinstance(definitions, Mapping):
        raise _DiscoveryFailure("invalid_definitions", "workflow.definitions", "Definitions must be an object.")
    raw = definitions.get("subgraphs", [])
    if isinstance(raw, list):
        items = [(None, item) for item in raw]
    elif isinstance(raw, Mapping):
        items = list(raw.items())
    else:
        raise _DiscoveryFailure(
            "invalid_subgraph_definitions",
            "workflow.definitions.subgraphs",
            "Subgraph definitions must be an array or mapping.",
        )
    result: dict[str, Mapping[str, Any]] = {}
    for index, (mapping_id, item) in enumerate(items):
        if not isinstance(item, Mapping):
            raise _DiscoveryFailure(
                "invalid_subgraph_definition",
                f"workflow.definitions.subgraphs[{index}]",
                "Subgraph definition must be an object.",
            )
        definition_path = f"workflow.definitions.subgraphs[{index}]"
        definition_id = _exact_text(
            item.get("id", mapping_id),
            path=f"{definition_path}.id",
            code="invalid_subgraph_definition_id",
            label="Subgraph definition ID",
            max_length=256,
        )
        definition_name = item.get("name")
        if definition_name is not None:
            _exact_text(
                definition_name,
                path=f"{definition_path}.name",
                code="invalid_subgraph_definition_name",
                label="Subgraph definition name",
                max_length=512,
            )
        if definition_id in result:
            raise _DiscoveryFailure(
                "duplicate_subgraph_definition_id",
                f"workflow.definitions.subgraphs[{index}].id",
                f"Definition ID {definition_id!r} is repeated.",
            )
        result[definition_id] = item
    return result


def discover_workflow_branches(
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    limits: BranchDiscoveryLimits | Mapping[str, Any] | None = None,
) -> WorkflowBranchCatalog:
    """Return one canonical, read-only branch catalog for a serialized workflow."""

    if not isinstance(workflow, Mapping):
        raise TypeError("workflow must be a serialized workflow mapping")
    if (
        not isinstance(workflow_identity, str)
        or not workflow_identity
        or len(workflow_identity) > 512
    ):
        raise ValueError("workflow_identity must be a non-empty string of at most 512 characters")
    if not isinstance(graph_hash, str) or len(graph_hash) != 64 or any(
        character not in "0123456789abcdef" for character in graph_hash
    ):
        raise ValueError("graph_hash must be a lowercase SHA-256 hex digest")
    active_limits = (
        limits
        if isinstance(limits, BranchDiscoveryLimits)
        else BranchDiscoveryLimits.model_validate(limits or {})
    )
    issues: list[dict[str, str]] = []
    scopes: list[WorkflowBranchScope] = []
    total_nodes = 0
    total_edges = 0
    work = _TopologyWorkBudget(active_limits.max_split_arm_work)
    try:
        definitions = _definition_map(workflow)
    except _DiscoveryFailure as exc:
        definitions = {}
        issues.append(_issue(exc.code, exc.path, exc.message))

    root_scope = BranchScopeRef(kind="root")
    queue: deque[
        tuple[
            BranchScopeRef,
            Mapping[str, Any],
            str | None,
            tuple[str, ...],
            str | None,
        ]
    ] = deque([(root_scope, workflow, None, (), None)])
    scope_children: dict[str, list[str]] = defaultdict(list)
    while queue:
        scope, payload, parent_scope_id, ancestry, definition_name = queue.popleft()
        scope_identifier = _scope_id(workflow_identity, scope)
        if len(scopes) >= active_limits.max_scopes:
            issues.append(
                _issue(
                    "scope_limit_exceeded",
                    "workflow.definitions.subgraphs",
                    f"Recursive discovery exceeds {active_limits.max_scopes} graph scopes.",
                )
            )
            break
        path = "workflow" if scope.kind == "root" else f"scope[{scope_identifier}]"
        try:
            graph = _build_scope_graph(
                payload,
                workflow_identity=workflow_identity,
                scope=scope,
                definition=scope.kind == "subgraph_instance",
                path=path,
            )
            if total_nodes + len(graph.nodes) > active_limits.max_nodes:
                raise _DiscoveryFailure(
                    "node_limit_exceeded",
                    path,
                    f"Expanded scopes exceed {active_limits.max_nodes} nodes.",
                )
            if total_edges + len(graph.edges) > active_limits.max_edges:
                raise _DiscoveryFailure(
                    "edge_limit_exceeded",
                    path,
                    f"Expanded scopes exceed {active_limits.max_edges} edges.",
                )
            total_nodes += len(graph.nodes)
            total_edges += len(graph.edges)
            drafts = _primitive_segments(graph)
            drafts.extend(_split_arms(graph, work=work))
            for draft in drafts:
                _assign_branch_identity(
                    draft,
                    graph=graph,
                    workflow_identity=workflow_identity,
                    scope=scope,
                    work=work,
                )
            _assign_relations(drafts, work=work)
            nested = scope.kind == "subgraph_instance"
            # Nested write authority is finalized only after every reachable
            # instance has been discovered.  Keep the records structurally
            # conservative here, then classify unique vs shared definitions in
            # one deterministic post-pass below.
            reasons = ["nested_scope_policy_pending"] if nested else []
            records = [
                _public_record(
                    draft,
                    graph=graph,
                    scope_id=scope_identifier,
                    writable=not nested,
                    reasons=reasons,
                    work=work,
                )
                for draft in drafts
            ]
            records.sort(key=lambda item: ({"segment": 0, "split_arm": 1, "isolated": 2}[item.kind], item.branch_id))
            scopes.append(
                WorkflowBranchScope(
                    scope_id=scope_identifier,
                    scope=scope,
                    parent_scope_id=parent_scope_id,
                    definition_name=definition_name,
                    writable=not nested,
                    reasons=reasons,
                    node_count=len(graph.nodes),
                    edge_count=len(graph.edges),
                    branches=records,
                )
            )
            if parent_scope_id is not None:
                scope_children[parent_scope_id].append(scope_identifier)

            if len(scope.scope_path) >= active_limits.max_depth:
                if any(node.node_type in definitions for node in graph.nodes.values()):
                    issues.append(
                        _issue(
                            "subgraph_depth_limit_exceeded",
                            path,
                            f"Nested subgraph discovery exceeds depth {active_limits.max_depth}.",
                        )
                    )
                continue
            for key in sorted(graph.nodes, key=_node_sort_key):
                node = graph.nodes[key]
                definition = definitions.get(node.node_type)
                if definition is None:
                    continue
                if node.node_type in ancestry:
                    issues.append(
                        _issue(
                            "recursive_subgraph_definition",
                            f"{path}.nodes[{node.node_id!r}]",
                            f"Definition {node.node_type!r} recursively references its ancestry.",
                        )
                    )
                    continue
                child_scope = BranchScopeRef(
                    kind="subgraph_instance",
                    scope_path=[
                        *scope.scope_path,
                        BranchScopeStep(
                            container_node_id=node.node_id,
                            subgraph_id=node.node_type,
                        ),
                    ],
                    subgraph_id=node.node_type,
                )
                raw_name = definition.get("name")
                queue.append(
                    (
                        child_scope,
                        definition,
                        scope_identifier,
                        (*ancestry, node.node_type),
                        raw_name if isinstance(raw_name, str) else None,
                    )
                )
        except _DiscoveryFailure as exc:
            issues.append(_issue(exc.code, exc.path, exc.message))
        except ValidationError:
            issues.append(
                _issue(
                    "invalid_branch_catalog_fact",
                    path,
                    "Workflow facts could not be represented by the bounded branch-catalog contract.",
                )
            )

    definition_instance_counts = Counter(
        scope.scope.subgraph_id
        for scope in scopes
        if scope.scope.kind == "subgraph_instance"
    )
    classified_scopes: list[WorkflowBranchScope] = []
    for scope in scopes:
        if scope.scope.kind != "subgraph_instance":
            classified_scopes.append(scope)
            continue
        reused = definition_instance_counts[scope.scope.subgraph_id] > 1
        reasons = ["shared_definition_acknowledgement_required"] if reused else []
        classified_scopes.append(
            scope.model_copy(
                update={
                    "writable": not reused,
                    "reasons": reasons,
                    "branches": [
                        branch.model_copy(
                            update={"writable": not reused, "reasons": reasons}
                        )
                        for branch in scope.branches
                    ],
                }
            )
        )
    scopes = classified_scopes

    scopes = [
        scope.model_copy(
            update={
                "child_scope_ids": sorted(set(scope_children.get(scope.scope_id, [])))
            }
        )
        for scope in scopes
    ]
    if issues:
        invalid_reason = "branch_catalog_invalid"
        scopes = [
            scope.model_copy(
                update={
                    "writable": False,
                    "reasons": sorted({*scope.reasons, invalid_reason}),
                    "branches": [
                        branch.model_copy(
                            update={
                                "writable": False,
                                "reasons": sorted({*branch.reasons, invalid_reason}),
                            }
                        )
                        for branch in scope.branches
                    ],
                }
            )
            for scope in scopes
        ]
    scopes.sort(key=lambda item: (len(item.scope.scope_path), item.scope_id))
    branch_records = [branch for scope in scopes for branch in scope.branches]
    summary = WorkflowBranchSummary(
        scope_count=len(scopes),
        node_count=sum(scope.node_count for scope in scopes),
        edge_count=sum(scope.edge_count for scope in scopes),
        segment_count=sum(branch.kind == "segment" for branch in branch_records),
        split_arm_count=sum(branch.kind == "split_arm" for branch in branch_records),
        isolated_count=sum(branch.kind == "isolated" for branch in branch_records),
    )
    branch_catalog_hash = _canonical_hash(
        {
            "schema": WORKFLOW_BRANCH_CATALOG_HASH_SCHEMA,
            "workflow_identity": workflow_identity,
            "scopes": [
                {
                    "scope_id": scope.scope_id,
                    "branch_ids": sorted(branch.branch_id for branch in scope.branches),
                }
                for scope in scopes
            ],
        }
    )
    validated_issues = [BranchIssue.model_validate(item) for item in issues]
    return WorkflowBranchCatalog(
        schema=WORKFLOW_BRANCH_CATALOG_SCHEMA,
        valid=not validated_issues,
        workflow_identity=workflow_identity,
        graph_hash=graph_hash,
        branch_catalog_hash=branch_catalog_hash,
        summary=summary,
        scopes=scopes,
        issues=validated_issues,
    )


BRANCH_DISCOVERY_LIMITS = BranchDiscoveryLimits()


__all__ = [
    "BRANCH_DISCOVERY_LIMITS",
    "BranchDiscoveryLimits",
    "BranchEdgeFact",
    "BranchIssue",
    "BranchNodeFact",
    "BranchScopeRef",
    "BranchScopeStep",
    "BranchSourceEndpoint",
    "BranchTargetEndpoint",
    "WORKFLOW_BRANCH_CATALOG_HASH_SCHEMA",
    "WORKFLOW_BRANCH_CATALOG_SCHEMA",
    "WORKFLOW_BRANCH_FINGERPRINT_SCHEMA",
    "WORKFLOW_BRANCH_ID_SCHEMA",
    "WorkflowBranchCatalog",
    "WorkflowBranchRecord",
    "WorkflowBranchScope",
    "WorkflowBranchSummary",
    "discover_workflow_branches",
]
