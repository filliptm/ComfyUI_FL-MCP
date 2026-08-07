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

WORKFLOW_COMPILER_SCHEMA = "fl-mcp.workflow-spec-compiler.v1"
_NORMALIZED_NAME = re.compile(r"[^a-z0-9]+")


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


def _resolve_runtime_name(
    requested: str,
    available: list[str],
    *,
    path: str,
    kind: str,
) -> tuple[str | None, list[dict[str, str]]]:
    matches = _runtime_name_matches(requested, available)
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
) -> dict[str, Any]:
    """Choose stable first options so their dependent dotted inputs can expand."""

    defaults: dict[str, Any] = {}
    inputs = node_info.get("input")
    if not isinstance(inputs, Mapping):
        return defaults
    for group in ("required", "optional"):
        specs = inputs.get(group)
        if not isinstance(specs, Mapping):
            continue
        for input_name, spec in specs.items():
            input_type, _ = _input_parts(spec)
            if input_type != "COMFY_DYNAMICCOMBO_V3" or input_name in requested_values:
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


def _canonicalize_node_values(
    node: WorkflowSpecNode,
    node_info: Mapping[str, Any],
    attachment_inputs: set[str],
    connection_hints: set[str],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    seed_values = dict(node.values)
    selector_defaults = _dynamic_selector_defaults(node_info, seed_values)
    seed_values.update(selector_defaults)
    first_inputs, _ = _expanded_inputs(node_info, seed_values, connection_hints)
    available = sorted(first_inputs)
    values: dict[str, Any] = dict(selector_defaults)
    for requested, value in node.values.items():
        resolved, name_issues = _resolve_runtime_name(
            requested,
            available,
            path=f"nodes.{node.alias}.values.{requested}",
            kind="widget",
        )
        issues.extend(name_issues)
        if resolved is not None:
            values[resolved] = value

    active_inputs, dynamic_issues = _expanded_inputs(node_info, values, connection_hints)
    for item in dynamic_issues:
        issues.append(
            {**item, "path": f"nodes.{node.alias}.{item['path']}"}
        )

    for input_name, spec in active_inputs.items():
        if input_name in values or input_name in attachment_inputs or _is_connectable(spec):
            continue
        has_default, default = _stable_default(spec)
        if has_default:
            values[input_name] = default

    active_inputs, final_dynamic_issues = _expanded_inputs(
        node_info,
        values,
        connection_hints,
    )
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
    validated_attachment_values: Mapping[tuple[str, str], str],
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
    issues = [item for item in resolution["issues"] if item["severity"] == "error"]
    partner_review = _partner_review(selected, catalog)
    if issues:
        return {
            "valid": False,
            "compiler_schema": WORKFLOW_COMPILER_SCHEMA,
            "resolution_hash": resolution["resolution_hash"],
            "plan_hash": None,
            "catalog": resolution["catalog"],
            "selected_node_types": selected,
            "partner_review": partner_review,
            "issues": issues,
            "error_count": len(issues),
            "warning_count": 0,
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
    exact_attachment_values: dict[tuple[str, str], str] = {}
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
            "warning_count": 0,
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
    return {
        "valid": compiled["valid"],
        "compiler_schema": WORKFLOW_COMPILER_SCHEMA,
        "resolution_hash": resolution["resolution_hash"],
        "plan_hash": compiled["plan_hash"],
        "catalog": compiled["catalog"],
        "selected_node_types": selected,
        "plan": compiled["plan"],
        "partner_review": partner_review,
        "issues": compiled["issues"],
        "error_count": compiled["error_count"],
        "warning_count": compiled["warning_count"],
        "apply_request": apply_request,
    }
