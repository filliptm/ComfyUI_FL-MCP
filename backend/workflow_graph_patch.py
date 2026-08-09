"""Canonical, deterministic GraphPatch v2 planning kernel.

The kernel validates a bounded graph augmentation against one complete editable
workflow snapshot and one pinned ``/object_info`` catalog.  It deliberately has
no canvas, queue, or execution side effects.  A frontend executor consumes the
returned apply envelope atomically and derives the exact final graph from its
own pinned snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, TypeAlias

from node_library import node_schema_hash
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)
from workflow_planner import (
    _canonical_widget_value,
    _expand_input_groups,
    _input_parts,
    _is_connectable,
    _validate_widget_value,
)
from workflow_refinement import NormalizedGraphEdge, NormalizedGraphSnapshot
from workflow_schema_capabilities import (
    InputCapability,
    MaterializedInput,
    NodeSchemaCapabilities,
    OutputCapability,
    classify_connection,
    infer_dynamic_selector_values,
    materialize_inputs,
    normalize_node_schema,
)

GRAPH_PATCH_SCHEMA = "fl-mcp.workflow-graph-patch.v2"
GRAPH_PATCH_HASH_SCHEMA = "fl-mcp.workflow-graph-patch-hash.v2"

_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_GRAPH_PATCH_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_GRAPH_PATCH_ATTACHMENTS = 8

NodeId: TypeAlias = StrictInt | StrictStr
SlotIndex: TypeAlias = Annotated[StrictInt, Field(ge=0)]
Coordinate: TypeAlias = StrictInt | StrictFloat


def _canonical_hash(value: Any) -> str:
    def normalize_json_numbers(item: Any) -> Any:
        """Make equivalent JSON number spellings hash identically.

        Tool/RPC boundaries are permitted to serialize an integral JSON number
        as either ``30`` or ``30.0``.  Those representations compare equal once
        decoded and resolve to the same schema-canonical widget value during the
        apply-time catalog recompile, so their lexical spelling must not break an
        otherwise unchanged apply envelope.  Booleans remain distinct, and
        non-integral floats retain their exact value.
        """

        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, Mapping):
            return {
                key: normalize_json_numbers(nested)
                for key, nested in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [normalize_json_numbers(nested) for nested in item]
        return item

    payload = json.dumps(
        normalize_json_numbers(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"severity": "error", "code": code, "path": path, "message": message}


def _is_image_upload_widget(capability: InputCapability) -> bool:
    """Return true only for ComfyUI's LoadImage-style upload choice widget."""

    return bool(
        capability.widget
        and not capability.connectable
        and capability.metadata.get("image_upload") is True
        and capability.enum_options
        and all(isinstance(option, str) for option in capability.enum_options)
    )


def _id_key(value: NodeId) -> tuple[str, str]:
    return type(value).__name__, json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ExistingNodeRef(BaseModel):
    """Reference an exact node already present in the pinned graph."""

    model_config = ConfigDict(extra="forbid")

    node_id: NodeId


class NewNodeRef(BaseModel):
    """Reference a node created by this patch through its stable alias."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_alias(self) -> NewNodeRef:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only lowercase "
                "letters, digits, and underscores"
            )
        return self


GraphPatchNodeRef: TypeAlias = ExistingNodeRef | NewNodeRef


def _ref_key(ref: GraphPatchNodeRef) -> tuple[str, Any]:
    if isinstance(ref, ExistingNodeRef):
        return "existing", _id_key(ref.node_id)
    return "new", ref.alias


def _ref_path(ref: GraphPatchNodeRef) -> str:
    if isinstance(ref, ExistingNodeRef):
        return f"existing:{type(ref.node_id).__name__}:{ref.node_id}"
    return f"new:{ref.alias}"


class GraphPatchLayoutHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: Coordinate
    y: Coordinate
    width: Coordinate | None = None
    height: Coordinate | None = None

    @model_validator(mode="after")
    def validate_geometry(self) -> GraphPatchLayoutHint:
        values = [self.x, self.y]
        if self.width is not None:
            values.append(self.width)
        if self.height is not None:
            values.append(self.height)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("layout coordinates and dimensions must be finite")
        if self.width is not None and float(self.width) <= 0:
            raise ValueError("layout width must be positive")
        if self.height is not None and float(self.height) <= 0:
            raise ValueError("layout height must be positive")
        return self


class GraphPatchSourceEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: GraphPatchNodeRef
    output_index: SlotIndex
    output: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=256)


class GraphPatchTargetEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: GraphPatchNodeRef
    input_index: SlotIndex
    occurrence_index: SlotIndex = 0
    socket_index: SlotIndex | None
    input: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=256)
    mode: Literal["slot", "convert_widget"] = "slot"

    @model_validator(mode="after")
    def validate_index_domains(self) -> GraphPatchTargetEndpoint:
        if self.mode == "slot" and self.socket_index is None:
            raise ValueError("slot targets require an exact socket_index")
        if self.mode == "convert_widget" and self.socket_index is not None:
            raise ValueError("convert_widget targets require socket_index=null")
        return self


class GraphPatchEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: GraphPatchSourceEndpoint
    target: GraphPatchTargetEndpoint


class GraphPatchNodeAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: ExistingNodeRef
    node_type: str = Field(..., min_length=1, max_length=256)
    schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class GraphPatchAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphPatchNodeAssertion] = Field(default_factory=list, max_length=500)
    edges: list[GraphPatchEdge] = Field(default_factory=list, max_length=2_000)


class GraphPatchCreateNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, max_length=64)
    node_type: str = Field(..., min_length=1, max_length=256)
    schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    values: dict[str, Any] = Field(default_factory=dict)
    layout_hint: GraphPatchLayoutHint | None = None

    @model_validator(mode="after")
    def validate_alias(self) -> GraphPatchCreateNode:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only lowercase "
                "letters, digits, and underscores"
            )
        return self


class GraphPatchUpdateNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: ExistingNodeRef
    node_type: str = Field(..., min_length=1, max_length=256)
    schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_values: dict[str, Any] = Field(default_factory=dict)
    set_values: dict[str, Any] = Field(default_factory=dict)
    layout_hint: GraphPatchLayoutHint | None = None

    @model_validator(mode="after")
    def validate_effect(self) -> GraphPatchUpdateNode:
        if not self.set_values and self.layout_hint is None:
            raise ValueError("update node needs set_values or layout_hint")
        return self


class GraphPatchRemoveNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: ExistingNodeRef
    node_type: str = Field(..., min_length=1, max_length=256)
    schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_incident_edges: list[GraphPatchEdge] = Field(
        default_factory=list,
        max_length=2_000,
    )


class GraphPatchAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: GraphPatchNodeRef
    input_index: SlotIndex
    input: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=256)
    filename: str = Field(..., min_length=1, max_length=512)
    subfolder: str = Field(..., min_length=8, max_length=1_024)
    file_type: Literal["input"] = "input"
    size_bytes: StrictInt = Field(..., ge=1, le=MAX_GRAPH_PATCH_ATTACHMENT_BYTES)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_relative_paths(self) -> GraphPatchAttachment:
        # Canonical GraphPatch attachments are compiler-attested Ren uploads,
        # not arbitrary references into ComfyUI's input/output/temp trees.
        # Keep the filename a basename and the directory a canonical POSIX path
        # rooted below input/ren-chat/<session>.
        if (
            "/" in self.filename
            or "\\" in self.filename
            or self.filename in {".", ".."}
            or PurePosixPath(self.filename).name != self.filename
            or PureWindowsPath(self.filename).drive
        ):
            raise ValueError("filename must be a safe basename")
        if "\\" in self.subfolder:
            raise ValueError("subfolder must use a canonical POSIX path")
        subfolder = PurePosixPath(self.subfolder)
        if (
            subfolder.is_absolute()
            or self.subfolder != subfolder.as_posix()
            or not subfolder.parts
            or subfolder.parts[0] != "ren-chat"
            or any(part in {"", ".", ".."} for part in subfolder.parts)
            or PureWindowsPath(self.subfolder).drive
        ):
            raise ValueError("subfolder must be inside the ren-chat input folder")
        return self


class GraphPatchExpectedDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_node_count: SlotIndex
    updated_node_count: SlotIndex
    removed_node_count: SlotIndex
    added_edge_count: SlotIndex
    removed_edge_count: SlotIndex
    final_node_count: SlotIndex
    final_edge_count: SlotIndex


class GraphPatchPlan(BaseModel):
    """Canonical frontend wire plan."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["patch"] = "patch"
    expected_workflow_identity: str = Field(..., min_length=8, max_length=256)
    expected_graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    assertions: GraphPatchAssertions
    create_nodes: list[GraphPatchCreateNode] = Field(default_factory=list, max_length=100)
    update_nodes: list[GraphPatchUpdateNode] = Field(default_factory=list, max_length=100)
    remove_edges: list[GraphPatchEdge] = Field(default_factory=list, max_length=2_000)
    add_edges: list[GraphPatchEdge] = Field(default_factory=list, max_length=2_000)
    remove_nodes: list[GraphPatchRemoveNode] = Field(default_factory=list, max_length=100)
    attachments: list[GraphPatchAttachment] = Field(
        default_factory=list,
        max_length=MAX_GRAPH_PATCH_ATTACHMENTS,
    )
    expected_delta: GraphPatchExpectedDelta


def graph_patch_hash(plan: GraphPatchPlan, expected_catalog_hash: str) -> str:
    return _canonical_hash(
        {
            "hash_schema": GRAPH_PATCH_HASH_SCHEMA,
            "expected_catalog_hash": expected_catalog_hash,
            "plan": plan.model_dump(mode="json"),
        }
    )


class ApplyGraphPatchRequest(BaseModel):
    """Idempotent canonical envelope consumed by the frontend executor."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(..., min_length=8, max_length=128)
    expected_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    patch_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    plan: GraphPatchPlan

    @model_validator(mode="after")
    def validate_identity_and_hash(self) -> ApplyGraphPatchRequest:
        if not _APPLICATION_ID_PATTERN.fullmatch(self.application_id):
            raise ValueError("application_id has an invalid format")
        expected = graph_patch_hash(self.plan, self.expected_catalog_hash)
        if self.patch_hash != expected:
            raise ValueError("patch_hash does not match the canonical plan")
        return self


class PlanGraphPatchRequest(BaseModel):
    """Dry-run input for compiling one canonical GraphPatch v2 envelope."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(..., min_length=8, max_length=128)
    expected_workflow_identity: str = Field(..., min_length=8, max_length=256)
    expected_graph_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    graph: NormalizedGraphSnapshot
    assertions: GraphPatchAssertions = Field(default_factory=GraphPatchAssertions)
    create_nodes: list[GraphPatchCreateNode] = Field(default_factory=list, max_length=100)
    update_nodes: list[GraphPatchUpdateNode] = Field(default_factory=list, max_length=100)
    remove_edges: list[GraphPatchEdge] = Field(default_factory=list, max_length=2_000)
    add_edges: list[GraphPatchEdge] = Field(default_factory=list, max_length=2_000)
    remove_nodes: list[GraphPatchRemoveNode] = Field(default_factory=list, max_length=100)
    attachments: list[GraphPatchAttachment] = Field(
        default_factory=list,
        max_length=MAX_GRAPH_PATCH_ATTACHMENTS,
    )

    @model_validator(mode="after")
    def validate_request(self) -> PlanGraphPatchRequest:
        if not _APPLICATION_ID_PATTERN.fullmatch(self.application_id):
            raise ValueError("application_id has an invalid format")
        if not any(
            (
                self.create_nodes,
                self.update_nodes,
                self.remove_edges,
                self.add_edges,
                self.remove_nodes,
                self.attachments,
            )
        ):
            raise ValueError("graph patch must contain at least one operation")
        payload = self.model_dump(mode="json", exclude_none=True)
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 2_097_152:
            raise ValueError("graph patch request must not exceed 2 MiB")
        return self


GraphPatchRequest = PlanGraphPatchRequest


def graph_patch_request_from_apply(
    envelope: ApplyGraphPatchRequest | Mapping[str, Any],
    graph: NormalizedGraphSnapshot | Mapping[str, Any],
) -> PlanGraphPatchRequest:
    """Reconstruct the only planner request represented by an apply envelope.

    Apply handlers must rebuild this request with a fresh complete graph,
    re-run :func:`compile_graph_patch` against the refreshed catalog, and
    require byte-for-byte canonical plan and ``patch_hash`` equality before
    invoking the frontend.  The helper itself is pure and performs no canvas
    mutation or queue/run action.
    """

    validated_envelope = (
        envelope
        if isinstance(envelope, ApplyGraphPatchRequest)
        else ApplyGraphPatchRequest.model_validate(envelope)
    )
    validated_graph = (
        graph
        if isinstance(graph, NormalizedGraphSnapshot)
        else NormalizedGraphSnapshot.model_validate(graph)
    )
    plan = validated_envelope.plan
    return PlanGraphPatchRequest(
        application_id=validated_envelope.application_id,
        expected_workflow_identity=plan.expected_workflow_identity,
        expected_graph_hash=plan.expected_graph_hash,
        expected_catalog_hash=validated_envelope.expected_catalog_hash,
        graph=validated_graph,
        assertions=plan.assertions,
        create_nodes=plan.create_nodes,
        update_nodes=plan.update_nodes,
        remove_edges=plan.remove_edges,
        add_edges=plan.add_edges,
        remove_nodes=plan.remove_nodes,
        attachments=plan.attachments,
    )


def _edge_key(edge: GraphPatchEdge, *, include_mode: bool = False) -> tuple[Any, ...]:
    key: tuple[Any, ...] = (
        _ref_key(edge.source.ref),
        edge.source.output_index,
        edge.source.output,
        edge.source.type,
        _ref_key(edge.target.ref),
        edge.target.input_index,
        edge.target.occurrence_index,
        edge.target.socket_index,
        edge.target.input,
        edge.target.type,
    )
    return (*key, edge.target.mode) if include_mode else key


def _edge_sort_key(edge: GraphPatchEdge) -> tuple[Any, ...]:
    # JSON text avoids Python's non-orderable None/int comparison for malformed
    # requests that mention one target as both a slot and a widget.
    return (
        json.dumps(
            edge.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _node_sort_key(ref: GraphPatchNodeRef) -> tuple[Any, ...]:
    return _ref_key(ref)


def _type_is_allowed(allowed_types: tuple[str, ...], concrete_type: str) -> bool:
    return "*" in allowed_types or concrete_type in allowed_types


def _activation_values(
    capability: InputCapability,
    values: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Materialize the exact selector branch containing ``capability``.

    A normalized graph records live socket indexes and names, but deliberately
    does not copy every widget value into the safety envelope.  Selector
    constraints are nevertheless sufficient to reproduce the one branch that
    declares a named socket.  Conflicting caller-provided selector values fail
    closed instead of guessing another branch.
    """

    result = dict(values)
    for constraint in capability.activation:
        if constraint.kind != "selector_equals":
            continue
        if constraint.source in result and result[constraint.source] != constraint.value:
            return None
        result[constraint.source] = constraint.value
    return result


def _materialize_capability(
    capabilities: NodeSchemaCapabilities,
    capability: InputCapability,
    *,
    values: Mapping[str, Any],
    connected_inputs: set[str],
) -> MaterializedInput | None:
    selector_values = _activation_values(capability, values)
    if selector_values is None:
        return None
    required_connections = set(connected_inputs)
    required_connections.update(
        constraint.source
        for constraint in capability.activation
        if constraint.kind == "input_connected"
    )
    materialized = materialize_inputs(
        capabilities,
        values=selector_values,
        connected_inputs=required_connections,
        include_inactive=True,
    )
    return next(
        (
            item
            for item in materialized
            if item.capability.declaration_index == capability.declaration_index
            and item.capability.occurrence_index == capability.occurrence_index
            and item.capability.path == capability.path
            and item.activation_state != "inactive"
        ),
        None,
    )


def _uses_dynamic_socket_projection(
    capabilities: NodeSchemaCapabilities,
    capability: InputCapability,
) -> bool:
    """Return whether a live socket index is runtime-defined for this input.

    Dynamic combo, autogrow, and dynamic-slot inputs can be compacted or moved
    by the browser after their selector/connection callbacks run.  Their schema
    declaration remains authoritative, but the materializer's connectable-only
    socket index is only a projection and need not equal LiteGraph's serialized
    ``node.inputs`` index.
    """

    return any(
        capability.path in group.generated_inputs
        or (
            group.kind == "dynamic_slot"
            and capability.kind == "dynamic_slot"
            and capability.path == group.path
        )
        for group in capabilities.dynamic_groups
    )


def _captured_dynamic_target_key(
    *,
    node_id: NodeId,
    input_index: int,
    occurrence_index: int,
    socket_index: int | None,
    input_name: str,
    input_type: str,
) -> tuple[Any, ...]:
    return (
        _id_key(node_id),
        input_index,
        occurrence_index,
        socket_index,
        input_name,
        input_type,
        "slot",
    )


def _baseline_edge(
    edge: NormalizedGraphEdge,
    *,
    target_capabilities: NodeSchemaCapabilities,
    target_widget_values: list[Any],
    connected_inputs: set[str],
    path: str,
    issues: list[dict[str, str]],
    captured_dynamic_targets: set[tuple[Any, ...]] | None = None,
) -> GraphPatchEdge:
    """Translate a serialized baseline link into both schema and socket domains."""

    selector_values = infer_dynamic_selector_values(
        target_capabilities,
        target_widget_values,
        connected_inputs=connected_inputs,
    )
    candidates: list[tuple[InputCapability, MaterializedInput]] = []
    for capability in target_capabilities.inputs:
        if (
            capability.hidden
            or not (capability.connectable or capability.widget_convertible)
            or capability.path != edge.target_input
            or not _type_is_allowed(capability.accepted_types, edge.type)
        ):
            continue
        materialized = _materialize_capability(
            target_capabilities,
            capability,
            values=selector_values,
            connected_inputs=connected_inputs,
        )
        if materialized is not None and (
            (
                capability.connectable
                and (
                    materialized.socket_index == edge.target_input_index
                    or _uses_dynamic_socket_projection(
                        target_capabilities,
                        capability,
                    )
                )
            )
            or capability.widget_convertible
        ):
            candidates.append((capability, materialized))
    if len(candidates) != 1:
        issues.append(
            _issue(
                "baseline_target_slot_unresolved",
                path,
                f"Baseline target {edge.target_node_id!r}.{edge.target_input} at live "
                f"socket {edge.target_input_index} resolves to {len(candidates)} exact "
                "active schema inputs; the graph cannot be patched safely.",
            )
        )
        # Preserve graph cardinality for downstream diagnostics.  The hard
        # issue above prevents this fallback from entering an apply envelope.
        declaration_index = edge.target_input_index
        occurrence_index = 0
    else:
        declaration_index = candidates[0][0].declaration_index
        occurrence_index = candidates[0][0].occurrence_index
        if (
            captured_dynamic_targets is not None
            and _uses_dynamic_socket_projection(target_capabilities, candidates[0][0])
        ):
            captured_dynamic_targets.add(
                _captured_dynamic_target_key(
                    node_id=edge.target_node_id,
                    input_index=declaration_index,
                    occurrence_index=occurrence_index,
                    socket_index=edge.target_input_index,
                    input_name=edge.target_input,
                    input_type=edge.type,
                )
            )
    return GraphPatchEdge(
        source={
            "ref": {"node_id": edge.source_node_id},
            "output_index": edge.source_output_index,
            "output": edge.source_output,
            "type": edge.type,
        },
        target={
            "ref": {"node_id": edge.target_node_id},
            "input_index": declaration_index,
            "occurrence_index": occurrence_index,
            "socket_index": edge.target_input_index,
            "input": edge.target_input,
            "type": edge.type,
            "mode": "slot",
        },
    )


def _opaque_baseline_edge(edge: NormalizedGraphEdge) -> GraphPatchEdge:
    """Preserve one unrelated serialized edge without inventing schema facts.

    Comfy workflows may contain frontend-only primitives, reroutes, subgraph
    artifacts, or nodes whose Python class is no longer loaded.  Those edges
    remain protected by the pinned whole-graph hash and the frontend's exact
    snapshot/final-topology verification.  Requiring ``/object_info`` for an
    edge that the patch never touches would make every otherwise independent
    edit impossible.  The live socket index is therefore used as a stable
    opaque declaration index only for preservation; touched endpoints still go
    through the strict schema path in :func:`_baseline_edge`.
    """

    return GraphPatchEdge(
        source={
            "ref": {"node_id": edge.source_node_id},
            "output_index": edge.source_output_index,
            "output": edge.source_output,
            "type": edge.type,
        },
        target={
            "ref": {"node_id": edge.target_node_id},
            "input_index": edge.target_input_index,
            "occurrence_index": 0,
            "socket_index": edge.target_input_index,
            "input": edge.target_input,
            "type": edge.type,
            "mode": "slot",
        },
    )


def _schema_type(spec: Any) -> str | None:
    input_type, _ = _input_parts(spec)
    if isinstance(input_type, str):
        return input_type
    if isinstance(input_type, list) and all(isinstance(item, str) for item in input_type):
        return "COMBO"
    return None


def _input_slots(
    raw_info: Mapping[str, Any],
    values: Mapping[str, Any],
    connected_inputs: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    groups, issues = _expand_input_groups(
        raw_info.get("input") if isinstance(raw_info.get("input"), dict) else {},
        values,
        connected_inputs,
    )
    slots: list[dict[str, Any]] = []
    ordered = [
        (group, name, spec)
        for group in ("required", "optional")
        for name, spec in groups[group].items()
    ]
    occurrences: dict[str, int] = {}
    socket_index = 0
    for index, (group, name, spec) in enumerate(ordered):
        occurrence_index = occurrences.get(name, 0)
        occurrences[name] = occurrence_index + 1
        connectable = _is_connectable(spec)
        slots.append({
            "index": index,
            "occurrence_index": occurrence_index,
            "socket_index": socket_index if connectable else None,
            "name": name,
            "type": _schema_type(spec),
            "spec": spec,
            "group": group,
            "connectable": connectable,
        })
        if connectable:
            socket_index += 1
    return slots, issues


def _matching_input_slots(
    slots: list[dict[str, Any]],
    name: str,
    occurrence_index: int | None = None,
) -> list[dict[str, Any]]:
    return [
        slot
        for slot in slots
        if slot["name"] == name
        and (
            occurrence_index is None
            or slot["occurrence_index"] == occurrence_index
        )
    ]


def _canonical_values(
    node_type: str,
    raw_info: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    connected_inputs: set[str],
    attachment_inputs: set[str],
    require_complete: bool,
    path: str,
    context_values: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    slots, dynamic_issues = _input_slots(
        raw_info,
        context_values if context_values is not None else values,
        connected_inputs | attachment_inputs,
    )
    capabilities = normalize_node_schema(node_type, raw_info)
    capability_paths = {
        capability.path
        for capability in capabilities.inputs
        if capability.connectable or capability.widget or capability.widget_convertible
    }
    dynamic_issues = [
        item
        for item in dynamic_issues
        if not (
            item["code"] == "unsupported_dynamic_input"
            and item["path"].removeprefix("inputs.") in capability_paths
        )
    ]
    issues = [
        _issue(item["code"], f"{path}.{item['path']}", item["message"])
        for item in dynamic_issues
    ]
    accepted: dict[str, Any] = {}
    for name, value in values.items():
        matches = _matching_input_slots(slots, name)
        if not matches:
            issues.append(
                _issue(
                    "unknown_widget",
                    f"{path}.values.{name}",
                    f"{node_type} has no active runtime input named {name!r}.",
                )
            )
            continue
        if len(matches) > 1:
            issues.append(
                _issue(
                    "ambiguous_widget_name",
                    f"{path}.values.{name}",
                    f"{node_type} has more than one active runtime input named {name!r}.",
                )
            )
            continue
        slot = matches[0]
        if name in connected_inputs:
            issues.append(
                _issue(
                    "value_connection_conflict",
                    f"{path}.values.{name}",
                    f"{node_type}.{name} is supplied by a connection and cannot also have a value.",
                )
            )
            continue
        if name in attachment_inputs:
            issues.append(
                _issue(
                    "value_attachment_conflict",
                    f"{path}.values.{name}",
                    f"{node_type}.{name} is supplied by an attachment and cannot also have a value.",
                )
            )
            continue
        value_issues = _validate_widget_value(name, slot["spec"], value)
        for item in value_issues:
            issues.append(
                _issue(item["code"], f"{path}.{item['path']}", item["message"])
            )
        if not value_issues:
            accepted[name] = _canonical_widget_value(slot["spec"], value)

    if require_complete:
        for slot in slots:
            name = slot["name"]
            if slot["connectable"]:
                continue
            if name in connected_inputs or name in attachment_inputs:
                continue
            _, metadata = _input_parts(slot["spec"])
            if metadata.get("dynamic_type") == "COMFY_DYNAMICCOMBO_V3" and name in values:
                continue
            if name not in values:
                issues.append(
                    _issue(
                        "missing_widget_value",
                        f"{path}.values.{name}",
                        f"{node_type} needs an explicit value for active widget {name!r}.",
                    )
                )
        supplied_inputs = connected_inputs | attachment_inputs
        active_capabilities = materialize_inputs(
            capabilities,
            values=context_values if context_values is not None else values,
            connected_inputs=supplied_inputs,
        )
        for materialized in active_capabilities:
            capability = materialized.capability
            if (
                capability.hidden
                or not capability.required
                or not capability.connectable
                or capability.path in connected_inputs
            ):
                continue
            issues.append(
                _issue(
                    "missing_required_connection",
                    f"{path}.inputs.{capability.path}",
                    f"Required input {node_type}.{capability.path} has no incoming edge.",
                )
            )
    return {name: accepted[name] for name in sorted(accepted)}, slots, issues


def _deduplicate_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, str]] = []
    for issue in issues:
        key = (issue["code"], issue["path"], issue["message"])
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def _has_cycle(nodes: set[tuple[Any, ...]], edges: list[GraphPatchEdge]) -> bool:
    indegree = dict.fromkeys(nodes, 0)
    adjacency: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {
        node: set() for node in nodes
    }
    for edge in edges:
        source = _ref_key(edge.source.ref)
        target = _ref_key(edge.target.ref)
        if source not in adjacency or target not in indegree:
            continue
        if target not in adjacency[source]:
            adjacency[source].add(target)
            indegree[target] += 1
    ready = deque(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in adjacency[source]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited != len(nodes)


def compile_graph_patch(
    request: PlanGraphPatchRequest,
    catalog: Mapping[str, Any],
    *,
    catalog_hash: str,
    source: str,
) -> dict[str, Any]:
    """Validate and canonicalize one GraphPatch without mutating the canvas."""

    issues: list[dict[str, str]] = []
    if request.expected_catalog_hash != catalog_hash:
        issues.append(
            _issue(
                "catalog_changed",
                "expected_catalog_hash",
                "The loaded-node catalog changed after discovery.",
            )
        )

    graph_nodes = {_id_key(node.node_id): node for node in request.graph.nodes}
    frontend_node_ids: dict[str, tuple[str, str]] = {}
    for node in request.graph.nodes:
        typed_key = _id_key(node.node_id)
        browser_key = str(node.node_id)
        previous = frontend_node_ids.setdefault(browser_key, typed_key)
        if previous != typed_key:
            issues.append(
                _issue(
                    "frontend_node_identity_collision",
                    "graph.nodes",
                    "The canvas contains numeric and string node IDs that collapse to "
                    f"the same browser identity {browser_key!r}; patching is unsafe.",
                )
            )
    capabilities_cache: dict[str, NodeSchemaCapabilities] = {}

    def capabilities_for(
        node_type: str,
        raw_info: Mapping[str, Any],
    ) -> NodeSchemaCapabilities:
        cached = capabilities_cache.get(node_type)
        if cached is None:
            cached = normalize_node_schema(node_type, raw_info)
            capabilities_cache[node_type] = cached
        return cached

    baseline_connected_inputs: dict[tuple[str, str], set[str]] = {}
    for edge in request.graph.edges:
        baseline_connected_inputs.setdefault(
            _id_key(edge.target_node_id), set()
        ).add(edge.target_input)
    graph_outputs = {
        (_id_key(output.node_id), output.output_index): output
        for output in request.graph.outputs
    }
    strict_baseline_node_keys: set[tuple[str, str]] = set()

    def require_baseline_schema(ref: GraphPatchNodeRef) -> None:
        if isinstance(ref, ExistingNodeRef):
            strict_baseline_node_keys.add(_id_key(ref.node_id))

    for assertion in request.assertions.nodes:
        require_baseline_schema(assertion.ref)
    for item in request.update_nodes:
        require_baseline_schema(item.ref)
    for item in request.remove_nodes:
        require_baseline_schema(item.ref)
    for item in request.attachments:
        require_baseline_schema(item.ref)
    for item in [*request.assertions.edges, *request.remove_edges]:
        require_baseline_schema(item.source.ref)
        require_baseline_schema(item.target.ref)

    baseline_edges: list[GraphPatchEdge] = []
    captured_dynamic_targets: set[tuple[Any, ...]] = set()
    for index, edge in enumerate(request.graph.edges):
        source_is_strict = _id_key(edge.source_node_id) in strict_baseline_node_keys
        target_is_strict = _id_key(edge.target_node_id) in strict_baseline_node_keys
        source_node = graph_nodes.get(_id_key(edge.source_node_id))
        raw_source = catalog.get(source_node.node_type) if source_node is not None else None
        active_source = graph_outputs.get(
            (_id_key(edge.source_node_id), edge.source_output_index)
        )
        if source_is_strict and (source_node is None or not isinstance(raw_source, Mapping)):
            issues.append(
                _issue(
                    "baseline_source_schema_unavailable",
                    f"graph.edges[{index}].source_node_id",
                    "The baseline source node or its loaded schema is unavailable.",
                )
            )
        elif source_is_strict:
            output_matches = [
                output
                for output in capabilities_for(source_node.node_type, raw_source).outputs
                if output.index == edge.source_output_index
                and output.name == edge.source_output
                and _type_is_allowed(output.produced_types, edge.type)
            ]
            if len(output_matches) != 1:
                issues.append(
                    _issue(
                        "baseline_source_slot_mismatch",
                        f"graph.edges[{index}].source_output_index",
                        "The baseline source output does not match its loaded schema.",
                    )
                )
        if source_is_strict and (
            active_source is None
            or active_source.output != edge.source_output
            or active_source.type != edge.type
        ):
            issues.append(
                _issue(
                    "baseline_active_source_slot_mismatch",
                    f"graph.edges[{index}].source_output_index",
                    "The baseline edge source is absent from the active output manifest.",
                )
            )
        target_node = graph_nodes.get(_id_key(edge.target_node_id))
        raw_target = catalog.get(target_node.node_type) if target_node is not None else None
        if target_is_strict and (target_node is None or not isinstance(raw_target, Mapping)):
            issues.append(
                _issue(
                    "baseline_target_schema_unavailable",
                    f"graph.edges[{index}].target_node_id",
                    "The baseline target node or its loaded schema is unavailable.",
                )
            )
            baseline_edges.append(_opaque_baseline_edge(edge))
        elif isinstance(raw_target, Mapping) and target_node is not None:
            target_capabilities = capabilities_for(target_node.node_type, raw_target)
            target_issues: list[dict[str, str]] = []
            canonical_edge = _baseline_edge(
                edge,
                target_capabilities=target_capabilities,
                target_widget_values=target_node.widget_values,
                connected_inputs=baseline_connected_inputs.get(
                    _id_key(edge.target_node_id), set()
                ),
                path=f"graph.edges[{index}].target_input_index",
                issues=target_issues,
                captured_dynamic_targets=captured_dynamic_targets,
            )
            if target_is_strict:
                issues.extend(target_issues)
            baseline_edges.append(
                _opaque_baseline_edge(edge)
                if target_issues and not target_is_strict
                else canonical_edge
            )
        else:
            baseline_edges.append(_opaque_baseline_edge(edge))
    baseline_edge_map = {_edge_key(edge): edge for edge in baseline_edges}

    if len(graph_nodes) != len(request.graph.nodes):
        issues.append(_issue("duplicate_graph_node", "graph.nodes", "Graph node IDs must be unique."))
    if len(baseline_edge_map) != len(baseline_edges):
        issues.append(_issue("duplicate_graph_edge", "graph.edges", "Graph edges must be unique."))
    if len(graph_outputs) != len(request.graph.outputs):
        issues.append(_issue("duplicate_graph_output", "graph.outputs", "Graph outputs must be unique."))

    create_by_alias: dict[str, GraphPatchCreateNode] = {}
    for index, node in enumerate(request.create_nodes):
        if node.alias in create_by_alias:
            issues.append(
                _issue(
                    "duplicate_create_alias",
                    f"create_nodes[{index}].alias",
                    f"Created alias {node.alias!r} is declared more than once.",
                )
            )
        create_by_alias[node.alias] = node

    assertion_by_id: dict[tuple[str, str], GraphPatchNodeAssertion] = {}
    canonical_assertions: list[GraphPatchNodeAssertion] = []
    for index, assertion in enumerate(request.assertions.nodes):
        key = _id_key(assertion.ref.node_id)
        if key in assertion_by_id:
            issues.append(
                _issue(
                    "duplicate_node_assertion",
                    f"assertions.nodes[{index}].ref.node_id",
                    f"Node {assertion.ref.node_id!r} is asserted more than once.",
                )
            )
            continue
        assertion_by_id[key] = assertion
        graph_node = graph_nodes.get(key)
        if graph_node is None:
            issues.append(
                _issue(
                    "asserted_node_missing",
                    f"assertions.nodes[{index}].ref.node_id",
                    f"Asserted node {assertion.ref.node_id!r} is absent from the graph.",
                )
            )
            continue
        if graph_node.node_type != assertion.node_type:
            issues.append(
                _issue(
                    "asserted_node_type_mismatch",
                    f"assertions.nodes[{index}].node_type",
                    f"Node {assertion.ref.node_id!r} is {graph_node.node_type}, not {assertion.node_type}.",
                )
            )
        raw_info = catalog.get(graph_node.node_type)
        if not isinstance(raw_info, Mapping):
            issues.append(
                _issue(
                    "asserted_node_type_not_loaded",
                    f"assertions.nodes[{index}].node_type",
                    f"Node type {graph_node.node_type!r} is not loaded.",
                )
            )
        elif assertion.schema_hash != node_schema_hash(graph_node.node_type, raw_info):
            issues.append(
                _issue(
                    "asserted_node_schema_mismatch",
                    f"assertions.nodes[{index}].schema_hash",
                    f"Node {assertion.ref.node_id!r} no longer matches its asserted schema.",
                )
            )
        canonical_assertions.append(assertion)

    def resolve_ref(
        ref: GraphPatchNodeRef,
        *,
        path: str,
    ) -> tuple[str | None, Mapping[str, Any] | None, str | None]:
        if isinstance(ref, ExistingNodeRef):
            graph_node = graph_nodes.get(_id_key(ref.node_id))
            if graph_node is None:
                issues.append(
                    _issue(
                        "unknown_existing_ref",
                        path,
                        f"Existing node {ref.node_id!r} is absent from the graph.",
                    )
                )
                return None, None, None
            raw_info = catalog.get(graph_node.node_type)
            if not isinstance(raw_info, Mapping):
                issues.append(
                    _issue(
                        "node_type_not_loaded",
                        path,
                        f"Existing node {ref.node_id!r} uses unloaded type {graph_node.node_type!r}.",
                    )
                )
                return graph_node.node_type, None, None
            return graph_node.node_type, raw_info, node_schema_hash(graph_node.node_type, raw_info)
        created = create_by_alias.get(ref.alias)
        if created is None:
            issues.append(
                _issue(
                    "unknown_new_ref",
                    path,
                    f"New alias {ref.alias!r} has no create_nodes declaration.",
                )
            )
            return None, None, None
        raw_info = catalog.get(created.node_type)
        if not isinstance(raw_info, Mapping):
            issues.append(
                _issue(
                    "node_type_not_loaded",
                    path,
                    f"Node type {created.node_type!r} is not loaded.",
                )
            )
            return created.node_type, None, None
        return created.node_type, raw_info, node_schema_hash(created.node_type, raw_info)

    touched_existing: set[tuple[str, str]] = set()
    for collection in (request.add_edges, request.remove_edges, request.assertions.edges):
        for edge in collection:
            for ref in (edge.source.ref, edge.target.ref):
                if isinstance(ref, ExistingNodeRef):
                    touched_existing.add(_id_key(ref.node_id))
    for update in request.update_nodes:
        touched_existing.add(_id_key(update.ref.node_id))
    for removal in request.remove_nodes:
        touched_existing.add(_id_key(removal.ref.node_id))
        for edge in removal.expected_incident_edges:
            for ref in (edge.source.ref, edge.target.ref):
                if isinstance(ref, ExistingNodeRef):
                    touched_existing.add(_id_key(ref.node_id))
    for attachment in request.attachments:
        if isinstance(attachment.ref, ExistingNodeRef):
            touched_existing.add(_id_key(attachment.ref.node_id))
    for key in sorted(touched_existing):
        if key not in assertion_by_id:
            issues.append(
                _issue(
                    "missing_node_assertion",
                    "assertions.nodes",
                    f"Touched existing node {key[1]} lacks an exact node assertion.",
                )
            )
    for key, assertion in assertion_by_id.items():
        if key not in touched_existing:
            issues.append(
                _issue(
                    "unused_node_assertion",
                    "assertions.nodes",
                    f"Node assertion for {assertion.ref.node_id!r} is not touched by the patch.",
                )
            )

    incoming_by_ref: dict[tuple[str, Any], set[str]] = {}
    for edge in request.add_edges:
        ref_key = _ref_key(edge.target.ref)
        incoming_by_ref.setdefault(ref_key, set()).add(edge.target.input)
    removed_incoming_by_ref: dict[tuple[str, Any], set[str]] = {}
    for edge in request.remove_edges:
        ref_key = _ref_key(edge.target.ref)
        removed_incoming_by_ref.setdefault(ref_key, set()).add(edge.target.input)

    def current_connected_inputs(ref: GraphPatchNodeRef) -> set[str]:
        if not isinstance(ref, ExistingNodeRef):
            return set()
        return set(baseline_connected_inputs.get(_id_key(ref.node_id), set()))

    def future_connected_inputs(ref: GraphPatchNodeRef) -> set[str]:
        ref_key = _ref_key(ref)
        return {
            *(current_connected_inputs(ref) - removed_incoming_by_ref.get(ref_key, set())),
            *incoming_by_ref.get(ref_key, set()),
        }
    attachment_by_ref: dict[tuple[str, Any], set[str]] = {}
    for attachment in request.attachments:
        attachment_by_ref.setdefault(_ref_key(attachment.ref), set()).add(attachment.input)

    canonical_creates: list[GraphPatchCreateNode] = []
    new_capabilities: dict[str, NodeSchemaCapabilities] = {}
    new_values: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(request.create_nodes):
        raw_info = catalog.get(node.node_type)
        path = f"create_nodes[{index}]"
        if not isinstance(raw_info, Mapping):
            issues.append(
                _issue(
                    "node_type_not_loaded",
                    f"{path}.node_type",
                    f"Node type {node.node_type!r} is not loaded.",
                )
            )
            continue
        actual_schema_hash = node_schema_hash(node.node_type, raw_info)
        if node.schema_hash != actual_schema_hash:
            issues.append(
                _issue(
                    "create_schema_mismatch",
                    f"{path}.schema_hash",
                    f"{node.node_type} no longer matches the supplied schema hash.",
                )
            )
        ref_key = _ref_key(NewNodeRef(alias=node.alias))
        accepted, slots, value_issues = _canonical_values(
            node.node_type,
            raw_info,
            node.values,
            connected_inputs=incoming_by_ref.get(ref_key, set()),
            attachment_inputs=attachment_by_ref.get(ref_key, set()),
            require_complete=True,
            path=path,
        )
        issues.extend(value_issues)
        new_capabilities[node.alias] = capabilities_for(node.node_type, raw_info)
        new_values[node.alias] = accepted
        canonical_creates.append(
            GraphPatchCreateNode(
                alias=node.alias,
                node_type=node.node_type,
                schema_hash=actual_schema_hash,
                values=accepted,
                layout_hint=node.layout_hint,
            )
        )

    canonical_updates: list[GraphPatchUpdateNode] = []
    update_keys: set[tuple[str, str]] = set()
    for index, update in enumerate(request.update_nodes):
        path = f"update_nodes[{index}]"
        key = _id_key(update.ref.node_id)
        if key in update_keys:
            issues.append(
                _issue(
                    "duplicate_update_node",
                    f"{path}.ref.node_id",
                    f"Node {update.ref.node_id!r} is updated more than once.",
                )
            )
            continue
        update_keys.add(key)
        graph_node = graph_nodes.get(key)
        if graph_node is None:
            issues.append(_issue("unknown_existing_ref", f"{path}.ref", "Update target is absent."))
            continue
        raw_info = catalog.get(graph_node.node_type)
        if not isinstance(raw_info, Mapping):
            issues.append(_issue("node_type_not_loaded", f"{path}.node_type", "Update type is unloaded."))
            continue
        actual_hash = node_schema_hash(graph_node.node_type, raw_info)
        if update.node_type != graph_node.node_type or update.schema_hash != actual_hash:
            issues.append(
                _issue(
                    "update_node_mismatch",
                    path,
                    "Update node type or schema hash does not match the pinned graph.",
                )
            )
        merged_values = {**update.expected_values, **update.set_values}
        accepted_expected, _, expected_issues = _canonical_values(
            graph_node.node_type,
            raw_info,
            update.expected_values,
            connected_inputs=current_connected_inputs(update.ref),
            attachment_inputs=attachment_by_ref.get(("existing", key), set()),
            require_complete=False,
            path=f"{path}.expected_values",
            context_values=update.expected_values,
        )
        accepted_set, _, set_issues = _canonical_values(
            graph_node.node_type,
            raw_info,
            update.set_values,
            connected_inputs=future_connected_inputs(update.ref),
            attachment_inputs=attachment_by_ref.get(("existing", key), set()),
            require_complete=False,
            path=f"{path}.set_values",
            context_values=merged_values,
        )
        issues.extend(expected_issues)
        issues.extend(set_issues)
        canonical_updates.append(
            GraphPatchUpdateNode(
                ref=update.ref,
                node_type=graph_node.node_type,
                schema_hash=actual_hash,
                expected_values=accepted_expected,
                set_values=accepted_set,
                layout_hint=update.layout_hint,
            )
        )

    remove_node_keys: set[tuple[str, str]] = set()
    canonical_removes: list[GraphPatchRemoveNode] = []
    for index, removal in enumerate(request.remove_nodes):
        path = f"remove_nodes[{index}]"
        key = _id_key(removal.ref.node_id)
        if key in remove_node_keys:
            issues.append(
                _issue(
                    "duplicate_remove_node",
                    f"{path}.ref.node_id",
                    f"Node {removal.ref.node_id!r} is removed more than once.",
                )
            )
            continue
        remove_node_keys.add(key)
        graph_node = graph_nodes.get(key)
        if graph_node is None:
            issues.append(_issue("unknown_existing_ref", f"{path}.ref", "Removal target is absent."))
            continue
        raw_info = catalog.get(graph_node.node_type)
        if not isinstance(raw_info, Mapping):
            issues.append(_issue("node_type_not_loaded", f"{path}.node_type", "Removal type is unloaded."))
            continue
        actual_hash = node_schema_hash(graph_node.node_type, raw_info)
        if removal.node_type != graph_node.node_type or removal.schema_hash != actual_hash:
            issues.append(
                _issue(
                    "remove_node_mismatch",
                    path,
                    "Removal node type or schema hash does not match the pinned graph.",
                )
            )
        incident = sorted(
            [
                edge
                for edge in baseline_edges
                if _ref_key(edge.source.ref) == ("existing", key)
                or _ref_key(edge.target.ref) == ("existing", key)
            ],
            key=_edge_sort_key,
        )
        expected_incident = sorted(removal.expected_incident_edges, key=_edge_sort_key)
        if {_edge_key(edge, include_mode=True) for edge in incident} != {
            _edge_key(edge, include_mode=True) for edge in expected_incident
        }:
            issues.append(
                _issue(
                    "undeclared_incident_edge",
                    f"{path}.expected_incident_edges",
                    "Removal must declare every exact baseline edge incident to the node.",
                )
            )
        canonical_removes.append(
            GraphPatchRemoveNode(
                ref=removal.ref,
                node_type=graph_node.node_type,
                schema_hash=actual_hash,
                expected_incident_edges=incident,
            )
        )
    if update_keys & remove_node_keys:
        issues.append(
            _issue(
                "update_remove_conflict",
                "update_nodes",
                "The same existing node cannot be updated and removed in one patch.",
            )
        )

    def is_existing_converted_slot(
        endpoint: GraphPatchTargetEndpoint,
        capability: InputCapability,
    ) -> bool:
        if not isinstance(endpoint.ref, ExistingNodeRef) or not capability.widget_convertible:
            return False
        return any(
            isinstance(edge.target.ref, ExistingNodeRef)
            and _id_key(edge.target.ref.node_id) == _id_key(endpoint.ref.node_id)
            and edge.target.input_index == endpoint.input_index
            and edge.target.occurrence_index == endpoint.occurrence_index
            and edge.target.socket_index == endpoint.socket_index
            and edge.target.input == endpoint.input
            and edge.target.type == endpoint.type
            for edge in baseline_edges
        )

    def validate_source(
        endpoint: GraphPatchSourceEndpoint,
        path: str,
    ) -> OutputCapability | None:
        node_type, raw_info, _ = resolve_ref(endpoint.ref, path=f"{path}.ref")
        if raw_info is None or node_type is None:
            return None
        capabilities = capabilities_for(node_type, raw_info)
        matches = [
            slot
            for slot in capabilities.outputs
            if slot.index == endpoint.output_index
            and slot.name == endpoint.output
            and _type_is_allowed(slot.produced_types, endpoint.type)
        ]
        if not matches:
            issues.append(
                _issue(
                    "source_slot_mismatch",
                    path,
                    f"{node_type} has no exact output {endpoint.output!r} at index "
                    f"{endpoint.output_index} with type {endpoint.type!r}.",
                )
            )
            return None
        if isinstance(endpoint.ref, ExistingNodeRef):
            active = graph_outputs.get((_id_key(endpoint.ref.node_id), endpoint.output_index))
            if (
                active is None
                or active.output != endpoint.output
                or active.type != endpoint.type
            ):
                issues.append(
                    _issue(
                        "active_source_slot_mismatch",
                        path,
                        "The active canvas source output does not match the supplied facts.",
                    )
                )
        return matches[0]

    def validate_target(
        endpoint: GraphPatchTargetEndpoint,
        path: str,
        *,
        state: Literal["current", "future"] = "future",
        allow_captured_dynamic_socket: bool = False,
    ) -> InputCapability | None:
        node_type, raw_info, _ = resolve_ref(endpoint.ref, path=f"{path}.ref")
        if raw_info is None or node_type is None:
            return None
        capabilities = capabilities_for(node_type, raw_info)
        if isinstance(endpoint.ref, NewNodeRef):
            capabilities = new_capabilities.get(endpoint.ref.alias, capabilities)
            values: Mapping[str, Any] = new_values.get(endpoint.ref.alias, {})
            connected_inputs = incoming_by_ref.get(_ref_key(endpoint.ref), set())
        else:
            update = next(
                (item for item in canonical_updates if item.ref.node_id == endpoint.ref.node_id),
                None,
            )
            if update is None:
                values = {}
            elif state == "current":
                values = update.expected_values
            else:
                values = {**update.expected_values, **update.set_values}
            connected_inputs = (
                current_connected_inputs(endpoint.ref)
                if state == "current"
                else future_connected_inputs(endpoint.ref)
            )
        matches = [
            capability
            for capability in capabilities.inputs
            if not capability.hidden
            and capability.path == endpoint.input
            and capability.declaration_index == endpoint.input_index
            and capability.occurrence_index == endpoint.occurrence_index
            and _type_is_allowed(capability.accepted_types, endpoint.type)
        ]
        capability = matches[0] if len(matches) == 1 else None
        materialized = (
            _materialize_capability(
                capabilities,
                capability,
                values=values,
                connected_inputs=set(connected_inputs),
            )
            if capability is not None
            else None
        )
        if capability is None or materialized is None:
            issues.append(
                _issue(
                    "target_slot_mismatch",
                    path,
                    f"{node_type} has no exact active input {endpoint.input!r} at index "
                    f"{endpoint.input_index} with type {endpoint.type!r}.",
                )
            )
            return None
        converted_existing = is_existing_converted_slot(endpoint, capability)
        captured_dynamic_socket = (
            allow_captured_dynamic_socket
            and isinstance(endpoint.ref, ExistingNodeRef)
            and _uses_dynamic_socket_projection(capabilities, capability)
            and _captured_dynamic_target_key(
                node_id=endpoint.ref.node_id,
                input_index=endpoint.input_index,
                occurrence_index=endpoint.occurrence_index,
                socket_index=endpoint.socket_index,
                input_name=endpoint.input,
                input_type=endpoint.type,
            )
            in captured_dynamic_targets
        )
        if (
            endpoint.mode == "slot"
            and not converted_existing
            and not captured_dynamic_socket
            and endpoint.socket_index != materialized.socket_index
        ):
            issues.append(
                _issue(
                    "target_socket_index_mismatch",
                    f"{path}.socket_index",
                    f"{node_type}.{endpoint.input} has socket index {materialized.socket_index!r}, "
                    f"not {endpoint.socket_index!r}.",
                )
            )
        if (
            endpoint.mode == "slot"
            and not capability.connectable
            and not converted_existing
        ):
            issues.append(
                _issue(
                    "target_requires_widget_conversion",
                    f"{path}.mode",
                    f"{node_type}.{endpoint.input} is a widget; use mode='convert_widget'.",
                )
            )
        if endpoint.mode == "convert_widget":
            if capability.connectable:
                issues.append(
                    _issue(
                        "unnecessary_widget_conversion",
                        f"{path}.mode",
                        f"{node_type}.{endpoint.input} is already a connection slot.",
                    )
                )
            elif not capability.widget_convertible:
                issues.append(
                    _issue(
                        "unsupported_widget_conversion",
                        f"{path}.mode",
                        f"Input {capability.path!r} cannot be converted deterministically.",
                    )
                )
        return capability

    canonical_assertion_edges: list[GraphPatchEdge] = []
    assertion_edge_keys: set[tuple[Any, ...]] = set()
    for index, edge in enumerate(request.assertions.edges):
        path = f"assertions.edges[{index}]"
        key = _edge_key(edge)
        if key in assertion_edge_keys:
            issues.append(_issue("duplicate_edge_assertion", path, "Edge is asserted more than once."))
            continue
        assertion_edge_keys.add(key)
        baseline = baseline_edge_map.get(key)
        if baseline is None:
            issues.append(_issue("asserted_edge_missing", path, "Asserted edge is absent from the baseline graph."))
            canonical_assertion_edges.append(edge)
            continue
        if edge.target.mode != baseline.target.mode:
            issues.append(
                _issue(
                    "asserted_edge_mode_mismatch",
                    f"{path}.target.mode",
                    "A baseline edge assertion must use its exact active slot mode.",
                )
            )
        validate_source(edge.source, f"{path}.source")
        validate_target(
            edge.target,
            f"{path}.target",
            state="current",
            allow_captured_dynamic_socket=True,
        )
        canonical_assertion_edges.append(baseline)

    canonical_remove_edges: list[GraphPatchEdge] = []
    remove_edge_keys: set[tuple[Any, ...]] = set()
    for index, edge in enumerate(request.remove_edges):
        path = f"remove_edges[{index}]"
        key = _edge_key(edge)
        if key in remove_edge_keys:
            issues.append(_issue("duplicate_remove_edge", path, "Edge is removed more than once."))
            continue
        remove_edge_keys.add(key)
        if isinstance(edge.source.ref, NewNodeRef) or isinstance(edge.target.ref, NewNodeRef):
            issues.append(_issue("remove_edge_new_ref", path, "A baseline edge cannot reference a new alias."))
        baseline = baseline_edge_map.get(key)
        if baseline is None:
            issues.append(_issue("remove_edge_missing", path, "Removed edge is absent from the baseline graph."))
        if key not in assertion_edge_keys:
            issues.append(_issue("missing_edge_assertion", path, "Removed edge lacks an exact baseline assertion."))
        validate_source(edge.source, f"{path}.source")
        validate_target(
            edge.target,
            f"{path}.target",
            state="current",
            allow_captured_dynamic_socket=True,
        )
        if baseline is not None and edge.target.mode != baseline.target.mode:
            issues.append(
                _issue(
                    "remove_edge_mode_mismatch",
                    f"{path}.target.mode",
                    "A removed baseline edge must use its exact active slot mode.",
                )
            )
        canonical_remove_edges.append(baseline or edge)

    for removal in canonical_removes:
        for edge in removal.expected_incident_edges:
            if _edge_key(edge) not in remove_edge_keys:
                issues.append(
                    _issue(
                        "incident_edge_not_removed",
                        "remove_nodes.expected_incident_edges",
                        "Every declared incident edge must also appear in remove_edges.",
                    )
                )

    canonical_add_edges: list[GraphPatchEdge] = []
    add_edge_keys: set[tuple[Any, ...]] = set()
    type_bindings: dict[str, str] = {}
    retained_edges = [
        edge for edge in baseline_edges if _edge_key(edge) not in remove_edge_keys
    ]

    def matchtype_key(
        ref: GraphPatchNodeRef,
        template_id: str | None,
    ) -> str | None:
        return f"{_ref_path(ref)}:{template_id}" if template_id else None

    def seed_type_binding(key: str | None, concrete_type: str, path: str) -> None:
        if key is None:
            return
        previous = type_bindings.get(key)
        if previous is not None and previous != concrete_type:
            issues.append(
                _issue(
                    "matchtype_binding_conflict",
                    path,
                    f"MATCHTYPE variable {key!r} is already bound to {previous!r}, "
                    f"not {concrete_type!r}.",
                )
            )
            return
        type_bindings[key] = concrete_type

    # Retained exact edges are part of the same final graph as new edges.  Seed
    # instance-scoped MATCHTYPE variables from every loaded touched endpoint so
    # an added edge cannot bind the other socket of an existing polymorphic node
    # to an incompatible type.  Also prove retained inputs remain active after
    # a declared selector update; otherwise the callback would silently drop a
    # link and force a visible frontend rollback.
    for index, edge in enumerate(retained_edges):
        source_strict = (
            isinstance(edge.source.ref, ExistingNodeRef)
            and _id_key(edge.source.ref.node_id) in strict_baseline_node_keys
        )
        target_strict = (
            isinstance(edge.target.ref, ExistingNodeRef)
            and _id_key(edge.target.ref.node_id) in strict_baseline_node_keys
        )
        if not source_strict and not target_strict:
            continue
        source_capability = (
            validate_source(edge.source, f"retained_edges[{index}].source")
            if source_strict
            else None
        )
        target_capability = (
            validate_target(
                edge.target,
                f"retained_edges[{index}].target",
                allow_captured_dynamic_socket=True,
            )
            if target_strict
            else None
        )
        if (
            target_strict
            and isinstance(edge.target.ref, ExistingNodeRef)
            and _id_key(edge.target.ref.node_id) in update_keys
            and target_capability is None
        ):
            issues.append(
                _issue(
                    "retained_edge_inactive_after_update",
                    f"retained_edges[{index}].target",
                    "An existing edge targets an input that is inactive after the "
                    "declared node update; remove or reconnect that edge explicitly.",
                )
            )
        if source_capability is not None:
            seed_type_binding(
                matchtype_key(edge.source.ref, source_capability.matchtype_template_id),
                edge.source.type,
                f"retained_edges[{index}].source",
            )
        if target_capability is not None:
            seed_type_binding(
                matchtype_key(
                    edge.target.ref,
                    target_capability.matchtype.template_id
                    if target_capability.matchtype is not None
                    else None,
                ),
                edge.target.type,
                f"retained_edges[{index}].target",
            )

    def validate_connection(edge: GraphPatchEdge, path: str) -> None:
        if edge.source.type != edge.target.type:
            issues.append(
                _issue(
                    "incompatible_edge_types",
                    path,
                    f"Edge source type {edge.source.type!r} does not match target "
                    f"type {edge.target.type!r}.",
                )
            )
        source_capability = validate_source(edge.source, f"{path}.source")
        target_capability = validate_target(edge.target, f"{path}.target")
        if source_capability is None or target_capability is None:
            return
        source_key = matchtype_key(
            edge.source.ref,
            source_capability.matchtype_template_id,
        )
        target_key = matchtype_key(
            edge.target.ref,
            target_capability.matchtype.template_id
            if target_capability.matchtype is not None
            else None,
        )
        if source_key is not None and source_key not in type_bindings:
            type_bindings[source_key] = edge.source.type
        if target_key is not None and target_key not in type_bindings:
            type_bindings[target_key] = edge.target.type
        compatibility = classify_connection(
            source_capability,
            target_capability,
            type_bindings=type_bindings,
            source_binding_key=source_key,
            target_binding_key=target_key,
        )
        type_bindings.clear()
        type_bindings.update(compatibility.type_bindings)
        remaining_reasons = [
            reason
            for reason in compatibility.reasons
            if not (
                (
                    edge.target.mode == "convert_widget"
                    or is_existing_converted_slot(edge.target, target_capability)
                )
                and reason.code == "widget_conversion_required"
            )
        ]
        unsupported = [reason for reason in remaining_reasons if reason.status == "unsupported"]
        adapters = [reason for reason in remaining_reasons if reason.status == "adapter_required"]
        if unsupported:
            issues.append(
                _issue(
                    "incompatible_edge_types",
                    path,
                    "; ".join(reason.message for reason in unsupported),
                )
            )
        elif adapters:
            issues.append(
                _issue(
                    "connection_adapter_required",
                    path,
                    "; ".join(
                        f"{reason.code}: {reason.message}" for reason in adapters
                    ),
                )
            )

    for index, edge in enumerate(request.add_edges):
        path = f"add_edges[{index}]"
        key = _edge_key(edge)
        if key in add_edge_keys:
            issues.append(_issue("duplicate_add_edge", path, "Edge is added more than once."))
            continue
        add_edge_keys.add(key)
        if key in baseline_edge_map and key not in remove_edge_keys:
            issues.append(_issue("add_edge_already_exists", path, "Added edge already exists in the baseline graph."))
        if key in remove_edge_keys:
            issues.append(_issue("remove_add_same_edge", path, "The same exact edge cannot be removed and re-added."))
        if _ref_key(edge.source.ref) == _ref_key(edge.target.ref):
            issues.append(_issue("self_loop", path, "GraphPatch edges cannot be self-loops."))
        for endpoint_path, ref in (("source", edge.source.ref), ("target", edge.target.ref)):
            if isinstance(ref, ExistingNodeRef) and _id_key(ref.node_id) in remove_node_keys:
                issues.append(
                    _issue(
                        "edge_references_removed_node",
                        f"{path}.{endpoint_path}.ref",
                        "An added edge cannot reference a node removed by the same patch.",
                    )
                )
        validate_connection(edge, path)
        canonical_add_edges.append(edge)

    occupied_targets: dict[tuple[Any, ...], GraphPatchEdge] = {}
    for edge in [*retained_edges, *canonical_add_edges]:
        target_key = (
            _ref_key(edge.target.ref),
            edge.target.input_index,
            edge.target.occurrence_index,
            edge.target.input,
        )
        previous = occupied_targets.get(target_key)
        if previous is not None and _edge_key(previous) != _edge_key(edge):
            issues.append(
                _issue(
                    "occupied_input",
                    "add_edges",
                    f"Input {_ref_path(edge.target.ref)}.{edge.target.input} has more than one incoming edge.",
                )
            )
        occupied_targets[target_key] = edge

    canonical_attachments: list[GraphPatchAttachment] = []
    attachment_targets: set[tuple[Any, ...]] = set()
    for index, attachment in enumerate(request.attachments):
        path = f"attachments[{index}]"
        if (
            isinstance(attachment.ref, ExistingNodeRef)
            and _id_key(attachment.ref.node_id) in remove_node_keys
        ):
            issues.append(
                _issue(
                    "attachment_references_removed_node",
                    f"{path}.ref",
                    "An attachment cannot target a node removed by the same patch.",
                )
            )
        target_key = (_ref_key(attachment.ref), attachment.input_index, 0, attachment.input)
        if target_key in attachment_targets:
            issues.append(_issue("duplicate_attachment_target", path, "Input has more than one attachment."))
            continue
        attachment_targets.add(target_key)
        if target_key in occupied_targets:
            issues.append(_issue("attachment_edge_conflict", path, "Input has both an attachment and an edge."))
        node_type, raw_info, _ = resolve_ref(attachment.ref, path=f"{path}.ref")
        if raw_info is not None and node_type is not None:
            capabilities = capabilities_for(node_type, raw_info)
            matches = [
                capability
                for capability in capabilities.inputs
                if not capability.hidden
                and capability.path == attachment.input
                and capability.declaration_index == attachment.input_index
                and _type_is_allowed(capability.accepted_types, attachment.type)
            ]
            slot = matches[0] if len(matches) == 1 else None
            if slot is None:
                issues.append(_issue("attachment_slot_mismatch", path, "Attachment target facts do not match the schema."))
            elif not _is_image_upload_widget(slot):
                issues.append(
                    _issue(
                        "attachment_target_not_image_upload",
                        path,
                        "Attachments require a LoadImage-style image choice/upload widget.",
                    )
                )
        canonical_attachments.append(attachment)

    final_node_refs: list[GraphPatchNodeRef] = [
        ExistingNodeRef(node_id=node.node_id)
        for node in request.graph.nodes
        if _id_key(node.node_id) not in remove_node_keys
    ] + [NewNodeRef(alias=node.alias) for node in canonical_creates]
    final_node_keys = {_ref_key(ref) for ref in final_node_refs}
    final_edges = sorted([*retained_edges, *canonical_add_edges], key=_edge_sort_key)
    for edge in final_edges:
        if _ref_key(edge.source.ref) not in final_node_keys or _ref_key(edge.target.ref) not in final_node_keys:
            issues.append(
                _issue(
                    "dangling_final_edge",
                    "expected_final.edges",
                    "The expected final graph contains an edge to a missing node.",
                )
            )
    if _has_cycle(final_node_keys, final_edges):
        issues.append(_issue("graph_cycle", "add_edges", "The expected final graph contains a cycle."))

    expected_delta = GraphPatchExpectedDelta(
        created_node_count=len(canonical_creates),
        updated_node_count=len(canonical_updates),
        removed_node_count=len(canonical_removes),
        added_edge_count=len(canonical_add_edges),
        removed_edge_count=len(canonical_remove_edges),
        final_node_count=len(final_node_refs),
        final_edge_count=len(final_edges),
    )

    canonical_plan = GraphPatchPlan(
        expected_workflow_identity=request.expected_workflow_identity,
        expected_graph_hash=request.expected_graph_hash,
        assertions=GraphPatchAssertions(
            nodes=sorted(canonical_assertions, key=lambda item: _id_key(item.ref.node_id)),
            edges=sorted(canonical_assertion_edges, key=_edge_sort_key),
        ),
        create_nodes=sorted(canonical_creates, key=lambda item: item.alias),
        update_nodes=sorted(canonical_updates, key=lambda item: _id_key(item.ref.node_id)),
        remove_edges=sorted(canonical_remove_edges, key=_edge_sort_key),
        add_edges=sorted(canonical_add_edges, key=_edge_sort_key),
        remove_nodes=sorted(canonical_removes, key=lambda item: _id_key(item.ref.node_id)),
        attachments=sorted(
            canonical_attachments,
            key=lambda item: (_ref_key(item.ref), item.input_index, item.input),
        ),
        expected_delta=expected_delta,
    )
    issues = _deduplicate_issues(issues)
    if issues:
        return {
            "valid": False,
            "schema": GRAPH_PATCH_SCHEMA,
            "patch_hash": None,
            "patch_hash_schema": GRAPH_PATCH_HASH_SCHEMA,
            "plan": None,
            "apply_request": None,
            "expected_final": None,
            "catalog": {
                "state": "pinned" if request.expected_catalog_hash == catalog_hash else "changed",
                "source": source,
                "catalog_hash": catalog_hash,
                "node_count": len(catalog),
            },
            "issues": issues,
            "error_count": len(issues),
        }

    patch_hash = graph_patch_hash(canonical_plan, catalog_hash)
    apply_request = ApplyGraphPatchRequest(
        application_id=request.application_id,
        expected_catalog_hash=catalog_hash,
        patch_hash=patch_hash,
        plan=canonical_plan,
    )
    return {
        "valid": True,
        "schema": GRAPH_PATCH_SCHEMA,
        "patch_hash": patch_hash,
        "patch_hash_schema": GRAPH_PATCH_HASH_SCHEMA,
        "plan": canonical_plan.model_dump(mode="json"),
        "apply_request": apply_request.model_dump(mode="json"),
        "expected_final": {
            "nodes": [
                ref.model_dump(mode="json")
                for ref in sorted(final_node_refs, key=_node_sort_key)
            ],
            "edges": [edge.model_dump(mode="json") for edge in final_edges],
        },
        "catalog": {
            "state": "pinned",
            "source": source,
            "catalog_hash": catalog_hash,
            "node_count": len(catalog),
        },
        "issues": [],
        "error_count": 0,
    }


def lower_legacy_append_plan(
    *,
    application_id: str,
    expected_catalog_hash: str,
    expected_workflow_identity: str,
    expected_graph_hash: str,
    graph: NormalizedGraphSnapshot,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    terminal_source: GraphPatchSourceEndpoint,
    create_nodes: list[GraphPatchCreateNode],
    sequential_edges: list[GraphPatchEdge],
    retained_side_edges: list[GraphPatchEdge] | None = None,
    source: str = "",
) -> dict[str, Any]:
    """Lower a clean legacy terminal append into the general GraphPatch kernel.

    This helper intentionally handles only creation plus edge addition.  Callers
    keep the existing refinement compiler for insert/replace/delete until their
    exact removal assertions are available.
    """

    add_edges = [*sequential_edges, *(retained_side_edges or [])]
    assertions: dict[tuple[str, str], GraphPatchNodeAssertion] = {}
    for endpoint in [terminal_source, *[edge.source for edge in add_edges]]:
        if not isinstance(endpoint.ref, ExistingNodeRef):
            continue
        graph_node = next(
            (node for node in graph.nodes if _id_key(node.node_id) == _id_key(endpoint.ref.node_id)),
            None,
        )
        if graph_node is None:
            continue
        raw_info = catalog.get(graph_node.node_type)
        if not isinstance(raw_info, Mapping):
            continue
        assertions[_id_key(endpoint.ref.node_id)] = GraphPatchNodeAssertion(
            ref=endpoint.ref,
            node_type=graph_node.node_type,
            schema_hash=node_schema_hash(graph_node.node_type, raw_info),
        )
    request = PlanGraphPatchRequest(
        application_id=application_id,
        expected_workflow_identity=expected_workflow_identity,
        expected_graph_hash=expected_graph_hash,
        expected_catalog_hash=expected_catalog_hash,
        graph=graph,
        assertions={"nodes": list(assertions.values()), "edges": []},
        create_nodes=create_nodes,
        add_edges=add_edges,
    )
    return compile_graph_patch(
        request,
        catalog,
        catalog_hash=catalog_hash,
        source=source,
    )


__all__ = [
    "ApplyGraphPatchRequest",
    "ExistingNodeRef",
    "GRAPH_PATCH_HASH_SCHEMA",
    "GRAPH_PATCH_SCHEMA",
    "GraphPatchAssertions",
    "GraphPatchAttachment",
    "GraphPatchCreateNode",
    "GraphPatchEdge",
    "GraphPatchExpectedDelta",
    "GraphPatchLayoutHint",
    "GraphPatchNodeAssertion",
    "GraphPatchNodeRef",
    "GraphPatchPlan",
    "GraphPatchRemoveNode",
    "GraphPatchRequest",
    "GraphPatchSourceEndpoint",
    "GraphPatchTargetEndpoint",
    "GraphPatchUpdateNode",
    "MAX_GRAPH_PATCH_ATTACHMENTS",
    "MAX_GRAPH_PATCH_ATTACHMENT_BYTES",
    "NewNodeRef",
    "PlanGraphPatchRequest",
    "compile_graph_patch",
    "graph_patch_request_from_apply",
    "graph_patch_hash",
    "lower_legacy_append_plan",
]
