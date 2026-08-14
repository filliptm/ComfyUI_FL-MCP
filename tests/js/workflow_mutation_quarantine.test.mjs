import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const root = new URL("../../", import.meta.url);


async function loadToolExecutor() {
    let source = await readFile(new URL("web/js/tool_executor.js", root), "utf8");
    source = source.replace(
        /^import\s+[\s\S]*?\s+from\s+["'][^"']+["'];\s*/gm,
        "",
    );
    source = source.replace("export class ToolExecutor", "class ToolExecutor");
    source += [
        "",
        "ToolExecutor.__forTestReconnected = () => {",
        "    const executor = Object.create(ToolExecutor.prototype);",
        "    executor.workflowMutationQuarantine = workflowMutationQuarantine;",
        "    return executor;",
        "};",
        "globalThis.__ToolExecutor = ToolExecutor;",
        "",
    ].join("\n");
    const context = vm.createContext({
        console: { log() {}, warn() {}, error() {}, debug() {} },
        structuredClone,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        performance: { now: () => 0 },
    });
    vm.runInContext(source, context, { filename: "web/js/tool_executor.js" });
    return context.__ToolExecutor;
}


test("an incomplete GraphPatch rollback quarantines every later canvas mutation", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    let activeIdentity = "workflow-a";
    executor.workflowMutationQuarantine = null;
    executor.flApi = {
        getActiveWorkflowIdentity: () => activeIdentity,
    };
    executor._applyWorkflowGraphPatchSerialized = async () => ({
        success: false,
        applied: false,
        already_applied: false,
        application_id: "mask-attachment-v1",
        patch_hash: "a".repeat(64),
        rollback: {
            attempted: true,
            complete: false,
            snapshot_restored: false,
            hash_verified: false,
            errors: [],
        },
    });

    const failed = await executor._handleApplyWorkflowGraphPatch({
        plan: { expected_workflow_identity: "workflow-a" },
    });
    assert.equal(failed.workflow_state_compromised, true);
    assert.equal(failed.mutation_quarantined, true);
    assert.equal(failed.recovery, "reload_or_reopen_workflow");

    assert.throws(
        () => executor._assertWorkflowMutationAllowed("queue_workflow"),
        error => (
            error?.code === "workflow_state_compromised"
            && error?.details?.retryable === false
            && error?.details?.recovery === "reload_or_reopen_workflow"
        ),
    );

    activeIdentity = "workflow-b";
    assert.doesNotThrow(
        () => executor._assertWorkflowMutationAllowed("edit_node_mask"),
    );
    assert.equal(executor.workflowMutationQuarantine, null);
});


test("a clean failed GraphPatch does not quarantine the workflow", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    executor.workflowMutationQuarantine = null;
    executor.flApi = { getActiveWorkflowIdentity: () => "workflow-a" };
    executor._applyWorkflowGraphPatchSerialized = async () => ({
        success: false,
        applied: false,
        already_applied: false,
        rollback: {
            attempted: true,
            complete: true,
            snapshot_restored: true,
            hash_verified: true,
            errors: [],
        },
    });

    const failed = await executor._handleApplyWorkflowGraphPatch({
        plan: { expected_workflow_identity: "workflow-a" },
    });
    assert.equal(failed.workflow_state_compromised, undefined);
    assert.equal(executor.workflowMutationQuarantine, null);
    assert.doesNotThrow(
        () => executor._assertWorkflowMutationAllowed("queue_workflow"),
    );
});


test("exact prompt setter preserves typed node IDs and rejects missing widgets", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    executor.workflowMutationQuarantine = null;
    let setCalls = 0;
    let stringValue = "old-string";
    executor.flApi = {
        pauseAutoQueue: () => ({ enabled: true }),
        restoreAutoQueue: state => assert.deepEqual(state, { enabled: true }),
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => ({ expectedGraphHash: "a".repeat(64) }),
        getWorkflowNode: nodeId => {
            assert.equal(typeof nodeId, "string");
            assert.equal(nodeId, "1");
            return { node_id: nodeId, values: { value: stringValue } };
        },
        setValuesExact: async (nodeId, values) => {
            assert.equal(typeof nodeId, "string");
            stringValue = values.value;
            setCalls += 1;
            return { applied: ["value"] };
        },
        acceptWorkflowMutationGuard: async () => "b".repeat(64),
    };

    const result = await executor._handleSetNodeValuesExact({
        expected_workflow_identity: "workflow-a",
        expected_graph_hash: "a".repeat(64),
        node_id: "1",
        widget_name: "value",
        value: "new-string",
        expected_current_value: "old-string",
    });
    assert.equal(result.node_id, "1");
    assert.equal(stringValue, "new-string");
    assert.equal(setCalls, 1);

    executor.flApi.getWorkflowNode = () => ({ node_id: "1", values: {} });
    await assert.rejects(
        executor._handleSetNodeValuesExact({
            expected_workflow_identity: "workflow-a",
            expected_graph_hash: "a".repeat(64),
            node_id: "1",
            widget_name: "missing",
            value: "secret",
        }),
        /exact widget missing is unavailable/,
    );
    assert.equal(setCalls, 1);
});


test("exact image reader pins workflow and graph around canonical node projection", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    const calls = [];
    const guard = { expectedGraphHash: "a".repeat(64) };
    executor.flApi = {
        pinActiveWorkflow(identity) {
            calls.push(["pin", identity]);
            return { identity };
        },
        async createWorkflowMutationGuard(pin) {
            calls.push(["guard", pin.identity]);
            return guard;
        },
        getNodeImageRef(nodeId, pin) {
            calls.push(["read", nodeId, pin.identity]);
            return { node_id: 1, image: { filename: "source.png", type: "input" } };
        },
        async assertWorkflowMutationGuard(value) {
            calls.push(["verify", value.expectedGraphHash]);
        },
    };

    const result = await executor._handleGetNodeImageRef({
        node_id: 1,
        expected_workflow_identity: "workflow-a",
        expected_graph_hash: "a".repeat(64),
    });

    assert.equal(result.node_id, 1);
    assert.equal(result.workflow_identity, "workflow-a");
    assert.equal(result.graph_hash, "a".repeat(64));
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
        ["pin", "workflow-a"],
        ["guard", "workflow-a"],
        ["read", 1, "workflow-a"],
        ["verify", "a".repeat(64)],
    ]);
});


test("canvas image page reader pins one exact workflow and verifies after discovery", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    const calls = [];
    const guard = { expectedGraphHash: "a".repeat(64) };
    executor.flApi = {
        pinActiveWorkflow(identity) {
            calls.push(["pin", identity]);
            return { identity };
        },
        async createWorkflowMutationGuard(pin) {
            calls.push(["guard", pin.identity]);
            return guard;
        },
        getCanvasImageRefs(options, pin) {
            calls.push(["read", options, pin.identity]);
            return {
                images: [],
                total_count: 0,
                offset: options.offset,
                limit: options.limit,
                has_more: false,
                next_offset: null,
                deduplicated: true,
            };
        },
        async assertWorkflowMutationGuard(value) {
            calls.push(["verify", value.expectedGraphHash]);
        },
    };

    const result = await executor._handleGetCanvasImageRefs({
        node_ids: [1, "2"],
        offset: 3,
        limit: 4,
        expected_workflow_identity: "workflow-a",
        expected_graph_hash: "a".repeat(64),
    });

    assert.equal(result.success, true);
    assert.equal(result.workflow_identity, "workflow-a");
    assert.equal(result.graph_hash, "a".repeat(64));
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
        ["pin", "workflow-a"],
        ["guard", "workflow-a"],
        ["read", { nodeIds: [1, "2"], offset: 3, limit: 4 }, "workflow-a"],
        ["verify", "a".repeat(64)],
    ]);

    executor.flApi.assertWorkflowMutationGuard = async () => {
        throw Object.assign(new Error("changed"), { code: "concurrent_workflow_edit" });
    };
    await assert.rejects(
        () => executor._handleGetCanvasImageRefs({
            node_ids: null,
            offset: 0,
            limit: 8,
            expected_workflow_identity: "workflow-a",
            expected_graph_hash: "a".repeat(64),
        }),
        error => error?.code === "concurrent_workflow_edit",
    );
});


test("exact prompt setter preserves verified success when auto-queue restore fails", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    executor.workflowMutationQuarantine = null;
    let value = "old prompt";
    let pauseCalls = 0;
    let restoreCalls = 0;
    executor.flApi = {
        pauseAutoQueue: () => {
            pauseCalls += 1;
            return { kind: "queueSettings", mode: "instant" };
        },
        restoreAutoQueue: () => {
            restoreCalls += 1;
            throw new Error("auto-queue restore failed");
        },
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => ({ expectedGraphHash: "a".repeat(64) }),
        getWorkflowNode: nodeId => ({ node_id: nodeId, values: { value } }),
        setValuesExact: async (_nodeId, values) => {
            value = values.value;
            return { applied: ["value"] };
        },
        acceptWorkflowMutationGuard: async () => "b".repeat(64),
    };

    const result = await executor._handleSetNodeValuesExact({
        expected_workflow_identity: "workflow-a",
        expected_graph_hash: "a".repeat(64),
        node_id: "1",
        widget_name: "value",
        value: "new prompt",
        expected_current_value: "old prompt",
    });

    assert.equal(result.success, true);
    assert.equal(result.verified, true);
    assert.equal(result.queued, false);
    assert.equal(value, "new prompt");
    assert.equal(restoreCalls, 1);
    assert.equal(pauseCalls, 2);
    assert.deepEqual(Array.from(result.cleanup_warnings, item => ({ ...item })), [{
        phase: "restore_auto_queue",
        message: "auto-queue restore failed",
    }]);
});


test("mask edit rechecks graph hash inside the shared frontend mutation handler", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    let edited = false;
    executor.flApi = {
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => ({ expectedGraphHash: "b".repeat(64) }),
        editNodeMask: async () => {
            edited = true;
        },
    };
    await assert.rejects(
        executor._handleEditNodeMask({
            expected_workflow_identity: "workflow-a",
            expected_graph_hash: "a".repeat(64),
            expected_source_image: { filename: "source.png", subfolder: "", type: "input" },
            node_id: 7,
            regions: [],
        }),
        /graph changed after mask inspection/i,
    );
    assert.equal(edited, false);
});


test("mask edit forwards the exact source-byte attestation into the locked API call", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    const attestation = {
        sha256: "c".repeat(64),
        size_bytes: 123456,
        width: 4096,
        height: 2160,
    };
    let exactContext = null;
    executor.flApi = {
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => ({ expectedGraphHash: "a".repeat(64) }),
        getNodeImageRef: () => ({
            image: { filename: "source.png", subfolder: "", type: "input" },
        }),
        editNodeMask: async (...args) => {
            exactContext = args[4];
            return { success: true };
        },
    };

    await executor._handleEditNodeMask({
        expected_workflow_identity: "workflow-a",
        expected_graph_hash: "a".repeat(64),
        expected_source_image: { filename: "source.png", subfolder: "", type: "input" },
        expected_source_attestation: attestation,
        node_id: 7,
        regions: [],
        coordinate_space: "normalized",
        clear_existing: true,
    });

    assert.deepEqual(
        { ...exactContext.expectedSourceAttestation },
        attestation,
    );
});


test("failed explicit prompt rollback quarantines across reconnect and blocks queue", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    executor.workflowMutationQuarantine = null;
    executor.flApi = {
        pauseAutoQueue: () => ({ enabled: true }),
        restoreAutoQueue: () => {},
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => ({ expectedGraphHash: "b".repeat(64) }),
        getActiveWorkflowIdentity: () => "workflow-a",
    };
    await assert.rejects(
        executor._handleSetNodeValuesExact({
            expected_workflow_identity: "workflow-a",
            expected_graph_hash: "a".repeat(64),
            node_id: 1,
            widget_name: "value",
            value: "old",
            expected_current_value: "new",
            expected_result_graph_hash: "a".repeat(64),
            quarantine_on_failure: true,
        }),
        error => error?.code === "workflow_state_compromised",
    );

    const reconnected = ToolExecutor.__forTestReconnected();
    reconnected.flApi = { getActiveWorkflowIdentity: () => "workflow-a" };
    assert.throws(
        () => reconnected._assertWorkflowMutationAllowed("queue_workflow"),
        error => error?.code === "workflow_state_compromised",
    );
});


test("an incomplete rollback quarantine survives ToolExecutor reconstruction", async () => {
    const ToolExecutor = await loadToolExecutor();
    const failedExecutor = Object.create(ToolExecutor.prototype);
    failedExecutor.workflowMutationQuarantine = null;
    failedExecutor.flApi = { getActiveWorkflowIdentity: () => "workflow-reconnect" };
    failedExecutor._applyWorkflowGraphPatchSerialized = async () => ({
        success: false,
        applied: false,
        already_applied: false,
        application_id: "failed-before-reconnect",
        patch_hash: "b".repeat(64),
        rollback: {
            attempted: true,
            complete: false,
            snapshot_restored: false,
            hash_verified: false,
            errors: ["snapshot mismatch"],
        },
    });

    await failedExecutor._handleApplyWorkflowGraphPatch({
        plan: { expected_workflow_identity: "workflow-reconnect" },
    });

    const reconnected = ToolExecutor.__forTestReconnected();
    reconnected.flApi = { getActiveWorkflowIdentity: () => "workflow-reconnect" };
    assert.throws(
        () => reconnected._assertWorkflowMutationAllowed("queue_workflow"),
        error => error?.code === "workflow_state_compromised",
    );
});


test("an identity read failure cannot clear an incomplete rollback quarantine", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    executor.workflowMutationQuarantine = Object.freeze({
        workflow_identity: "workflow-unknown",
        application_id: "failed-identity-read",
        patch_hash: "c".repeat(64),
    });
    executor.flApi = {
        getActiveWorkflowIdentity: () => {
            throw new Error("workflow store temporarily unavailable");
        },
    };

    assert.throws(
        () => executor._assertWorkflowMutationAllowed("edit_node_mask"),
        error => error?.code === "workflow_state_compromised",
    );
    assert.equal(executor.workflowMutationQuarantine.workflow_identity, "workflow-unknown");
});
