"""Deterministic, schema-scoped capability and conversion-path facts.

This module turns the authoritative loaded ``/object_info`` catalog into a
small hypergraph.  A node profile is a hyperedge: it can consume several exact
input ports and produce several exact output ports in one operation.  The
bounded route search is deliberately advisory.  It can suggest intermediary
nodes to a workflow compiler, but it never authorizes a class, slot, value, or
mutation; the live compiler must still validate the resulting GraphPatch.

The implementation is intentionally local and lightweight.  It uses normalized
runtime schemas and schema-hash-scoped verified lessons as ranking priors.  It
does not use embeddings, web metadata, alphabetical tie breaking, or learned
facts as build authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from node_library import classify_node_origin, node_schema_hash
from workflow_schema_capabilities import (
    ActivationConstraint,
    Cardinality,
    CompatibilityStatus,
    InputCapability,
    NodeSchemaCapabilities,
    OutputCapability,
    materialize_inputs,
    normalize_node_schema,
)

CAPABILITY_GRAPH_SCHEMA = "fl-mcp.workflow-capability-graph.v1"
MAX_INTERMEDIARIES = 2
MAX_REPORTED_REJECTIONS = 16
MAX_SELECTOR_VARIANTS = 64

NodeOrigin = Literal["native", "custom", "partner", "unknown"]
RouteStatus = Literal[
    "direct",
    "resolved",
    "needs_choice",
    "unresolved",
    "extra_nodes_disallowed",
]
BindingMode = Literal["slot", "convert_widget"]
CardinalityEffect = Literal[
    "exact",
    "mapped_scalar_over_list",
    "scalar_to_list_input",
]

_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_HEAVY_TEXT_MARKERS = (
    "checkpoint loader",
    "diffusion model",
    "image generator",
    "video generator",
    "text to image",
    "text to video",
    "image to video",
    "sampler",
)
_HEAVY_COMFY_TYPES = {
    "MODEL",
    "CLIP",
    "CONDITIONING",
    "GUIDER",
    "SAMPLER",
    "SIGMAS",
    "NOISE",
}
_VERIFIED_EVIDENCE = {
    "atomic_canvas_application",
    "atomic_graph_patch_application",
}


def _sorted_types(values: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: (value.casefold(), value)))


def _clean_types(values: Collection[str], *, label: str) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must contain only non-empty type strings")
        result.add(value.strip())
    return frozenset(result)


@dataclass(frozen=True, order=True)
class RouteEndpoint:
    """One concrete Comfy type plus its native execution cardinality."""

    type: str
    cardinality: Cardinality = "scalar"

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("route endpoint type must be a non-empty string")
        object.__setattr__(self, "type", self.type.strip())
        if self.cardinality not in {"scalar", "list"}:
            raise ValueError("route endpoint cardinality must be 'scalar' or 'list'")


@dataclass(frozen=True)
class _EndpointMatch:
    source: RouteEndpoint
    target_cardinality: Cardinality
    effect: CardinalityEffect
    penalty: int


def _sorted_endpoints(values: Collection[RouteEndpoint]) -> tuple[RouteEndpoint, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda item: (item.type.casefold(), item.type, item.cardinality),
        )
    )


def _clean_endpoints(
    values: Collection[RouteEndpoint],
    *,
    label: str,
) -> frozenset[RouteEndpoint]:
    result: set[RouteEndpoint] = set()
    for value in values:
        if not isinstance(value, RouteEndpoint):
            raise TypeError(f"{label} must contain only RouteEndpoint values")
        result.add(value)
    return frozenset(result)


def _route_endpoints(
    types: Collection[str],
    endpoints: Collection[RouteEndpoint] | None,
    *,
    label: str,
) -> frozenset[RouteEndpoint]:
    cleaned_types = _clean_types(types, label=f"{label}_types")
    if endpoints is None:
        return frozenset(RouteEndpoint(value) for value in cleaned_types)
    if cleaned_types:
        raise ValueError(f"pass either {label}_types or {label}_endpoints, not both")
    return _clean_endpoints(endpoints, label=f"{label}_endpoints")


def _endpoint_match(
    accepted_types: Collection[str],
    target_cardinality: Cardinality,
    available: Collection[RouteEndpoint],
) -> tuple[_EndpointMatch | None, bool]:
    accepted = set(accepted_types)
    candidates: list[_EndpointMatch] = []
    for endpoint in available:
        if endpoint.type == "*" or (
            "*" not in accepted and endpoint.type not in accepted
        ):
            continue
        if endpoint.cardinality == target_cardinality:
            effect: CardinalityEffect = "exact"
            penalty = 0
        elif endpoint.cardinality == "list" and target_cardinality == "scalar":
            effect = "mapped_scalar_over_list"
            penalty = 1
        else:
            # Comfy represents every connected value as a list internally, so
            # INPUT_IS_LIST nodes natively receive a scalar source as a
            # one-element list.  It is valid, but less exact than matching the
            # declared cardinality.
            effect = "scalar_to_list_input"
            penalty = 1
        candidates.append(
            _EndpointMatch(
                source=endpoint,
                target_cardinality=target_cardinality,
                effect=effect,
                penalty=penalty,
            )
        )
    if not candidates:
        return None, False
    best_penalty = min(item.penalty for item in candidates)
    best = sorted(
        (item for item in candidates if item.penalty == best_penalty),
        key=lambda item: (
            item.source.type.casefold(),
            item.source.type,
            item.source.cardinality,
            item.effect,
        ),
    )
    return (best[0], len(best) > 1)


def _covers(
    available: Collection[RouteEndpoint],
    required: Collection[RouteEndpoint],
) -> bool:
    available_set = set(available)
    return all(
        any(
            (
                required_endpoint.type == "*" and candidate.type != "*"
                or candidate.type == required_endpoint.type
            )
            for candidate in available_set
        )
        for required_endpoint in required
    )


@dataclass(frozen=True)
class StableWidgetValue:
    """One schema-provided stable value needed by a synthesized converter."""

    path: str
    value: Any
    provenance: str


@dataclass(frozen=True)
class TransformInputPort:
    path: str
    name: str
    declaration_index: int
    occurrence_index: int
    accepted_types: tuple[str, ...]
    required: bool
    connectable: bool
    widget_convertible: bool
    cardinality: str
    matchtype_template_id: str | None
    group: str
    activation: tuple[ActivationConstraint, ...]
    activation_state: str
    stable_default: StableWidgetValue | None

    @property
    def data_input(self) -> bool:
        return self.connectable or self.widget_convertible


@dataclass(frozen=True)
class TransformOutputPort:
    index: int
    name: str
    produced_types: tuple[str, ...]
    cardinality: str
    matchtype_template_id: str | None


@dataclass(frozen=True)
class TransformProfile:
    """One schema-hash-scoped node hyperedge."""

    node_type: str
    schema_hash: str
    display_name: str
    category: str
    description: str
    origin: NodeOrigin
    api_node: bool
    output_node: bool
    deprecated: bool
    experimental: bool
    schema_status: CompatibilityStatus
    schema_issue_codes: tuple[str, ...]
    variant_key: str
    selector_values: tuple[StableWidgetValue, ...]
    nondefault_selector_count: int
    inputs: tuple[TransformInputPort, ...]
    default_active_inputs: tuple[TransformInputPort, ...]
    outputs: tuple[TransformOutputPort, ...]
    heavy: bool
    heavy_reasons: tuple[str, ...]

    @property
    def produced_types(self) -> tuple[str, ...]:
        return _sorted_types(
            {
                output_type
                for output in self.outputs
                for output_type in output.produced_types
            }
        )

    @property
    def accepted_types(self) -> tuple[str, ...]:
        return _sorted_types(
            {
                input_type
                for input_port in self.default_active_inputs
                if input_port.data_input
                for input_type in input_port.accepted_types
            }
        )

    def input_ports_named(self, name: str) -> tuple[TransformInputPort, ...]:
        """Return exact or unique-suffix schema inputs without fuzzy guessing."""

        exact = tuple(
            item for item in self.inputs if item.path == name or item.name == name
        )
        if exact:
            return exact
        suffix = f".{name}"
        return tuple(item for item in self.inputs if item.path.endswith(suffix))

    def output_ports_named(self, name: str) -> tuple[TransformOutputPort, ...]:
        return tuple(item for item in self.outputs if item.name == name)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityGraphIssue:
    code: str
    node_type: str
    message: str


@dataclass(frozen=True)
class VerifiedCapabilityLesson:
    """A caller-provided, schema-scoped ranking hint from a verified apply."""

    node_type: str
    schema_hash: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class RoutePolicy:
    """Fail-closed policy for automatic intermediary selection."""

    max_intermediaries: int = MAX_INTERMEDIARIES
    allow_extra_nodes: bool = True
    exact_intermediary_node_types: frozenset[str] = field(default_factory=frozenset)
    explicitly_allowed_node_types: frozenset[str] = field(default_factory=frozenset)
    allow_partner: bool = False
    allow_api: bool = False
    allow_heavy: bool = False
    allow_output_nodes: bool = False
    allow_adapter_required: bool = False
    allow_unknown_origin: bool = False
    allow_deprecated: bool = False
    allow_experimental: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_intermediaries, bool) or not isinstance(
            self.max_intermediaries, int
        ):
            raise TypeError("max_intermediaries must be an integer")
        if not 0 <= self.max_intermediaries <= MAX_INTERMEDIARIES:
            raise ValueError(
                f"max_intermediaries must be between 0 and {MAX_INTERMEDIARIES}"
            )
        for name in (
            "exact_intermediary_node_types",
            "explicitly_allowed_node_types",
        ):
            values = getattr(self, name)
            if not isinstance(values, frozenset):
                values = frozenset(values)
                object.__setattr__(self, name, values)
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty node type strings")


@dataclass(frozen=True)
class RouteInputBinding:
    path: str
    declaration_index: int
    occurrence_index: int
    input_type: str
    source_cardinality: Cardinality
    target_cardinality: Cardinality
    cardinality_effect: CardinalityEffect
    mode: BindingMode
    required: bool


@dataclass(frozen=True)
class RouteStep:
    node_type: str
    schema_hash: str
    origin: NodeOrigin
    input_bindings: tuple[RouteInputBinding, ...]
    selector_values: tuple[StableWidgetValue, ...]
    nondefault_selector_count: int
    required_stable_widget_values: tuple[StableWidgetValue, ...]
    produced_outputs: tuple[TransformOutputPort, ...]
    produced_endpoints: tuple[RouteEndpoint, ...]
    produced_types: tuple[str, ...]
    verified_lesson_count: int
    evidence: tuple[str, ...]


@dataclass(frozen=True, order=True)
class RouteCost:
    intermediary_count: int
    cardinality_penalty: int
    unverified_steps: int
    origin_penalty: int
    defaulted_required_widget_count: int
    nondefault_selector_count: int = 0


@dataclass(frozen=True)
class ConversionRoute:
    steps: tuple[RouteStep, ...]
    resulting_endpoints: tuple[RouteEndpoint, ...]
    resulting_types: tuple[str, ...]
    cost: RouteCost
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RouteRejection:
    node_type: str
    schema_hash: str
    code: str
    message: str
    goal_types_produced: tuple[str, ...] = ()
    missing_input_types: tuple[str, ...] = ()
    missing_widget_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteResult:
    status: RouteStatus
    valid: bool
    needs_choice: bool
    available_types: tuple[str, ...]
    required_types: tuple[str, ...]
    available_endpoints: tuple[RouteEndpoint, ...]
    required_endpoints: tuple[RouteEndpoint, ...]
    selected: ConversionRoute | None
    choices: tuple[ConversionRoute, ...]
    rejections: tuple[RouteRejection, ...]
    accepted_verified_lesson_count: int
    ignored_verified_lesson_count: int
    issue_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ApplicationFailure:
    code: str
    message: str
    missing_input_types: tuple[str, ...] = ()
    missing_widget_paths: tuple[str, ...] = ()


def _input_port(
    capability: InputCapability,
    *,
    activation_state: str,
    selector_value: StableWidgetValue | None = None,
) -> TransformInputPort:
    default = selector_value or (
        StableWidgetValue(
            path=capability.path,
            value=capability.default.value,
            provenance=capability.default.provenance,
        )
        if capability.default.available
        else None
    )
    return TransformInputPort(
        path=capability.path,
        name=capability.name,
        declaration_index=capability.declaration_index,
        occurrence_index=capability.occurrence_index,
        accepted_types=capability.accepted_types,
        required=capability.required,
        connectable=capability.connectable,
        widget_convertible=capability.widget_convertible,
        cardinality=capability.cardinality,
        matchtype_template_id=(
            capability.matchtype.template_id
            if capability.matchtype is not None
            else None
        ),
        group=capability.group,
        activation=capability.activation,
        activation_state=activation_state,
        stable_default=default,
    )


def _output_port(capability: OutputCapability) -> TransformOutputPort:
    return TransformOutputPort(
        index=capability.index,
        name=capability.name,
        produced_types=capability.produced_types,
        cardinality=capability.cardinality,
        matchtype_template_id=capability.matchtype_template_id,
    )


def _heavy_classification(
    node_type: str,
    node_info: Mapping[str, Any],
    *,
    accepted_types: Collection[str],
    produced_types: Collection[str],
) -> tuple[bool, tuple[str, ...]]:
    """Conservatively identify model/generation nodes unsuitable as adapters."""

    text = " ".join(
        str(value or "")
        for value in (
            node_type,
            node_info.get("display_name"),
            node_info.get("category"),
            node_info.get("description"),
        )
    ).casefold()
    normalized_text = " ".join(_WORD_PATTERN.findall(text))
    reasons = [
        f"semantic_marker:{marker}"
        for marker in _HEAVY_TEXT_MARKERS
        if marker in normalized_text
    ]
    heavy_types = (_HEAVY_COMFY_TYPES & set(accepted_types)) | (
        _HEAVY_COMFY_TYPES & set(produced_types)
    )
    reasons.extend(f"model_type:{value}" for value in _sorted_types(heavy_types))
    return bool(reasons), tuple(reasons)


class _SelectorVariantError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _selector_assignment_key(values: Mapping[str, Any]) -> str:
    return json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _selector_assignments(
    capabilities: NodeSchemaCapabilities,
) -> tuple[dict[str, Any], ...]:
    """Enumerate finite active selector branches without a Cartesian explosion."""

    completed: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(values: dict[str, Any]) -> None:
        materialized = materialize_inputs(capabilities, values=values)
        active_selectors = [
            item.capability
            for item in materialized
            if item.capability.kind == "dynamic_selector"
        ]
        by_path: dict[str, InputCapability] = {}
        for selector in active_selectors:
            previous = by_path.get(selector.path)
            if previous is not None and previous.declaration_index != selector.declaration_index:
                raise _SelectorVariantError(
                    "ambiguous_dynamic_selector_branch",
                    f"Active dynamic selector path {selector.path!r} is duplicated.",
                )
            by_path[selector.path] = selector
        pending = [
            selector
            for selector in sorted(
                by_path.values(),
                key=lambda item: (item.declaration_index, item.path),
            )
            if selector.path not in values
        ]
        if not pending:
            key = _selector_assignment_key(values)
            if key not in seen:
                if len(completed) >= MAX_SELECTOR_VARIANTS:
                    raise _SelectorVariantError(
                        "dynamic_selector_variant_limit_exceeded",
                        "The node exposes more than "
                        f"{MAX_SELECTOR_VARIANTS} active selector variants.",
                    )
                seen.add(key)
                completed.append(dict(values))
            return

        selector = pending[0]
        options = list(selector.enum_options)
        if not options:
            raise _SelectorVariantError(
                "dynamic_selector_options_unavailable",
                f"Dynamic selector {selector.path!r} has no finite options.",
            )
        if selector.default.available and selector.default.value in options:
            options.remove(selector.default.value)
            options.insert(0, selector.default.value)
        for option in options:
            walk({**values, selector.path: option})

    walk({})
    return tuple(completed or ({},))


def _derive_transform_profile(
    node_type: str,
    schema: dict[str, Any],
    capabilities: NodeSchemaCapabilities,
    selector_assignment: Mapping[str, Any],
) -> TransformProfile:
    materialized_items = materialize_inputs(
        capabilities,
        values=selector_assignment,
    )
    materialized = {
        item.capability.declaration_index: item
        for item in materialized_items
    }
    active_selectors = {
        item.capability.path: item.capability
        for item in materialized_items
        if item.capability.kind == "dynamic_selector"
    }
    selector_values: list[StableWidgetValue] = []
    nondefault_selector_count = 0
    for path, value in sorted(selector_assignment.items()):
        selector = active_selectors.get(path)
        if selector is None:
            raise _SelectorVariantError(
                "inactive_dynamic_selector_assignment",
                f"Selector assignment {path!r} is inactive in its own branch.",
            )
        is_default = selector.default.available and selector.default.value == value
        if not is_default:
            nondefault_selector_count += 1
        selector_values.append(
            StableWidgetValue(
                path=path,
                value=value,
                provenance=(
                    "dynamic_selector_default"
                    if is_default
                    else "dynamic_selector_branch"
                ),
            )
        )
    selector_by_path = {item.path: item for item in selector_values}
    inputs = tuple(
        _input_port(
            item,
            activation_state=(
                materialized[item.declaration_index].activation_state
                if item.declaration_index in materialized
                else "inactive"
            ),
            selector_value=selector_by_path.get(item.path),
        )
        for item in capabilities.inputs
        if not item.hidden
    )
    default_active_inputs = tuple(
        item for item in inputs if item.activation_state != "inactive"
    )
    outputs = tuple(_output_port(item) for item in capabilities.outputs)
    accepted_types = {
        input_type
        for item in default_active_inputs
        if item.data_input
        for input_type in item.accepted_types
    }
    produced_types = {
        output_type for item in outputs for output_type in item.produced_types
    }
    heavy, heavy_reasons = _heavy_classification(
        node_type,
        schema,
        accepted_types=accepted_types,
        produced_types=produced_types,
    )
    origin = classify_node_origin(schema)
    assignment_key = _selector_assignment_key(selector_assignment)
    variant_key = (
        "default"
        if nondefault_selector_count == 0
        else hashlib.sha256(assignment_key.encode("utf-8")).hexdigest()[:16]
    )
    return TransformProfile(
        node_type=node_type,
        schema_hash=node_schema_hash(node_type, schema),
        display_name=str(schema.get("display_name") or node_type),
        category=str(schema.get("category") or ""),
        description=str(schema.get("description") or ""),
        origin=origin,  # type: ignore[arg-type]
        api_node=bool(schema.get("api_node")),
        output_node=bool(schema.get("output_node")),
        deprecated=bool(schema.get("deprecated")),
        experimental=bool(schema.get("experimental")),
        schema_status=capabilities.classification.status,
        schema_issue_codes=tuple(
            dict.fromkeys(reason.code for reason in capabilities.classification.reasons)
        ),
        variant_key=variant_key,
        selector_values=tuple(selector_values),
        nondefault_selector_count=nondefault_selector_count,
        inputs=inputs,
        default_active_inputs=default_active_inputs,
        outputs=outputs,
        heavy=heavy,
        heavy_reasons=heavy_reasons,
    )


def derive_transform_profiles(
    node_type: str,
    node_info: Mapping[str, Any],
) -> tuple[TransformProfile, ...]:
    """Derive every bounded, viable selector-specific hyperedge for one class."""

    if not isinstance(node_type, str) or not node_type:
        raise ValueError("node_type must be a non-empty string")
    if not isinstance(node_info, Mapping):
        raise TypeError("node_info must be a mapping")
    schema = dict(node_info)
    capabilities = normalize_node_schema(node_type, schema)
    return tuple(
        _derive_transform_profile(
            node_type,
            schema,
            capabilities,
            selector_assignment,
        )
        for selector_assignment in _selector_assignments(capabilities)
    )


def derive_transform_profile(
    node_type: str,
    node_info: Mapping[str, Any],
) -> TransformProfile:
    """Derive the stable default-branch profile for backwards compatibility."""

    return derive_transform_profiles(node_type, node_info)[0]


def _origin_penalty(origin: NodeOrigin) -> int:
    return {"native": 0, "custom": 1, "unknown": 2, "partner": 3}[origin]


def _policy_rejection(
    profile: TransformProfile,
    policy: RoutePolicy,
) -> _ApplicationFailure | None:
    explicit = profile.node_type in policy.explicitly_allowed_node_types
    if (
        policy.exact_intermediary_node_types
        and profile.node_type not in policy.exact_intermediary_node_types
    ):
        return _ApplicationFailure(
            "not_in_exact_node_set",
            "The node is not in the exact intermediary allowlist.",
        )
    if profile.schema_status == "unsupported":
        return _ApplicationFailure(
            "unsupported_schema",
            "The loaded schema is classified unsupported.",
        )
    if (
        profile.schema_status == "adapter_required"
        and not policy.allow_adapter_required
        and not explicit
    ):
        return _ApplicationFailure(
            "schema_adapter_required",
            "Automatic routing excludes schemas needing a runtime adapter.",
        )
    if profile.deprecated and not policy.allow_deprecated and not explicit:
        return _ApplicationFailure("deprecated_node", "Deprecated nodes are excluded.")
    if profile.experimental and not policy.allow_experimental and not explicit:
        return _ApplicationFailure(
            "experimental_node",
            "Experimental nodes are excluded from automatic conversion.",
        )
    if profile.origin == "partner" and not policy.allow_partner and not explicit:
        return _ApplicationFailure(
            "partner_node_excluded",
            "Partner nodes require explicit intermediary permission.",
        )
    if profile.api_node and not policy.allow_api and not explicit:
        return _ApplicationFailure(
            "api_node_excluded",
            "API nodes require explicit intermediary permission.",
        )
    if (
        profile.origin == "unknown"
        and not policy.allow_unknown_origin
        and not explicit
    ):
        return _ApplicationFailure(
            "unknown_origin_excluded",
            "Unknown-provenance nodes are excluded from automatic conversion.",
        )
    if profile.heavy and not policy.allow_heavy and not explicit:
        return _ApplicationFailure(
            "heavy_node_excluded",
            "Model and generation nodes are not automatic conversion adapters.",
        )
    if profile.output_node and not policy.allow_output_nodes and not explicit:
        return _ApplicationFailure(
            "output_node_excluded",
            "Terminal output nodes are not automatic conversion adapters.",
        )
    if not profile.outputs:
        return _ApplicationFailure(
            "no_outputs",
            "The node has no output and cannot advance a conversion route.",
        )
    return None


def _accepted_lesson_counts(
    profiles: Mapping[str, TransformProfile],
    lessons: Sequence[VerifiedCapabilityLesson],
) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    ignored = 0
    for lesson in lessons:
        profile = (
            profiles.get(lesson.node_type)
            if isinstance(lesson.node_type, str)
            else None
        )
        payload = lesson.payload
        evidence = payload.get("evidence") if isinstance(payload, Mapping) else None
        source_node_type = (
            payload.get("source_node_type") if isinstance(payload, Mapping) else None
        )
        target_node_type = (
            payload.get("target_node_type") if isinstance(payload, Mapping) else None
        )
        source_schema_hash = (
            payload.get("source_schema_hash") if isinstance(payload, Mapping) else None
        )
        target_schema_hash = (
            payload.get("target_schema_hash") if isinstance(payload, Mapping) else None
        )
        source_profile = (
            profiles.get(source_node_type)
            if isinstance(source_node_type, str)
            else None
        )
        target_profile = (
            profiles.get(target_node_type)
            if isinstance(target_node_type, str)
            else None
        )
        if (
            profile is None
            or profile.schema_hash != lesson.schema_hash
            or not isinstance(payload, Mapping)
            or not isinstance(evidence, str)
            or evidence not in _VERIFIED_EVIDENCE
            or not isinstance(source_node_type, str)
            or not source_node_type
            or not isinstance(target_node_type, str)
            or not target_node_type
            or not isinstance(source_schema_hash, str)
            or not isinstance(target_schema_hash, str)
            or source_profile is None
            or target_profile is None
            or source_profile.schema_hash != source_schema_hash
            or target_profile.schema_hash != target_schema_hash
            or (
                lesson.node_type == source_node_type
                and lesson.schema_hash != source_schema_hash
            )
            or (
                lesson.node_type == target_node_type
                and lesson.schema_hash != target_schema_hash
            )
            or lesson.node_type not in (source_node_type, target_node_type)
        ):
            ignored += 1
            continue
        counts[lesson.node_type] = counts.get(lesson.node_type, 0) + 1
    return counts, ignored


def _apply_profile(
    profile: TransformProfile,
    available_endpoints: frozenset[RouteEndpoint],
    *,
    verified_lesson_count: int,
) -> tuple[
    RouteStep | None,
    frozenset[RouteEndpoint],
    _ApplicationFailure | None,
]:
    bindings: list[RouteInputBinding] = []
    stable_values: list[StableWidgetValue] = []
    missing_types: set[str] = set()
    missing_widgets: list[str] = []
    matchtype_bindings: dict[str, str] = {}

    for input_port in profile.default_active_inputs:
        if input_port.activation_state == "conditional" and input_port.required:
            missing_widgets.append(input_port.path)
            continue
        if input_port.data_input:
            # Optional sockets express capabilities, not intent.  Connecting an
            # implicit primary source to one merely because its type matches can
            # silently change a node's behavior (reference images, masks, audio,
            # controls, and similar side inputs).  Explicit workflow edges remain
            # the authority for every optional data input.
            if not input_port.required:
                continue
            endpoint_match, endpoint_ambiguous = _endpoint_match(
                input_port.accepted_types,
                input_port.cardinality,
                available_endpoints,
            )
            if endpoint_ambiguous:
                return (
                    None,
                    available_endpoints,
                    _ApplicationFailure(
                        "ambiguous_input_binding",
                        "Multiple equally exact source endpoints match one converter input.",
                        missing_input_types=input_port.accepted_types,
                    ),
                )
            if endpoint_match is not None:
                bindings.append(
                    RouteInputBinding(
                        path=input_port.path,
                        declaration_index=input_port.declaration_index,
                        occurrence_index=input_port.occurrence_index,
                        input_type=endpoint_match.source.type,
                        source_cardinality=endpoint_match.source.cardinality,
                        target_cardinality=endpoint_match.target_cardinality,
                        cardinality_effect=endpoint_match.effect,
                        mode=(
                            "slot" if input_port.connectable else "convert_widget"
                        ),
                        required=input_port.required,
                    )
                )
                if input_port.matchtype_template_id is not None:
                    previous = matchtype_bindings.get(input_port.matchtype_template_id)
                    if previous is not None and previous != endpoint_match.source.type:
                        return (
                            None,
                            available_endpoints,
                            _ApplicationFailure(
                                "conflicting_matchtype_bindings",
                                "Inputs bound to one match-type template disagree on type.",
                                missing_input_types=(
                                    previous,
                                    endpoint_match.source.type,
                                ),
                            ),
                        )
                    matchtype_bindings[input_port.matchtype_template_id] = (
                        endpoint_match.source.type
                    )
                continue
            if (
                input_port.widget_convertible
                and input_port.stable_default is not None
            ):
                stable_values.append(input_port.stable_default)
            else:
                missing_types.update(input_port.accepted_types)
            continue

        if input_port.required:
            if input_port.stable_default is not None:
                stable_values.append(input_port.stable_default)
            else:
                missing_widgets.append(input_port.path)

    if missing_types or missing_widgets:
        return (
            None,
            available_endpoints,
            _ApplicationFailure(
                "unsatisfied_required_inputs",
                "The converter has required inputs with no upstream type or stable value.",
                missing_input_types=_sorted_types(missing_types),
                missing_widget_paths=tuple(sorted(set(missing_widgets))),
            ),
        )
    if not bindings:
        return (
            None,
            available_endpoints,
            _ApplicationFailure(
                "no_bound_upstream_input",
                "The node would generate an unrelated value instead of converting the source.",
            ),
        )

    mapped_execution = any(
        binding.cardinality_effect == "mapped_scalar_over_list"
        for binding in bindings
    )
    produced_endpoints: set[RouteEndpoint] = set()
    unresolved_output_paths: list[str] = []
    for output in profile.outputs:
        if output.matchtype_template_id is not None:
            bound_type = matchtype_bindings.get(output.matchtype_template_id)
            if bound_type is None:
                unresolved_output_paths.append(f"output[{output.index}]")
            else:
                produced_endpoints.add(
                    RouteEndpoint(
                        bound_type,
                        "list"
                        if mapped_execution or output.cardinality == "list"
                        else "scalar",
                    )
                )
            continue
        concrete_types = set(output.produced_types) - {"*"}
        produced_endpoints.update(
            RouteEndpoint(
                output_type,
                "list"
                if mapped_execution or output.cardinality == "list"
                else "scalar",
            )
            for output_type in concrete_types
        )
        if "*" in output.produced_types:
            unresolved_output_paths.append(f"output[{output.index}]")
    if not produced_endpoints:
        return (
            None,
            available_endpoints,
            _ApplicationFailure(
                "unbound_polymorphic_output",
                "The schema does not bind a polymorphic output to a concrete input type.",
                missing_widget_paths=tuple(unresolved_output_paths),
            ),
        )
    resulting_endpoints = available_endpoints | produced_endpoints
    if resulting_endpoints == available_endpoints:
        return (
            None,
            available_endpoints,
            _ApplicationFailure(
                "no_new_output_type",
                "The node does not add a type needed by a conversion route.",
            ),
        )
    evidence = [
        f"schema:{profile.schema_hash}",
        f"origin:{profile.origin}",
    ]
    if verified_lesson_count:
        evidence.append(f"schema_valid_verified_lessons:{verified_lesson_count}")
    step = RouteStep(
        node_type=profile.node_type,
        schema_hash=profile.schema_hash,
        origin=profile.origin,
        input_bindings=tuple(bindings),
        selector_values=profile.selector_values,
        nondefault_selector_count=profile.nondefault_selector_count,
        required_stable_widget_values=tuple(stable_values),
        produced_outputs=profile.outputs,
        produced_endpoints=_sorted_endpoints(produced_endpoints),
        produced_types=_sorted_types(
            {item.type for item in produced_endpoints}
        ),
        verified_lesson_count=verified_lesson_count,
        evidence=tuple(evidence),
    )
    return step, resulting_endpoints, None


def _route_cost(steps: Sequence[RouteStep]) -> RouteCost:
    return RouteCost(
        intermediary_count=len(steps),
        cardinality_penalty=sum(
            binding.cardinality_effect != "exact"
            for step in steps
            for binding in step.input_bindings
        ),
        unverified_steps=sum(step.verified_lesson_count == 0 for step in steps),
        origin_penalty=sum(_origin_penalty(step.origin) for step in steps),
        defaulted_required_widget_count=sum(
            len(step.required_stable_widget_values) for step in steps
        ),
        nondefault_selector_count=sum(
            step.nondefault_selector_count for step in steps
        ),
    )


def _route(
    steps: tuple[RouteStep, ...],
    resulting_endpoints: frozenset[RouteEndpoint],
) -> ConversionRoute:
    evidence = tuple(
        dict.fromkeys(
            evidence_item
            for step in steps
            for evidence_item in step.evidence
        )
    )
    return ConversionRoute(
        steps=steps,
        resulting_endpoints=_sorted_endpoints(resulting_endpoints),
        resulting_types=_sorted_types(
            {item.type for item in resulting_endpoints}
        ),
        cost=_route_cost(steps),
        evidence=evidence,
    )


def _route_signature(route: ConversionRoute) -> tuple[Any, ...]:
    return tuple(
        (
            step.node_type,
            step.schema_hash,
            tuple(
                (item.path, _selector_assignment_key({"value": item.value}))
                for item in step.selector_values
            ),
            tuple(
                (
                    binding.path,
                    binding.input_type,
                    binding.source_cardinality,
                    binding.target_cardinality,
                    binding.cardinality_effect,
                    binding.mode,
                )
                for binding in step.input_bindings
            ),
        )
        for step in route.steps
    )


def _rejection_sort_key(item: RouteRejection) -> tuple[Any, ...]:
    return (
        -len(item.goal_types_produced),
        len(item.missing_input_types) + len(item.missing_widget_paths),
        item.code,
        item.node_type.casefold(),
        item.node_type,
    )


@dataclass(frozen=True)
class CapabilityGraph:
    """Pure catalog-derived graph with bounded deterministic route search."""

    profiles: tuple[TransformProfile, ...]
    issues: tuple[CapabilityGraphIssue, ...] = ()
    schema: str = CAPABILITY_GRAPH_SCHEMA

    @classmethod
    def from_catalog(cls, catalog: Mapping[str, Any]) -> CapabilityGraph:
        if not isinstance(catalog, Mapping):
            raise TypeError("catalog must be a mapping")
        profiles: list[TransformProfile] = []
        issues: list[CapabilityGraphIssue] = []
        for node_type in sorted(catalog, key=lambda value: (str(value).casefold(), str(value))):
            node_info = catalog[node_type]
            if not isinstance(node_type, str) or not node_type:
                issues.append(
                    CapabilityGraphIssue(
                        "invalid_node_type",
                        str(node_type),
                        "Catalog node types must be non-empty strings.",
                    )
                )
                continue
            if not isinstance(node_info, Mapping):
                issues.append(
                    CapabilityGraphIssue(
                        "invalid_node_schema",
                        node_type,
                        "Catalog node schemas must be mappings.",
                    )
                )
                continue
            try:
                profiles.extend(derive_transform_profiles(node_type, node_info))
            except _SelectorVariantError as exc:
                issues.append(
                    CapabilityGraphIssue(
                        exc.code,
                        node_type,
                        str(exc),
                    )
                )
            except (TypeError, ValueError) as exc:
                issues.append(
                    CapabilityGraphIssue(
                        "profile_derivation_failed",
                        node_type,
                        str(exc),
                    )
                )
        return cls(profiles=tuple(profiles), issues=tuple(issues))

    def profile(self, node_type: str) -> TransformProfile | None:
        """Return the stable default variant used for schema/output lookup."""

        return next(
            (item for item in self.profiles if item.node_type == node_type),
            None,
        )

    def find_route(
        self,
        *,
        available_types: Collection[str] = (),
        required_types: Collection[str] = (),
        available_endpoints: Collection[RouteEndpoint] | None = None,
        required_endpoints: Collection[RouteEndpoint] | None = None,
        policy: RoutePolicy | None = None,
        verified_lessons: Sequence[VerifiedCapabilityLesson] = (),
    ) -> RouteResult:
        """Find zero to two intermediary hyperedges without guessing on ties."""

        policy = policy or RoutePolicy()
        available = _route_endpoints(
            available_types,
            available_endpoints,
            label="available",
        )
        required = _route_endpoints(
            required_types,
            required_endpoints,
            label="required",
        )
        if not required:
            raise ValueError("required route endpoints must not be empty")
        available_type_names = _sorted_types({item.type for item in available})
        required_type_names = _sorted_types({item.type for item in required})
        sorted_available_endpoints = _sorted_endpoints(available)
        sorted_required_endpoints = _sorted_endpoints(required)
        by_type: dict[str, TransformProfile] = {}
        for profile in self.profiles:
            by_type.setdefault(profile.node_type, profile)
        lesson_counts, ignored_lessons = _accepted_lesson_counts(
            by_type,
            verified_lessons,
        )
        accepted_lesson_count = sum(lesson_counts.values())

        direct = ConversionRoute(
            steps=(),
            resulting_endpoints=sorted_available_endpoints,
            resulting_types=available_type_names,
            cost=RouteCost(
                intermediary_count=0,
                cardinality_penalty=0,
                unverified_steps=0,
                origin_penalty=0,
                defaulted_required_widget_count=0,
            ),
            evidence=("direct_type_compatibility",),
        )
        if _covers(available, required):
            return RouteResult(
                status="direct",
                valid=True,
                needs_choice=False,
                available_types=available_type_names,
                required_types=required_type_names,
                available_endpoints=sorted_available_endpoints,
                required_endpoints=sorted_required_endpoints,
                selected=direct,
                choices=(direct,),
                rejections=(),
                accepted_verified_lesson_count=accepted_lesson_count,
                ignored_verified_lesson_count=ignored_lessons,
                issue_codes=(),
            )
        if not policy.allow_extra_nodes or policy.max_intermediaries == 0:
            return RouteResult(
                status="extra_nodes_disallowed",
                valid=False,
                needs_choice=False,
                available_types=available_type_names,
                required_types=required_type_names,
                available_endpoints=sorted_available_endpoints,
                required_endpoints=sorted_required_endpoints,
                selected=None,
                choices=(),
                rejections=(),
                accepted_verified_lesson_count=accepted_lesson_count,
                ignored_verified_lesson_count=ignored_lessons,
                issue_codes=("extra_nodes_disallowed",),
            )

        eligible: list[TransformProfile] = []
        rejections: dict[tuple[str, str], RouteRejection] = {}
        for profile in self.profiles:
            policy_failure = _policy_rejection(profile, policy)
            if policy_failure is not None:
                goal_produced = _sorted_types(
                    set(profile.produced_types) & set(required_type_names)
                )
                if goal_produced or (
                    policy.exact_intermediary_node_types
                    and profile.node_type in policy.exact_intermediary_node_types
                ):
                    rejection = RouteRejection(
                        node_type=profile.node_type,
                        schema_hash=profile.schema_hash,
                        code=policy_failure.code,
                        message=policy_failure.message,
                        goal_types_produced=goal_produced,
                    )
                    rejections[(profile.node_type, policy_failure.code)] = rejection
                continue
            eligible.append(profile)

        # Backward type relevance keeps the two-hop search lightweight without
        # changing correctness for a bounded schema-type route.
        relevant_types = set(required_type_names)
        for _ in range(policy.max_intermediaries):
            expanded = set(relevant_types)
            for profile in eligible:
                if not (set(profile.produced_types) & relevant_types):
                    continue
                for input_port in profile.default_active_inputs:
                    if input_port.data_input:
                        expanded.update(input_port.accepted_types)
            if expanded == relevant_types:
                break
            relevant_types = expanded
        eligible = [
            profile
            for profile in eligible
            if set(profile.produced_types) & relevant_types
        ]

        frontier: list[
            tuple[frozenset[RouteEndpoint], tuple[RouteStep, ...]]
        ] = [
            (available, ())
        ]
        candidates: list[ConversionRoute] = []
        seen_frontier: set[
            tuple[frozenset[RouteEndpoint], tuple[Any, ...]]
        ] = set()
        for _depth in range(1, policy.max_intermediaries + 1):
            next_frontier: list[
                tuple[frozenset[RouteEndpoint], tuple[RouteStep, ...]]
            ] = []
            for state, prior_steps in frontier:
                used_types = {step.node_type for step in prior_steps}
                for profile in eligible:
                    if profile.node_type in used_types:
                        continue
                    step, next_state, failure = _apply_profile(
                        profile,
                        state,
                        verified_lesson_count=lesson_counts.get(profile.node_type, 0),
                    )
                    if failure is not None:
                        goal_produced = _sorted_types(
                            set(profile.produced_types) & set(required_type_names)
                        )
                        if goal_produced or failure.code == "unsatisfied_required_inputs":
                            rejection = RouteRejection(
                                node_type=profile.node_type,
                                schema_hash=profile.schema_hash,
                                code=failure.code,
                                message=failure.message,
                                goal_types_produced=goal_produced,
                                missing_input_types=failure.missing_input_types,
                                missing_widget_paths=failure.missing_widget_paths,
                            )
                            rejections[(profile.node_type, failure.code)] = rejection
                        continue
                    if step is None:
                        continue
                    steps = (*prior_steps, step)
                    route = _route(steps, next_state)
                    if _covers(next_state, required):
                        candidates.append(route)
                        continue
                    frontier_key = (
                        next_state,
                        _route_signature(route),
                    )
                    if frontier_key not in seen_frontier:
                        seen_frontier.add(frontier_key)
                        next_frontier.append((next_state, steps))
            if candidates:
                break
            frontier = next_frontier
            if not frontier:
                break

        unique_candidates = {
            _route_signature(candidate): candidate for candidate in candidates
        }
        ranked = sorted(
            unique_candidates.values(),
            key=lambda item: (
                item.cost,
                tuple(step.node_type.casefold() for step in item.steps),
                tuple(step.node_type for step in item.steps),
            ),
        )
        ranked_rejections = tuple(
            sorted(rejections.values(), key=_rejection_sort_key)[
                :MAX_REPORTED_REJECTIONS
            ]
        )
        if not ranked:
            issue_codes = tuple(
                dict.fromkeys(
                    ["conversion_route_unresolved"]
                    + [item.code for item in ranked_rejections]
                )
            )
            return RouteResult(
                status="unresolved",
                valid=False,
                needs_choice=False,
                available_types=available_type_names,
                required_types=required_type_names,
                available_endpoints=sorted_available_endpoints,
                required_endpoints=sorted_required_endpoints,
                selected=None,
                choices=(),
                rejections=ranked_rejections,
                accepted_verified_lesson_count=accepted_lesson_count,
                ignored_verified_lesson_count=ignored_lessons,
                issue_codes=issue_codes,
            )

        best_cost = ranked[0].cost
        best = tuple(item for item in ranked if item.cost == best_cost)
        if len(best) != 1:
            return RouteResult(
                status="needs_choice",
                valid=False,
                needs_choice=True,
                available_types=available_type_names,
                required_types=required_type_names,
                available_endpoints=sorted_available_endpoints,
                required_endpoints=sorted_required_endpoints,
                selected=None,
                choices=tuple(ranked),
                rejections=ranked_rejections,
                accepted_verified_lesson_count=accepted_lesson_count,
                ignored_verified_lesson_count=ignored_lessons,
                issue_codes=("ambiguous_conversion_route",),
            )
        return RouteResult(
            status="resolved",
            valid=True,
            needs_choice=False,
            available_types=available_type_names,
            required_types=required_type_names,
            available_endpoints=sorted_available_endpoints,
            required_endpoints=sorted_required_endpoints,
            selected=best[0],
            choices=tuple(ranked),
            rejections=ranked_rejections,
            accepted_verified_lesson_count=accepted_lesson_count,
            ignored_verified_lesson_count=ignored_lessons,
            issue_codes=(),
        )


def build_capability_graph(catalog: Mapping[str, Any]) -> CapabilityGraph:
    """Convenience wrapper for compiler integration."""

    return CapabilityGraph.from_catalog(catalog)
