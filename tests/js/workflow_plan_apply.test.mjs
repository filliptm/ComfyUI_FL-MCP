import assert from "node:assert/strict";
import test from "node:test";

import {
    applyWorkflowPlanAtomic,
    WORKFLOW_APPLICATION_SCHEMA,
} from "../../web/js/workflow_plan_apply.js";


const plan = {
    schema: "fl-mcp.workflow-plan.v1",
    catalog_hash: "c".repeat(64),
    nodes: [
        {
            alias: "blank",
            node_type: "EmptyImage",
            schema_hash: "a".repeat(64),
            values: { width: 512, height: 512, batch_size: 1, color: 0 },
        },
        {
            alias: "output",
            node_type: "SaveImage",
            schema_hash: "b".repeat(64),
            values: { filename_prefix: "atomic-e2e" },
        },
    ],
    connections: [
        {
            source_alias: "blank",
            source_output: "IMAGE",
            source_output_index: 0,
            source_type: "IMAGE",
            target_alias: "output",
            target_input: "images",
            target_type: "IMAGE",
        },
    ],
};


class FakeCanvasAdapter {
    constructor() {
        this.nodes = new Map();
        this.connections = [];
        this.nextId = 1;
        this.createCalls = 0;
        this.connectCalls = 0;
        this.failCreateAlias = null;
        this.failConnect = false;
        this.failAttachment = false;
    }

    findApplicationNodes(applicationId) {
        return [...this.nodes.values()]
            .filter(node => node.metadata?.application_id === applicationId)
            .map(node => ({
                node_id: node.id,
                node_type: node.node_type,
                metadata: node.metadata,
            }));
    }

    createNode(plannedNode) {
        this.createCalls += 1;
        if (plannedNode.alias === this.failCreateAlias) {
            throw new Error(`failed to create ${plannedNode.alias}`);
        }
        const id = this.nextId++;
        this.nodes.set(id, {
            id,
            node_type: plannedNode.node_type,
            values: structuredClone(plannedNode.values),
            metadata: null,
        });
        return { id, position: { x: id * 100, y: 0 }, size: { width: 200, height: 100 } };
    }

    setNodeMetadata(id, metadata) {
        this.nodes.get(id).metadata = structuredClone(metadata);
    }

    getNodeValues(id) {
        return structuredClone(this.nodes.get(id).values);
    }

    assignAttachment(id, binding) {
        if (this.failAttachment) throw new Error("attachment failed");
        const value = `${binding.image.subfolder}/${binding.image.filename}`;
        this.nodes.get(id).values[binding.input_name] = value;
        return { node_id: id, image: structuredClone(binding.image) };
    }

    connectNodes(sourceId, targetId, connection) {
        this.connectCalls += 1;
        if (this.failConnect) throw new Error("connection failed");
        const record = {
            sourceId,
            targetId,
            sourceOutputIndex: connection.source_output_index,
            targetInput: connection.target_input,
        };
        this.connections.push(record);
        return record;
    }

    connectionExists(sourceId, targetId, connection) {
        return this.connections.some(item => (
            item.sourceId === sourceId
            && item.targetId === targetId
            && item.sourceOutputIndex === connection.source_output_index
            && item.targetInput === connection.target_input
        ));
    }

    listConnections(nodeIds) {
        const selected = new Set(nodeIds);
        return this.connections
            .filter(item => selected.has(item.sourceId) || selected.has(item.targetId))
            .map(item => ({
                source_node_id: item.sourceId,
                source_output_index: item.sourceOutputIndex,
                target_node_id: item.targetId,
                target_input: item.targetInput,
            }));
    }

    removeNodes(ids) {
        const removed = new Set(ids);
        for (const id of removed) this.nodes.delete(id);
        this.connections = this.connections.filter(item => (
            !removed.has(item.sourceId) && !removed.has(item.targetId)
        ));
    }

    nodeExists(id) {
        return this.nodes.has(id);
    }
}


function request(applicationId = "atomic-test-0001") {
    return {
        plan,
        plan_hash: "d".repeat(64),
        application_id: applicationId,
    };
}


test("atomic apply creates values, exact connections, aliases, and metadata", () => {
    const adapter = new FakeCanvasAdapter();
    const result = applyWorkflowPlanAtomic(request(), adapter);

    assert.equal(result.success, true);
    assert.equal(result.applied, true);
    assert.equal(result.already_applied, false);
    assert.deepEqual(result.aliases, { blank: 1, output: 2 });
    assert.equal(result.node_count, 2);
    assert.equal(result.connection_count, 1);
    assert.equal(result.verification.valid, true);
    assert.equal(result.application_schema, WORKFLOW_APPLICATION_SCHEMA);
    assert.equal(adapter.nodes.get(1).values.width, 512);
    assert.equal(adapter.nodes.get(2).values.filename_prefix, "atomic-e2e");
    assert.equal(adapter.nodes.get(1).metadata.schema, WORKFLOW_APPLICATION_SCHEMA);
    assert.equal(result.queued, false);
});


test("retrying the same application is idempotent", () => {
    const adapter = new FakeCanvasAdapter();
    const first = applyWorkflowPlanAtomic(request(), adapter);
    const second = applyWorkflowPlanAtomic(request(), adapter);

    assert.equal(first.applied, true);
    assert.equal(second.success, true);
    assert.equal(second.applied, false);
    assert.equal(second.already_applied, true);
    assert.deepEqual(second.aliases, { blank: 1, output: 2 });
    assert.equal(adapter.createCalls, 2);
    assert.equal(adapter.connectCalls, 1);
    assert.equal(adapter.nodes.size, 2);
});


test("an application ID conflict fails closed without mutation", () => {
    const adapter = new FakeCanvasAdapter();
    applyWorkflowPlanAtomic(request(), adapter);
    adapter.nodes.get(2).values.filename_prefix = "manually-changed";

    const result = applyWorkflowPlanAtomic(request(), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "idempotency_conflict");
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.nodes.size, 2);
    assert.equal(adapter.createCalls, 2);
});


test("an application ID conflict rejects unexpected internal or external edges", () => {
    const adapter = new FakeCanvasAdapter();
    applyWorkflowPlanAtomic(request(), adapter);
    adapter.connections.push({
        sourceId: 1,
        targetId: 99,
        sourceOutputIndex: 0,
        targetInput: "images",
    });

    const result = applyWorkflowPlanAtomic(request(), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "idempotency_conflict");
    assert.ok(result.verification.issues.some(issue => (
        issue.code === "application_connection_unexpected"
    )));
    assert.equal(adapter.createCalls, 2);
});


test("a node creation failure removes every node created by the transaction", () => {
    const adapter = new FakeCanvasAdapter();
    adapter.failCreateAlias = "output";

    const result = applyWorkflowPlanAtomic(request("atomic-test-create-failure"), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "workflow_application_failed");
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(result.rollback.attempted_node_ids, [1]);
    assert.equal(adapter.nodes.size, 0);
});


test("a connection failure rolls back the entire created subgraph", () => {
    const adapter = new FakeCanvasAdapter();
    adapter.failConnect = true;

    const result = applyWorkflowPlanAtomic(request("atomic-test-connect-failure"), adapter);

    assert.equal(result.success, false);
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(result.rollback.attempted_node_ids, [1, 2]);
    assert.equal(adapter.nodes.size, 0);
    assert.equal(adapter.connections.length, 0);
});


test("validated chat attachments are assigned inside the atomic transaction", () => {
    const adapter = new FakeCanvasAdapter();
    const attachmentPlan = structuredClone(plan);
    attachmentPlan.nodes[0].values.image = "ren-chat/session/main.png";
    attachmentPlan.attachments = [{
        node_alias: "blank",
        input_name: "image",
        image: { filename: "main.png", subfolder: "ren-chat/session", type: "input" },
    }];

    const result = applyWorkflowPlanAtomic({
        ...request("atomic-test-attachment"),
        plan: attachmentPlan,
    }, adapter);

    assert.equal(result.success, true);
    assert.equal(result.attachment_count, 1);
    assert.equal(adapter.nodes.get(1).values.image, "ren-chat/session/main.png");
    assert.equal(result.verification.valid, true);
});


test("attachment assignment failure rolls back the complete subgraph", () => {
    const adapter = new FakeCanvasAdapter();
    adapter.failAttachment = true;
    const attachmentPlan = structuredClone(plan);
    attachmentPlan.nodes[0].values.image = "ren-chat/session/main.png";
    attachmentPlan.attachments = [{
        node_alias: "blank",
        input_name: "image",
        image: { filename: "main.png", subfolder: "ren-chat/session", type: "input" },
    }];

    const result = applyWorkflowPlanAtomic({
        ...request("atomic-test-attachment-failure"),
        plan: attachmentPlan,
    }, adapter);

    assert.equal(result.success, false);
    assert.equal(result.rollback.complete, true);
    assert.equal(adapter.nodes.size, 0);
});
