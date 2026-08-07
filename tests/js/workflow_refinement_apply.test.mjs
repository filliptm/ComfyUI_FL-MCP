import assert from "node:assert/strict";
import test from "node:test";

import { REFINEMENT_LEDGER_KEY, workflowGraphHash } from "../../web/js/graph_precondition.js";
import {
    applyWorkflowRefinementAtomic,
    WORKFLOW_REFINEMENT_PROPERTY,
    WORKFLOW_REFINEMENT_SCHEMA,
} from "../../web/js/workflow_refinement_apply.js";

const EXPECTED_WORKFLOW_IDENTITY = "fl-mcp-workflow:test-session:1";


function typedId(value) {
    return `${typeof value}:${JSON.stringify(value)}`;
}


function sameId(left, right) {
    return typedId(left) === typedId(right);
}


function edge(id, sourceId, sourceSlot, targetId, targetSlot, type = "IMAGE") {
    return {
        id,
        source_node_id: sourceId,
        source_output_index: sourceSlot,
        source_output: "IMAGE",
        target_node_id: targetId,
        target_input_index: targetSlot,
        target_input: "image",
        type,
    };
}


function node(id, type, values = {}) {
    return {
        id,
        type,
        values: structuredClone(values),
        widgets_values: Object.values(structuredClone(values)),
        properties: {},
        pos: [id * 100, 0],
        size: [220, 120],
        mode: 0,
        flags: { pinned: false },
        title: `${type} test node`,
        color: "#223344",
        bgcolor: "#112233",
        boxcolor: "#334455",
        shape: "box",
        showAdvanced: false,
    };
}


function replaceWorkflow() {
    return {
        version: 0.4,
        last_node_id: 6,
        last_link_id: 13,
        nodes: [
            node(1, "Source"),
            node(2, "OldFirst"),
            node(3, "OldSecond"),
            node(4, "Sink"),
            node(5, "SiblingSource"),
            node(6, "SiblingSink"),
        ],
        links: [
            edge(10, 1, 0, 2, 0),
            edge(11, 2, 0, 3, 0),
            edge(12, 3, 0, 4, 0),
            edge(13, 5, 0, 6, 0),
        ],
        groups: [],
        config: {},
        extra: { ds: { scale: 0.8, offset: [20, -10] }, theme: "dark" },
    };
}


class FakeRefinementAdapter {
    constructor(workflow = replaceWorkflow()) {
        this.workflow = structuredClone(workflow);
        this.createCalls = 0;
        this.connectCalls = 0;
        this.disconnectCalls = 0;
        this.removeCalls = 0;
        this.restoreCalls = 0;
        this.readGuardCalls = 0;
        this.events = [];
        this.failConnectAfterMutation = false;
        this.sabotageSiblingOnRemove = false;
        this.sabotageSiblingFactsOnRemove = false;
        this.corruptRestore = false;
    }

    async withReadGuard(operation) {
        this.readGuardCalls += 1;
        return await operation();
    }

    captureWorkflow() {
        return structuredClone(this.workflow);
    }

    restoreWorkflow(snapshot) {
        this.restoreCalls += 1;
        this.workflow = structuredClone(snapshot);
        if (this.corruptRestore) this.workflow.extra.theme = "corrupted";
        return { restored: true };
    }

    getNode(nodeId) {
        const stored = this.workflow.nodes.find(item => sameId(item.id, nodeId));
        if (!stored) return null;
        return {
            id: stored.id,
            node_id: stored.id,
            type: stored.type,
            node_type: stored.type,
            values: structuredClone(stored.values || {}),
            properties: structuredClone(stored.properties || {}),
            position: { x: stored.pos[0], y: stored.pos[1] },
            size: { width: stored.size[0], height: stored.size[1] },
            serialized_node: structuredClone(stored),
        };
    }

    listConnections() {
        return structuredClone(this.workflow.links);
    }

    disconnectConnection(expected) {
        this.disconnectCalls += 1;
        const matches = this.workflow.links
            .map((connection, index) => ({ connection, index }))
            .filter(({ connection }) => (
                sameId(connection.source_node_id, expected.source_node_id)
                && connection.source_output_index === expected.source_output_index
                && sameId(connection.target_node_id, expected.target_node_id)
                && connection.target_input_index === expected.target_input_index
            ));
        if (matches.length !== 1) throw new Error(`disconnect found ${matches.length} edges`);
        this.workflow.links.splice(matches[0].index, 1);
        return { disconnected: true };
    }

    createNode(plannedNode) {
        this.createCalls += 1;
        const id = this.workflow.last_node_id + 1;
        this.workflow.last_node_id = id;
        this.workflow.nodes.push(node(id, plannedNode.node_type, plannedNode.values));
        return { id, node_id: id };
    }

    setNodeMetadata(nodeId, metadata) {
        const stored = this.workflow.nodes.find(item => sameId(item.id, nodeId));
        if (!stored) throw new Error("metadata node missing");
        stored.properties[WORKFLOW_REFINEMENT_PROPERTY] = structuredClone(metadata);
    }

    connectNodes(sourceId, targetId, connection) {
        this.connectCalls += 1;
        const occupied = this.workflow.links.some(link => (
            sameId(link.target_node_id, targetId)
            && link.target_input_index === connection.target_input_index
        ));
        if (occupied) throw new Error("target input is occupied");
        const id = this.workflow.last_link_id + 1;
        this.workflow.last_link_id = id;
        this.workflow.links.push({
            id,
            source_node_id: sourceId,
            source_output_index: connection.source_output_index,
            source_output: connection.source_output,
            target_node_id: targetId,
            target_input_index: connection.target_input_index,
            target_input: connection.target_input,
            type: connection.type,
        });
        if (this.failConnectAfterMutation) throw new Error("connection failed after mutation");
        return { connected: true, id };
    }

    removeNodes(nodeIds) {
        this.removeCalls += 1;
        const removed = new Set(nodeIds.map(typedId));
        if (this.sabotageSiblingOnRemove) removed.add(typedId(6));
        if (this.sabotageSiblingFactsOnRemove) {
            const sibling = this.workflow.nodes.find(item => sameId(item.id, 6));
            sibling.widgets_values = ["changed outside the refined path"];
            sibling.mode = 4;
            sibling.flags.pinned = true;
            sibling.title = "Changed title";
            sibling.color = "#ffffff";
            sibling.bgcolor = "#000000";
            sibling.boxcolor = "#ff0000";
            sibling.shape = "round";
            sibling.showAdvanced = true;
        }
        this.workflow.nodes = this.workflow.nodes.filter(item => !removed.has(typedId(item.id)));
        this.workflow.links = this.workflow.links.filter(connection => (
            !removed.has(typedId(connection.source_node_id))
            && !removed.has(typedId(connection.target_node_id))
        ));
        return { removed: removed.size };
    }

    setWorkflowExtra(key, value) {
        this.workflow.extra ||= {};
        this.workflow.extra[key] = structuredClone(value);
        return value;
    }

    async afterMutationStep(step) {
        this.events.push(structuredClone(step));
    }
}


function pathConnections(workflow) {
    return workflow.links.slice(0, 3).map(connection => ({
        source_node_id: connection.source_node_id,
        source_output_index: connection.source_output_index,
        source_output: connection.source_output,
        target_node_id: connection.target_node_id,
        target_input_index: connection.target_input_index,
        target_input: connection.target_input,
        type: connection.type,
    }));
}


async function replaceRequest(adapter, overrides = {}) {
    const workflow = adapter.captureWorkflow();
    return {
        application_id: "refinement-replace-0001",
        refinement_hash: "a".repeat(64),
        plan: {
            operation: "replace",
            expected_workflow_identity: EXPECTED_WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            expected_path: {
                nodes: [
                    { node_id: 2, node_type: "OldFirst" },
                    { node_id: 3, node_type: "OldSecond" },
                ],
                connections: pathConnections(workflow),
            },
            replacement: {
                nodes: [
                    {
                        alias: "new_first",
                        node_type: "NewFirst",
                        schema_hash: "b".repeat(64),
                        values: { strength: 0.75 },
                    },
                    {
                        alias: "new_second",
                        node_type: "NewSecond",
                        schema_hash: "c".repeat(64),
                        values: { mode: "precise" },
                    },
                ],
                connections: [{
                    source_alias: "new_first",
                    source_output_index: 0,
                    source_output: "IMAGE",
                    target_alias: "new_second",
                    target_input_index: 0,
                    target_input: "image",
                    type: "IMAGE",
                }],
                input: {
                    target_alias: "new_first",
                    target_input_index: 0,
                    target_input: "image",
                    type: "IMAGE",
                },
                output: {
                    source_alias: "new_second",
                    source_output_index: 0,
                    source_output: "IMAGE",
                    type: "IMAGE",
                },
            },
            ...overrides,
        },
    };
}


test("replace splices a strict chain sequentially and preserves siblings", async () => {
    const adapter = new FakeRefinementAdapter();
    const siblingBefore = structuredClone(adapter.workflow.links[3]);
    const result = await applyWorkflowRefinementAtomic(await replaceRequest(adapter), adapter);

    assert.equal(result.success, true);
    assert.equal(result.applied, true);
    assert.equal(result.already_applied, false);
    assert.equal(result.refinement_schema, WORKFLOW_REFINEMENT_SCHEMA);
    assert.deepEqual(result.aliases, { new_first: 7, new_second: 8 });
    assert.deepEqual(result.created_node_ids, [7, 8]);
    assert.deepEqual(result.removed_node_ids, [2, 3]);
    assert.equal(adapter.getNode(2), null);
    assert.equal(adapter.getNode(3), null);
    assert.equal(adapter.getNode(7).node_type, "NewFirst");
    assert.equal(adapter.getNode(8).node_type, "NewSecond");
    assert.deepEqual(
        adapter.workflow.links.find(link => link.id === 13),
        siblingBefore,
    );
    assert.deepEqual(
        adapter.events.map(event => event.phase),
        [
            "disconnect", "disconnect", "disconnect",
            "node", "node",
            "connection", "connection", "connection",
            "remove",
        ],
    );
    assert.equal(adapter.events.at(-1).phase, "remove");
    assert.equal(result.verification.valid, true);
    assert.equal(adapter.readGuardCalls, 2);
    assert.equal(result.queued, false);
    assert.equal(
        adapter.workflow.extra[REFINEMENT_LEDGER_KEY]
            .entries["refinement-replace-0001"].result_graph_hash,
        result.graph_hash,
    );
});


test("retrying the same refinement uses the graph-hash ledger without mutation", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = await replaceRequest(adapter);
    const first = await applyWorkflowRefinementAtomic(request, adapter);
    const counts = {
        create: adapter.createCalls,
        connect: adapter.connectCalls,
        disconnect: adapter.disconnectCalls,
        remove: adapter.removeCalls,
    };
    const second = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(first.success, true);
    assert.equal(second.success, true);
    assert.equal(second.applied, false);
    assert.equal(second.already_applied, true);
    assert.equal(second.graph_hash, first.graph_hash);
    assert.deepEqual(counts, {
        create: adapter.createCalls,
        connect: adapter.connectCalls,
        disconnect: adapter.disconnectCalls,
        remove: adapter.removeCalls,
    });
});


test("a reused refinement ID with a different hash fails closed", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = await replaceRequest(adapter);
    await applyWorkflowRefinementAtomic(request, adapter);
    const result = await applyWorkflowRefinementAtomic({
        ...request,
        refinement_hash: "f".repeat(64),
    }, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "refinement_idempotency_conflict");
    assert.equal(result.rollback.attempted, false);
});


test("graph hash mismatch fails before any canvas mutation", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = await replaceRequest(adapter, { expected_graph_hash: "f".repeat(64) });
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "graph_precondition_failed");
    assert.equal(result.rollback.attempted, false);
    assert.deepEqual(adapter.captureWorkflow(), before);
    assert.equal(adapter.disconnectCalls, 0);
    assert.equal(adapter.createCalls, 0);
});


test("missing exact workflow identity rejects a non-canonical refinement before mutation", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = await replaceRequest(adapter);
    delete request.plan.expected_workflow_identity;
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "invalid_refinement_payload");
    assert.equal(result.expected_workflow_identity, null);
    assert.equal(adapter.disconnectCalls, 0);
    assert.equal(adapter.createCalls, 0);
});


test("an undeclared branch on an internal node rejects a non-linear path", async () => {
    const workflow = replaceWorkflow();
    workflow.last_link_id = 14;
    workflow.links.push(edge(14, 2, 1, 6, 1));
    const adapter = new FakeRefinementAdapter(workflow);
    const request = await replaceRequest(adapter);
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "expected_path_mismatch");
    assert.ok(result.verification.issues.some(issue => (
        issue.code === "expected_path_not_strictly_linear"
    )));
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.disconnectCalls, 0);
});


test("insert replaces one exact boundary edge with a new chain", async () => {
    const workflow = replaceWorkflow();
    workflow.nodes = workflow.nodes.filter(item => ![2, 3].includes(item.id));
    workflow.links = [edge(12, 1, 0, 4, 0), edge(13, 5, 0, 6, 0)];
    const adapter = new FakeRefinementAdapter(workflow);
    const request = {
        application_id: "refinement-insert-0001",
        refinement_hash: "1".repeat(64),
        plan: {
            operation: "insert",
            expected_workflow_identity: EXPECTED_WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            expected_path: {
                nodes: [],
                connections: [structuredClone(workflow.links[0])],
            },
            replacement: {
                nodes: [{
                    alias: "inserted",
                    node_type: "InsertedNode",
                    schema_hash: "2".repeat(64),
                    values: { amount: 4 },
                }],
                connections: [],
                input: {
                    target_alias: "inserted",
                    target_input_index: 0,
                    target_input: "image",
                    type: "IMAGE",
                },
                output: {
                    source_alias: "inserted",
                    source_output_index: 0,
                    source_output: "IMAGE",
                    type: "IMAGE",
                },
            },
        },
    };
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(result.removed_node_ids, []);
    assert.equal(adapter.workflow.links.some(link => (
        sameId(link.source_node_id, 1) && sameId(link.target_node_id, 7)
    )), true);
    assert.equal(adapter.workflow.links.some(link => (
        sameId(link.source_node_id, 7) && sameId(link.target_node_id, 4)
    )), true);
    assert.equal(adapter.removeCalls, 0);
});


test("delete reconnects boundaries before removing obsolete nodes last", async () => {
    const adapter = new FakeRefinementAdapter();
    const workflow = adapter.captureWorkflow();
    const request = {
        application_id: "refinement-delete-0001",
        refinement_hash: "3".repeat(64),
        plan: {
            operation: "delete",
            expected_workflow_identity: EXPECTED_WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            expected_path: {
                nodes: [
                    { node_id: 2, node_type: "OldFirst" },
                    { node_id: 3, node_type: "OldSecond" },
                ],
                connections: pathConnections(workflow),
            },
        },
    };
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(result.created_node_ids, []);
    assert.deepEqual(result.removed_node_ids, [2, 3]);
    assert.equal(adapter.workflow.links.some(link => (
        sameId(link.source_node_id, 1) && sameId(link.target_node_id, 4)
    )), true);
    assert.deepEqual(
        adapter.events.map(event => event.phase),
        ["disconnect", "disconnect", "disconnect", "connection", "remove"],
    );
});


test("a post-mutation connection failure restores the complete snapshot and hash", async () => {
    const adapter = new FakeRefinementAdapter();
    adapter.failConnectAfterMutation = true;
    const before = adapter.captureWorkflow();
    const beforeHash = await workflowGraphHash(before);
    const result = await applyWorkflowRefinementAtomic(await replaceRequest(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, true);
    assert.equal(result.rollback.snapshot_restored, true);
    assert.equal(result.rollback.hash_verified, true);
    assert.equal(result.rollback.restored_graph_hash, beforeHash);
    assert.deepEqual(adapter.captureWorkflow(), before);
    assert.equal(adapter.restoreCalls, 1);
});


test("post-apply sibling damage fails exact verification and rolls back", async () => {
    const adapter = new FakeRefinementAdapter();
    adapter.sabotageSiblingOnRemove = true;
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(await replaceRequest(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "post_refinement_verification_failed");
    assert.ok(result.verification.issues.some(issue => (
        issue.code === "sibling_node_changed" || issue.code === "sibling_connection_changed"
    )));
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("serialized sibling widget and presentation changes fail verification and roll back", async () => {
    const adapter = new FakeRefinementAdapter();
    adapter.sabotageSiblingFactsOnRemove = true;
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(await replaceRequest(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "post_refinement_verification_failed");
    assert.ok(result.verification.issues.some(issue => issue.code === "sibling_node_changed"));
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("rollback reports incomplete when a snapshot cannot be restored exactly", async () => {
    const adapter = new FakeRefinementAdapter();
    adapter.failConnectAfterMutation = true;
    adapter.corruptRestore = true;
    const result = await applyWorkflowRefinementAtomic(await replaceRequest(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, false);
    assert.equal(result.rollback.snapshot_restored, false);
    assert.equal(result.rollback.hash_verified, false);
});
