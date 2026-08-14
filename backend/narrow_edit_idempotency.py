"""Bounded in-process receipts for Ren's narrow prompt and mask mutations.

The browser remains the durable authority for a ComfyUI page session.  This
ledger only closes duplicate-call races inside one MCP subprocess and keeps no
raw request payloads (notably, no prompt text).  A caller-provided operation ID
is bound to a canonical request digest; reusing it for different arguments is
always an error.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

NARROW_EDIT_OPERATION_SCHEMA = "fl-mcp.narrow-edit-operation.v1"
NARROW_EDIT_OPERATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
)
NARROW_EDIT_REQUEST_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class NarrowEditIdempotencyError(ValueError):
    """A bounded, classified idempotency failure safe for user diagnostics."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_narrow_operation_id(operation_id: Any) -> str:
    """Validate one opaque, non-secret operation identifier."""

    if not isinstance(operation_id, str) or not NARROW_EDIT_OPERATION_ID_PATTERN.fullmatch(
        operation_id
    ):
        raise NarrowEditIdempotencyError(
            "narrow_edit_operation_id_invalid",
            "operation_id must be 8-128 characters using letters, digits, '.', '_', ':', or '-'.",
        )
    return operation_id


def _canonical_typed_bytes(value: Any) -> bytes:
    """Encode exact JSON facts consistently with the browser implementation."""

    item_type = type(value)
    if value is None:
        return b"n;"
    if item_type is bool:
        return b"b1;" if value else b"b0;"
    if item_type in {int, float}:
        try:
            number = float(value)
        except OverflowError as exc:
            raise NarrowEditIdempotencyError(
                "narrow_edit_payload_invalid",
                "Operation payload numbers must fit the browser Number domain.",
            ) from exc
        if not math.isfinite(number):
            raise NarrowEditIdempotencyError(
                "narrow_edit_payload_invalid",
                "Operation payload numbers must be finite.",
            )
        if number == 0:
            number = 0.0
        return b"d" + struct.pack(">d", number).hex().encode("ascii") + b";"
    if item_type is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise NarrowEditIdempotencyError(
                "narrow_edit_payload_invalid",
                "Operation payload text must be valid UTF-8.",
            ) from exc
        return (
            b"s"
            + str(len(encoded)).encode("ascii")
            + b":"
            + encoded.hex().encode("ascii")
            + b";"
        )
    if item_type is list:
        return (
            b"a"
            + str(len(value)).encode("ascii")
            + b":["
            + b"".join(_canonical_typed_bytes(item) for item in value)
            + b"];"
        )
    if item_type is dict:
        if any(type(key) is not str for key in value):
            raise NarrowEditIdempotencyError(
                "narrow_edit_payload_invalid",
                "Operation payload object keys must be strings.",
            )
        encoded_items: list[bytes] = []
        for key in sorted(value):
            encoded_items.append(_canonical_typed_bytes(key))
            encoded_items.append(_canonical_typed_bytes(value[key]))
        return (
            b"o"
            + str(len(value)).encode("ascii")
            + b":{" + b"".join(encoded_items) + b"};"
        )
    raise NarrowEditIdempotencyError(
        "narrow_edit_payload_invalid",
        "Operation payloads must use the exact JSON data model.",
    )


def canonical_narrow_operation_hash(tool: str, payload: Mapping[str, Any]) -> str:
    """Hash one mutation request without retaining or logging its raw payload."""

    if not isinstance(tool, str) or not tool or len(tool) > 128:
        raise NarrowEditIdempotencyError(
            "narrow_edit_tool_invalid",
            "The narrow mutation tool name is invalid.",
        )
    if not isinstance(payload, Mapping):
        raise NarrowEditIdempotencyError(
            "narrow_edit_payload_invalid",
            "The narrow mutation payload must be an object.",
        )
    canonical_payload = {
        "schema": NARROW_EDIT_OPERATION_SCHEMA,
        "tool": tool,
        "payload": dict(payload),
    }
    return hashlib.sha256(_canonical_typed_bytes(canonical_payload)).hexdigest()


@dataclass(frozen=True)
class NarrowEditClaim:
    """One exact claim returned without exposing retained mutable state."""

    status: Literal["new", "pending", "completed"]
    session_id: str
    tool: str
    operation_id: str
    request_hash: str
    receipt: dict[str, Any] | None = None


@dataclass
class _LedgerEntry:
    session_id: str
    tool: str
    operation_id: str
    request_hash: str
    state: Literal["pending", "completed"]
    created_at: float
    expires_at: float
    receipt: dict[str, Any] | None = None


class NarrowEditOperationLedger:
    """Thread-safe TTL ledger that retains only digests and safe receipts."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30 * 60,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, str, str], _LedgerEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(session_id: str, tool: str, operation_id: str) -> tuple[str, str, str]:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise NarrowEditIdempotencyError(
                "narrow_edit_session_invalid",
                "The browser session identity is invalid.",
            )
        if not isinstance(tool, str) or not tool or len(tool) > 128:
            raise NarrowEditIdempotencyError(
                "narrow_edit_tool_invalid",
                "The narrow mutation tool name is invalid.",
            )
        return session_id, tool, validate_narrow_operation_id(operation_id)

    @staticmethod
    def _request_hash(request_hash: Any) -> str:
        if not isinstance(request_hash, str) or not NARROW_EDIT_REQUEST_HASH_PATTERN.fullmatch(
            request_hash
        ):
            raise NarrowEditIdempotencyError(
                "narrow_edit_request_hash_invalid",
                "The narrow mutation request digest is invalid.",
            )
        return request_hash

    def _prune(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[key]

    def _make_room(self) -> None:
        if len(self._entries) < self._max_entries:
            return
        completed = [
            (key, entry)
            for key, entry in self._entries.items()
            if entry.state == "completed"
        ]
        if completed:
            oldest_key, _ = min(completed, key=lambda item: item[1].created_at)
            del self._entries[oldest_key]
            return
        raise NarrowEditIdempotencyError(
            "narrow_edit_ledger_capacity",
            "Too many narrow mutations are still pending; wait for one to finish before retrying.",
        )

    def claim(
        self,
        *,
        session_id: str,
        tool: str,
        operation_id: str,
        request_hash: str,
    ) -> NarrowEditClaim:
        """Claim or recover one exact operation without retaining its payload."""

        key = self._key(session_id, tool, operation_id)
        digest = self._request_hash(request_hash)
        with self._lock:
            now = self._clock()
            self._prune(now)
            existing = self._entries.get(key)
            if existing is not None:
                if existing.request_hash != digest:
                    raise NarrowEditIdempotencyError(
                        "narrow_edit_idempotency_conflict",
                        "operation_id was already used with different mutation arguments.",
                    )
                return NarrowEditClaim(
                    status=existing.state,
                    session_id=session_id,
                    tool=tool,
                    operation_id=operation_id,
                    request_hash=digest,
                    receipt=deepcopy(existing.receipt),
                )
            self._make_room()
            self._entries[key] = _LedgerEntry(
                session_id=session_id,
                tool=tool,
                operation_id=operation_id,
                request_hash=digest,
                state="pending",
                created_at=now,
                expires_at=now + self._ttl_seconds,
            )
            return NarrowEditClaim(
                status="new",
                session_id=session_id,
                tool=tool,
                operation_id=operation_id,
                request_hash=digest,
            )

    def complete(self, claim: NarrowEditClaim, receipt: Mapping[str, Any]) -> None:
        """Store a caller-sanitized receipt for exact replay."""

        if not isinstance(receipt, Mapping):
            raise NarrowEditIdempotencyError(
                "narrow_edit_receipt_invalid",
                "The narrow mutation receipt must be an object.",
            )
        key = self._key(claim.session_id, claim.tool, claim.operation_id)
        with self._lock:
            now = self._clock()
            self._prune(now)
            entry = self._entries.get(key)
            if entry is None or entry.request_hash != claim.request_hash:
                raise NarrowEditIdempotencyError(
                    "narrow_edit_claim_stale",
                    "The narrow mutation claim expired or changed before completion.",
                )
            entry.state = "completed"
            entry.receipt = deepcopy(dict(receipt))
            entry.expires_at = now + self._ttl_seconds

    def discard_pending(self, claim: NarrowEditClaim) -> bool:
        """Release one known pre-commit failure; completed receipts are immutable."""

        key = self._key(claim.session_id, claim.tool, claim.operation_id)
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is None
                or entry.request_hash != claim.request_hash
                or entry.state != "pending"
            ):
                return False
            del self._entries[key]
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._entries)
