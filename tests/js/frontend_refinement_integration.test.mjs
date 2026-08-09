import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

import {
    applyWorkflowGraphPatchAtomic,
    WORKFLOW_GRAPH_PATCH_PROPERTY,
} from "../../web/js/workflow_graph_patch_apply.js";
import { workflowGraphHash } from "../../web/js/graph_precondition.js";
import { enrichGraphPatchNode } from "../../web/js/node_schema_contract.js";


const root = new URL("../../", import.meta.url);


async function loadBrowserClass(relativePath, className, globals = {}) {
    let source = await readFile(new URL(relativePath, root), "utf8");
    source = source.replace(
        /^import\s+[\s\S]*?\s+from\s+["'][^"']+["'];\s*/gm,
        "",
    );
    source = source.replace(`export class ${className}`, `class ${className}`);
    source += `\nglobalThis.__loadedClass = ${className};\n`;

    const quietConsole = {
        log() {},
        warn() {},
        error() {},
        debug() {},
    };
    const context = vm.createContext({
        console: quietConsole,
        structuredClone,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        ...globals,
    });
    vm.runInContext(source, context, { filename: relativePath });
    return { Class: context.__loadedClass, context };
}


function flApiHarness() {
    const originalWorkflow = {
        key: "workflow-a",
        changeTracker: {
            changeCount: 0,
            afterChangeCalls: 0,
            afterChange() { this.afterChangeCalls += 1; },
        },
    };
    const secondWorkflow = { key: "workflow-b" };
    const workflowStore = { activeWorkflow: originalWorkflow };
    let graphState = {
        marker: "raw",
        nodes: [],
        links: [],
        extra: {},
    };
    const graph = {
        _nodes: [],
        links: new Map(),
        extra: {},
        serialize: () => structuredClone(graphState),
        setDirtyCanvas() {},
        change() {},
        removeLink() {},
    };
    const canvas = {
        beforeCalls: 0,
        afterCalls: 0,
        read_only: false,
        emitBeforeChange() { this.beforeCalls += 1; },
        emitAfterChange() { this.afterCalls += 1; },
        setDirty() {},
    };
    const loadCalls = [];
    const app = {
        graph,
        canvas,
        extensionManager: { workflow: workflowStore },
        async loadGraphData(snapshot, clean, restoreView, workflow, options) {
            loadCalls.push({ snapshot, clean, restoreView, workflow, options });
            graphState = structuredClone(snapshot);
            workflowStore.activeWorkflow = workflow;
        },
        async graphToPrompt() {
            graphState = { ...graphState, marker: "after-graph-to-prompt" };
            return {
                output: { api: true },
                workflow: { marker: "queue-normalized", nodes: [], links: [], extra: {} },
            };
        },
    };
    const browserApi = { dispatchCustomEvent() {} };
    return {
        app,
        browserApi,
        canvas,
        graph,
        get graphState() { return graphState; },
        set graphState(value) { graphState = value; },
        loadCalls,
        originalWorkflow,
        secondWorkflow,
        workflowStore,
    };
}


async function loadFlApi(harness, globals = {}) {
    return loadBrowserClass("web/js/fl_api.js", "FL_API", {
        app: harness.app,
        api: harness.browserApi,
        LiteGraph: { createNode() { return null; } },
        GRAPH_PRECONDITION_SCHEMA: "test.graph.v1",
        workflowGraphHash: async workflow => `hash:${workflow.marker || "graph"}`,
        workflowGraphHashExcludingExtra: async workflow => `content-hash:${workflow.marker || "graph"}`,
        nodeIdsEqual: (left, right) => (
            typeof left === typeof right && JSON.stringify(left) === JSON.stringify(right)
        ),
        findNonOverlappingPosition: value => ({ x: value.x, y: value.y }),
        getGraphInsertionOrigin: () => ({ x: 0, y: 0 }),
        convertComfyWidgetToInput: async (node, widget) => {
            const input = { name: widget.name, type: widget.options?.input_type || "*" };
            node.inputs ||= [];
            node.inputs.push(input);
            return input;
        },
        formatImageWidgetRef: ref => (
            [ref.subfolder, ref.filename].filter(Boolean).join("/") + ` [${ref.type || "input"}]`
        ),
        parseImageWidgetRef: value => {
            if (typeof value !== "string") return null;
            const match = value.match(/^(.*?)(?:\s+\[(input|output|temp)\])?$/);
            const parts = (match?.[1] || "").split("/").filter(Boolean);
            const filename = parts.pop();
            return filename ? {
                filename,
                subfolder: parts.join("/"),
                type: match?.[2] || "input",
            } : null;
        },
        ...globals,
    });
}


test("ToolExecutor derives its advertised tools from the registered handler map", async () => {
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.toolHandlers = executor._registerHandlers();

    const supportedTools = executor.getSupportedTools();
    const contractRevisions = executor.getToolContractRevisions();
    assert.deepEqual(
        Array.from(supportedTools),
        Object.keys(executor.toolHandlers).sort(),
    );
    assert.deepEqual(
        Object.keys(contractRevisions),
        Object.keys(executor.toolHandlers).sort(),
    );
    assert.ok(supportedTools.includes("apply_workflow_graph_patch"));
    assert.equal(contractRevisions.apply_workflow_graph_patch, 2);
    assert.equal(contractRevisions.find_node, 1);
    assert.deepEqual(Array.from(supportedTools), [...supportedTools].sort());
});


function refinementParams(workflowIdentity) {
    return { plan: { expected_workflow_identity: workflowIdentity } };
}


test("refinement rollback restores the pinned workflow object and balances its transaction", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    flApi.restoreNestedImageReferences = () => {};
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const transaction = flApi.beginWorkflowChangeTransaction(pin);
    assert.equal(harness.canvas.read_only, true);
    const snapshot = flApi.captureWorkflowSnapshot(pin);

    harness.workflowStore.activeWorkflow = harness.secondWorkflow;
    assert.throws(
        () => flApi.captureWorkflowSnapshot(pin),
        /active workflow changed during refinement/,
    );

    const restored = await flApi.restoreWorkflowSnapshot(snapshot, pin);
    flApi.endWorkflowChangeTransaction(transaction);

    assert.equal(harness.loadCalls.length, 1);
    assert.equal(harness.loadCalls[0].workflow, harness.originalWorkflow);
    assert.notEqual(harness.loadCalls[0].workflow, null);
    assert.equal(harness.loadCalls[0].clean, false);
    assert.equal(harness.loadCalls[0].restoreView, false);
    assert.equal(harness.loadCalls[0].options.deferWarnings, true);
    assert.equal(harness.loadCalls[0].options.skipAssetScans, true);
    assert.equal(harness.loadCalls[0].options.silentAssetErrors, true);
    assert.equal(harness.workflowStore.activeWorkflow, harness.originalWorkflow);
    assert.equal(restored.workflow_identity_verified, true);
    assert.equal(harness.canvas.beforeCalls, 1);
    assert.equal(harness.canvas.afterCalls, 1);
    assert.equal(harness.canvas.read_only, false);
});


test("workflow identity is stable per object and distinguishes identical duplicate tabs", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const firstApi = new FL_API();
    const secondApi = new FL_API();
    const firstIdentity = firstApi.getActiveWorkflowIdentity();

    assert.equal(secondApi.getActiveWorkflowIdentity(), firstIdentity);
    assert.equal(firstApi.pinActiveWorkflow(firstIdentity).workflow, harness.originalWorkflow);

    const duplicateWorkflow = {
        ...harness.originalWorkflow,
        changeTracker: { ...harness.originalWorkflow.changeTracker },
    };
    harness.workflowStore.activeWorkflow = duplicateWorkflow;
    const duplicateIdentity = firstApi.getActiveWorkflowIdentity();

    assert.notEqual(duplicateIdentity, firstIdentity);
    assert.throws(
        () => firstApi.pinActiveWorkflow(firstIdentity),
        error => (
            error?.code === "workflow_identity_precondition_failed"
            && error?.details?.expected_workflow_identity === firstIdentity
            && error?.details?.actual_workflow_identity === duplicateIdentity
        ),
    );
    assert.equal(firstApi.pinActiveWorkflow(duplicateIdentity).workflow, duplicateWorkflow);
    assert.equal(harness.canvas.beforeCalls, 0);
});


test("same-workflow edits violate the mutation guard while accepted agent edits advance it", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const guard = await flApi.createWorkflowMutationGuard(pin);

    harness.graphState = { ...harness.graphState, marker: "agent-edit" };
    await flApi.acceptWorkflowMutationGuard(guard);
    assert.equal(await flApi.assertWorkflowMutationGuard(guard), "hash:agent-edit");

    harness.graphState = { ...harness.graphState, marker: "user-edit" };
    await assert.rejects(
        () => flApi.assertWorkflowMutationGuard(guard),
        error => (
            error?.code === "concurrent_workflow_edit"
            && error?.details?.expected_graph_hash === "hash:agent-edit"
            && error?.details?.actual_graph_hash === "hash:user-edit"
        ),
    );
});


test("inactive transaction cleanup never calls a private tracker afterChange or masks failure", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const transaction = flApi.beginWorkflowChangeTransaction(pin);
    harness.originalWorkflow.changeTracker.changeCount = 1;
    harness.originalWorkflow.changeTracker.afterChange = () => {
        throw new Error("inactive tracker must not be called");
    };
    harness.workflowStore.activeWorkflow = harness.secondWorkflow;

    const closed = flApi.endWorkflowChangeTransaction(transaction);

    assert.equal(closed.closed, false);
    assert.equal(closed.workflow_identity_verified, false);
    assert.equal(harness.originalWorkflow.changeTracker.changeCount, 0);
    assert.equal(harness.canvas.afterCalls, 0);
    assert.equal(harness.canvas.read_only, false);
    assert.equal(transaction.ended, true);
});


test("API-format workflow reads hash the editable graph rather than the queue-normalized copy", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const result = await flApi.getCurrentWorkflowJSON(true);

    assert.equal(result.workflow.marker, "queue-normalized");
    assert.equal(result.graph_hash, "hash:raw");
    assert.equal(result.workflow_identity, flApi.getActiveWorkflowIdentity());
    assert.equal(result.workflow_identity_schema, "fl-mcp.workflow-instance.v1");
    assert.equal(harness.graphState.marker, "after-graph-to-prompt");
    assert.deepEqual(JSON.parse(JSON.stringify(result.output)), { api: true });
});


test("Map-backed links are enumerated and exact refinement connects trust returned endpoints", async () => {
    const harness = flApiHarness();
    const source = {
        id: 1,
        type: "Source",
        comfyClass: "Source",
        outputs: [{ name: "IMAGE", type: "IMAGE", links: [] }],
    };
    const target = {
        id: 2,
        type: "Target",
        comfyClass: "Target",
        inputs: [{ name: "image", type: "IMAGE", link: null }],
    };
    harness.graph._nodes = [source, target];
    harness.graph.links = new Map([[
        9,
        { id: 9, origin_id: 1, origin_slot: 0, target_id: 2, target_slot: 0, type: "IMAGE" },
    ]]);
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());

    assert.equal(flApi.listWorkflowConnections(pin).length, 1);
    harness.graph.links = {
        9: { id: 9, origin_id: 1, origin_slot: 0, target_id: 2, target_slot: 0, type: "IMAGE" },
    };
    assert.equal(flApi.listWorkflowConnections(pin).length, 1);

    source.connect = () => null;
    assert.throws(
        () => flApi.connectWorkflowNodesExact(1, 2, {
            source_output_index: 0,
            target_input_index: 0,
        }, pin),
        /LiteGraph rejected exact connection/,
    );

    source.connect = () => ({
        id: 10,
        origin_id: 1,
        origin_slot: 0,
        target_id: 2,
        target_slot: 1,
        type: "IMAGE",
    });
    assert.throws(
        () => flApi.connectWorkflowNodesExact(1, 2, {
            source_output_index: 0,
            target_input_index: 0,
        }, pin),
        /LiteGraph redirected exact connection/,
    );

    source.connect = () => ({
        id: 11,
        origin_id: 1,
        origin_slot: 0,
        target_id: 2,
        target_slot: 0,
        type: "IMAGE",
    });
    const connected = flApi.connectWorkflowNodesExact(1, 2, {
        source_output_index: 0,
        target_input_index: 0,
    }, pin);
    assert.equal(connected.id, 11);
    assert.equal(connected.source_output, "IMAGE");
    assert.equal(connected.target_input, "image");

    assert.throws(
        () => flApi.connectWorkflowNodesExact(1, 2, {
            source_output_index: 0.5,
            target_input_index: 0,
        }, pin),
        /Invalid exact source output index/,
    );
});


test("FL_API exact values defer absent dotted widgets until their selector materializes", async () => {
    const harness = flApiHarness();
    const selector = {
        name: "model",
        type: "combo",
        value: "basic",
        callback(value, _canvas, node) {
            if (value === "advanced" && !node.widgets.some(item => item.name === "model.detail")) {
                node.widgets.push({ name: "model.detail", type: "number", value: 0 });
            }
        },
    };
    const node = {
        id: 1,
        type: "DynamicNode",
        comfyClass: "DynamicNode",
        widgets: [selector],
        inputs: [],
        outputs: [],
    };
    harness.graph._nodes = [node];
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();

    const deferred = await flApi.setValuesExact(1, { "model.detail": 7 });
    assert.deepEqual(Array.from(deferred.applied), []);

    const applied = await flApi.setValuesExact(1, {
        "model.detail": 7,
        model: "advanced",
    });

    assert.deepEqual(new Set(applied.applied), new Set(["model", "model.detail"]));
    assert.equal(node.widgets.find(item => item.name === "model.detail").value, 7);
});


test("FL_API attachment assignment verifies string refs without changing node properties", async () => {
    const harness = flApiHarness();
    const imageWidget = {
        name: "image",
        type: "combo",
        value: "old.png [input]",
        options: { values: ["old.png [input]"] },
    };
    const node = {
        id: 2,
        type: "LoadImage",
        comfyClass: "LoadImage",
        widgets: [imageWidget],
        widgets_values: [imageWidget.value],
        inputs: [],
        outputs: [],
        properties: { preserve: "exactly" },
    };
    harness.graph._nodes = [node];
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const attachment = {
        input: "image",
        filename: "approved.png",
        subfolder: "ren-chat",
        file_type: "output",
    };

    const assigned = flApi.assignAttachmentExact(2, attachment);

    assert.equal(assigned.value, "ren-chat/approved.png [output]");
    assert.equal(imageWidget.value, assigned.value);
    assert.equal(node.widgets_values[0], assigned.value);
    assert.equal(flApi.verifyAttachmentExact(2, attachment), true);
    assert.deepEqual(node.properties, { preserve: "exactly" });
});


test("FL_API widget conversion accepts modern coexisting sockets and supported core conversion", async () => {
    const harness = flApiHarness();
    let coreConversions = 0;
    const existingWidget = {
        name: "frame_rate",
        type: "number",
        value: 24,
        options: { input_type: "FLOAT" },
    };
    const existing = {
        id: 3,
        type: "VHS_VideoCombine",
        comfyClass: "VHS_VideoCombine",
        widgets: [existingWidget],
        inputs: [{ name: "frame_rate", type: "FLOAT", widget: { name: "frame_rate" } }],
        outputs: [],
        convertWidgetToInput() { throw new Error("deprecated conversion must not run"); },
    };
    const convertibleWidget = {
        name: "strength",
        type: "number",
        value: 1,
        options: { input_type: "FLOAT" },
    };
    const convertible = {
        id: 4,
        type: "LegacyConvertible",
        comfyClass: "LegacyConvertible",
        widgets: [convertibleWidget],
        inputs: [],
        outputs: [],
    };
    harness.graph._nodes = [existing, convertible];
    const { Class: FL_API } = await loadFlApi(harness, {
        convertComfyWidgetToInput: async (node, widget) => {
            coreConversions += 1;
            const input = { name: widget.name, type: widget.options.input_type };
            node.inputs.push(input);
            return input;
        },
    });
    const flApi = new FL_API();

    const coexistence = await flApi.convertWidgetToInputExact(3, {
        input: "frame_rate",
        type: "FLOAT",
        occurrence_index: 0,
    });
    const converted = await flApi.convertWidgetToInputExact(4, {
        input: "strength",
        type: "FLOAT",
        occurrence_index: 0,
    });

    assert.equal(coexistence.socket_index, 0);
    assert.equal(converted.socket_index, 0);
    assert.equal(coreConversions, 1);
});


test("tool executor pins every refinement adapter call and closes transactions on failure", async () => {
    const calls = [];
    const workflowIdentity = "fl-mcp-workflow:test-session:1";
    const pin = { workflow: { key: "workflow-a" } };
    const mutationGuard = { pin, expectedGraphHash: "hash:raw" };
    const flApi = {
        pauseAutoQueue() { calls.push("pause"); return { enabled: true }; },
        restoreAutoQueue() { calls.push("auto-restored"); },
        pinActiveWorkflow(expected) {
            assert.equal(expected, workflowIdentity);
            calls.push("pin");
            return pin;
        },
        beginWorkflowChangeTransaction(received) {
            assert.equal(received, pin);
            calls.push("begin");
            return { pin, ended: false };
        },
        endWorkflowChangeTransaction() { calls.push("end"); },
        createWorkflowMutationGuard(received) {
            assert.equal(received, pin);
            calls.push("guard-start");
            return mutationGuard;
        },
        assertWorkflowMutationGuard(received) {
            assert.equal(received, mutationGuard);
            calls.push("hash-check");
        },
        acceptWorkflowMutationGuard(received) {
            assert.equal(received, mutationGuard);
            calls.push("hash-accept");
        },
        captureWorkflowSnapshot(received) { assert.equal(received, pin); calls.push("capture"); return {}; },
        restoreWorkflowSnapshot(_snapshot, received) { assert.equal(received, pin); calls.push("rollback"); },
        getWorkflowNode(_id, received) { assert.equal(received, pin); calls.push("node"); return null; },
        listWorkflowConnections(received) { assert.equal(received, pin); calls.push("links"); return []; },
        create() { calls.push("create"); return { id: 7 }; },
        setValues() { calls.push("values"); },
        setNodeProperty() { calls.push("metadata"); },
        disconnectWorkflowConnection(_edge, received) {
            assert.equal(received, pin);
            calls.push("disconnect");
        },
        connectWorkflowNodesExact(_source, _target, _connection, received) {
            assert.equal(received, pin);
            calls.push("connect");
        },
        remove() { calls.push("remove"); },
        setWorkflowExtra(_key, _value, received) {
            assert.equal(received, pin);
            calls.push("extra");
        },
    };
    let fail = false;
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async (_params, adapter) => {
                await adapter.captureWorkflow();
                await adapter.createNode({ node_type: "TestNode", values: { value: 1 } });
                await adapter.withReadGuard(async () => {
                    await adapter.getNode(7);
                    await adapter.listConnections();
                });
                await adapter.disconnectConnection({});
                await adapter.connectNodes(1, 2, {
                    source_output_index: 0,
                    target_input_index: 0,
                });
                await adapter.setNodeMetadata(7, {});
                await adapter.removeNodes([1]);
                await adapter.setWorkflowExtra("ledger", {});
                await adapter.afterMutationStep({ phase: "node" });
                if (fail) throw new Error("forced failure");
                return { success: true };
            },
            setTimeout: callback => { callback(); return 1; },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;

    const params = refinementParams(workflowIdentity);
    const result = await executor._handleApplyWorkflowRefinement(params);
    assert.equal(result.success, true);
    assert.deepEqual(calls, [
        "pin", "pause", "begin", "guard-start",
        "hash-check", "capture", "hash-check",
        "hash-check", "create", "values", "hash-accept",
        "hash-check", "node", "links", "hash-check",
        "hash-check", "disconnect", "hash-accept",
        "hash-check", "connect", "hash-accept",
        "hash-check", "metadata", "hash-accept",
        "hash-check", "remove", "hash-accept",
        "hash-check", "extra", "hash-accept",
        "hash-check", "hash-check", "end", "auto-restored",
    ]);

    calls.length = 0;
    fail = true;
    await assert.rejects(
        () => executor._handleApplyWorkflowRefinement(params),
        /forced failure/,
    );
    assert.equal(calls.at(-2), "end");
    assert.equal(calls.at(-1), "auto-restored");
});


test("tool executor registers and routes GraphPatch through the guarded real adapter", async () => {
    const calls = [];
    let failEndCleanup = false;
    let failRestoreCleanup = false;
    let failEngine = false;
    const workflowIdentity = "fl-mcp-workflow:graph-patch-session:1";
    const pin = { workflow: { key: "graph-patch-workflow" } };
    const guard = { pin, expectedGraphHash: "hash:graph-patch" };
    const childContext = { key: "new:child", node_type: "Child", schema_hash: "a".repeat(64) };
    const flApi = {
        pinActiveWorkflow(expected) {
            assert.equal(expected, workflowIdentity);
            calls.push("pin");
            return pin;
        },
        async getNodeDefinitions(nodeTypes) {
            assert.deepEqual(Array.from(nodeTypes), ["Child"]);
            calls.push("catalog");
            return { Child: { input: {}, output: [] } };
        },
        assertActiveWorkflow(received) { assert.equal(received, pin); calls.push("identity"); },
        pauseAutoQueue() { calls.push("pause"); return { mode: "disabled" }; },
        restoreAutoQueue() {
            calls.push("auto-restored");
            if (failRestoreCleanup) throw new Error("auto-queue cleanup failed");
        },
        beginWorkflowChangeTransaction(received) {
            assert.equal(received, pin);
            calls.push("begin");
            return { pin };
        },
        endWorkflowChangeTransaction() {
            calls.push("end");
            if (failEndCleanup) throw new Error("transaction cleanup failed");
        },
        createWorkflowMutationGuard(received) {
            assert.equal(received, pin);
            calls.push("guard-start");
            return guard;
        },
        assertWorkflowMutationGuard(received) {
            assert.equal(received, guard);
            calls.push("hash-check");
        },
        acceptWorkflowMutationGuard(received) {
            assert.equal(received, guard);
            calls.push("hash-accept");
        },
        captureWorkflowSnapshot(received) {
            assert.equal(received, pin);
            calls.push("capture");
            return { nodes: [], links: [], extra: {} };
        },
        restoreWorkflowSnapshot(_snapshot, received) {
            assert.equal(received, pin);
            calls.push("restore");
        },
        getWorkflowNode(nodeId, received) {
            assert.equal(received, pin);
            calls.push(`node:${nodeId}`);
            return { id: nodeId, node_id: nodeId, node_type: "Child", type: "Child" };
        },
        listWorkflowConnections(received) {
            assert.equal(received, pin);
            calls.push("links");
            return [];
        },
        create(nodeType) { assert.equal(nodeType, "Child"); calls.push("create"); return { id: 7 }; },
        setValuesExact(nodeId, values) {
            assert.equal(nodeId, 7);
            assert.deepEqual(values, { value: 3 });
            calls.push("values");
            return { applied: ["value"] };
        },
        setNodeProperty() { calls.push("metadata"); },
        setRect() { calls.push("layout"); },
        assignAttachmentExact() { calls.push("attachment"); },
        verifyAttachmentExact() { calls.push("attachment-verify"); return true; },
        convertWidgetToInputExact() { calls.push("convert"); return { socket_index: 0 }; },
        disconnectWorkflowConnection() { calls.push("disconnect"); },
        connectWorkflowNodesExact() { calls.push("connect"); },
        remove() { calls.push("remove"); },
        setWorkflowExtra(_key, _value, received) {
            assert.equal(received, pin);
            calls.push("extra");
        },
    };
    const params = {
        schema_contracts: { Child: { schema_hash: "a".repeat(64), schema: {} } },
        plan: {
            expected_workflow_identity: workflowIdentity,
            assertions: { nodes: [], edges: [] },
            create_nodes: [{ alias: "child", node_type: "Child" }],
            update_nodes: [],
            remove_nodes: [],
        },
    };
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            WORKFLOW_GRAPH_PATCH_PROPERTY: "graph-patch",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async () => ({}),
            buildGraphPatchSchemaContexts: async (plan, catalog, contracts) => {
                assert.equal(plan, params.plan);
                assert.deepEqual(catalog, { Child: { input: {}, output: [] } });
                assert.equal(contracts, params.schema_contracts);
                calls.push("schema-context");
                return new Map([["new:child", childContext]]);
            },
            enrichGraphPatchNode: (node, context) => {
                assert.equal(context, childContext);
                calls.push("enrich");
                return { ...node, schema_hash: context.schema_hash };
            },
            applyWorkflowGraphPatchAtomic: async (received, adapter) => {
                assert.equal(received, params);
                calls.push("engine");
                if (failEngine) throw new Error("graph patch engine failed");
                assert.equal(typeof adapter.restoreWorkflow, "function");
                assert.equal(typeof adapter.disconnectConnection, "function");
                assert.equal(typeof adapter.connectNodes, "function");
                assert.equal(typeof adapter.assignAttachmentExact, "function");
                assert.equal(typeof adapter.verifyAttachmentExact, "function");
                assert.equal(typeof adapter.convertWidgetToInput, "function");
                await adapter.captureWorkflow();
                const created = await adapter.createNode({ alias: "child", node_type: "Child" });
                assert.equal(created.id, 7);
                const observed = await adapter.getNode(7);
                assert.equal(observed.schema_hash, childContext.schema_hash);
                await adapter.setNodeValuesExact(7, { value: 3 });
                await adapter.setNodeMetadata(7, {});
                await adapter.setNodeLayoutExact(7, { x: 1, y: 2 });
                await adapter.afterMutationStep({ phase: "node", delay_ms: 0 });
                return { success: true, queued: false };
            },
            setTimeout: callback => { callback(); return 1; },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;
    executor.queryExecutor = {};
    executor.toolHandlers = executor._registerHandlers();

    const result = await executor.toolHandlers.apply_workflow_graph_patch(params);

    assert.deepEqual(result, { success: true, queued: false });
    assert.ok(calls.indexOf("catalog") < calls.indexOf("pause"));
    assert.ok(calls.includes("schema-context"));
    assert.ok(calls.includes("engine"));
    assert.equal(calls.at(-2), "end");
    assert.equal(calls.at(-1), "auto-restored");

    calls.length = 0;
    failEndCleanup = true;
    failRestoreCleanup = true;
    const committedWithCleanupWarnings = await executor.toolHandlers.apply_workflow_graph_patch(params);
    assert.equal(committedWithCleanupWarnings.success, true);
    assert.deepEqual(
        Array.from(committedWithCleanupWarnings.cleanup_warnings, item => ({ ...item })),
        [
            {
                phase: "end_workflow_change_transaction",
                message: "transaction cleanup failed",
            },
            {
                phase: "restore_auto_queue",
                message: "auto-queue cleanup failed",
            },
        ],
    );
    assert.equal(calls.at(-2), "end");
    assert.equal(calls.at(-1), "auto-restored");

    calls.length = 0;
    failEngine = true;
    await assert.rejects(
        () => executor.toolHandlers.apply_workflow_graph_patch(params),
        error => {
            assert.equal(error.message, "graph patch engine failed");
            assert.deepEqual(
                Array.from(error.cleanup_warnings, item => ({ ...item })),
                [
                    {
                        phase: "end_workflow_change_transaction",
                        message: "transaction cleanup failed",
                    },
                    {
                        phase: "restore_auto_queue",
                        message: "auto-queue cleanup failed",
                    },
                ],
            );
            return true;
        },
    );
    assert.equal(calls.at(-2), "end");
    assert.equal(calls.at(-1), "auto-restored");
});


test("the guarded ToolExecutor adapter lets an edge unlock a deferred dynamic-slot value", async () => {
    const workflowIdentity = "fl-mcp-workflow:dynamic-slot-adapter:1";
    const catalogHash = "c".repeat(64);
    const sourceSchema = "1".repeat(64);
    const dynamicSchema = "2".repeat(64);
    let workflow = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const calls = [];
    const pin = { workflow: { key: "dynamic-slot-workflow" } };
    const guard = { pin };
    const clone = value => structuredClone(value);
    const findNode = id => workflow.nodes.find(node => String(node.id) === String(id));
    const sourceNode = id => ({
        id,
        type: "Source",
        schema_hash: sourceSchema,
        inputs: [],
        schema_inputs: [],
        outputs: [{ name: "IMAGE", type: "IMAGE", index: 0, links: [] }],
        widgets: [],
        values: {},
        widgets_values: [],
        properties: {},
        pos: [0, 0],
        size: [240, 140],
        mode: 0,
        flags: {},
    });
    const dynamicNode = id => ({
        id,
        type: "DynamicSlotValues",
        schema_hash: dynamicSchema,
        inputs: [{
            name: "unrelated_video",
            type: "VIDEO",
            index: 9,
            schema_index: 9,
            socket_index: 0,
            link: null,
        }, {
            name: "payload",
            type: "*",
            index: 0,
            schema_index: 0,
            socket_index: 1,
            link: null,
        }],
        schema_inputs: [
            {
                name: "payload",
                type: "COMFY_DYNAMICSLOT_V3",
                resolved_type: "IMAGE",
                accepted_types: ["IMAGE"],
                index: 0,
                kind: "socket",
            },
            { name: "payload.strength", type: "FLOAT", index: 1, kind: "widget" },
        ],
        outputs: [],
        widgets: [],
        values: {},
        widgets_values: [],
        properties: {},
        pos: [300, 0],
        size: [240, 140],
        mode: 0,
        flags: {},
        dynamic_input_roots: ["payload"],
    });
    const observedNode = id => {
        const node = findNode(id);
        return node ? {
            ...clone(node),
            node_id: node.id,
            node_type: node.type,
            position: { x: node.pos[0], y: node.pos[1] },
            size: { width: node.size[0], height: node.size[1] },
            live_inputs: clone(node.inputs),
            serialized_node: clone(node),
        } : null;
    };
    const flApi = {
        pinActiveWorkflow(expected) {
            assert.equal(expected, workflowIdentity);
            return pin;
        },
        async getNodeDefinitions(types) {
            return Object.fromEntries(types.map(type => [type, { input: {}, output: [] }]));
        },
        assertActiveWorkflow(received) { assert.equal(received, pin); },
        pauseAutoQueue() { return { disabled: true }; },
        restoreAutoQueue() {},
        beginWorkflowChangeTransaction() { return { pin }; },
        endWorkflowChangeTransaction() {},
        createWorkflowMutationGuard() { return guard; },
        assertWorkflowMutationGuard() {},
        acceptWorkflowMutationGuard() {},
        captureWorkflowSnapshot() { return clone(workflow); },
        restoreWorkflowSnapshot(snapshot) { workflow = clone(snapshot); },
        getWorkflowNode(id) { return observedNode(id); },
        listWorkflowConnections() { return clone(workflow.links); },
        create(type) {
            const id = workflow.last_node_id + 1;
            workflow.last_node_id = id;
            workflow.nodes.push(type === "Source" ? sourceNode(id) : dynamicNode(id));
            calls.push(`create:${type}`);
            return { id };
        },
        setValuesExact(id, values) {
            const node = findNode(id);
            const applied = [];
            for (const [name, value] of Object.entries(values)) {
                const target = node.widgets.find(widget => widget.name === name);
                if (!target) continue;
                target.value = clone(value);
                node.values[name] = clone(value);
                applied.push(name);
            }
            if (node.type === "DynamicSlotValues" && applied.length > 0) {
                node.size = [390, 260];
            }
            node.widgets_values = Object.values(clone(node.values));
            calls.push(`values:${applied.length ? applied.join(",") : "deferred"}`);
            return { applied };
        },
        setNodeProperty(id, key, value) { findNode(id).properties[key] = clone(value); },
        setRect(id, layout) {
            const node = findNode(id);
            node.pos = [layout.x, layout.y];
            if (layout.width !== undefined) node.size[0] = layout.width;
            if (layout.height !== undefined) node.size[1] = layout.height;
        },
        connectWorkflowNodesExact(sourceId, targetId, connection) {
            const id = workflow.last_link_id + 1;
            workflow.last_link_id = id;
            workflow.links.push({ id, ...clone(connection) });
            const target = findNode(targetId);
            target.widgets.push({
                name: "payload.strength",
                type: "FLOAT",
                schema_index: 1,
                value: 0.5,
            });
            target.values["payload.strength"] = 0.5;
            target.widgets_values = Object.values(clone(target.values));
            target.size = [360, 230];
            calls.push(`connect:${sourceId}->${targetId}`);
            return { id };
        },
        disconnectWorkflowConnection() {},
        assignAttachmentExact() {},
        verifyAttachmentExact() { return true; },
        convertWidgetToInputExact() { return { socket_index: 0 }; },
        remove(ids) {
            const removed = new Set(ids.map(String));
            workflow.nodes = workflow.nodes.filter(node => !removed.has(String(node.id)));
        },
        setWorkflowExtra(key, value) { workflow.extra[key] = clone(value); },
    };
    const request = {
        application_id: "dynamic-slot-real-adapter-test",
        expected_catalog_hash: catalogHash,
        patch_hash: "d".repeat(64),
        schema_contracts: {
            Source: { schema_hash: sourceSchema, schema: {} },
            DynamicSlotValues: { schema_hash: dynamicSchema, schema: {} },
        },
        plan: {
            operation: "patch",
            expected_workflow_identity: workflowIdentity,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: { nodes: [], edges: [] },
            create_nodes: [
                {
                    alias: "dynamic_slot",
                    node_type: "DynamicSlotValues",
                    schema_hash: dynamicSchema,
                    values: { "payload.strength": 0.75 },
                    layout_hint: { x: 300, y: 0, width: 240, height: 140 },
                },
                {
                    alias: "source",
                    node_type: "Source",
                    schema_hash: sourceSchema,
                    values: {},
                },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: [{
                source: {
                    ref: { alias: "source" },
                    output_index: 0,
                    output: "IMAGE",
                    type: "IMAGE",
                },
                target: {
                    ref: { alias: "dynamic_slot" },
                    input_index: 0,
                    occurrence_index: 0,
                    socket_index: 0,
                    input: "payload",
                    type: "IMAGE",
                    mode: "slot",
                },
            }],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 2,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 0,
                final_node_count: 2,
                final_edge_count: 1,
            },
        },
    };
    const contexts = new Map([
        ["new:source", {
            key: "new:source",
            node_type: "Source",
            schema_hash: sourceSchema,
            schema_inputs: [],
            schema_outputs: [{ index: 0, name: "IMAGE", type: "IMAGE" }],
            dynamic_selector_names: [],
            dynamic_input_roots: [],
        }],
        ["new:dynamic_slot", {
            key: "new:dynamic_slot",
            node_type: "DynamicSlotValues",
            schema_hash: dynamicSchema,
            schema_inputs: [{
                index: 0,
                occurrence_index: 0,
                name: "payload",
                type: "IMAGE",
                kind: "socket",
                socket_index: 0,
            }],
            schema_outputs: [],
            dynamic_selector_names: [],
            dynamic_input_roots: ["payload"],
        }],
    ]);
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            WORKFLOW_GRAPH_PATCH_PROPERTY,
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async () => ({}),
            buildGraphPatchSchemaContexts: async () => contexts,
            enrichGraphPatchNode,
            applyWorkflowGraphPatchAtomic,
            setTimeout: callback => { callback(); return 1; },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;
    executor.queryExecutor = {};
    executor.toolHandlers = executor._registerHandlers();

    const result = await executor.toolHandlers.apply_workflow_graph_patch(request);

    assert.equal(result.success, true);
    assert.equal(result.queued, false);
    assert.equal(workflow.nodes.length, 2);
    assert.equal(workflow.links.length, 1);
    assert.equal(workflow.links[0].target_input_index, 1);
    assert.equal(workflow.links[0].target_input, "payload");
    const finalDynamic = findNode(result.aliases.dynamic_slot);
    assert.equal(finalDynamic.values["payload.strength"], 0.75);
    assert.deepEqual(finalDynamic.pos, [300, 0]);
    assert.deepEqual(finalDynamic.size, [240, 140]);
    const deferred = calls.indexOf("values:deferred");
    const connected = calls.findIndex(item => item.startsWith("connect:"));
    const applied = calls.indexOf("values:payload.strength");
    assert.ok(deferred >= 0 && deferred < connected && connected < applied);
});


test("batched verification reads 5,000 node facts with only two graph-hash checks", async () => {
    const workflowIdentity = "fl-mcp-workflow:large-session:1";
    const pin = { workflow: { key: "large-workflow" } };
    const guard = { pin, expectedGraphHash: "hash:large" };
    let hashChecks = 0;
    let nodeReads = 0;
    const flApi = {
        pinActiveWorkflow(expected) {
            assert.equal(expected, workflowIdentity);
            return pin;
        },
        pauseAutoQueue() { return {}; },
        restoreAutoQueue() {},
        beginWorkflowChangeTransaction() { return {}; },
        endWorkflowChangeTransaction() {},
        createWorkflowMutationGuard() { return guard; },
        assertWorkflowMutationGuard(received) {
            assert.equal(received, guard);
            hashChecks += 1;
        },
        acceptWorkflowMutationGuard() {
            throw new Error("read-only verification must not accept a mutation");
        },
        getWorkflowNode(nodeId, received) {
            assert.equal(received, pin);
            nodeReads += 1;
            return { id: nodeId, node_id: nodeId, type: "Sibling" };
        },
    };
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async (_params, adapter) => {
                await adapter.withReadGuard(() => {
                    for (let nodeId = 1; nodeId <= 5_000; nodeId += 1) {
                        adapter.getNode(nodeId);
                    }
                });
                return { success: true };
            },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;

    const result = await executor._handleApplyWorkflowRefinement(
        refinementParams(workflowIdentity),
    );

    assert.equal(result.success, true);
    assert.equal(nodeReads, 5_000);
    assert.equal(hashChecks, 2);
});


test("tool executor rejects a stale workflow identity before pausing or opening a transaction", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    let pauseCalls = 0;
    flApi.pauseAutoQueue = () => {
        pauseCalls += 1;
        return {};
    };
    flApi.restoreAutoQueue = () => {};
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async () => {
                throw new Error("engine must not start");
            },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;

    await assert.rejects(
        () => executor._handleApplyWorkflowRefinement(
            refinementParams("fl-mcp-workflow:stale-session:9"),
        ),
        error => error?.code === "workflow_identity_precondition_failed",
    );
    assert.equal(pauseCalls, 0);
    assert.equal(harness.canvas.beforeCalls, 0);
});


test("concurrent refinement fails busy immediately and the lock releases after failure", async () => {
    const events = [];
    let releaseFirst;
    let markFirstStarted;
    const firstGate = new Promise(resolve => { releaseFirst = resolve; });
    const firstStarted = new Promise(resolve => { markFirstStarted = resolve; });
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async params => {
                events.push(`start:${params.request_id}`);
                if (params.request_id === "first") {
                    markFirstStarted();
                    await firstGate;
                    events.push("fail:first");
                    throw new Error("first failed");
                }
                events.push(`finish:${params.request_id}`);
                return { success: true, request_id: params.request_id };
            },
        },
    );
    const makeFlApi = label => {
        const pin = { workflow: { key: label } };
        const guard = { pin, expectedGraphHash: `hash:${label}` };
        return {
            pinActiveWorkflow(expected) {
                events.push(`pin:${label}:${expected}`);
                return pin;
            },
            pauseAutoQueue() { events.push(`pause:${label}`); return {}; },
            restoreAutoQueue() { events.push(`restore-auto:${label}`); },
            beginWorkflowChangeTransaction() { events.push(`begin:${label}`); return {}; },
            endWorkflowChangeTransaction() { events.push(`end:${label}`); },
            createWorkflowMutationGuard() { events.push(`guard:${label}`); return guard; },
        };
    };
    const firstExecutor = Object.create(ToolExecutor.prototype);
    firstExecutor.flApi = makeFlApi("first");
    const secondExecutor = Object.create(ToolExecutor.prototype);
    secondExecutor.flApi = makeFlApi("second");
    const firstPromise = firstExecutor._handleApplyWorkflowRefinement({
        request_id: "first",
        ...refinementParams("identity-first"),
    });
    await firstStarted;
    const secondPromise = secondExecutor._handleApplyWorkflowRefinement({
        request_id: "second",
        ...refinementParams("identity-second"),
    });
    await assert.rejects(
        secondPromise,
        error => error?.code === "canvas_mutation_busy" && error?.details?.retryable === true,
    );

    assert.equal(events.some(event => event.startsWith("pin:second")), false);
    assert.equal(events.includes("start:second"), false);

    releaseFirst();
    await assert.rejects(firstPromise, /first failed/);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(events.some(event => event.startsWith("pin:second")), false);
    assert.equal(events.includes("start:second"), false);

    const thirdExecutor = Object.create(ToolExecutor.prototype);
    thirdExecutor.flApi = makeFlApi("third");
    const thirdResult = await thirdExecutor._handleApplyWorkflowRefinement({
        request_id: "third",
        ...refinementParams("identity-third"),
    });
    assert.equal(thirdResult.success, true);
    assert.ok(events.indexOf("restore-auto:first") < events.indexOf("pin:third:identity-third"));
    assert.ok(events.indexOf("pin:third:identity-third") < events.indexOf("start:third"));
});


test("registered create and remove fail busy without delayed mutation, then run after release", async () => {
    const events = [];
    let releaseRefinement;
    let markRefinementStarted;
    const refinementGate = new Promise(resolve => { releaseRefinement = resolve; });
    const refinementStarted = new Promise(resolve => { markRefinementStarted = resolve; });
    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async () => {
                events.push("refinement-start");
                markRefinementStarted();
                await refinementGate;
                events.push("refinement-finish");
                return { success: true };
            },
            performance: { now: () => 1 },
        },
    );
    const pin = { workflow: { key: "workflow" } };
    const guard = { pin, expectedGraphHash: "hash:workflow" };
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = {
        pinActiveWorkflow() { events.push("pin"); return pin; },
        pauseAutoQueue() { events.push("pause"); return {}; },
        restoreAutoQueue() { events.push("restore-auto"); },
        beginWorkflowChangeTransaction() { events.push("begin"); return {}; },
        endWorkflowChangeTransaction() { events.push("end"); },
        createWorkflowMutationGuard() { return guard; },
        create(nodeType) {
            events.push(`create:${nodeType}`);
            return { id: 10, type: nodeType };
        },
        remove(nodeIds) {
            events.push(`remove:${nodeIds.join(",")}`);
            return { removed: nodeIds.length };
        },
    };
    executor.queryExecutor = {};
    const sentMessages = [];
    executor.wsClient = { send: async message => { sentMessages.push(message); } };
    executor.executionLog = [];
    executor.maxLogEntries = 10;
    executor.toolHandlers = executor._registerHandlers();

    const refinementPromise = executor.toolHandlers.apply_workflow_refinement(
        refinementParams("identity-workflow"),
    );
    await refinementStarted;
    const createBusyResult = executor.executeToolRequest({
        request_id: "busy-create",
        tool_name: "create_node",
        parameters: { node_type: "TestNode" },
    });
    const removePromise = executor.toolHandlers.remove_nodes({ node_ids: [4, 5] });
    await createBusyResult;
    await assert.rejects(removePromise, error => error?.code === "canvas_mutation_busy");

    assert.equal(sentMessages.length, 1);
    assert.equal(sentMessages[0].success, false);
    assert.equal(sentMessages[0].error_code, "canvas_mutation_busy");
    assert.deepEqual(
        JSON.parse(JSON.stringify(sentMessages[0].error_details)),
        { retryable: true },
    );
    assert.equal(events.includes("create:TestNode"), false);
    assert.equal(events.includes("remove:4,5"), false);

    releaseRefinement();
    const refinement = await refinementPromise;
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(events.includes("create:TestNode"), false);
    assert.equal(events.includes("remove:4,5"), false);

    const created = await executor.toolHandlers.create_node({ node_type: "TestNode" });
    const removed = await executor.toolHandlers.remove_nodes({ node_ids: [4, 5] });

    assert.equal(refinement.success, true);
    assert.equal(created.id, 10);
    assert.equal(removed.removed_count, 2);
    assert.ok(events.indexOf("restore-auto") < events.indexOf("create:TestNode"));
    assert.ok(events.indexOf("create:TestNode") < events.indexOf("remove:4,5"));
});


test("a same-workflow edit during a reveal delay fails guarded and restores the snapshot", async () => {
    const harness = flApiHarness();
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    flApi.restoreNestedImageReferences = () => {};
    flApi.pauseAutoQueue = () => ({ enabled: true });
    flApi.restoreAutoQueue = () => {};

    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            WORKFLOW_APPLICATION_PROPERTY: "application",
            WORKFLOW_REFINEMENT_PROPERTY: "refinement",
            applyWorkflowPlanAtomic: async () => ({}),
            applyWorkflowRefinementAtomic: async (_params, adapter) => {
                const before = await adapter.captureWorkflow();
                try {
                    await adapter.afterMutationStep({ phase: "node" });
                    return { success: true };
                } catch (error) {
                    await adapter.restoreWorkflow(before);
                    return {
                        success: false,
                        error: { code: error.code, message: error.message },
                        rollback: { attempted: true, complete: true },
                    };
                }
            },
            setTimeout: callback => {
                harness.graphState = { ...harness.graphState, marker: "user-edit" };
                callback();
                return 1;
            },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;

    const result = await executor._handleApplyWorkflowRefinement(
        refinementParams(flApi.getActiveWorkflowIdentity()),
    );

    assert.equal(result.success, false);
    assert.equal(result.error.code, "concurrent_workflow_edit");
    assert.equal(result.rollback.complete, true);
    assert.equal(harness.graphState.marker, "raw");
    assert.equal(harness.loadCalls.length, 1);
    assert.equal(harness.loadCalls[0].workflow, harness.originalWorkflow);
    assert.equal(harness.canvas.beforeCalls, 1);
    assert.equal(harness.canvas.afterCalls, 1);
    assert.equal(harness.canvas.read_only, false);
});
