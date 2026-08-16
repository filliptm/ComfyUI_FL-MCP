/** Page-reload durable, plaintext-free queue-operation tombstones and receipts. */

import { canonicalTypedText, validateNarrowOperationId } from "./narrow_edit_idempotency.js";
import {
    validateExecutionPromptId,
    validateExecutionProvenance,
} from "./execution_provenance.js";

const STORAGE_KEY = "fl_mcp_queue_operations_v1";
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const MAX_ENTRIES = 256;


function failure(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
}


function operationFacts(operation) {
    const operationId = validateNarrowOperationId(operation?.operationId);
    const requestHash = operation?.operationRequestHash;
    if (typeof requestHash !== "string" || !SHA256_PATTERN.test(requestHash)) {
        throw failure("narrow_edit_request_hash_invalid", "The queue request digest is invalid.");
    }
    return { operationId, requestHash };
}


function storage() {
    const target = globalThis.sessionStorage;
    if (!target) {
        throw failure(
            "queue_durable_ledger_unavailable",
            "Durable page storage is unavailable, so queueing was blocked before submission.",
        );
    }
    try {
        const probe = `${STORAGE_KEY}:probe`;
        target.setItem(probe, "1");
        target.removeItem(probe);
    } catch (cause) {
        throw failure(
            "queue_durable_ledger_unavailable",
            "Durable page storage is unavailable, so queueing was blocked before submission.",
        );
    }
    return target;
}


function readEntries() {
    const raw = storage().getItem(STORAGE_KEY);
    if (raw === null) return {};
    let value;
    try {
        value = JSON.parse(raw);
        canonicalTypedText(value);
    } catch (cause) {
        throw failure(
            "queue_durable_ledger_corrupt",
            "The durable queue ledger is unreadable; queueing was blocked.",
        );
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw failure("queue_durable_ledger_corrupt", "The durable queue ledger is invalid.");
    }
    return value;
}


function writeEntries(entries) {
    storage().setItem(STORAGE_KEY, JSON.stringify(entries));
}


function safeReceipt(value, operation) {
    const facts = operationFacts(operation);
    let provenance;
    try {
        provenance = validateExecutionProvenance(value?.execution_provenance, operation);
    } catch {
        throw failure("queue_receipt_invalid", "The accepted queue provenance is invalid.");
    }
    const receipt = {
        queued: value?.queued === true,
        prompt_id: value?.prompt_id,
        queue_number: Number.isSafeInteger(value?.queue_number) ? value.queue_number : null,
        batch_count: 1,
        node_errors: {},
        execution_provenance: provenance,
    };
    if (
        !receipt.queued
        || !receipt.execution_provenance
    ) {
        throw failure("queue_receipt_invalid", "The accepted queue receipt is incomplete.");
    }
    try {
        validateExecutionPromptId(receipt.prompt_id);
    } catch {
        throw failure("queue_receipt_invalid", "The accepted queue receipt is incomplete.");
    }
    canonicalTypedText(receipt);
    return structuredClone(receipt);
}


export function readDurableQueueOperation(operation) {
    const { operationId, requestHash } = operationFacts(operation);
    const entry = readEntries()[operationId] ?? null;
    if (!entry) return null;
    if (entry.request_hash !== requestHash) {
        throw failure(
            "narrow_edit_idempotency_conflict",
            "operation_id was already used with different queue arguments.",
        );
    }
    if (entry.state === "pending") return { state: "pending" };
    if (entry.state !== "completed") {
        throw failure("queue_durable_ledger_corrupt", "The durable queue ledger is invalid.");
    }
    return { state: "completed", receipt: safeReceipt(entry.receipt, operation) };
}


export function reserveDurableQueueOperation(operation) {
    const { operationId, requestHash } = operationFacts(operation);
    const entries = readEntries();
    const existing = entries[operationId];
    if (existing) {
        if (existing.request_hash !== requestHash) {
            throw failure(
                "narrow_edit_idempotency_conflict",
                "operation_id was already used with different queue arguments.",
            );
        }
        if (existing.state === "completed") return safeReceipt(existing.receipt, operation);
        throw failure(
            "queue_outcome_unknown",
            "A prior queue attempt is unresolved; it was not queued again.",
        );
    }
    const keys = Object.keys(entries);
    if (keys.length >= MAX_ENTRIES) {
        throw failure(
            "queue_durable_ledger_capacity",
            "The durable queue ledger is full; queueing was blocked to preserve at-most-once safety.",
        );
    }
    entries[operationId] = { request_hash: requestHash, state: "pending" };
    writeEntries(entries);
    return null;
}


export function completeDurableQueueOperation(operation, result) {
    const { operationId, requestHash } = operationFacts(operation);
    const entries = readEntries();
    const entry = entries[operationId];
    if (!entry || entry.request_hash !== requestHash) {
        throw failure("queue_durable_ledger_conflict", "The queue reservation changed before completion.");
    }
    const receipt = safeReceipt(result, operation);
    entries[operationId] = {
        request_hash: requestHash,
        state: "completed",
        receipt,
    };
    writeEntries(entries);
    return receipt;
}


export function discardDurableQueueOperation(operation) {
    const { operationId, requestHash } = operationFacts(operation);
    const entries = readEntries();
    const entry = entries[operationId];
    if (!entry || entry.request_hash !== requestHash || entry.state !== "pending") return false;
    delete entries[operationId];
    writeEntries(entries);
    return true;
}


export function clearDurableQueueOperationsForTests() {
    storage().removeItem(STORAGE_KEY);
}
