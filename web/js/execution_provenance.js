/** Compact, plaintext-free identity for the exact ComfyUI queue submission. */

import {
    canonicalTypedText,
    sha256Hex,
    validateNarrowOperationId,
} from "./narrow_edit_idempotency.js";
import {
    GRAPH_PRECONDITION_SCHEMA,
    workflowGraphHash,
} from "./graph_precondition.js";

export const EXECUTION_PROVENANCE_SCHEMA = "fl-mcp.execution-provenance.v1";
export const EXECUTION_API_PROMPT_HASH_SCHEMA = "fl-mcp.execution-api-prompt.typed-v1";
export const EXECUTION_WORKFLOW_SNAPSHOT_HASH_SCHEMA = "fl-mcp.execution-workflow.typed-v1";
export const EXECUTION_PROVENANCE_KEY = "fl_mcp_execution_provenance";

const MAX_NODE_COUNT = 10000;
const MAX_CANONICAL_BYTES = 8 * 1024 * 1024;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const PROMPT_ID_PATTERN = /^[A-Za-z0-9_.:/-]{1,128}$/;
const PROVENANCE_FIELDS = new Set([
    "schema", "source", "operation_id", "operation_request_hash",
    "api_prompt", "editable_workflow", "graph_hash", "graph_hash_schema",
    "raw_prompt_returned", "captured_at_ms",
]);
const HASH_FACT_FIELDS = new Set([
    "schema", "sha256", "canonical_bytes", "node_count",
]);
const WORKFLOW_HASH_FACT_FIELDS = new Set([
    ...HASH_FACT_FIELDS, "workflow_id", "revision",
]);


function exactKeys(value, fields) {
    return Boolean(
        value
        && typeof value === "object"
        && !Array.isArray(value)
        && Object.keys(value).length === fields.size
        && Object.keys(value).every(key => fields.has(key))
    );
}


function boundedHashFact(value, fields, schema) {
    return Boolean(
        exactKeys(value, fields)
        && value.schema === schema
        && typeof value.sha256 === "string"
        && SHA256_PATTERN.test(value.sha256)
        && Number.isSafeInteger(value.canonical_bytes)
        && value.canonical_bytes > 0
        && value.canonical_bytes <= MAX_CANONICAL_BYTES
        && Number.isSafeInteger(value.node_count)
        && value.node_count >= 0
        && value.node_count <= MAX_NODE_COUNT
    );
}


function queueOperationFacts(operation = {}) {
    const operationId = validateNarrowOperationId(operation.operationId);
    const operationRequestHash = operation.operationRequestHash;
    if (typeof operationRequestHash !== "string" || !SHA256_PATTERN.test(operationRequestHash)) {
        throw new Error("The queue operation request digest is invalid.");
    }
    return {
        operation_id: operationId,
        operation_request_hash: operationRequestHash,
    };
}


export function validateExecutionPromptId(value) {
    if (
        typeof value !== "string"
        || !PROMPT_ID_PATTERN.test(value)
        || new TextEncoder().encode(value).length > 256
    ) {
        throw new Error("The execution prompt identifier is invalid.");
    }
    return value;
}


/** Strict, detached, plaintext-free execution provenance validation. */
export function validateExecutionProvenance(value, operation = null) {
    const operationFacts = operation ? queueOperationFacts(operation) : null;
    let workflowIdBytes = null;
    try {
        canonicalTypedText(value);
        workflowIdBytes = typeof value?.editable_workflow?.workflow_id === "string"
            ? new TextEncoder().encode(value.editable_workflow.workflow_id).length
            : null;
    } catch {
        throw new Error("The execution provenance is invalid.");
    }
    if (
        !exactKeys(value, PROVENANCE_FIELDS)
        || value.schema !== EXECUTION_PROVENANCE_SCHEMA
        || value.source !== "frontend_queue_capture"
        || value.raw_prompt_returned !== false
        || value.graph_hash_schema !== GRAPH_PRECONDITION_SCHEMA
        || typeof value.graph_hash !== "string"
        || !SHA256_PATTERN.test(value.graph_hash)
        || !Number.isSafeInteger(value.captured_at_ms)
        || value.captured_at_ms < 0
        || validateNarrowOperationId(value.operation_id) !== value.operation_id
        || typeof value.operation_request_hash !== "string"
        || !SHA256_PATTERN.test(value.operation_request_hash)
        || !boundedHashFact(
            value.api_prompt,
            HASH_FACT_FIELDS,
            EXECUTION_API_PROMPT_HASH_SCHEMA,
        )
        || !boundedHashFact(
            value.editable_workflow,
            WORKFLOW_HASH_FACT_FIELDS,
            EXECUTION_WORKFLOW_SNAPSHOT_HASH_SCHEMA,
        )
        || !(
            value.editable_workflow.workflow_id === null
            || (
                typeof value.editable_workflow.workflow_id === "string"
                && workflowIdBytes <= 256
            )
        )
        || !(
            value.editable_workflow.revision === null
            || Number.isSafeInteger(value.editable_workflow.revision)
        )
        || (
            operationFacts
            && (
                value.operation_id !== operationFacts.operation_id
                || value.operation_request_hash !== operationFacts.operation_request_hash
            )
        )
    ) {
        throw new Error("The execution provenance is invalid.");
    }
    return structuredClone(value);
}


async function typedHash(schema, value) {
    const canonical = canonicalTypedText({ schema, value });
    const canonicalBytes = new TextEncoder().encode(canonical).length;
    if (canonicalBytes > MAX_CANONICAL_BYTES) {
        throw new Error("The queued submission exceeds the provenance byte limit.");
    }
    return {
        schema,
        sha256: await sha256Hex(canonical),
        canonical_bytes: canonicalBytes,
    };
}


function nodeCount(value, label) {
    const nodes = label === "api_prompt" ? value : value?.nodes;
    if (!nodes || typeof nodes !== "object" || Array.isArray(nodes) !== (label !== "api_prompt")) {
        throw new Error(`The submitted ${label} has an invalid node collection.`);
    }
    const count = Array.isArray(nodes) ? nodes.length : Object.keys(nodes).length;
    if (count > MAX_NODE_COUNT) {
        throw new Error(`The submitted ${label} exceeds the node limit.`);
    }
    return count;
}


export function submissionCarriesExecutionProvenance(data, expected) {
    const record = data?.workflow?.extra?.[EXECUTION_PROVENANCE_KEY];
    if (!record || !expected) return false;
    try {
        return canonicalTypedText(record) === canonicalTypedText(expected);
    } catch {
        return false;
    }
}


export function executionProvenanceFromSubmission(data) {
    const record = data?.workflow?.extra?.[EXECUTION_PROVENANCE_KEY];
    if (!record) return null;
    try {
        return validateExecutionProvenance(record);
    } catch {
        return null;
    }
}


function provenanceFromPromptTuple(promptTuple) {
    return promptTuple?.[3]?.extra_pnginfo?.workflow?.extra?.[EXECUTION_PROVENANCE_KEY] ?? null;
}


function isQueueOperationRecord(record, operationId) {
    try {
        return validateExecutionProvenance(record).operation_id === operationId;
    } catch {
        return false;
    }
}


async function verifiedQueueOperationRecord(promptTuple, operationId) {
    const record = provenanceFromPromptTuple(promptTuple);
    if (!isQueueOperationRecord(record, operationId)) return null;
    const apiPrompt = promptTuple?.[2];
    const workflow = promptTuple?.[3]?.extra_pnginfo?.workflow;
    if (!apiPrompt || !workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
        return null;
    }
    const detachedWorkflow = structuredClone(workflow);
    if (
        !detachedWorkflow.extra
        || typeof detachedWorkflow.extra !== "object"
        || Array.isArray(detachedWorkflow.extra)
    ) return null;
    delete detachedWorkflow.extra[EXECUTION_PROVENANCE_KEY];
    if (!Number.isSafeInteger(record.captured_at_ms) || record.captured_at_ms < 0) return null;
    try {
        const expected = await executionProvenanceForSubmission(
            { output: apiPrompt, workflow: detachedWorkflow },
            {
                graph_hash: await workflowGraphHash(detachedWorkflow),
                graph_hash_schema: GRAPH_PRECONDITION_SCHEMA,
            },
            {
                operationId: record.operation_id,
                operationRequestHash: record.operation_request_hash,
            },
        );
        expected.captured_at_ms = record.captured_at_ms;
        return canonicalTypedText(record) === canonicalTypedText(expected)
            ? structuredClone(record)
            : null;
    } catch {
        return null;
    }
}


/** Locate only compact receipts; queued API prompt/workflow payloads never escape. */
export async function recoverQueueOperationFromPayloads(
    { queue = null, history = null } = {},
    operation,
) {
    const facts = queueOperationFacts(operation);
    const candidates = [];
    const conflicts = [];
    const inspect = async (promptId, promptTuple, queueNumber = null) => {
        try {
            validateExecutionPromptId(promptId);
        } catch {
            return;
        }
        const record = await verifiedQueueOperationRecord(promptTuple, facts.operation_id);
        if (!record) return;
        const candidate = {
            prompt_id: promptId,
            queue_number: Number.isSafeInteger(queueNumber) ? queueNumber : null,
            batch_count: 1,
            node_errors: {},
            queued: true,
            execution_provenance: structuredClone(record),
        };
        if (record.operation_request_hash !== facts.operation_request_hash) {
            conflicts.push(candidate);
        } else {
            candidates.push(candidate);
        }
    };
    for (const item of [
        ...(Array.isArray(queue?.queue_running) ? queue.queue_running : []),
        ...(Array.isArray(queue?.queue_pending) ? queue.queue_pending : []),
    ]) {
        if (Array.isArray(item)) await inspect(item[1], item, item[0]);
    }
    if (history && typeof history === "object" && !Array.isArray(history)) {
        for (const [promptId, record] of Object.entries(history)) {
            await inspect(promptId, record?.prompt, record?.prompt?.[0]);
        }
    }
    if (conflicts.length > 0) {
        const error = new Error("operation_id already identifies a different queued request.");
        error.code = "narrow_edit_idempotency_conflict";
        throw error;
    }
    const unique = new Map();
    for (const candidate of candidates) unique.set(candidate.prompt_id, candidate);
    if (unique.size > 1) {
        const error = new Error("Multiple executions claim the same queue operation ID.");
        error.code = "queue_operation_ambiguous";
        throw error;
    }
    return unique.size === 1 ? structuredClone(unique.values().next().value) : null;
}


/** Hash the exact output/workflow pair passed to api.queuePrompt. */
export async function executionProvenanceForSubmission(
    data,
    workflowFacts = {},
    operation = null,
) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
        throw new Error("The queued submission must be an exact object.");
    }
    const apiPrompt = data.output;
    const workflow = data.workflow;
    if (!apiPrompt || !workflow) {
        throw new Error("The queued submission lacks output or workflow data.");
    }
    const [apiPromptIdentity, workflowIdentity] = await Promise.all([
        typedHash(EXECUTION_API_PROMPT_HASH_SCHEMA, apiPrompt),
        typedHash(EXECUTION_WORKFLOW_SNAPSHOT_HASH_SCHEMA, workflow),
    ]);
    return {
        schema: EXECUTION_PROVENANCE_SCHEMA,
        source: "frontend_queue_capture",
        ...(operation ? queueOperationFacts(operation) : {}),
        api_prompt: {
            ...apiPromptIdentity,
            node_count: nodeCount(apiPrompt, "api_prompt"),
        },
        editable_workflow: {
            ...workflowIdentity,
            node_count: nodeCount(workflow, "editable_workflow"),
            workflow_id: typeof workflow.id === "string" ? workflow.id : null,
            revision: Number.isSafeInteger(workflow.revision) ? workflow.revision : null,
        },
        graph_hash: workflowFacts.graph_hash,
        graph_hash_schema: workflowFacts.graph_hash_schema,
        raw_prompt_returned: false,
    };
}


/**
 * Return a detached queue submission with a compact provenance record embedded
 * in the submitted workflow metadata. The live canvas is never modified.
 */
export async function prepareExecutionSubmission(data, operation) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
        throw new Error("The queued submission must be an exact object.");
    }
    const submission = structuredClone(data);
    if (!submission.workflow || typeof submission.workflow !== "object") {
        throw new Error("The queued submission lacks editable workflow data.");
    }
    if (
        submission.workflow.extra !== undefined
        && (
            !submission.workflow.extra
            || typeof submission.workflow.extra !== "object"
            || Array.isArray(submission.workflow.extra)
        )
    ) {
        throw new Error("The submitted workflow has invalid extra metadata.");
    }
    submission.workflow.extra ||= {};
    delete submission.workflow.extra[EXECUTION_PROVENANCE_KEY];
    const graphHash = await workflowGraphHash(submission.workflow);
    const provenance = await executionProvenanceForSubmission(submission, {
        graph_hash: graphHash,
        graph_hash_schema: GRAPH_PRECONDITION_SCHEMA,
    }, operation);
    provenance.captured_at_ms = Date.now();
    submission.workflow.extra[EXECUTION_PROVENANCE_KEY] = structuredClone(provenance);
    return { submission, provenance };
}
