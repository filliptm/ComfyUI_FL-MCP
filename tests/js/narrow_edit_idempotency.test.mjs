import assert from "node:assert/strict";
import test from "node:test";

import {
    NarrowEditOperationLedger,
    canonicalNarrowOperationHash,
    validateNarrowOperationId,
} from "../../web/js/narrow_edit_idempotency.js";


test("canonical narrow hashes match stable typed JSON facts without exposing prompt text", async () => {
    const prompt = "private character description";
    const left = await canonicalNarrowOperationHash("update_connected_prompt", {
        prompt,
        region: { x: 0.25, paint: true },
    });
    const right = await canonicalNarrowOperationHash("update_connected_prompt", {
        region: { paint: true, x: 0.25 },
        prompt,
    });
    assert.equal(left, right);
    assert.match(left, /^[0-9a-f]{64}$/);
    assert.equal(left.includes("private"), false);
    assert.notEqual(
        await canonicalNarrowOperationHash("tool", { id: 1 }),
        await canonicalNarrowOperationHash("tool", { id: "1" }),
    );
});


test("browser and backend typed canonical hash vectors remain identical", async () => {
    // Produced by backend.narrow_edit_idempotency.canonical_narrow_operation_hash.
    assert.equal(
        await canonicalNarrowOperationHash("edit_node_mask", {
            node_id: "1",
            clear_existing: true,
            regions: [{ x: 0.125, y: -0, shape: "ellipse" }],
        }),
        "e4ccc8b3329096322f2358b9c233be473a78d8291eeeedae64406dc5a8dfc50a",
    );
});


test("operation IDs are bounded opaque identifiers", () => {
    assert.equal(validateNarrowOperationId("mask-op-0001"), "mask-op-0001");
    for (const value of ["short", " bad-op-id", "contains/slash", "x".repeat(129), null]) {
        assert.throws(
            () => validateNarrowOperationId(value),
            error => error?.code === "narrow_edit_operation_id_invalid",
        );
    }
});


test("same in-flight operation is coalesced and executes only once", async () => {
    const ledger = new NarrowEditOperationLedger();
    const requestHash = "a".repeat(64);
    let executeCalls = 0;
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const options = {
        tool: "set_node_values_exact",
        operationId: "prompt-op-0001",
        requestHash,
        executionPayload: { node_id: 34, value: "private prompt" },
        execute: async () => {
            executeCalls += 1;
            await gate;
            return { success: true, queued: false, value_sha256: "b".repeat(64) };
        },
        binding: result => ({ value_sha256: result.value_sha256 }),
        attest: async () => true,
    };

    const first = ledger.run(options);
    const retry = ledger.run(options);
    release();
    const [firstResult, retryResult] = await Promise.all([first, retry]);

    assert.equal(executeCalls, 1);
    assert.equal(firstResult.already_applied, false);
    assert.equal(retryResult.already_applied, true);
    assert.equal(firstResult.queued, false);
    assert.equal(JSON.stringify([...ledger.entries.values()]).includes("private prompt"), false);
});


test("completed retry requires fresh state attestation and returns cached success", async () => {
    const ledger = new NarrowEditOperationLedger();
    let executeCalls = 0;
    let stateMatches = true;
    const options = {
        tool: "edit_node_mask",
        operationId: "mask-op-000001",
        requestHash: "c".repeat(64),
        executionPayload: { node_id: 1, regions: [{ x: 0.2 }] },
        execute: async () => {
            executeCalls += 1;
            return {
                success: true,
                review_token: "review-opaque",
                image: { filename: "pending.png", type: "input" },
                queued: false,
            };
        },
        binding: result => ({
            review_token: result.review_token,
            image: result.image,
        }),
        attest: async (receipt, binding) => (
            stateMatches
            && receipt.review_token === binding.review_token
            && binding.image.filename === "pending.png"
        ),
    };

    await ledger.run(options);
    const replay = await ledger.run(options);
    assert.equal(executeCalls, 1);
    assert.equal(replay.already_applied, true);
    assert.equal(replay.review_token, "review-opaque");
    assert.equal(replay.queued, false);

    stateMatches = false;
    await assert.rejects(
        ledger.run(options),
        error => error?.code === "narrow_edit_replay_state_changed",
    );
    assert.equal(executeCalls, 1);
});


test("same operation ID with a changed canonical public request hard-conflicts", async () => {
    const ledger = new NarrowEditOperationLedger();
    const base = {
        tool: "edit_node_mask",
        operationId: "mask-op-000001",
        requestHash: "d".repeat(64),
        executionPayload: { x: 0.2 },
        execute: async () => ({ success: true, queued: false }),
        attest: async () => true,
    };
    await ledger.run(base);
    await assert.rejects(
        ledger.run({ ...base, requestHash: "e".repeat(64) }),
        error => error?.code === "narrow_edit_idempotency_conflict",
    );
    const replay = await ledger.run({ ...base, executionPayload: { x: 0.3 } });
    assert.equal(replay.already_applied, true);
});


test("failed or unknown execution leaves a tombstone and is never run twice", async () => {
    const ledger = new NarrowEditOperationLedger();
    let executeCalls = 0;
    const options = {
        tool: "confirm_mask_review",
        operationId: "confirm-op-0001",
        requestHash: "f".repeat(64),
        executionPayload: { review_token: "opaque" },
        execute: async () => {
            executeCalls += 1;
            throw new Error("connection vanished after an unknown point");
        },
        attest: async () => true,
    };
    await assert.rejects(ledger.run(options), /connection vanished/);
    await assert.rejects(
        ledger.run(options),
        error => error?.code === "narrow_edit_outcome_pending",
    );
    assert.equal(executeCalls, 1);
});


test("recover is read-only, attested, and confirmation receipts never queue", async () => {
    const ledger = new NarrowEditOperationLedger();
    const options = {
        tool: "confirm_mask_review",
        operationId: "confirm-op-0001",
        requestHash: "1".repeat(64),
        executionPayload: { node_id: 1, review_token: "opaque" },
        execute: async () => ({
            success: true,
            approved: true,
            review_token: "opaque",
            queued: false,
        }),
        binding: () => ({ image: { filename: "approved.png", type: "input" } }),
        attest: async (receipt, binding) => (
            receipt.approved === true && binding.image.filename === "approved.png"
        ),
    };
    await ledger.run(options);
    const recovered = await ledger.recover({
        tool: options.tool,
        operationId: options.operationId,
        requestHash: options.requestHash,
        attest: options.attest,
    });
    assert.equal(recovered.approved, true);
    assert.equal(recovered.already_applied, true);
    assert.equal(recovered.queued, false);

    await assert.rejects(
        ledger.recover({
            tool: "confirm_mask_review",
            operationId: "confirm-op-9999",
            requestHash: options.requestHash,
            attest: options.attest,
        }),
        error => error?.code === "narrow_edit_operation_not_found",
    );
});


test("TTL/capacity remain bounded and do not evict live pending operations", async () => {
    let now = 0;
    const ledger = new NarrowEditOperationLedger({
        ttlMs: 5,
        maxEntries: 2,
        now: () => now,
    });
    const completed = {
        tool: "edit_node_mask",
        operationId: "mask-op-complete",
        requestHash: "2".repeat(64),
        executionPayload: { value: 1 },
        execute: async () => ({ success: true }),
        attest: async () => true,
    };
    await ledger.run(completed);

    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const pending = ledger.run({
        ...completed,
        operationId: "mask-op-pending1",
        requestHash: "3".repeat(64),
        executionPayload: { value: 2 },
        execute: async () => {
            await gate;
            return { success: true };
        },
    });
    await Promise.resolve();

    // The completed receipt is evicted first; the pending call is retained.
    await ledger.run({
        ...completed,
        operationId: "mask-op-third001",
        requestHash: "4".repeat(64),
        executionPayload: { value: 3 },
    });
    assert.equal(ledger.size, 2);
    release();
    await pending;

    now = 6;
    assert.equal(ledger.size, 0);
});
