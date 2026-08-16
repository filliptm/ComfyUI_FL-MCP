"""ComfyUI filesystem utilities for FL-MCP.

Provides secure, deterministic access to ComfyUI directory structure
for MCP-based analysis and discovery.
"""

import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx
from comfy_models import ComfyFileInfo, ComfyFolderType, ComfySearchResult
from comfy_runtime_paths import configured_runtime_paths
from config import settings
from extra_model_paths_loader import ExtraModelPathsLoader
from narrow_edit_idempotency import NarrowEditIdempotencyError, _canonical_typed_bytes
from path_resolver import PathResolver

logger = logging.getLogger(__name__)

READ_MAX_CHARS = 24000
READ_MAX_LINES = 800
LONG_LINE_CHARS = 1000
SEARCH_LINE_CHARS = 600
MAX_READ_FILE_BYTES = 5 * 1024 * 1024
EXECUTION_SUBMISSION_ATTESTATION_SCHEMA = "fl-mcp.execution-submission-attestation.v1"
EXECUTION_PROVENANCE_SCHEMA = "fl-mcp.execution-provenance.v1"
EXECUTION_PROVENANCE_SOURCE = "frontend_queue_capture"
EXECUTION_PROVENANCE_EXTRA_KEY = "fl_mcp_execution_provenance"
EXECUTION_GRAPH_HASH_SCHEMA = "fl-mcp.graph-precondition.v1"
SUBMITTED_API_PROMPT_HASH_SCHEMA = "fl-mcp.execution-api-prompt.typed-v1"
SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA = "fl-mcp.execution-workflow.typed-v1"
SUBMITTED_NODE_INPUTS_HASH_SCHEMA = "fl-mcp.execution-node-inputs.typed-v1"
SUBMITTED_STRING_INPUT_HASH_SCHEMA = "fl-mcp.execution-string-input.typed-v1"
MAX_EXECUTION_SUBMISSION_ATTESTATION_BYTES = 8 * 1024 * 1024
MAX_EXECUTION_HISTORY_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EXECUTION_SUBMISSION_NODES = 10_000
MAX_EXECUTION_ATTESTED_NODE_IDS = 20
MAX_EXECUTION_NODE_INPUTS = 1_000
MAX_EXECUTION_NODE_STRING_FACTS = 32
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_FIELDS = {
    "schema",
    "source",
    "api_prompt",
    "editable_workflow",
    "graph_hash",
    "graph_hash_schema",
    "raw_prompt_returned",
    "captured_at_ms",
    "operation_id",
    "operation_request_hash",
}
_PROVENANCE_API_PROMPT_FIELDS = {"schema", "sha256", "canonical_bytes", "node_count"}
_PROVENANCE_WORKFLOW_FIELDS = {
    "schema",
    "sha256",
    "canonical_bytes",
    "node_count",
    "workflow_id",
    "revision",
}


class _SubmissionAttestationUnavailable(ValueError):
    """Internal bounded failure that is safe to expose as a fixed reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _bounded_typed_sha256(value: Any, *, schema: str) -> tuple[str, int]:
    """Hash one value with the browser-parity typed canonical encoding."""

    try:
        canonical = _canonical_typed_bytes({"schema": schema, "value": value})
    except (NarrowEditIdempotencyError, OverflowError, RecursionError) as exc:
        raise _SubmissionAttestationUnavailable("submission_malformed") from exc
    if len(canonical) > MAX_EXECUTION_SUBMISSION_ATTESTATION_BYTES:
        raise _SubmissionAttestationUnavailable("submission_too_large")
    return hashlib.sha256(canonical).hexdigest(), len(canonical)


def _bounded_node_count(nodes: Any) -> int:
    if not isinstance(nodes, (list, Mapping)):
        raise _SubmissionAttestationUnavailable("submission_malformed")
    count = len(nodes)
    if count > MAX_EXECUTION_SUBMISSION_NODES:
        raise _SubmissionAttestationUnavailable("submission_too_large")
    return count


def _safe_integer(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) <= 9_007_199_254_740_991
    )


def _safe_nonnegative_integer(value: Any) -> bool:
    return _safe_integer(value) and value >= 0


def _workflow_without_execution_provenance(
    workflow: Mapping[str, Any],
) -> tuple[dict[str, Any], Any]:
    detached = dict(workflow)
    extra = workflow.get("extra")
    provenance = None
    if isinstance(extra, Mapping):
        detached_extra = dict(extra)
        provenance = detached_extra.pop(EXECUTION_PROVENANCE_EXTRA_KEY, None)
        detached["extra"] = detached_extra
    return detached, provenance


def _capture_record_verification(
    record: Any,
    *,
    api_prompt: Mapping[str, Any],
    api_prompt_sha256: str,
    api_prompt_bytes: int,
    api_prompt_node_count: int,
    editable_workflow_sha256: str,
    editable_workflow_bytes: int,
    editable_workflow_node_count: int,
    editable_workflow_id: str | None,
    editable_workflow_revision: int | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    safe_graph_facts = {"graph_hash": None, "graph_hash_schema": None}
    if not isinstance(record, Mapping):
        return False, "frontend_queue_capture_missing", safe_graph_facts
    if set(record) != _PROVENANCE_FIELDS:
        return False, "frontend_queue_capture_malformed", safe_graph_facts
    api_record = record.get("api_prompt")
    workflow_record = record.get("editable_workflow")
    if (
        not isinstance(api_record, Mapping)
        or set(api_record) != _PROVENANCE_API_PROMPT_FIELDS
        or not isinstance(workflow_record, Mapping)
        or set(workflow_record) != _PROVENANCE_WORKFLOW_FIELDS
    ):
        return False, "frontend_queue_capture_malformed", safe_graph_facts
    workflow_id = workflow_record.get("workflow_id")
    revision = workflow_record.get("revision")
    if (
        record.get("schema") != EXECUTION_PROVENANCE_SCHEMA
        or record.get("source") != EXECUTION_PROVENANCE_SOURCE
        or record.get("raw_prompt_returned") is not False
        or not isinstance(record.get("operation_id"), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", record["operation_id"]) is None
        or not isinstance(record.get("operation_request_hash"), str)
        or _SHA256_PATTERN.fullmatch(record["operation_request_hash"]) is None
        or not _safe_nonnegative_integer(record.get("captured_at_ms"))
        or api_record.get("schema") != SUBMITTED_API_PROMPT_HASH_SCHEMA
        or workflow_record.get("schema") != SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA
        or (workflow_id is not None and not isinstance(workflow_id, str))
        or (revision is not None and not _safe_integer(revision))
        or record.get("graph_hash_schema") != EXECUTION_GRAPH_HASH_SCHEMA
        or not isinstance(record.get("graph_hash"), str)
        or _SHA256_PATTERN.fullmatch(record["graph_hash"]) is None
        or not isinstance(api_record.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(api_record["sha256"]) is None
        or not _safe_nonnegative_integer(api_record.get("canonical_bytes"))
        or not _safe_nonnegative_integer(api_record.get("node_count"))
        or not isinstance(workflow_record.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(workflow_record["sha256"]) is None
        or not _safe_nonnegative_integer(workflow_record.get("canonical_bytes"))
        or not _safe_nonnegative_integer(workflow_record.get("node_count"))
    ):
        return False, "frontend_queue_capture_malformed", safe_graph_facts
    try:
        workflow_id_bytes = str(workflow_id or "").encode("utf-8")
    except UnicodeEncodeError:
        return False, "frontend_queue_capture_malformed", safe_graph_facts
    if len(workflow_id_bytes) > 256:
        return False, "frontend_queue_capture_malformed", safe_graph_facts
    expected_api = {
        "schema": SUBMITTED_API_PROMPT_HASH_SCHEMA,
        "sha256": api_prompt_sha256,
        "canonical_bytes": api_prompt_bytes,
        "node_count": api_prompt_node_count,
    }
    expected_workflow = {
        "schema": SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA,
        "sha256": editable_workflow_sha256,
        "canonical_bytes": editable_workflow_bytes,
        "node_count": editable_workflow_node_count,
        "workflow_id": editable_workflow_id,
        "revision": editable_workflow_revision,
    }
    if dict(api_record) != expected_api or dict(workflow_record) != expected_workflow:
        return False, "frontend_queue_capture_hash_mismatch", safe_graph_facts
    if len(api_prompt) != api_prompt_node_count:
        return False, "frontend_queue_capture_hash_mismatch", safe_graph_facts
    return True, None, {
        "graph_hash": record["graph_hash"],
        "graph_hash_schema": EXECUTION_GRAPH_HASH_SCHEMA,
        "captured_at_ms": record["captured_at_ms"],
        "workflow_id": editable_workflow_id,
        "revision": editable_workflow_revision,
        "operation_id": record["operation_id"],
        "operation_request_hash": record["operation_request_hash"],
    }


def _typed_node_id(value: Any) -> int | str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return value


def _editable_workflow_nodes(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nodes = workflow.get("nodes")
    if isinstance(nodes, list):
        values = nodes
    elif isinstance(nodes, Mapping):
        values = [
            ({**node, "id": key} if isinstance(node, Mapping) and "id" not in node else node)
            for key, node in nodes.items()
        ]
    else:
        return []
    return [node for node in values if isinstance(node, Mapping)]


def _input_name_fact(name: str) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
        return {"input": name}
    encoded = name.encode("utf-8")
    return {
        "input_name_sha256": hashlib.sha256(encoded).hexdigest(),
        "input_name_utf8_bytes": len(encoded),
    }


def _string_input_kind(name: str, value: str) -> str:
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    if (
        any(role in normalized for role in ("image", "mask", "reference", "filename"))
        or re.search(r"\.(?:png|jpe?g|webp|gif|bmp|tiff?)(?:\s*\[[^]]+\])?$", value, re.I)
    ):
        return "image_reference"
    return "text"


def _requested_node_attestation(
    *,
    requested_node_id: int | str,
    api_prompt: Mapping[str, Any],
    editable_workflow: Mapping[str, Any],
) -> dict[str, Any]:
    unavailable = {"node_id": requested_node_id, "available": False}
    nodes = _editable_workflow_nodes(editable_workflow)
    exact_matches = [
        node
        for node in nodes
        if type(_typed_node_id(node.get("id"))) is type(requested_node_id)
        and _typed_node_id(node.get("id")) == requested_node_id
    ]
    string_id_matches = [
        node
        for node in nodes
        if (node_id := _typed_node_id(node.get("id"))) is not None
        and str(node_id) == str(requested_node_id)
    ]
    if len(exact_matches) != 1:
        return {
            **unavailable,
            "reason": "editable_node_ambiguous" if exact_matches else "editable_node_missing",
        }
    if len(string_id_matches) != 1:
        return {**unavailable, "reason": "api_prompt_node_id_collision"}
    api_node = api_prompt.get(str(requested_node_id))
    if not isinstance(api_node, Mapping):
        return {**unavailable, "reason": "api_prompt_node_missing"}
    class_type = api_node.get("class_type")
    inputs = api_node.get("inputs")
    try:
        class_type_bytes = class_type.encode("utf-8") if isinstance(class_type, str) else b""
    except UnicodeEncodeError:
        class_type_bytes = b""
    if not 1 <= len(class_type_bytes) <= 256 or not isinstance(inputs, Mapping):
        return {**unavailable, "reason": "api_prompt_node_malformed"}
    if len(inputs) > MAX_EXECUTION_NODE_INPUTS:
        return {**unavailable, "reason": "node_inputs_too_large"}
    try:
        inputs_sha256, inputs_canonical_bytes = _bounded_typed_sha256(
            inputs,
            schema=SUBMITTED_NODE_INPUTS_HASH_SCHEMA,
        )
        string_facts = []
        input_names = list(inputs)
        if any(not isinstance(name, str) for name in input_names):
            raise _SubmissionAttestationUnavailable("submission_malformed")
        input_names.sort()
        for name in input_names:
            value = inputs[name]
            if not isinstance(value, str):
                continue
            if len(string_facts) >= MAX_EXECUTION_NODE_STRING_FACTS:
                break
            value_sha256, _ = _bounded_typed_sha256(
                value,
                schema=SUBMITTED_STRING_INPUT_HASH_SCHEMA,
            )
            string_facts.append(
                {
                    **_input_name_fact(name),
                    "kind": _string_input_kind(name, value),
                    "value_sha256": value_sha256,
                    "utf8_bytes": len(value.encode("utf-8")),
                }
            )
    except (UnicodeEncodeError, _SubmissionAttestationUnavailable) as exc:
        reason = exc.reason if isinstance(exc, _SubmissionAttestationUnavailable) else "submission_malformed"
        return {**unavailable, "reason": reason}
    return {
        "node_id": requested_node_id,
        "available": True,
        "class_type": class_type,
        "inputs_hash_schema": SUBMITTED_NODE_INPUTS_HASH_SCHEMA,
        "inputs_sha256": inputs_sha256,
        "inputs_canonical_bytes": inputs_canonical_bytes,
        "input_count": len(inputs),
        "string_inputs": string_facts,
        "string_inputs_truncated": sum(
            1 for value in inputs.values() if isinstance(value, str)
        ) > len(string_facts),
    }


def _submission_attestation(
    raw_prompt: Any,
    *,
    attest_node_ids: tuple[int | str, ...] = (),
) -> dict[str, Any]:
    """Derive non-plaintext identity facts from one Comfy history prompt tuple."""

    unavailable = {
        "schema": EXECUTION_SUBMISSION_ATTESTATION_SCHEMA,
        "available": False,
    }
    try:
        if not isinstance(raw_prompt, (list, tuple)) or len(raw_prompt) < 4:
            raise _SubmissionAttestationUnavailable("submission_malformed")
        api_prompt = raw_prompt[2]
        extra_data = raw_prompt[3]
        if not isinstance(api_prompt, Mapping) or not isinstance(extra_data, Mapping):
            raise _SubmissionAttestationUnavailable("submission_malformed")
        extra_pnginfo = extra_data.get("extra_pnginfo")
        if not isinstance(extra_pnginfo, Mapping):
            raise _SubmissionAttestationUnavailable("submission_malformed")
        raw_editable_workflow = extra_pnginfo.get("workflow")
        if not isinstance(raw_editable_workflow, Mapping):
            raise _SubmissionAttestationUnavailable("submission_malformed")
        editable_workflow, provenance_record = _workflow_without_execution_provenance(
            raw_editable_workflow
        )

        api_prompt_node_count = _bounded_node_count(api_prompt)
        editable_workflow_node_count = _bounded_node_count(editable_workflow.get("nodes"))
        api_prompt_sha256, api_prompt_bytes = _bounded_typed_sha256(
            api_prompt,
            schema=SUBMITTED_API_PROMPT_HASH_SCHEMA,
        )
        editable_workflow_sha256, editable_workflow_bytes = (
            _bounded_typed_sha256(
                editable_workflow,
                schema=SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA,
            )
        )
        workflow_id_value = editable_workflow.get("id")
        editable_workflow_id = (
            workflow_id_value if isinstance(workflow_id_value, str) else None
        )
        workflow_revision_value = editable_workflow.get("revision")
        editable_workflow_revision = (
            workflow_revision_value if _safe_integer(workflow_revision_value) else None
        )
        capture_verified, capture_reason, capture_facts = _capture_record_verification(
            provenance_record,
            api_prompt=api_prompt,
            api_prompt_sha256=api_prompt_sha256,
            api_prompt_bytes=api_prompt_bytes,
            api_prompt_node_count=api_prompt_node_count,
            editable_workflow_sha256=editable_workflow_sha256,
            editable_workflow_bytes=editable_workflow_bytes,
            editable_workflow_node_count=editable_workflow_node_count,
            editable_workflow_id=editable_workflow_id,
            editable_workflow_revision=editable_workflow_revision,
        )
        if len(attest_node_ids) > MAX_EXECUTION_ATTESTED_NODE_IDS:
            raise _SubmissionAttestationUnavailable("too_many_attested_node_ids")
        typed_requested = [(type(node_id).__name__, node_id) for node_id in attest_node_ids]
        if (
            any(_typed_node_id(node_id) is None for node_id in attest_node_ids)
            or len(set(typed_requested)) != len(typed_requested)
        ):
            raise _SubmissionAttestationUnavailable("attested_node_ids_malformed")
        node_attestations = [
            _requested_node_attestation(
                requested_node_id=node_id,
                api_prompt=api_prompt,
                editable_workflow=editable_workflow,
            )
            for node_id in attest_node_ids
        ]
    except _SubmissionAttestationUnavailable as exc:
        return {**unavailable, "reason": exc.reason}

    return {
        "schema": EXECUTION_SUBMISSION_ATTESTATION_SCHEMA,
        "available": True,
        "raw_prompt_returned": False,
        "hash_algorithm": "sha256",
        "source": (
            EXECUTION_PROVENANCE_SOURCE if provenance_record is not None else "history_derived"
        ),
        "verified": capture_verified,
        **({"verification_reason": capture_reason} if capture_reason else {}),
        "api_prompt": {
            "schema": SUBMITTED_API_PROMPT_HASH_SCHEMA,
            "sha256": api_prompt_sha256,
            "canonical_bytes": api_prompt_bytes,
            "node_count": api_prompt_node_count,
        },
        "editable_workflow": {
            "schema": SUBMITTED_EDITABLE_WORKFLOW_HASH_SCHEMA,
            "sha256": editable_workflow_sha256,
            "canonical_bytes": editable_workflow_bytes,
            "node_count": editable_workflow_node_count,
        },
        "node_attestations": node_attestations,
        **capture_facts,
    }


async def _bounded_history_json_response(
    client: httpx.AsyncClient,
    url: str,
) -> Any:
    """Stream one opt-in history response under a pre-decode byte ceiling."""

    async with client.stream("GET", url, timeout=10.0) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdecimal():
            if int(content_length) > MAX_EXECUTION_HISTORY_RESPONSE_BYTES:
                raise ComfyUIError("ComfyUI history response exceeds the safe byte limit.")
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > MAX_EXECUTION_HISTORY_RESPONSE_BYTES:
                raise ComfyUIError("ComfyUI history response exceeds the safe byte limit.")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComfyUIError("ComfyUI returned malformed execution history JSON.") from exc


def _chunk_long_lines(text: str, limit: int = LONG_LINE_CHARS) -> str:
    chunks: List[str] = []
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        if len(body) <= limit:
            chunks.append(line)
            continue
        for index in range(0, len(body), limit):
            suffix = "\n" if index + limit < len(body) else newline
            chunks.append(body[index:index + limit] + suffix)
    return "".join(chunks)


def _bounded_text(text: str, max_chars: int = READ_MAX_CHARS) -> str:
    max_chars = max(1, min(int(max_chars), READ_MAX_CHARS))
    separated = _chunk_long_lines(text)
    if len(separated) <= max_chars:
        return separated
    return separated[:max_chars].rstrip() + (
            f"\n\n[FL-MCP truncated output at {max_chars} characters; "
        "request a narrower line range or search if more context is needed.]"
    )


def _short_line(text: str, max_chars: int = SEARCH_LINE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " [line truncated]"


class ComfyUIError(Exception):
    """Base exception for ComfyUI tool errors."""
    pass


class ComfyUINotFoundError(ComfyUIError):
    """Raised when ComfyUI installation cannot be located."""
    pass


class ComfyUISecurityError(ComfyUIError):
    """Raised when attempting to access files outside ComfyUI directory."""
    pass


class ComfyUITools:
    """Core ComfyUI filesystem utilities."""
    
    def __init__(self, comfyui_root: Optional[str] = None, comfy_url: str = "http://127.0.0.1:8188"):
        """Initialize ComfyUI tools with auto-detection.
        
        Args:
            comfyui_root: Path to ComfyUI installation (auto-detected if None)
            comfy_url: URL of ComfyUI server (default: http://127.0.0.1:8188)
        """
        self.comfyui_root = Path(comfyui_root) if comfyui_root else self._find_comfyui_root()
        self.comfy_url = comfy_url
        self._validate_comfyui_installation()
        
        # Load extra model paths and merge with defaults
        loader = ExtraModelPathsLoader(
            self.comfyui_root,
            config_path=settings.extra_model_paths_path,
        )
        extra_configs = loader.load()
        
        resolver = PathResolver(self.comfyui_root)
        default_mappings = resolver.get_default_mappings()
        runtime_paths = configured_runtime_paths()
        runtime_folder_types = {
            "input": ComfyFolderType.INPUT,
            "output": ComfyFolderType.OUTPUT,
            "temp": ComfyFolderType.TEMP,
        }
        for folder_name, runtime_path in runtime_paths.items():
            default_mappings[runtime_folder_types[folder_name]] = [runtime_path]
        self.folder_mappings = resolver.merge_with_extra_paths(default_mappings, extra_configs)
        
        # Log summary
        logger.info(f"ComfyUI tools initialized for: {self.comfyui_root}")
        logger.info(f"Loaded folder mappings for {len(self.folder_mappings)} folder types")
        for folder_type, paths in self.folder_mappings.items():
            logger.debug(f"  {folder_type.value}: {len(paths)} path(s)")
        
        # Safe file extensions for reading
        self.safe_read_extensions = {
            '.py', '.json', '.yaml', '.yml', '.toml', '.txt', '.md', '.rst',
            '.cfg', '.ini', '.conf', '.log', '.csv', '.xml', '.html', '.js', 
            '.css', '.sh', '.bat'
        }
    
    def _find_comfyui_root(self) -> Path:
        """Auto-detect ComfyUI installation directory."""
        # Get current FL-MCP project root
        current_dir = Path(__file__).parent.parent  # backend -> fl_js root
        
        # Check if we have a symlinked ComfyUI in the project
        project_comfyui = current_dir / "ComfyUI"
        if project_comfyui.exists() and (project_comfyui / "nodes.py").exists():
            logger.info(f"Found ComfyUI via project symlink: {project_comfyui}")
            return project_comfyui.resolve()
        
        # Check if we're running as a ComfyUI custom node
        custom_node_root = current_dir.parent  # fl_js -> custom_nodes
        if (custom_node_root.name == "custom_nodes" and 
            (custom_node_root.parent / "nodes.py").exists()):
            comfyui_root = custom_node_root.parent
            logger.info(f"Found ComfyUI via custom node installation: {comfyui_root}")
            return comfyui_root
        
        # Check common locations
        common_paths = [
            Path("/ComfyUI"),
            Path("~/ComfyUI").expanduser(),
            Path("../ComfyUI"),
            Path("../../ComfyUI"),
        ]
        
        if settings.comfyui_path:
            common_paths.insert(0, Path(settings.comfyui_path))
        
        for path in common_paths:
            if path.exists() and (path / "nodes.py").exists():
                logger.info(f"Found ComfyUI at: {path}")
                return path.resolve()
        
        raise ComfyUINotFoundError(
            "ComfyUI installation not found. Set the ComfyUI path in Ren settings."
        )
    
    def _validate_comfyui_installation(self) -> None:
        """Validate that directory is a valid ComfyUI installation."""
        required_files = ["nodes.py", "folder_paths.py"]
        required_dirs = ["custom_nodes", "models", "output"]
        
        for file in required_files:
            if not (self.comfyui_root / file).exists():
                raise ComfyUINotFoundError(
                    f"Invalid ComfyUI installation: missing {file}"
                )
        
        for dir in required_dirs:
            if not (self.comfyui_root / dir).exists():
                raise ComfyUINotFoundError(
                    f"Invalid ComfyUI installation: missing {dir}/ directory"
                )
    
    def _validate_path(self, path: str) -> Path:
        """Validate path is within ComfyUI directory."""
        try:
            full_path = (self.comfyui_root / path).resolve()
            
            # Ensure path is within ComfyUI directory
            if not str(full_path).startswith(str(self.comfyui_root)):
                raise ComfyUISecurityError(
                    f"Path outside ComfyUI directory: {path}"
                )
            
            return full_path
            
        except Exception as e:
            raise ComfyUISecurityError(f"Invalid path: {path} - {e}")
    
    def _iter_all_paths(self, folder_type: ComfyFolderType) -> Iterator[Path]:
        """Iterate all configured paths for a folder type.
        
        This is an internal helper to avoid code duplication between
        list_folders and search_files.
        
        Args:
            folder_type: Type of folder to iterate
            
        Yields:
            Path objects for each configured path that exists
        """
        folder_paths = self.folder_mappings.get(folder_type, [])
        for folder_path in folder_paths:
            if folder_path.exists():
                yield folder_path
            else:
                logger.debug(f"Skipping non-existent path: {folder_path}")
    
    async def fetch_history(
        self,
        prompt_id: Optional[str] = None,
        max_items: int = 10,
        *,
        include_submission_attestation: bool = False,
        attest_node_ids: tuple[int | str, ...] = (),
    ) -> Dict[str, Any]:
        """Fetch execution history from ComfyUI.
        
        Args:
            prompt_id: Optional specific prompt ID to fetch. If None, fetches recent history.
            max_items: Maximum number of history items to fetch (default: 10)
            include_submission_attestation: Derive bounded hashes and node counts from
                the submitted prompt tuple before redacting it. Raw prompt and widget
                values are never returned.
            attest_node_ids: Up to 20 exact typed node IDs for compact, non-plaintext
                per-node input attestations. Used only with submission attestation.
            
        Returns:
            If prompt_id is provided: Single history entry dict or None if not found
            If prompt_id is None: Dict mapping prompt_id -> history entry
            
        Raises:
            ComfyUIError: If history fetch fails
        """
        try:
            async with httpx.AsyncClient() as client:
                history_path = (
                    f"/history/{quote(prompt_id, safe='')}"
                    if prompt_id
                    else "/history"
                )
                history_url = f"{self.comfy_url}{history_path}"
                if include_submission_attestation:
                    if not prompt_id:
                        raise ComfyUIError(
                            "Submission attestation requires one exact prompt ID."
                        )
                    history = await _bounded_history_json_response(client, history_url)
                else:
                    response = await client.get(
                        history_url,
                        params=None if prompt_id else {"max_items": max_items},
                        timeout=10.0,
                    )
                    response.raise_for_status()
                    history = response.json()
                if not isinstance(history, dict):
                    raise ComfyUIError("ComfyUI returned malformed execution history JSON.")
                
                # The raw submitted API prompt and editable workflow can contain
                # arbitrary widget plaintext. Keep the historical default redaction;
                # the opt-in path returns only bounded identity metadata.
                for entry in history.values():
                    if not isinstance(entry, dict):
                        continue
                    if include_submission_attestation:
                        entry["submission_attestation"] = _submission_attestation(
                            entry.get("prompt"),
                            attest_node_ids=attest_node_ids,
                        )
                    entry.pop("prompt", None)
                
                if prompt_id:
                    return history.get(prompt_id)
                else:
                    return history
                    
        except ComfyUIError:
            raise
        except httpx.TimeoutException:
            raise ComfyUIError(
                f"ComfyUI server timeout. Is ComfyUI running at {self.comfy_url}?"
            )
        except httpx.RequestError as e:
            raise ComfyUIError(
                f"Failed to connect to ComfyUI at {self.comfy_url}: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to fetch history: {e}")
            raise ComfyUIError(f"Failed to fetch history: {e}")    

    async def delete_queue_items(
        self,
        clear_all: bool = False,
        prompt_ids: Optional[List[str]] = None,
        interrupt_running: bool = False
    ) -> Dict[str, Any]:
        """Delete items from the ComfyUI queue.
        
        Args:
            clear_all: If True, clear all pending items from queue
            prompt_ids: List of specific prompt IDs to delete
            interrupt_running: If True, also interrupt currently running workflow
            
        Returns:
            Dict with operation results:
            {
                "success": bool,
                "cleared_all": bool,
                "deleted_ids": List[str],
                "interrupted": bool,
                "message": str
            }
            
        Raises:
            ComfyUIError: If operation fails or parameters are invalid
        """
        # Validation: must provide at least one operation
        if not clear_all and not prompt_ids and not interrupt_running:
            raise ComfyUIError(
                "Must specify at least one operation: clear_all, prompt_ids, or interrupt_running"
            )
        
        # Validation: cannot specify both clear_all and prompt_ids
        if clear_all and prompt_ids:
            raise ComfyUIError(
                "Cannot specify both clear_all=True and prompt_ids. Choose one."
            )
        
        results = {
            "success": True,
            "cleared_all": False,
            "deleted_ids": [],
            "interrupted": False,
            "message": ""
        }
        
        messages = []
        
        try:
            async with httpx.AsyncClient() as client:
                # Operation 1: Clear all pending items
                if clear_all:
                    try:
                        response = await client.post(
                            f"{self.comfy_url}/queue",
                            json={"clear": True},
                            timeout=10.0
                        )
                        response.raise_for_status()
                        results["cleared_all"] = True
                        messages.append("Cleared all pending queue items")
                        logger.info("Queue cleared successfully")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to clear queue: {e}")
                        results["success"] = False
                        messages.append(f"Failed to clear queue: {e.response.status_code}")
                
                # Operation 2: Delete specific prompt IDs
                if prompt_ids:
                    try:
                        response = await client.post(
                            f"{self.comfy_url}/queue",
                            json={"delete": prompt_ids},
                            timeout=10.0
                        )
                        response.raise_for_status()
                        results["deleted_ids"] = prompt_ids
                        messages.append(f"Deleted {len(prompt_ids)} queue item(s): {', '.join(prompt_ids)}")
                        logger.info(f"Deleted queue items: {prompt_ids}")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to delete queue items: {e}")
                        results["success"] = False
                        messages.append(f"Failed to delete items: {e.response.status_code}")
                
                # Operation 3: Interrupt running workflow
                if interrupt_running:
                    try:
                        response = await client.post(
                            f"{self.comfy_url}/interrupt",
                            json={},
                            timeout=10.0
                        )
                        response.raise_for_status()
                        results["interrupted"] = True
                        messages.append("Interrupted currently running workflow")
                        logger.info("Workflow interrupted successfully")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to interrupt workflow: {e}")
                        # Don't fail the entire operation if interrupt fails
                        # (might not have anything running)
                        messages.append(f"Interrupt failed (nothing running?): {e.response.status_code}")
                
                results["message"] = "; ".join(messages)
                return results
                
        except httpx.TimeoutException:
            raise ComfyUIError(
                f"ComfyUI server timeout. Is ComfyUI running at {self.comfy_url}?"
            )
        except httpx.RequestError as e:
            raise ComfyUIError(
                f"Failed to connect to ComfyUI at {self.comfy_url}: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to delete queue items: {e}")
            raise ComfyUIError(f"Failed to delete queue items: {e}")


    def list_folders(
        self,
        folder_type: Union[str, ComfyFolderType],
        pattern: Optional[str] = None,
        sort_by: Optional[str] = None,
        order: str = "asc",
        limit: int = 50
    ) -> List[ComfyFileInfo]:
        """List contents of a ComfyUI directory by type with filtering and sorting.
        
        Searches all configured paths for the folder type, including paths from
        extra_model_paths.yaml if present. Deduplicates files with the same name
        found in multiple paths.
        
        Args:
            folder_type: Type of folder to list (e.g., 'checkpoints', 'loras')
            pattern: Optional regex pattern to filter paths (case-insensitive)
            sort_by: Optional sort field ('name', 'size', 'modified_time', 'type')
            order: Sort order ('asc' or 'desc')
            limit: Maximum number of items to return
        
        Returns:
            List of ComfyFileInfo objects, filtered, sorted, and limited
        
        Raises:
            ComfyUINotFoundError: If ComfyUI installation not found
            ComfyUISecurityError: If path traversal detected
        """
        # Convert string to enum if needed
        if isinstance(folder_type, str):
            try:
                folder_type = ComfyFolderType(folder_type)
            except ValueError:
                raise ComfyUIError(f"Invalid folder type: {folder_type}")
        
        # Get all paths for this folder type
        folder_paths = self.folder_mappings.get(folder_type, [])
        
        if not folder_paths:
            raise ComfyUIError(f"Unknown folder type: {folder_type}")
        
        # Collect items from all paths with deduplication
        items = []
        seen_names = set()  # Deduplicate by name (first occurrence wins)
        
        for folder_path in self._iter_all_paths(folder_type):
            # Security check
            try:
                folder_path.resolve()
                # Note: folder_path might be outside comfyui_root if from extra_model_paths.yaml
                # This is intentional - we trust the YAML config
            except (OSError, RuntimeError) as e:
                logger.warning(f"Cannot resolve path {folder_path}: {e}")
                continue
            
            for entry in folder_path.iterdir():
                # Skip duplicates (same filename in multiple paths)
                if entry.name in seen_names:
                    logger.debug(f"Skipping duplicate: {entry.name}")
                    continue
                
                seen_names.add(entry.name)
                
                try:
                    stat = entry.stat()
                    
                    # Calculate relative path
                    # Try relative to comfyui_root first, fallback to absolute
                    try:
                        relative_path = str(entry.relative_to(self.comfyui_root))
                    except ValueError:
                        # Path is outside comfyui_root (from extra_model_paths.yaml)
                        relative_path = str(entry)
                    
                    items.append(ComfyFileInfo(
                        name=entry.name,
                        path=relative_path,
                        is_directory=entry.is_dir(),
                        size=stat.st_size if entry.is_file() else None,
                        modified_time=stat.st_mtime,
                        extension=entry.suffix[1:] if entry.suffix else None
                    ))
                except (OSError, PermissionError) as e:
                    logger.warning(f"Cannot access {entry}: {e}")
                    continue
        
        # Apply regex filter if pattern provided
        if pattern:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
                original_count = len(items)
                items = [item for item in items if regex.search(item.path)]
                logger.debug(
                    f"Filtered from {original_count} to {len(items)} items "
                    f"matching pattern: {pattern}"
                )
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern}': {e}")
                # Continue without filtering on invalid pattern
        
        # Apply sorting
        if sort_by is None:
            # Default: directories first, then alphabetical by name
            items.sort(key=lambda x: (not x.is_directory, x.name.lower()))
            logger.debug("Applied default sort: directories first, then by name")
        else:
            # Sort by specified field
            reverse = (order == "desc")
            
            if sort_by == "name":
                items.sort(key=lambda x: x.name.lower(), reverse=reverse)
            elif sort_by == "size":
                items.sort(key=lambda x: x.size or 0, reverse=reverse)
            elif sort_by == "modified_time":
                items.sort(key=lambda x: x.modified_time or 0, reverse=reverse)
            elif sort_by == "type":
                # Sort by: directories vs files, then by extension
                if order == "asc":
                    items.sort(key=lambda x: (not x.is_directory, x.extension or ""))
                else:
                    items.sort(key=lambda x: (x.is_directory, x.extension or ""), reverse=True)
            
            logger.debug(f"Sorted by {sort_by} ({order})")
        
        # Apply limit
        original_count = len(items)
        if limit and len(items) > limit:
            items = items[:limit]
            logger.debug(f"Limited results from {original_count} to {limit} items")
        
        return items
    
    def read_file(
        self,
        path: str,
        max_size: int = READ_MAX_CHARS,
        start_line: int = 1,
        line_count: int = 240,
    ) -> str:
        """Read a text file within the ComfyUI directory."""
        try:
            # Validate path
            full_path = self._validate_path(path)
            
            if not full_path.exists():
                raise ComfyUIError(f"File does not exist: {path}")
            
            if not full_path.is_file():
                raise ComfyUIError(f"Path is not a file: {path}")
            
            # Check file size
            file_size = full_path.stat().st_size
            if file_size > MAX_READ_FILE_BYTES:
                raise ComfyUIError(
                    f"File too large for text inspection: {file_size} bytes (max: {MAX_READ_FILE_BYTES})"
                )
            
            # Check file extension for safety
            if full_path.suffix.lower() not in self.safe_read_extensions:
                logger.warning(
                    f"Reading potentially unsafe file type: {full_path.suffix}"
                )
            
            # Read file
            try:
                content = full_path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                # Try with fallback encoding
                content = full_path.read_text(encoding='latin-1')
                logger.warning(f"File read with fallback encoding: {path}")

            start_line = max(1, int(start_line))
            line_count = max(1, min(int(line_count), READ_MAX_LINES))
            lines = content.splitlines(keepends=True)
            start_index = min(start_line - 1, len(lines))
            end_index = min(start_index + line_count, len(lines))
            selected = "".join(lines[start_index:end_index])
            bounded = _bounded_text(selected, max_size)
            logger.info(
                f"Read file excerpt: {path} "
                f"(lines {start_index + 1}-{end_index} of {len(lines)}, {len(bounded)} chars)"
            )
            return bounded
                
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"Error reading file {path}: {e}")
    
    def search_files(
        self,
        pattern: str,
        folder_type: Union[str, ComfyFolderType] = ComfyFolderType.CUSTOM_NODES,
        file_pattern: Optional[str] = None,
        max_results: int = 20,
        context_lines: int = 2
    ) -> List[ComfySearchResult]:
        """Search for pattern in files within a ComfyUI directory.
        
        Searches all configured paths for the folder type, including paths from
        extra_model_paths.yaml if present.
        """
        try:
            # Convert string to enum if needed
            if isinstance(folder_type, str):
                folder_type = ComfyFolderType(folder_type)
            
            # Get all paths for this folder type
            folder_paths = self.folder_mappings.get(folder_type, [])
            
            if not folder_paths:
                raise ComfyUIError(f"Unknown folder type: {folder_type}")
            
            # Compile regex pattern
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            
            results = []
            files_searched = 0
            
            # Search in all configured paths
            for folder_path in self._iter_all_paths(folder_type):
                # Search files recursively
                for file_path in folder_path.rglob(file_pattern or "*"):
                    if not file_path.is_file():
                        continue
                    
                    # Skip binary files and very large files
                    if file_path.suffix.lower() not in self.safe_read_extensions:
                        continue
                    
                    try:
                        file_size = file_path.stat().st_size
                        if file_size > 1024 * 1024:  # Skip files > 1MB
                            continue
                        
                        # Read and search file
                        content = file_path.read_text(encoding='utf-8', errors='ignore')
                        lines = content.split('\n')
                        
                        for line_num, line in enumerate(lines, 1):
                            if regex.search(line):
                                # Extract context
                                start_line = max(0, line_num - context_lines - 1)
                                end_line = min(len(lines), line_num + context_lines)
                                
                                context_before = lines[start_line:line_num-1]
                                context_after = lines[line_num:end_line]
                                
                                # Calculate relative path
                                try:
                                    relative_path = str(file_path.relative_to(self.comfyui_root))
                                except ValueError:
                                    # Path is outside comfyui_root
                                    relative_path = str(file_path)
                                
                                results.append(ComfySearchResult(
                                    file_path=relative_path,
                                    line_number=line_num,
                                    line_content=_short_line(line.strip()),
                                    context_before=[_short_line(item) for item in context_before],
                                    context_after=[_short_line(item) for item in context_after]
                                ))
                                
                                if len(results) >= max_results:
                                    logger.info(f"Search truncated at {max_results} results")
                                    return results
                        
                        files_searched += 1
                        
                    except (OSError, UnicodeDecodeError) as e:
                        logger.debug(f"Skipped file {file_path}: {e}")
                        continue
            
            logger.info(f"Search complete: {len(results)} matches in {files_searched} files")
            return results
            
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"Error searching files: {e}")

    def extract_workflow_from_image(self, image_path: str) -> dict:
        """Extract ComfyUI workflow from PNG metadata.
        
        Args:
            image_path: Path to PNG file relative to ComfyUI root
            
        Returns:
            Workflow dictionary if found, None if no metadata
            
        Raises:
            ComfyUIError: If file access fails or is invalid
        """
        try:
            import json

            from PIL import Image
            
            # Validate and resolve path
            full_path = self._validate_path(image_path)
            
            if not full_path.exists():
                raise ComfyUIError(f"Image file does not exist: {image_path}")
            
            if not full_path.is_file():
                raise ComfyUIError(f"Path is not a file: {image_path}")
            
            # Check file extension
            if full_path.suffix.lower() not in ['.png', '.webp']:
                raise ComfyUIError(
                    f"Unsupported file format: {full_path.suffix}. "
                    "Only PNG and WebP files contain workflow metadata."
                )
            
            # Open image and extract metadata
            img = Image.open(full_path)
            
            # Try to get workflow from metadata
            workflow_json = None
            
            # Method 1: Check img.text attribute (PNG tEXt chunks)
            if hasattr(img, 'text') and 'workflow' in img.text:
                workflow_json = img.text['workflow']
            
            # Method 2: Check img.info dictionary (fallback)
            elif 'workflow' in img.info:
                workflow_json = img.info['workflow']
            
            # No workflow found
            if not workflow_json:
                logger.info(f"No workflow metadata found in: {image_path}")
                return None
            
            # Parse JSON
            try:
                workflow = json.loads(workflow_json)
                logger.info(
                    f"Extracted workflow from {image_path}: "
                    f"{len(workflow.get('nodes', []))} nodes, "
                    f"version {workflow.get('version', 'unknown')}"
                )
                return workflow
            except json.JSONDecodeError as e:
                raise ComfyUIError(f"Invalid workflow JSON in image metadata: {e}")
            
        except ComfyUIError:
            raise
        except ImportError:
            raise ComfyUIError(
                "PIL (Pillow) not available. Install with: pip install pillow"
            )
        except Exception as e:
            raise ComfyUIError(f"Error extracting workflow from image: {e}")

# Global instance
_comfy_tools: Optional[ComfyUITools] = None


def get_comfy_tools() -> ComfyUITools:
    """Get or create the global ComfyUITools instance."""
    global _comfy_tools
    if _comfy_tools is None:
        _comfy_tools = ComfyUITools()
    return _comfy_tools
