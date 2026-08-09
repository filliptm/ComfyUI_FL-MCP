"""Normalize ComfyUI ``/object_info`` schemas into reusable capability facts.

The workflow compiler and graph-patch validator need the same answer to a few
fundamental questions: which inputs are widgets or sockets, which dynamic
selector activates a dotted input, whether a slot carries a list, and whether
two polymorphic slots can connect.  ComfyUI exposes those facts through several
schema generations and custom-node conventions.  This module provides one
pure, deterministic interpretation without reading the canvas or mutating a
workflow.

Normalization is intentionally lossless for the metadata needed by future
validators.  It does not claim that the current canvas adapter can apply every
normalized feature.  ``CompatibilityClassification`` separates schemas that
are directly supported from features that need a graph-scoped/frontend adapter
and malformed or unknown features that must fail closed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

SCHEMA_CAPABILITY_CONTRACT = "fl-mcp.workflow-schema-capabilities.v1"

CompatibilityStatus = Literal["supported", "adapter_required", "unsupported"]
InputGroup = Literal["required", "optional", "hidden"]
InputKind = Literal[
    "widget",
    "socket",
    "dynamic_selector",
    "dynamic_slot",
    "matchtype",
    "hidden",
]
Cardinality = Literal["scalar", "list"]
ActivationState = Literal["active", "inactive", "conditional"]

_STATUS_RANK: dict[CompatibilityStatus, int] = {
    "supported": 0,
    "adapter_required": 1,
    "unsupported": 2,
}
_PRIMITIVE_WIDGET_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}
_DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
_AUTOGROW = "COMFY_AUTOGROW_V3"
_DYNAMIC_SLOT = "COMFY_DYNAMICSLOT_V3"
_MATCHTYPE = "COMFY_MATCHTYPE_V3"
_KNOWN_COMFY_TYPES = {_DYNAMIC_COMBO, _AUTOGROW, _DYNAMIC_SLOT, _MATCHTYPE}
MAX_DYNAMIC_INPUTS = 512


def _canonical_json(value: Any) -> Any:
    """Return a JSON-like value with deterministic mapping order."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    return value


def _join(prefix: str | None, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _type_tokens(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(token.strip() for token in value.split(",") if token.strip()))


def _input_parts(spec: Any) -> tuple[Any, dict[str, Any], bool]:
    if not isinstance(spec, (list, tuple)) or not spec:
        return None, {}, False
    metadata = spec[1] if len(spec) > 1 and isinstance(spec[1], Mapping) else {}
    return spec[0], dict(metadata), True


def _ordered_names(
    values: Mapping[str, Any],
    input_order: Mapping[str, Any] | None,
    group: str,
) -> list[str]:
    """Preserve explicit runtime order and then the server's declaration order."""

    ordered: list[str] = []
    declared = input_order.get(group) if isinstance(input_order, Mapping) else None
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        ordered.extend(str(name) for name in declared if str(name) in values)
    ordered.extend(str(name) for name in values if str(name) not in ordered)
    return ordered


@dataclass(frozen=True)
class CompatibilityReason:
    status: CompatibilityStatus
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class CompatibilityClassification:
    status: CompatibilityStatus
    reasons: tuple[CompatibilityReason, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return _canonical_json(asdict(self))


@dataclass(frozen=True)
class StableDefault:
    available: bool
    value: Any = None
    provenance: Literal[
        "schema_default",
        "legacy_enum_first",
        "combo_option_first",
        "dynamic_option_first",
        "none",
    ] = "none"


@dataclass(frozen=True)
class ActivationConstraint:
    """One deterministic condition or materialization rule for an input."""

    kind: Literal["selector_equals", "input_connected", "autogrow_slot"]
    source: str
    value: Any = None
    ordinal: int | None = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class MatchTypeCapability:
    template_id: str
    allowed_types: tuple[str, ...]


@dataclass(frozen=True)
class InputCapability:
    name: str
    path: str
    group: InputGroup
    declaration_index: int
    kind: InputKind
    declared_type: Any
    accepted_types: tuple[str, ...]
    required: bool
    hidden: bool
    hidden_kind: Literal["auth", "context", "usage", "internal"] | None
    widget: bool
    widget_convertible: bool
    connectable: bool
    force_input: bool
    cardinality: Cardinality
    default: StableDefault
    enum_options: tuple[Any, ...] = ()
    matchtype: MatchTypeCapability | None = None
    activation: tuple[ActivationConstraint, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    duplicate_name: bool = False
    occurrence_index: int = 0

    def as_dict(self) -> dict[str, Any]:
        return _canonical_json(asdict(self))


@dataclass(frozen=True)
class OutputCapability:
    index: int
    name: str
    declared_type: Any
    produced_types: tuple[str, ...]
    cardinality: Cardinality
    enum_options: tuple[Any, ...] = ()
    matchtype_template_id: str | None = None
    matchtype_allowed_types: tuple[str, ...] = ()
    duplicate_name: bool = False

    def as_dict(self) -> dict[str, Any]:
        return _canonical_json(asdict(self))


@dataclass(frozen=True)
class DynamicSelectorOption:
    key: str
    activated_inputs: tuple[str, ...]


@dataclass(frozen=True)
class DynamicGroupCapability:
    path: str
    kind: Literal[
        "dynamic_combo",
        "autogrow",
        "dynamic_slot",
    ]
    activation: tuple[ActivationConstraint, ...]
    options: tuple[DynamicSelectorOption, ...] = ()
    generated_inputs: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    slot_type: str | None = None


@dataclass(frozen=True)
class MaterializedInput:
    capability: InputCapability
    activation_state: ActivationState
    socket_index: int | None


@dataclass(frozen=True)
class NodeSchemaCapabilities:
    contract: str
    node_type: str
    inputs: tuple[InputCapability, ...]
    outputs: tuple[OutputCapability, ...]
    dynamic_groups: tuple[DynamicGroupCapability, ...]
    input_is_list: bool
    classification: CompatibilityClassification

    def as_dict(self) -> dict[str, Any]:
        return _canonical_json(asdict(self))

    def inputs_named(self, name: str) -> tuple[InputCapability, ...]:
        return tuple(item for item in self.inputs if item.name == name or item.path == name)

    def outputs_named(self, name: str) -> tuple[OutputCapability, ...]:
        return tuple(item for item in self.outputs if item.name == name)


@dataclass(frozen=True)
class ConnectionCompatibility:
    status: CompatibilityStatus
    reasons: tuple[CompatibilityReason, ...]
    type_bindings: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return _canonical_json(asdict(self))


def _classification(reasons: Collection[CompatibilityReason]) -> CompatibilityClassification:
    unique = {
        (reason.status, reason.code, reason.path, reason.message): reason for reason in reasons
    }
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda reason: (
                _STATUS_RANK[reason.status],
                reason.code,
                reason.path,
                reason.message,
            ),
        )
    )
    status: CompatibilityStatus = "supported"
    if ordered:
        status = max(ordered, key=lambda reason: _STATUS_RANK[reason.status]).status
    return CompatibilityClassification(status=status, reasons=ordered)


def stable_default_for_spec(spec: Any) -> StableDefault:
    """Return a value plus the exact provenance of any deterministic fallback."""

    input_type, metadata, valid = _input_parts(spec)
    if not valid:
        return StableDefault(False)
    if "default" in metadata:
        return StableDefault(True, _canonical_json(metadata["default"]), "schema_default")
    if isinstance(input_type, list) and input_type:
        return StableDefault(True, _canonical_json(input_type[0]), "legacy_enum_first")
    if input_type == "COMBO":
        options = metadata.get("options")
        if isinstance(options, list) and options:
            return StableDefault(True, _canonical_json(options[0]), "combo_option_first")
    if input_type == _DYNAMIC_COMBO:
        options = metadata.get("options")
        if isinstance(options, list) and options:
            first = options[0]
            if isinstance(first, Mapping) and str(first.get("key") or "").strip():
                return StableDefault(True, str(first["key"]), "dynamic_option_first")
            if isinstance(first, str) and first:
                return StableDefault(True, first, "dynamic_option_first")
    return StableDefault(False)


def _hidden_kind(name: str, input_type: Any) -> Literal["auth", "context", "usage", "internal"]:
    token = f"{name} {input_type}".upper()
    if any(part in token for part in ("AUTH", "API_KEY", "API KEY", "TOKEN")):
        return "auth"
    if "USAGE_SOURCE" in token or "USAGE SOURCE" in token:
        return "usage"
    if any(
        part in token
        for part in ("UNIQUE_ID", "PROMPT", "EXTRA_PNGINFO", "DYNPROMPT", "EXTRA_DATA")
    ):
        return "context"
    return "internal"


def _enum_options(input_type: Any, metadata: Mapping[str, Any]) -> tuple[Any, ...]:
    if isinstance(input_type, list):
        return tuple(_canonical_json(value) for value in input_type)
    if input_type == "COMBO" and isinstance(metadata.get("options"), list):
        return tuple(_canonical_json(value) for value in metadata["options"])
    if input_type == _DYNAMIC_COMBO and isinstance(metadata.get("options"), list):
        return tuple(
            str(option.get("key")) if isinstance(option, Mapping) else str(option)
            for option in metadata["options"]
            if (
                isinstance(option, str) and option
                or isinstance(option, Mapping) and str(option.get("key") or "").strip()
            )
        )
    return ()


def _matchtype(
    metadata: Mapping[str, Any],
    *,
    path: str,
    reasons: list[CompatibilityReason],
) -> MatchTypeCapability | None:
    template = metadata.get("template")
    if not isinstance(template, Mapping):
        reasons.append(
            CompatibilityReason(
                "unsupported",
                "malformed_matchtype",
                path,
                "COMFY_MATCHTYPE_V3 requires template metadata.",
            )
        )
        return None
    template_id = str(template.get("template_id") or "").strip()
    allowed = template.get("allowed_types")
    allowed_types: tuple[str, ...]
    if allowed == "*":
        allowed_types = ("*",)
    elif isinstance(allowed, str):
        allowed_types = _type_tokens(allowed)
    elif isinstance(allowed, Sequence) and not isinstance(allowed, (str, bytes)):
        allowed_types = tuple(str(value) for value in allowed if str(value).strip())
    else:
        allowed_types = ()
    if not template_id or not allowed_types:
        reasons.append(
            CompatibilityReason(
                "unsupported",
                "malformed_matchtype",
                path,
                "COMFY_MATCHTYPE_V3 needs a template_id and allowed_types.",
            )
        )
        return None
    reasons.append(
        CompatibilityReason(
            "adapter_required",
            "graph_scoped_matchtype",
            path,
            "Polymorphic match types require graph-scoped type-variable binding.",
        )
    )
    return MatchTypeCapability(template_id=template_id, allowed_types=allowed_types)


class _Normalizer:
    def __init__(self, node_type: str, schema: Mapping[str, Any]) -> None:
        self.node_type = node_type
        self.schema = schema
        self.inputs: list[InputCapability] = []
        self.groups: list[DynamicGroupCapability] = []
        self.reasons: list[CompatibilityReason] = []
        self.declaration_index = 0
        self.input_is_list = bool(schema.get("is_input_list"))

    def add_input(
        self,
        *,
        name: str,
        path: str,
        group: InputGroup,
        spec: Any,
        activation: tuple[ActivationConstraint, ...],
        forced_kind: InputKind | None = None,
        forced_type: Any = None,
        forced_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        input_type, metadata, valid = _input_parts(spec)
        if forced_metadata is not None:
            metadata = dict(forced_metadata)
        if forced_type is not None:
            input_type = forced_type
        if not valid and forced_type is None:
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_input_spec",
                    path,
                    "Input specs must be a non-empty sequence of type and metadata.",
                )
            )
            return

        force_input = bool(metadata.get("forceInput") or metadata.get("force_input"))
        hidden = group == "hidden"
        enum_options = _enum_options(input_type, metadata)
        matchtype: MatchTypeCapability | None = None
        if hidden:
            kind: InputKind = "hidden"
            widget = False
            convertible = False
            connectable = False
        elif forced_kind is not None:
            kind = forced_kind
            widget = kind == "dynamic_selector"
            convertible = False
            connectable = kind in {"dynamic_slot", "matchtype", "socket"}
        elif input_type == _MATCHTYPE:
            kind = "matchtype"
            widget = False
            convertible = False
            connectable = True
        elif force_input:
            kind = "socket"
            widget = False
            convertible = False
            connectable = True
        elif isinstance(input_type, list) or input_type in _PRIMITIVE_WIDGET_TYPES:
            kind = "widget"
            widget = True
            convertible = True
            connectable = False
        elif isinstance(input_type, str):
            kind = "socket"
            widget = False
            convertible = False
            connectable = True
        else:
            kind = "socket"
            widget = False
            convertible = False
            connectable = False
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "invalid_input_type",
                    path,
                    "Input type must be a Comfy type string or legacy option list.",
                )
            )

        if not hidden and input_type == _MATCHTYPE:
            matchtype = _matchtype(metadata, path=path, reasons=self.reasons)
        elif not hidden and isinstance(input_type, str) and input_type.startswith("COMFY_"):
            if input_type not in _KNOWN_COMFY_TYPES:
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "unknown_comfy_input_type",
                        path,
                        f"No schema adapter is registered for {input_type}.",
                    )
                )

        accepted_types = (
            matchtype.allowed_types
            if matchtype is not None
            else _type_tokens(input_type)
        )
        if isinstance(input_type, list):
            accepted_types = ("COMBO",)
        if isinstance(metadata.get("formats"), Mapping):
            self.reasons.append(
                CompatibilityReason(
                    "adapter_required",
                    "custom_conditional_widgets",
                    path,
                    "Custom conditional widget metadata needs a node-specific adapter.",
                )
            )
        self.inputs.append(
            InputCapability(
                name=name,
                path=path,
                group=group,
                declaration_index=self.declaration_index,
                kind=kind,
                declared_type=_canonical_json(input_type),
                accepted_types=accepted_types,
                required=group == "required",
                hidden=hidden,
                hidden_kind=_hidden_kind(name, input_type) if hidden else None,
                widget=widget,
                widget_convertible=convertible,
                connectable=connectable,
                force_input=force_input,
                cardinality="list" if self.input_is_list else "scalar",
                default=stable_default_for_spec([input_type, metadata]),
                enum_options=enum_options,
                matchtype=matchtype,
                activation=activation,
                metadata=_canonical_json(metadata),
            )
        )
        self.declaration_index += 1

    def walk_groups(
        self,
        groups: Mapping[str, Any],
        *,
        prefix: str | None = None,
        activation: tuple[ActivationConstraint, ...] = (),
        input_order: Mapping[str, Any] | None = None,
        depth: int = 0,
    ) -> None:
        if depth > 12:
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "dynamic_depth_exceeded",
                    prefix or "input",
                    "Dynamic input nesting exceeds the capability limit of 12.",
                )
            )
            return
        for raw_group in ("required", "optional", "hidden"):
            raw_inputs = groups.get(raw_group)
            if raw_inputs is None:
                continue
            if not isinstance(raw_inputs, Mapping):
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "malformed_input_group",
                        _join(prefix, raw_group),
                        "Input groups must be mappings.",
                    )
                )
                continue
            group: InputGroup = raw_group  # type: ignore[assignment]
            for name in _ordered_names(raw_inputs, input_order, raw_group):
                spec = raw_inputs[name]
                # Legacy Comfy hidden context inputs are commonly emitted as a
                # bare type string rather than the public [type, metadata] form.
                if isinstance(spec, str):
                    spec = [spec, {}]
                path = _join(prefix, name)
                input_type, metadata, valid = _input_parts(spec)
                if not valid:
                    self.add_input(
                        name=name,
                        path=path,
                        group=group,
                        spec=spec,
                        activation=activation,
                    )
                    continue
                if input_type == _DYNAMIC_COMBO:
                    self.dynamic_combo(
                        name=name,
                        path=path,
                        group=group,
                        spec=spec,
                        metadata=metadata,
                        activation=activation,
                        depth=depth,
                    )
                    continue
                if input_type == _AUTOGROW:
                    self.autogrow(
                        path=path,
                        group=group,
                        metadata=metadata,
                        activation=activation,
                        depth=depth,
                    )
                    continue
                if input_type == _DYNAMIC_SLOT:
                    self.dynamic_slot(
                        name=name,
                        path=path,
                        group=group,
                        metadata=metadata,
                        activation=activation,
                        depth=depth,
                    )
                    continue
                self.add_input(
                    name=name,
                    path=path,
                    group=group,
                    spec=spec,
                    activation=activation,
                )

    def dynamic_combo(
        self,
        *,
        name: str,
        path: str,
        group: InputGroup,
        spec: Any,
        metadata: Mapping[str, Any],
        activation: tuple[ActivationConstraint, ...],
        depth: int,
    ) -> None:
        options = metadata.get("options")
        if not isinstance(options, list) or not options:
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_dynamic_combo",
                    path,
                    "COMFY_DYNAMICCOMBO_V3 requires a non-empty options list.",
                )
            )
            return
        self.add_input(
            name=name,
            path=path,
            group=group,
            spec=spec,
            activation=activation,
            forced_kind="dynamic_selector",
        )
        option_facts: list[DynamicSelectorOption] = []
        seen_keys: set[str] = set()
        for option_index, option in enumerate(options):
            if isinstance(option, str):
                key = option
                nested = None
            elif isinstance(option, Mapping):
                key = str(option.get("key") or "").strip()
                nested = option.get("inputs")
            else:
                key = ""
                nested = None
            if not key or key in seen_keys:
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "malformed_dynamic_option",
                        f"{path}.options[{option_index}]",
                        "Dynamic option keys must be non-empty and unique.",
                    )
                )
                continue
            seen_keys.add(key)
            before = len(self.inputs)
            condition = (*activation, ActivationConstraint("selector_equals", path, key))
            if nested is not None:
                if not isinstance(nested, Mapping):
                    self.reasons.append(
                        CompatibilityReason(
                            "unsupported",
                            "malformed_dynamic_option_inputs",
                            f"{path}.options[{option_index}].inputs",
                            "Dynamic option inputs must be a mapping.",
                        )
                    )
                else:
                    self.walk_groups(
                        nested,
                        prefix=path,
                        activation=condition,
                        depth=depth + 1,
                    )
            option_facts.append(
                DynamicSelectorOption(
                    key=key,
                    activated_inputs=tuple(item.path for item in self.inputs[before:]),
                )
            )
        self.groups.append(
            DynamicGroupCapability(
                path=path,
                kind="dynamic_combo",
                activation=activation,
                options=tuple(option_facts),
                generated_inputs=tuple(
                    dict.fromkeys(
                        item_path
                        for option in option_facts
                        for item_path in option.activated_inputs
                    )
                ),
            )
        )

    def autogrow(
        self,
        *,
        path: str,
        group: InputGroup,
        metadata: Mapping[str, Any],
        activation: tuple[ActivationConstraint, ...],
        depth: int,
    ) -> None:
        template = metadata.get("template")
        if not isinstance(template, Mapping):
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_autogrow",
                    path,
                    "COMFY_AUTOGROW_V3 requires template metadata.",
                )
            )
            return
        template_groups = template.get("input")
        if not isinstance(template_groups, Mapping):
            template_groups = template
        candidates: list[tuple[InputGroup, str, Any]] = []
        for template_group in ("required", "optional"):
            values = template_groups.get(template_group)
            if isinstance(values, Mapping):
                candidates.extend(
                    (template_group, str(name), spec)  # type: ignore[arg-type]
                    for name, spec in values.items()
                )
        if len(candidates) != 1:
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_autogrow_template",
                    path,
                    "Autogrow templates must describe exactly one input.",
                )
            )
            return
        template_group, _, template_spec = candidates[0]
        try:
            minimum = max(0, int(template.get("min", 0)))
        except (TypeError, ValueError):
            minimum = 0
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_autogrow_minimum",
                    path,
                    "Autogrow minimum must be an integer.",
                )
            )
        names = template.get("names")
        if isinstance(names, list) and all(str(value).strip() for value in names):
            if len(names) > MAX_DYNAMIC_INPUTS:
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "dynamic_input_limit_exceeded",
                        path,
                        f"Autogrow declares more than {MAX_DYNAMIC_INPUTS} inputs.",
                    )
                )
                return
            generated_names = [str(value) for value in names]
            maximum = len(generated_names)
        else:
            prefix = str(template.get("prefix") or "input")
            try:
                maximum = int(template.get("max", max(1, minimum)))
            except (TypeError, ValueError):
                maximum = -1
            if maximum > MAX_DYNAMIC_INPUTS:
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "dynamic_input_limit_exceeded",
                        path,
                        f"Autogrow declares more than {MAX_DYNAMIC_INPUTS} inputs.",
                    )
                )
                return
            generated_names = [f"{prefix}{index}" for index in range(max(0, maximum))]
        if maximum < minimum or maximum < 0 or len(set(generated_names)) != len(generated_names):
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_autogrow_range",
                    path,
                    "Autogrow names/range must be unique and cover the declared minimum.",
                )
            )
            return
        generated_paths: list[str] = []
        for ordinal, generated_name in enumerate(generated_names):
            generated_path = _join(path, generated_name)
            generated_paths.append(generated_path)
            generated_group: InputGroup = (
                "required" if template_group == "required" and ordinal < minimum else "optional"
            )
            self.add_input(
                name=generated_name,
                path=generated_path,
                group=generated_group,
                spec=template_spec,
                activation=(
                    *activation,
                    ActivationConstraint(
                        "autogrow_slot",
                        path,
                        ordinal=ordinal,
                        minimum=minimum,
                        maximum=maximum,
                    ),
                ),
            )
        self.groups.append(
            DynamicGroupCapability(
                path=path,
                kind="autogrow",
                activation=activation,
                generated_inputs=tuple(generated_paths),
                minimum=minimum,
                maximum=maximum,
            )
        )

    def dynamic_slot(
        self,
        *,
        name: str,
        path: str,
        group: InputGroup,
        metadata: Mapping[str, Any],
        activation: tuple[ActivationConstraint, ...],
        depth: int,
    ) -> None:
        slot_type = metadata.get("slotType")
        if not isinstance(slot_type, str) or not slot_type.strip():
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_dynamic_slot",
                    path,
                    "COMFY_DYNAMICSLOT_V3 requires a non-empty slotType.",
                )
            )
            return
        socket_metadata = {**metadata, "forceInput": True, "dynamic_type": _DYNAMIC_SLOT}
        self.add_input(
            name=name,
            path=path,
            group=group,
            spec=[slot_type, socket_metadata],
            activation=activation,
            forced_kind="dynamic_slot",
            forced_type=slot_type,
            forced_metadata=socket_metadata,
        )
        before = len(self.inputs)
        nested = metadata.get("inputs")
        if nested is not None:
            if not isinstance(nested, Mapping):
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "malformed_dynamic_slot_inputs",
                        path,
                        "Dynamic slot dependent inputs must be a mapping.",
                    )
                )
            else:
                self.walk_groups(
                    nested,
                    prefix=path,
                    activation=(*activation, ActivationConstraint("input_connected", path)),
                    depth=depth + 1,
                )
        self.groups.append(
            DynamicGroupCapability(
                path=path,
                kind="dynamic_slot",
                activation=activation,
                generated_inputs=tuple(item.path for item in self.inputs[before:]),
                slot_type=slot_type,
            )
        )

    def normalize_outputs(self) -> tuple[OutputCapability, ...]:
        raw_outputs = self.schema.get("output")
        if raw_outputs is None:
            raw_outputs = []
        if not isinstance(raw_outputs, list):
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_outputs",
                    "output",
                    "Output types must be a list.",
                )
            )
            return ()
        names = self.schema.get("output_name")
        names = names if isinstance(names, list) else []
        list_flags = self.schema.get("output_is_list")
        list_flags = list_flags if isinstance(list_flags, list) else []
        matchtypes = self.schema.get("output_matchtypes")
        matchtypes = matchtypes if isinstance(matchtypes, list) else []
        input_matchtypes = {
            item.matchtype.template_id: item.matchtype.allowed_types
            for item in self.inputs
            if item.matchtype is not None
        }
        pending: list[OutputCapability] = []
        for index, output_type in enumerate(raw_outputs):
            name = (
                str(names[index])
                if index < len(names) and names[index] not in (None, "")
                else str(output_type)
            )
            template_id: str | None = None
            allowed_types: tuple[str, ...] = ()
            enum_options: tuple[Any, ...] = ()
            produced_types = _type_tokens(output_type)
            if output_type == _MATCHTYPE:
                template_id = (
                    str(matchtypes[index]).strip()
                    if index < len(matchtypes) and matchtypes[index] is not None
                    else ""
                )
                if not template_id:
                    self.reasons.append(
                        CompatibilityReason(
                            "unsupported",
                            "unbound_matchtype_output",
                            f"output[{index}]",
                            "COMFY_MATCHTYPE_V3 output needs an output_matchtypes template ID.",
                        )
                    )
                    template_id = None
                else:
                    allowed_types = input_matchtypes.get(template_id, ())
                    if not allowed_types:
                        self.reasons.append(
                            CompatibilityReason(
                                "adapter_required",
                                "external_matchtype_binding",
                                f"output[{index}]",
                                "Output match type is not bound by a normalized input template.",
                            )
                        )
                    produced_types = allowed_types
            elif isinstance(output_type, str) and output_type.startswith("COMFY_"):
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "unknown_comfy_output_type",
                        f"output[{index}]",
                        f"No schema adapter is registered for {output_type}.",
                    )
                )
            elif isinstance(output_type, list):
                enum_options = tuple(_canonical_json(value) for value in output_type)
                produced_types = ("COMBO",)
                if not enum_options:
                    self.reasons.append(
                        CompatibilityReason(
                            "adapter_required",
                            "dynamic_output_options_unavailable",
                            f"output[{index}]",
                            "An empty legacy combo output needs a runtime option adapter.",
                        )
                    )
            elif not isinstance(output_type, str):
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "invalid_output_type",
                        f"output[{index}]",
                        "Output type must be a Comfy type string.",
                    )
                )
            pending.append(
                OutputCapability(
                    index=index,
                    name=name,
                    declared_type=_canonical_json(output_type),
                    produced_types=produced_types,
                    cardinality=(
                        "list"
                        if index < len(list_flags) and bool(list_flags[index])
                        else "scalar"
                    ),
                    enum_options=enum_options,
                    matchtype_template_id=template_id,
                    matchtype_allowed_types=allowed_types,
                )
            )
        counts = Counter(item.name for item in pending)
        return tuple(replace(item, duplicate_name=counts[item.name] > 1) for item in pending)

    def finish(self) -> NodeSchemaCapabilities:
        raw_inputs = self.schema.get("input")
        if raw_inputs is None:
            raw_inputs = {}
        if not isinstance(raw_inputs, Mapping):
            self.reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "malformed_inputs",
                    "input",
                    "Node input schema must be a mapping.",
                )
            )
            raw_inputs = {}
        input_order = self.schema.get("input_order")
        self.walk_groups(
            raw_inputs,
            input_order=input_order if isinstance(input_order, Mapping) else None,
        )
        outputs = self.normalize_outputs()
        counts = Counter(item.path for item in self.inputs if not item.hidden)
        occurrences: Counter[str] = Counter()
        normalized_inputs: list[InputCapability] = []
        for item in self.inputs:
            occurrence = occurrences[item.path]
            occurrences[item.path] += 1
            normalized_inputs.append(
                replace(
                    item,
                    duplicate_name=not item.hidden and counts[item.path] > 1,
                    occurrence_index=occurrence,
                )
            )
        duplicate_constraints: dict[tuple[str, tuple[ActivationConstraint, ...]], int] = Counter(
            (item.path, item.activation) for item in normalized_inputs if not item.hidden
        )
        for (path, _), count in duplicate_constraints.items():
            if count > 1:
                self.reasons.append(
                    CompatibilityReason(
                        "unsupported",
                        "duplicate_active_input_path",
                        path,
                        "The same runtime input path is declared more than once in one context.",
                    )
                )
        return NodeSchemaCapabilities(
            contract=SCHEMA_CAPABILITY_CONTRACT,
            node_type=self.node_type,
            inputs=tuple(normalized_inputs),
            outputs=outputs,
            dynamic_groups=tuple(
                sorted(self.groups, key=lambda group: (group.path, group.kind))
            ),
            input_is_list=self.input_is_list,
            classification=_classification(self.reasons),
        )


def normalize_node_schema(
    node_type: str,
    schema: Mapping[str, Any],
) -> NodeSchemaCapabilities:
    """Normalize one live ``/object_info`` entry without reading mutable defaults."""

    if not isinstance(node_type, str) or not node_type.strip():
        raise ValueError("node_type must be a non-empty string")
    if not isinstance(schema, Mapping):
        raise TypeError("schema must be a mapping")
    return _Normalizer(node_type, schema).finish()


def classify_schema_compatibility(
    capabilities: NodeSchemaCapabilities,
) -> CompatibilityClassification:
    """Return the deterministic aggregate classification for a normalized node."""

    return capabilities.classification


def _effective_selector_values(
    capabilities: NodeSchemaCapabilities,
    supplied: Mapping[str, Any],
    *,
    use_stable_defaults: bool,
) -> dict[str, Any]:
    values = dict(supplied)
    if not use_stable_defaults:
        return values
    for item in capabilities.inputs:
        if (
            item.kind == "dynamic_selector"
            and item.path not in values
            and item.default.available
        ):
            values[item.path] = item.default.value
    return values


def activation_state(
    capability: InputCapability,
    *,
    values: Mapping[str, Any],
    connected_inputs: Collection[str] | None,
) -> ActivationState:
    """Evaluate selector/connection gates; autogrow constraints are ordering facts."""

    conditional = False
    for constraint in capability.activation:
        if constraint.kind == "selector_equals":
            if constraint.source not in values:
                conditional = True
            elif values[constraint.source] != constraint.value:
                return "inactive"
        elif constraint.kind == "input_connected":
            if connected_inputs is None:
                conditional = True
            elif constraint.source not in connected_inputs:
                return "inactive"
    return "conditional" if conditional else "active"


def materialize_inputs(
    capabilities: NodeSchemaCapabilities,
    *,
    values: Mapping[str, Any] | None = None,
    connected_inputs: Collection[str] | None = None,
    use_stable_defaults: bool = True,
    include_inactive: bool = False,
) -> tuple[MaterializedInput, ...]:
    """Resolve dynamic alternatives and assign exact connectable socket indexes.

    Potential autogrow slots stay in declared order; their ``autogrow_slot``
    constraints tell a frontend adapter which connections must be materialized
    sequentially.  Inactive selector branches are omitted by default.
    """

    effective_values = _effective_selector_values(
        capabilities,
        values or {},
        use_stable_defaults=use_stable_defaults,
    )
    states = [
        activation_state(
            item,
            values=effective_values,
            connected_inputs=connected_inputs,
        )
        for item in capabilities.inputs
    ]
    socket_index = 0
    result: list[MaterializedInput] = []
    for item, state in zip(capabilities.inputs, states, strict=True):
        current_index: int | None = None
        if state != "inactive" and item.connectable:
            current_index = socket_index
            socket_index += 1
        if include_inactive or state != "inactive":
            result.append(
                MaterializedInput(
                    capability=item,
                    activation_state=state,
                    socket_index=current_index,
                )
            )
    return tuple(result)


def infer_dynamic_selector_values(
    capabilities: NodeSchemaCapabilities,
    serialized_widget_values: Sequence[Any],
    *,
    connected_inputs: Collection[str] | None = None,
) -> dict[str, Any]:
    """Recover active dynamic-selector values from one serialized canvas node.

    LiteGraph stores widget values positionally while ``/object_info`` stores the
    corresponding names and activation rules.  Dynamic selectors occur before
    the branch they activate, so repeatedly materializing the active widget list
    converges even when one selector changes the position of a later selector.
    Values that are absent, malformed, or outside the live option set are ignored;
    callers then fail closed if an exact active declaration remains ambiguous.
    """

    if isinstance(serialized_widget_values, (str, bytes)):
        return {}
    widget_values = list(serialized_widget_values)
    values: dict[str, Any] = {}
    selector_count = sum(item.kind == "dynamic_selector" for item in capabilities.inputs)
    for _ in range(max(2, selector_count + 2)):
        materialized = materialize_inputs(
            capabilities,
            values=values,
            connected_inputs=connected_inputs,
        )
        widgets = [
            item.capability
            for item in materialized
            if item.capability.widget and not item.capability.hidden
        ]
        updated = dict(values)
        for index, capability in enumerate(widgets):
            if index >= len(widget_values) or capability.kind != "dynamic_selector":
                continue
            observed = widget_values[index]
            if observed in capability.enum_options:
                updated[capability.path] = observed
        if updated == values:
            break
        values = updated
    return values


def _allowed(allowed_types: tuple[str, ...], concrete: str) -> bool:
    return "*" in allowed_types or concrete in allowed_types


def classify_connection(
    source: OutputCapability,
    target: InputCapability,
    *,
    type_bindings: Mapping[str, str] | None = None,
    source_binding_key: str | None = None,
    target_binding_key: str | None = None,
) -> ConnectionCompatibility:
    """Classify one exact edge and return any graph-scoped type bindings.

    Matchtype IDs are local to a node instance.  Graph validators should pass
    instance-scoped binding keys (for example ``"42:input_type"``); otherwise
    an unresolved polymorphic edge is classified as adapter-required rather
    than guessing across nodes.
    """

    reasons: list[CompatibilityReason] = []
    bindings = dict(type_bindings or {})
    if target.hidden:
        reasons.append(
            CompatibilityReason(
                "unsupported",
                "hidden_input_not_connectable",
                target.path,
                "Hidden context/auth inputs cannot be supplied by graph edges.",
            )
        )
        return ConnectionCompatibility(
            "unsupported", _classification(reasons).reasons, _canonical_json(bindings)
        )
    if not target.connectable:
        if target.widget_convertible:
            reasons.append(
                CompatibilityReason(
                    "adapter_required",
                    "widget_conversion_required",
                    target.path,
                    "The target widget must be converted to a socket before connecting.",
                )
            )
        else:
            reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "target_not_connectable",
                    target.path,
                    "The target is neither a socket nor a convertible widget.",
                )
            )

    source_types = set(source.produced_types)
    target_types = set(target.accepted_types)
    source_match = source.matchtype_template_id is not None
    target_match = target.matchtype is not None
    source_key = source_binding_key
    target_key = target_binding_key
    if source_match and source_key and source_key in bindings:
        bound = bindings[source_key]
        if source.matchtype_allowed_types and not _allowed(
            source.matchtype_allowed_types, bound
        ):
            reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "matchtype_binding_conflict",
                    f"output[{source.index}]",
                    f"Existing matchtype binding {bound} violates the output schema.",
                )
            )
        else:
            source_types = {bound}
            source_match = False
    if target_match and target_key and target_key in bindings:
        bound = bindings[target_key]
        if not _allowed(target.matchtype.allowed_types, bound):
            reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "matchtype_binding_conflict",
                    target.path,
                    f"Existing matchtype binding {bound} violates the input schema.",
                )
            )
        else:
            target_types = {bound}
            target_match = False

    concrete_source = (
        next(iter(source_types))
        if not source_match and len(source_types) == 1 and "*" not in source_types
        else None
    )
    concrete_target = (
        next(iter(target_types))
        if not target_match and len(target_types) == 1 and "*" not in target_types
        else None
    )
    if target_match and concrete_source is not None:
        if not _allowed(target.matchtype.allowed_types, concrete_source):
            reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "matchtype_input_rejects_type",
                    target.path,
                    f"Matchtype input does not allow {concrete_source}.",
                )
            )
        elif target_key:
            bindings[target_key] = concrete_source
            target_types = {concrete_source}
            target_match = False
        else:
            reasons.append(
                CompatibilityReason(
                    "adapter_required",
                    "matchtype_binding_key_required",
                    target.path,
                    "A node-instance binding key is required for this polymorphic input.",
                )
            )
    if source_match and concrete_target is not None:
        if source.matchtype_allowed_types and not _allowed(
            source.matchtype_allowed_types, concrete_target
        ):
            reasons.append(
                CompatibilityReason(
                    "unsupported",
                    "matchtype_output_rejects_type",
                    f"output[{source.index}]",
                    f"Matchtype output cannot bind to {concrete_target}.",
                )
            )
        elif source_key:
            bindings[source_key] = concrete_target
            source_types = {concrete_target}
            source_match = False
        else:
            reasons.append(
                CompatibilityReason(
                    "adapter_required",
                    "matchtype_binding_key_required",
                    f"output[{source.index}]",
                    "A node-instance binding key is required for this polymorphic output.",
                )
            )
    if source_match or target_match:
        reasons.append(
            CompatibilityReason(
                "adapter_required",
                "unresolved_matchtype_edge",
                target.path,
                "The edge needs graph-scoped polymorphic type unification.",
            )
        )
    elif source_types and target_types and not (
        "*" in source_types or "*" in target_types or source_types & target_types
    ):
        reasons.append(
            CompatibilityReason(
                "unsupported",
                "incompatible_types",
                target.path,
                "Source and target Comfy types do not intersect.",
            )
        )
    elif not source_types or not target_types:
        reasons.append(
            CompatibilityReason(
                "unsupported",
                "unknown_connection_type",
                target.path,
                "The source or target has no deterministic Comfy type.",
            )
        )

    # Comfy's execution engine represents every connected value as a list.  A
    # regular scalar-input node is mapped over list outputs, while INPUT_IS_LIST
    # nodes receive that list as a whole.  OUTPUT_IS_LIST controls result merging,
    # not graph compatibility, so every cardinality pairing is a native edge.
    classification = _classification(reasons)
    return ConnectionCompatibility(
        classification.status,
        classification.reasons,
        _canonical_json(bindings),
    )
