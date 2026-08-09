"""Deterministic dry-run compilation for ComfyUI workflow plans."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from chat_images import ChatImageReference
from node_library import (
    CATALOG_HASH_SCHEMA,
    NODE_SCHEMA_HASH_SCHEMA,
    classify_node_origin,
    node_schema_hash,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

WORKFLOW_PLAN_SCHEMA = "fl-mcp.workflow-plan.v1"
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PRIMITIVE_INPUT_TYPES = {"BOOLEAN", "COMBO", "FLOAT", "INT", "STRING"}
_DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
_AUTOGROW = "COMFY_AUTOGROW_V3"
_DYNAMIC_SLOT = "COMFY_DYNAMICSLOT_V3"
_MAX_DYNAMIC_INPUTS = 512


class WorkflowPlanNode(BaseModel):
    """One planned node with a stable semantic alias."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Stable lowercase alias such as input_image or final_output.",
    )
    node_type: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Exact loaded ComfyUI node class from node_library_search.",
    )
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Exact widget values keyed by the runtime input name.",
    )

    @model_validator(mode="after")
    def validate_alias(self) -> WorkflowPlanNode:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores"
            )
        return self


class WorkflowPlanConnection(BaseModel):
    """One exact planned edge between semantic aliases."""

    model_config = ConfigDict(extra="forbid")

    source_alias: str = Field(..., min_length=1, max_length=64)
    source_output: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Exact output name from the source runtime schema.",
    )
    source_output_index: int | None = Field(
        None,
        ge=0,
        description=(
            "Optional exact output index. Required only when a node exposes the "
            "same output name more than once."
        ),
    )
    target_alias: str = Field(..., min_length=1, max_length=64)
    target_input: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Exact input name from the target runtime schema.",
    )


class WorkflowPlanAttachment(BaseModel):
    """Bind one trusted Ren chat upload to a node widget in the atomic plan."""

    model_config = ConfigDict(extra="forbid")

    node_alias: str = Field(..., min_length=1, max_length=64)
    input_name: str = Field(
        "image",
        min_length=1,
        max_length=256,
        description="Exact image widget name on the target node.",
    )
    image: ChatImageReference

    @model_validator(mode="after")
    def validate_alias(self) -> WorkflowPlanAttachment:
        if not _ALIAS_PATTERN.fullmatch(self.node_alias):
            raise ValueError(
                "node_alias must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores"
            )
        return self


class PlanWorkflowRequest(BaseModel):
    """Compile and validate a workflow without changing the canvas."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[WorkflowPlanNode] = Field(..., min_length=1, max_length=200)
    connections: list[WorkflowPlanConnection] = Field(
        default_factory=list,
        max_length=500,
    )
    attachments: list[WorkflowPlanAttachment] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Trusted Ren chat uploads to include in validation and atomic application. "
            "The backend verifies every referenced file before planning."
        ),
    )
    expected_catalog_hash: str | None = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Optional catalog hash returned by node_library_status. Planning fails "
            "if the loaded-node generation changed."
        ),
    )

    @model_validator(mode="after")
    def validate_payload_size(self) -> PlanWorkflowRequest:
        payload = self.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False)) > 262_144:
            raise ValueError("workflow plan request must not exceed 256 KiB")
        return self


class ApplyWorkflowPlanRequest(PlanWorkflowRequest):
    """Re-submit a valid plan for one idempotent atomic canvas application."""

    expected_catalog_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Exact catalog hash used to produce plan_hash.",
    )
    plan_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Exact plan hash returned by plan_workflow.",
    )
    application_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
        description=(
            "Stable ID for this intended application. Reuse it only when retrying "
            "the same plan; use a new ID to intentionally create another copy."
        ),
    )


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    severity: Literal["error", "warning"] = "error",
) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }


def _input_parts(spec: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(spec, (list, tuple)) or not spec:
        return None, {}
    metadata = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    return spec[0], metadata


def _join_input_name(prefix: str | None, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _first_template_input(template: Any) -> tuple[str, Any] | None:
    if not isinstance(template, dict):
        return None
    inputs = template.get("input")
    if not isinstance(inputs, dict):
        return None
    for group in ("required", "optional"):
        values = inputs.get(group)
        if not isinstance(values, dict):
            continue
        for spec in values.values():
            return group, spec
    return None


def _expand_input_groups(
    groups: Mapping[str, Any],
    values: Mapping[str, Any],
    connected_inputs: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Expand Comfy v3 dynamic inputs into exact dotted runtime names."""

    expanded: dict[str, dict[str, Any]] = {"required": {}, "optional": {}}
    issues: list[dict[str, str]] = []

    def add_group(
        group: str,
        inputs: Any,
        prefix: str | None = None,
        depth: int = 0,
    ) -> None:
        if not isinstance(inputs, dict):
            return
        if depth > 8:
            issues.append(
                _issue(
                    "dynamic_input_depth_exceeded",
                    f"inputs.{prefix or '<root>'}",
                    "Dynamic input nesting exceeds the supported depth of 8.",
                )
            )
            return
        destination = "optional" if group == "optional" else "required"
        for raw_name, spec in inputs.items():
            name = _join_input_name(prefix, str(raw_name))
            input_type, metadata = _input_parts(spec)
            if input_type == _DYNAMIC_COMBO:
                options = metadata.get("options")
                option_records = (
                    [item for item in options if isinstance(item, dict)]
                    if isinstance(options, list)
                    else []
                )
                option_keys = [
                    str(item.get("key")) for item in option_records if item.get("key") is not None
                ]
                expanded[destination][name] = [
                    option_keys,
                    {**metadata, "dynamic_type": _DYNAMIC_COMBO},
                ]
                selected = values.get(name)
                if selected is None:
                    issues.append(
                        _issue(
                            "missing_dynamic_selection",
                            f"values.{name}",
                            f"Dynamic input {name} needs an explicit option.",
                        )
                    )
                    continue
                selected_record = next(
                    (item for item in option_records if str(item.get("key")) == str(selected)),
                    None,
                )
                if selected_record is None:
                    continue
                nested = selected_record.get("inputs")
                if isinstance(nested, dict):
                    add_group("required", nested.get("required"), name, depth + 1)
                    add_group("optional", nested.get("optional"), name, depth + 1)
                continue

            if input_type == _AUTOGROW:
                template = metadata.get("template")
                template_input = _first_template_input(template)
                if template_input is None:
                    issues.append(
                        _issue(
                            "invalid_autogrow_schema",
                            f"inputs.{name}",
                            f"Autogrow input {name} has no usable template input.",
                        )
                    )
                    continue
                template_group, template_spec = template_input
                names = template.get("names") if isinstance(template, dict) else None
                try:
                    minimum = int(template.get("min", 0)) if isinstance(template, dict) else 0
                except (TypeError, ValueError):
                    issues.append(
                        _issue(
                            "invalid_autogrow_schema",
                            f"inputs.{name}",
                            f"Autogrow input {name} has an invalid minimum.",
                        )
                    )
                    continue
                if not isinstance(names, list):
                    prefix_name = (
                        str(template.get("prefix") or "input")
                        if isinstance(template, dict)
                        else "input"
                    )
                    try:
                        maximum = (
                            int(template.get("max", max(minimum, 1)))
                            if isinstance(template, dict)
                            else max(minimum, 1)
                        )
                    except (TypeError, ValueError):
                        maximum = -1
                    if maximum < 0 or maximum > _MAX_DYNAMIC_INPUTS:
                        issues.append(
                            _issue(
                                "dynamic_input_limit_exceeded",
                                f"inputs.{name}",
                                f"Autogrow inputs must be between 0 and {_MAX_DYNAMIC_INPUTS}.",
                            )
                        )
                        continue
                    names = [f"{prefix_name}{index}" for index in range(maximum)]
                elif len(names) > _MAX_DYNAMIC_INPUTS:
                    issues.append(
                        _issue(
                            "dynamic_input_limit_exceeded",
                            f"inputs.{name}",
                            f"Autogrow inputs must not exceed {_MAX_DYNAMIC_INPUTS}.",
                        )
                    )
                    continue
                for index, item_name in enumerate(names):
                    nested_name = _join_input_name(name, str(item_name))
                    nested_group = (
                        "required"
                        if template_group == "required" and index < minimum
                        else "optional"
                    )
                    expanded[nested_group][nested_name] = template_spec
                continue

            if input_type == _DYNAMIC_SLOT:
                slot_type = metadata.get("slotType")
                expanded["optional"][name] = [
                    slot_type,
                    {**metadata, "dynamic_type": _DYNAMIC_SLOT, "forceInput": True},
                ]
                nested = metadata.get("inputs")
                if isinstance(nested, dict) and name in connected_inputs:
                    add_group("required", nested.get("required"), name, depth + 1)
                    add_group("optional", nested.get("optional"), name, depth + 1)
                continue

            if isinstance(input_type, str) and input_type.startswith("COMFY_"):
                issues.append(
                    _issue(
                        "unsupported_dynamic_input",
                        f"inputs.{name}",
                        f"Dynamic input type {input_type} is not supported by this planner.",
                    )
                )
                continue

            expanded[destination][name] = spec

    add_group("required", groups.get("required"))
    add_group("optional", groups.get("optional"))
    return expanded, issues


def _is_connectable(spec: Any) -> bool:
    input_type, metadata = _input_parts(spec)
    if metadata.get("forceInput") is True or metadata.get("force_input") is True:
        return True
    if isinstance(input_type, list):
        return False
    return isinstance(input_type, str) and input_type not in _PRIMITIVE_INPUT_TYPES


def _validate_widget_value(name: str, spec: Any, value: Any) -> list[dict[str, str]]:
    input_type, metadata = _input_parts(spec)
    path = f"values.{name}"
    issues: list[dict[str, str]] = []
    if _is_connectable(spec):
        return [
            _issue(
                "value_for_connection_input",
                path,
                f"{name} is a connection input and cannot be configured as a widget value.",
            )
        ]
    if isinstance(input_type, list):
        if value not in input_type:
            issues.append(
                _issue(
                    "invalid_option",
                    path,
                    f"{value!r} is not one of the allowed options for {name}.",
                )
            )
        return issues
    if input_type == "COMBO":
        options = metadata.get("options")
        if not isinstance(options, list):
            return [
                _issue(
                    "invalid_combo_schema",
                    path,
                    f"{name} has no valid COMBO options in the runtime schema.",
                )
            ]
        if metadata.get("multiselect") is True:
            if not isinstance(value, list):
                return [
                    _issue(
                        "invalid_value_type",
                        path,
                        f"{name} expects a list of COMBO options.",
                    )
                ]
            invalid = [item for item in value if item not in options]
            if invalid:
                return [
                    _issue(
                        "invalid_option",
                        path,
                        f"{invalid!r} contains unsupported options for {name}.",
                    )
                ]
        elif value not in options:
            return [
                _issue(
                    "invalid_option",
                    path,
                    f"{value!r} is not one of the allowed options for {name}.",
                )
            ]
        return issues
    valid_type = {
        "BOOLEAN": lambda item: isinstance(item, bool),
        "FLOAT": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "INT": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "STRING": lambda item: isinstance(item, str),
    }.get(input_type)
    if valid_type is not None and not valid_type(value):
        return [
            _issue(
                "invalid_value_type",
                path,
                f"{name} expects {input_type}, got {type(value).__name__}.",
            )
        ]
    if isinstance(value, float) and not math.isfinite(value):
        return [
            _issue(
                "non_finite_value",
                path,
                f"{name} must be a finite number.",
            )
        ]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = metadata.get("min")
        maximum = metadata.get("max")
        if isinstance(minimum, (int, float)) and value < minimum:
            issues.append(
                _issue("value_below_minimum", path, f"{name} must be at least {minimum}.")
            )
        if isinstance(maximum, (int, float)) and value > maximum:
            issues.append(_issue("value_above_maximum", path, f"{name} must be at most {maximum}."))
    return issues


def _canonical_widget_value(spec: Any, value: Any) -> Any:
    input_type, _ = _input_parts(spec)
    if input_type == "FLOAT" and isinstance(value, (int, float)):
        return 0.0 if value == 0 else float(value)
    return value


def _output_slots(node_info: Mapping[str, Any]) -> list[dict[str, Any]]:
    output_types = node_info.get("output")
    if not isinstance(output_types, list):
        return []
    output_names = node_info.get("output_name")
    if not isinstance(output_names, list):
        output_names = []
    slots = []
    for index, output_type in enumerate(output_types):
        name = (
            output_names[index]
            if index < len(output_names) and output_names[index]
            else output_type
        )
        slots.append({"index": index, "name": str(name), "type": output_type})
    return slots


def _type_tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _types_compatible(output_type: Any, input_type: Any) -> bool:
    output_tokens = _type_tokens(output_type)
    input_tokens = _type_tokens(input_type)
    return bool(output_tokens and input_tokens) and (
        "*" in output_tokens or "*" in input_tokens or bool(output_tokens & input_tokens)
    )


def _cycle_aliases(
    aliases: set[str],
    connections: list[dict[str, Any]],
) -> list[str]:
    """Return aliases participating in a directed cycle, if one exists."""

    adjacency = {alias: set() for alias in aliases}
    for connection in connections:
        source = connection["source_alias"]
        target = connection["target_alias"]
        adjacency[source].add(target)

    state = dict.fromkeys(aliases, 0)
    stack: list[str] = []
    stack_positions: dict[str, int] = {}
    cyclic: set[str] = set()

    def visit(alias: str) -> None:
        state[alias] = 1
        stack_positions[alias] = len(stack)
        stack.append(alias)
        for target in sorted(adjacency[alias]):
            if state[target] == 0:
                visit(target)
            elif state[target] == 1:
                cyclic.update(stack[stack_positions[target] :])
        stack.pop()
        stack_positions.pop(alias, None)
        state[alias] = 2

    for alias in sorted(aliases):
        if state[alias] == 0:
            visit(alias)
    return sorted(cyclic)


def _public_node_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded, metadata-free summary of one resolved node."""

    active_inputs = []
    for group in ("required", "optional"):
        for name, spec in sorted(record["inputs"][group].items()):
            input_type, _ = _input_parts(spec)
            active_inputs.append(
                {
                    "name": name,
                    "required": group == "required",
                    "connectable": _is_connectable(spec),
                    "type": input_type,
                }
            )
    return {key: value for key, value in record.items() if key != "inputs"} | {
        "active_inputs": active_inputs
    }


def _validated_attachment_widget_value(value: Any) -> str | None:
    """Read the widget value from a backend attachment attestation.

    Plain strings remain accepted for the legacy pure planner tests/callers;
    GraphPatch compilation requires the full integrity mapping separately.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("widget_value"), str):
        return value["widget_value"]
    return None


def compile_workflow_plan(
    request: PlanWorkflowRequest,
    catalog: Mapping[str, Any],
    *,
    catalog_hash: str,
    source: str,
    validated_attachment_values: Mapping[tuple[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Resolve and validate a plan against one immutable catalog snapshot."""

    issues: list[dict[str, str]] = []
    if request.expected_catalog_hash and request.expected_catalog_hash != catalog_hash:
        issues.append(
            _issue(
                "catalog_changed",
                "expected_catalog_hash",
                "The loaded-node catalog changed after discovery; search and inspect nodes again.",
            )
        )

    attachment_values = {
        key: widget_value
        for key, value in (validated_attachment_values or {}).items()
        if (widget_value := _validated_attachment_widget_value(value)) is not None
    }
    declared_attachment_keys = {
        (binding.node_alias, binding.input_name)
        for binding in request.attachments
    }
    for index, binding in enumerate(request.attachments):
        key = (binding.node_alias, binding.input_name)
        if key not in attachment_values:
            issues.append(
                _issue(
                    "attachment_not_validated",
                    f"attachments[{index}]",
                    "The chat attachment was not validated by the trusted backend.",
                )
            )
    if len(declared_attachment_keys) != len(request.attachments):
        issues.append(
            _issue(
                "duplicate_attachment_binding",
                "attachments",
                "A node widget may receive only one chat attachment binding.",
            )
        )

    aliases: dict[str, WorkflowPlanNode] = {}
    node_records: dict[str, dict[str, Any]] = {}
    resolved_nodes: list[dict[str, Any]] = []
    partner_aliases: list[str] = []
    output_aliases: list[str] = []
    planned_target_inputs: dict[str, set[str]] = {}
    for connection in request.connections:
        planned_target_inputs.setdefault(connection.target_alias, set()).add(
            connection.target_input
        )

    for index, node in enumerate(request.nodes):
        path = f"nodes[{index}]"
        if node.alias in aliases:
            issues.append(
                _issue(
                    "duplicate_alias",
                    f"{path}.alias",
                    f"Alias {node.alias!r} is used more than once.",
                )
            )
            continue
        aliases[node.alias] = node
        raw_info = catalog.get(node.node_type)
        if not isinstance(raw_info, dict):
            issues.append(
                _issue(
                    "node_type_not_loaded",
                    f"{path}.node_type",
                    f"Node type {node.node_type!r} is not loaded in this ComfyUI instance.",
                )
            )
            continue

        node_values = dict(node.values)
        for (binding_alias, input_name), widget_value in attachment_values.items():
            if binding_alias != node.alias:
                continue
            if input_name in node_values and node_values[input_name] != widget_value:
                issues.append(
                    _issue(
                        "attachment_value_conflict",
                        f"{path}.values.{input_name}",
                        f"{node.alias}.{input_name} conflicts with its chat attachment binding.",
                    )
                )
            node_values[input_name] = widget_value

        input_groups, dynamic_issues = _expand_input_groups(
            raw_info.get("input") if isinstance(raw_info.get("input"), dict) else {},
            node_values,
            planned_target_inputs.get(node.alias, set()),
        )
        for issue in dynamic_issues:
            issue["path"] = f"{path}.{issue['path']}"
            issues.append(issue)
        all_inputs = {**input_groups["required"], **input_groups["optional"]}
        accepted_values: dict[str, Any] = {}
        for name, value in node_values.items():
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
            value_issues = (
                []
                if (node.alias, name) in attachment_values and isinstance(value, str)
                else _validate_widget_value(name, spec, value)
            )
            for issue in value_issues:
                issue["path"] = f"{path}.{issue['path']}"
                issues.append(issue)
            if not value_issues:
                accepted_values[name] = _canonical_widget_value(spec, value)
        for name, spec in all_inputs.items():
            if name in node_values or _is_connectable(spec):
                continue
            _, metadata = _input_parts(spec)
            if metadata.get("dynamic_type") == _DYNAMIC_COMBO:
                continue
            issues.append(
                _issue(
                    "missing_widget_value",
                    f"{path}.values.{name}",
                    f"{node.node_type} needs an explicit value for active widget {name!r}.",
                )
            )

        origin = classify_node_origin(raw_info)
        if origin == "partner":
            partner_aliases.append(node.alias)
        if bool(raw_info.get("output_node")):
            output_aliases.append(node.alias)
        record = {
            "alias": node.alias,
            "node_type": node.node_type,
            "display_name": raw_info.get("display_name") or node.node_type,
            "origin": origin,
            "python_module": str(raw_info.get("python_module") or ""),
            "schema_hash": node_schema_hash(node.node_type, raw_info),
            "schema_hash_schema": NODE_SCHEMA_HASH_SCHEMA,
            "values": accepted_values,
            "inputs": input_groups,
            "outputs": _output_slots(raw_info),
            "api_node": bool(raw_info.get("api_node")),
            "output_node": bool(raw_info.get("output_node")),
        }
        node_records[node.alias] = record
        resolved_nodes.append(record)

    resolved_connections: list[dict[str, Any]] = []
    occupied_targets: set[tuple[str, str]] = set()
    for index, connection in enumerate(request.connections):
        path = f"connections[{index}]"
        source_record = node_records.get(connection.source_alias)
        target_record = node_records.get(connection.target_alias)
        if source_record is None:
            issues.append(
                _issue(
                    "unknown_source_alias",
                    f"{path}.source_alias",
                    f"Source alias {connection.source_alias!r} is unresolved.",
                )
            )
        if target_record is None:
            issues.append(
                _issue(
                    "unknown_target_alias",
                    f"{path}.target_alias",
                    f"Target alias {connection.target_alias!r} is unresolved.",
                )
            )
        if source_record is None or target_record is None:
            continue

        output_matches = [
            slot
            for slot in source_record["outputs"]
            if slot["name"] == connection.source_output
            and (
                connection.source_output_index is None
                or slot["index"] == connection.source_output_index
            )
        ]
        if not output_matches:
            issues.append(
                _issue(
                    "unknown_output_slot",
                    f"{path}.source_output",
                    f"{source_record['node_type']} has no output named {connection.source_output!r}.",
                )
            )
            continue
        if len(output_matches) > 1:
            issues.append(
                _issue(
                    "ambiguous_output_slot",
                    f"{path}.source_output",
                    f"Output name {connection.source_output!r} is not unique on "
                    f"{source_record['node_type']}; provide source_output_index.",
                )
            )
            continue
        target_inputs = {
            **target_record["inputs"]["required"],
            **target_record["inputs"]["optional"],
        }
        target_spec = target_inputs.get(connection.target_input)
        if target_spec is None:
            issues.append(
                _issue(
                    "unknown_input_slot",
                    f"{path}.target_input",
                    f"{target_record['node_type']} has no active input named {connection.target_input!r}.",
                )
            )
            continue
        if not _is_connectable(target_spec):
            issues.append(
                _issue(
                    "target_not_connectable",
                    f"{path}.target_input",
                    f"{connection.target_input!r} is a widget input, not a connection slot.",
                )
            )
            continue
        target_key = (connection.target_alias, connection.target_input)
        if target_key in occupied_targets:
            issues.append(
                _issue(
                    "duplicate_target_connection",
                    f"{path}.target_input",
                    f"{connection.target_alias}.{connection.target_input} already has a planned connection.",
                )
            )
            continue
        output = output_matches[0]
        input_type, _ = _input_parts(target_spec)
        if not _types_compatible(output["type"], input_type):
            issues.append(
                _issue(
                    "incompatible_slot_types",
                    path,
                    f"Cannot connect {output['type']} to {input_type}.",
                )
            )
            continue
        if connection.target_input in target_record["values"]:
            issues.append(
                _issue(
                    "value_connection_conflict",
                    path,
                    f"{connection.target_alias}.{connection.target_input} has both a value and a connection.",
                )
            )
            continue
        occupied_targets.add(target_key)
        resolved_connections.append(
            {
                "source_alias": connection.source_alias,
                "source_output": connection.source_output,
                "source_output_index": output["index"],
                "source_type": output["type"],
                "target_alias": connection.target_alias,
                "target_input": connection.target_input,
                "target_type": input_type,
            }
        )

    cycle_aliases = _cycle_aliases(set(node_records), resolved_connections)
    if cycle_aliases:
        issues.append(
            _issue(
                "workflow_cycle",
                "connections",
                "Workflow connections contain a cycle involving: " + ", ".join(cycle_aliases),
            )
        )

    for alias, record in node_records.items():
        for input_name, spec in record["inputs"]["required"].items():
            if not _is_connectable(spec):
                continue
            if (alias, input_name) in occupied_targets:
                continue
            issues.append(
                _issue(
                    "missing_required_connection",
                    f"nodes.{alias}.inputs.{input_name}",
                    f"Required input {alias}.{input_name} is not connected.",
                )
            )

    for index, binding in enumerate(request.attachments):
        record = node_records.get(binding.node_alias)
        if record is None:
            issues.append(
                _issue(
                    "unknown_attachment_alias",
                    f"attachments[{index}].node_alias",
                    f"Attachment target alias {binding.node_alias!r} is unresolved.",
                )
            )
            continue
        inputs = {**record["inputs"]["required"], **record["inputs"]["optional"]}
        spec = inputs.get(binding.input_name)
        if spec is None:
            issues.append(
                _issue(
                    "unknown_attachment_input",
                    f"attachments[{index}].input_name",
                    f"{record['node_type']} has no active input named {binding.input_name!r}.",
                )
            )
        elif _is_connectable(spec):
            issues.append(
                _issue(
                    "attachment_input_not_widget",
                    f"attachments[{index}].input_name",
                    f"{binding.node_alias}.{binding.input_name} is a connection slot, not an image widget.",
                )
            )
        else:
            input_type, _ = _input_parts(spec)
            if not isinstance(input_type, list) or not all(
                isinstance(option, str) for option in input_type
            ):
                issues.append(
                    _issue(
                        "attachment_input_not_image_widget",
                        f"attachments[{index}].input_name",
                        f"{binding.node_alias}.{binding.input_name} is not a Load Image-style choice widget.",
                    )
                )

    if partner_aliases:
        issues.append(
            _issue(
                "partner_authentication_may_be_required",
                "requirements.partner_authentication_aliases",
                "Partner/API nodes may require ComfyUI account or provider authentication: "
                + ", ".join(sorted(partner_aliases)),
                severity="warning",
            )
        )

    canonical_nodes = [
        {
            "alias": record["alias"],
            "node_type": record["node_type"],
            "schema_hash": record["schema_hash"],
            "values": record["values"],
        }
        for record in sorted(resolved_nodes, key=lambda item: item["alias"])
    ]
    canonical_connections = sorted(
        resolved_connections,
        key=lambda item: (
            item["source_alias"],
            item["source_output_index"],
            item["target_alias"],
            item["target_input"],
        ),
    )
    canonical_plan = {
        "schema": WORKFLOW_PLAN_SCHEMA,
        "catalog_hash": catalog_hash,
        "nodes": canonical_nodes,
        "connections": canonical_connections,
    }
    if request.attachments:
        canonical_plan["attachments"] = [
            binding.model_dump(mode="json")
            for binding in sorted(
                request.attachments,
                key=lambda item: (item.node_alias, item.input_name),
            )
        ]
    error_count = sum(issue["severity"] == "error" for issue in issues)
    warning_count = sum(issue["severity"] == "warning" for issue in issues)
    plan_hash = _canonical_hash(canonical_plan) if error_count == 0 else None
    return {
        "valid": error_count == 0,
        "plan_hash": plan_hash,
        "plan_hash_schema": WORKFLOW_PLAN_SCHEMA,
        "catalog": {
            "state": "pinned",
            "source": source,
            "node_count": len(catalog),
            "catalog_hash": catalog_hash,
            "catalog_hash_schema": CATALOG_HASH_SCHEMA,
        },
        "plan": canonical_plan,
        "resolved_nodes": [
            _public_node_record(record)
            for record in sorted(resolved_nodes, key=lambda item: item["alias"])
        ],
        "resolved_connections": resolved_connections,
        "requirements": {
            "partner_authentication_aliases": sorted(partner_aliases),
            "output_node_aliases": sorted(output_aliases),
        },
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
    }
