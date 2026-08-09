"""High-level deterministic compilation from semantic workflow intent."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from node_library import classify_node_origin
from pydantic import BaseModel, ConfigDict, Field, model_validator
from workflow_planner import (
    PlanWorkflowRequest,
    WorkflowPlanAttachment,
    WorkflowPlanConnection,
    WorkflowPlanNode,
    _expand_input_groups,
    _input_parts,
    _is_connectable,
    _output_slots,
    compile_workflow_plan,
)
from workflow_resolver import (
    ResolveWorkflowSpecRequest,
    WorkflowCapabilitySpec,
    resolve_workflow_spec,
)
from workflow_schema_capabilities import materialize_inputs, normalize_node_schema

WORKFLOW_COMPILER_SCHEMA = "fl-mcp.workflow-spec-compiler.v1"
_NORMALIZED_NAME = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_GENERIC_WIDGET_NAME_TERMS = {
    "choice",
    "method",
    "mode",
    "option",
    "parameter",
    "selector",
    "setting",
    "type",
    "value",
}


class WorkflowSpecNode(WorkflowCapabilitySpec):
    """One semantic node role plus the values requested by the user."""

    values: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Requested widget values. Exact runtime names are accepted; unique short "
            "suffixes such as aspect_ratio are canonicalized to dotted dynamic names."
        ),
    )


class WorkflowSpecConnection(BaseModel):
    """A semantic edge whose slot names may use unique runtime suffixes."""

    model_config = ConfigDict(extra="forbid")

    source_alias: str = Field(..., min_length=1, max_length=64)
    source_output: str = Field(..., min_length=1, max_length=256)
    source_output_index: int | None = Field(None, ge=0)
    target_alias: str = Field(..., min_length=1, max_length=64)
    target_input: str = Field(..., min_length=1, max_length=256)


class CompileWorkflowSpecRequest(BaseModel):
    """Compile semantic roles, values, edges, and chat uploads in one dry run."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[WorkflowSpecNode] = Field(..., min_length=1, max_length=200)
    connections: list[WorkflowSpecConnection] = Field(default_factory=list, max_length=500)
    attachments: list[WorkflowPlanAttachment] = Field(default_factory=list, max_length=32)
    expected_catalog_hash: str | None = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    application_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
        description="Stable ID to use when applying the returned exact plan.",
    )

    @model_validator(mode="after")
    def validate_contract(self) -> CompileWorkflowSpecRequest:
        aliases = [node.alias for node in self.nodes]
        if len(aliases) != len(set(aliases)):
            raise ValueError("node aliases must be unique")
        payload = self.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False)) > 262_144:
            raise ValueError("workflow compiler request must not exceed 256 KiB")
        return self


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    severity: Literal["error", "warning"] = "error",
) -> dict[str, str]:
    return {"severity": severity, "code": code, "path": path, "message": message}


def _normalized(value: str) -> str:
    return _NORMALIZED_NAME.sub("", value.casefold())


def _runtime_name_matches(requested: str, available: list[str]) -> list[str]:
    if requested in available:
        return [requested]
    token = _normalized(requested)
    return sorted(
        name
        for name in available
        if _normalized(name) == token or _normalized(name.rsplit(".", 1)[-1]) == token
    )


def _semantic_widget_name_terms(value: str) -> tuple[str, ...]:
    """Return conservative leaf-name terms for unique widget-key aliases."""

    leaf = value.rsplit(".", 1)[-1]
    expanded = _CAMEL_BOUNDARY.sub(" ", leaf)
    words = [
        item
        for item in _NORMALIZED_NAME.split(expanded.casefold())
        if item and item not in _GENERIC_WIDGET_NAME_TERMS
    ]
    stemmed: set[str] = set()
    for word in words:
        stem = word
        for suffix in ("ments", "ment", "ings", "ing", "ed"):
            if stem.endswith(suffix) and len(stem) - len(suffix) >= 4:
                stem = stem[: -len(suffix)]
                break
        stemmed.add(stem)
    return tuple(sorted(stemmed))


def _semantic_widget_name_matches(
    requested: str,
    available: list[str],
) -> list[str]:
    requested_terms = _semantic_widget_name_terms(requested)
    if not requested_terms:
        return []
    requested_set = set(requested_terms)
    matches: list[str] = []
    for name in available:
        candidate_terms = _semantic_widget_name_terms(name)
        if candidate_terms == requested_terms or (
            len(candidate_terms) == 1 and set(candidate_terms) < requested_set
        ):
            matches.append(name)
    return sorted(matches)


def _resolve_runtime_name(
    requested: str,
    available: list[str],
    *,
    path: str,
    kind: str,
    allow_semantic_widget_alias: bool = False,
) -> tuple[str | None, list[dict[str, str]]]:
    matches = _runtime_name_matches(requested, available)
    if len(matches) == 1:
        return matches[0], []
    if not matches and allow_semantic_widget_alias:
        matches = _semantic_widget_name_matches(requested, available)
        if len(matches) == 1:
            return matches[0], []
    if not matches:
        return None, [
            _issue(
                f"unknown_{kind}",
                path,
                f"No active runtime {kind.replace('_', ' ')} matches {requested!r}.",
            )
        ]
    return None, [
        _issue(
            f"ambiguous_{kind}",
            path,
            f"{requested!r} matches multiple runtime names: {', '.join(matches)}.",
        )
    ]


def _selector_option_value(value: Any, options: tuple[Any, ...]) -> Any | None:
    """Return one exact live selector option for an exact/normalized value."""

    exact = [option for option in options if type(option) is type(value) and option == value]
    if len(exact) == 1:
        return exact[0]
    if not isinstance(value, str):
        return None
    token = _normalized(value)
    normalized = [
        option
        for option in options
        if isinstance(option, str) and _normalized(option) == token
    ]
    return normalized[0] if len(normalized) == 1 else None


def _selector_alias_name_related(requested: str, selector_path: str) -> bool:
    """Require a meaningful name relationship for value-driven aliases."""

    requested_token = _normalized(requested)
    selector_token = _normalized(selector_path.rsplit(".", 1)[-1])
    shared_prefix = 0
    for left, right in zip(requested_token, selector_token, strict=False):
        if left != right:
            break
        shared_prefix += 1
    if shared_prefix >= 4:
        return True
    ignored = {"choice", "mode", "option", "selector", "type", "value"}
    requested_words = {
        item for item in _NORMALIZED_NAME.split(requested.casefold()) if item
    } - ignored
    selector_words = {
        item
        for item in _NORMALIZED_NAME.split(
            selector_path.rsplit(".", 1)[-1].casefold()
        )
        if item
    } - ignored
    return bool(requested_words & selector_words)


def _canonicalize_dynamic_selector_aliases(
    requested_values: Mapping[str, Any],
    node_info: Mapping[str, Any],
    *,
    path: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Resolve unique semantic selector names/options from the loaded schema.

    This accepts exact selector names, a related semantic name whose value names
    one unique live option (``resize_mode='scale dimensions'``), or a key that
    itself names one unique live option (``scale_dimensions``). No option or
    selector is guessed when the schema leaves more than one candidate.
    """

    node_type = str(node_info.get("name") or node_info.get("display_name") or "Node")
    capabilities = normalize_node_schema(node_type, node_info)
    selectors = [
        item
        for item in capabilities.inputs
        if item.kind == "dynamic_selector" and not item.hidden
    ]
    if not selectors:
        return dict(requested_values), []

    selector_paths = [item.path for item in selectors]
    selector_by_path = {item.path: item for item in selectors}
    consumed: set[str] = set()
    canonical: dict[str, Any] = {}
    issues: list[dict[str, str]] = []

    def accept(requested: str, selector_path: str, value: Any) -> None:
        consumed.add(requested)
        previous = canonical.get(selector_path)
        if selector_path in canonical and previous != value:
            issues.append(
                _issue(
                    "conflicting_selector_aliases",
                    f"{path}.{requested}",
                    f"Multiple values resolve to selector {selector_path!r}.",
                )
            )
            return
        canonical[selector_path] = value

    # Exact/normalized selector names are authoritative and order-independent.
    for requested in sorted(requested_values):
        matches = _runtime_name_matches(requested, selector_paths)
        if len(matches) != 1:
            continue
        selector_path = matches[0]
        selector = selector_by_path[selector_path]
        value = requested_values[requested]
        canonical_value = _selector_option_value(value, selector.enum_options)
        accept(
            requested,
            selector_path,
            canonical_value if canonical_value is not None else value,
        )

    for requested in sorted(requested_values):
        if requested in consumed:
            continue
        requested_value = requested_values[requested]
        key_candidates = [
            (selector.path, option)
            for selector in selectors
            for option in selector.enum_options
            if isinstance(option, str)
            and _normalized(requested) == _normalized(option)
        ]
        if len(key_candidates) == 1:
            selector_path, option = key_candidates[0]
            selector = selector_by_path[selector_path]
            value_option = _selector_option_value(
                requested_value,
                selector.enum_options,
            )
            if value_option is not None and value_option != option:
                issues.append(
                    _issue(
                        "conflicting_selector_alias_value",
                        f"{path}.{requested}",
                        f"Key {requested!r} names option {option!r}, but its value "
                        f"names {value_option!r}.",
                    )
                )
                consumed.add(requested)
                continue
            accept(requested, selector_path, option)
            continue
        if len(key_candidates) > 1:
            issues.append(
                _issue(
                    "ambiguous_selector_option_alias",
                    f"{path}.{requested}",
                    f"{requested!r} names an option on multiple dynamic selectors.",
                )
            )
            consumed.add(requested)
            continue

        value_candidates = []
        for selector in selectors:
            if not _selector_alias_name_related(requested, selector.path):
                continue
            option = _selector_option_value(requested_value, selector.enum_options)
            if option is not None:
                value_candidates.append((selector.path, option))
        if len(value_candidates) == 1:
            accept(requested, *value_candidates[0])
        elif len(value_candidates) > 1:
            issues.append(
                _issue(
                    "ambiguous_selector_value_alias",
                    f"{path}.{requested}",
                    f"{requested!r} and its value match multiple dynamic selectors.",
                )
            )
            consumed.add(requested)

    result = {
        key: value
        for key, value in requested_values.items()
        if key not in consumed
    }
    result.update(canonical)
    return result, issues


def _stable_default(spec: Any) -> tuple[bool, Any]:
    input_type, metadata = _input_parts(spec)
    if "default" in metadata:
        return True, metadata["default"]
    if isinstance(input_type, list) and input_type:
        return True, input_type[0]
    if input_type == "COMBO":
        options = metadata.get("options")
        if isinstance(options, list) and options:
            return True, options[0]
    if input_type == "COMFY_DYNAMICCOMBO_V3":
        options = metadata.get("options")
        if isinstance(options, list) and options:
            first = options[0]
            if isinstance(first, Mapping) and str(first.get("key") or "").strip():
                return True, first["key"]
            if isinstance(first, str) and first:
                return True, first
    return False, None


def _dynamic_selector_defaults(
    node_info: Mapping[str, Any],
    requested_values: Mapping[str, Any],
    connection_hints: set[str],
) -> dict[str, Any]:
    """Choose selector options from requested dotted slots before using defaults.

    Comfy partner nodes often expose materially different inputs under each
    ``COMFY_DYNAMICCOMBO_V3`` option.  Selecting the first option before looking at
    requested values/connections makes a valid non-default socket appear unknown.
    Score every option by the exact or uniquely-suffixed runtime names it activates;
    use a unique best match, otherwise retain the schema's stable default.
    """

    defaults: dict[str, Any] = {}
    inputs = node_info.get("input")
    if not isinstance(inputs, Mapping):
        return defaults
    for group in ("required", "optional"):
        specs = inputs.get(group)
        if not isinstance(specs, Mapping):
            continue
        for input_name, spec in specs.items():
            input_type, metadata = _input_parts(spec)
            if input_type != "COMFY_DYNAMICCOMBO_V3" or input_name in requested_values:
                continue
            options = metadata.get("options")
            records = [item for item in options if isinstance(item, Mapping)] if isinstance(options, list) else []
            hints = [
                name
                for name in [*requested_values, *sorted(connection_hints)]
                if name != input_name
            ]
            scored: list[tuple[int, int, Any]] = []
            for position, record in enumerate(records):
                key = record.get("key")
                if key is None:
                    continue
                candidate_values = {**requested_values, input_name: key}
                candidate_inputs, _ = _expanded_inputs(
                    node_info,
                    candidate_values,
                    connection_hints,
                )
                available = sorted(candidate_inputs)
                score = sum(bool(_runtime_name_matches(hint, available)) for hint in hints)
                scored.append((score, -position, key))
            if scored:
                best_score = max(item[0] for item in scored)
                best = [item for item in scored if item[0] == best_score]
                if best_score > 0 and len(best) == 1:
                    defaults[input_name] = best[0][2]
                    continue
            has_default, default = _stable_default(spec)
            if has_default:
                defaults[input_name] = default
    return defaults


def _expanded_inputs(
    node_info: Mapping[str, Any],
    values: Mapping[str, Any],
    connected_inputs: set[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    groups, issues = _expand_input_groups(
        node_info.get("input") if isinstance(node_info.get("input"), dict) else {},
        values,
        connected_inputs,
    )
    return {**groups["required"], **groups["optional"]}, issues


def _capability_inputs(
    node_info: Mapping[str, Any],
    values: Mapping[str, Any],
    connected_inputs: set[str],
) -> dict[str, Any]:
    node_type = str(node_info.get("name") or node_info.get("display_name") or "Node")
    capabilities = normalize_node_schema(node_type, node_info)
    return {
        item.capability.path: [
            item.capability.declared_type,
            dict(item.capability.metadata),
        ]
        for item in materialize_inputs(
            capabilities,
            values=values,
            connected_inputs=connected_inputs,
        )
        if not item.capability.hidden
    }


def _supported_dynamic_paths(node_info: Mapping[str, Any]) -> set[str]:
    node_type = str(node_info.get("name") or node_info.get("display_name") or "Node")
    return {
        item.path
        for item in normalize_node_schema(node_type, node_info).inputs
        if item.connectable or item.widget or item.widget_convertible
    }


def _canonicalize_node_values(
    node: WorkflowSpecNode,
    node_info: Mapping[str, Any],
    attachment_inputs: set[str],
    connection_hints: set[str],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    seed_values, issues = _canonicalize_dynamic_selector_aliases(
        node.values,
        node_info,
        path=f"nodes.{node.alias}.values",
    )
    selector_defaults = _dynamic_selector_defaults(
        node_info,
        seed_values,
        connection_hints,
    )
    seed_values.update(selector_defaults)
    first_inputs, _ = _expanded_inputs(node_info, seed_values, connection_hints)
    first_inputs.update(_capability_inputs(node_info, seed_values, connection_hints))
    available = sorted(first_inputs)
    values: dict[str, Any] = dict(selector_defaults)
    for requested, value in seed_values.items():
        resolved, name_issues = _resolve_runtime_name(
            requested,
            available,
            path=f"nodes.{node.alias}.values.{requested}",
            kind="widget",
            allow_semantic_widget_alias=True,
        )
        issues.extend(name_issues)
        if resolved is not None:
            values[resolved] = value

    active_inputs, dynamic_issues = _expanded_inputs(node_info, values, connection_hints)
    active_inputs.update(_capability_inputs(node_info, values, connection_hints))
    supported_dynamic_paths = _supported_dynamic_paths(node_info)
    dynamic_issues = [
        item
        for item in dynamic_issues
        if not (
            item["code"] == "unsupported_dynamic_input"
            and item["path"].removeprefix("inputs.") in supported_dynamic_paths
        )
    ]
    for item in dynamic_issues:
        issues.append(
            {**item, "path": f"nodes.{node.alias}.{item['path']}"}
        )

    for input_name, spec in active_inputs.items():
        if (
            input_name in values
            or input_name in attachment_inputs
            or input_name in connection_hints
            or _is_connectable(spec)
        ):
            continue
        has_default, default = _stable_default(spec)
        if has_default:
            values[input_name] = default

    active_inputs, final_dynamic_issues = _expanded_inputs(
        node_info,
        values,
        connection_hints,
    )
    active_inputs.update(_capability_inputs(node_info, values, connection_hints))
    final_dynamic_issues = [
        item
        for item in final_dynamic_issues
        if not (
            item["code"] == "unsupported_dynamic_input"
            and item["path"].removeprefix("inputs.") in supported_dynamic_paths
        )
    ]
    existing_dynamic_paths = {(item["code"], item["path"]) for item in issues}
    for item in final_dynamic_issues:
        item = {**item, "path": f"nodes.{node.alias}.{item['path']}"}
        if (item["code"], item["path"]) not in existing_dynamic_paths:
            issues.append(item)
    return values, active_inputs, issues


def _partner_review(
    selected_node_types: Mapping[str, str],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    nodes = [
        {"alias": alias, "node_type": node_type}
        for alias, node_type in sorted(selected_node_types.items())
        if isinstance(catalog.get(node_type), dict)
        and classify_node_origin(catalog[node_type]) == "partner"
    ]
    return {
        "required": bool(nodes),
        "nodes": nodes,
        "authentication": "may_be_required" if nodes else "not_applicable",
        "cost": "may_consume_credits_only_when_executed" if nodes else "not_applicable",
        "privacy": (
            "inputs_may_be_sent_to_the_external_partner_only_when_executed"
            if nodes
            else "not_applicable"
        ),
        "current_operation_transmits_images": False,
        "web_lookup_required": False,
    }


def compile_workflow_spec(
    request: CompileWorkflowSpecRequest,
    catalog: Mapping[str, Any],
    *,
    catalog_hash: str,
    source: str,
    validated_attachment_values: Mapping[tuple[str, str], Any],
) -> dict[str, Any]:
    """Resolve, canonicalize, default, and validate one bounded workflow request."""

    capability_request = ResolveWorkflowSpecRequest(
        capabilities=[
            WorkflowCapabilitySpec.model_validate(
                node.model_dump(mode="json", exclude={"values"})
            )
            for node in request.nodes
        ],
        expected_catalog_hash=request.expected_catalog_hash,
    )
    resolution = resolve_workflow_spec(
        capability_request,
        catalog,
        catalog_hash=catalog_hash,
        source=source,
    )
    selected = resolution["selected_node_types"]
    # Resolution warnings are safety-relevant evidence (partner/privacy,
    # experimental classes, unknown provenance).  Keep them in the one-pass
    # compiler result instead of silently dropping everything except errors.
    issues = list(resolution["issues"])
    partner_review = _partner_review(selected, catalog)
    resolution_error_count = sum(
        item["severity"] == "error" for item in issues
    )
    if resolution_error_count:
        return {
            "valid": False,
            "compiler_schema": WORKFLOW_COMPILER_SCHEMA,
            "resolution_hash": resolution["resolution_hash"],
            "plan_hash": None,
            "catalog": resolution["catalog"],
            "selected_node_types": selected,
            "partner_review": partner_review,
            "issues": issues,
            "error_count": resolution_error_count,
            "warning_count": sum(item["severity"] == "warning" for item in issues),
            "apply_request": None,
        }

    attachment_inputs_by_alias: dict[str, set[str]] = {}
    for binding in request.attachments:
        attachment_inputs_by_alias.setdefault(binding.node_alias, set()).add(binding.input_name)
    connection_hints_by_alias: dict[str, set[str]] = {}
    for connection in request.connections:
        connection_hints_by_alias.setdefault(connection.target_alias, set()).add(
            connection.target_input
        )

    exact_nodes: list[WorkflowPlanNode] = []
    active_inputs_by_alias: dict[str, dict[str, Any]] = {}
    for node in request.nodes:
        node_type = selected.get(node.alias)
        node_info = catalog.get(node_type) if node_type else None
        if not isinstance(node_info, dict):
            continue
        values, active_inputs, value_issues = _canonicalize_node_values(
            node,
            node_info,
            attachment_inputs_by_alias.get(node.alias, set()),
            connection_hints_by_alias.get(node.alias, set()),
        )
        issues.extend(value_issues)
        exact_nodes.append(
            WorkflowPlanNode(alias=node.alias, node_type=node_type, values=values)
        )
        active_inputs_by_alias[node.alias] = active_inputs

    exact_attachments: list[WorkflowPlanAttachment] = []
    exact_attachment_values: dict[tuple[str, str], Any] = {}
    for index, binding in enumerate(request.attachments):
        active_inputs = active_inputs_by_alias.get(binding.node_alias, {})
        resolved, name_issues = _resolve_runtime_name(
            binding.input_name,
            sorted(active_inputs),
            path=f"attachments[{index}].input_name",
            kind="attachment_input",
        )
        issues.extend(name_issues)
        raw_key = (binding.node_alias, binding.input_name)
        if resolved is None or raw_key not in validated_attachment_values:
            continue
        exact_attachments.append(
            WorkflowPlanAttachment(
                node_alias=binding.node_alias,
                input_name=resolved,
                image=binding.image,
            )
        )
        exact_attachment_values[(binding.node_alias, resolved)] = (
            validated_attachment_values[raw_key]
        )

    exact_connections: list[WorkflowPlanConnection] = []
    for index, connection in enumerate(request.connections):
        source_type = selected.get(connection.source_alias)
        source_info = catalog.get(source_type) if source_type else None
        outputs = _output_slots(source_info) if isinstance(source_info, dict) else []
        output_names = [slot["name"] for slot in outputs]
        source_output, source_issues = _resolve_runtime_name(
            connection.source_output,
            output_names,
            path=f"connections[{index}].source_output",
            kind="output_slot",
        )
        issues.extend(source_issues)
        target_input, target_issues = _resolve_runtime_name(
            connection.target_input,
            sorted(active_inputs_by_alias.get(connection.target_alias, {})),
            path=f"connections[{index}].target_input",
            kind="input_slot",
        )
        issues.extend(target_issues)
        if source_output is None or target_input is None:
            continue
        exact_connections.append(
            WorkflowPlanConnection(
                source_alias=connection.source_alias,
                source_output=source_output,
                source_output_index=connection.source_output_index,
                target_alias=connection.target_alias,
                target_input=target_input,
            )
        )

    error_count = sum(item["severity"] == "error" for item in issues)
    if error_count:
        return {
            "valid": False,
            "compiler_schema": WORKFLOW_COMPILER_SCHEMA,
            "resolution_hash": resolution["resolution_hash"],
            "plan_hash": None,
            "catalog": resolution["catalog"],
            "selected_node_types": selected,
            "partner_review": partner_review,
            "issues": issues,
            "error_count": error_count,
            "warning_count": sum(item["severity"] == "warning" for item in issues),
            "apply_request": None,
        }

    plan_request = PlanWorkflowRequest(
        nodes=exact_nodes,
        connections=exact_connections,
        attachments=exact_attachments,
        expected_catalog_hash=request.expected_catalog_hash,
    )
    compiled = compile_workflow_plan(
        plan_request,
        catalog,
        catalog_hash=catalog_hash,
        source=source,
        validated_attachment_values=exact_attachment_values,
    )
    apply_request = None
    if compiled["valid"]:
        apply_request = {
            **plan_request.model_dump(mode="json"),
            "expected_catalog_hash": catalog_hash,
            "plan_hash": compiled["plan_hash"],
            "application_id": request.application_id,
        }
    combined_issues = [*issues, *compiled["issues"]]
    # Keep deterministic ordering while avoiding byte-identical duplicates.
    deduplicated_issues: list[dict[str, str]] = []
    seen_issues: set[tuple[str, str, str, str]] = set()
    for item in combined_issues:
        key = (
            item["severity"],
            item["code"],
            item["path"],
            item["message"],
        )
        if key not in seen_issues:
            seen_issues.add(key)
            deduplicated_issues.append(item)
    return {
        "valid": compiled["valid"],
        "compiler_schema": WORKFLOW_COMPILER_SCHEMA,
        "resolution_hash": resolution["resolution_hash"],
        "plan_hash": compiled["plan_hash"],
        "catalog": compiled["catalog"],
        "selected_node_types": selected,
        "plan": compiled["plan"],
        "partner_review": partner_review,
        "issues": deduplicated_issues,
        "error_count": sum(
            item["severity"] == "error" for item in deduplicated_issues
        ),
        "warning_count": sum(
            item["severity"] == "warning" for item in deduplicated_issues
        ),
        "apply_request": apply_request,
    }
