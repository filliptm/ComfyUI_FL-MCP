import assert from "node:assert/strict";
import test from "node:test";

class MemoryStorage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
    setItem(key, value) { this.values.set(key, String(value)); }
    removeItem(key) { this.values.delete(key); }
}

globalThis.sessionStorage = new MemoryStorage();

const {
    clearDurableQueueOperationsForTests,
    completeDurableQueueOperation,
    readDurableQueueOperation,
    reserveDurableQueueOperation,
} = await import("../../web/js/queue_operation_store.js");

const operation = {
    operationId: "queue-op-store01",
    operationRequestHash: "a".repeat(64),
};

function receipt() {
    return {
        queued: true,
        prompt_id: "prompt-1",
        queue_number: 4,
        execution_provenance: {
            schema: "fl-mcp.execution-provenance.v1",
            source: "frontend_queue_capture",
            operation_id: operation.operationId,
            operation_request_hash: operation.operationRequestHash,
            api_prompt: {
                schema: "fl-mcp.execution-api-prompt.typed-v1",
                sha256: "b".repeat(64),
                canonical_bytes: 123,
                node_count: 2,
            },
            editable_workflow: {
                schema: "fl-mcp.execution-workflow.typed-v1",
                sha256: "c".repeat(64),
                canonical_bytes: 456,
                node_count: 2,
                workflow_id: "workflow-1",
                revision: 3,
            },
            graph_hash: "d".repeat(64),
            graph_hash_schema: "fl-mcp.graph-precondition.v1",
            raw_prompt_returned: false,
            captured_at_ms: 1,
        },
    };
}

test("durable queue reservation survives a page module retry and blocks reenqueuing", () => {
    clearDurableQueueOperationsForTests();
    assert.equal(reserveDurableQueueOperation(operation), null);
    assert.deepEqual(readDurableQueueOperation(operation), { state: "pending" });
    assert.throws(
        () => reserveDurableQueueOperation(operation),
        error => error?.code === "queue_outcome_unknown",
    );
    assert.throws(
        () => readDurableQueueOperation({ ...operation, operationRequestHash: "e".repeat(64) }),
        error => error?.code === "narrow_edit_idempotency_conflict",
    );
});

test("durable completed receipt is compact, exact, and plaintext-free", () => {
    clearDurableQueueOperationsForTests();
    reserveDurableQueueOperation(operation);
    const completed = completeDurableQueueOperation(operation, {
        ...receipt(),
        raw_result: { prompt: "PRIVATE PROMPT" },
        node_errors: { "34": { message: "PRIVATE PROMPT" } },
    });
    assert.equal(completed.prompt_id, "prompt-1");
    assert.doesNotMatch(JSON.stringify(completed), /PRIVATE PROMPT/);
    assert.deepEqual(readDurableQueueOperation(operation), {
        state: "completed",
        receipt: completed,
    });
});

test("durable completion rejects provenance bound to another queue request", () => {
    clearDurableQueueOperationsForTests();
    reserveDurableQueueOperation(operation);
    const changed = receipt();
    changed.execution_provenance.operation_request_hash = "f".repeat(64);
    assert.throws(
        () => completeDurableQueueOperation(operation, changed),
        error => error?.code === "queue_receipt_invalid",
    );
});


test("corrupt durable receipts never return nested plaintext or malformed identity facts", () => {
    const corruptions = [
        value => { value.execution_provenance.api_prompt.secret = "PRIVATE NESTED PROMPT"; },
        value => { value.execution_provenance.editable_workflow.secret = "PRIVATE NESTED PROMPT"; },
        value => { value.execution_provenance.graph_hash_schema = "wrong-schema"; },
        value => { value.execution_provenance.captured_at_ms = "1"; },
        value => { value.prompt_id = "PRIVATE PROMPT ID WITH SPACES"; },
    ];
    for (const corrupt of corruptions) {
        clearDurableQueueOperationsForTests();
        reserveDurableQueueOperation(operation);
        completeDurableQueueOperation(operation, receipt());
        const key = "fl_mcp_queue_operations_v1";
        const stored = JSON.parse(sessionStorage.getItem(key));
        corrupt(stored[operation.operationId].receipt);
        sessionStorage.setItem(key, JSON.stringify(stored));
        let observed;
        assert.throws(
            () => { observed = readDurableQueueOperation(operation); },
            error => {
                assert.equal(error?.code, "queue_receipt_invalid");
                assert.doesNotMatch(String(error?.message), /PRIVATE|wrong-schema/);
                return true;
            },
        );
        assert.equal(observed, undefined);
    }
});
