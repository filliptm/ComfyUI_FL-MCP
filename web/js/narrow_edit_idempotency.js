/** Lost-response idempotency for Ren's narrow prompt and mask mutation lane. */

export const NARROW_EDIT_OPERATION_SCHEMA = "fl-mcp.narrow-edit-operation.v1";

const OPERATION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;


function narrowEditError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
}


export function validateNarrowOperationId(operationId) {
    if (typeof operationId !== "string" || !OPERATION_ID_PATTERN.test(operationId)) {
        throw narrowEditError(
            "narrow_edit_operation_id_invalid",
            "operation_id must be 8-128 characters using letters, digits, '.', '_', ':', or '-'.",
        );
    }
    return operationId;
}


function assertWellFormedString(value, path) {
    for (let index = 0; index < value.length; index += 1) {
        const code = value.charCodeAt(index);
        if (code >= 0xd800 && code <= 0xdbff) {
            const next = value.charCodeAt(index + 1);
            if (!(next >= 0xdc00 && next <= 0xdfff)) {
                throw narrowEditError(
                    "narrow_edit_payload_invalid",
                    `Operation payload text must be valid UTF-8 (at ${path}).`,
                );
            }
            index += 1;
        } else if (code >= 0xdc00 && code <= 0xdfff) {
            throw narrowEditError(
                "narrow_edit_payload_invalid",
                `Operation payload text must be valid UTF-8 (at ${path}).`,
            );
        }
    }
}


function utf8Hex(value, path) {
    assertWellFormedString(value, path);
    return [...new TextEncoder().encode(value)]
        .map(byte => byte.toString(16).padStart(2, "0"))
        .join("");
}


function compareUnicodeScalars(left, right) {
    const leftPoints = Array.from(left, character => character.codePointAt(0));
    const rightPoints = Array.from(right, character => character.codePointAt(0));
    const length = Math.min(leftPoints.length, rightPoints.length);
    for (let index = 0; index < length; index += 1) {
        if (leftPoints[index] < rightPoints[index]) return -1;
        if (leftPoints[index] > rightPoints[index]) return 1;
    }
    return leftPoints.length - rightPoints.length;
}


function float64Hex(value, path) {
    if (!Number.isFinite(value)) {
        throw narrowEditError(
            "narrow_edit_payload_invalid",
            `Operation payload numbers must be finite (at ${path}).`,
        );
    }
    const normalized = Object.is(value, -0) ? 0 : value;
    const bytes = new Uint8Array(8);
    new DataView(bytes.buffer).setFloat64(0, normalized, false);
    return [...bytes].map(byte => byte.toString(16).padStart(2, "0")).join("");
}


// path is diagnostic only (it names the exact offending field when this
// throws on a real, deeply nested workflow/prompt payload) - it never
// affects the canonical text or its hash, so existing hashes are unchanged.
export function canonicalTypedText(value, path = "$") {
    if (value === null) return "n;";
    if (typeof value === "boolean") return value ? "b1;" : "b0;";
    if (typeof value === "number") return `d${float64Hex(value, path)};`;
    if (typeof value === "string") {
        const encoded = new TextEncoder().encode(value);
        return `s${encoded.length}:${utf8Hex(value, path)};`;
    }
    if (Array.isArray(value)) {
        return `a${value.length}:[${
            value.map((item, index) => canonicalTypedText(item, `${path}[${index}]`)).join("")
        }];`;
    }
    if (value && typeof value === "object") {
        const prototype = Object.getPrototypeOf(value);
        if (prototype !== Object.prototype && prototype !== null) {
            throw narrowEditError(
                "narrow_edit_payload_invalid",
                `Operation payloads must use the exact JSON data model `
                + `(at ${path}: a non-plain object was found).`,
            );
        }
        const keys = Object.keys(value).sort(compareUnicodeScalars);
        return `o${keys.length}:{${keys.map(
            key => `${canonicalTypedText(key, `${path}.<key>`)}`
                + `${canonicalTypedText(value[key], `${path}.${key}`)}`,
        ).join("")}};`;
    }
    throw narrowEditError(
        "narrow_edit_payload_invalid",
        `Operation payloads must use the exact JSON data model `
        + `(at ${path}: found ${value === undefined ? "undefined" : typeof value}).`,
    );
}


export async function sha256Hex(value) {
    const bytes = new TextEncoder().encode(value);
    if (globalThis.crypto?.subtle) {
        const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
        return [...new Uint8Array(digest)]
            .map(byte => byte.toString(16).padStart(2, "0"))
            .join("");
    }
    if (typeof process !== "undefined" && process.versions?.node) {
        const { createHash } = await import("node:crypto");
        return createHash("sha256").update(bytes).digest("hex");
    }
    throw narrowEditError(
        "sha256_unavailable",
        "SHA-256 is unavailable in this browser.",
    );
}


export async function canonicalNarrowOperationHash(tool, payload) {
    if (typeof tool !== "string" || !tool || tool.length > 128) {
        throw narrowEditError(
            "narrow_edit_tool_invalid",
            "The narrow mutation tool name is invalid.",
        );
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw narrowEditError(
            "narrow_edit_payload_invalid",
            "The narrow mutation payload must be an object.",
        );
    }
    return await sha256Hex(canonicalTypedText({
        schema: NARROW_EDIT_OPERATION_SCHEMA,
        tool,
        payload,
    }));
}


function validateRequestHash(requestHash) {
    if (typeof requestHash !== "string" || !SHA256_PATTERN.test(requestHash)) {
        throw narrowEditError(
            "narrow_edit_request_hash_invalid",
            "The narrow mutation request digest is invalid.",
        );
    }
    return requestHash;
}


function cloneJson(value, label) {
    // Validate before cloning so receipts cannot retain DOM objects, promises,
    // image elements, undefined fields, or other non-portable page state.
    canonicalTypedText(value);
    try {
        return structuredClone(value);
    } catch (error) {
        throw narrowEditError(
            "narrow_edit_receipt_invalid",
            `${label} must be detached JSON data.`,
        );
    }
}


/**
 * Bounded page-lifetime ledger. It stores hashes plus caller-sanitized receipts
 * and bindings, never raw execution payloads (including prompt strings).
 */
export class NarrowEditOperationLedger {
    constructor({ ttlMs = 30 * 60 * 1000, maxEntries = 256, now = () => Date.now() } = {}) {
        if (!Number.isFinite(ttlMs) || ttlMs <= 0) {
            throw new TypeError("ttlMs must be positive");
        }
        if (!Number.isInteger(maxEntries) || maxEntries < 1) {
            throw new TypeError("maxEntries must be a positive integer");
        }
        this.ttlMs = ttlMs;
        this.maxEntries = maxEntries;
        this.now = now;
        this.entries = new Map();
    }

    _key(tool, operationId) {
        if (typeof tool !== "string" || !tool || tool.length > 128) {
            throw narrowEditError(
                "narrow_edit_tool_invalid",
                "The narrow mutation tool name is invalid.",
            );
        }
        return `${tool.length}:${tool}:${validateNarrowOperationId(operationId)}`;
    }

    _prune() {
        const now = this.now();
        for (const [key, entry] of this.entries) {
            if (entry.expiresAt <= now) this.entries.delete(key);
        }
    }

    _makeRoom() {
        if (this.entries.size < this.maxEntries) return;
        const completed = [...this.entries.entries()]
            .filter(([, entry]) => entry.state === "completed")
            .sort((left, right) => left[1].createdAt - right[1].createdAt);
        if (completed.length > 0) {
            this.entries.delete(completed[0][0]);
            return;
        }
        throw narrowEditError(
            "narrow_edit_ledger_capacity",
            "Too many narrow mutations are still pending; wait for one to finish before retrying.",
        );
    }

    _matchingEntry(tool, operationId, requestHash) {
        const key = this._key(tool, operationId);
        const digest = validateRequestHash(requestHash);
        const entry = this.entries.get(key) || null;
        if (entry && entry.requestHash !== digest) {
            throw narrowEditError(
                "narrow_edit_idempotency_conflict",
                "operation_id was already used with different mutation arguments.",
            );
        }
        return { key, digest, entry };
    }

    async _attestedReplay(entry, attest) {
        if (typeof attest !== "function") {
            throw narrowEditError(
                "narrow_edit_attestation_required",
                "An exact state attestation is required before replaying mutation success.",
            );
        }
        const valid = await attest(
            cloneJson(entry.receipt, "Mutation receipt"),
            cloneJson(entry.binding, "Mutation binding"),
        );
        if (valid !== true) {
            throw narrowEditError(
                "narrow_edit_replay_state_changed",
                "The workflow state no longer matches the completed mutation; it was not run again.",
            );
        }
        return {
            ...cloneJson(entry.receipt, "Mutation receipt"),
            operation_id: entry.operationId,
            operation_request_hash: entry.requestHash,
            already_applied: true,
            queued: entry.receipt?.queued === true,
        };
    }

    /** Execute at most once, coalesce concurrent retries, and attest later replay. */
    async run({
        tool,
        operationId,
        requestHash,
        requestPayload,
        executionPayload,
        execute,
        receipt = value => value,
        binding = () => ({}),
        attest,
    }) {
        this._prune();
        const { key, digest, entry } = this._matchingEntry(
            tool,
            operationId,
            requestHash,
        );
        const requestHashPromise = requestPayload === undefined
            ? Promise.resolve(digest)
            : canonicalNarrowOperationHash(tool, requestPayload);
        const executionHashPromise = canonicalNarrowOperationHash(tool, executionPayload);
        if (entry) {
            const observedRequestHash = await requestHashPromise;
            if (observedRequestHash !== digest) {
                throw narrowEditError(
                    "narrow_edit_request_hash_mismatch",
                    "The canonical mutation arguments do not match operation_request_hash.",
                );
            }
            // requestHash is the canonical public mutation contract. The
            // backend may re-resolve a post-commit graph after losing a reply,
            // so its derived transactional payload is allowed to differ and is
            // never executed when the public operation already exists.
            if (entry.state === "completed") {
                return await this._attestedReplay(entry, attest);
            }
            if (entry.promise) {
                const firstResult = await entry.promise;
                return {
                    ...cloneJson(firstResult, "Mutation receipt"),
                    operation_id: operationId,
                    operation_request_hash: digest,
                    already_applied: true,
                    queued: firstResult?.queued === true,
                };
            }
            throw narrowEditError(
                "narrow_edit_outcome_pending",
                "The prior mutation outcome is unknown and cannot be executed twice.",
            );
        }
        if (typeof execute !== "function") {
            throw new TypeError("execute must be a function");
        }
        this._makeRoom();
        const createdAt = this.now();
        const created = {
            tool,
            operationId,
            requestHash: digest,
            executionHash: null,
            executionHashPromise,
            state: "pending",
            createdAt,
            expiresAt: createdAt + this.ttlMs,
            promise: null,
            receipt: null,
            binding: null,
        };
        // Reserve synchronously before the digest promise yields. Concurrent
        // identical calls will observe this entry and join its one execution.
        this.entries.set(key, created);
        created.promise = (async () => {
            try {
                const observedRequestHash = await requestHashPromise;
                if (observedRequestHash !== digest) {
                    throw narrowEditError(
                        "narrow_edit_request_hash_mismatch",
                        "The canonical mutation arguments do not match operation_request_hash.",
                    );
                }
                created.executionHash = await executionHashPromise;
            } catch (error) {
                // Canonicalization happens before execute(), so this is a known
                // pre-mutation failure and needs no tombstone.
                this.entries.delete(key);
                throw error;
            }
            const result = await execute();
            const safeReceipt = cloneJson(await receipt(result), "Mutation receipt");
            const safeBinding = cloneJson(await binding(result), "Mutation binding");
            created.receipt = safeReceipt;
            created.binding = safeBinding;
            created.state = "completed";
            created.expiresAt = this.now() + this.ttlMs;
            created.promise = null;
            return safeReceipt;
        })();
        try {
            const firstReceipt = await created.promise;
            return {
                ...cloneJson(firstReceipt, "Mutation receipt"),
                operation_id: operationId,
                operation_request_hash: digest,
                already_applied: false,
                queued: firstReceipt?.queued === true,
            };
        } catch (error) {
            // Keep an unknown/pending tombstone. Retrying with the same ID must
            // recover or fail closed, never execute the mutation a second time.
            created.promise = null;
            throw error;
        }
    }

    /** Recover a prior completed result without executing any mutation. */
    async recover({ tool, operationId, requestHash, attest }) {
        this._prune();
        const { entry } = this._matchingEntry(tool, operationId, requestHash);
        if (!entry) {
            throw narrowEditError(
                "narrow_edit_operation_not_found",
                "No bounded browser receipt exists for this operation; it was not run again.",
            );
        }
        if (entry.state === "completed") {
            return await this._attestedReplay(entry, attest);
        }
        if (entry.promise) {
            const result = await entry.promise;
            return {
                ...cloneJson(result, "Mutation receipt"),
                operation_id: operationId,
                operation_request_hash: requestHash,
                already_applied: true,
                queued: result?.queued === true,
            };
        }
        throw narrowEditError(
            "narrow_edit_outcome_pending",
            "The prior mutation outcome is unknown and cannot be executed twice.",
        );
    }

    /** Release only a caller-proven pre-mutation failure. */
    discardPending({ tool, operationId, requestHash }) {
        this._prune();
        const { key, entry } = this._matchingEntry(tool, operationId, requestHash);
        if (!entry || entry.state !== "pending" || entry.promise) return false;
        this.entries.delete(key);
        return true;
    }

    clear() {
        this.entries.clear();
    }

    get size() {
        this._prune();
        return this.entries.size;
    }
}


// Module lifetime matches the ComfyUI page and survives executor reconnects.
export const narrowEditOperationLedger = new NarrowEditOperationLedger();
