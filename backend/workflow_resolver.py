"""Deterministic semantic resolution against the loaded ComfyUI node catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from node_library import classify_node_origin, node_schema_hash
from pydantic import BaseModel, ConfigDict, Field, model_validator

WORKFLOW_SPEC_RESOLUTION_SCHEMA = "fl-mcp.workflow-spec-resolution.v1"
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DYNAMIC_INPUT_TYPES = {
    "COMFY_AUTOGROW_V3",
    "COMFY_DYNAMICCOMBO_V3",
    "COMFY_DYNAMICSLOT_V3",
}
NodeOrigin = Literal["native", "custom", "partner", "unknown"]


class WorkflowCapabilitySpec(BaseModel):
    """One semantic role that must resolve to an exact locally loaded class."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Stable lowercase role alias, for example image_loader or sampler.",
    )
    capability: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Concise functional terms, for example 'load image' or 'upscale image'.",
    )
    requested_node_type: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        description=(
            "Exact loaded class explicitly requested by the user. Missing exact classes "
            "fail closed and are never silently substituted."
        ),
    )
    preferred_node_types: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Exact loaded classes from the existing graph or a verified local pattern. "
            "These outrank otherwise equivalent candidates."
        ),
    )
    required_input_types: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Every listed Comfy data type must be accepted by the candidate.",
    )
    required_output_types: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Every listed Comfy data type must be produced by the candidate.",
    )
    allowed_origins: list[NodeOrigin] = Field(
        default_factory=lambda: ["native", "custom", "partner", "unknown"],
        min_length=1,
        max_length=4,
        description="Loaded-node origins permitted for this role.",
    )
    allow_deprecated: bool = Field(
        False,
        description="Permit a loaded class marked deprecated. Disabled by default.",
    )
    max_candidates: int = Field(
        5,
        ge=1,
        le=10,
        description="Maximum ranked local candidates returned for this role.",
    )

    @model_validator(mode="after")
    def validate_contract(self) -> WorkflowCapabilitySpec:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only lowercase "
                "letters, digits, and underscores"
            )
        for field_name in (
            "preferred_node_types",
            "required_input_types",
            "required_output_types",
            "allowed_origins",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
        return self


class ResolveWorkflowSpecRequest(BaseModel):
    """Resolve semantic workflow roles against one pinned /object_info generation."""

    model_config = ConfigDict(extra="forbid")

    capabilities: list[WorkflowCapabilitySpec] = Field(
        ...,
        min_length=1,
        max_length=64,
    )
    expected_catalog_hash: str | None = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Optional catalog hash from node_library_status.",
    )

    @model_validator(mode="after")
    def validate_aliases(self) -> ResolveWorkflowSpecRequest:
        aliases = [item.alias for item in self.capabilities]
        if len(aliases) != len(set(aliases)):
            raise ValueError("capability aliases must be unique")
        payload = self.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False)) > 131_072:
            raise ValueError("workflow capability request must not exceed 128 KiB")
        return self


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


def _words(value: Any) -> list[str]:
    expanded = _CAMEL_BOUNDARY.sub(" ", str(value or ""))
    return _WORD_PATTERN.findall(expanded.casefold())


def _normalized_text(value: Any) -> str:
    return " ".join(_words(value))


def _term_matches(token: str, field_word: str) -> bool:
    if token == field_word:
        return True
    common = 0
    for left, right in zip(token, field_word):
        if left != right:
            break
        common += 1
    return common >= 5


def _input_types(node_info: Mapping[str, Any]) -> set[str]:
    """Collect exact connectable input types, including nested v3 dynamic schemas."""

    found: set[str] = set()
    visited: set[int] = set()

    def visit_spec(value: Any) -> None:
        if not isinstance(value, (list, tuple)) or not value:
            return
        if isinstance(value[0], str):
            input_type = value[0]
            if input_type not in _DYNAMIC_INPUT_TYPES:
                found.add(input_type)
        if len(value) > 1:
            visit_metadata(value[1])

    def visit_metadata(value: Any) -> None:
        if isinstance(value, (dict, list, tuple)):
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key in {"required", "optional"} and isinstance(nested, Mapping):
                    for spec in nested.values():
                        visit_spec(spec)
                else:
                    visit_metadata(nested)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit_metadata(nested)

    inputs = node_info.get("input")
    if isinstance(inputs, Mapping):
        for group in ("required", "optional"):
            specs = inputs.get(group)
            if isinstance(specs, Mapping):
                for spec in specs.values():
                    visit_spec(spec)
    return found


def _output_types(node_info: Mapping[str, Any]) -> set[str]:
    outputs = node_info.get("output")
    if not isinstance(outputs, (list, tuple)):
        return set()
    return {str(item) for item in outputs if isinstance(item, str)}


def _search_fields(node_type: str, node_info: Mapping[str, Any]) -> list[tuple[str, str, int]]:
    aliases = node_info.get("search_aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return [
        ("node type", _normalized_text(node_type), 700),
        ("display name", _normalized_text(node_info.get("display_name") or node_type), 650),
        *[
            ("search alias", _normalized_text(alias), 600)
            for alias in aliases
            if str(alias).strip()
        ],
        ("category", _normalized_text(node_info.get("category")), 320),
        ("description", _normalized_text(node_info.get("description")), 220),
    ]


def _capability_score(
    node_type: str,
    node_info: Mapping[str, Any],
    capability: str,
) -> tuple[int, list[str]]:
    phrase = _normalized_text(capability)
    tokens = list(dict.fromkeys(_words(capability)))
    if not phrase or not tokens:
        return 0, []

    fields = _search_fields(node_type, node_info)
    identity_matches: list[tuple[int, str]] = []
    for label, value, weight in fields:
        if label not in {"node type", "display name", "search alias"}:
            continue
        # A user-facing node name embedded in a longer capability phrase is a
        # stronger signal than a description that happens to share many terms.
        # This keeps requests such as "Nano Banana 2 image editing" pinned to the
        # locally loaded node whose display name is exactly "Nano Banana 2".
        if len(value.split()) >= 2 and value in phrase:
            identity_matches.append(
                (weight + 2_500, f"capability explicitly names {label}")
            )
    phrase_matches: list[tuple[int, str]] = []
    for label, value, weight in fields:
        if value == phrase:
            phrase_matches.append((weight + 300, f"exact {label} capability match"))
        elif phrase in value:
            phrase_matches.append((weight, f"{label} contains capability phrase"))

    reasons: list[str] = []
    score = 0
    if identity_matches:
        identity_score, reason = max(identity_matches, key=lambda item: item[0])
        score += identity_score
        reasons.append(reason)
    if phrase_matches:
        phrase_score, reason = max(phrase_matches, key=lambda item: item[0])
        score += phrase_score
        reasons.append(reason)

    token_score = 0
    matched_tokens: list[str] = []
    for token in tokens:
        best = 0
        for _, value, weight in fields:
            if any(_term_matches(token, field_word) for field_word in value.split()):
                best = max(best, max(20, weight // 8))
        if best:
            token_score += best
            matched_tokens.append(token)
    required_token_matches = (
        1 if len(tokens) == 1 else 2 if len(tokens) == 2 else (len(tokens) + 1) // 2
    )
    if len(matched_tokens) < required_token_matches and not phrase_matches:
        return 0, []
    if matched_tokens:
        score += token_score
        reasons.append("matched capability terms: " + ", ".join(matched_tokens))
    return score, reasons


def _origin_bonus(origin: str) -> int:
    # Local-first baseline from the roadmap. Relevance and explicit preferences
    # outweigh this small, stable tie-break policy.
    return {"native": 60, "custom": 40, "partner": 20, "unknown": 0}.get(origin, 0)


def _candidate(
    spec: WorkflowCapabilitySpec,
    node_type: str,
    node_info: Mapping[str, Any],
) -> dict[str, Any] | None:
    origin = classify_node_origin(dict(node_info))
    if origin not in spec.allowed_origins:
        return None
    if bool(node_info.get("deprecated")) and not spec.allow_deprecated:
        return None

    input_types = _input_types(node_info)
    output_types = _output_types(node_info)
    if not set(spec.required_input_types).issubset(input_types):
        return None
    if not set(spec.required_output_types).issubset(output_types):
        return None

    text_score, reasons = _capability_score(node_type, node_info, spec.capability)
    explicitly_requested = node_type == spec.requested_node_type
    preferred = node_type in spec.preferred_node_types
    if not explicitly_requested and not preferred and text_score <= 0:
        return None

    score = text_score + _origin_bonus(origin)
    if preferred:
        score += 5_000
        reasons.insert(0, "preferred class from existing graph or verified pattern")
    if explicitly_requested:
        score += 10_000
        reasons.insert(0, "exact class explicitly requested")
    reasons.append(f"{origin} origin policy bonus {_origin_bonus(origin)}")

    return {
        "node_type": node_type,
        "display_name": str(node_info.get("display_name") or node_type),
        "category": str(node_info.get("category") or ""),
        "description": str(node_info.get("description") or ""),
        "origin": origin,
        "python_module": str(node_info.get("python_module") or ""),
        "api_node": bool(node_info.get("api_node")),
        "deprecated": bool(node_info.get("deprecated")),
        "experimental": bool(node_info.get("experimental")),
        "input_types": sorted(input_types, key=lambda value: (value.casefold(), value)),
        "output_types": sorted(output_types, key=lambda value: (value.casefold(), value)),
        "schema_hash": node_schema_hash(node_type, dict(node_info)),
        "score": score,
        "match_reasons": reasons,
    }


def _resolve_capability(
    spec: WorkflowCapabilitySpec,
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    path = f"capabilities.{spec.alias}"

    if spec.requested_node_type and spec.requested_node_type not in catalog:
        issues.append(
            _issue(
                "requested_node_not_loaded",
                f"{path}.requested_node_type",
                f"Explicitly requested class {spec.requested_node_type!r} is not loaded; "
                "no substitute was selected.",
            )
        )
        return {
            "alias": spec.alias,
            "capability": spec.capability,
            "selected": None,
            "candidates": [],
        }, issues

    candidates: list[dict[str, Any]] = []
    for node_type in sorted(catalog, key=lambda value: (value.casefold(), value)):
        node_info = catalog[node_type]
        if not isinstance(node_info, Mapping):
            continue
        resolved = _candidate(spec, node_type, node_info)
        if resolved is not None:
            candidates.append(resolved)

    if spec.requested_node_type:
        candidates = [
            item for item in candidates if item["node_type"] == spec.requested_node_type
        ]

    candidates.sort(
        key=lambda item: (-item["score"], item["node_type"].casefold(), item["node_type"])
    )
    candidates = candidates[: spec.max_candidates]
    selected = candidates[0] if candidates else None

    if selected is None:
        code = "requested_node_rejected" if spec.requested_node_type else "no_local_candidate"
        message = (
            f"Explicitly requested class {spec.requested_node_type!r} violates the role's "
            "origin, type, or deprecation guardrails."
            if spec.requested_node_type
            else "No locally loaded node satisfies the capability, type, and origin constraints."
        )
        issues.append(_issue(code, path, message))
    else:
        tied = [item["node_type"] for item in candidates if item["score"] == selected["score"]]
        if len(tied) > 1:
            issues.append(
                _issue(
                    "lexical_tiebreak_applied",
                    path,
                    "Equivalent top scores were resolved by stable lexical class order: "
                    + ", ".join(tied),
                    severity="warning",
                )
            )
        if selected["origin"] == "partner":
            issues.append(
                _issue(
                    "partner_authentication_cost_privacy_review_required",
                    path,
                    "The selected partner/API node may require account authentication, "
                    "consume provider credits, and send workflow inputs to a hosted service. "
                    "Review those conditions before execution.",
                    severity="warning",
                )
            )
        if selected["origin"] == "unknown":
            issues.append(
                _issue(
                    "unknown_loaded_node_origin",
                    path,
                    "The class is loaded locally, but /object_info provenance did not identify "
                    "it as native, custom, or partner. Inspect its module before execution.",
                    severity="warning",
                )
            )
        if selected["experimental"]:
            issues.append(
                _issue(
                    "experimental_node_selected",
                    path,
                    "The selected loaded class is marked experimental.",
                    severity="warning",
                )
            )

    return {
        "alias": spec.alias,
        "capability": spec.capability,
        "selected": selected,
        "candidates": candidates,
    }, issues


def resolve_workflow_spec(
    request: ResolveWorkflowSpecRequest,
    catalog: Mapping[str, Any],
    *,
    catalog_hash: str,
    source: str,
) -> dict[str, Any]:
    """Resolve all semantic roles deterministically against one catalog snapshot."""

    issues: list[dict[str, str]] = []
    resolutions: list[dict[str, Any]] = []
    if request.expected_catalog_hash and request.expected_catalog_hash != catalog_hash:
        issues.append(
            _issue(
                "catalog_changed",
                "expected_catalog_hash",
                "The loaded-node catalog changed. Refresh discovery and resolve again.",
            )
        )
    else:
        for spec in request.capabilities:
            resolution, role_issues = _resolve_capability(spec, catalog)
            resolutions.append(resolution)
            issues.extend(role_issues)

    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    valid = error_count == 0 and len(resolutions) == len(request.capabilities)
    resolved_contract = {
        "schema": WORKFLOW_SPEC_RESOLUTION_SCHEMA,
        "catalog_hash": catalog_hash,
        "request": request.model_dump(mode="json"),
        "resolutions": resolutions,
    }
    resolution_hash = _canonical_hash(resolved_contract) if valid else None

    return {
        "valid": valid,
        "resolution_schema": WORKFLOW_SPEC_RESOLUTION_SCHEMA,
        "resolution_hash": resolution_hash,
        "catalog": {
            "source": source,
            "catalog_hash": catalog_hash,
            "node_count": len(catalog),
        },
        "policy": {
            "selection_order": [
                "explicit requested class",
                "preferred existing or verified-pattern class",
                "semantic capability score",
                "native origin",
                "custom origin",
                "partner origin",
                "unknown origin",
                "lexical node-class tie-break",
            ],
            "registry_candidates_eligible": False,
            "local_catalog_only": True,
            "deprecated_excluded_by_default": True,
        },
        "resolutions": resolutions,
        "selected_node_types": {
            item["alias"]: item["selected"]["node_type"]
            for item in resolutions
            if item["selected"] is not None
        },
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
    }
