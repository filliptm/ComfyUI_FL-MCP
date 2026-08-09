"""Deterministic semantic resolution against the loaded ComfyUI node catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from html import unescape
from typing import Any, Literal

from node_library import classify_node_origin, node_schema_hash
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from workflow_schema_capabilities import normalize_node_schema

WORKFLOW_SPEC_RESOLUTION_SCHEMA = "fl-mcp.workflow-spec-resolution.v1"
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DYNAMIC_INPUT_TYPES = {
    "COMFY_AUTOGROW_V3",
    "COMFY_DYNAMICCOMBO_V3",
    "COMFY_DYNAMICSLOT_V3",
}
_GENERIC_IDENTITY_TERMS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "image",
    "input",
    "node",
    "of",
    "output",
    "the",
    "to",
    "use",
    "using",
    "video",
    "with",
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
        max_length=1_000,
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

    @field_validator(
        "preferred_node_types",
        "required_input_types",
        "required_output_types",
        "allowed_origins",
        mode="before",
    )
    @classmethod
    def canonicalize_set_fields(cls, value: Any) -> Any:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return sorted(set(value), key=lambda item: (item.casefold(), item))
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> WorkflowCapabilitySpec:
        if not _ALIAS_PATTERN.fullmatch(self.alias):
            raise ValueError(
                "alias must start with a lowercase letter and contain only lowercase "
                "letters, digits, and underscores"
            )
        # Pydantic does not run before-validators for omitted defaults. Apply the
        # same canonical ordering after defaults are populated so an omitted
        # allowed_origins set hashes exactly like its explicit equivalent.
        for field_name in (
            "preferred_node_types",
            "required_input_types",
            "required_output_types",
            "allowed_origins",
        ):
            values = getattr(self, field_name)
            setattr(
                self,
                field_name,
                sorted(set(values), key=lambda value: (value.casefold(), value)),
            )
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


def _longest_contiguous_identity_phrase(
    capability_words: list[str],
    identity_words: list[str],
) -> tuple[str, ...]:
    """Return the longest exact ordered identity phrase present in the intent."""

    if len(capability_words) < 2 or len(identity_words) < 2:
        return ()
    capability = tuple(capability_words)
    maximum = min(len(capability), len(identity_words))
    for length in range(maximum, 1, -1):
        capability_phrases = {
            capability[index : index + length]
            for index in range(len(capability) - length + 1)
        }
        for index in range(len(identity_words) - length + 1):
            phrase = tuple(identity_words[index : index + length])
            if phrase in capability_phrases and any(
                word not in _GENERIC_IDENTITY_TERMS
                and (len(word) >= 3 or any(character.isdigit() for character in word))
                for word in phrase
            ):
                return phrase
    return ()


def _term_matches(token: str, field_word: str) -> bool:
    if token == field_word:
        return True
    common = 0
    for left, right in zip(token, field_word, strict=False):
        if left != right:
            break
        common += 1
    return common >= 5


def _schema_types(
    node_type: str,
    node_info: Mapping[str, Any],
) -> tuple[set[str], set[str], dict[str, Any]]:
    """Return normalized socket types and classified schema evidence."""

    capabilities = normalize_node_schema(node_type, node_info)
    input_types = {
        value
        for item in capabilities.inputs
        if item.connectable or item.kind == "matchtype"
        for value in item.accepted_types
    }
    output_types = {
        value
        for item in capabilities.outputs
        for value in item.produced_types
    }
    return input_types, output_types, capabilities.classification.as_dict()


def _signature_input_types(
    node_type: str,
    node_info: Mapping[str, Any],
) -> set[str]:
    """Return every input type the graph-patch executor can supply.

    Normal capability ranking intentionally reports only sockets and MATCHTYPE
    declarations.  A signature-only fallback must additionally account for
    primitive widgets that GraphPatch can convert to sockets (for example
    ``CreateVideo.fps``).  Keeping this evidence private avoids changing the
    established candidate result contract.
    """

    capabilities = normalize_node_schema(node_type, node_info)
    return {
        value
        for item in capabilities.inputs
        if item.connectable or item.kind == "matchtype" or item.widget_convertible
        for value in item.accepted_types
    }


def _supports_required_types(required: list[str], available: set[str]) -> bool:
    return all(value in available or "*" in available for value in required)


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
    capability_words = _words(capability)
    tokens = list(dict.fromkeys(capability_words))
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

    # Ordered identity phrases distinguish semantic variants that share most of
    # their vocabulary. For example, "Seedance 2.0 reference to video" is much
    # stronger evidence for a Reference variant than the unrelated word "first"
    # elsewhere in "first available reference input". Exact order is required;
    # equal phrase evidence remains an explicit-choice tie below.
    identity_phrase_matches: list[tuple[int, int, str, tuple[str, ...]]] = []
    for field_index, (label, value, _weight) in enumerate(fields):
        if label not in {"node type", "display name", "search alias"}:
            continue
        overlap = _longest_contiguous_identity_phrase(
            capability_words,
            value.split(),
        )
        if not overlap:
            continue
        qualifier_count = sum(
            word not in _GENERIC_IDENTITY_TERMS
            and (len(word) >= 3 or any(character.isdigit() for character in word))
            for word in overlap
        )
        phrase_bonus = len(overlap) * 80 + qualifier_count * 120
        identity_phrase_matches.append(
            (phrase_bonus, -field_index, label, overlap)
        )
    if identity_phrase_matches:
        phrase_bonus, _, label, overlap = max(
            identity_phrase_matches,
            key=lambda item: item[:2],
        )
        score += phrase_bonus
        reasons.append(
            f"contiguous {label} phrase: " + " ".join(overlap)
        )

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
    if (
        len(matched_tokens) < required_token_matches
        and not phrase_matches
        and not identity_matches
        and not identity_phrase_matches
    ):
        return 0, []
    if matched_tokens:
        score += token_score
        reasons.append("matched capability terms: " + ", ".join(matched_tokens))
    return score, reasons


def _origin_bonus(origin: str) -> int:
    # Local-first baseline from the roadmap. Relevance and explicit preferences
    # outweigh this small, stable tie-break policy.
    return {"native": 60, "custom": 40, "partner": 20, "unknown": 0}.get(origin, 0)


def _compact_description(value: Any, *, limit: int = 320) -> str:
    """Keep resolver evidence useful without returning custom-node HTML manuals."""

    plain = unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    compact = " ".join(plain.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _candidate(
    spec: WorkflowCapabilitySpec,
    node_type: str,
    node_info: Mapping[str, Any],
    *,
    signature_only: bool = False,
) -> dict[str, Any] | None:
    origin = classify_node_origin(dict(node_info))
    if origin not in spec.allowed_origins:
        return None
    if bool(node_info.get("deprecated")) and not spec.allow_deprecated:
        return None

    input_types, output_types, compatibility = _schema_types(node_type, node_info)
    eligible_input_types = (
        _signature_input_types(node_type, node_info)
        if signature_only
        else input_types
    )
    if signature_only and ("*" in eligible_input_types or "*" in output_types):
        # A graph-scoped MATCHTYPE wildcard is not an exact I/O signature.  It
        # needs surrounding edge constraints and must never become a resolver
        # shortcut for an otherwise unrelated semantic role.
        return None
    if not _supports_required_types(spec.required_input_types, eligible_input_types):
        return None
    if not _supports_required_types(spec.required_output_types, output_types):
        return None

    text_score, reasons = _capability_score(node_type, node_info, spec.capability)
    primary_identity_match = any(
        reason in {
            "capability explicitly names node type",
            "capability explicitly names display name",
        }
        for reason in reasons
    )
    explicitly_requested = node_type == spec.requested_node_type
    preferred = node_type in spec.preferred_node_types
    if not explicitly_requested and not preferred and text_score <= 0 and not signature_only:
        return None

    if signature_only and text_score <= 0:
        reasons.append("exact I/O signature satisfies the role constraints")

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
        "description": _compact_description(node_info.get("description")),
        "origin": origin,
        "python_module": str(node_info.get("python_module") or ""),
        "api_node": bool(node_info.get("api_node")),
        "deprecated": bool(node_info.get("deprecated")),
        "experimental": bool(node_info.get("experimental")),
        "input_types": sorted(input_types, key=lambda value: (value.casefold(), value)),
        "output_types": sorted(output_types, key=lambda value: (value.casefold(), value)),
        "schema_hash": node_schema_hash(node_type, dict(node_info)),
        "schema_compatibility": compatibility,
        "score": score,
        "match_reasons": reasons,
        "_primary_identity_match": primary_identity_match,
        "_signature_only": signature_only and text_score <= 0,
    }


def _resolve_capability(
    spec: WorkflowCapabilitySpec,
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    path = f"capabilities.{spec.alias}"
    effective_spec = spec

    if spec.requested_node_type and spec.requested_node_type not in catalog:
        requested_display = spec.requested_node_type.strip().casefold()
        display_matches = [
            node_type
            for node_type, node_info in catalog.items()
            if isinstance(node_info, Mapping)
            and isinstance(node_info.get("display_name"), str)
            and node_info["display_name"].strip().casefold() == requested_display
        ]
        display_matches.sort(key=lambda value: (value.casefold(), value))
        if len(display_matches) == 1:
            effective_spec = spec.model_copy(
                update={"requested_node_type": display_matches[0]}
            )
        else:
            code = (
                "requested_node_display_ambiguous"
                if display_matches
                else "requested_node_not_loaded"
            )
            message = (
                f"Explicit node identity {spec.requested_node_type!r} matches multiple "
                "loaded display names; choose one exact class: "
                + ", ".join(display_matches)
                if display_matches
                else f"Explicitly requested class or unique display name "
                f"{spec.requested_node_type!r} is not loaded; no substitute was selected."
            )
            issues.append(
                _issue(code, f"{path}.requested_node_type", message)
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
        resolved = _candidate(effective_spec, node_type, node_info)
        if resolved is not None:
            candidates.append(resolved)

    signature_fallback = False
    if (
        not candidates
        and effective_spec.requested_node_type is None
        and effective_spec.required_input_types
        and effective_spec.required_output_types
    ):
        # Verbose intent can contain no stable name tokens even when its exact
        # I/O contract identifies one locally loaded class.  Apply this only
        # after origin/deprecation/type safety filters, and never choose among
        # multiple signature-compatible classes.
        for node_type in sorted(catalog, key=lambda value: (value.casefold(), value)):
            node_info = catalog[node_type]
            if not isinstance(node_info, Mapping):
                continue
            resolved = _candidate(
                effective_spec,
                node_type,
                node_info,
                signature_only=True,
            )
            if resolved is not None:
                candidates.append(resolved)
        signature_fallback = bool(candidates)

    if effective_spec.requested_node_type:
        candidates = [
            item
            for item in candidates
            if item["node_type"] == effective_spec.requested_node_type
        ]

    def candidate_rank(item: Mapping[str, Any]) -> tuple[bool, int]:
        # An exact node/display identity written into the capability is stronger
        # than a merely preferred historical class. Explicit requested_node_type
        # remains absolute because it filters the candidate set above.
        return bool(item.get("_primary_identity_match")), int(item["score"])

    candidates.sort(
        key=lambda item: (
            -int(bool(item.get("_primary_identity_match"))),
            -item["score"],
            item["node_type"].casefold(),
            item["node_type"],
        )
    )
    signature_candidate_count = len(candidates) if signature_fallback else 0
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
        selected_rank = candidate_rank(selected)
        tied = (
            [item["node_type"] for item in candidates]
            if signature_fallback and signature_candidate_count > 1
            else [
                item["node_type"]
                for item in candidates
                if candidate_rank(item) == selected_rank
            ]
        )
        if len(tied) > 1 or (signature_fallback and signature_candidate_count > 1):
            omitted = signature_candidate_count - len(tied)
            suffix = f" (and {omitted} more)" if omitted > 0 else ""
            issues.append(
                _issue(
                    "ambiguous_local_candidate",
                    path,
                    "Multiple locally loaded classes are equally suitable. Choose one "
                    "explicitly before compiling: " + ", ".join(tied) + suffix,
                )
            )
            selected = None
        elif selected is not None and signature_fallback:
            selected["match_reasons"].insert(
                0,
                "unique local class matching the exact I/O signature",
            )
        if selected is not None and selected["origin"] == "partner":
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
        if selected is not None and selected["origin"] == "unknown":
            issues.append(
                _issue(
                    "unknown_loaded_node_origin",
                    path,
                    "The class is loaded locally, but /object_info provenance did not identify "
                    "it as native, custom, or partner. Inspect its module before execution.",
                    severity="warning",
                )
            )

        if selected is not None and selected["experimental"]:
            issues.append(
                _issue(
                    "experimental_node_selected",
                    path,
                    "The selected loaded class is marked experimental.",
                    severity="warning",
                )
            )
        if (
            selected is not None
            and selected["schema_compatibility"]["status"] == "adapter_required"
        ):
            issues.append(
                _issue(
                    "schema_adapter_required",
                    path,
                    "The selected class uses a loaded schema feature that requires "
                    "runtime graph adaptation. The compiler must materialize and "
                    "verify that adapter before mutation.",
                    severity="warning",
                )
            )

    for candidate in candidates:
        candidate.pop("_primary_identity_match", None)
        candidate.pop("_signature_only", None)

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
        "needs_choice": any(
            item["code"]
            in {"ambiguous_local_candidate", "requested_node_display_ambiguous"}
            for item in issues
        ),
        "resolution_schema": WORKFLOW_SPEC_RESOLUTION_SCHEMA,
        "resolution_hash": resolution_hash,
        "catalog": {
            "source": source,
            "catalog_hash": catalog_hash,
            "node_count": len(catalog),
        },
        "policy": {
            "selection_order": [
                "explicit requested class or unique exact display name",
                "explicit node or display identity in capability",
                "preferred existing or verified-pattern class",
                "semantic capability score",
                "native origin",
                "custom origin",
                "partner origin",
                "unknown origin",
                "explicit user choice when top candidates remain tied",
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
