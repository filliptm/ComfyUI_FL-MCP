from __future__ import annotations

import pytest

from backend.narrow_edit_idempotency import (
    NarrowEditIdempotencyError,
    NarrowEditOperationLedger,
    canonical_narrow_operation_hash,
    validate_narrow_operation_id,
)


def test_canonical_hash_is_stable_typed_and_does_not_retain_prompt_text():
    left = canonical_narrow_operation_hash(
        "update_connected_prompt",
        {
            "prompt": "private character description",
            "regions": [{"x": 0.25, "paint": True}],
        },
    )
    right = canonical_narrow_operation_hash(
        "update_connected_prompt",
        {
            "regions": [{"paint": True, "x": 0.25}],
            "prompt": "private character description",
        },
    )
    assert left == right
    assert len(left) == 64
    assert "private" not in left
    assert canonical_narrow_operation_hash("tool", {"id": 1}) != (
        canonical_narrow_operation_hash("tool", {"id": "1"})
    )


def test_canonical_hash_matches_browser_vector():
    assert canonical_narrow_operation_hash(
        "edit_node_mask",
        {
            "node_id": "1",
            "clear_existing": True,
            "regions": [{"x": 0.125, "y": -0.0, "shape": "ellipse"}],
        },
    ) == "e4ccc8b3329096322f2358b9c233be473a78d8291eeeedae64406dc5a8dfc50a"


@pytest.mark.parametrize(
    "value",
    ["short", " bad-op-id", "x" * 129, "contains/slash", True, None],
)
def test_operation_id_is_bounded_opaque_and_non_freeform(value):
    with pytest.raises(NarrowEditIdempotencyError) as exc_info:
        validate_narrow_operation_id(value)
    assert exc_info.value.code == "narrow_edit_operation_id_invalid"


def test_same_request_recovers_pending_then_completed_receipt_without_payload():
    now = [10.0]
    ledger = NarrowEditOperationLedger(clock=lambda: now[0])
    request_hash = canonical_narrow_operation_hash(
        "update_connected_prompt",
        {"prompt": "never retain this prompt", "operation": "append"},
    )
    kwargs = {
        "session_id": "browser-session",
        "tool": "update_connected_prompt",
        "operation_id": "prompt-op-0001",
        "request_hash": request_hash,
    }

    first = ledger.claim(**kwargs)
    assert first.status == "new"
    assert ledger.claim(**kwargs).status == "pending"
    ledger.complete(first, {"success": True, "queued": False, "sha256": "a" * 64})

    replay = ledger.claim(**kwargs)
    assert replay.status == "completed"
    assert replay.receipt == {"success": True, "queued": False, "sha256": "a" * 64}
    assert "never retain" not in repr(ledger._entries)

    replay.receipt["success"] = False
    assert ledger.claim(**kwargs).receipt["success"] is True


def test_same_operation_id_with_changed_payload_is_a_hard_conflict():
    ledger = NarrowEditOperationLedger()
    base = {
        "session_id": "browser-session",
        "tool": "edit_node_mask",
        "operation_id": "mask-op-000001",
    }
    ledger.claim(
        **base,
        request_hash=canonical_narrow_operation_hash("edit_node_mask", {"x": 0.2}),
    )
    with pytest.raises(NarrowEditIdempotencyError) as exc_info:
        ledger.claim(
            **base,
            request_hash=canonical_narrow_operation_hash("edit_node_mask", {"x": 0.3}),
        )
    assert exc_info.value.code == "narrow_edit_idempotency_conflict"
    assert "0.2" not in str(exc_info.value) and "0.3" not in str(exc_info.value)


def test_ttl_and_capacity_are_bounded_without_evicting_live_pending_claims():
    now = [0.0]
    ledger = NarrowEditOperationLedger(
        ttl_seconds=5,
        max_entries=2,
        clock=lambda: now[0],
    )

    def claim(suffix: str):
        return ledger.claim(
            session_id="browser-session",
            tool="edit_node_mask",
            operation_id=f"mask-op-{suffix}",
            request_hash=canonical_narrow_operation_hash(
                "edit_node_mask", {"suffix": suffix}
            ),
        )

    first = claim("00000001")
    claim("00000002")
    with pytest.raises(NarrowEditIdempotencyError) as exc_info:
        claim("00000003")
    assert exc_info.value.code == "narrow_edit_ledger_capacity"

    ledger.complete(first, {"success": True})
    assert claim("00000003").status == "new"
    assert len(ledger) == 2

    now[0] = 6.0
    assert len(ledger) == 0
    assert claim("00000004").status == "new"


def test_discard_only_releases_matching_pending_claim():
    ledger = NarrowEditOperationLedger()
    kwargs = {
        "session_id": "browser-session",
        "tool": "confirm_mask_review",
        "operation_id": "confirm-op-0001",
        "request_hash": canonical_narrow_operation_hash(
            "confirm_mask_review", {"review_token": "opaque"}
        ),
    }
    claim = ledger.claim(**kwargs)
    assert ledger.discard_pending(claim) is True
    assert ledger.discard_pending(claim) is False
    assert ledger.claim(**kwargs).status == "new"

    completed = ledger.claim(**kwargs)
    ledger.complete(completed, {"approved": True, "queued": False})
    assert ledger.discard_pending(completed) is False
