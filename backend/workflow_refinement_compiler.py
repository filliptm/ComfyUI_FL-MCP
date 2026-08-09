"""One-pass semantic compilation for deterministic workflow graph patches."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from chat_images import ChatImageReference
from node_library import node_schema_hash
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)
from workflow_capability_graph import (
    CapabilityGraph,
    ConversionRoute,
    RouteEndpoint,
    RoutePolicy,
    RouteStep,
    TransformOutputPort,
    TransformProfile,
    VerifiedCapabilityLesson,
    build_capability_graph,
)
from workflow_compiler import (
    WorkflowSpecNode,
    _canonicalize_dynamic_selector_aliases,
    _canonicalize_node_values,
    _partner_review,
    _resolve_runtime_name,
)
from workflow_graph_patch import (
    MAX_GRAPH_PATCH_ATTACHMENTS,
    ExistingNodeRef,
    GraphPatchAttachment,
    GraphPatchCreateNode,
    GraphPatchEdge,
    GraphPatchLayoutHint,
    GraphPatchNodeAssertion,
    GraphPatchRemoveNode,
    GraphPatchRequest,
    GraphPatchSourceEndpoint,
    GraphPatchTargetEndpoint,
    GraphPatchUpdateNode,
    NewNodeRef,
    SlotIndex,
    _baseline_edge,
    compile_graph_patch,
)
from workflow_refinement import NormalizedGraphSnapshot, normalize_workflow_graph
from workflow_resolver import (
    ResolveWorkflowSpecRequest,
    WorkflowCapabilitySpec,
    resolve_workflow_spec,
)
from workflow_schema_capabilities import (
    InputCapability,
    OutputCapability,
    classify_connection,
    infer_dynamic_selector_values,
    materialize_inputs,
    normalize_node_schema,
)

WORKFLOW_REFINEMENT_COMPILER_SCHEMA = "fl-mcp.workflow-refinement-compiler.v2"
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SEMANTIC_WORD = re.compile(r"[a-z0-9]+")
_ENDPOINT_CANDIDATE_LIMIT = 8
_AUTOGROW_ORDINAL_WORDS = {
    "first": 0,
    "second": 1,
    "third": 2,
    "fourth": 3,
    "fifth": 4,
    "sixth": 5,
    "seventh": 6,
    "eighth": 7,
    "ninth": 8,
    "tenth": 9,
}
NodeId: TypeAlias = StrictInt | StrictStr


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    severity: Literal["error", "warning"] = "error",
) -> dict[str, str]:
    return {"severity": severity, "code": code, "path": path, "message": message}


def _compact_output_candidates(slots: Sequence[OutputCapability]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "output_index": item.index,
            "types": list(item.produced_types),
        }
        for item in slots
    ]


def _autogrow_constraint(item: InputCapability) -> Any | None:
    return next(
        (constraint for constraint in item.activation if constraint.kind == "autogrow_slot"),
        None,
    )


def _compact_input_candidate(item: Any) -> dict[str, Any]:
    capability = item.capability
    autogrow = _autogrow_constraint(capability)
    candidate: dict[str, Any] = {
        "path": capability.path,
        "name": capability.name,
        "input_index": capability.declaration_index,
        "occurrence_index": capability.occurrence_index,
        "socket_index": item.socket_index,
        "types": list(capability.accepted_types),
        "mode": "slot" if capability.connectable else "convert_widget",
    }
    if autogrow is not None:
        candidate["dynamic_group"] = autogrow.source
        candidate["ordinal"] = autogrow.ordinal
    return candidate


def _issues_with_endpoint_candidates(
    issues: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    compact = [dict(item) for item in candidates[:_ENDPOINT_CANDIDATE_LIMIT]]
    return [
        {
            **item,
            "candidate_count": len(candidates),
            "candidates": compact,
        }
        for item in issues
    ]


def _semantic_words(value: str) -> tuple[str, ...]:
    ignored = {"input", "port", "slot", "socket", "source", "target"}
    result: list[str] = []
    for token in _SEMANTIC_WORD.findall(value.casefold()):
        if token.isdigit() or token in ignored:
            continue
        singular = token[:-1] if token.endswith("s") and len(token) > 3 else token
        if singular not in ignored:
            result.append(singular)
    return tuple(result)


def _semantic_autogrow_request(
    value: str,
) -> tuple[set[str], int | None, bool]:
    """Extract group terms and optional zero-based ordinal from natural intent."""

    tokens = _SEMANTIC_WORD.findall(value.casefold())
    choose_next_available = "available" in tokens or "next" in tokens
    ordinals: set[int] = set()
    ordinal_tokens: set[str] = set()
    for token in tokens:
        ordinal = _AUTOGROW_ORDINAL_WORDS.get(token)
        if ordinal is None:
            match = re.fullmatch(r"([1-9][0-9]*)(?:st|nd|rd|th)", token)
            if match is not None:
                ordinal = int(match.group(1)) - 1
        if ordinal is not None:
            ordinals.add(ordinal)
            ordinal_tokens.add(token)
    if choose_next_available:
        requested_ordinal = None
    elif len(ordinals) == 1:
        requested_ordinal = next(iter(ordinals))
    else:
        requested_ordinal = None

    modifiers = {
        "active",
        "available",
        "dynamic",
        "next",
        *ordinal_tokens,
    }
    requested_words = set(_semantic_words(" ".join(
        token for token in tokens if token not in modifiers
    )))
    return requested_words, requested_ordinal, len(ordinals) > 1 and not choose_next_available


def _types_overlap(left: Collection[str], right: Collection[str]) -> bool:
    return "*" in left or "*" in right or bool(set(left) & set(right))


def _semantic_autogrow_target(
    requested: str,
    materialized_inputs: Sequence[Any],
    *,
    source_types: Collection[str],
    occupied_inputs: Collection[str],
    path: str,
) -> tuple[Any | None, list[dict[str, Any]]]:
    """Resolve one semantic input phrase to the first unused AUTOGROW slot.

    Exact runtime names are resolved before this helper.  This fallback is
    intentionally narrow: only active AUTOGROW groups whose loaded types can
    accept the source are eligible, and more than one matching group requires
    an explicit choice.
    """

    requested_words, requested_ordinal, ordinal_ambiguous = (
        _semantic_autogrow_request(requested)
    )
    if not requested_words or not source_types:
        return None, []
    by_group: dict[str, list[Any]] = {}
    for item in materialized_inputs:
        capability = item.capability
        constraint = _autogrow_constraint(capability)
        if (
            capability.hidden
            or constraint is None
            or not capability.connectable
            or not _types_overlap(source_types, capability.accepted_types)
        ):
            continue
        by_group.setdefault(constraint.source, []).append(item)

    ranked_groups: list[tuple[int, str, list[Any]]] = []
    source_type_words = {value.casefold() for value in source_types}
    for group, items in by_group.items():
        group_words = set(_semantic_words(group))
        slot_words = {
            word
            for item in items
            for word in _semantic_words(item.capability.name)
        }
        if requested_words <= group_words:
            rank = 3 if requested_words == group_words else 2
        elif requested_words <= slot_words:
            rank = 2
        elif len(requested_words) == 1 and requested_words <= source_type_words:
            rank = 1
        else:
            continue
        ranked_groups.append((rank, group, items))

    if not ranked_groups:
        return None, []
    best_rank = max(item[0] for item in ranked_groups)
    best_groups = [item for item in ranked_groups if item[0] == best_rank]
    best_groups.sort(key=lambda item: (item[1].casefold(), item[1]))
    available_by_group = [
        (
            rank,
            group,
            sorted(
                (
                    item
                    for item in items
                    if (
                        requested_ordinal is None
                        or _autogrow_constraint(item.capability).ordinal
                        == requested_ordinal
                    )
                    if item.capability.path not in occupied_inputs
                ),
                key=lambda item: (
                    _autogrow_constraint(item.capability).ordinal,
                    item.capability.declaration_index,
                    item.capability.path,
                ),
            ),
        )
        for rank, group, items in best_groups
    ]
    ordinal_slots = [
        item
        for _, _, items in best_groups
        for item in items
        if requested_ordinal is None
        or _autogrow_constraint(item.capability).ordinal == requested_ordinal
    ]
    if ordinal_ambiguous:
        candidates = [
            _compact_input_candidate(item)
            for item in sorted(
                ordinal_slots,
                key=lambda entry: (
                    _autogrow_constraint(entry.capability).ordinal,
                    entry.capability.declaration_index,
                    entry.capability.path,
                ),
            )
        ]
        return None, [
            {
                **_issue(
                    "ambiguous_semantic_input_ordinal",
                    f"{path}.target_input",
                    f"{requested!r} names multiple dynamic input ordinals.",
                ),
                "candidate_count": len(candidates),
                "candidates": candidates[:_ENDPOINT_CANDIDATE_LIMIT],
            }
        ]
    if requested_ordinal is not None and not ordinal_slots:
        candidates = [
            _compact_input_candidate(item)
            for _, _, items in best_groups
            for item in sorted(
                items,
                key=lambda entry: (
                    _autogrow_constraint(entry.capability).ordinal,
                    entry.capability.declaration_index,
                    entry.capability.path,
                ),
            )
        ]
        return None, [
            {
                **_issue(
                    "autogrow_input_ordinal_unavailable",
                    f"{path}.target_input",
                    f"The requested dynamic input ordinal in {requested!r} is not available.",
                ),
                "candidate_count": len(candidates),
                "candidates": candidates[:_ENDPOINT_CANDIDATE_LIMIT],
            }
        ]
    available_groups = [item for item in available_by_group if item[2]]
    if len(available_groups) == 1:
        return available_groups[0][2][0], []

    candidates = [
        _compact_input_candidate(items[0])
        for _, _, items in available_groups
        if items
    ]
    if not available_groups:
        exhausted = [
            item
            for _, _, items in best_groups
            for item in sorted(
                items,
                key=lambda entry: (
                    _autogrow_constraint(entry.capability).ordinal,
                    entry.capability.declaration_index,
                ),
            )[:1]
        ]
        candidates = [_compact_input_candidate(item) for item in exhausted]
        return None, [
            {
                **_issue(
                    "autogrow_input_exhausted",
                    f"{path}.target_input",
                    f"All compatible dynamic inputs matching {requested!r} are occupied.",
                ),
                "candidate_count": len(candidates),
                "candidates": candidates[:_ENDPOINT_CANDIDATE_LIMIT],
            }
        ]
    return None, [
        {
            **_issue(
                "ambiguous_semantic_input_slot",
                f"{path}.target_input",
                f"{requested!r} matches multiple compatible dynamic input groups.",
            ),
            "candidate_count": len(candidates),
            "candidates": candidates[:_ENDPOINT_CANDIDATE_LIMIT],
        }
    ]


def _typed_id_key(value: NodeId) -> tuple[str, Any]:
    return type(value).__name__, value


def _alternate_numeric_id(value: NodeId) -> NodeId | None:
    """Return the other canonical JSON representation of a numeric node ID.

    ComfyUI can expose a newly created LiteGraph ID as ``"12"`` in the apply
    result and serialize that same canvas node as ``12`` on the next graph
    read.  Exact typed identity remains authoritative; this alternate is used
    only when the exact representation has no candidate.
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        if str(parsed) == value:
            return parsed
    return None


class ExistingNodeSelector(BaseModel):
    """Resolve one semantic role to an exact node in the active workflow."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, max_length=64)
    selected: StrictBool = False
    node_id: NodeId | None = None
    node_type: str | None = Field(None, min_length=1, max_length=256)
    workflow_alias: str | None = Field(None, min_length=1, max_length=128)
    title_contains: str | None = Field(None, min_length=1, max_length=256)
    value_contains: str | None = Field(None, min_length=1, max_length=512)
    topology: Literal["any", "source", "sink"] = "any"
    occurrence: Literal["only", "first", "last"] = "only"

    @model_validator(mode="after")
    def validate_selector(self) -> ExistingNodeSelector:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError("alias must be a lowercase semantic identifier")
        if not any(
            (
                self.node_id is not None,
                self.selected,
                self.node_type,
                self.workflow_alias,
                self.title_contains,
                self.value_contains,
                self.topology != "any",
            )
        ):
            raise ValueError("an existing-node selector needs at least one match criterion")
        return self


class RefinementSpecEdge(BaseModel):
    """One desired edge between semantic existing/new aliases."""

    model_config = ConfigDict(extra="forbid")

    source_alias: str = Field(..., min_length=1, max_length=64)
    source_output: str = Field(..., min_length=1, max_length=256)
    source_output_index: SlotIndex | None = None
    target_alias: str = Field(..., min_length=1, max_length=64)
    target_input: str = Field(..., min_length=1, max_length=256)
    target_mode: Literal["auto", "slot", "convert_widget"] = "auto"


class RelativeLayoutDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: StrictInt | StrictFloat
    y: StrictInt | StrictFloat

    @model_validator(mode="after")
    def validate_delta(self) -> RelativeLayoutDelta:
        if not all(math.isfinite(float(value)) for value in (self.x, self.y)):
            raise ValueError("relative layout deltas must be finite")
        if abs(float(self.x)) > 100_000 or abs(float(self.y)) > 100_000:
            raise ValueError("relative layout deltas must not exceed 100000")
        return self


class RefinementSpecUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alias: str = Field(..., min_length=1, max_length=64)
    expected_values: dict[str, Any] = Field(default_factory=dict)
    set_values: dict[str, Any] = Field(default_factory=dict)
    layout_hint: GraphPatchLayoutHint | None = None
    move_by: RelativeLayoutDelta | None = None

    @model_validator(mode="after")
    def validate_layout_mode(self) -> RefinementSpecUpdate:
        if self.layout_hint is not None and self.move_by is not None:
            raise ValueError("layout_hint and move_by are mutually exclusive")
        if not self.set_values and self.layout_hint is None and self.move_by is None:
            raise ValueError("update needs set_values, layout_hint, or move_by")
        return self


class RefinementSpecAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alias: str = Field(..., min_length=1, max_length=64)
    target_input: str = Field("image", min_length=1, max_length=256)
    image: ChatImageReference


class CompileWorkflowRefinementSpecRequest(BaseModel):
    """Describe one complete semantic build or edit of the current workflow."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    existing_nodes: list[ExistingNodeSelector] = Field(default_factory=list, max_length=100)
    create_nodes: list[WorkflowSpecNode] = Field(default_factory=list, max_length=100)
    add_edges: list[RefinementSpecEdge] = Field(default_factory=list, max_length=2_000)
    remove_edges: list[RefinementSpecEdge] = Field(default_factory=list, max_length=2_000)
    update_nodes: list[RefinementSpecUpdate] = Field(default_factory=list, max_length=100)
    remove_nodes: list[str] = Field(default_factory=list, max_length=100)
    attachments: list[RefinementSpecAttachment] = Field(
        default_factory=list,
        max_length=MAX_GRAPH_PATCH_ATTACHMENTS,
    )
    allow_inferred_converters: StrictBool = True
    expected_graph_hash: str | None = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_catalog_hash: str | None = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_aliases_and_effect(self) -> CompileWorkflowRefinementSpecRequest:
        aliases = [item.alias for item in self.existing_nodes] + [
            item.alias for item in self.create_nodes
        ]
        if len(aliases) != len(set(aliases)):
            raise ValueError("existing and created semantic aliases must be unique")
        known = set(aliases)
        referenced = {
            alias
            for edge in [*self.add_edges, *self.remove_edges]
            for alias in (edge.source_alias, edge.target_alias)
        }
        referenced.update(item.target_alias for item in self.update_nodes)
        referenced.update(self.remove_nodes)
        referenced.update(item.target_alias for item in self.attachments)
        unknown = sorted(referenced - known)
        if unknown:
            raise ValueError("semantic references use unknown aliases: " + ", ".join(unknown))
        if not any(
            (
                self.create_nodes,
                self.add_edges,
                self.remove_edges,
                self.update_nodes,
                self.remove_nodes,
                self.attachments,
            )
        ):
            raise ValueError("refinement spec must contain at least one edit")
        payload = self.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_048_576:
            raise ValueError("refinement compiler request must not exceed 1 MiB")
        return self


def _semantic_workflow_alias(node: Mapping[str, Any]) -> str | None:
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return None
    for key in (
        "fl_mcp_workflow_graph_patch",
        "fl_mcp_workflow_refinement",
        "fl_mcp_workflow_application",
    ):
        metadata = properties.get(key)
        if isinstance(metadata, Mapping) and isinstance(metadata.get("alias"), str):
            return metadata["alias"]
    return None


def _searchable_node_text(node: Mapping[str, Any]) -> str:
    safe = {
        "title": node.get("title"),
        "properties": node.get("properties"),
        "widgets_values": node.get("widgets_values"),
    }
    return json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str).casefold()


def _raw_nodes(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = workflow.get("nodes")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, Mapping):
        return [item for item in raw.values() if isinstance(item, Mapping)]
    return []


def _node_order(node: Mapping[str, Any]) -> tuple[float, str, str]:
    order = node.get("order")
    numeric = float(order) if isinstance(order, (int, float)) and not isinstance(order, bool) else 0.0
    node_id = node.get("id")
    return numeric, type(node_id).__name__, str(node_id)


def _resolve_existing_selectors(
    selectors: Sequence[ExistingNodeSelector],
    workflow: Mapping[str, Any],
    graph: NormalizedGraphSnapshot,
    catalog: Mapping[str, Any],
    selected_node_ids: Sequence[NodeId] | None = None,
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    nodes = _raw_nodes(workflow)
    indegree = {_typed_id_key(node.node_id): 0 for node in graph.nodes}
    outdegree = {_typed_id_key(node.node_id): 0 for node in graph.nodes}
    for edge in graph.edges:
        outdegree[_typed_id_key(edge.source_node_id)] += 1
        indegree[_typed_id_key(edge.target_node_id)] += 1
    selected: dict[str, Mapping[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    selected_id_keys = {
        _typed_id_key(node_id) for node_id in (selected_node_ids or [])
    }
    claimed_ids: set[tuple[str, Any]] = set()
    for selector in selectors:
        def matching_candidates(
            expected_node_id: NodeId | None,
            active_selector: ExistingNodeSelector = selector,
        ) -> list[Mapping[str, Any]]:
            matches: list[Mapping[str, Any]] = []
            for node in nodes:
                node_id = node.get("id")
                node_type = (
                    node.get("type") or node.get("comfyClass") or node.get("class_type")
                )
                if isinstance(node_id, bool) or not isinstance(node_id, (int, str)):
                    continue
                key = _typed_id_key(node_id)
                if active_selector.selected and key not in selected_id_keys:
                    continue
                if expected_node_id is not None and key != _typed_id_key(expected_node_id):
                    continue
                if active_selector.node_type and node_type != active_selector.node_type:
                    continue
                if (
                    active_selector.workflow_alias
                    and _semantic_workflow_alias(node) != active_selector.workflow_alias
                ):
                    continue
                if active_selector.title_contains:
                    title = node.get("title")
                    if isinstance(title, str) and title.strip():
                        if active_selector.title_contains.casefold() not in title.casefold():
                            continue
                    else:
                        catalog_entry = catalog.get(node_type)
                        display_name = (
                            catalog_entry.get("display_name")
                            if isinstance(catalog_entry, Mapping)
                            else None
                        )
                        if not (
                            isinstance(display_name, str)
                            and active_selector.title_contains.casefold()
                            == display_name.casefold()
                        ):
                            continue
                if (
                    active_selector.value_contains
                    and active_selector.value_contains.casefold()
                    not in _searchable_node_text(node)
                ):
                    continue
                if active_selector.topology == "source" and indegree.get(key, 0) != 0:
                    continue
                if active_selector.topology == "sink" and outdegree.get(key, 0) != 0:
                    continue
                matches.append(node)
            return matches

        exact_node_id_present = bool(
            selector.node_id is not None
            and any(
                not isinstance(node.get("id"), bool)
                and isinstance(node.get("id"), (int, str))
                and _typed_id_key(node["id"]) == _typed_id_key(selector.node_id)
                for node in nodes
            )
        )
        candidates = matching_candidates(selector.node_id)
        if (
            not candidates
            and selector.node_id is not None
            and not exact_node_id_present
        ):
            alternate_id = _alternate_numeric_id(selector.node_id)
            if alternate_id is not None:
                fallback_candidates = matching_candidates(alternate_id)
                if len(fallback_candidates) == 1:
                    candidates = fallback_candidates
                elif len(fallback_candidates) > 1:
                    issues.append(
                        _issue(
                            "existing_selector_ambiguous",
                            f"existing_nodes.{selector.alias}",
                            "The numeric node-ID representation fallback matches multiple "
                            "nodes; add an exact alias, value, title, or topology criterion.",
                        )
                    )
                    evidence.append(
                        {
                            "alias": selector.alias,
                            "selected": None,
                            "candidates": [
                                {
                                    "node_id": item.get("id"),
                                    "node_type": item.get("type"),
                                    "workflow_alias": _semantic_workflow_alias(item),
                                    "title": item.get("title"),
                                }
                                for item in fallback_candidates[:10]
                            ],
                        }
                    )
                    continue
        candidates.sort(key=_node_order)
        if not candidates:
            issues.append(
                _issue(
                    "existing_selector_no_match",
                    f"existing_nodes.{selector.alias}",
                    "No active workflow node matches this deterministic selector.",
                )
            )
            continue
        if (selector.selected or selector.occurrence == "only") and len(candidates) != 1:
            issues.append(
                _issue(
                    "existing_selector_ambiguous",
                    f"existing_nodes.{selector.alias}",
                    f"Selector matches {len(candidates)} nodes; select exactly one node or add "
                    "an exact alias, value, ID, or occurrence.",
                )
            )
            evidence.append(
                {
                    "alias": selector.alias,
                    "selected": None,
                    "candidates": [
                        {
                            "node_id": item.get("id"),
                            "node_type": item.get("type"),
                            "workflow_alias": _semantic_workflow_alias(item),
                            "title": item.get("title"),
                        }
                        for item in candidates[:10]
                    ],
                }
            )
            continue
        chosen = candidates[0] if selector.occurrence == "first" else candidates[-1]
        chosen_key = _typed_id_key(chosen["id"])
        if chosen_key in claimed_ids:
            issues.append(
                _issue(
                    "existing_selector_duplicate_target",
                    f"existing_nodes.{selector.alias}",
                    f"Node {chosen['id']!r} was selected by more than one semantic alias.",
                )
            )
            continue
        claimed_ids.add(chosen_key)
        selected[selector.alias] = chosen
        evidence.append(
            {
                "alias": selector.alias,
                "selected": {
                    "node_id": chosen.get("id"),
                    "node_type": chosen.get("type"),
                    "workflow_alias": _semantic_workflow_alias(chosen),
                    "title": chosen.get("title"),
                },
                "candidate_count": len(candidates),
                "occurrence": selector.occurrence,
            }
        )
    return selected, evidence, issues


def _endpoint_ref(alias: str, existing: Mapping[str, Mapping[str, Any]]) -> ExistingNodeRef | NewNodeRef:
    node = existing.get(alias)
    return ExistingNodeRef(node_id=node["id"]) if node is not None else NewNodeRef(alias=alias)


def _node_type_for_alias(
    alias: str,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
) -> str | None:
    node = existing.get(alias)
    if node is not None:
        value = node.get("type") or node.get("comfyClass") or node.get("class_type")
        return str(value) if value else None
    return created_types.get(alias)


def _connected_input_names(raw: Mapping[str, Any]) -> set[str]:
    serialized = raw.get("inputs")
    if not isinstance(serialized, list):
        return set()
    return {
        str(item.get("name"))
        for item in serialized
        if isinstance(item, Mapping)
        and item.get("name")
        and item.get("link") is not None
    }


def _canonicalize_update_mapping(
    values: Mapping[str, Any],
    capabilities: Any,
    *,
    context_values: Mapping[str, Any],
    connected_inputs: set[str],
    path: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Resolve semantic widget names against one exact active selector branch."""

    materialized = materialize_inputs(
        capabilities,
        values=context_values,
        connected_inputs=connected_inputs,
    )
    available = [
        item.capability.path
        for item in materialized
        if item.capability.widget and not item.capability.hidden
    ]
    accepted: dict[str, Any] = {}
    issues: list[dict[str, str]] = []
    for requested, value in values.items():
        resolved, name_issues = _resolve_runtime_name(
            requested,
            available,
            path=f"{path}.{requested}",
            kind="widget",
            allow_semantic_widget_alias=True,
        )
        issues.extend(name_issues)
        if resolved is None:
            continue
        if resolved in accepted and accepted[resolved] != value:
            issues.append(
                _issue(
                    "conflicting_widget_aliases",
                    f"{path}.{requested}",
                    f"Multiple update names resolve to {resolved!r} with different values.",
                )
            )
            continue
        accepted[resolved] = value
    return accepted, issues


def _relative_layout_hint(
    item: RefinementSpecUpdate,
    raw: Mapping[str, Any],
    *,
    path: str,
) -> tuple[GraphPatchLayoutHint | None, list[dict[str, str]]]:
    if item.move_by is None:
        return item.layout_hint, []
    position = raw.get("pos")
    size = raw.get("size")
    if not (
        isinstance(position, (list, tuple))
        and len(position) >= 2
        and isinstance(size, (list, tuple))
        and len(size) >= 2
    ):
        return None, [
            _issue(
                "existing_layout_unavailable",
                f"{path}.move_by",
                "The selected node has no exact serialized position and size.",
            )
        ]
    geometry = [position[0], position[1], size[0], size[1]]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in geometry
    ):
        return None, [
            _issue(
                "existing_layout_invalid",
                f"{path}.move_by",
                "The selected node's serialized layout is not finite numeric geometry.",
            )
        ]
    x = position[0] + item.move_by.x
    y = position[1] + item.move_by.y
    if abs(float(x)) > 10_000_000 or abs(float(y)) > 10_000_000:
        return None, [
            _issue(
                "relative_layout_out_of_bounds",
                f"{path}.move_by",
                "The resulting absolute canvas position exceeds the safe bound.",
            )
        ]
    try:
        return (
            GraphPatchLayoutHint(
                x=x,
                y=y,
                width=size[0],
                height=size[1],
            ),
            [],
        )
    except ValueError as exc:
        return None, [
            _issue(
                "existing_layout_invalid",
                f"{path}.move_by",
                str(exc),
            )
        ]


def _canonicalize_existing_update(
    item: RefinementSpecUpdate,
    raw: Mapping[str, Any],
    node_info: Mapping[str, Any],
    *,
    current_connected_inputs: set[str],
    future_connected_inputs: set[str],
    path: str,
) -> tuple[
    GraphPatchUpdateNode | None,
    dict[str, Any],
    dict[str, Any],
    list[dict[str, str]],
]:
    """Pin current selectors, then resolve set-values in their future branch."""

    node_type = str(raw.get("type") or raw.get("comfyClass") or raw.get("class_type"))
    capabilities = normalize_node_schema(node_type, node_info)
    serialized_widget_values = raw.get("widgets_values", [])
    current_selectors = infer_dynamic_selector_values(
        capabilities,
        serialized_widget_values if isinstance(serialized_widget_values, list) else [],
        connected_inputs=current_connected_inputs,
    )
    selector_paths = [
        capability.path
        for capability in capabilities.inputs
        if capability.kind == "dynamic_selector" and not capability.hidden
    ]
    canonical_expected_values, expected_alias_issues = (
        _canonicalize_dynamic_selector_aliases(
            item.expected_values,
            node_info,
            path=f"{path}.expected_values",
        )
    )
    canonical_set_values, set_alias_issues = _canonicalize_dynamic_selector_aliases(
        item.set_values,
        node_info,
        path=f"{path}.set_values",
    )
    issues: list[dict[str, str]] = [
        *expected_alias_issues,
        *set_alias_issues,
    ]

    def selector_updates(
        values: Mapping[str, Any],
        *,
        value_path: str,
        require_current: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for requested, value in values.items():
            resolved, name_issues = _resolve_runtime_name(
                requested,
                selector_paths,
                path=f"{value_path}.{requested}",
                kind="selector",
            )
            if resolved is None:
                if any(issue["code"].startswith("ambiguous_") for issue in name_issues):
                    issues.extend(name_issues)
                continue
            if (
                require_current
                and resolved in current_selectors
                and current_selectors[resolved] != value
            ):
                issues.append(
                    _issue(
                        "expected_selector_value_mismatch",
                        f"{value_path}.{requested}",
                        f"Active selector {resolved!r} is {current_selectors[resolved]!r}, "
                        f"not {value!r}.",
                    )
                )
            result[resolved] = value
        return result

    expected_selectors = selector_updates(
        canonical_expected_values,
        value_path=f"{path}.expected_values",
        require_current=True,
    )
    expected_context = {**current_selectors, **expected_selectors}
    canonical_expected, expected_issues = _canonicalize_update_mapping(
        canonical_expected_values,
        capabilities,
        context_values=expected_context,
        connected_inputs=current_connected_inputs,
        path=f"{path}.expected_values",
    )
    issues.extend(expected_issues)
    # Selector values inferred from the exact serialized node are implicit safety
    # preconditions for every dynamic update, even when the user omitted them.
    canonical_expected = {**current_selectors, **canonical_expected}

    set_selectors = selector_updates(
        canonical_set_values,
        value_path=f"{path}.set_values",
        require_current=False,
    )
    future_context = {**current_selectors, **set_selectors}
    canonical_set, set_issues = _canonicalize_update_mapping(
        canonical_set_values,
        capabilities,
        context_values=future_context,
        connected_inputs=future_connected_inputs,
        path=f"{path}.set_values",
    )
    issues.extend(set_issues)
    values_after_update = {**current_selectors, **canonical_set}
    layout_hint, layout_issues = _relative_layout_hint(item, raw, path=path)
    issues.extend(layout_issues)
    if not canonical_set and layout_hint is None:
        return None, current_selectors, values_after_update, issues
    return (
        GraphPatchUpdateNode(
            ref={"node_id": raw["id"]},
            node_type=node_type,
            schema_hash=node_schema_hash(node_type, node_info),
            expected_values=canonical_expected,
            set_values=canonical_set,
            layout_hint=layout_hint,
        ),
        current_selectors,
        values_after_update,
        issues,
    )


def _source_endpoint(
    edge: RefinementSpecEdge,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    graph: NormalizedGraphSnapshot,
    catalog: Mapping[str, Any],
    path: str,
) -> tuple[GraphPatchSourceEndpoint | None, list[dict[str, str]]]:
    node_type = _node_type_for_alias(edge.source_alias, existing, created_types)
    node_info = catalog.get(node_type) if node_type else None
    issues: list[dict[str, str]] = []
    if not isinstance(node_info, Mapping):
        return None, [_issue("source_type_missing", path, "Source node type is not loaded.")]
    capabilities = normalize_node_schema(node_type, node_info)
    slots = capabilities.outputs
    resolved_name, name_issues = _resolve_runtime_name(
        edge.source_output,
        [item.name for item in slots],
        path=f"{path}.source_output",
        kind="output_slot",
    )
    requested_type: str | None = None
    if resolved_name is None and any(
        item.get("code") == "unknown_output_slot" for item in name_issues
    ):
        # A MATCHTYPE output's loaded name describes its operation (for example
        # ``resized``), not its eventual concrete Comfy type. Semantic callers
        # may therefore identify that output by an allowed concrete type. Keep
        # this fallback graph-generic and deterministic: it is valid only when
        # the requested type plus optional exact index selects one polymorphic
        # output slot.
        requested_token = edge.source_output.casefold()
        typed_matches = [
            (item, produced_type)
            for item in slots
            if item.matchtype_template_id is not None
            and (
                edge.source_output_index is None
                or item.index == edge.source_output_index
            )
            for produced_type in item.produced_types
            if produced_type.casefold() == requested_token
        ]
        if len(typed_matches) == 1:
            resolved_name = typed_matches[0][0].name
            requested_type = typed_matches[0][1]
            name_issues = []
    if resolved_name is None:
        issues.extend(
            _issues_with_endpoint_candidates(
                name_issues,
                _compact_output_candidates(slots),
            )
        )
        return None, issues
    issues.extend(name_issues)
    matches = [
        item
        for item in slots
        if resolved_name is not None
        and item.name == resolved_name
        and (edge.source_output_index is None or item.index == edge.source_output_index)
    ]
    if len(matches) != 1:
        issues.append(
            _issue(
                "source_output_ambiguous",
                f"{path}.source_output_index",
                "Source output needs one unique name/index match.",
            )
        )
        return None, issues
    slot = matches[0]
    ref = _endpoint_ref(edge.source_alias, existing)
    if isinstance(ref, ExistingNodeRef):
        active = [
            item
            for item in graph.outputs
            if _typed_id_key(item.node_id) == _typed_id_key(ref.node_id)
            and item.output_index == slot.index
            and item.output == slot.name
        ]
        if len(active) != 1:
            issues.append(
                _issue(
                    "active_source_output_mismatch",
                    path,
                    "The selected existing source does not expose this exact active output.",
                )
            )
            return None, issues
        placeholder_type = active[0].type
    else:
        placeholder_type = (
            requested_type
            or slot.produced_types[0]
            if len(slot.produced_types) == 1
            else requested_type or "UNRESOLVED"
        )
    return (
        GraphPatchSourceEndpoint(
            ref=ref,
            output_index=slot.index,
            output=slot.name,
            type=placeholder_type,
        ),
        issues,
    )


def _source_endpoint_types(
    edge: RefinementSpecEdge,
    endpoint: GraphPatchSourceEndpoint,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    catalog: Mapping[str, Any],
) -> frozenset[str]:
    if endpoint.type != "UNRESOLVED":
        return frozenset({endpoint.type})
    node_type = _node_type_for_alias(edge.source_alias, existing, created_types)
    node_info = catalog.get(node_type) if node_type else None
    if not isinstance(node_info, Mapping):
        return frozenset()
    outputs = normalize_node_schema(node_type, node_info).outputs
    return frozenset(
        produced_type
        for item in outputs
        if item.index == endpoint.output_index and item.name == endpoint.output
        for produced_type in item.produced_types
    )


def _source_endpoint_cardinality(
    edge: RefinementSpecEdge,
    endpoint: GraphPatchSourceEndpoint,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    catalog: Mapping[str, Any],
) -> str | None:
    node_type = _node_type_for_alias(edge.source_alias, existing, created_types)
    node_info = catalog.get(node_type) if node_type else None
    if not isinstance(node_info, Mapping):
        return None
    matches = [
        item
        for item in normalize_node_schema(node_type, node_info).outputs
        if item.index == endpoint.output_index and item.name == endpoint.output
    ]
    return matches[0].cardinality if len(matches) == 1 else None


def _target_endpoint_types(
    edge: RefinementSpecEdge,
    endpoint: GraphPatchTargetEndpoint,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    catalog: Mapping[str, Any],
) -> frozenset[str]:
    if endpoint.type != "UNRESOLVED":
        return frozenset({endpoint.type})
    node_type = _node_type_for_alias(edge.target_alias, existing, created_types)
    node_info = catalog.get(node_type) if node_type else None
    if not isinstance(node_info, Mapping):
        return frozenset()
    inputs = normalize_node_schema(node_type, node_info).inputs
    return frozenset(
        accepted_type
        for item in inputs
        if item.declaration_index == endpoint.input_index
        and item.occurrence_index == endpoint.occurrence_index
        and item.path == endpoint.input
        for accepted_type in item.accepted_types
    )


def _target_endpoint_cardinality(
    edge: RefinementSpecEdge,
    endpoint: GraphPatchTargetEndpoint,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    catalog: Mapping[str, Any],
) -> str | None:
    node_type = _node_type_for_alias(edge.target_alias, existing, created_types)
    node_info = catalog.get(node_type) if node_type else None
    if not isinstance(node_info, Mapping):
        return None
    matches = [
        item
        for item in normalize_node_schema(node_type, node_info).inputs
        if item.declaration_index == endpoint.input_index
        and item.occurrence_index == endpoint.occurrence_index
        and item.path == endpoint.input
    ]
    return matches[0].cardinality if len(matches) == 1 else None


@dataclass(frozen=True)
class _CardinalityEdgeFact:
    source_output_key: tuple[str, int, str]
    target_node_key: str
    target_input_cardinality: str


def _endpoint_node_key(ref: ExistingNodeRef | NewNodeRef) -> str:
    return ref.model_dump_json()


def _endpoint_output_key(
    ref: ExistingNodeRef | NewNodeRef,
    output_index: int,
    output: str,
) -> tuple[str, int, str]:
    return (_endpoint_node_key(ref), output_index, output)


def _removed_input_name(requested: str, actual: str) -> bool:
    return requested == actual or actual.endswith(f".{requested}")


def _effective_output_cardinalities(
    *,
    graph: NormalizedGraphSnapshot,
    workflow: Mapping[str, Any],
    catalog: Mapping[str, Any],
    created_types: Mapping[str, str],
    created_values: Mapping[str, Mapping[str, Any]],
    existing: Mapping[str, Mapping[str, Any]],
    existing_values: Mapping[str, Mapping[str, Any]],
    direct_edges: Sequence[
        tuple[
            RefinementSpecEdge,
            GraphPatchSourceEndpoint,
            GraphPatchTargetEndpoint,
            str,
        ]
    ],
    removed_connection_hints: Mapping[str, set[str]],
    removed_aliases: Collection[str],
) -> dict[tuple[str, int, str], str | None]:
    """Resolve effective Comfy output cardinality over the retained future graph.

    A list entering a scalar input makes Comfy map that node over the list, so
    every otherwise-scalar output becomes a list. This effect is monotonic and
    is propagated across retained baseline edges plus explicitly direct new
    edges. Unknown retained schema facts propagate as unknown and therefore
    fail converter inference closed only when they can influence its source.
    """

    graph_node_types = {
        _typed_id_key(item.node_id): item.node_type for item in graph.nodes
    }
    raw_nodes_by_id = {
        _typed_id_key(item["id"]): item
        for item in _raw_nodes(workflow)
        if "id" in item
    }
    existing_alias_by_id = {
        _typed_id_key(raw["id"]): alias
        for alias, raw in existing.items()
        if "id" in raw
    }
    removed_node_keys = {
        _endpoint_node_key(ExistingNodeRef(node_id=existing[alias]["id"]))
        for alias in removed_aliases
        if alias in existing and "id" in existing[alias]
    }
    removed_inputs_by_node: dict[str, set[str]] = {}
    for alias, names in removed_connection_hints.items():
        raw = existing.get(alias)
        if raw is not None and "id" in raw:
            removed_inputs_by_node.setdefault(
                _endpoint_node_key(ExistingNodeRef(node_id=raw["id"])),
                set(),
            ).update(names)

    declared_outputs: dict[tuple[str, int, str], str] = {}
    node_keys: set[str] = set()

    def add_outputs(
        ref: ExistingNodeRef | NewNodeRef,
        node_type: str,
    ) -> None:
        node_key = _endpoint_node_key(ref)
        node_keys.add(node_key)
        node_info = catalog.get(node_type)
        if not isinstance(node_info, Mapping):
            return
        for output in normalize_node_schema(node_type, node_info).outputs:
            declared_outputs[
                _endpoint_output_key(ref, output.index, output.name)
            ] = output.cardinality

    for node in graph.nodes:
        add_outputs(ExistingNodeRef(node_id=node.node_id), node.node_type)
    for alias, node_type in created_types.items():
        add_outputs(NewNodeRef(alias=alias), node_type)

    retained_graph_edges = []
    connected_inputs_by_target: dict[str, set[str]] = {}
    for edge in graph.edges:
        source_ref = ExistingNodeRef(node_id=edge.source_node_id)
        target_ref = ExistingNodeRef(node_id=edge.target_node_id)
        source_key = _endpoint_node_key(source_ref)
        target_key = _endpoint_node_key(target_ref)
        if source_key in removed_node_keys or target_key in removed_node_keys:
            continue
        removed_names = removed_inputs_by_node.get(target_key, set())
        if any(
            _removed_input_name(requested, edge.target_input)
            for requested in removed_names
        ):
            continue
        retained_graph_edges.append(edge)
        connected_inputs_by_target.setdefault(target_key, set()).add(
            edge.target_input
        )
    for _, _, target, _ in direct_edges:
        connected_inputs_by_target.setdefault(
            _endpoint_node_key(target.ref),
            set(),
        ).add(target.input)

    edge_facts: list[_CardinalityEdgeFact] = []
    unknown_targets: set[str] = set()
    for edge in retained_graph_edges:
        source_ref = ExistingNodeRef(node_id=edge.source_node_id)
        target_ref = ExistingNodeRef(node_id=edge.target_node_id)
        source_output_key = _endpoint_output_key(
            source_ref,
            edge.source_output_index,
            edge.source_output,
        )
        target_node_key = _endpoint_node_key(target_ref)
        target_node_type = graph_node_types.get(_typed_id_key(edge.target_node_id))
        target_info = catalog.get(target_node_type) if target_node_type else None
        if (
            source_output_key not in declared_outputs
            or not isinstance(target_info, Mapping)
        ):
            unknown_targets.add(target_node_key)
            continue
        target_capabilities = normalize_node_schema(target_node_type, target_info)
        raw_target = raw_nodes_by_id.get(_typed_id_key(edge.target_node_id), {})
        connected_inputs = connected_inputs_by_target.get(target_node_key, set())
        selector_values = infer_dynamic_selector_values(
            target_capabilities,
            raw_target.get("widgets_values", [])
            if isinstance(raw_target, Mapping)
            else [],
            connected_inputs=connected_inputs,
        )
        target_alias = existing_alias_by_id.get(_typed_id_key(edge.target_node_id))
        if target_alias is not None:
            selector_values.update(existing_values.get(target_alias, {}))
        materialized = materialize_inputs(
            target_capabilities,
            values=selector_values,
            connected_inputs=connected_inputs,
        )
        matches = [
            item
            for item in materialized
            if item.socket_index == edge.target_input_index
            and (
                item.capability.path == edge.target_input
                or item.capability.name == edge.target_input
            )
        ]
        if len(matches) != 1:
            unknown_targets.add(target_node_key)
            continue
        edge_facts.append(
            _CardinalityEdgeFact(
                source_output_key=source_output_key,
                target_node_key=target_node_key,
                target_input_cardinality=matches[0].capability.cardinality,
            )
        )

    for edge, source, target, _ in direct_edges:
        source_cardinality = _source_endpoint_cardinality(
            edge,
            source,
            existing=existing,
            created_types=created_types,
            catalog=catalog,
        )
        target_cardinality = _target_endpoint_cardinality(
            edge,
            target,
            existing=existing,
            created_types=created_types,
            catalog=catalog,
        )
        source_output_key = _endpoint_output_key(
            source.ref,
            source.output_index,
            source.output,
        )
        target_node_key = _endpoint_node_key(target.ref)
        if source_cardinality is None or target_cardinality is None:
            unknown_targets.add(target_node_key)
            continue
        declared_outputs.setdefault(source_output_key, source_cardinality)
        edge_facts.append(
            _CardinalityEdgeFact(
                source_output_key=source_output_key,
                target_node_key=target_node_key,
                target_input_cardinality=target_cardinality,
            )
        )

    edge_facts.sort(
        key=lambda item: (
            item.source_output_key,
            item.target_node_key,
            item.target_input_cardinality,
        )
    )
    mapped_nodes: set[str] = set()
    unknown_nodes = set(unknown_targets)
    for _ in range(max(1, 2 * len(node_keys) + 1)):
        changed = False
        for edge in edge_facts:
            declared = declared_outputs.get(edge.source_output_key)
            source_node_key = edge.source_output_key[0]
            if declared == "list" or source_node_key in mapped_nodes:
                source_cardinality = "list"
            elif declared is None or source_node_key in unknown_nodes:
                source_cardinality = None
            else:
                source_cardinality = "scalar"
            if edge.target_input_cardinality != "scalar":
                continue
            if source_cardinality == "list":
                if edge.target_node_key not in mapped_nodes:
                    mapped_nodes.add(edge.target_node_key)
                    unknown_nodes.discard(edge.target_node_key)
                    changed = True
            elif (
                source_cardinality is None
                and edge.target_node_key not in mapped_nodes
                and edge.target_node_key not in unknown_nodes
            ):
                unknown_nodes.add(edge.target_node_key)
                changed = True
        if not changed:
            break

    result: dict[tuple[str, int, str], str | None] = {}
    for output_key, declared in declared_outputs.items():
        node_key = output_key[0]
        result[output_key] = (
            "list"
            if declared == "list" or node_key in mapped_nodes
            else None
            if node_key in unknown_nodes
            else "scalar"
        )
    return result


def _compact_route(route: ConversionRoute) -> dict[str, Any]:
    return {
        "node_types": [step.node_type for step in route.steps],
        "schema_hashes": [step.schema_hash for step in route.steps],
        "input_bindings": [
            {
                "node_type": step.node_type,
                "bindings": [
                    {
                        "path": binding.path,
                        "type": binding.input_type,
                        "source_cardinality": binding.source_cardinality,
                        "target_cardinality": binding.target_cardinality,
                        "cardinality_effect": binding.cardinality_effect,
                        "mode": binding.mode,
                    }
                    for binding in step.input_bindings
                ],
            }
            for step in route.steps
        ],
        "selector_values": [
            {
                "node_type": step.node_type,
                "values": [
                    {
                        "path": item.path,
                        "value": item.value,
                        "provenance": item.provenance,
                    }
                    for item in step.selector_values
                ],
            }
            for step in route.steps
            if step.selector_values
        ],
        "resulting_endpoints": [
            {"type": item.type, "cardinality": item.cardinality}
            for item in route.resulting_endpoints
        ],
        "resulting_types": list(route.resulting_types),
        "cost": {
            "intermediary_count": route.cost.intermediary_count,
            "cardinality_penalty": route.cost.cardinality_penalty,
            "unverified_steps": route.cost.unverified_steps,
            "origin_penalty": route.cost.origin_penalty,
            "defaulted_required_widget_count": (
                route.cost.defaulted_required_widget_count
            ),
            "nondefault_selector_count": route.cost.nondefault_selector_count,
        },
    }


def _route_step_identity(step: RouteStep) -> tuple[Any, ...]:
    return (
        step.node_type,
        step.schema_hash,
        tuple(
            (
                item.path,
                json.dumps(
                    item.value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                item.provenance,
            )
            for item in step.selector_values
        ),
        tuple(
            (
                binding.path,
                binding.declaration_index,
                binding.occurrence_index,
                binding.input_type,
                binding.source_cardinality,
                binding.target_cardinality,
                binding.cardinality_effect,
                binding.mode,
            )
            for binding in step.input_bindings
        ),
    )


def _route_identity(route: ConversionRoute) -> tuple[Any, ...]:
    return tuple(_route_step_identity(step) for step in route.steps)


def _route_candidate_identity(
    route: ConversionRoute,
    *,
    original_source_type: str,
    original_source_cardinality: str,
) -> tuple[Any, ...]:
    if all(
        step.input_bindings
        and all(
            binding.input_type == original_source_type
            and binding.source_cardinality == original_source_cardinality
            for binding in step.input_bindings
        )
        for step in route.steps
    ):
        return (
            "independent_siblings",
            tuple(sorted(_route_step_identity(step) for step in route.steps)),
        )
    return ("ordered_route", _route_identity(route))


def _implicit_repeated_binding_facts(
    route: ConversionRoute,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for step in route.steps:
        bindings_by_type: dict[str, list[Any]] = {}
        for binding in step.input_bindings:
            bindings_by_type.setdefault(binding.input_type, []).append(binding)
        for input_type, bindings in sorted(bindings_by_type.items()):
            if len(bindings) > 1:
                result.append(
                    {
                        "node_type": step.node_type,
                        "input_type": input_type,
                        "input_paths": sorted(item.path for item in bindings),
                        "bindings": [
                            {
                                "path": item.path,
                                "source_cardinality": item.source_cardinality,
                                "target_cardinality": item.target_cardinality,
                                "cardinality_effect": item.cardinality_effect,
                            }
                            for item in sorted(bindings, key=lambda item: item.path)
                        ],
                    }
                )
    return result


def _route_step_output_types(
    step: RouteStep,
    profile: TransformProfile,
    output: TransformOutputPort,
) -> frozenset[str]:
    concrete = set(output.produced_types) - {"*"}
    if output.matchtype_template_id is None:
        return frozenset(concrete)
    bound = {
        binding.input_type
        for binding in step.input_bindings
        for input_port in profile.inputs
        if input_port.path == binding.path
        and input_port.matchtype_template_id == output.matchtype_template_id
    }
    return frozenset({*concrete, *bound})


def _route_step_output_cardinality(
    step: RouteStep,
    output: TransformOutputPort,
) -> str:
    if output.cardinality == "list" or any(
        binding.cardinality_effect == "mapped_scalar_over_list"
        for binding in step.input_bindings
    ):
        return "list"
    return "scalar"


def _route_output_for_type(
    route_steps: Sequence[tuple[str, RouteStep, TransformProfile]],
    output_type: str,
    *,
    output_cardinality: str,
    before_step: int | None = None,
    allow_native_cardinality_mapping: bool = False,
) -> tuple[str, TransformOutputPort] | None:
    limit = len(route_steps) if before_step is None else before_step
    for alias, step, profile in reversed(route_steps[:limit]):
        type_matches = [
            output
            for output in step.produced_outputs
            if output_type in _route_step_output_types(step, profile, output)
        ]
        exact_matches = [
            output
            for output in type_matches
            if _route_step_output_cardinality(step, output) == output_cardinality
        ]
        if len(exact_matches) == 1:
            return alias, exact_matches[0]
        if len(exact_matches) > 1:
            return None
        if allow_native_cardinality_mapping:
            if len(type_matches) == 1:
                return alias, type_matches[0]
            if len(type_matches) > 1:
                return None
    return None


def _inferred_aliases(
    *,
    source_alias: str,
    source_output: str,
    source_output_index: int,
    route: ConversionRoute,
) -> list[str]:
    payload = {
        "source_alias": source_alias,
        "source_output": source_output,
        "source_output_index": source_output_index,
        "route": [
            {
                "node_type": step.node_type,
                "schema_hash": step.schema_hash,
                "selector_values": [
                    {"path": item.path, "value": item.value}
                    for item in step.selector_values
                ],
            }
            for step in route.steps
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return [
        f"inferred_converter_{digest}_{index + 1}"
        for index in range(len(route.steps))
    ]


def _target_endpoint(
    edge: RefinementSpecEdge,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    created_values: Mapping[str, Mapping[str, Any]],
    existing_values: Mapping[str, Mapping[str, Any]],
    active_inputs: Mapping[str, Mapping[str, Any]],
    connected_inputs: Mapping[str, set[str]],
    source_types: Collection[str] | None = None,
    occupied_inputs: Collection[str] = (),
    catalog: Mapping[str, Any],
    path: str,
) -> tuple[GraphPatchTargetEndpoint | None, list[dict[str, str]]]:
    node_type = _node_type_for_alias(edge.target_alias, existing, created_types)
    node_info = catalog.get(node_type) if node_type else None
    issues: list[dict[str, str]] = []
    if not isinstance(node_info, Mapping):
        return None, [_issue("target_type_missing", path, "Target node type is not loaded.")]
    capability = normalize_node_schema(node_type, node_info)
    if edge.target_alias in created_types:
        materialized_inputs = materialize_inputs(
            capability,
            values=created_values.get(edge.target_alias, {}),
            connected_inputs=connected_inputs.get(edge.target_alias, set()),
        )
        available = list(active_inputs.get(edge.target_alias, {}))
    else:
        raw = existing.get(edge.target_alias, {})
        live_connected = _connected_input_names(raw)
        planned_connected = connected_inputs.get(edge.target_alias, set())
        selector_values = infer_dynamic_selector_values(
            capability,
            raw.get("widgets_values", []) if isinstance(raw, Mapping) else [],
            connected_inputs=live_connected,
        )
        selector_values.update(existing_values.get(edge.target_alias, {}))
        materialized_inputs = materialize_inputs(
            capability,
            values=selector_values,
            connected_inputs={*live_connected, *planned_connected},
        )
        available = [
            item.capability.path
            for item in materialized_inputs
            if not item.capability.hidden
        ]
    resolved_name, name_issues = _resolve_runtime_name(
        edge.target_input,
        available,
        path=f"{path}.target_input",
        kind="input_slot",
    )
    requested_type: str | None = None
    if resolved_name is None and any(
        item.get("code") == "unknown_input_slot" for item in name_issues
    ):
        requested_token = edge.target_input.casefold()
        typed_matches = [
            (item, accepted_type)
            for item in materialized_inputs
            if not item.capability.hidden
            and item.capability.matchtype is not None
            for accepted_type in item.capability.accepted_types
            if accepted_type.casefold() == requested_token
        ]
        if len(typed_matches) == 1:
            resolved_name = typed_matches[0][0].capability.path
            requested_type = typed_matches[0][1]
            name_issues = []
    semantic_issues: list[dict[str, Any]] = []
    if (
        resolved_name is None
        and source_types
        and any(item.get("code") == "unknown_input_slot" for item in name_issues)
    ):
        semantic_match, semantic_issues = _semantic_autogrow_target(
            edge.target_input,
            materialized_inputs,
            source_types=source_types,
            occupied_inputs=occupied_inputs,
            path=path,
        )
        if semantic_match is not None:
            resolved_name = semantic_match.capability.path
            requested_type = (
                next(iter(source_types)) if len(source_types) == 1 else None
            )
            name_issues = []
    if resolved_name is None:
        if semantic_issues:
            issues.extend(semantic_issues)
        else:
            candidates = [
                _compact_input_candidate(item)
                for item in materialized_inputs
                if not item.capability.hidden
            ]
            issues.extend(_issues_with_endpoint_candidates(name_issues, candidates))
        return None, issues
    issues.extend(name_issues)
    matching_materialized = [
        item
        for item in materialized_inputs
        if not item.capability.hidden
        and (
            item.capability.path == resolved_name
            or item.capability.name == resolved_name
        )
    ]
    matching_capabilities = [item.capability for item in matching_materialized]
    if len(matching_capabilities) != 1:
        return None, [
            _issue(
                "target_schema_input_ambiguous",
                path,
                f"{node_type}.{resolved_name} does not resolve to one exact schema input.",
            )
        ]
    input_capability = matching_capabilities[0]
    endpoint_type = (
        requested_type
        or input_capability.accepted_types[0]
        if len(input_capability.accepted_types) == 1
        else requested_type or "UNRESOLVED"
    )
    if not endpoint_type:
        return None, [_issue("target_type_unknown", path, "Target input type is unknown.")]
    declaration_index = input_capability.declaration_index
    connectable = input_capability.connectable
    widget_convertible = input_capability.widget_convertible
    requested_mode = edge.target_mode
    if requested_mode == "auto":
        mode = "slot" if connectable else "convert_widget" if widget_convertible else "slot"
    else:
        mode = requested_mode
    if mode == "slot" and not connectable:
        issues.append(
            _issue(
                "target_requires_widget_conversion",
                f"{path}.target_mode",
                f"{node_type}.{resolved_name} is a widget and needs convert_widget mode.",
            )
        )
    if mode == "convert_widget" and not widget_convertible:
        issues.append(
            _issue(
                "target_widget_not_convertible",
                f"{path}.target_mode",
                f"{node_type}.{resolved_name} is not a deterministically convertible widget.",
            )
        )
    socket_index = None
    if mode == "slot":
        exact_materialized = [
            item
            for item in materialized_inputs
            if item.capability.declaration_index == declaration_index
            and item.capability.occurrence_index == input_capability.occurrence_index
            and item.capability.path == input_capability.path
        ]
        if len(exact_materialized) != 1 or exact_materialized[0].socket_index is None:
            return None, [
                _issue(
                    "target_dynamic_input_inactive",
                    path,
                    f"{node_type}.{resolved_name} is not active for the planned selector values.",
                )
            ]
        socket_index = exact_materialized[0].socket_index
    return (
        GraphPatchTargetEndpoint(
            ref=_endpoint_ref(edge.target_alias, existing),
            input_index=declaration_index,
            occurrence_index=input_capability.occurrence_index,
            socket_index=socket_index,
            input=resolved_name,
            type=endpoint_type,
            mode=mode,
        ),
        issues,
    )


@dataclass(frozen=True)
class _SemanticEdgeFacts:
    source_endpoint: GraphPatchSourceEndpoint
    target_endpoint: GraphPatchTargetEndpoint
    source_capability: OutputCapability
    target_capability: InputCapability
    source_types: frozenset[str]
    target_types: frozenset[str]
    source_binding_key: str | None
    target_binding_key: str | None
    path: str


def _semantic_edge_facts(
    edge: RefinementSpecEdge,
    source_endpoint: GraphPatchSourceEndpoint,
    target_endpoint: GraphPatchTargetEndpoint,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    graph: NormalizedGraphSnapshot,
    catalog: Mapping[str, Any],
    path: str,
) -> tuple[_SemanticEdgeFacts | None, list[dict[str, str]]]:
    source_type = _node_type_for_alias(edge.source_alias, existing, created_types)
    target_type = _node_type_for_alias(edge.target_alias, existing, created_types)
    source_info = catalog.get(source_type) if source_type else None
    target_info = catalog.get(target_type) if target_type else None
    if not isinstance(source_info, Mapping) or not isinstance(target_info, Mapping):
        return None, [
            _issue(
                "edge_schema_missing",
                path,
                "The source or target schema is unavailable for type unification.",
            )
        ]
    source_capabilities = normalize_node_schema(source_type, source_info)
    target_capabilities = normalize_node_schema(target_type, target_info)
    source_matches = [
        item
        for item in source_capabilities.outputs
        if item.index == source_endpoint.output_index
        and item.name == source_endpoint.output
    ]
    target_matches = [
        item
        for item in target_capabilities.inputs
        if item.declaration_index == target_endpoint.input_index
        and item.occurrence_index == target_endpoint.occurrence_index
        and item.path == target_endpoint.input
    ]
    if len(source_matches) != 1 or len(target_matches) != 1:
        return None, [
            _issue(
                "edge_schema_slot_ambiguous",
                path,
                "The edge endpoints do not resolve to one exact normalized schema slot.",
            )
        ]
    source_capability = source_matches[0]
    target_capability = target_matches[0]
    source_types = set(source_capability.produced_types)
    if isinstance(source_endpoint.ref, ExistingNodeRef):
        active_types = {
            item.type
            for item in graph.outputs
            if _typed_id_key(item.node_id) == _typed_id_key(source_endpoint.ref.node_id)
            and item.output_index == source_endpoint.output_index
            and item.output == source_endpoint.output
        }
        if len(active_types) != 1:
            return None, [
                _issue(
                    "active_source_output_ambiguous",
                    path,
                    "The selected existing output has no single active concrete type.",
                )
            ]
        active_type = next(iter(active_types))
        if "*" not in source_types and active_type not in source_types:
            return None, [
                _issue(
                    "active_source_type_mismatch",
                    path,
                    f"Active output type {active_type!r} violates the loaded source schema.",
                )
            ]
        source_types = {active_type}
    elif source_endpoint.type != "UNRESOLVED":
        if "*" not in source_types and source_endpoint.type not in source_types:
            return None, [
                _issue(
                    "source_type_hint_mismatch",
                    path,
                    f"Requested source type {source_endpoint.type!r} violates the "
                    "loaded source schema.",
                )
            ]
        source_types = {source_endpoint.type}
    return (
        _SemanticEdgeFacts(
            source_endpoint=source_endpoint,
            target_endpoint=target_endpoint,
            source_capability=source_capability,
            target_capability=target_capability,
            source_types=frozenset(source_types),
            target_types=frozenset(target_capability.accepted_types),
            source_binding_key=(
                f"{edge.source_alias}:{source_capability.matchtype_template_id}"
                if source_capability.matchtype_template_id
                else None
            ),
            target_binding_key=(
                f"{edge.target_alias}:{target_capability.matchtype.template_id}"
                if target_capability.matchtype is not None
                else None
            ),
            path=path,
        ),
        [],
    )


def _edge_candidate_types(
    facts: _SemanticEdgeFacts,
    bindings: Mapping[str, str],
) -> set[str] | None:
    """Return a finite equality domain, or ``None`` for unconstrained wildcard."""

    def bound_domain(types: frozenset[str], key: str | None) -> set[str]:
        result = set(types)
        if key is None or key not in bindings:
            return result
        bound = bindings[key]
        if "*" not in result and bound not in result:
            return set()
        return {bound}

    source_types = bound_domain(facts.source_types, facts.source_binding_key)
    target_types = bound_domain(facts.target_types, facts.target_binding_key)
    if not source_types or not target_types:
        return set()
    source_wildcard = "*" in source_types
    target_wildcard = "*" in target_types
    if source_wildcard and target_wildcard:
        return None
    if source_wildcard:
        return target_types - {"*"} or None
    if target_wildcard:
        return source_types - {"*"} or None
    return source_types & target_types


def _semantic_edge_sort_key(facts: _SemanticEdgeFacts) -> tuple[Any, ...]:
    return (
        facts.source_endpoint.ref.model_dump_json(),
        facts.source_endpoint.output_index,
        facts.source_endpoint.output,
        facts.target_endpoint.ref.model_dump_json(),
        facts.target_endpoint.input_index,
        facts.target_endpoint.occurrence_index,
        facts.target_endpoint.input,
    )


def _solve_edge_types(
    unresolved: Sequence[
        tuple[
            RefinementSpecEdge,
            GraphPatchSourceEndpoint,
            GraphPatchTargetEndpoint,
            str,
        ]
    ],
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    graph: NormalizedGraphSnapshot,
    catalog: Mapping[str, Any],
) -> tuple[list[GraphPatchEdge], list[dict[str, str]]]:
    """Solve node-instance MATCHTYPE constraints over the complete edge set."""

    issues: list[dict[str, str]] = []
    facts: list[_SemanticEdgeFacts] = []
    for edge, source_endpoint, target_endpoint, path in unresolved:
        item, item_issues = _semantic_edge_facts(
            edge,
            source_endpoint,
            target_endpoint,
            existing=existing,
            created_types=created_types,
            graph=graph,
            catalog=catalog,
            path=path,
        )
        issues.extend(item_issues)
        if item is not None:
            facts.append(item)
    facts.sort(key=_semantic_edge_sort_key)

    bindings: dict[str, str] = {}
    while True:
        proposals: dict[str, set[str]] = {}
        for item in facts:
            candidates = _edge_candidate_types(item, bindings)
            if candidates is None or len(candidates) != 1:
                continue
            concrete_type = next(iter(candidates))
            for key in (item.source_binding_key, item.target_binding_key):
                if key is not None and key not in bindings:
                    proposals.setdefault(key, set()).add(concrete_type)
        conflicts = {
            key: values for key, values in proposals.items() if len(values) > 1
        }
        if conflicts:
            for key, values in sorted(conflicts.items()):
                issues.append(
                    _issue(
                        "matchtype_binding_conflict",
                        "add_edges",
                        f"MATCHTYPE variable {key!r} is constrained to conflicting types: "
                        f"{', '.join(sorted(values))}.",
                    )
                )
            return [], issues
        changed = False
        for key, values in sorted(proposals.items()):
            value = next(iter(values))
            if key not in bindings:
                bindings[key] = value
                changed = True
        if not changed:
            break

    solved: list[GraphPatchEdge] = []
    for item in facts:
        candidates = _edge_candidate_types(item, bindings)
        if candidates is None:
            issues.append(
                _issue(
                    "edge_type_ambiguous",
                    item.path,
                    "The edge remains polymorphic after graph-wide MATCHTYPE solving.",
                )
            )
            continue
        if not candidates:
            code = (
                "matchtype_binding_conflict"
                if item.source_binding_key or item.target_binding_key
                else "edge_type_incompatible"
            )
            issues.append(
                _issue(
                    code,
                    item.path,
                    "The graph-wide source and target type constraints do not intersect.",
                )
            )
            continue
        if len(candidates) != 1:
            issues.append(
                _issue(
                    "edge_type_ambiguous",
                    item.path,
                    "The edge remains polymorphic after graph-wide MATCHTYPE solving.",
                )
            )
            continue
        concrete_type = next(iter(candidates))
        compatibility = classify_connection(
            item.source_capability,
            item.target_capability,
            type_bindings=bindings,
            source_binding_key=item.source_binding_key,
            target_binding_key=item.target_binding_key,
        )
        remaining = [
            reason
            for reason in compatibility.reasons
            if not (
                item.target_endpoint.mode == "convert_widget"
                and reason.code == "widget_conversion_required"
            )
        ]
        unsupported = [reason for reason in remaining if reason.status == "unsupported"]
        adapters = [reason for reason in remaining if reason.status == "adapter_required"]
        if unsupported:
            issues.append(
                _issue(
                    "edge_type_incompatible",
                    item.path,
                    "; ".join(reason.message for reason in unsupported),
                )
            )
            continue
        if adapters:
            issues.append(
                _issue(
                    "connection_adapter_required",
                    item.path,
                    "; ".join(
                        f"{reason.code}: {reason.message}" for reason in adapters
                    ),
                )
            )
            continue
        solved.append(
            GraphPatchEdge(
                source=item.source_endpoint.model_copy(update={"type": concrete_type}),
                target=item.target_endpoint.model_copy(update={"type": concrete_type}),
            )
        )
    return solved, issues


def _unify_edge_type(
    edge: RefinementSpecEdge,
    source_endpoint: GraphPatchSourceEndpoint,
    target_endpoint: GraphPatchTargetEndpoint,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    created_types: Mapping[str, str],
    graph: NormalizedGraphSnapshot,
    catalog: Mapping[str, Any],
    path: str,
) -> tuple[
    GraphPatchSourceEndpoint | None,
    GraphPatchTargetEndpoint | None,
    list[dict[str, str]],
]:
    solved, issues = _solve_edge_types(
        [(edge, source_endpoint, target_endpoint, path)],
        existing=existing,
        created_types=created_types,
        graph=graph,
        catalog=catalog,
    )
    if len(solved) != 1:
        return None, None, issues
    return solved[0].source, solved[0].target, issues


def compile_workflow_refinement_spec(
    request: CompileWorkflowRefinementSpecRequest,
    workflow: Mapping[str, Any],
    *,
    workflow_identity: str,
    graph_hash: str,
    catalog: Mapping[str, Any],
    catalog_hash: str,
    source: str,
    validated_attachment_values: Mapping[tuple[str, str], Any] | None = None,
    selected_node_ids: Sequence[NodeId] | None = None,
    verified_lessons: Sequence[VerifiedCapabilityLesson] = (),
) -> dict[str, Any]:
    """Resolve one semantic new build or current-workflow edit to GraphPatch."""

    graph = normalize_workflow_graph(workflow)
    issues: list[dict[str, str]] = []
    inferred_routes: list[dict[str, Any]] = []
    inference_needs_choice = False
    if request.expected_graph_hash and request.expected_graph_hash != graph_hash:
        issues.append(_issue("graph_changed", "expected_graph_hash", "The active workflow changed."))
    if request.expected_catalog_hash and request.expected_catalog_hash != catalog_hash:
        issues.append(_issue("catalog_changed", "expected_catalog_hash", "The local node catalog changed."))
    existing, selector_evidence, selector_issues = _resolve_existing_selectors(
        request.existing_nodes,
        workflow,
        graph,
        catalog,
        selected_node_ids,
    )
    issues.extend(selector_issues)

    capability_request = ResolveWorkflowSpecRequest(
        capabilities=[
            WorkflowCapabilitySpec.model_validate(
                item.model_dump(mode="json", exclude={"values"})
            )
            for item in request.create_nodes
        ],
        expected_catalog_hash=catalog_hash,
    ) if request.create_nodes else None
    resolution = (
        resolve_workflow_spec(
            capability_request,
            catalog,
            catalog_hash=catalog_hash,
            source=source,
        )
        if capability_request is not None
        else {
            "valid": True,
            "needs_choice": False,
            "resolution_hash": None,
            "selected_node_types": {},
            "resolutions": [],
            "issues": [],
        }
    )
    issues.extend(resolution["issues"])
    selected_types: dict[str, str] = resolution["selected_node_types"]
    partner_review = _partner_review(selected_types, catalog)

    attachment_inputs: dict[str, set[str]] = {}
    for item in request.attachments:
        attachment_inputs.setdefault(item.target_alias, set()).add(item.target_input)
    connection_hints: dict[str, set[str]] = {}
    for edge in request.add_edges:
        connection_hints.setdefault(edge.target_alias, set()).add(edge.target_input)
    removed_connection_hints: dict[str, set[str]] = {}
    for edge in request.remove_edges:
        removed_connection_hints.setdefault(edge.target_alias, set()).add(
            edge.target_input
        )

    creates: list[GraphPatchCreateNode] = []
    active_inputs: dict[str, Mapping[str, Any]] = {}
    created_values: dict[str, Mapping[str, Any]] = {}
    for item in request.create_nodes:
        node_type = selected_types.get(item.alias)
        node_info = catalog.get(node_type) if node_type else None
        if not isinstance(node_info, Mapping):
            continue
        values, inputs, value_issues = _canonicalize_node_values(
            item,
            node_info,
            attachment_inputs.get(item.alias, set()),
            connection_hints.get(item.alias, set()),
        )
        issues.extend(value_issues)
        active_inputs[item.alias] = inputs
        created_values[item.alias] = values
        creates.append(
            GraphPatchCreateNode(
                alias=item.alias,
                node_type=node_type,
                schema_hash=node_schema_hash(node_type, node_info),
                values=values,
            )
        )

    updates: list[GraphPatchUpdateNode] = []
    existing_current_values: dict[str, Mapping[str, Any]] = {}
    existing_update_values: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(request.update_nodes):
        raw = existing.get(item.target_alias)
        if raw is None:
            continue
        node_type = str(raw.get("type"))
        node_info = catalog.get(node_type)
        if not isinstance(node_info, Mapping):
            continue
        current_connected_inputs = _connected_input_names(raw)
        future_connected_inputs = {
            *(
                current_connected_inputs
                - removed_connection_hints.get(item.target_alias, set())
            ),
            *connection_hints.get(item.target_alias, set()),
        }
        update, current_values, values_after_update, update_issues = (
            _canonicalize_existing_update(
                item,
                raw,
                node_info,
                current_connected_inputs=current_connected_inputs,
                future_connected_inputs=future_connected_inputs,
                path=f"update_nodes[{index}]",
            )
        )
        if update is not None:
            updates.append(update)
        existing_current_values[item.target_alias] = current_values
        existing_update_values[item.target_alias] = values_after_update
        issues.extend(update_issues)

    resolved_add_edges: list[
        tuple[
            RefinementSpecEdge,
            GraphPatchSourceEndpoint,
            GraphPatchTargetEndpoint,
            str,
        ]
    ] = []
    allocated_target_inputs: dict[str, set[str]] = {
        alias: _connected_input_names(raw)
        for alias, raw in existing.items()
    }
    for index, item in enumerate(request.add_edges):
        path = f"add_edges[{index}]"
        source_endpoint, source_issues = _source_endpoint(
            item,
            existing=existing,
            created_types=selected_types,
            graph=graph,
            catalog=catalog,
            path=path,
        )
        source_types = (
            _source_endpoint_types(
                item,
                source_endpoint,
                existing=existing,
                created_types=selected_types,
                catalog=catalog,
            )
            if source_endpoint is not None
            else frozenset()
        )
        target_endpoint, target_issues = _target_endpoint(
            item,
            existing=existing,
            created_types=selected_types,
            created_values=created_values,
            existing_values=existing_update_values,
            active_inputs=active_inputs,
            connected_inputs=connection_hints,
            source_types=source_types,
            occupied_inputs=allocated_target_inputs.get(item.target_alias, set()),
            catalog=catalog,
            path=path,
        )
        issues.extend(source_issues)
        issues.extend(target_issues)
        if source_endpoint is not None and target_endpoint is not None:
            allocated_target_inputs.setdefault(item.target_alias, set()).add(
                target_endpoint.input
            )
            resolved_add_edges.append(
                (item, source_endpoint, target_endpoint, path)
            )

    direct_add_edges: list[
        tuple[
            RefinementSpecEdge,
            GraphPatchSourceEndpoint,
            GraphPatchTargetEndpoint,
            str,
        ]
    ] = []
    incompatible_add_edges: list[
        tuple[
            RefinementSpecEdge,
            GraphPatchSourceEndpoint,
            GraphPatchTargetEndpoint,
            str,
        ]
    ] = []
    incompatible_by_source: dict[
        tuple[str, str, int, str, str],
        list[
            tuple[
                RefinementSpecEdge,
                GraphPatchSourceEndpoint,
                GraphPatchTargetEndpoint,
                str,
            ]
        ],
    ] = {}
    incompatible_target_options: dict[str, tuple[str, ...]] = {}
    incompatible_target_cardinalities: dict[str, str] = {}
    for resolved in resolved_add_edges:
        item, source_endpoint, target_endpoint, path = resolved
        source_domain = _source_endpoint_types(
            item,
            source_endpoint,
            existing=existing,
            created_types=selected_types,
            catalog=catalog,
        )
        target_domain = _target_endpoint_types(
            item,
            target_endpoint,
            existing=existing,
            created_types=selected_types,
            catalog=catalog,
        )
        concrete_source_domain = source_domain - {"*"}
        concrete_target_domain = target_domain - {"*"}
        directly_compatible = (
            bool(concrete_source_domain & concrete_target_domain)
            or ("*" in source_domain and bool(concrete_target_domain))
            or ("*" in target_domain and bool(concrete_source_domain))
        )
        if (
            not directly_compatible
            and len(concrete_source_domain) == 1
            and source_domain == concrete_source_domain
            and concrete_target_domain
            and target_domain == concrete_target_domain
        ):
            target_cardinality = _target_endpoint_cardinality(
                item,
                target_endpoint,
                existing=existing,
                created_types=selected_types,
                catalog=catalog,
            )
            if target_cardinality is None:
                issues.append(
                    _issue(
                        "converter_cardinality_unresolved",
                        path,
                        "Converter inference requires one exact normalized source "
                        "output and target input cardinality.",
                    )
                )
                continue
            incompatible_add_edges.append(resolved)
            incompatible_target_options[path] = tuple(
                sorted(
                    concrete_target_domain,
                    key=lambda value: (value.casefold(), value),
                )
            )
            incompatible_target_cardinalities[path] = target_cardinality
        else:
            # Direct concrete compatibility and graph-scoped polymorphic edges
            # stay on the existing exact solver path. Inference never replaces
            # an edge the loaded schemas already support.
            direct_add_edges.append(resolved)

    effective_cardinalities = _effective_output_cardinalities(
        graph=graph,
        workflow=workflow,
        catalog=catalog,
        created_types=selected_types,
        created_values=created_values,
        existing=existing,
        existing_values=existing_update_values,
        direct_edges=direct_add_edges,
        removed_connection_hints=removed_connection_hints,
        removed_aliases=request.remove_nodes,
    )
    for resolved in incompatible_add_edges:
        item, source_endpoint, _, path = resolved
        source_domain = _source_endpoint_types(
            item,
            source_endpoint,
            existing=existing,
            created_types=selected_types,
            catalog=catalog,
        )
        source_type = next(iter(source_domain))
        source_cardinality = effective_cardinalities.get(
            _endpoint_output_key(
                source_endpoint.ref,
                source_endpoint.output_index,
                source_endpoint.output,
            )
        )
        if source_cardinality is None:
            issues.append(
                _issue(
                    "converter_cardinality_unresolved",
                    path,
                    "The effective source cardinality is not provable from the "
                    "retained and explicitly direct workflow graph.",
                )
            )
            continue
        key = (
            item.source_alias,
            source_endpoint.output,
            source_endpoint.output_index,
            source_type,
            source_cardinality,
        )
        incompatible_by_source.setdefault(key, []).append(resolved)

    synthesized_add_edges: list[
        tuple[
            RefinementSpecEdge,
            GraphPatchSourceEndpoint,
            GraphPatchTargetEndpoint,
            str,
        ]
    ] = []
    capability_graph: CapabilityGraph | None = None
    used_aliases = {*existing, *selected_types}
    for route_index, (source_key, grouped) in enumerate(
        sorted(incompatible_by_source.items(), key=lambda item: item[0])
    ):
        grouped = sorted(
            grouped,
            key=lambda item: (
                item[2].ref.model_dump_json(),
                item[2].input_index,
                item[2].occurrence_index,
                item[2].input,
            ),
        )
        (
            source_alias,
            source_output,
            source_output_index,
            source_type,
            source_cardinality,
        ) = source_key
        target_type_options = [
            incompatible_target_options[item[3]] for item in grouped
        ]
        target_cardinalities = [
            incompatible_target_cardinalities[item[3]] for item in grouped
        ]
        evidence: dict[str, Any] = {
            "source": {
                "alias": source_alias,
                "output": source_output,
                "output_index": source_output_index,
                "type": source_type,
                "cardinality": source_cardinality,
            },
            "required_type_options": [
                {
                    "target": {
                        "alias": item[0].target_alias,
                        "input": item[2].input,
                        "input_index": item[2].input_index,
                        "occurrence_index": item[2].occurrence_index,
                    },
                    "types": list(options),
                    "cardinality": cardinality,
                }
                for item, options, cardinality in zip(
                    grouped,
                    target_type_options,
                    target_cardinalities,
                    strict=True,
                )
            ],
            "status": "unresolved",
            "selected": [],
            "choices": [],
        }
        if not request.allow_inferred_converters:
            evidence["status"] = "disabled"
            inferred_routes.append(evidence)
            issues.append(
                _issue(
                    "converter_inference_disabled",
                    grouped[0][3],
                    "The direct edge types are incompatible and inferred converter "
                    "nodes are disabled for this exact/no-extra request.",
                )
            )
            continue

        capability_graph = capability_graph or build_capability_graph(catalog)
        target_assignments: list[tuple[str, ...]] = [()]
        target_domain_too_broad = False
        for options in target_type_options:
            if len(options) > 64 // len(target_assignments):
                target_domain_too_broad = True
                break
            target_assignments = [
                (*prior, option)
                for prior in target_assignments
                for option in options
            ]
        if target_domain_too_broad:
            evidence["status"] = "target_domain_too_broad"
            inferred_routes.append(evidence)
            issues.append(
                _issue(
                    "converter_target_domain_too_broad",
                    grouped[0][3],
                    "Converter inference would require more than 64 target-type combinations.",
                )
            )
            continue

        candidates: dict[
            tuple[tuple[Any, ...], tuple[str, ...]],
            tuple[ConversionRoute, tuple[str, ...]],
        ] = {}
        rejection_records: dict[tuple[Any, ...], dict[str, Any]] = {}
        implicit_binding_rejections: dict[tuple[Any, ...], dict[str, Any]] = {}
        issue_codes: set[str] = set()
        accepted_lesson_count = 0
        ignored_lesson_count = 0
        for assignment in target_assignments:
            required_endpoints = {
                RouteEndpoint(target_type, target_cardinality)
                for target_type, target_cardinality in zip(
                    assignment,
                    target_cardinalities,
                    strict=True,
                )
            }
            route_result = capability_graph.find_route(
                available_endpoints={
                    RouteEndpoint(source_type, source_cardinality)
                },
                required_endpoints=required_endpoints,
                policy=RoutePolicy(max_intermediaries=2),
                verified_lessons=verified_lessons,
            )
            accepted_lesson_count = max(
                accepted_lesson_count,
                route_result.accepted_verified_lesson_count,
            )
            ignored_lesson_count = max(
                ignored_lesson_count,
                route_result.ignored_verified_lesson_count,
            )
            issue_codes.update(route_result.issue_codes)
            selectable: Sequence[ConversionRoute] = route_result.choices
            if not selectable and route_result.selected is not None:
                selectable = (route_result.selected,)
            for candidate in selectable:
                repeated_bindings = _implicit_repeated_binding_facts(candidate)
                if repeated_bindings:
                    implicit_binding_rejections[
                        (_route_identity(candidate), assignment)
                    ] = {
                        **_compact_route(candidate),
                        "bindings": repeated_bindings,
                    }
                    continue
                candidate_key = (
                    _route_candidate_identity(
                        candidate,
                        original_source_type=source_type,
                        original_source_cardinality=source_cardinality,
                    ),
                    assignment,
                )
                previous = candidates.get(candidate_key)
                if previous is None or (
                    candidate.cost,
                    _route_identity(candidate),
                ) < (
                    previous[0].cost,
                    _route_identity(previous[0]),
                ):
                    candidates[candidate_key] = (candidate, assignment)
            for item in route_result.rejections:
                compact_rejection = {
                    "node_type": item.node_type,
                    "code": item.code,
                    "goal_types_produced": list(item.goal_types_produced),
                    "missing_input_types": list(item.missing_input_types),
                    "missing_widget_paths": list(item.missing_widget_paths),
                }
                rejection_records[
                    (
                        item.node_type,
                        item.code,
                        item.missing_input_types,
                        item.missing_widget_paths,
                    )
                ] = compact_rejection

        ranked_candidates = sorted(
            candidates.values(),
            key=lambda item: (
                item[0].cost,
                _route_identity(item[0]),
                item[1],
            ),
        )
        best_candidates = (
            [
                item
                for item in ranked_candidates
                if item[0].cost == ranked_candidates[0][0].cost
            ]
            if ranked_candidates
            else []
        )

        def compact_candidate(
            item: tuple[ConversionRoute, tuple[str, ...]],
            group_items: tuple[
                tuple[
                    RefinementSpecEdge,
                    GraphPatchSourceEndpoint,
                    GraphPatchTargetEndpoint,
                    str,
                ],
                ...,
            ] = tuple(grouped),
            group_cardinalities: tuple[str, ...] = tuple(target_cardinalities),
        ) -> dict[str, Any]:
            candidate, assignment = item
            return {
                **_compact_route(candidate),
                "target_types": [
                    {
                        "target": {
                            "alias": group_items[index][0].target_alias,
                            "input": group_items[index][2].input,
                            "input_index": group_items[index][2].input_index,
                            "occurrence_index": group_items[index][2].occurrence_index,
                        },
                        "type": target_type,
                        "cardinality": group_cardinalities[index],
                    }
                    for index, target_type in enumerate(assignment)
                ],
            }

        evidence.update(
            {
                "accepted_verified_lesson_count": accepted_lesson_count,
                "ignored_verified_lesson_count": ignored_lesson_count,
                "issue_codes": sorted(issue_codes),
                "choices": [
                    compact_candidate(item) for item in best_candidates[:5]
                ],
                "rejected_implicit_bindings": list(
                    implicit_binding_rejections.values()
                )[:5],
            }
        )
        if len(best_candidates) != 1 or not best_candidates[0][0].steps:
            needs_choice = len(best_candidates) > 1
            requires_explicit_mapping = (
                not needs_choice
                and not best_candidates
                and bool(implicit_binding_rejections)
            )
            requires_side_inputs = not needs_choice and any(
                item["goal_types_produced"] and item["missing_input_types"]
                for item in rejection_records.values()
            )
            inference_needs_choice = inference_needs_choice or needs_choice
            evidence["status"] = "needs_choice" if needs_choice else "unresolved"
            issue_code = (
                "ambiguous_conversion_route"
                if needs_choice
                else "conversion_route_requires_explicit_input_mappings"
                if requires_explicit_mapping
                else "conversion_route_requires_explicit_side_inputs"
                if requires_side_inputs
                else "conversion_route_unresolved"
            )
            issues.append(
                {
                    **_issue(
                        issue_code,
                        grouped[0][3],
                        (
                            "Multiple equally safe local converter routes or target "
                            "types match; choose one explicitly."
                            if needs_choice
                            else "A candidate converter would bind one inferred source "
                            "to multiple same-type inputs; map those inputs explicitly."
                            if requires_explicit_mapping
                            else "A candidate converter needs additional typed source "
                            "branches; add those side-input edges explicitly."
                            if requires_side_inputs
                            else "No safe local converter route can satisfy all target types."
                        ),
                    ),
                    "route_candidates": evidence["choices"],
                    "rejections": list(rejection_records.values())[:5],
                }
            )
            inferred_routes.append(evidence)
            continue

        route, selected_assignment = best_candidates[0]
        issue_codes.discard("ambiguous_conversion_route")
        evidence["status"] = "resolved"
        evidence["issue_codes"] = sorted(issue_codes)
        evidence["required_types"] = sorted(
            set(selected_assignment), key=lambda value: (value.casefold(), value)
        )
        evidence["required_endpoints"] = [
            {"type": item.type, "cardinality": item.cardinality}
            for item in sorted(
                {
                    RouteEndpoint(target_type, target_cardinality)
                    for target_type, target_cardinality in zip(
                        selected_assignment,
                        target_cardinalities,
                        strict=True,
                    )
                }
            )
        ]
        selected_target_types = {
            item[3]: target_type
            for item, target_type in zip(
                grouped,
                selected_assignment,
                strict=True,
            )
        }
        selected_target_cardinalities = {
            item[3]: target_cardinality
            for item, target_cardinality in zip(
                grouped,
                target_cardinalities,
                strict=True,
            )
        }

        aliases = _inferred_aliases(
            source_alias=source_alias,
            source_output=source_output,
            source_output_index=source_output_index,
            route=route,
        )
        if len(creates) + len(aliases) > 100:
            issues.append(
                _issue(
                    "inferred_node_limit_exceeded",
                    grouped[0][3],
                    "The selected converter route would exceed the 100-node patch limit.",
                )
            )
            evidence["status"] = "node_limit_exceeded"
            inferred_routes.append(evidence)
            continue
        collisions = sorted(set(aliases) & used_aliases)
        if collisions:
            issues.append(
                _issue(
                    "inferred_alias_collision",
                    grouped[0][3],
                    "Deterministic inferred aliases collide with requested aliases: "
                    + ", ".join(collisions),
                )
            )
            evidence["status"] = "alias_collision"
            inferred_routes.append(evidence)
            continue

        route_steps: list[tuple[str, RouteStep, TransformProfile]] = []
        synthesis_failed = False
        for alias, step in zip(aliases, route.steps, strict=True):
            profile = capability_graph.profile(step.node_type)
            node_info = catalog.get(step.node_type)
            if (
                profile is None
                or not isinstance(node_info, Mapping)
                or profile.schema_hash != step.schema_hash
            ):
                issues.append(
                    _issue(
                        "inferred_schema_changed",
                        grouped[0][3],
                        "An inferred converter no longer matches the pinned local schema.",
                    )
                )
                synthesis_failed = True
                break
            values_requested = {
                item.path: item.value for item in step.selector_values
            }
            values_requested.update({
                item.path: item.value
                for item in step.required_stable_widget_values
            })
            binding_paths = {item.path for item in step.input_bindings}
            inferred_spec = WorkflowSpecNode(
                alias=alias,
                capability="inferred safe local converter",
                requested_node_type=step.node_type,
                values=values_requested,
            )
            values, inputs, value_issues = _canonicalize_node_values(
                inferred_spec,
                node_info,
                set(),
                binding_paths,
            )
            issues.extend(value_issues)
            selected_types[alias] = step.node_type
            created_values[alias] = values
            active_inputs[alias] = inputs
            connection_hints[alias] = binding_paths
            creates.append(
                GraphPatchCreateNode(
                    alias=alias,
                    node_type=step.node_type,
                    schema_hash=step.schema_hash,
                    values=values,
                )
            )
            route_steps.append((alias, step, profile))
            used_aliases.add(alias)
            evidence["selected"].append(
                {
                    "alias": alias,
                    "node_type": step.node_type,
                    "schema_hash": step.schema_hash,
                    "input_bindings": [
                        {
                            "path": binding.path,
                            "type": binding.input_type,
                            "source_cardinality": binding.source_cardinality,
                            "target_cardinality": binding.target_cardinality,
                            "cardinality_effect": binding.cardinality_effect,
                            "mode": binding.mode,
                        }
                        for binding in step.input_bindings
                    ],
                    "stable_widget_values": [
                        {"path": item.path, "value": item.value}
                        for item in step.required_stable_widget_values
                    ],
                    "selector_values": [
                        {"path": item.path, "value": item.value}
                        for item in step.selector_values
                    ],
                    "verified_lesson_count": step.verified_lesson_count,
                }
            )
        if synthesis_failed:
            evidence["status"] = "synthesis_failed"
            inferred_routes.append(evidence)
            continue

        original_spec, original_source, _, _ = grouped[0]

        def resolve_synthetic_source(
            spec: RefinementSpecEdge,
            *,
            synthetic_path: str,
            expected_spec: RefinementSpecEdge = original_spec,
            expected_source: GraphPatchSourceEndpoint = original_source,
        ) -> GraphPatchSourceEndpoint | None:
            if (
                spec.source_alias == expected_spec.source_alias
                and spec.source_output == expected_source.output
                and spec.source_output_index == expected_source.output_index
            ):
                return expected_source
            endpoint, endpoint_issues = _source_endpoint(
                spec,
                existing=existing,
                created_types=selected_types,
                graph=graph,
                catalog=catalog,
                path=synthetic_path,
            )
            issues.extend(endpoint_issues)
            return endpoint

        for step_index, (target_alias, step, _) in enumerate(route_steps):
            for binding_index, binding in enumerate(step.input_bindings):
                if (
                    binding.input_type == source_type
                    and binding.source_cardinality == source_cardinality
                ):
                    producer_alias = original_spec.source_alias
                    producer_output = original_source.output
                    producer_output_index = original_source.output_index
                else:
                    producer = _route_output_for_type(
                        route_steps,
                        binding.input_type,
                        output_cardinality=binding.source_cardinality,
                        before_step=step_index,
                    )
                    if producer is None:
                        issues.append(
                            _issue(
                                "inferred_binding_source_ambiguous",
                                grouped[0][3],
                                f"No unique prior converter output supplies "
                                f"{binding.input_type!r} with "
                                f"{binding.source_cardinality!r} cardinality.",
                            )
                        )
                        synthesis_failed = True
                        continue
                    producer_alias, producer_port = producer
                    producer_output = producer_port.name
                    producer_output_index = producer_port.index
                synthetic_path = (
                    f"inferred_routes[{route_index}].steps[{step_index}]"
                    f".input_bindings[{binding_index}]"
                )
                spec = RefinementSpecEdge(
                    source_alias=producer_alias,
                    source_output=producer_output,
                    source_output_index=producer_output_index,
                    target_alias=target_alias,
                    target_input=binding.path,
                    target_mode=binding.mode,
                )
                source_endpoint = resolve_synthetic_source(
                    spec,
                    synthetic_path=synthetic_path,
                )
                target_endpoint, target_issues = _target_endpoint(
                    spec,
                    existing=existing,
                    created_types=selected_types,
                    created_values=created_values,
                    existing_values=existing_update_values,
                    active_inputs=active_inputs,
                    connected_inputs=connection_hints,
                    source_types={binding.input_type},
                    occupied_inputs=allocated_target_inputs.get(target_alias, set()),
                    catalog=catalog,
                    path=synthetic_path,
                )
                issues.extend(target_issues)
                if source_endpoint is None or target_endpoint is None:
                    synthesis_failed = True
                    continue
                allocated_target_inputs.setdefault(target_alias, set()).add(
                    target_endpoint.input
                )
                synthesized_add_edges.append(
                    (spec, source_endpoint, target_endpoint, synthetic_path)
                )

        for target_index, (original, _, target_endpoint, target_path) in enumerate(
            grouped
        ):
            selected_target_type = selected_target_types[target_path]
            selected_target_cardinality = selected_target_cardinalities[target_path]
            producer = _route_output_for_type(
                route_steps,
                selected_target_type,
                output_cardinality=selected_target_cardinality,
                allow_native_cardinality_mapping=True,
            )
            if producer is None:
                issues.append(
                    _issue(
                        "inferred_target_output_ambiguous",
                        grouped[0][3],
                        f"No unique inferred output supplies {selected_target_type!r} "
                        f"for {selected_target_cardinality!r} cardinality.",
                    )
                )
                synthesis_failed = True
                continue
            producer_alias, producer_port = producer
            synthetic_path = (
                f"inferred_routes[{route_index}].targets[{target_index}]"
            )
            spec = RefinementSpecEdge(
                source_alias=producer_alias,
                source_output=producer_port.name,
                source_output_index=producer_port.index,
                target_alias=original.target_alias,
                target_input=target_endpoint.input,
                target_mode=target_endpoint.mode,
            )
            source_endpoint = resolve_synthetic_source(
                spec,
                synthetic_path=synthetic_path,
            )
            if source_endpoint is None:
                synthesis_failed = True
                continue
            synthesized_add_edges.append(
                (
                    spec,
                    source_endpoint,
                    target_endpoint.model_copy(
                        update={"type": selected_target_type}
                    ),
                    synthetic_path,
                )
            )
        if synthesis_failed:
            evidence["status"] = "synthesis_failed"
        else:
            evidence["status"] = "resolved"
        inferred_routes.append(evidence)

    partner_review = _partner_review(selected_types, catalog)
    canonical_add_edges, add_type_issues = _solve_edge_types(
        [*direct_add_edges, *synthesized_add_edges],
        existing=existing,
        created_types=selected_types,
        graph=graph,
        catalog=catalog,
    )
    issues.extend(add_type_issues)

    # Existing-edge removals reuse exact active endpoint facts. They are rare in
    # the first compiler path, but remain part of the semantic contract.
    canonical_remove_edges: list[GraphPatchEdge] = []
    for index, item in enumerate(request.remove_edges):
        path = f"remove_edges[{index}]"
        source_endpoint, source_issues = _source_endpoint(
            item,
            existing=existing,
            created_types=selected_types,
            graph=graph,
            catalog=catalog,
            path=path,
        )
        target_endpoint, target_issues = _target_endpoint(
            item,
            existing=existing,
            created_types=selected_types,
            created_values=created_values,
            existing_values=existing_current_values,
            active_inputs=active_inputs,
            connected_inputs={},
            catalog=catalog,
            path=path,
        )
        issues.extend(source_issues)
        issues.extend(target_issues)
        if source_endpoint is not None and target_endpoint is not None:
            source_endpoint, target_endpoint, type_issues = _unify_edge_type(
                item,
                source_endpoint,
                target_endpoint,
                existing=existing,
                created_types=selected_types,
                graph=graph,
                catalog=catalog,
                path=path,
            )
            issues.extend(type_issues)
            if source_endpoint is not None and target_endpoint is not None:
                canonical_remove_edges.append(
                    GraphPatchEdge(source=source_endpoint, target=target_endpoint)
                )

    def runtime_edge_key(edge: GraphPatchEdge) -> tuple[Any, ...]:
        return (
            edge.source.ref.model_dump_json(),
            edge.source.output_index,
            edge.target.ref.model_dump_json(),
            edge.target.socket_index,
        )

    graph_node_types = {
        _typed_id_key(item.node_id): item.node_type for item in graph.nodes
    }
    workflow_nodes_by_id = {
        _typed_id_key(item["id"]): item
        for item in _raw_nodes(workflow)
        if "id" in item
    }
    connected_names_by_target: dict[tuple[str, Any], set[str]] = {}
    for graph_edge in graph.edges:
        connected_names_by_target.setdefault(
            _typed_id_key(graph_edge.target_node_id),
            set(),
        ).add(graph_edge.target_input)

    removals: list[GraphPatchRemoveNode] = []
    for alias in request.remove_nodes:
        raw = existing.get(alias)
        if raw is None:
            continue
        node_type = str(raw.get("type"))
        node_info = catalog.get(node_type)
        if not isinstance(node_info, Mapping):
            continue
        node_id = raw["id"]
        incident: list[GraphPatchEdge] = []
        for edge_index, graph_edge in enumerate(graph.edges):
            if not (
                _typed_id_key(graph_edge.source_node_id) == _typed_id_key(node_id)
                or _typed_id_key(graph_edge.target_node_id) == _typed_id_key(node_id)
            ):
                continue
            target_type = graph_node_types.get(_typed_id_key(graph_edge.target_node_id))
            target_info = catalog.get(target_type) if target_type else None
            if not isinstance(target_info, Mapping):
                issues.append(
                    _issue(
                        "remove_incident_target_unloaded",
                        f"remove_nodes.{alias}",
                        "An incident edge targets a node whose exact schema is not loaded.",
                    )
                )
                continue
            edge_issues: list[dict[str, str]] = []
            exact = _baseline_edge(
                graph_edge,
                target_capabilities=normalize_node_schema(target_type, target_info),
                target_widget_values=list(
                    workflow_nodes_by_id.get(
                        _typed_id_key(graph_edge.target_node_id),
                        {},
                    ).get("widgets_values", [])
                ),
                connected_inputs=connected_names_by_target.get(
                    _typed_id_key(graph_edge.target_node_id),
                    set(),
                ),
                path=f"remove_nodes.{alias}.incident_edges[{edge_index}]",
                issues=edge_issues,
            )
            issues.extend(edge_issues)
            incident.append(exact)
        existing_remove_keys = {runtime_edge_key(edge) for edge in canonical_remove_edges}
        for edge in incident:
            if runtime_edge_key(edge) not in existing_remove_keys:
                canonical_remove_edges.append(edge)
                existing_remove_keys.add(runtime_edge_key(edge))
        removals.append(
            GraphPatchRemoveNode(
                ref={"node_id": node_id},
                node_type=node_type,
                schema_hash=node_schema_hash(node_type, node_info),
                expected_incident_edges=incident,
            )
        )

    graph_attachments: list[GraphPatchAttachment] = []
    validated_attachment_values = validated_attachment_values or {}
    for index, item in enumerate(request.attachments):
        target_edge = RefinementSpecEdge(
            source_alias=item.target_alias,
            source_output="unused",
            target_alias=item.target_alias,
            target_input=item.target_input,
        )
        endpoint, endpoint_issues = _target_endpoint(
            target_edge,
            existing=existing,
            created_types=selected_types,
            created_values=created_values,
            existing_values=existing_update_values,
            active_inputs=active_inputs,
            connected_inputs=connection_hints,
            catalog=catalog,
            path=f"attachments[{index}]",
        )
        issues.extend(endpoint_issues)
        key = (item.target_alias, item.target_input)
        attestation = validated_attachment_values.get(key)
        valid_attestation = bool(
            isinstance(attestation, Mapping)
            and attestation.get("widget_value") == item.image.widget_value()
            and isinstance(attestation.get("size_bytes"), int)
            and not isinstance(attestation.get("size_bytes"), bool)
            and attestation["size_bytes"] > 0
            and isinstance(attestation.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", attestation["sha256"])
        )
        if endpoint is None or not valid_attestation:
            if not valid_attestation:
                issues.append(
                    _issue(
                        "attachment_not_validated",
                        f"attachments[{index}]",
                        "The trusted chat attachment was not validated on disk.",
                    )
                )
            continue
        graph_attachments.append(
            GraphPatchAttachment(
                ref=endpoint.ref,
                input_index=endpoint.input_index,
                input=endpoint.input,
                type=endpoint.type,
                filename=item.image.filename,
                subfolder=item.image.subfolder,
                file_type=item.image.type,
                size_bytes=attestation["size_bytes"],
                sha256=attestation["sha256"],
            )
        )

    touched_existing_ids = {
        ref.node_id
        for edge in [*canonical_add_edges, *canonical_remove_edges]
        for ref in (edge.source.ref, edge.target.ref)
        if isinstance(ref, ExistingNodeRef)
    }
    touched_existing_ids.update(item.ref.node_id for item in updates)
    touched_existing_ids.update(item.ref.node_id for item in removals)
    touched_existing_ids.update(
        item.ref.node_id for item in graph_attachments if isinstance(item.ref, ExistingNodeRef)
    )
    assertions: list[GraphPatchNodeAssertion] = []
    for node_id in sorted(touched_existing_ids, key=lambda value: (type(value).__name__, str(value))):
        raw = workflow_nodes_by_id.get(_typed_id_key(node_id))
        if raw is None:
            issues.append(
                _issue(
                    "touched_existing_node_missing",
                    "assertions.nodes",
                    f"Touched node {node_id!r} is absent from the active workflow.",
                )
            )
            continue
        node_type = str(raw.get("type"))
        node_info = catalog.get(node_type)
        if isinstance(node_info, Mapping):
            assertions.append(
                GraphPatchNodeAssertion(
                    ref={"node_id": node_id},
                    node_type=node_type,
                    schema_hash=node_schema_hash(node_type, node_info),
                )
            )

    error_count = sum(item["severity"] == "error" for item in issues)
    if error_count:
        return {
            "valid": False,
            "compiler_schema": WORKFLOW_REFINEMENT_COMPILER_SCHEMA,
            "needs_choice": resolution.get("needs_choice", False)
            or inference_needs_choice
            or any(item["code"] == "existing_selector_ambiguous" for item in issues),
            "selection": selector_evidence,
            "resolution": resolution,
            "partner_review": partner_review,
            "inferred_routes": inferred_routes,
            "patch_hash": None,
            "plan": None,
            "apply_request": None,
            "issues": issues,
            "error_count": error_count,
            "warning_count": sum(item["severity"] == "warning" for item in issues),
        }

    patch_request = GraphPatchRequest(
        application_id=request.application_id,
        expected_workflow_identity=workflow_identity,
        expected_graph_hash=graph_hash,
        expected_catalog_hash=catalog_hash,
        graph=graph,
        assertions={"nodes": assertions, "edges": canonical_remove_edges},
        create_nodes=creates,
        update_nodes=updates,
        remove_edges=canonical_remove_edges,
        add_edges=canonical_add_edges,
        remove_nodes=removals,
        attachments=graph_attachments,
    )
    compiled = compile_graph_patch(
        patch_request,
        catalog,
        catalog_hash=catalog_hash,
        source=source,
    )
    combined_issues = [*issues, *compiled["issues"]]
    return {
        "valid": compiled["valid"],
        "compiler_schema": WORKFLOW_REFINEMENT_COMPILER_SCHEMA,
        "needs_choice": False,
        "selection": selector_evidence,
        "resolution": resolution,
        "partner_review": partner_review,
        "inferred_routes": inferred_routes,
        "patch_hash": compiled["patch_hash"],
        "plan": compiled["plan"],
        "apply_request": compiled["apply_request"],
        "expected_final": compiled.get("expected_final"),
        "catalog": compiled["catalog"],
        "issues": combined_issues,
        "error_count": sum(item["severity"] == "error" for item in combined_issues),
        "warning_count": sum(item["severity"] == "warning" for item in combined_issues),
    }


__all__ = [
    "CompileWorkflowRefinementSpecRequest",
    "ExistingNodeSelector",
    "RefinementSpecAttachment",
    "RefinementSpecEdge",
    "RefinementSpecUpdate",
    "WORKFLOW_REFINEMENT_COMPILER_SCHEMA",
    "compile_workflow_refinement_spec",
]
