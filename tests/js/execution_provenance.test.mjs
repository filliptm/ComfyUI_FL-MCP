import assert from "node:assert/strict";
import test from "node:test";

import {
    EXECUTION_PROVENANCE_KEY,
    executionProvenanceFromSubmission,
    executionProvenanceForSubmission,
    prepareExecutionSubmission,
    recoverQueueOperationFromPayloads,
    submissionCarriesExecutionProvenance,
    validateExecutionProvenance,
    validateExecutionPromptId,
} from "../../web/js/execution_provenance.js";
import {
    GRAPH_PRECONDITION_SCHEMA,
    workflowGraphHash,
} from "../../web/js/graph_precondition.js";


function submission() {
    return {
        output: {
            "1": { class_type: "LoadImage", inputs: { image: "mask.png [input]" } },
            "34": { class_type: "PrimitiveStringMultiline", inputs: { value: "private prompt" } },
        },
        workflow: {
            id: "workflow-1",
            revision: 2,
            nodes: [
                { id: 1, type: "LoadImage", widgets_values: ["mask.png [input]"] },
                { id: 34, type: "PrimitiveStringMultiline", widgets_values: ["private prompt"] },
            ],
            links: [],
            extra: { ds: { scale: 2, offset: [1, 2] } },
        },
    };
}


test("queue provenance is embedded only in the detached submitted workflow", async () => {
    const original = submission();
    const expectedGraphHash = await workflowGraphHash(original.workflow);
    const operation = { operationId: "queue-op-0001", operationRequestHash: "d".repeat(64) };
    const { submission: queued, provenance } = await prepareExecutionSubmission(original, operation);

    assert.equal(Object.hasOwn(original.workflow.extra, EXECUTION_PROVENANCE_KEY), false);
    assert.deepEqual(queued.output, original.output);
    assert.deepEqual(queued.workflow.nodes, original.workflow.nodes);
    assert.deepEqual(queued.workflow.links, original.workflow.links);
    assert.deepEqual(queued.workflow.extra.ds, original.workflow.extra.ds);
    assert.deepEqual(queued.workflow.extra[EXECUTION_PROVENANCE_KEY], provenance);
    assert.equal(submissionCarriesExecutionProvenance(queued, provenance), true);
    assert.deepEqual(executionProvenanceFromSubmission(queued), provenance);
    assert.equal(submissionCarriesExecutionProvenance(original, provenance), false);
    assert.equal(provenance.graph_hash, expectedGraphHash);
    assert.equal(provenance.api_prompt.node_count, 2);
    assert.equal(provenance.editable_workflow.node_count, 2);
    assert.equal(provenance.editable_workflow.workflow_id, "workflow-1");
    assert.equal(provenance.editable_workflow.revision, 2);
    assert.equal(provenance.raw_prompt_returned, false);
    assert.equal(provenance.operation_id, operation.operationId);
    assert.equal(provenance.operation_request_hash, operation.operationRequestHash);
    assert.doesNotMatch(JSON.stringify(provenance), /private prompt|mask\.png/);
});

test("hashes the exact submitted workflow shape after reserving provenance metadata", async () => {
    const original = {
        output: { "1": { class_type: "LoadImage", inputs: { image: "source.png" } } },
        workflow: { id: "wf-no-extra", revision: 0, nodes: [], links: [] },
    };
    const submittedWithoutRecord = structuredClone(original);
    submittedWithoutRecord.workflow.extra = {};
    const expectedGraphHash = await workflowGraphHash(submittedWithoutRecord.workflow);
    const expected = await executionProvenanceForSubmission(submittedWithoutRecord, {
        graph_hash: expectedGraphHash,
        graph_hash_schema: GRAPH_PRECONDITION_SCHEMA,
    });

    const { submission, provenance } = await prepareExecutionSubmission(original, {
        operationId: "queue-op-0002",
        operationRequestHash: "e".repeat(64),
    });

    assert.equal(provenance.graph_hash, expectedGraphHash);
    assert.equal(provenance.editable_workflow.sha256, expected.editable_workflow.sha256);
    assert.equal(Object.hasOwn(original.workflow, "extra"), false);
    assert.equal(submission.workflow.extra.fl_mcp_execution_provenance.schema, provenance.schema);
});


test("queue/history recovery returns one compact exact operation receipt", async () => {
    const operation = { operationId: "queue-op-0003", operationRequestHash: "f".repeat(64) };
    const prepared = await prepareExecutionSubmission(submission(), operation);
    const tuple = [7, "prompt-7", prepared.submission.output, {
        extra_pnginfo: { workflow: prepared.submission.workflow },
    }];
    const recovered = await recoverQueueOperationFromPayloads(
        { queue: { queue_running: [], queue_pending: [tuple] }, history: {} },
        operation,
    );
    assert.equal(recovered.prompt_id, "prompt-7");
    assert.equal(recovered.queued, true);
    assert.doesNotMatch(JSON.stringify(recovered), /private prompt|mask\.png/);
    await assert.rejects(
        recoverQueueOperationFromPayloads({
            queue: { queue_running: [tuple], queue_pending: [[8, "prompt-8", ...tuple.slice(2)]] },
        }, operation),
        error => error?.code === "queue_operation_ambiguous",
    );
});


test("prompt, mask, numeric, and Unicode-key changes alter typed provenance", async () => {
    const base = submission();
    base.output["edge"] = {
        class_type: "Test",
        inputs: { small: 1e-7, one: 1.0, negzero: -0, "\u{10000}": 1, "\uE000": 2 },
    };
    const first = await executionProvenanceForSubmission(base);

    const promptChanged = structuredClone(base);
    promptChanged.output["34"].inputs.value = "another private prompt";
    const maskChanged = structuredClone(base);
    maskChanged.output["1"].inputs.image = "other.png [input]";

    assert.notEqual(
        first.api_prompt.sha256,
        (await executionProvenanceForSubmission(promptChanged)).api_prompt.sha256,
    );
    assert.notEqual(
        first.api_prompt.sha256,
        (await executionProvenanceForSubmission(maskChanged)).api_prompt.sha256,
    );
    assert.match(first.api_prompt.sha256, /^[0-9a-f]{64}$/);
});


test("strict provenance validation rejects all extra/nontyped identity facts", async () => {
    const operation = { operationId: "queue-op-0004", operationRequestHash: "9".repeat(64) };
    const { provenance } = await prepareExecutionSubmission(submission(), operation);
    assert.deepEqual(validateExecutionProvenance(provenance, operation), provenance);
    const corruptions = [
        value => { value.api_prompt.secret = "PRIVATE"; },
        value => { value.editable_workflow.secret = "PRIVATE"; },
        value => { value.api_prompt.schema = "wrong"; },
        value => { value.api_prompt.canonical_bytes = 0; },
        value => { value.editable_workflow.node_count = 10001; },
        value => { value.editable_workflow.workflow_id = "x".repeat(257); },
        value => { value.editable_workflow.revision = 1.5; },
        value => { value.graph_hash_schema = "wrong"; },
        value => { value.captured_at_ms = "1"; },
    ];
    for (const corrupt of corruptions) {
        const changed = structuredClone(provenance);
        corrupt(changed);
        assert.throws(() => validateExecutionProvenance(changed, operation));
    }
});


test("execution prompt identifiers are bounded and plaintext-shaped values are rejected", () => {
    assert.equal(validateExecutionPromptId("prompt-1234/ok:1"), "prompt-1234/ok:1");
    for (const value of ["PRIVATE PROMPT WITH SPACES", "x".repeat(129), "", null]) {
        assert.throws(() => validateExecutionPromptId(value));
    }
});
