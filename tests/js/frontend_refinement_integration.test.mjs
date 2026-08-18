import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

import {
    applyWorkflowGraphPatchAtomic,
    WORKFLOW_GRAPH_PATCH_PROPERTY,
} from "../../web/js/workflow_graph_patch_apply.js";
import {
    canonicalWorkflowJSON,
    workflowGraphHash,
    workflowGraphHashExcludingExtra,
} from "../../web/js/graph_precondition.js";
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
        canonicalWorkflowJSON: workflow => JSON.stringify(workflow),
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


const NAVIGATION_GRAPH_HASH = "a".repeat(64);
const STALE_NAVIGATION_GRAPH_HASH = "b".repeat(64);


async function branchNavigationFixture(options = {}) {
    const harness = flApiHarness();
    const effects = [];
    const scopeReads = [];
    const mutationCalls = {
        graphChange: 0,
        graphChangedEvent: 0,
        queue: 0,
    };
    const rootNode = { id: 1, title: "Root duplicate" };
    const priorSelection = { id: 99, title: "Prior selection" };
    const leafNode = { id: 1, title: "Leaf duplicate" };
    const secondLeafNode = { id: 2, title: "Leaf second" };
    const leafGraph = {
        id: "subgraph-leaf",
        _nodes: [leafNode, secondLeafNode],
        links: new Map(),
    };
    const leafContainer = {
        id: 8,
        title: "Leaf container",
        subgraph: leafGraph,
        isSubgraphNode: () => true,
    };
    const middleNode = { id: 1, title: "Middle duplicate" };
    const middleGraph = {
        id: "subgraph-middle",
        _nodes: [middleNode, leafContainer],
        links: new Map(),
    };
    const middleContainer = {
        id: 7,
        title: "Middle container",
        subgraph: middleGraph,
        isSubgraphNode: () => true,
    };
    const rootGraph = harness.graph;
    rootGraph.id = "workflow-root";
    rootGraph._nodes = [rootNode, priorSelection, middleContainer];
    rootGraph.change = () => { mutationCalls.graphChange += 1; };
    rootGraph.subgraphs = new Map([
        [middleGraph.id, middleGraph],
        [leafGraph.id, leafGraph],
    ]);
    rootGraph.resolveSubgraphIdPath = nodeIds => {
        scopeReads.push(nodeIds.join("/"));
        let graph = rootGraph;
        return nodeIds.map(nodeId => {
            const node = graph._nodes.find(item => String(item.id) === String(nodeId));
            if (!node?.subgraph) throw new Error("invalid scope");
            graph = node.subgraph;
            return node;
        });
    };
    for (const graph of [rootGraph, middleGraph, leafGraph]) {
        for (const node of graph._nodes) node.graph = graph;
    }

    if (options.serializedRuntimeIdProjection) {
        const graphs = [rootGraph, middleGraph, leafGraph];
        for (const graph of graphs) {
            for (const node of graph._nodes) {
                node.id = String(node.id);
                node.serialize = () => ({
                    id: Number(node.id),
                    type: node.subgraph?.id || node.type || "Pass",
                    title: node.title,
                    pos: [0, 0],
                    size: [220, 120],
                    flags: {},
                    mode: 0,
                    inputs: [],
                    outputs: [],
                    properties: {},
                    widgets_values: [],
                });
            }
        }
        const definitionFor = graph => ({
            id: graph.id,
            nodes: graph._nodes.map(node => node.serialize()),
            links: [],
            inputs: [],
            outputs: [],
            groups: [],
            reroutes: [],
            extra: {},
        });
        rootGraph.serialize = () => ({
            version: 0.4,
            last_node_id: 99,
            last_link_id: 0,
            nodes: rootGraph._nodes.map(node => node.serialize()),
            links: [],
            groups: [],
            config: {},
            extra: {},
            definitions: {
                subgraphs: [definitionFor(middleGraph), definitionFor(leafGraph)],
            },
        });
    }

    const canvas = harness.canvas;
    canvas.graph = rootGraph;
    canvas.ds = { scale: 1.25, offset: [17, 23] };
    canvas.selectedItems = new Set([priorSelection]);
    canvas.selected_nodes = { [priorSelection.id]: priorSelection };
    canvas.setGraph = graph => {
        effects.push(`set-graph:${graph.id}`);
        canvas.graph = graph;
        canvas.selectedItems = new Set();
        canvas.selected_nodes = {};
    };
    canvas.selectItems = items => {
        effects.push(`select:${items.map(item => item.title).join("|")}`);
        canvas.selectedItems = new Set(items);
        canvas.selected_nodes = Object.fromEntries(items.map(item => [item.id, item]));
    };
    canvas.fitViewToSelectionAnimated = async () => {
        effects.push("fit");
        canvas.ds.scale = 2;
        canvas.ds.offset[0] = 100;
        canvas.ds.offset[1] = 200;
    };

    harness.app.rootGraph = rootGraph;
    harness.app.queuePrompt = () => { mutationCalls.queue += 1; };
    harness.browserApi.dispatchCustomEvent = eventName => {
        if (eventName === "graphChanged") mutationCalls.graphChangedEvent += 1;
    };
    const { Class: FL_API } = await loadFlApi(harness, {
        nodeIdsEqual: (left, right) => String(left) === String(right),
        canonicalWorkflowJSON: workflow => {
            options.onCanonicalWorkflowJSON?.({ harness, workflow });
            return JSON.stringify(workflow);
        },
        workflowGraphHash: options.workflowGraphHash
            || (async () => NAVIGATION_GRAPH_HASH),
    });
    const flApi = new FL_API();
    return {
        harness,
        flApi,
        effects,
        scopeReads,
        mutationCalls,
        rootGraph,
        rootNode,
        priorSelection,
        middleGraph,
        leafGraph,
        leafNode,
        secondLeafNode,
        workflowIdentity: flApi.getActiveWorkflowIdentity(),
    };
}


function branchNavigationRequest(fixture, overrides = {}) {
    return {
        branch_id: "branch:test:upscale",
        expected_workflow_identity: fixture.workflowIdentity,
        expected_graph_hash: NAVIGATION_GRAPH_HASH,
        scope_path: [],
        node_ids: [1],
        ...overrides,
    };
}


function assertNavigationUiUnchanged(fixture) {
    assert.equal(fixture.harness.canvas.graph, fixture.rootGraph);
    assert.deepEqual(
        Array.from(fixture.harness.canvas.selectedItems),
        [fixture.priorSelection],
    );
    assert.equal(fixture.harness.canvas.ds.scale, 1.25);
    assert.deepEqual(fixture.harness.canvas.ds.offset, [17, 23]);
    assert.deepEqual(fixture.effects, []);
}


function serializedNavigationRoot(fixture) {
    return JSON.stringify(fixture.rootGraph.serialize());
}


function assertNoNavigationMutationCalls(fixture) {
    assert.deepEqual(fixture.mutationCalls, {
        graphChange: 0,
        graphChangedEvent: 0,
        queue: 0,
    });
}


function assertNoPrivateNavigationDiagnostics(value, sentinelSecret) {
    const serialized = JSON.stringify(value);
    assert.equal(serialized.includes(sentinelSecret), false);
    assert.equal(serialized.includes("expected_root_token"), false);
    assert.equal(serialized.includes("actual_root_token"), false);
    assert.ok(serialized.length < 4096, "branch navigation diagnostics must stay bounded");
}


test("exact branch navigation selects and natively fits a root branch", async () => {
    const fixture = await branchNavigationFixture();
    const beforeRoot = serializedNavigationRoot(fixture);
    const result = await fixture.flApi.navigateWorkflowBranchExact(
        branchNavigationRequest(fixture),
    );

    assert.equal(fixture.harness.canvas.graph, fixture.rootGraph);
    assert.deepEqual(Array.from(fixture.harness.canvas.selectedItems), [fixture.rootNode]);
    assert.deepEqual(fixture.effects, ["select:Root duplicate", "fit"]);
    assert.deepEqual(JSON.parse(JSON.stringify(result)), {
        branch_id: "branch:test:upscale",
        workflow_identity: fixture.workflowIdentity,
        graph_hash: NAVIGATION_GRAPH_HASH,
        scope_path: [],
        scope_graph_id: "workflow-root",
        selected_node_ids: [1],
        selected_count: 1,
        fitted_count: 1,
        fit_method: "native_selection",
        queued: false,
    });
    assert.equal(serializedNavigationRoot(fixture), beforeRoot);
    assertNoNavigationMutationCalls(fixture);
});


test("stale branch workflow identity or graph hash has no canvas effects", async t => {
    await t.test("workflow identity", async () => {
        const fixture = await branchNavigationFixture();
        const beforeRoot = serializedNavigationRoot(fixture);
        await assert.rejects(
            () => fixture.flApi.navigateWorkflowBranchExact(branchNavigationRequest(
                fixture,
                { expected_workflow_identity: "fl-mcp-workflow:stale:99" },
            )),
            error => error?.code === "workflow_identity_precondition_failed",
        );
        assertNavigationUiUnchanged(fixture);
        assert.equal(serializedNavigationRoot(fixture), beforeRoot);
        assertNoNavigationMutationCalls(fixture);
    });

    await t.test("graph hash", async () => {
        const fixture = await branchNavigationFixture();
        const beforeRoot = serializedNavigationRoot(fixture);
        await assert.rejects(
            () => fixture.flApi.navigateWorkflowBranchExact(branchNavigationRequest(
                fixture,
                { expected_graph_hash: STALE_NAVIGATION_GRAPH_HASH },
            )),
            error => error?.code === "branch_navigation_precondition_failed",
        );
        assertNavigationUiUnchanged(fixture);
        assert.equal(serializedNavigationRoot(fixture), beforeRoot);
        assertNoNavigationMutationCalls(fixture);
    });
});


test("branch navigation closes async graph-hash race windows", async t => {
    await t.test("a graph edit during the pre-effect hash has no transient canvas effect", async () => {
        const sentinelSecret = "SENTINEL_PRIVATE_BRANCH_PROMPT_DURING_HASH";
        let hashCalls = 0;
        let releaseHash;
        let markHashStarted;
        const hashGate = new Promise(resolve => { releaseHash = resolve; });
        const hashStarted = new Promise(resolve => { markHashStarted = resolve; });
        const fixture = await branchNavigationFixture({
            workflowGraphHash: async () => {
                hashCalls += 1;
                if (hashCalls === 2) {
                    markHashStarted();
                    await hashGate;
                }
                return NAVIGATION_GRAPH_HASH;
            },
        });

        const navigation = fixture.flApi.navigateWorkflowBranchExact(
            branchNavigationRequest(fixture),
        );
        await hashStarted;
        fixture.harness.graphState = {
            ...fixture.harness.graphState,
            marker: sentinelSecret,
        };
        releaseHash();
        let observedError = null;
        await assert.rejects(
            navigation,
            error => {
                observedError = error;
                return (
                    error?.code === "branch_navigation_precondition_failed"
                    && error?.details?.reason === "graph_changed_during_hash"
                );
            },
        );

        assertNoPrivateNavigationDiagnostics({
            error: observedError.message,
            error_code: observedError.code,
            error_details: observedError.details,
        }, sentinelSecret);
        assertNavigationUiUnchanged(fixture);
        assertNoNavigationMutationCalls(fixture);
    });

    await t.test("a graph edit immediately after a verified hash emits no canonical token", async () => {
        const sentinelSecret = "SENTINEL_PRIVATE_BRANCH_PROMPT_AFTER_HASH";
        let canonicalCalls = 0;
        const fixture = await branchNavigationFixture({
            onCanonicalWorkflowJSON: ({ harness }) => {
                canonicalCalls += 1;
                if (canonicalCalls === 4) {
                    harness.graphState = {
                        ...harness.graphState,
                        marker: sentinelSecret,
                    };
                }
            },
        });

        let observedError = null;
        await assert.rejects(
            () => fixture.flApi.navigateWorkflowBranchExact(
                branchNavigationRequest(fixture),
            ),
            error => {
                observedError = error;
                return (
                    error?.code === "branch_navigation_precondition_failed"
                    && error?.details?.reason === "graph_changed_after_hash"
                );
            },
        );

        assertNoPrivateNavigationDiagnostics({
            error: observedError.message,
            error_code: observedError.code,
            error_details: observedError.details,
        }, sentinelSecret);
        assertNavigationUiUnchanged(fixture);
        assertNoNavigationMutationCalls(fixture);
    });

    await t.test("a graph edit during the final hash fails and restores the prior UI", async () => {
        let hashCalls = 0;
        let releaseHash;
        let markHashStarted;
        const hashGate = new Promise(resolve => { releaseHash = resolve; });
        const hashStarted = new Promise(resolve => { markHashStarted = resolve; });
        const fixture = await branchNavigationFixture({
            workflowGraphHash: async () => {
                hashCalls += 1;
                if (hashCalls === 3) {
                    markHashStarted();
                    await hashGate;
                }
                return NAVIGATION_GRAPH_HASH;
            },
        });

        const navigation = fixture.flApi.navigateWorkflowBranchExact(
            branchNavigationRequest(fixture),
        );
        await hashStarted;
        fixture.harness.graphState = {
            ...fixture.harness.graphState,
            marker: "edited-during-final-hash",
        };
        const editedRoot = serializedNavigationRoot(fixture);
        releaseHash();
        await assert.rejects(
            navigation,
            error => (
                error?.code === "branch_navigation_precondition_failed"
                && error?.details?.reason === "graph_changed_during_hash"
            ),
        );

        assert.equal(fixture.harness.canvas.graph, fixture.rootGraph);
        assert.deepEqual(
            Array.from(fixture.harness.canvas.selectedItems),
            [fixture.priorSelection],
        );
        assert.equal(fixture.harness.canvas.ds.scale, 1.25);
        assert.deepEqual(fixture.harness.canvas.ds.offset, [17, 23]);
        assert.deepEqual(fixture.effects, [
            "select:Root duplicate",
            "fit",
            "select:Prior selection",
        ]);
        assert.equal(serializedNavigationRoot(fixture), editedRoot);
        assertNoNavigationMutationCalls(fixture);
    });

    await t.test("a scope and selection change during the final hash cannot report success", async () => {
        let hashCalls = 0;
        let releaseHash;
        let markHashStarted;
        const hashGate = new Promise(resolve => { releaseHash = resolve; });
        const hashStarted = new Promise(resolve => { markHashStarted = resolve; });
        const fixture = await branchNavigationFixture({
            workflowGraphHash: async () => {
                hashCalls += 1;
                if (hashCalls === 3) {
                    markHashStarted();
                    await hashGate;
                }
                return NAVIGATION_GRAPH_HASH;
            },
        });
        const beforeRoot = serializedNavigationRoot(fixture);

        const navigation = fixture.flApi.navigateWorkflowBranchExact(
            branchNavigationRequest(fixture),
        );
        await hashStarted;
        fixture.harness.canvas.graph = fixture.middleGraph;
        fixture.harness.canvas.selectedItems = new Set([
            fixture.middleGraph._nodes[0],
        ]);
        fixture.harness.canvas.selected_nodes = {
            1: fixture.middleGraph._nodes[0],
        };
        releaseHash();
        await assert.rejects(
            navigation,
            error => error?.code === "branch_navigation_verification_failed",
        );

        assert.equal(fixture.harness.canvas.graph, fixture.rootGraph);
        assert.deepEqual(
            Array.from(fixture.harness.canvas.selectedItems),
            [fixture.priorSelection],
        );
        assert.equal(fixture.harness.canvas.ds.scale, 1.25);
        assert.deepEqual(fixture.harness.canvas.ds.offset, [17, 23]);
        assert.equal(serializedNavigationRoot(fixture), beforeRoot);
        assertNoNavigationMutationCalls(fixture);
    });
});


test("invalid branch scope and node identities fail before selection or navigation", async t => {
    const cases = [
        {
            name: "malformed scope",
            overrides: { scope_path: [{ container_node_id: 7 }] },
            code: "invalid_branch_navigation",
        },
        {
            name: "ordinary node used as a scope container",
            overrides: {
                scope_path: [{ container_node_id: 1, subgraph_id: "subgraph-middle" }],
            },
            code: "branch_scope_not_found",
        },
        {
            name: "wrong subgraph identity",
            overrides: {
                scope_path: [{ container_node_id: 7, subgraph_id: "subgraph-wrong" }],
            },
            code: "branch_scope_not_found",
        },
        {
            name: "missing local node",
            overrides: { node_ids: [404] },
            code: "branch_node_missing",
        },
        {
            name: "repeated requested node",
            overrides: { node_ids: [1, 1] },
            code: "branch_node_ambiguous",
        },
    ];
    for (const item of cases) {
        await t.test(item.name, async () => {
            const fixture = await branchNavigationFixture();
            await assert.rejects(
                () => fixture.flApi.navigateWorkflowBranchExact(
                    branchNavigationRequest(fixture, item.overrides),
                ),
                error => error?.code === item.code,
            );
            assertNavigationUiUnchanged(fixture);
        });
    }

    await t.test("duplicate exact local node identity", async () => {
        const fixture = await branchNavigationFixture();
        fixture.rootGraph._nodes.push({ id: 1, title: "Colliding root ID" });
        await assert.rejects(
            () => fixture.flApi.navigateWorkflowBranchExact(
                branchNavigationRequest(fixture),
            ),
            error => error?.code === "branch_node_ambiguous",
        );
        assertNavigationUiUnchanged(fixture);
    });
});


test("root branch navigation preserves exact numeric and string node IDs", async () => {
    const fixture = await branchNavigationFixture();
    const stringNode = { id: "1", title: "String root ID", graph: fixture.rootGraph };
    fixture.rootGraph._nodes.push(stringNode);
    const beforeRoot = serializedNavigationRoot(fixture);

    const result = await fixture.flApi.navigateWorkflowBranchExact(
        branchNavigationRequest(fixture, { node_ids: [1, "1"] }),
    );

    assert.deepEqual(
        Array.from(fixture.harness.canvas.selectedItems),
        [fixture.rootNode, stringNode],
    );
    assert.deepEqual(result.selected_node_ids, [1, "1"]);
    assert.equal(serializedNavigationRoot(fixture), beforeRoot);
    assertNoNavigationMutationCalls(fixture);
});


test("root branch navigation projects an attested serialized integer ID to its live string node", async () => {
    const fixture = await branchNavigationFixture({ serializedRuntimeIdProjection: true });
    const beforeRoot = serializedNavigationRoot(fixture);

    const result = await fixture.flApi.navigateWorkflowBranchExact(
        branchNavigationRequest(fixture, { node_ids: [1] }),
    );

    assert.equal(fixture.rootNode.id, "1");
    assert.deepEqual(Array.from(fixture.harness.canvas.selectedItems), [fixture.rootNode]);
    assert.deepEqual(result.selected_node_ids, [1]);
    assert.equal(serializedNavigationRoot(fixture), beforeRoot);
    assertNoNavigationMutationCalls(fixture);
});


test("branch navigation rejects an ambiguous serialized projection without coercing live IDs", async () => {
    const fixture = await branchNavigationFixture({ serializedRuntimeIdProjection: true });
    const collision = {
        id: 1,
        title: "Genuine numeric collision",
        graph: fixture.rootGraph,
        serialize: () => ({
            id: 1,
            type: "Pass",
            title: "Genuine numeric collision",
            pos: [0, 0],
            size: [220, 120],
            flags: {},
            mode: 0,
            inputs: [],
            outputs: [],
            properties: {},
            widgets_values: [],
        }),
    };
    fixture.rootGraph._nodes.push(collision);

    await assert.rejects(
        () => fixture.flApi.navigateWorkflowBranchExact(
            branchNavigationRequest(fixture, { node_ids: [1] }),
        ),
        error => error?.code === "branch_node_ambiguous",
    );

    assertNavigationUiUnchanged(fixture);
    assertNoNavigationMutationCalls(fixture);
});


test("nested branch navigation uses the exact recursive scope despite duplicate local IDs", async () => {
    const fixture = await branchNavigationFixture();
    const beforeRoot = serializedNavigationRoot(fixture);
    const scopePath = [
        { container_node_id: 7, subgraph_id: "subgraph-middle" },
        { container_node_id: 8, subgraph_id: "subgraph-leaf" },
    ];
    const result = await fixture.flApi.navigateWorkflowBranchExact(
        branchNavigationRequest(fixture, {
            scope_path: scopePath,
            node_ids: [1, 2],
        }),
    );

    assert.equal(fixture.harness.canvas.graph, fixture.leafGraph);
    assert.deepEqual(
        Array.from(fixture.harness.canvas.selectedItems),
        [fixture.leafNode, fixture.secondLeafNode],
    );
    assert.deepEqual(fixture.effects, [
        "set-graph:subgraph-leaf",
        "select:Leaf duplicate|Leaf second",
        "fit",
    ]);
    assert.deepEqual(fixture.scopeReads, ["7/8", "7/8"]);
    assert.deepEqual(result.scope_path, scopePath);
    assert.deepEqual(result.selected_node_ids, [1, 2]);
    assert.equal(result.scope_graph_id, "subgraph-leaf");
    assert.equal(result.queued, false);
    assert.equal(serializedNavigationRoot(fixture), beforeRoot);
    assertNoNavigationMutationCalls(fixture);
});


test("nested branch navigation projects serialized scope and node IDs to live strings", async () => {
    const fixture = await branchNavigationFixture({ serializedRuntimeIdProjection: true });
    const beforeRoot = serializedNavigationRoot(fixture);
    const scopePath = [
        { container_node_id: 7, subgraph_id: "subgraph-middle" },
        { container_node_id: 8, subgraph_id: "subgraph-leaf" },
    ];

    const result = await fixture.flApi.navigateWorkflowBranchExact(
        branchNavigationRequest(fixture, {
            scope_path: scopePath,
            node_ids: [1, 2],
        }),
    );

    assert.equal(fixture.leafNode.id, "1");
    assert.equal(fixture.secondLeafNode.id, "2");
    assert.equal(fixture.harness.canvas.graph, fixture.leafGraph);
    assert.deepEqual(
        Array.from(fixture.harness.canvas.selectedItems),
        [fixture.leafNode, fixture.secondLeafNode],
    );
    assert.deepEqual(result.scope_path, scopePath);
    assert.deepEqual(result.selected_node_ids, [1, 2]);
    assert.equal(serializedNavigationRoot(fixture), beforeRoot);
    assertNoNavigationMutationCalls(fixture);
});


test("nested branch navigation preserves exact numeric and string local IDs", async () => {
    const fixture = await branchNavigationFixture();
    const stringLeafNode = { id: "1", title: "String leaf ID", graph: fixture.leafGraph };
    fixture.leafGraph._nodes.push(stringLeafNode);
    const beforeRoot = serializedNavigationRoot(fixture);
    const scopePath = [
        { container_node_id: 7, subgraph_id: "subgraph-middle" },
        { container_node_id: 8, subgraph_id: "subgraph-leaf" },
    ];

    const result = await fixture.flApi.navigateWorkflowBranchExact(
        branchNavigationRequest(fixture, {
            scope_path: scopePath,
            node_ids: [1, "1"],
        }),
    );

    assert.equal(fixture.harness.canvas.graph, fixture.leafGraph);
    assert.deepEqual(
        Array.from(fixture.harness.canvas.selectedItems),
        [fixture.leafNode, stringLeafNode],
    );
    assert.deepEqual(result.selected_node_ids, [1, "1"]);
    assert.equal(serializedNavigationRoot(fixture), beforeRoot);
    assertNoNavigationMutationCalls(fixture);
});


test("failed branch navigation restores and verifies graph, selection, and viewport exactly", async t => {
    const cases = [
        { name: "successful exact restoration", field: null, behavior: null },
        { name: "silent graph restoration", field: "setGraph", behavior: "silent" },
        { name: "throwing graph restoration", field: "setGraph", behavior: "throw" },
        { name: "silent selection restoration", field: "selectItems", behavior: "silent" },
        { name: "throwing selection restoration", field: "selectItems", behavior: "throw" },
    ];
    for (const item of cases) {
        await t.test(item.name, async () => {
            const fixture = await branchNavigationFixture();
            const canvas = fixture.harness.canvas;
            const beforeRoot = serializedNavigationRoot(fixture);
            const originalSetGraph = canvas.setGraph.bind(canvas);
            const originalSelectItems = canvas.selectItems.bind(canvas);
            let setGraphCalls = 0;
            let selectCalls = 0;
            canvas.setGraph = graph => {
                setGraphCalls += 1;
                if (item.field === "setGraph" && setGraphCalls === 2) {
                    fixture.effects.push(`restore-set-graph:${item.behavior}`);
                    if (item.behavior === "throw") throw new Error("setGraph restore failed");
                    return;
                }
                originalSetGraph(graph);
            };
            canvas.selectItems = items => {
                selectCalls += 1;
                if (item.field === "selectItems" && selectCalls === 2) {
                    fixture.effects.push(`restore-selection:${item.behavior}`);
                    if (item.behavior === "throw") throw new Error("selection restore failed");
                    return;
                }
                originalSelectItems(items);
            };
            canvas.fitViewToSelectionAnimated = async () => {
                fixture.effects.push("fit-failure");
                canvas.ds.scale = 4;
                canvas.ds.offset[0] = 400;
                canvas.ds.offset[1] = 500;
                throw new Error("forced navigation fit failure");
            };

            const scopePath = [
                { container_node_id: 7, subgraph_id: "subgraph-middle" },
                { container_node_id: 8, subgraph_id: "subgraph-leaf" },
            ];
            let observedError = null;
            await assert.rejects(
                () => fixture.flApi.navigateWorkflowBranchExact(
                    branchNavigationRequest(fixture, {
                        scope_path: scopePath,
                        node_ids: [1, 2],
                    }),
                ),
                error => {
                    observedError = error;
                    return error?.message === "forced navigation fit failure";
                },
            );

            assert.equal(observedError.message, "forced navigation fit failure");
            assert.equal(serializedNavigationRoot(fixture), beforeRoot);
            assertNoNavigationMutationCalls(fixture);
            assert.equal(canvas.ds.scale, 1.25);
            assert.deepEqual(canvas.ds.offset, [17, 23]);
            if (item.field === null) {
                assert.equal(canvas.graph, fixture.rootGraph);
                assert.deepEqual(Array.from(canvas.selectedItems), [fixture.priorSelection]);
                assert.equal(observedError.details, undefined);
                return;
            }

            assert.equal(observedError.details?.code, "branch_navigation_restore_failed");
            assert.equal(
                observedError.details?.cause?.message,
                "forced navigation fit failure",
            );
            assert.equal(observedError.details?.cause?.details, null);
            assert.ok(observedError.details?.issues?.some(issue => (
                issue.field === (item.field === "setGraph" ? "graph" : "selection")
            )));
        });
    }
});


test("ToolExecutor never serializes private branch workflow tokens on its wire", async () => {
    const sentinelSecret = "SENTINEL_PRIVATE_BRANCH_PROMPT_TOOL_WIRE";
    let hashCalls = 0;
    let releaseHash;
    let markHashStarted;
    const hashGate = new Promise(resolve => { releaseHash = resolve; });
    const hashStarted = new Promise(resolve => { markHashStarted = resolve; });
    const fixture = await branchNavigationFixture({
        workflowGraphHash: async () => {
            hashCalls += 1;
            if (hashCalls === 3) {
                markHashStarted();
                await hashGate;
            }
            return NAVIGATION_GRAPH_HASH;
        },
    });
    const canvas = fixture.harness.canvas;
    const originalSelectItems = canvas.selectItems.bind(canvas);
    let selectionCalls = 0;
    canvas.selectItems = items => {
        selectionCalls += 1;
        if (selectionCalls === 2) return;
        originalSelectItems(items);
    };

    const { Class: ToolExecutor } = await loadBrowserClass(
        "web/js/tool_executor.js",
        "ToolExecutor",
        {
            FL_API: class {},
            QueryExecutor: class {},
            performance: { now: () => 1 },
        },
    );
    const sentMessages = [];
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = fixture.flApi;
    executor.queryExecutor = {};
    executor.wsClient = { send: async message => { sentMessages.push(message); } };
    executor.executionLog = [];
    executor.maxLogEntries = 10;
    executor.toolHandlers = executor._registerHandlers();

    const execution = executor.executeToolRequest({
        request_id: "private-branch-wire",
        tool_name: "navigate_workflow_branch",
        parameters: branchNavigationRequest(fixture),
    });
    await hashStarted;
    fixture.harness.graphState = {
        ...fixture.harness.graphState,
        marker: sentinelSecret,
    };
    releaseHash();
    await execution;

    assert.equal(sentMessages.length, 1);
    assert.equal(sentMessages[0].success, false);
    assert.equal(
        sentMessages[0].error_code,
        "branch_navigation_precondition_failed",
    );
    assert.equal(
        sentMessages[0].error_details?.code,
        "branch_navigation_restore_failed",
    );
    assert.equal(
        sentMessages[0].error_details?.cause?.details?.reason,
        "graph_changed_during_hash",
    );
    assertNoPrivateNavigationDiagnostics(sentMessages[0], sentinelSecret);
    assertNoPrivateNavigationDiagnostics(executor.executionLog, sentinelSecret);
    assertNoNavigationMutationCalls(fixture);
});


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
    assert.ok(supportedTools.includes("navigate_workflow_branch"));
    assert.equal(contractRevisions.apply_workflow_graph_patch, 3);
    assert.equal(contractRevisions.get_node_image_ref, 2);
    assert.equal(contractRevisions.get_canvas_image_refs, 1);
    assert.equal(contractRevisions.get_selected_nodes, 2);
    assert.equal(contractRevisions.navigate_workflow_branch, 1);
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


test("nested image rollback verifies exact serialization before presentation-only full-size hydration", async () => {
    const harness = flApiHarness();
    const imageValue = "ren-chat/session/source.png [input]";
    const node = {
        id: 33,
        widgets: [{ name: "image", value: imageValue, options: { values: [imageValue] } }],
        widgets_values: [imageValue],
        properties: {},
        images: [{ filename: "runtime-only-sentinel.png", subfolder: "", type: "input" }],
        imgs: [],
        imageIndex: -1,
    };
    harness.graph._nodes = [node];
    harness.browserApi.apiURL = value => value;
    harness.graph.serialize = () => ({
        marker: "nested-image",
        nodes: [{
            id: node.id,
            widgets_values: structuredClone(node.widgets_values),
            properties: structuredClone(node.properties),
        }],
        links: [],
        extra: {},
    });
    harness.app.loadGraphData = async (snapshot, _clean, _restoreView, workflow) => {
        const restoredNode = snapshot.nodes[0];
        node.widgets[0].value = restoredNode.widgets_values[0];
        node.widgets_values = structuredClone(restoredNode.widgets_values);
        node.properties = structuredClone(restoredNode.properties);
        harness.workflowStore.activeWorkflow = workflow;
    };

    class FullSizeImage {
        constructor() {
            this.complete = true;
            this.naturalWidth = 3584;
            this.naturalHeight = 1536;
        }

        set src(value) {
            this._src = value;
            this.onload?.();
        }

        get src() {
            return this._src;
        }
    }

    const parseNestedImage = currentNode => {
        const value = currentNode?.widgets?.find(widget => widget.name === "image")?.value;
        if (typeof value !== "string" || !value.includes("/")) return null;
        return {
            filename: "source.png",
            subfolder: "ren-chat/session",
            type: "input",
        };
    };
    const exactHash = async workflow => JSON.stringify(workflow);
    const { Class: FL_API } = await loadFlApi(harness, {
        Image: FullSizeImage,
        nestedImageRefForNode: parseNestedImage,
        workflowGraphHash: exactHash,
        canonicalWorkflowJSON: workflow => JSON.stringify(workflow),
    });
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const snapshot = flApi.captureWorkflowSnapshot(pin);
    const runtimeImagesBefore = structuredClone(node.images);

    node.widgets[0].value = "wrong.png [input]";
    node.widgets_values = ["wrong.png [input]"];
    node.properties = { image: "wrong.png [input]" };

    const restored = await flApi.restoreWorkflowSnapshot(snapshot, pin);

    assert.equal(restored.snapshot_restored, true);
    assert.equal(restored.hash_verified, true);
    assert.equal(restored.graph_hash, await exactHash(snapshot));
    assert.deepEqual(harness.graph.serialize(), snapshot);
    assert.deepEqual(node.properties, {});
    assert.deepEqual(node.widgets_values, [imageValue]);
    assert.deepEqual(node.images, runtimeImagesBefore);
    assert.equal(node.imageIndex, 0);
    assert.equal(node.imgs.length, 1);
    assert.equal(node.imgs[0].naturalWidth, 3584);
    assert.equal(node.imgs[0].naturalHeight, 1536);
    assert.match(node.imgs[0].src, /^\/view\?/);
    assert.doesNotMatch(node.imgs[0].src, /\/fl_mcp\/image\/thumbnail/);
});


test("rollback skips image hydration when loadGraphData does not restore the exact snapshot", async () => {
    const harness = flApiHarness();
    const exactHash = async workflow => JSON.stringify(workflow);
    const { Class: FL_API } = await loadFlApi(harness, {
        workflowGraphHash: exactHash,
        canonicalWorkflowJSON: workflow => JSON.stringify(workflow),
    });
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const snapshot = flApi.captureWorkflowSnapshot(pin);
    let hydrationCalls = 0;
    flApi.hydrateNestedImagePreviews = () => { hydrationCalls += 1; };
    harness.app.loadGraphData = async (restored, _clean, _restoreView, workflow) => {
        harness.graphState = {
            ...structuredClone(restored),
            extra: { ...(restored.extra || {}), rollback_corruption: true },
        };
        harness.workflowStore.activeWorkflow = workflow;
    };

    await assert.rejects(
        flApi.restoreWorkflowSnapshot(snapshot, pin),
        /did not restore exactly; preview hydration was skipped/,
    );
    assert.equal(hydrationCalls, 0);
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


test("mutation guard rejects a graph rewrite while its digest is pending", async () => {
    const harness = flApiHarness();
    let rewriteDuringHash = false;
    const guardedHash = async workflow => {
        const digest = `hash:${workflow.marker || "graph"}`;
        await Promise.resolve();
        if (rewriteDuringHash) {
            rewriteDuringHash = false;
            harness.graphState = { ...harness.graphState, marker: "hash-race-user" };
        }
        return digest;
    };
    const { Class: FL_API } = await loadFlApi(harness, {
        workflowGraphHash: guardedHash,
    });
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const guard = await flApi.createWorkflowMutationGuard(pin);

    rewriteDuringHash = true;
    await assert.rejects(
        () => flApi.assertWorkflowMutationGuard(guard),
        error => (
            error?.code === "concurrent_workflow_edit"
            && error?.details?.reason === "graph_changed_during_hash"
            && error?.details?.phase === "assert"
            && error?.details?.expected_graph_hash === "hash:raw"
            && error?.details?.actual_graph_hash === "hash:hash-race-user"
        ),
    );
});


test("created-node normalization settles without accepting edits elsewhere", async () => {
    const harness = flApiHarness();
    const lifecycleEffects = [];
    let frameId = 0;
    const guardedHash = async workflow => {
        const digest = await workflowGraphHash(workflow);
        lifecycleEffects.shift()?.();
        return digest;
    };
    const { Class: FL_API } = await loadFlApi(harness, {
        canonicalWorkflowJSON,
        workflowGraphHash: guardedHash,
        requestAnimationFrame: callback => {
            frameId += 1;
            callback(frameId);
            return frameId;
        },
        cancelAnimationFrame() {},
    });
    harness.graphState = {
        version: 0.4,
        last_node_id: 1,
        last_link_id: 0,
        nodes: [{
            id: 1,
            type: "Sentinel",
            pos: [0, 0],
            size: [220, 120],
            widgets_values: ["untouched"],
            properties: {},
        }],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const guard = await flApi.createWorkflowMutationGuard(pin);

    harness.graphState.last_node_id = 2;
    harness.graphState.nodes.push({
        id: 2,
        type: "LoadImage",
        pos: [300, 0],
        size: [282.798828125, 102],
        widgets_values: ["source.png", "image"],
        properties: {},
    });
    const checkpoint = flApi.captureCreatedNodeNormalizationCheckpoint(guard, {
        node_id: 2,
        node_type: "LoadImage",
        definition_id: null,
    });
    lifecycleEffects.push(() => {
        const created = harness.graphState.nodes.find(node => node.id === 2);
        created.size = [282.798828125, 314];
        created.widgets_values = ["source.png", "image"];
        created.properties = { cnr_id: "comfy-core", ver: "0.29.0" };
    });

    const acceptedHash = await flApi.acceptCreatedNodeNormalization(guard, checkpoint);

    assert.equal(acceptedHash, await workflowGraphHash(harness.graphState));
    assert.equal(guard.expectedGraphHash, acceptedHash);
    assert.deepEqual(harness.graphState.nodes[0].widgets_values, ["untouched"]);
    assert.deepEqual(harness.graphState.nodes[1].size, [282.798828125, 314]);

    harness.graphState.last_node_id = 3;
    harness.graphState.nodes.push({
        id: 3,
        type: "LoadImage",
        pos: [600, 0],
        size: [282.798828125, 102],
        widgets_values: ["source.png", "image"],
        properties: {},
    });
    const unsafeCheckpoint = flApi.captureCreatedNodeNormalizationCheckpoint(guard, {
        node_id: 3,
        node_type: "LoadImage",
        definition_id: null,
    });
    lifecycleEffects.push(() => {
        harness.graphState.nodes[0].pos = [99, 0];
    });

    await assert.rejects(
        () => flApi.acceptCreatedNodeNormalization(guard, unsafeCheckpoint),
        error => (
            error?.code === "concurrent_workflow_edit"
            && error?.details?.phase === "created_node_normalization"
            && error?.details?.reason === "change_outside_created_node_during_hash"
            && error?.details?.node_id === 3
        ),
    );
});



test("hidden-page node normalization waits for visible lifecycle frames", async () => {
    const harness = flApiHarness();
    const visibility = { visibilityState: "hidden" };
    const { Class: FL_API } = await loadFlApi(harness, {
        canonicalWorkflowJSON,
        workflowGraphHash,
        document: visibility,
    });
    harness.graphState = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const guard = await flApi.createWorkflowMutationGuard(pin);

    harness.graphState.last_node_id = 1;
    harness.graphState.nodes.push({
        id: 1,
        type: "LoadImage",
        pos: [0, 0],
        size: [282.798828125, 102],
        widgets_values: ["source.png", "image"],
        properties: {},
    });
    const checkpoint = flApi.captureCreatedNodeNormalizationCheckpoint(guard, {
        node_id: 1,
        node_type: "LoadImage",
        definition_id: null,
    });
    let now = 0;
    const pendingTurns = [];
    flApi._createdNodeNormalizationNow = () => now;
    flApi._waitForCreatedNodeNormalizationTurn = () => new Promise(resolve => {
        pendingTurns.push(resolve);
    });
    let settled = false;
    const acceptance = flApi.acceptCreatedNodeNormalization(guard, checkpoint)
        .then(value => {
            settled = true;
            return value;
        });
    const advance = async ({ ms, visible, mutate = null }) => {
        now += ms;
        visibility.visibilityState = visible ? "visible" : "hidden";
        mutate?.();
        assert.ok(pendingTurns.length > 0, "normalization turn must be pending");
        pendingTurns.shift()({
            visible,
            frameObserved: visible,
        });
        await Promise.resolve();
        await Promise.resolve();
    };

    await advance({
        ms: 80,
        visible: false,
        mutate: () => {
            harness.graphState.nodes[0].size = [282.798828125, 314];
        },
    });
    await advance({ ms: 80, visible: false });
    await advance({ ms: 80, visible: false });
    assert.equal(settled, false, "hidden timer turns cannot prove rAF lifecycle quiescence");
    await advance({ ms: 40, visible: true });
    await advance({ ms: 40, visible: true });
    await advance({ ms: 40, visible: true });
    assert.equal(settled, false, "three frames alone are shorter than the quiet horizon");
    await advance({ ms: 80, visible: true });
    await advance({ ms: 40, visible: true });

    await acceptance;

    assert.deepEqual(harness.graphState.nodes[0].size, [282.798828125, 314]);
    assert.equal(guard.expectedGraphHash, await workflowGraphHash(harness.graphState));
});


test("visible-tab node normalization settles from wall-clock quiet time even when no frame paints", async () => {
    const harness = flApiHarness();
    const visibility = { visibilityState: "visible" };
    const { Class: FL_API } = await loadFlApi(harness, {
        canonicalWorkflowJSON,
        workflowGraphHash,
        document: visibility,
    });
    harness.graphState = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());
    const guard = await flApi.createWorkflowMutationGuard(pin);

    harness.graphState.last_node_id = 1;
    harness.graphState.nodes.push({
        id: 1,
        type: "LoadImage",
        pos: [0, 0],
        size: [282.798828125, 102],
        widgets_values: ["source.png", "image"],
        properties: {},
    });
    const checkpoint = flApi.captureCreatedNodeNormalizationCheckpoint(guard, {
        node_id: 1,
        node_type: "LoadImage",
        definition_id: null,
    });

    let now = 0;
    const pendingTurns = [];
    flApi._createdNodeNormalizationNow = () => now;
    flApi._waitForCreatedNodeNormalizationTurn = () => new Promise(resolve => {
        pendingTurns.push(resolve);
    });

    let settled = false;
    const acceptance = flApi.acceptCreatedNodeNormalization(guard, checkpoint)
        .then(value => {
            settled = true;
            return value;
        });

    // The document reports "visible" the entire time (never hidden), but the
    // rAF watchdog times out on every turn before a frame paints - e.g. an
    // unfocused-but-visible window under OS/browser rAF throttling. A missed
    // paint on a genuinely visible tab is not evidence the graph is still
    // changing; the graph token (a data-model snapshot) is the real signal,
    // and it must still be able to settle from wall-clock quiescence alone.
    // Waiting must yield a real timer turn, not just microtasks: the
    // acceptance path hashes the graph with the real workflowGraphHash,
    // which resolves via WebCrypto's macrotask, not a microtask.
    const waitForEvent = async (predicate, maxAttempts = 200) => {
        for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
            if (predicate()) return true;
            await new Promise(resolve => setTimeout(resolve, 0));
        }
        return false;
    };

    for (let i = 0; i < 8; i += 1) {
        const arrived = await waitForEvent(() => pendingTurns.length > 0 || settled);
        if (!arrived || settled) break;
        now += 60;
        pendingTurns.shift()({ visible: true, frameObserved: false });
    }

    assert.equal(
        await waitForEvent(() => settled),
        true,
        "wall-clock-quiet turns without a rendered frame must still settle",
    );
    const acceptedHash = await acceptance;
    assert.equal(guard.expectedGraphHash, acceptedHash);
    assert.equal(guard.expectedGraphHash, await workflowGraphHash(harness.graphState));
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


test("FL_API mask inspection follows the exact pending revision source", async () => {
    const harness = flApiHarness();
    const node = {
        id: 41,
        type: "LoadImage",
        comfyClass: "LoadImage",
        title: "LOAD & MASK IMAGE",
        widgets: [{ name: "image", value: "original/source.png [input]" }],
        images: [],
    };
    harness.graph._nodes = [node];
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const original = {
        filename: "source.png",
        subfolder: "original",
        type: "input",
    };
    const pending = {
        filename: "revision-1.png",
        subfolder: "fl_mcp_masks",
        type: "input",
    };
    flApi.pendingMaskReviews.set("41", {
        token: "review-1",
        nodeId: 41,
        image: pending,
        originalImage: original,
        previewUrl: null,
    });

    const inspected = flApi.getNodeImageRef(41);
    assert.deepEqual(inspected.image, pending);
    assert.equal(inspected.pending_review, true);
    assert.deepEqual(inspected.original_image, original);

    flApi.pendingMaskReviews.clear();
    const committed = flApi.getNodeImageRef(41);
    assert.deepEqual(committed.image, original);
    assert.equal(committed.pending_review, false);
});


test("FL_API exact image inspection returns canonical serialized ID for string runtime ID", async () => {
    const harness = flApiHarness();
    const imageWidget = { name: "image", value: "reference.png [input]" };
    const node = {
        id: "1",
        type: "LoadImage",
        comfyClass: "LoadImage",
        title: "Reference",
        widgets: [imageWidget],
        images: [],
        serialize: () => ({ id: 1, type: "LoadImage" }),
        pos: [10, 20],
        size: [300, 200],
        mode: 0,
        inputs: [],
        outputs: [],
    };
    harness.graph._nodes = [node];
    harness.canvas.selected_nodes = { "1": node };
    harness.graphState = {
        marker: "raw",
        nodes: [{ id: 1, type: "LoadImage" }],
        links: [],
        extra: {},
    };
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());

    const result = flApi.getNodeImageRef(1, pin);

    assert.equal(result.node_id, 1);
    assert.equal(typeof result.node_id, "number");
    assert.deepEqual(result.image, {
        filename: "reference.png",
        subfolder: "",
        type: "input",
    });
    assert.throws(
        () => flApi.getNodeImageRef("1", pin),
        /missing|absent|not a serialized workflow ID/,
    );
    const selected = flApi.getSelectedNodes(pin);
    assert.equal(selected.length, 1);
    assert.equal(selected[0].id, 1);
    assert.equal(typeof selected[0].id, "number");
});


test("FL_API canvas image discovery is stable, plural, exact, and deduplicated", async () => {
    const harness = flApiHarness();
    const makeNode = (runtimeId, serializedId, x, y, widget, images = []) => ({
        id: runtimeId,
        type: "LoadImage",
        comfyClass: "LoadImage",
        title: `Image ${serializedId}`,
        widgets: widget ? [{ name: "image", value: widget }] : [],
        images,
        pos: [x, y],
        serialize: () => ({
            id: serializedId,
            type: "LoadImage",
            pos: [x, y],
        }),
    });
    const lower = makeNode("1", 1, 20, 100, "shared.png [input]", [
        { filename: "batch-1.png", subfolder: "temp", type: "temp" },
        { filename: "batch-2.png", subfolder: "temp", type: "temp" },
    ]);
    const upper = makeNode("2", 2, 20, 0, "shared.png [input]");
    harness.graph._nodes = [lower, upper];
    harness.graphState = {
        marker: "raw",
        nodes: [lower.serialize(), upper.serialize()],
        links: [],
        extra: {},
    };
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const pin = flApi.pinActiveWorkflow(flApi.getActiveWorkflowIdentity());

    const firstPage = flApi.getCanvasImageRefs({ offset: 0, limit: 2 }, pin);

    assert.equal(firstPage.total_count, 3);
    assert.equal(firstPage.has_more, true);
    assert.equal(firstPage.next_offset, 2);
    assert.deepEqual(firstPage.images[0].image, {
        filename: "shared.png",
        subfolder: "",
        type: "input",
    });
    assert.deepEqual(
        Array.from(firstPage.images[0].sources, source => source.node_id),
        [2, 1],
    );
    assert.equal(firstPage.images[0].source_count, 2);
    assert.equal(firstPage.images[1].image.filename, "batch-1.png");
    assert.equal(firstPage.images[1].sources[0].image_index, 0);

    const explicit = flApi.getCanvasImageRefs({ nodeIds: [1] }, pin);
    assert.deepEqual(
        Array.from(explicit.images, item => item.image.filename),
        ["shared.png", "batch-1.png", "batch-2.png"],
    );
    assert.throws(
        () => flApi.getCanvasImageRefs({ nodeIds: ["1"] }, pin),
        error => error?.code === "canvas_image_node_unavailable",
    );
    assert.throws(
        () => flApi.getCanvasImageRefs({ nodeIds: [1, 1] }, pin),
        /duplicate exact IDs/,
    );
});


test("mask edit receipt with serialized integer ID survives pending view and confirmation", async () => {
    const harness = flApiHarness();
    const imageWidget = { name: "image", value: "source.png [input]" };
    const node = {
        id: "1",
        type: "LoadImage",
        comfyClass: "LoadImage",
        widgets: [imageWidget],
        serialize: () => ({ id: 1, type: "LoadImage" }),
    };
    harness.graph._nodes = [node];
    harness.graphState = {
        nodes: [{ id: 1, type: "LoadImage" }],
        links: [],
        extra: {},
    };
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const workflowIdentity = flApi.getActiveWorkflowIdentity();
    const source = { filename: "source.png", subfolder: "", type: "input" };
    const edited = { filename: "edited.png", subfolder: "fl_mcp_masks", type: "input" };
    const graphHash = "a".repeat(64);
    // This is the exact authority emitted by editNodeMask: canonical serialized
    // node ID, never the live LiteGraph string projection.
    flApi.pendingMaskReviews.set("number:1", {
        token: "review-typed-id",
        nodeId: 1,
        image: edited,
        originalImage: source,
        committedImage: source,
        previewUrl: null,
        workflowIdentity,
        graphHash,
        sourceImage: source,
    });
    flApi.createWorkflowMutationGuard = async () => ({ expectedGraphHash: graphHash });
    flApi.acceptWorkflowMutationGuard = async () => "b".repeat(64);
    flApi._assignImageToNode = (_node, image) => {
        imageWidget.value = `${image.subfolder ? `${image.subfolder}/` : ""}${image.filename} [input]`;
    };
    flApi._releaseMaskReviewPreview = () => {};
    flApi._markGraphChanged = () => {};
    flApi._restoreAutoQueueAfterMaskReview = () => {};

    const pending = flApi.getPendingMaskReviewReceipt(1);
    assert.equal(pending.node_id, 1);
    assert.equal(typeof pending.node_id, "number");
    assert.equal(pending.review_token, "review-typed-id");

    const confirmed = await flApi.confirmMaskReview(1, pending.review_token);
    assert.equal(confirmed.success, true);
    assert.equal(confirmed.node_id, 1);
    assert.equal(typeof confirmed.node_id, "number");
    assert.equal(confirmed.review_token, pending.review_token);
    assert.equal(confirmed.queued, false);
    assert.equal(flApi.pendingMaskReviews.size, 0);
});


test("FL_API mask confirmation preserves verified success when auto-queue restore fails", async () => {
    const harness = flApiHarness();
    const node = { id: 41, type: "LoadImage", widgets: [] };
    harness.graph._nodes = [node];
    const { Class: FL_API } = await loadFlApi(harness);
    const flApi = new FL_API();
    const original = {
        filename: "source.png",
        subfolder: "original",
        type: "input",
    };
    const approved = {
        filename: "mask.png",
        subfolder: "fl_mcp_masks",
        type: "input",
    };
    let committed = structuredClone(original);
    let pauseCalls = 0;
    let graphChanges = 0;
    flApi.pendingMaskReviews.set("number:41", {
        token: "review-1",
        nodeId: 41,
        image: approved,
        originalImage: original,
        committedImage: original,
        previewUrl: null,
        workflowIdentity: "workflow-a",
        graphHash: "a".repeat(64),
        sourceImage: original,
    });
    flApi.maskReviewAutoQueueState = { kind: "queueSettings", mode: "instant" };
    flApi.pinActiveWorkflow = identity => ({ identity });
    flApi.createWorkflowMutationGuard = async () => ({
        expectedGraphHash: "a".repeat(64),
    });
    flApi._workflowNodeFromSerializedId = () => node;
    flApi.getNodeImageRef = () => ({ image: structuredClone(committed) });
    flApi._assignImageToNode = (_node, image) => {
        committed = structuredClone(image);
    };
    flApi.acceptWorkflowMutationGuard = async () => "b".repeat(64);
    flApi._releaseMaskReviewPreview = () => {};
    flApi._markGraphChanged = () => {
        graphChanges += 1;
    };
    flApi._restoreAutoQueueAfterMaskReview = () => {
        throw new Error("auto-queue restore failed");
    };
    flApi._pauseAutoQueueForMaskReview = () => {
        pauseCalls += 1;
        return { kind: "queueSettings", mode: "disabled" };
    };

    const result = await flApi.confirmMaskReview(41, "review-1");

    assert.equal(result.success, true);
    assert.equal(result.approved, true);
    assert.equal(result.queued, false);
    assert.deepEqual(committed, approved);
    assert.equal(graphChanges, 1);
    assert.equal(pauseCalls, 1);
    assert.equal(flApi.pendingMaskReviews.size, 0);
    assert.deepEqual(Array.from(result.cleanup_warnings, item => ({ ...item })), [{
        phase: "restore_auto_queue",
        message: "auto-queue restore failed",
    }]);
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
        captureCreatedNodeNormalizationCheckpoint(received, target) {
            assert.equal(received, guard);
            assert.deepEqual(JSON.parse(JSON.stringify(target)), {
                node_id: 7,
                node_type: "Child",
                definition_id: null,
            });
            calls.push("normalization-checkpoint");
            return { target };
        },
        acceptCreatedNodeNormalization(received, checkpoint) {
            assert.equal(received, guard);
            assert.equal(checkpoint.target.node_id, 7);
            calls.push("normalization-accept");
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


test("root GraphPatch keeps serialized integer IDs across live string-node create and retry", async () => {
    const schemaHash = "7".repeat(64);
    const deferredNodeEffects = [];
    class ProjectedNode {
        constructor(id = null) {
            this.id = id === null ? null : String(id);
            this.type = "ProjectedSource";
            this.comfyClass = "ProjectedSource";
            this.title = "Projected source";
            this.pos = [0, 0];
            this.size = [220, 120];
            this.inputs = [{ name: "image", type: "IMAGE", link: null }];
            this.outputs = [{ name: "image", type: "IMAGE", links: [] }];
            this.widgets = [];
            this.widgets_values = [];
            this.properties = {};
            this.flags = {};
            this.mode = 0;
        }

        serialize() {
            return {
                id: Number(this.id),
                type: this.type,
                pos: structuredClone(this.pos),
                size: structuredClone(this.size),
                flags: structuredClone(this.flags),
                mode: this.mode,
                inputs: structuredClone(this.inputs),
                outputs: structuredClone(this.outputs),
                widgets_values: [],
                properties: structuredClone(this.properties),
            };
        }

        connect(sourceSlot, target, targetSlot) {
            const link = this.graph.addLink(this, sourceSlot, target, targetSlot);
            this.outputs[sourceSlot].links.push(link.id);
            target.inputs[targetSlot].link = link.id;
            return link;
        }
    }
    class ProjectedGraph {
        constructor() {
            this._nodes = [new ProjectedNode(1), new ProjectedNode(2)];
            for (const node of this._nodes) node.graph = this;
            this.links = new Map();
            this.extra = {};
            this.lastNodeId = 2;
            this.lastLinkId = 0;
            this._nodes[0].connect(0, this._nodes[1], 0);
        }

        add(node) {
            this.lastNodeId += 1;
            node.id = String(this.lastNodeId);
            node.graph = this;
            this._nodes.push(node);
            deferredNodeEffects.push(() => {
                node.size = [node.size[0], node.size[1] + 17];
                node.properties.frontend_normalized = true;
            });
        }

        remove(node) {
            for (const link of [...this.links.values()]) {
                if (link.origin_id === node.id || link.target_id === node.id) {
                    this.removeLink(link.id);
                }
            }
            this._nodes = this._nodes.filter(item => item !== node);
        }

        removeLink(linkId) {
            const link = this.links.get(linkId);
            if (!link) return;
            const source = this._nodes.find(node => node.id === link.origin_id);
            const target = this._nodes.find(node => node.id === link.target_id);
            if (source) {
                source.outputs[link.origin_slot].links = source.outputs[link.origin_slot].links
                    .filter(id => id !== link.id);
            }
            if (target) target.inputs[link.target_slot].link = null;
            this.links.delete(linkId);
        }

        addLink(source, sourceSlot, target, targetSlot) {
            this.lastLinkId += 1;
            const link = {
                id: this.lastLinkId,
                origin_id: source.id,
                origin_slot: sourceSlot,
                target_id: target.id,
                target_slot: targetSlot,
                type: "IMAGE",
            };
            this.links.set(link.id, link);
            return link;
        }

        serialize() {
            return {
                version: 0.4,
                last_node_id: this.lastNodeId,
                last_link_id: this.lastLinkId,
                nodes: this._nodes.map(node => node.serialize()),
                links: [...this.links.values()].map(link => [
                    link.id,
                    Number(link.origin_id),
                    link.origin_slot,
                    Number(link.target_id),
                    link.target_slot,
                    link.type,
                ]),
                groups: [],
                config: {},
                extra: structuredClone(this.extra),
            };
        }

        change() {}
        setDirtyCanvas() {}
    }

    const graph = new ProjectedGraph();
    const guardedWorkflowGraphHash = async snapshot => {
        const digest = await workflowGraphHash(snapshot);
        deferredNodeEffects.shift()?.();
        return digest;
    };
    const workflow = { key: "projected-root-workflow", changeTracker: { changeCount: 0 } };
    const app = {
        graph,
        rootGraph: graph,
        canvas: {
            read_only: false,
            emitBeforeChange() {},
            emitAfterChange() {},
            setDirty() {},
        },
        extensionManager: { workflow: { activeWorkflow: workflow } },
    };
    const { Class: FL_API } = await loadBrowserClass("web/js/fl_api.js", "FL_API", {
        app,
        api: { dispatchCustomEvent() {} },
        LiteGraph: { createNode: nodeType => (
            nodeType === "ProjectedSource" ? new ProjectedNode() : null
        ) },
        GRAPH_PRECONDITION_SCHEMA: "fl-mcp.graph-precondition.v1",
        canonicalWorkflowJSON,
        workflowGraphHash: guardedWorkflowGraphHash,
        workflowGraphHashExcludingExtra,
        nodeIdsEqual: (left, right) => String(left) === String(right),
        findNonOverlappingPosition: value => ({ x: value.x, y: value.y }),
        getGraphInsertionOrigin: () => ({ x: 300, y: 0 }),
        convertComfyWidgetToInput: async () => false,
        formatImageWidgetRef: value => String(value),
        nestedImageRefForNode: () => null,
        normalizeMaskRegion: value => value,
        parseImageWidgetRef: () => null,
        summarizeMaskPixels: () => ({}),
        document: { visibilityState: "visible" },
        requestAnimationFrame: callback => {
            callback();
            return 1;
        },
        cancelAnimationFrame() {},
    });
    const flApi = new FL_API();
    flApi.pauseAutoQueue = () => ({ mode: "disabled" });
    flApi.restoreAutoQueue = () => {};
    flApi.getNodeDefinitions = async () => ({ ProjectedSource: {} });
    const workflowIdentity = flApi.getActiveWorkflowIdentity();
    const originalEdge = {
        source: {
            ref: { node_id: 1 },
            output_index: 0,
            output: "image",
            type: "IMAGE",
        },
        target: {
            ref: { node_id: 2 },
            input_index: 0,
            occurrence_index: 0,
            socket_index: 0,
            input: "image",
            type: "IMAGE",
            mode: "slot",
        },
    };
    const request = {
        application_id: "projected-root-clone-retry-test",
        expected_catalog_hash: "c".repeat(64),
        patch_hash: "d".repeat(64),
        schema_contracts: {
            ProjectedSource: { schema_hash: schemaHash, schema: {} },
        },
        plan: {
            operation: "patch",
            expected_workflow_identity: workflowIdentity,
            expected_graph_hash: await workflowGraphHash(graph.serialize()),
            assertions: {
                nodes: [1, 2].map(nodeId => ({
                    ref: { node_id: nodeId },
                    node_type: "ProjectedSource",
                    schema_hash: schemaHash,
                })),
                edges: [structuredClone(originalEdge)],
            },
            create_nodes: [{
                alias: "clone",
                node_type: "ProjectedSource",
                schema_hash: schemaHash,
                values: {},
            }],
            update_nodes: [{
                ref: { node_id: 1 },
                node_type: "ProjectedSource",
                schema_hash: schemaHash,
                expected_values: {},
                set_values: {},
                layout_hint: { x: 10, y: 20, width: 230, height: 130 },
            }],
            remove_edges: [structuredClone(originalEdge)],
            add_edges: [{
                source: {
                    ref: { node_id: 1 },
                    output_index: 0,
                    output: "image",
                    type: "IMAGE",
                },
                target: {
                    ref: { alias: "clone" },
                    input_index: 0,
                    occurrence_index: 0,
                    socket_index: 0,
                    input: "image",
                    type: "IMAGE",
                    mode: "slot",
                },
            }],
            remove_nodes: [{
                ref: { node_id: 2 },
                node_type: "ProjectedSource",
                schema_hash: schemaHash,
                expected_incident_edges: [structuredClone(originalEdge)],
            }],
            attachments: [],
            expected_delta: {
                created_node_count: 1,
                updated_node_count: 1,
                removed_node_count: 1,
                added_edge_count: 1,
                removed_edge_count: 1,
                final_node_count: 2,
                final_edge_count: 1,
            },
        },
    };
    const contexts = new Map([
        ["existing:1", {
            key: "existing:1",
            node_type: "ProjectedSource",
            schema_hash: schemaHash,
            schema_inputs: [{
                index: 0,
                occurrence_index: 0,
                name: "image",
                type: "IMAGE",
                kind: "socket",
                socket_index: 0,
            }],
            schema_outputs: [{ index: 0, name: "image", type: "IMAGE" }],
            dynamic_selector_names: [],
            dynamic_input_roots: [],
        }],
        ["existing:2", {
            key: "existing:2",
            node_type: "ProjectedSource",
            schema_hash: schemaHash,
            schema_inputs: [{
                index: 0,
                occurrence_index: 0,
                name: "image",
                type: "IMAGE",
                kind: "socket",
                socket_index: 0,
            }],
            schema_outputs: [{ index: 0, name: "image", type: "IMAGE" }],
            dynamic_selector_names: [],
            dynamic_input_roots: [],
        }],
        ["new:clone", {
            key: "new:clone",
            node_type: "ProjectedSource",
            schema_hash: schemaHash,
            schema_inputs: [{
                index: 0,
                occurrence_index: 0,
                name: "image",
                type: "IMAGE",
                kind: "socket",
                socket_index: 0,
            }],
            schema_outputs: [{ index: 0, name: "image", type: "IMAGE" }],
            dynamic_selector_names: [],
            dynamic_input_roots: [],
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
            applyWorkflowGraphPatchAtomic,
            buildGraphPatchSchemaContexts: async () => contexts,
            enrichGraphPatchNode,
            setTimeout: callback => { callback(); return 1; },
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;
    executor.queryExecutor = {};
    executor.toolHandlers = executor._registerHandlers();

    const first = await executor.toolHandlers.apply_workflow_graph_patch(request);
    const retry = await executor.toolHandlers.apply_workflow_graph_patch(request);

    assert.equal(first.success, true, JSON.stringify(first.error));
    assert.deepEqual(first.aliases, { clone: 3 });
    assert.deepEqual(first.created_node_ids, [3]);
    assert.deepEqual(first.removed_node_ids, [2]);
    assert.deepEqual(graph._nodes.map(node => node.id), ["1", "3"]);
    assert.deepEqual(graph.serialize().nodes.map(node => node.id), [1, 3]);
    assert.deepEqual(graph.serialize().links.map(link => [link[1], link[3]]), [[1, 3]]);
    assert.deepEqual(graph._nodes[0].pos, [10, 20]);
    assert.deepEqual(graph._nodes[0].size, [230, 130]);
    assert.equal(graph._nodes[1].properties.frontend_normalized, true);
    assert.equal(retry.success, true, JSON.stringify(retry.error));
    assert.equal(retry.already_applied, true);
    assert.equal(graph._nodes.length, 2);

    const collidingRuntimeNode = new ProjectedNode(1);
    collidingRuntimeNode.id = 1;
    collidingRuntimeNode.graph = graph;
    graph._nodes.push(collidingRuntimeNode);
    assert.throws(
        () => flApi.getWorkflowNode(1),
        error => error?.code === "workflow_node_projection_ambiguous",
    );
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
        captureCreatedNodeNormalizationCheckpoint(_guard, target) { return { target }; },
        acceptCreatedNodeNormalization() {},
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


test("registered branch navigation is serialized by the shared canvas lock", async () => {
    const calls = [];
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
        },
    );
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = {
        async navigateWorkflowBranchExact(params) {
            calls.push(`start:${params.branch_id}`);
            if (params.branch_id === "first") {
                markFirstStarted();
                await firstGate;
            }
            calls.push(`finish:${params.branch_id}`);
            return { branch_id: params.branch_id, queued: false };
        },
    };
    executor.queryExecutor = {};
    executor.toolHandlers = executor._registerHandlers();

    const first = executor.toolHandlers.navigate_workflow_branch({ branch_id: "first" });
    await firstStarted;
    await assert.rejects(
        () => executor.toolHandlers.navigate_workflow_branch({ branch_id: "second" }),
        error => error?.code === "canvas_mutation_busy" && error?.details?.retryable === true,
    );
    assert.deepEqual(calls, ["start:first"]);

    releaseFirst();
    assert.deepEqual(await first, { branch_id: "first", queued: false });
    assert.deepEqual(
        await executor.toolHandlers.navigate_workflow_branch({ branch_id: "third" }),
        { branch_id: "third", queued: false },
    );
    assert.deepEqual(calls, [
        "start:first",
        "finish:first",
        "start:third",
        "finish:third",
    ]);
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
