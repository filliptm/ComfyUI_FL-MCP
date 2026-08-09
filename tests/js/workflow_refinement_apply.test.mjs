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


function comfySameId(left, right) {
    return String(left) === String(right);
}


function serializedComfyId(value) {
    const text = String(value);
    return /^-?\d+$/.test(text) ? Number(text) : value;
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
        inputs: [{ name: "image", type: "IMAGE", link: null }],
        outputs: [{ name: "IMAGE", type: "IMAGE", links: [] }],
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
        this.sabotageWorkflowMetadataOnConnect = false;
        this.sabotageWorkflowIdentityOnConnect = false;
        this.sabotageV1StableStateOnConnect = false;
        this.changeViewportOnConnect = false;
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
        if (this.workflow.state) this.workflow.state.lastNodeId = id;
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
        if (this.workflow.state) this.workflow.state.lastLinkId = id;
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
        if (this.sabotageWorkflowMetadataOnConnect) {
            this.workflow.groups.push({ title: "undeclared group" });
        }
        if (this.sabotageWorkflowIdentityOnConnect) {
            this.workflow.id = "silently-replaced-workflow";
            this.workflow.definitions = {
                subgraphs: [{ id: "undeclared" }],
                models: [],
            };
        }
        if (this.sabotageV1StableStateOnConnect) {
            this.workflow.state.lastGroupId += 1;
            this.workflow.state.lastRerouteId += 1;
        }
        if (this.changeViewportOnConnect) {
            this.workflow.extra.ds = { scale: 1.25, offset: [80, 40] };
        }
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


/**
 * ComfyUI 0.29 can serialize numeric node IDs while exposing the same IDs as
 * strings through live LiteGraph nodes and links. Keep that split deliberate
 * here so verification exercises the production identity boundary.
 */
class MixedComfyIdAdapter extends FakeRefinementAdapter {
    getNode(nodeId) {
        const stored = this.workflow.nodes.find(item => comfySameId(item.id, nodeId));
        if (!stored) return null;
        const liveId = String(stored.id);
        return {
            id: liveId,
            node_id: liveId,
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
        return this.workflow.links.map(connection => ({
            ...structuredClone(connection),
            source_node_id: String(connection.source_node_id),
            target_node_id: String(connection.target_node_id),
        }));
    }

    createNode(plannedNode) {
        this.createCalls += 1;
        const serializedId = this.workflow.last_node_id + 1;
        this.workflow.last_node_id = serializedId;
        if (this.workflow.state) this.workflow.state.lastNodeId = serializedId;
        this.workflow.nodes.push(node(serializedId, plannedNode.node_type, plannedNode.values));
        const liveId = String(serializedId);
        return { id: liveId, node_id: liveId };
    }

    setNodeMetadata(nodeId, metadata) {
        const stored = this.workflow.nodes.find(item => comfySameId(item.id, nodeId));
        if (!stored) throw new Error("metadata node missing");
        stored.properties[WORKFLOW_REFINEMENT_PROPERTY] = structuredClone(metadata);
    }

    connectNodes(sourceId, targetId, connection) {
        this.connectCalls += 1;
        const occupied = this.workflow.links.some(link => (
            comfySameId(link.target_node_id, targetId)
            && link.target_input_index === connection.target_input_index
        ));
        if (occupied) throw new Error("target input is occupied");
        const id = this.workflow.last_link_id + 1;
        this.workflow.last_link_id = id;
        if (this.workflow.state) this.workflow.state.lastLinkId = id;
        this.workflow.links.push({
            id,
            source_node_id: serializedComfyId(sourceId),
            source_output_index: connection.source_output_index,
            source_output: connection.source_output,
            target_node_id: serializedComfyId(targetId),
            target_input_index: connection.target_input_index,
            target_input: connection.target_input,
            type: connection.type,
        });
        return { connected: true, id };
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


function appendWorkflow() {
    return {
        id: "wavelet-refinement-workflow",
        version: 0.4,
        last_node_id: 61,
        last_link_id: 109,
        state: {
            lastNodeId: 61,
            lastLinkId: 109,
            lastGroupId: 4,
            lastRerouteId: 7,
        },
        definitions: { subgraphs: [], models: [] },
        reroutes: [],
        floatingLinks: [],
        nodes: [
            node(45, "Source"),
            node(46, "BranchProcessor"),
            node(47, "BranchProcessor"),
            node(48, "LoadImage", { image: "industrial-background.png" }),
            node(52, "GeminiNanoBanana2V2", { prompt: "preserve the subject" }),
            node(60, "PreviewImage"),
            node(61, "PreviewImage"),
        ],
        links: [
            edge(100, 45, 0, 46, 0),
            edge(101, 45, 0, 47, 0),
            edge(102, 46, 0, 48, 0),
            edge(103, 47, 0, 48, 1),
            edge(104, 46, 0, 52, 0),
            edge(105, 47, 0, 52, 1),
            edge(106, 48, 0, 60, 0),
            edge(107, 52, 0, 61, 0),
            edge(108, 48, 0, 61, 1),
            edge(109, 52, 0, 60, 1),
        ],
        groups: [],
        config: {},
        extra: { ds: { scale: 0.9, offset: [12, -8] }, theme: "dark" },
    };
}


function productionWaveletWorkflow() {
    return {
        id: "97d55d32-5cd4-435b-9b58-80625783b470",
        version: 0.4,
        revision: 0,
        last_node_id: 59,
        last_link_id: 49,
        nodes: [
            node(48, "LoadImage", { image: "industrial-background.png" }),
            node(49, "LoadImage", { image: "main-portrait.png" }),
            node(50, "GeminiNanoBanana2V2", { prompt: "lighting pass" }),
            node(53, "PreviewImage"),
            node(54, "PreviewImage"),
            node(51, "GeminiNanoBanana2V2", { prompt: "depth pass" }),
            node(52, "GeminiNanoBanana2V2", { prompt: "final pass" }),
        ],
        links: [
            edge(37, 51, 0, 52, 5),
            edge(38, 50, 0, 51, 5),
            edge(39, 49, 0, 50, 5),
            edge(40, 49, 0, 52, 6),
            edge(41, 49, 0, 51, 6),
            edge(42, 48, 0, 51, 7),
            edge(43, 48, 0, 50, 6),
            edge(44, 48, 0, 52, 7),
            edge(45, 50, 0, 53, 0),
            edge(46, 51, 0, 54, 0),
        ],
        groups: [],
        config: {},
        extra: { ds: { scale: 0.9, offset: [12, -8] }, theme: "dark" },
    };
}


async function waveletAppendRequest(adapter, overrides = {}) {
    return {
        application_id: "refinement-append-wavelet-0001",
        refinement_hash: "4".repeat(64),
        plan: {
            operation: "append",
            expected_workflow_identity: EXPECTED_WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            expected_path: { nodes: [], connections: [] },
            replacement: {
                nodes: [
                    {
                        alias: "wavelet_fix",
                        node_type: "WaveletColorFix",
                        schema_hash: "d42f74e5ea682c89c4c4a964decd4025e830401d830fabfe10ed781aa7ef9b8e",
                        values: { align_method: "wavelet" },
                    },
                    {
                        alias: "save_image",
                        node_type: "SaveImage",
                        schema_hash: "919918acfefb2f1b0b628d52527ef9554c86b658c0ec4ee6ff96ef8785d8310f",
                        values: { filename_prefix: "ren-wavelet-color-fix" },
                    },
                ],
                connections: [{
                    source_alias: "wavelet_fix",
                    source_output_index: 0,
                    source_output: "image",
                    target_alias: "save_image",
                    target_input_index: 0,
                    target_input: "images",
                    type: "IMAGE",
                }],
                input: null,
                primary_input: {
                    source_node_id: 52,
                    source_node_type: "GeminiNanoBanana2V2",
                    source_schema_hash: "6fa7d9dc1c847507a78f7937bfc98c0fb863b5f40b64754e2913ccb3373a04fc",
                    source_output_index: 0,
                    source_output: "IMAGE",
                    target_alias: "wavelet_fix",
                    target_input_index: 0,
                    target_input: "target_image",
                    type: "IMAGE",
                },
                side_inputs: [{
                    source_node_id: 48,
                    source_node_type: "LoadImage",
                    source_schema_hash: "a".repeat(64),
                    source_output_index: 0,
                    source_output: "IMAGE",
                    target_alias: "wavelet_fix",
                    target_input_index: 1,
                    target_input: "source_image",
                    type: "IMAGE",
                }],
                output: null,
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


test("append builds Wavelet fan-in plus terminal SaveImage and preserves source fan-out", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    const original = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(adapter),
        adapter,
    );

    assert.equal(result.success, true);
    assert.equal(result.operation, "append");
    assert.deepEqual(result.aliases, { wavelet_fix: 62, save_image: 63 });
    assert.deepEqual(result.created_node_ids, [62, 63]);
    assert.deepEqual(result.removed_node_ids, []);
    assert.equal(adapter.disconnectCalls, 0);
    assert.equal(adapter.removeCalls, 0);
    assert.equal(original.nodes.length, 7);
    assert.equal(original.links.length, 10);
    assert.equal(adapter.workflow.nodes.length, 9);
    assert.equal(adapter.workflow.links.length, 13);
    assert.deepEqual(adapter.workflow.nodes.slice(0, 7), original.nodes);
    assert.deepEqual(adapter.workflow.links.slice(0, 10), original.links);
    assert.deepEqual(
        adapter.workflow.links.slice(10).map(connection => ({
            source_node_id: connection.source_node_id,
            source_output_index: connection.source_output_index,
            source_output: connection.source_output,
            target_node_id: connection.target_node_id,
            target_input_index: connection.target_input_index,
            target_input: connection.target_input,
            type: connection.type,
        })),
        [
            {
                source_node_id: 52,
                source_output_index: 0,
                source_output: "IMAGE",
                target_node_id: 62,
                target_input_index: 0,
                target_input: "target_image",
                type: "IMAGE",
            },
            {
                source_node_id: 48,
                source_output_index: 0,
                source_output: "IMAGE",
                target_node_id: 62,
                target_input_index: 1,
                target_input: "source_image",
                type: "IMAGE",
            },
            {
                source_node_id: 62,
                source_output_index: 0,
                source_output: "image",
                target_node_id: 63,
                target_input_index: 0,
                target_input: "images",
                type: "IMAGE",
            },
        ],
    );
    assert.deepEqual(adapter.getNode(62).values, { align_method: "wavelet" });
    assert.deepEqual(
        adapter.getNode(63).values,
        { filename_prefix: "ren-wavelet-color-fix" },
    );
    assert.deepEqual(
        adapter.events.map(event => event.phase),
        ["node", "node", "connection", "connection", "connection"],
    );
    assert.equal(result.verification.valid, true);
    assert.equal(result.verification.preserved_sibling_connection_count, 10);
    assert.equal(result.queued, false);
});


test("append accepts numeric serialized IDs with string live LiteGraph IDs", async () => {
    const adapter = new MixedComfyIdAdapter(productionWaveletWorkflow());
    const original = adapter.captureWorkflow();
    const request = await waveletAppendRequest(adapter);
    request.application_id = "ren-append-wavelet-color-fix-save-20260808-v1";
    request.plan.replacement.nodes[1].values.filename_prefix = "ComfyUI";

    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(result.applied, true);
    assert.equal(result.operation, "append");
    assert.deepEqual(result.aliases, { wavelet_fix: "60", save_image: "61" });
    assert.deepEqual(result.created_node_ids, ["60", "61"]);
    assert.deepEqual(result.removed_node_ids, []);
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.restoreCalls, 0);
    assert.equal(original.nodes.length, 7);
    assert.equal(original.links.length, 10);
    assert.equal(adapter.workflow.nodes.length, 9);
    assert.equal(adapter.workflow.links.length, 13);
    assert.deepEqual(adapter.workflow.nodes.slice(0, 7), original.nodes);
    assert.deepEqual(adapter.workflow.links.slice(0, 10), original.links);
    assert.deepEqual(
        adapter.listConnections().slice(10).map(connection => ({
            source_node_id: connection.source_node_id,
            source_output_index: connection.source_output_index,
            source_output: connection.source_output,
            target_node_id: connection.target_node_id,
            target_input_index: connection.target_input_index,
            target_input: connection.target_input,
            type: connection.type,
        })),
        [
            {
                source_node_id: "52",
                source_output_index: 0,
                source_output: "IMAGE",
                target_node_id: "60",
                target_input_index: 0,
                target_input: "target_image",
                type: "IMAGE",
            },
            {
                source_node_id: "48",
                source_output_index: 0,
                source_output: "IMAGE",
                target_node_id: "60",
                target_input_index: 1,
                target_input: "source_image",
                type: "IMAGE",
            },
            {
                source_node_id: "60",
                source_output_index: 0,
                source_output: "image",
                target_node_id: "61",
                target_input_index: 0,
                target_input: "images",
                type: "IMAGE",
            },
        ],
    );
    assert.deepEqual(
        adapter.events.map(event => event.phase),
        ["node", "node", "connection", "connection", "connection"],
    );
    assert.equal(result.verification.valid, true);
    assert.equal(result.verification.connection_count, 13);
    assert.equal(result.verification.preserved_sibling_connection_count, 10);
    assert.equal(result.queued, false);
});


test("append rejects a multiply-assigned target socket before canvas mutation", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    const request = await waveletAppendRequest(adapter);
    request.plan.replacement.side_inputs[0].target_input_index = 0;
    request.plan.replacement.side_inputs[0].target_input = "target_image";
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "replacement_target_input_occupied");
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.createCalls, 0);
    assert.equal(adapter.connectCalls, 0);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("append rejects an unknown external source before canvas mutation", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    const request = await waveletAppendRequest(adapter);
    request.plan.replacement.side_inputs[0].source_node_id = 999;
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "external_input_mismatch");
    assert.ok(result.verification.issues.some(issue => issue.code === "external_source_missing"));
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.createCalls, 0);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("append rejects a stale external source slot before canvas mutation", async () => {
    const workflow = appendWorkflow();
    workflow.nodes.find(item => item.id === 48).outputs[0].name = "MASK";
    const adapter = new FakeRefinementAdapter(workflow);
    const request = await waveletAppendRequest(adapter);
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "external_input_mismatch");
    assert.ok(result.verification.issues.some(issue => (
        issue.code === "external_source_output_mismatch"
    )));
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.createCalls, 0);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("ordinary splice rejects a side input that would create a feedback cycle", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = await replaceRequest(adapter);
    request.plan.replacement.side_inputs = [{
        source_node_id: 4,
        source_node_type: "Sink",
        source_schema_hash: "9".repeat(64),
        source_output_index: 0,
        source_output: "IMAGE",
        target_alias: "new_first",
        target_input_index: 1,
        target_input: "reference",
        type: "IMAGE",
    }];
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "refinement_cycle");
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.disconnectCalls, 0);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("ordinary splice accepts an exact non-cyclic side input and preserves its fan-out", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = await replaceRequest(adapter);
    request.plan.replacement.side_inputs = [{
        source_node_id: 5,
        source_node_type: "SiblingSource",
        source_schema_hash: "8".repeat(64),
        source_output_index: 0,
        source_output: "IMAGE",
        target_alias: "new_second",
        target_input_index: 1,
        target_input: "reference",
        type: "IMAGE",
    }];
    const siblingBefore = structuredClone(adapter.workflow.links[3]);
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(adapter.workflow.links.find(link => link.id === 13), siblingBefore);
    assert.equal(adapter.workflow.links.some(link => (
        sameId(link.source_node_id, 5)
        && sameId(link.target_node_id, 8)
        && link.target_input_index === 1
        && link.target_input === "reference"
    )), true);
    assert.equal(result.verification.valid, true);
});


test("append connection failure rolls back the complete Wavelet graph", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    adapter.failConnectAfterMutation = true;
    const before = adapter.captureWorkflow();
    const beforeHash = await workflowGraphHash(before);
    const result = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(adapter),
        adapter,
    );

    assert.equal(result.success, false);
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, true);
    assert.equal(result.rollback.restored_graph_hash, beforeHash);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("append workflow metadata damage fails verification and restores the snapshot", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    adapter.sabotageWorkflowMetadataOnConnect = true;
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(adapter),
        adapter,
    );

    assert.equal(result.success, false);
    assert.equal(result.error.code, "post_refinement_verification_failed");
    assert.ok(result.verification.issues.some(issue => (
        issue.code === "workflow_metadata_changed"
        && issue.fields.includes("groups")
    )));
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("append rejects workflow identity and definitions changes and rolls back", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    adapter.sabotageWorkflowIdentityOnConnect = true;
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(adapter),
        adapter,
    );

    assert.equal(result.success, false);
    assert.equal(result.error.code, "post_refinement_verification_failed");
    const issue = result.verification.issues.find(candidate => (
        candidate.code === "workflow_metadata_changed"
    ));
    assert.deepEqual(issue.fields, ["definitions", "id"]);
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("v1 state permits node and link counters but protects group and reroute counters", async () => {
    const accepted = new FakeRefinementAdapter(appendWorkflow());
    const acceptedResult = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(accepted),
        accepted,
    );

    assert.equal(acceptedResult.success, true);
    assert.deepEqual(accepted.workflow.state, {
        lastNodeId: 63,
        lastLinkId: 112,
        lastGroupId: 4,
        lastRerouteId: 7,
    });

    const rejected = new FakeRefinementAdapter(appendWorkflow());
    rejected.sabotageV1StableStateOnConnect = true;
    const before = rejected.captureWorkflow();
    const rejectedResult = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(rejected),
        rejected,
    );
    assert.equal(rejectedResult.success, false);
    assert.equal(rejectedResult.error.code, "post_refinement_verification_failed");
    assert.ok(rejectedResult.verification.issues.some(issue => (
        issue.code === "workflow_metadata_changed"
        && issue.fields.includes("state")
    )));
    assert.equal(rejectedResult.rollback.complete, true);
    assert.deepEqual(rejected.captureWorkflow(), before);
});


test("append tolerates viewport-only extra.ds changes during visible construction", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    adapter.changeViewportOnConnect = true;
    const result = await applyWorkflowRefinementAtomic(
        await waveletAppendRequest(adapter),
        adapter,
    );

    assert.equal(result.success, true);
    assert.equal(result.verification.valid, true);
    assert.deepEqual(adapter.workflow.extra.ds, {
        scale: 1.25,
        offset: [80, 40],
    });
    assert.equal(
        adapter.workflow.extra[REFINEMENT_LEDGER_KEY]
            .entries["refinement-append-wavelet-0001"].result_graph_hash,
        result.graph_hash,
    );
});


test("append stale graph guard fails before creating Wavelet nodes", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    const request = await waveletAppendRequest(adapter, {
        expected_graph_hash: "f".repeat(64),
    });
    const before = adapter.captureWorkflow();
    const result = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "graph_precondition_failed");
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.createCalls, 0);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("retrying an append is idempotent and does not duplicate nodes or edges", async () => {
    const adapter = new FakeRefinementAdapter(appendWorkflow());
    const request = await waveletAppendRequest(adapter);
    const first = await applyWorkflowRefinementAtomic(request, adapter);
    const mutationCounts = {
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
    assert.deepEqual(mutationCounts, {
        create: adapter.createCalls,
        connect: adapter.connectCalls,
        disconnect: adapter.disconnectCalls,
        remove: adapter.removeCalls,
    });
    assert.equal(adapter.workflow.nodes.length, 9);
    assert.equal(adapter.workflow.links.length, 13);
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


test("prototype-named refinement IDs remain ordinary idempotency keys", async () => {
    const adapter = new FakeRefinementAdapter();
    const request = {
        ...await replaceRequest(adapter),
        application_id: "constructor",
    };

    const first = await applyWorkflowRefinementAtomic(request, adapter);
    const second = await applyWorkflowRefinementAtomic(request, adapter);

    assert.equal(first.success, true);
    assert.equal(first.applied, true);
    assert.equal(second.success, true);
    assert.equal(second.already_applied, true);
    assert.equal(Object.prototype.hasOwnProperty.call(
        adapter.workflow.extra[REFINEMENT_LEDGER_KEY].entries,
        "constructor",
    ), true);
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
