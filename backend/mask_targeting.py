"""Deterministic mask-target resolution and short-lived edit attestations."""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

MAX_MASK_TARGET_CANDIDATES = 8
MASK_CONTEXT_TTL_SECONDS = 300.0
MAX_MASK_CONTEXT_TOKENS = 512
MAX_MASK_IMAGE_FILENAME_BYTES = 255
MAX_MASK_IMAGE_SUBFOLDER_BYTES = 512
MAX_MASK_AUTHORITY_TEXT_BYTES = 256
MAX_IMAGE_ROUTE_HOPS = 8
MAX_IMAGE_ROUTE_VISITS = 32
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _typed_id_key(value: Any) -> tuple[str, Any]:
    """Keep frontend-exact numeric and string node IDs distinct."""

    return type(value).__name__, value


def _node_items(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_nodes = workflow.get("nodes")
    if isinstance(raw_nodes, list):
        values = raw_nodes
    elif isinstance(raw_nodes, Mapping):
        values = []
        for mapping_key, raw_node in raw_nodes.items():
            if isinstance(raw_node, Mapping) and "id" not in raw_node:
                values.append({**raw_node, "id": mapping_key})
            else:
                values.append(raw_node)
    else:
        return []
    return [node for node in values if isinstance(node, Mapping)]


def _node_id(node: Mapping[str, Any]) -> int | str | None:
    value = node.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return value


def _node_type(node: Mapping[str, Any]) -> str:
    return str(node.get("type") or node.get("comfyClass") or node.get("class_type") or "")


def _slot_type(slot: Any) -> str:
    return str(slot.get("type") or "").upper() if isinstance(slot, Mapping) else ""


def _slot_name(slot: Any) -> str:
    return str(slot.get("name") or "") if isinstance(slot, Mapping) else ""


def _slot_type_options(value: str) -> set[str] | None:
    """Return exact serialized socket types, or None for an unconstrained wildcard."""

    normalized = str(value or "").upper().strip()
    if not normalized or normalized == "*":
        return None
    return {item.strip() for item in normalized.split(",") if item.strip()}


def _is_mask_image_candidate(node: Mapping[str, Any]) -> bool:
    output_types = {
        _slot_type(output)
        for output in node.get("outputs", [])
        if isinstance(output, Mapping)
    }
    return {"IMAGE", "MASK"} <= output_types


def _link_parts(link: Any) -> tuple[Any, int, Any, int, str] | None:
    if isinstance(link, Mapping):
        source_id = link.get("origin_id", link.get("source_node_id"))
        source_slot = link.get("origin_slot", link.get("source_output_index"))
        target_id = link.get("target_id", link.get("target_node_id"))
        target_slot = link.get("target_slot", link.get("target_input_index"))
        link_type = link.get("type", "")
    elif isinstance(link, (list, tuple)) and len(link) >= 6:
        _, source_id, source_slot, target_id, target_slot, link_type = link[:6]
    else:
        return None
    if (
        isinstance(source_id, bool)
        or not isinstance(source_id, (int, str))
        or isinstance(target_id, bool)
        or not isinstance(target_id, (int, str))
        or isinstance(source_slot, bool)
        or not isinstance(source_slot, int)
        or isinstance(target_slot, bool)
        or not isinstance(target_slot, int)
    ):
        return None
    return source_id, source_slot, target_id, target_slot, str(link_type or "").upper()


def _workflow_links(
    workflow: Mapping[str, Any],
) -> list[tuple[Any, int, Any, int, str]] | None:
    raw_links = workflow.get("links", [])
    values = list(raw_links.values()) if isinstance(raw_links, Mapping) else raw_links
    if not isinstance(values, list):
        return None
    nodes_by_id = _unique_nodes_by_id(workflow)
    if not nodes_by_id and _node_items(workflow):
        return None
    links: list[tuple[Any, int, Any, int, str]] = []
    seen_targets: set[tuple[tuple[str, Any], int]] = set()
    for value in values:
        parts = _link_parts(value)
        if parts is None:
            return None
        source_id, source_slot, target_id, target_slot, link_type = parts
        if source_slot < 0 or target_slot < 0:
            return None
        source_node = nodes_by_id.get(_typed_id_key(source_id))
        target_node = nodes_by_id.get(_typed_id_key(target_id))
        if source_node is None or target_node is None:
            return None
        source_outputs = source_node.get("outputs", [])
        target_inputs = target_node.get("inputs", [])
        if (
            not isinstance(source_outputs, list)
            or not isinstance(target_inputs, list)
            or source_slot >= len(source_outputs)
            or target_slot >= len(target_inputs)
        ):
            return None
        source_types = _slot_type_options(_slot_type(source_outputs[source_slot]))
        target_types = _slot_type_options(_slot_type(target_inputs[target_slot]))
        declared_types = _slot_type_options(link_type)
        if source_types is not None and target_types is not None and not (
            source_types & target_types
        ):
            return None
        if declared_types is not None and (
            (source_types is not None and not declared_types & source_types)
            or (target_types is not None and not declared_types & target_types)
        ):
            # Never manufacture IMAGE/MASK authority from a contradictory
            # link.type field. Endpoint socket declarations remain primary.
            return None
        target_key = (_typed_id_key(target_id), target_slot)
        if target_key in seen_targets:
            # A serialized input slot has exactly one link. Treat conflicting
            # authority as unusable instead of accepting array order.
            return None
        seen_targets.add(target_key)
        links.append((source_id, source_slot, target_id, target_slot, link_type))
    return links


def _unique_nodes_by_id(workflow: Mapping[str, Any]) -> dict[tuple[str, Any], Mapping[str, Any]]:
    nodes_by_id: dict[tuple[str, Any], Mapping[str, Any]] = {}
    for node in _node_items(workflow):
        node_id = _node_id(node)
        if node_id is None:
            continue
        key = _typed_id_key(node_id)
        if key in nodes_by_id:
            return {}
        nodes_by_id[key] = node
    return nodes_by_id


@dataclass(frozen=True, slots=True)
class MaskTargetCandidate:
    node_id: int | str
    node_type: str
    title: str
    connected_output_types: tuple[str, ...]
    downstream_node_ids: tuple[int | str, ...]
    topology_score: int

    def public(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "title": self.title,
            "connected_output_types": list(self.connected_output_types),
            "downstream_node_ids": list(self.downstream_node_ids),
            "topology_score": self.topology_score,
        }


@dataclass(frozen=True, slots=True)
class MaskTargetResolution:
    target: MaskTargetCandidate | None
    candidates: tuple[MaskTargetCandidate, ...]
    reason: str
    prompt_producers: tuple[dict[str, Any], ...] = ()

    @property
    def needs_choice(self) -> bool:
        return self.target is None


@dataclass(frozen=True, slots=True)
class PromptProducerCandidate:
    producer_node_id: int | str
    producer_node_type: str
    producer_output: str
    producer_output_index: int
    consumer_node_id: int | str
    consumer_node_type: str
    consumer_input: str
    consumer_input_index: int
    score: int

    def public(self) -> dict[str, Any]:
        return {
            "producer_node_id": self.producer_node_id,
            "producer_node_type": self.producer_node_type,
            "producer_output": self.producer_output,
            "producer_output_index": self.producer_output_index,
            "consumer_node_id": self.consumer_node_id,
            "consumer_node_type": self.consumer_node_type,
            "consumer_input": self.consumer_input,
            "consumer_input_index": self.consumer_input_index,
        }


@dataclass(frozen=True, slots=True)
class PromptProducerResolution:
    target: PromptProducerCandidate | None
    candidates: tuple[PromptProducerCandidate, ...]
    reason: str

    @property
    def needs_choice(self) -> bool:
        return self.target is None


@dataclass(frozen=True, slots=True)
class DirectPromptWidgetCandidate:
    """One unconnected STRING prompt input backed by its consumer node widget."""

    node_id: int | str
    node_type: str
    consumer_input: str
    consumer_input_index: int
    producer_widget: str
    score: int

    @property
    def producer_node_id(self) -> int | str:
        return self.node_id

    @property
    def producer_node_type(self) -> str:
        return self.node_type

    @property
    def consumer_node_id(self) -> int | str:
        return self.node_id

    @property
    def consumer_node_type(self) -> str:
        return self.node_type

    def public(self) -> dict[str, Any]:
        return {
            "target_mode": "direct_widget",
            "producer_node_id": self.node_id,
            "producer_node_type": self.node_type,
            "producer_widget": self.producer_widget,
            "consumer_node_id": self.node_id,
            "consumer_node_type": self.node_type,
            "consumer_input": self.consumer_input,
            "consumer_input_index": self.consumer_input_index,
        }


@dataclass(frozen=True, slots=True)
class DirectPromptWidgetResolution:
    target: DirectPromptWidgetCandidate | None
    candidates: tuple[DirectPromptWidgetCandidate, ...]
    reason: str

    @property
    def needs_choice(self) -> bool:
        return self.target is None


@dataclass(frozen=True, slots=True)
class ReferenceImageCandidate:
    producer_node_id: int | str
    producer_node_type: str
    producer_output: str
    producer_output_index: int
    consumer_node_id: int | str
    consumer_node_type: str
    consumer_input: str
    consumer_input_index: int
    score: int
    route_node_ids: tuple[int | str, ...] = ()
    direct_producer_node_id: int | str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "producer_node_id": self.producer_node_id,
            "producer_node_type": self.producer_node_type,
            "producer_output": self.producer_output,
            "producer_output_index": self.producer_output_index,
            "consumer_node_id": self.consumer_node_id,
            "consumer_node_type": self.consumer_node_type,
            "consumer_input": self.consumer_input,
            "consumer_input_index": self.consumer_input_index,
            "route_node_ids": list(self.route_node_ids),
            "direct_producer_node_id": self.direct_producer_node_id,
        }


@dataclass(frozen=True, slots=True)
class ReferenceImageResolution:
    target: ReferenceImageCandidate | None
    candidates: tuple[ReferenceImageCandidate, ...]
    reason: str

    @property
    def needs_choice(self) -> bool:
        return self.target is None


def _candidate_facts(
    workflow: Mapping[str, Any],
) -> tuple[list[MaskTargetCandidate], dict[tuple[str, Any], Mapping[str, Any]]]:
    nodes = _node_items(workflow)
    nodes_by_id = _unique_nodes_by_id(workflow)
    if not nodes_by_id and nodes:
        return [], {}
    links = _workflow_links(workflow)
    if links is None:
        return [], nodes_by_id
    candidates: list[MaskTargetCandidate] = []
    for node in nodes:
        node_id = _node_id(node)
        if node_id is None or not _is_mask_image_candidate(node):
            continue
        output_types = [
            _slot_type(output)
            for output in node.get("outputs", [])
            if isinstance(output, Mapping)
        ]
        connected_types: set[str] = set()
        downstream_ids: set[tuple[str, Any]] = set()
        mask_connections = 0
        image_connections = 0
        for source_id, source_slot, target_id, _target_slot, link_type in links:
            if _typed_id_key(source_id) != _typed_id_key(node_id):
                continue
            output_type = output_types[source_slot] if source_slot < len(output_types) else link_type
            if output_type:
                connected_types.add(output_type)
            downstream_ids.add(_typed_id_key(target_id))
            if output_type == "MASK" or link_type == "MASK":
                mask_connections += 1
            if output_type == "IMAGE" or link_type == "IMAGE":
                image_connections += 1
        topology_score = mask_connections * 10 + image_connections * 2
        if mask_connections and image_connections:
            topology_score += 5
        ordered_downstream = tuple(
            key[1] for key in sorted(downstream_ids, key=lambda item: (item[0], str(item[1])))
        )
        candidates.append(
            MaskTargetCandidate(
                node_id=node_id,
                node_type=_node_type(node),
                title=str(node.get("title") or _node_type(node) or node_id),
                connected_output_types=tuple(sorted(connected_types)),
                downstream_node_ids=ordered_downstream,
                topology_score=topology_score,
            )
        )
    candidates.sort(key=lambda item: (-item.topology_score, type(item.node_id).__name__, str(item.node_id)))
    return candidates, nodes_by_id


def _prompt_producer_evidence(
    workflow: Mapping[str, Any],
    target: MaskTargetCandidate,
) -> tuple[dict[str, Any], ...]:
    """Describe direct STRING producers on the target's immediate downstream nodes."""

    downstream = {_typed_id_key(value) for value in target.downstream_node_ids}
    resolution = resolve_connected_prompt_producer(workflow)
    evidence: list[dict[str, Any]] = []
    for candidate in resolution.candidates:
        if _typed_id_key(candidate.consumer_node_id) in downstream:
            evidence.append(candidate.public())
    if resolution.target is not None and not evidence:
        candidate = resolution.target
        if _typed_id_key(candidate.consumer_node_id) in downstream:
            evidence.append(candidate.public())
    return tuple(evidence[:MAX_MASK_TARGET_CANDIDATES])


def resolve_connected_prompt_producer(
    workflow: Mapping[str, Any],
    *,
    consumer_node_id: int | str | None = None,
    consumer_input: str | None = None,
) -> PromptProducerResolution:
    """Resolve one exact connected STRING producer feeding a prompt input."""

    nodes = _node_items(workflow)
    nodes_by_id = _unique_nodes_by_id(workflow)
    if not nodes_by_id and nodes:
        return PromptProducerResolution(None, (), "invalid_duplicate_node_ids")
    candidates: list[PromptProducerCandidate] = []
    links = _workflow_links(workflow)
    if links is None:
        return PromptProducerResolution(None, (), "invalid_workflow_links")
    for source_id, source_slot, target_id, target_slot, link_type in links:
        if (
            consumer_node_id is not None
            and _typed_id_key(target_id) != _typed_id_key(consumer_node_id)
        ):
            continue
        target_key = _typed_id_key(target_id)
        target_node = nodes_by_id.get(target_key)
        source_node = nodes_by_id.get(_typed_id_key(source_id))
        if target_node is None or source_node is None:
            continue
        target_inputs = target_node.get("inputs", [])
        source_outputs = source_node.get("outputs", [])
        target_input = target_inputs[target_slot] if target_slot < len(target_inputs) else {}
        source_output = source_outputs[source_slot] if source_slot < len(source_outputs) else {}
        input_name = _slot_name(target_input)
        if (
            consumer_input is not None
            and _normalized_role_name(input_name) != _normalized_role_name(consumer_input)
        ):
            continue
        input_type = _slot_type(target_input) or link_type
        output_type = _slot_type(source_output) or link_type
        if output_type != "STRING" and link_type != "STRING":
            continue
        if input_type != "STRING" and "prompt" not in input_name.casefold():
            continue
        normalized_input = _normalized_role_name(input_name)
        if normalized_input in {"prompt", "positive", "positiveprompt", "mainprompt"}:
            score = 20
        elif "negative" in normalized_input:
            score = 5
        elif "prompt" in normalized_input:
            score = 10
        else:
            score = 0
        score += 2 if input_type == "STRING" else 0
        score += 1 if output_type == "STRING" else 0
        candidates.append(
            PromptProducerCandidate(
                producer_node_id=source_id,
                producer_node_type=_node_type(source_node),
                producer_output=_slot_name(source_output),
                producer_output_index=source_slot,
                consumer_node_id=target_id,
                consumer_node_type=_node_type(target_node),
                consumer_input=input_name,
                consumer_input_index=target_slot,
                score=score,
            )
        )
    candidates.sort(
        key=lambda item: (
            -item.score,
            type(item.producer_node_id).__name__,
            str(item.producer_node_id),
            item.producer_output_index,
        )
    )
    if candidates and (
        len(candidates) == 1 or candidates[0].score > candidates[1].score
    ):
        return PromptProducerResolution(
            target=candidates[0],
            candidates=(candidates[0],),
            reason="unique_connected_prompt_producer",
        )
    return PromptProducerResolution(
        target=None,
        candidates=tuple(candidates[:MAX_MASK_TARGET_CANDIDATES]),
        reason="ambiguous_prompt_producers" if candidates else "no_connected_prompt_producer",
    )


def resolve_direct_prompt_widget(
    workflow: Mapping[str, Any],
    *,
    consumer_node_id: int | str | None = None,
    consumer_input: str | None = None,
) -> DirectPromptWidgetResolution:
    """Resolve one exact unconnected STRING prompt input on its owning node.

    A direct widget is eligible only while no serialized link targets that input
    slot. Generic STRING widgets are never guessed: without an explicit input
    name, the socket itself must have a prompt role.
    """

    nodes = _node_items(workflow)
    nodes_by_id = _unique_nodes_by_id(workflow)
    if not nodes_by_id and nodes:
        return DirectPromptWidgetResolution(None, (), "invalid_duplicate_node_ids")
    links = _workflow_links(workflow)
    if links is None:
        return DirectPromptWidgetResolution(None, (), "invalid_workflow_links")
    connected_inputs = {
        (_typed_id_key(target_id), target_slot)
        for _source_id, _source_slot, target_id, target_slot, _link_type in links
    }
    requested_role = (
        _normalized_role_name(consumer_input)
        if consumer_input is not None
        else None
    )
    candidates: list[DirectPromptWidgetCandidate] = []
    for node in nodes:
        node_id = _node_id(node)
        if node_id is None:
            continue
        if (
            consumer_node_id is not None
            and _typed_id_key(node_id) != _typed_id_key(consumer_node_id)
        ):
            continue
        inputs = node.get("inputs", [])
        if not isinstance(inputs, list):
            continue
        for input_index, input_slot in enumerate(inputs):
            if not isinstance(input_slot, Mapping):
                continue
            input_name = _slot_name(input_slot)
            normalized_input = _normalized_role_name(input_name)
            widget = input_slot.get("widget")
            widget_name = (
                widget.get("name")
                if isinstance(widget, Mapping)
                else None
            )
            if (
                not input_name
                or _slot_type(input_slot) != "STRING"
                or (_typed_id_key(node_id), input_index) in connected_inputs
                or input_slot.get("link") is not None
                or not isinstance(widget_name, str)
                or not widget_name
                or len(widget_name.encode("utf-8")) > 256
            ):
                continue
            # An explicitly supplied input is an identity selector, not a
            # fuzzy role hint. Keep punctuation/case distinctions exact so a
            # request can never drift onto a similarly named STRING widget.
            if consumer_input is not None and input_name != consumer_input:
                continue
            if requested_role is None and "prompt" not in normalized_input:
                continue
            if normalized_input in {"prompt", "positive", "positiveprompt", "mainprompt"}:
                score = 20
            elif "negative" in normalized_input:
                score = 5
            elif "prompt" in normalized_input:
                score = 10
            else:
                # A non-prompt STRING input is eligible only when the caller
                # named that exact input explicitly.
                score = 1
            candidates.append(
                DirectPromptWidgetCandidate(
                    node_id=node_id,
                    node_type=_node_type(node),
                    consumer_input=input_name,
                    consumer_input_index=input_index,
                    producer_widget=widget_name,
                    score=score,
                )
            )
    candidates.sort(
        key=lambda item: (
            -item.score,
            type(item.node_id).__name__,
            str(item.node_id),
            item.consumer_input_index,
        )
    )
    candidate_nodes = {_typed_id_key(item.node_id) for item in candidates}
    if (
        candidates
        and (consumer_node_id is not None or len(candidate_nodes) == 1)
        and (len(candidates) == 1 or candidates[0].score > candidates[1].score)
    ):
        return DirectPromptWidgetResolution(
            target=candidates[0],
            candidates=(candidates[0],),
            reason="unique_direct_prompt_widget",
        )
    return DirectPromptWidgetResolution(
        target=None,
        candidates=tuple(candidates[:MAX_MASK_TARGET_CANDIDATES]),
        reason=(
            "ambiguous_direct_prompt_widgets"
            if candidates
            else "no_unconnected_prompt_widget"
        ),
    )


def _normalized_role_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_exact_image_owner(node: Mapping[str, Any]) -> bool:
    """Return true only for nodes whose widget names one exact Comfy image."""

    return _normalized_role_name(_node_type(node)) in {"loadimage", "loadimagemask"}


def _is_safe_image_route_node(node: Mapping[str, Any]) -> bool:
    """Limit upstream tracing to presentation-preserving image plumbing."""

    normalized = _normalized_role_name(_node_type(node))
    return any(
        marker in normalized
        for marker in (
            "resize", "rescale", "scaleimage", "imagescale", "imageupscale",
            "padimage", "imagepad", "reroute", "batch",
        )
    )


def _unique_upstream_image_owners(
    workflow: Mapping[str, Any],
    *,
    producer_node_id: int | str,
    producer_output_index: int,
) -> list[tuple[int | str, int, tuple[int | str, ...]]]:
    """Trace one bounded IMAGE route back to exact LoadImage-like owners."""

    nodes_by_id = _unique_nodes_by_id(workflow)
    links = _workflow_links(workflow)
    if links is None:
        return []
    owners: list[tuple[int | str, int, tuple[int | str, ...]]] = []
    stack = [(producer_node_id, producer_output_index, (producer_node_id,), 0)]
    visits = 0
    while stack and visits < MAX_IMAGE_ROUTE_VISITS:
        node_id, output_index, reverse_path, depth = stack.pop()
        visits += 1
        node = nodes_by_id.get(_typed_id_key(node_id))
        if node is None:
            continue
        if _is_exact_image_owner(node):
            owners.append((node_id, output_index, tuple(reversed(reverse_path))))
            continue
        if depth >= MAX_IMAGE_ROUTE_HOPS or not _is_safe_image_route_node(node):
            continue
        upstream: list[tuple[int | str, int]] = []
        for source_id, source_slot, target_id, target_slot, link_type in links:
            if _typed_id_key(target_id) != _typed_id_key(node_id):
                continue
            inputs = node.get("inputs", [])
            target_input = inputs[target_slot] if target_slot < len(inputs) else {}
            if (_slot_type(target_input) or link_type) == "IMAGE":
                upstream.append((source_id, source_slot))
        # A multi-input batch/merge does not identify one source image.
        if len(upstream) != 1:
            continue
        source_id, source_slot = upstream[0]
        if any(_typed_id_key(source_id) == _typed_id_key(item) for item in reverse_path):
            continue
        stack.append((source_id, source_slot, (*reverse_path, source_id), depth + 1))
    deduped: dict[tuple[tuple[str, Any], int], tuple[int | str, int, tuple[int | str, ...]]] = {}
    for owner in owners:
        deduped[(_typed_id_key(owner[0]), owner[1])] = owner
    return list(deduped.values())[:MAX_MASK_TARGET_CANDIDATES]


def resolve_reference_image_producer(
    workflow: Mapping[str, Any],
    *,
    consumer_node_id: int | str | None = None,
) -> ReferenceImageResolution:
    """Resolve an IMAGE producer feeding an image2/reference role input."""

    nodes = _node_items(workflow)
    nodes_by_id = _unique_nodes_by_id(workflow)
    if not nodes_by_id and nodes:
        return ReferenceImageResolution(None, (), "invalid_duplicate_node_ids")
    candidates: list[ReferenceImageCandidate] = []
    links = _workflow_links(workflow)
    if links is None:
        return ReferenceImageResolution(None, (), "no_reference_image")
    for source_id, source_slot, target_id, target_slot, link_type in links:
        if (
            consumer_node_id is not None
            and _typed_id_key(target_id) != _typed_id_key(consumer_node_id)
        ):
            continue
        target_node = nodes_by_id.get(_typed_id_key(target_id))
        source_node = nodes_by_id.get(_typed_id_key(source_id))
        if target_node is None or source_node is None:
            continue
        target_inputs = target_node.get("inputs", [])
        source_outputs = source_node.get("outputs", [])
        target_input = target_inputs[target_slot] if target_slot < len(target_inputs) else {}
        source_output = source_outputs[source_slot] if source_slot < len(source_outputs) else {}
        input_name = _slot_name(target_input)
        normalized_name = _normalized_role_name(input_name)
        input_type = _slot_type(target_input) or link_type
        output_type = _slot_type(source_output) or link_type
        if output_type != "IMAGE" and link_type != "IMAGE":
            continue
        if input_type != "IMAGE":
            continue
        if normalized_name in {"image2", "reference", "referenceimage", "characterreference"}:
            score = 20
        elif "reference" in normalized_name:
            score = 15
        elif normalized_name.endswith("image2"):
            score = 10
        else:
            continue
        for owner_id, owner_slot, route_node_ids in _unique_upstream_image_owners(
            workflow,
            producer_node_id=source_id,
            producer_output_index=source_slot,
        ):
            owner_node = nodes_by_id.get(_typed_id_key(owner_id))
            if owner_node is None:
                continue
            owner_outputs = owner_node.get("outputs", [])
            owner_output = owner_outputs[owner_slot] if owner_slot < len(owner_outputs) else {}
            candidates.append(
                ReferenceImageCandidate(
                    producer_node_id=owner_id,
                    producer_node_type=_node_type(owner_node),
                    producer_output=_slot_name(owner_output),
                    producer_output_index=owner_slot,
                    consumer_node_id=target_id,
                    consumer_node_type=_node_type(target_node),
                    consumer_input=input_name,
                    consumer_input_index=target_slot,
                    score=score,
                    route_node_ids=(*route_node_ids, target_id),
                    direct_producer_node_id=source_id,
                )
            )
    candidates.sort(
        key=lambda item: (
            -item.score,
            type(item.producer_node_id).__name__,
            str(item.producer_node_id),
            item.producer_output_index,
        )
    )
    if candidates and (
        len(candidates) == 1 or candidates[0].score > candidates[1].score
    ):
        return ReferenceImageResolution(
            target=candidates[0],
            candidates=(candidates[0],),
            reason="unique_reference_image_producer",
        )
    return ReferenceImageResolution(
        target=None,
        candidates=tuple(candidates[:MAX_MASK_TARGET_CANDIDATES]),
        reason="ambiguous_reference_images" if candidates else "no_reference_image",
    )


def resolve_reference_prompt_producer(
    workflow: Mapping[str, Any],
    reference: ReferenceImageCandidate,
) -> PromptProducerResolution | DirectPromptWidgetResolution:
    """Resolve a unique prompt producer on the bounded downstream reference route."""

    reachable: list[int | str] = [reference.consumer_node_id]
    seen = {_typed_id_key(reference.consumer_node_id)}
    frontier = [(reference.consumer_node_id, 0)]
    links = _workflow_links(workflow)
    if links is None:
        return PromptProducerResolution(None, (), "invalid_workflow_links")
    while frontier and len(seen) < MAX_IMAGE_ROUTE_VISITS:
        node_id, depth = frontier.pop(0)
        if depth >= MAX_IMAGE_ROUTE_HOPS:
            continue
        for source_id, _source_slot, target_id, _target_slot, link_type in links:
            if _typed_id_key(source_id) != _typed_id_key(node_id) or link_type != "IMAGE":
                continue
            key = _typed_id_key(target_id)
            if key in seen:
                continue
            seen.add(key)
            reachable.append(target_id)
            frontier.append((target_id, depth + 1))

    candidates: dict[tuple[tuple[str, Any], tuple[str, Any], int], PromptProducerCandidate] = {}
    for consumer_id in reachable:
        resolution = resolve_connected_prompt_producer(
            workflow,
            consumer_node_id=consumer_id,
        )
        for candidate in resolution.candidates:
            key = (
                _typed_id_key(candidate.producer_node_id),
                _typed_id_key(candidate.consumer_node_id),
                candidate.consumer_input_index,
            )
            candidates[key] = candidate
    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            -item.score,
            type(item.producer_node_id).__name__,
            str(item.producer_node_id),
            item.producer_output_index,
        ),
    )
    if ordered and (len(ordered) == 1 or ordered[0].score > ordered[1].score):
        return PromptProducerResolution(
            target=ordered[0],
            candidates=(ordered[0],),
            reason="unique_reference_route_prompt_producer",
        )
    if ordered:
        return PromptProducerResolution(
            target=None,
            candidates=tuple(ordered[:MAX_MASK_TARGET_CANDIDATES]),
            reason="ambiguous_reference_route_prompts",
        )

    direct_candidates: dict[
        tuple[tuple[str, Any], int, str],
        DirectPromptWidgetCandidate,
    ] = {}
    for consumer_id in reachable:
        direct = resolve_direct_prompt_widget(
            workflow,
            consumer_node_id=consumer_id,
        )
        values = (
            direct.candidates
            if direct.target is None
            else (direct.target,)
        )
        for candidate in values:
            key = (
                _typed_id_key(candidate.node_id),
                candidate.consumer_input_index,
                candidate.producer_widget,
            )
            direct_candidates[key] = candidate
    direct_ordered = sorted(
        direct_candidates.values(),
        key=lambda item: (
            -item.score,
            type(item.node_id).__name__,
            str(item.node_id),
            item.consumer_input_index,
        ),
    )
    direct_candidate_nodes = {
        _typed_id_key(item.node_id) for item in direct_ordered
    }
    if direct_ordered and (
        len(direct_candidate_nodes) == 1
        and (
            len(direct_ordered) == 1
            or direct_ordered[0].score > direct_ordered[1].score
        )
    ):
        return DirectPromptWidgetResolution(
            target=direct_ordered[0],
            candidates=(direct_ordered[0],),
            reason="unique_reference_route_direct_prompt_widget",
        )
    return DirectPromptWidgetResolution(
        target=None,
        candidates=tuple(direct_ordered[:MAX_MASK_TARGET_CANDIDATES]),
        reason=(
            "ambiguous_reference_route_direct_prompt_widgets"
            if direct_ordered
            else "no_reference_route_prompt"
        ),
    )


def resolve_mask_target(
    workflow: Mapping[str, Any],
    *,
    requested_node_id: int | str | None = None,
    selected_node_ids: Sequence[int | str] = (),
) -> MaskTargetResolution:
    """Resolve one exact mask image source without title/class first-match behavior."""

    if _workflow_links(workflow) is None:
        return MaskTargetResolution(
            target=None,
            candidates=(),
            reason="invalid_workflow_links",
        )
    candidates, _ = _candidate_facts(workflow)
    by_id = {_typed_id_key(item.node_id): item for item in candidates}

    target: MaskTargetCandidate | None = None
    reason = ""
    if requested_node_id is not None:
        target = by_id.get(_typed_id_key(requested_node_id))
        reason = "explicit_exact_id" if target else "requested_node_not_mask_compatible"
    else:
        selected = []
        seen: set[tuple[str, Any]] = set()
        for node_id in selected_node_ids:
            key = _typed_id_key(node_id)
            candidate = by_id.get(key)
            if candidate is not None and key not in seen:
                selected.append(candidate)
                seen.add(key)
        qualified = [item for item in candidates if item.topology_score >= 15]
        # A selected reference LoadImage often has only an IMAGE edge. A unique
        # dual IMAGE+MASK inpaint source is stronger role evidence and must win.
        if len(selected) == 1:
            selected_target = selected[0]
            if (
                selected_target.topology_score > 0
                and qualified
                and (
                    len(qualified) == 1
                    or qualified[0].topology_score > qualified[1].topology_score
                )
                and _typed_id_key(qualified[0].node_id)
                != _typed_id_key(selected_target.node_id)
            ):
                target = qualified[0]
                reason = "unique_topology_mask_source"
            else:
                target = selected_target
                reason = "single_selected_mask_source"
        elif len(selected) > 1:
            reason = "multiple_selected_mask_sources"
            candidates = selected
        else:
            qualified = [item for item in candidates if item.topology_score > 0]
            if qualified and (
                len(qualified) == 1
                or qualified[0].topology_score > qualified[1].topology_score
            ):
                target = qualified[0]
                reason = "unique_topology_mask_source"
            elif len(candidates) == 1:
                target = candidates[0]
                reason = "single_mask_source"
            else:
                reason = "ambiguous_mask_sources" if candidates else "no_mask_sources"

    if target is not None:
        return MaskTargetResolution(
            target=target,
            candidates=(target,),
            reason=reason,
            prompt_producers=_prompt_producer_evidence(workflow, target),
        )
    return MaskTargetResolution(
        target=None,
        candidates=tuple(candidates[:MAX_MASK_TARGET_CANDIDATES]),
        reason=reason,
    )


def normalize_image_reference(value: Mapping[str, Any]) -> tuple[str, str, str]:
    filename = str(value.get("filename") or "")
    subfolder = str(value.get("subfolder") or "")
    image_type = str(value.get("type") or "input")
    if not filename:
        raise ValueError("mask source image reference has no filename")
    if (
        "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or len(filename.encode("utf-8")) > MAX_MASK_IMAGE_FILENAME_BYTES
    ):
        raise ValueError("mask source image filename is invalid or too long")
    if (
        subfolder.startswith(("/", "\\"))
        or "\\" in subfolder
        or (
            subfolder != ""
            and any(part in {"", ".", ".."} for part in subfolder.split("/"))
        )
        or len(subfolder.encode("utf-8")) > MAX_MASK_IMAGE_SUBFOLDER_BYTES
    ):
        raise ValueError("mask source image subfolder is invalid or too long")
    if image_type not in {"input", "output", "temp"}:
        raise ValueError("mask source image type is invalid")
    return filename, subfolder, image_type


def _bounded_authority_text(value: str, *, label: str, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    encoded = value.encode("utf-8")
    if len(encoded) < minimum or len(encoded) > MAX_MASK_AUTHORITY_TEXT_BYTES:
        raise ValueError(f"{label} length is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _bounded_node_id(value: Any, *, label: str, optional: bool = False) -> int | str | None:
    if value is None and optional:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or (isinstance(value, str) and len(value.encode("utf-8")) > MAX_MASK_AUTHORITY_TEXT_BYTES)
    ):
        raise ValueError(f"{label} is invalid or too long")
    return value


@dataclass(frozen=True, slots=True)
class MaskContextAuthority:
    session_id: str
    workflow_identity: str
    graph_hash: str
    node_id: int | str
    source_image: tuple[str, str, str]
    source_attestation: tuple[str, int, int, int] | None
    expires_at: float
    reference_node_id: int | str | None = None
    reference_consumer_node_id: int | str | None = None
    prompt_consumer_node_id: int | str | None = None


class MaskContextTokenStore:
    """In-memory, bounded, one-use authority for inspected mask sources."""

    def __init__(
        self,
        *,
        ttl_seconds: float = MASK_CONTEXT_TTL_SECONDS,
        max_tokens: int = MAX_MASK_CONTEXT_TOKENS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_tokens <= 0:
            raise ValueError("mask context bounds must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._clock = clock
        self._tokens: dict[str, MaskContextAuthority] = {}
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> None:
        for token, authority in list(self._tokens.items()):
            if authority.expires_at <= now:
                self._tokens.pop(token, None)
        while len(self._tokens) >= self.max_tokens:
            oldest = min(self._tokens, key=lambda key: self._tokens[key].expires_at)
            self._tokens.pop(oldest, None)

    def issue(
        self,
        *,
        session_id: str,
        workflow_identity: str,
        graph_hash: str,
        node_id: int | str,
        source_image: Mapping[str, Any],
        source_attestation: Mapping[str, Any] | None = None,
        reference_node_id: int | str | None = None,
        reference_consumer_node_id: int | str | None = None,
        prompt_consumer_node_id: int | str | None = None,
    ) -> tuple[str, MaskContextAuthority]:
        now = self._clock()
        session_id = _bounded_authority_text(session_id, label="session_id")
        workflow_identity = _bounded_authority_text(
            workflow_identity,
            label="workflow_identity",
            minimum=8,
        )
        if not isinstance(graph_hash, str) or _SHA256_RE.fullmatch(graph_hash) is None:
            raise ValueError("graph_hash must be a lowercase SHA-256 digest")
        node_id = _bounded_node_id(node_id, label="mask target node ID")
        reference_node_id = _bounded_node_id(
            reference_node_id, label="reference node ID", optional=True
        )
        reference_consumer_node_id = _bounded_node_id(
            reference_consumer_node_id,
            label="reference consumer node ID",
            optional=True,
        )
        prompt_consumer_node_id = _bounded_node_id(
            prompt_consumer_node_id,
            label="prompt consumer node ID",
            optional=True,
        )
        normalized_source_attestation = None
        if source_attestation is not None:
            if not isinstance(source_attestation, Mapping):
                raise ValueError("source attestation must be an object")
            sha256 = source_attestation.get("sha256")
            size_bytes = source_attestation.get("size_bytes")
            width = source_attestation.get("width")
            height = source_attestation.get("height")
            if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
                raise ValueError("source attestation sha256 must be a lowercase SHA-256 digest")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in (size_bytes, width, height)
            ):
                raise ValueError(
                    "source attestation size and decoded dimensions must be positive integers"
                )
            normalized_source_attestation = (sha256, size_bytes, width, height)
        authority = MaskContextAuthority(
            session_id=session_id,
            workflow_identity=workflow_identity,
            graph_hash=graph_hash,
            node_id=node_id,
            source_image=normalize_image_reference(source_image),
            source_attestation=normalized_source_attestation,
            expires_at=now + self.ttl_seconds,
            reference_node_id=reference_node_id,
            reference_consumer_node_id=reference_consumer_node_id,
            prompt_consumer_node_id=prompt_consumer_node_id,
        )
        with self._lock:
            self._purge_locked(now)
            token = secrets.token_urlsafe(24)
            while token in self._tokens:
                token = secrets.token_urlsafe(24)
            self._tokens[token] = authority
        return token, authority

    def inspect(self, token: str, *, session_id: str) -> MaskContextAuthority:
        if not isinstance(token, str) or not 16 <= len(token.encode("utf-8")) <= 256:
            raise ValueError("mask_context_token is malformed")
        session_id = _bounded_authority_text(session_id, label="session_id")
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            authority = self._tokens.get(token)
            if authority is None:
                raise ValueError("mask_context_token is missing, expired, or already used")
            if authority.session_id != session_id:
                raise ValueError("mask_context_token belongs to a different Ren session")
            return authority

    def consume(self, token: str, *, expected: MaskContextAuthority) -> None:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            authority = self._tokens.get(token)
            if authority is None:
                raise ValueError("mask_context_token is missing, expired, or already used")
            if authority != expected:
                raise ValueError("mask_context_token authority changed unexpectedly")
            self._tokens.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
