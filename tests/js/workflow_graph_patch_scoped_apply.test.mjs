import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

import {
    applyWorkflowGraphPatchAtomic,
    GRAPH_PATCH_LEDGER_KEY,
    GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID,
    GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID,
    SCOPED_WORKFLOW_GRAPH_PATCH_SCHEMA,
    workflowScopeDefinitionHash,
} from "../../web/js/workflow_graph_patch_apply.js";
import {
    canonicalWorkflowJSON,
    workflowGraphHash,
    workflowGraphHashExcludingExtra,
} from "../../web/js/graph_precondition.js";
import { enrichGraphPatchNode } from "../../web/js/node_schema_contract.js";


const PASS_HASH = "1".repeat(64);
const REPLACEMENT_HASH = "2".repeat(64);
const CATALOG_HASH = "c".repeat(64);
const PATCH_HASH = "d".repeat(64);
const INPUT_ID = "00000000-0000-7000-8000-000000000001";
const OUTPUT_ID = "00000000-0000-7000-8000-000000000002";


function clone(value) {
    return structuredClone(value);
}


function jsonTransportClone(value) {
    return JSON.parse(JSON.stringify(value));
}


function port(id, linkIds) {
    return { id, name: "image", type: "IMAGE", linkIds: clone(linkIds) };
}


function rawNode(id, type, schemaHash) {
    return {
        id,
        type,
        schema_hash: schemaHash,
        inputs: [{ name: "image", type: "IMAGE", link: null }],
        outputs: [{ name: "image", type: "IMAGE", links: [] }],
        widgets_values: [],
        properties: {},
        pos: [40, 60],
        size: [220, 120],
        flags: {},
        mode: 0,
    };
}


function linearDefinition(id, nodeType = "Pass", schemaHash = PASS_HASH) {
    const node = rawNode(1, nodeType, schemaHash);
    node.inputs[0].link = 1;
    node.outputs[0].links = [2];
    return {
        id,
        name: `Definition ${id}`,
        version: 1,
        state: { lastNodeId: 1, lastLinkId: 2, sentinel: `${id}-state` },
        inputNode: { id: -10, bounding: [0, 0, 75, 100] },
        outputNode: { id: -20, bounding: [500, 0, 75, 100] },
        inputs: [port(`${id === "scope-def" ? INPUT_ID : "10000000-0000-7000-8000-000000000001"}`, [1])],
        outputs: [port(`${id === "scope-def" ? OUTPUT_ID : "10000000-0000-7000-8000-000000000002"}`, [2])],
        nodes: [node],
        links: [
            [1, -10, 0, 1, 0, "IMAGE"],
            [2, 1, 0, -20, 0, "IMAGE"],
        ],
        groups: [{ title: `${id}-group`, bounding: [1, 2, 3, 4] }],
        reroutes: [{ id: 9, pos: [7, 8], sentinel: id }],
        extra: { sentinel: `${id}-extra` },
    };
}


function containerNode(id, definitionId) {
    return {
        id,
        type: definitionId,
        inputs: [{ name: "image", type: "IMAGE" }],
        outputs: [{ name: "image", type: "IMAGE" }],
        widgets_values: [],
        properties: { container_sentinel: `${definitionId}-${String(id)}` },
        pos: [0, 0],
        size: [240, 140],
    };
}


function outerDefinition() {
    const definition = linearDefinition("outer-def", "scope-def", "a".repeat(64));
    definition.nodes = [containerNode(200, "scope-def")];
    definition.links = [
        [1, -10, 0, 200, 0, "IMAGE"],
        [2, 200, 0, -20, 0, "IMAGE"],
    ];
    definition.nodes[0].inputs[0].link = 1;
    definition.nodes[0].outputs[0].links = [2];
    return definition;
}


function rootWorkflow() {
    const definitions = [
        outerDefinition(),
        linearDefinition("scope-def"),
        linearDefinition("sibling-def"),
    ];
    for (const definition of definitions) {
        definition.state.lastNodeId = 300;
        definition.state.lastLinkId = 2;
    }
    return {
        version: 1,
        last_node_id: 300,
        last_link_id: 2,
        revision: 11,
        nodes: [containerNode(100, "outer-def"), containerNode(300, "sibling-def")],
        links: [],
        groups: [{ title: "root-group", bounding: [10, 20, 30, 40] }],
        reroutes: [{ id: 90, pos: [70, 80], sentinel: "root" }],
        definitions: {
            subgraphs: definitions,
        },
        extra: { sentinel: "root-extra", nested: { state: { lastNodeId: "must-stay" } } },
    };
}


function path() {
    return [
        { container_node_id: 100, subgraph_id: "outer-def" },
        { container_node_id: 200, subgraph_id: "scope-def" },
    ];
}


function boundary(kind) {
    return {
        slot_id: kind === "scope_input" ? INPUT_ID : OUTPUT_ID,
        slot_index: 0,
        name: "image",
        type: "IMAGE",
    };
}


function source(ref) {
    return { ref, output_index: 0, output: "image", type: "IMAGE" };
}


function target(ref) {
    return {
        ref,
        input_index: 0,
        occurrence_index: 0,
        socket_index: 0,
        input: "image",
        type: "IMAGE",
        mode: "slot",
    };
}


function edge(sourceRef, targetRef) {
    return { source: source(sourceRef), target: target(targetRef) };
}


function typedEqual(left, right) {
    return typeof left === typeof right && Object.is(left, right);
}


function parseLink(link) {
    if (Array.isArray(link)) {
        return {
            id: link[0], sourceId: link[1], sourceSlot: link[2],
            targetId: link[3], targetSlot: link[4], type: link[5] ?? null,
        };
    }
    return {
        id: link.id ?? link.link_id,
        sourceId: link.origin_id ?? link.source_id ?? link.source_node_id,
        sourceSlot: link.origin_slot ?? link.source_slot ?? link.source_output_index,
        targetId: link.target_id ?? link.target_node_id,
        targetSlot: link.target_slot ?? link.target_input_index,
        type: link.type ?? link.link_type ?? null,
    };
}


class ScopedHarness {
    constructor(workflow = rootWorkflow()) {
        this.workflow = clone(workflow);
        this.mutations = 0;
        this.restoreCalls = 0;
        this.connectCalls = 0;
        this.failConnectAt = null;
        this.sabotageSiblingCounter = false;
        this.raceOnResolve = false;
    }

    definition(id = "scope-def") {
        return this.workflow.definitions.subgraphs.find(item => item.id === id);
    }

    captureWorkflow() {
        return clone(this.workflow);
    }

    restoreWorkflow(snapshot) {
        this.restoreCalls += 1;
        this.workflow = clone(snapshot);
    }

    setWorkflowExtra(key, value) {
        this.workflow.extra[key] = clone(value);
    }

    resolveScopedGraph(descriptor) {
        assert.deepEqual(descriptor.scope.scope_path, path());
        if (this.raceOnResolve) this.workflow.extra.race = "changed-before-effects";
        return this.scopeAdapter(descriptor);
    }

    refreshManifests() {
        const definition = this.definition();
        definition.inputs.forEach(item => { item.linkIds = []; });
        definition.outputs.forEach(item => { item.linkIds = []; });
        for (const node of definition.nodes) {
            node.inputs.forEach(item => { item.link = null; });
            node.outputs.forEach(item => { item.links = []; });
        }
        for (const raw of definition.links) {
            const link = parseLink(raw);
            if (typedEqual(link.sourceId, -10)) {
                definition.inputs[link.sourceSlot].linkIds.push(link.id);
            } else {
                const sourceNode = definition.nodes.find(node => typedEqual(node.id, link.sourceId));
                sourceNode.outputs[link.sourceSlot].links.push(link.id);
            }
            if (typedEqual(link.targetId, -20)) {
                definition.outputs[link.targetSlot].linkIds.push(link.id);
            } else {
                const targetNode = definition.nodes.find(node => typedEqual(node.id, link.targetId));
                targetNode.inputs[link.targetSlot].link = link.id;
            }
        }
    }

    virtualNode(descriptor, inputSide) {
        const definition = this.definition();
        const ports = inputSide ? definition.inputs : definition.outputs;
        return {
            id: inputSide ? descriptor.input_runtime_id : descriptor.output_runtime_id,
            type: inputSide ? descriptor.input_node_type : descriptor.output_node_type,
            schema_hash: inputSide ? descriptor.input_schema_hash : descriptor.output_schema_hash,
            inputs: inputSide ? [] : ports.map((item, index) => ({
                name: item.name, type: item.type, link: item.linkIds[0] ?? null,
                socket_index: index, schema_index: index,
            })),
            schema_inputs: inputSide ? [] : ports.map((item, index) => ({
                name: item.name, type: item.type, index, occurrence_index: 0, kind: "socket",
            })),
            outputs: inputSide ? ports.map((item, index) => ({
                name: item.name, type: item.type, links: clone(item.linkIds), index,
            })) : [],
            widgets: [], values: {}, widgets_values: [], properties: {},
            pos: [inputSide ? 0 : 500, 0], size: [75, 100], flags: {}, mode: 0,
        };
    }

    nodeFacts(node) {
        return {
            ...clone(node),
            node_id: node.id,
            node_type: node.type,
            schema_inputs: node.inputs.map((item, index) => ({
                name: item.name,
                type: item.type,
                index,
                occurrence_index: 0,
                kind: "socket",
                socket_index: index,
            })),
            live_inputs: node.inputs.map((item, index) => ({
                ...clone(item), socket_index: index, schema_index: index,
            })),
            outputs: node.outputs.map((item, index) => ({ ...clone(item), index })),
            widgets: [],
            values: {},
            position: { x: node.pos[0], y: node.pos[1] },
            size: { width: node.size[0], height: node.size[1] },
            serialized_node: clone(node),
        };
    }

    scopeAdapter(descriptor) {
        const harness = this;
        const findNode = id => harness.definition().nodes.find(node => typedEqual(node.id, id));
        const connectionFacts = () => harness.definition().links.map(raw => {
            const link = parseLink(raw);
            return {
                id: link.id,
                source_node_id: link.sourceId,
                source_output_index: link.sourceSlot,
                source_output: "image",
                target_node_id: link.targetId,
                target_input_index: link.targetSlot,
                target_input: "image",
                type: link.type,
            };
        });
        return {
            captureDefinition: () => clone(harness.definition()),
            captureWorkflow: () => ({
                version: harness.definition().version,
                state: clone(harness.definition().state),
                nodes: [
                    harness.virtualNode(descriptor, true),
                    ...clone(harness.definition().nodes),
                    harness.virtualNode(descriptor, false),
                ],
                links: clone(harness.definition().links),
            }),
            getNode(id) {
                if (typedEqual(id, descriptor.input_runtime_id)) {
                    return harness.nodeFacts(harness.virtualNode(descriptor, true));
                }
                if (typedEqual(id, descriptor.output_runtime_id)) {
                    return harness.nodeFacts(harness.virtualNode(descriptor, false));
                }
                const node = findNode(id);
                return node ? harness.nodeFacts(node) : null;
            },
            listConnections: connectionFacts,
            createNode(planned) {
                harness.mutations += 1;
                const definition = harness.definition();
                const id = harness.workflow.last_node_id + 1;
                harness.workflow.last_node_id = id;
                for (const item of harness.workflow.definitions.subgraphs) {
                    item.state.lastNodeId = id;
                }
                const node = rawNode(id, planned.node_type, planned.schema_hash);
                node.pos = [planned.layout_hint?.x ?? 100, planned.layout_hint?.y ?? 100];
                if (planned.layout_hint?.width !== undefined) node.size[0] = planned.layout_hint.width;
                if (planned.layout_hint?.height !== undefined) node.size[1] = planned.layout_hint.height;
                definition.nodes.push(node);
                return { id, node_id: id, type: planned.node_type };
            },
            setNodeValuesExact() { return { applied: [] }; },
            setNodeMetadata(id, metadata) {
                harness.mutations += 1;
                findNode(id).properties.fl_mcp_workflow_graph_patch = clone(metadata);
            },
            setNodeLayoutExact(id, layout) {
                harness.mutations += 1;
                const node = findNode(id);
                node.pos = [layout.x, layout.y];
                if (layout.width !== undefined) node.size[0] = layout.width;
                if (layout.height !== undefined) node.size[1] = layout.height;
            },
            disconnectConnection(expected) {
                harness.mutations += 1;
                const definition = harness.definition();
                const index = definition.links.findIndex(raw => {
                    const link = parseLink(raw);
                    return typedEqual(link.sourceId, expected.source_node_id)
                        && link.sourceSlot === expected.source_output_index
                        && typedEqual(link.targetId, expected.target_node_id)
                        && link.targetSlot === expected.target_input_index;
                });
                if (index < 0) throw new Error("missing exact scoped link");
                definition.links.splice(index, 1);
                harness.refreshManifests();
                return { disconnected: true };
            },
            connectNodes(sourceId, targetId, connection) {
                harness.mutations += 1;
                harness.connectCalls += 1;
                const definition = harness.definition();
                const id = harness.workflow.last_link_id + 1;
                harness.workflow.last_link_id = id;
                for (const item of harness.workflow.definitions.subgraphs) {
                    item.state.lastLinkId = id;
                }
                definition.links.push([
                    id,
                    sourceId,
                    connection.source_output_index,
                    targetId,
                    connection.target_input_index,
                    connection.type,
                ]);
                harness.refreshManifests();
                if (harness.sabotageSiblingCounter) {
                    harness.definition("sibling-def").state.lastNodeId += 100;
                }
                if (harness.connectCalls === harness.failConnectAt) {
                    throw new Error("injected scoped connect failure");
                }
                return connectionFacts().find(item => item.id === id);
            },
            removeNodes(ids) {
                harness.mutations += 1;
                const keys = new Set(ids.map(id => `${typeof id}:${String(id)}`));
                harness.definition().nodes = harness.definition().nodes.filter(node => (
                    !keys.has(`${typeof node.id}:${String(node.id)}`)
                ));
                harness.refreshManifests();
                return { removed: ids.length };
            },
        };
    }
}


async function replaceRequest(harness, overrides = {}) {
    const oldRef = { node_id: 1 };
    const newRef = { alias: "replacement" };
    const inputRef = { scope_input: boundary("scope_input") };
    const outputRef = { scope_output: boundary("scope_output") };
    const entry = edge(inputRef, oldRef);
    const exit = edge(oldRef, outputRef);
    return {
        application_id: "scoped-replace-20260809-v1",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: PATCH_HASH,
        plan: {
            operation: "scoped_patch",
            expected_workflow_identity: "fl-mcp-workflow:scoped-js-test:1",
            expected_graph_hash: await workflowGraphHash(harness.captureWorkflow()),
            scope: {
                kind: "subgraph_definition",
                scope_path: path(),
                definition_id: "scope-def",
                definition_hash: await workflowScopeDefinitionHash(harness.definition()),
                edit_mode: "instance",
                affected_scope_paths: [path()],
            },
            assertions: {
                nodes: [{ ref: oldRef, node_type: "Pass", schema_hash: PASS_HASH }],
                edges: [entry, exit],
            },
            create_nodes: [{
                alias: "replacement",
                node_type: "Replacement",
                schema_hash: REPLACEMENT_HASH,
                values: {},
                layout_hint: { x: 300, y: 40, width: 260, height: 150 },
            }],
            update_nodes: [],
            remove_edges: [entry, exit],
            add_edges: [edge(inputRef, newRef), edge(newRef, outputRef)],
            remove_nodes: [{
                ref: oldRef,
                node_type: "Pass",
                schema_hash: PASS_HASH,
                expected_incident_edges: [entry, exit],
            }],
            attachments: [],
            expected_delta: {
                created_node_count: 1,
                updated_node_count: 0,
                removed_node_count: 1,
                added_edge_count: 2,
                removed_edge_count: 2,
                final_node_count: 1,
                final_edge_count: 2,
            },
            ...clone(overrides),
        },
    };
}


function addRetainedScopedConnection(harness) {
    const definition = harness.definition();
    definition.nodes.push(rawNode(2, "Pass", PASS_HASH), rawNode(3, "Pass", PASS_HASH));
    definition.links.push([3, 2, 0, 3, 0, "IMAGE"]);
    harness.workflow.last_link_id = 3;
    for (const item of harness.workflow.definitions.subgraphs) item.state.lastLinkId = 3;
    harness.refreshManifests();
    return edge({ node_id: 2 }, { node_id: 3 });
}


const repositoryRoot = new URL("../../", import.meta.url);


async function loadBrowserClass(relativePath, className, globals = {}) {
    let sourceText = await readFile(new URL(relativePath, repositoryRoot), "utf8");
    sourceText = sourceText.replace(
        /^import\s+[\s\S]*?\s+from\s+["'][^"']+["'];\s*/gm,
        "",
    );
    sourceText = sourceText.replace(`export class ${className}`, `class ${className}`);
    sourceText += `\nglobalThis.__loadedClass = ${className};\n`;
    const context = vm.createContext({
        console: { log() {}, warn() {}, error() {}, debug() {} },
        structuredClone,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        ...globals,
    });
    vm.runInContext(sourceText, context, { filename: relativePath });
    return context.__loadedClass;
}


class NativeScopeNode {
    constructor(raw, runtimeStringIds = false) {
        this.runtimeStringIds = runtimeStringIds;
        this.id = raw.id ?? null;
        if (runtimeStringIds && Number.isInteger(this.id) && this.id >= 0) {
            this.id = String(this.id);
        }
        this.type = raw.type;
        this.comfyClass = raw.type;
        this.title = raw.title || raw.type;
        this.inputs = clone(raw.inputs || []);
        this.outputs = clone(raw.outputs || []);
        this.widgets = [];
        this.widgets_values = clone(raw.widgets_values || []);
        this.properties = clone(raw.properties || {});
        this.pos = clone(raw.pos || [0, 0]);
        this.size = clone(raw.size || [220, 120]);
        this.flags = clone(raw.flags || {});
        this.mode = raw.mode ?? 0;
        this.schema_hash = raw.schema_hash;
        this.graph = null;
        this.subgraph = null;
    }

    isSubgraphNode() {
        return Boolean(this.subgraph);
    }

    serialize() {
        return {
            id: this.runtimeStringIds && this.id !== null ? Number(this.id) : this.id,
            type: this.type,
            ...(this.schema_hash ? { schema_hash: this.schema_hash } : {}),
            inputs: clone(this.inputs),
            outputs: clone(this.outputs),
            widgets_values: clone(this.widgets_values),
            properties: clone(this.properties),
            pos: clone(this.pos),
            size: clone(this.size),
            flags: clone(this.flags),
            mode: this.mode,
        };
    }

    connect(sourceSlot, target, targetSlot) {
        return this.graph.addRuntimeLink(this.id, sourceSlot, target.id, targetSlot);
    }

    disconnectInput(targetSlot) {
        const link = [...this.graph.links.values()].find(item => (
            typedEqual(item.target_id, this.id) && item.target_slot === targetSlot
        ));
        if (!link) return false;
        this.graph.metrics.nodeDisconnects += 1;
        this.graph.removeLink(link.id);
        return true;
    }
}


class NativeSubgraph {
    constructor(
        definition,
        sharedState,
        metrics,
        runtimeStringIds = false,
        nativeUndefinedBoundaryPinned = false,
        nativeInvalidArray = false,
        nativeDefinedOptionalFields = false,
    ) {
        this.id = definition.id;
        this.base = clone(definition);
        this.state = sharedState;
        this.metrics = metrics;
        this.runtimeStringIds = runtimeStringIds;
        this.nativeUndefinedBoundaryPinned = nativeUndefinedBoundaryPinned;
        this.nativeInvalidArray = nativeInvalidArray;
        this.nativeDefinedOptionalFields = nativeDefinedOptionalFields;
        this._nodes = (definition.nodes || []).map(raw => (
            new NativeScopeNode(raw, runtimeStringIds)
        ));
        this.links = new Map((definition.links || []).map(raw => {
            const item = parseLink(raw);
            return [item.id, {
                id: item.id,
                origin_id: runtimeStringIds && item.sourceId >= 0
                    ? String(item.sourceId)
                    : item.sourceId,
                origin_slot: item.sourceSlot,
                target_id: runtimeStringIds && item.targetId >= 0
                    ? String(item.targetId)
                    : item.targetId,
                target_slot: item.targetSlot,
                type: item.type,
            }];
        }));
        this.inputs = (definition.inputs || []).map((raw, index) => (
            this.boundaryPort(raw, index, true)
        ));
        this.outputs = (definition.outputs || []).map((raw, index) => (
            this.boundaryPort(raw, index, false)
        ));
        this.inputNode = {
            slots: this.inputs,
            boundingRect: clone(definition.inputNode?.bounding || [0, 0, 75, 100]),
        };
        this.outputNode = {
            slots: this.outputs,
            boundingRect: clone(definition.outputNode?.bounding || [500, 0, 75, 100]),
        };
        this.failRuntimeConnectAt = null;
        for (const node of this._nodes) node.graph = this;
        this.refreshManifests();
    }

    boundaryPort(raw, index, inputSide) {
        const port = {
            id: raw.id,
            name: raw.name,
            type: raw.type,
            linkIds: clone(raw.linkIds || []),
        };
        if (inputSide) {
            port.connect = (input, target) => {
                this.metrics.inputBoundaryConnects += 1;
                return this.addRuntimeLink(-10, index, target.id, target.inputs.indexOf(input));
            };
        } else {
            port.connect = (output, sourceNode) => {
                this.metrics.outputBoundaryConnects += 1;
                return this.addRuntimeLink(
                    sourceNode.id,
                    sourceNode.outputs.indexOf(output),
                    -20,
                    index,
                );
            };
            port.disconnect = () => {
                const matches = [...this.links.values()].filter(link => (
                    typedEqual(link.target_id, -20) && link.target_slot === index
                ));
                this.metrics.outputBoundaryDisconnects += 1;
                for (const link of matches) this.removeLink(link.id);
            };
        }
        return port;
    }

    refreshManifests() {
        this.inputs.forEach(port => { port.linkIds = []; });
        this.outputs.forEach(port => { port.linkIds = []; });
        for (const node of this._nodes) {
            node.inputs.forEach(input => { input.link = null; });
            node.outputs.forEach(output => { output.links = []; });
        }
        for (const link of this.links.values()) {
            if (typedEqual(link.origin_id, -10)) {
                this.inputs[link.origin_slot].linkIds.push(link.id);
            } else {
                const sourceNode = this._nodes.find(node => typedEqual(node.id, link.origin_id));
                sourceNode.outputs[link.origin_slot].links.push(link.id);
            }
            if (typedEqual(link.target_id, -20)) {
                this.outputs[link.target_slot].linkIds.push(link.id);
            } else {
                const targetNode = this._nodes.find(node => typedEqual(node.id, link.target_id));
                targetNode.inputs[link.target_slot].link = link.id;
            }
        }
    }

    addRuntimeLink(sourceId, sourceSlot, targetId, targetSlot) {
        this.metrics.runtimeConnects += 1;
        if (this.metrics.runtimeConnects === this.failRuntimeConnectAt) {
            throw new Error("forced native Subgraph boundary connect failure");
        }
        this.state.lastLinkId += 1;
        const link = {
            id: this.state.lastLinkId,
            origin_id: sourceId,
            origin_slot: sourceSlot,
            target_id: targetId,
            target_slot: targetSlot,
            type: "IMAGE",
        };
        this.links.set(link.id, link);
        this.refreshManifests();
        return link;
    }

    removeLink(id) {
        this.links.delete(id);
        this.refreshManifests();
    }

    add(node) {
        this.state.lastNodeId += 1;
        node.runtimeStringIds = this.runtimeStringIds;
        node.id = this.runtimeStringIds
            ? String(this.state.lastNodeId)
            : this.state.lastNodeId;
        node.graph = this;
        this._nodes.push(node);
        this.refreshManifests();
    }

    remove(node) {
        for (const link of [...this.links.values()]) {
            if (typedEqual(link.origin_id, node.id) || typedEqual(link.target_id, node.id)) {
                this.links.delete(link.id);
            }
        }
        this._nodes = this._nodes.filter(item => item !== node);
        this.refreshManifests();
    }

    asSerialisable() {
        const definition = {
            ...clone(this.base),
            state: clone(this.state),
            inputNode: clone(this.base.inputNode),
            outputNode: clone(this.base.outputNode),
            inputs: this.inputs.map(port => ({
                id: port.id, name: port.name, type: port.type, linkIds: clone(port.linkIds),
            })),
            outputs: this.outputs.map(port => ({
                id: port.id, name: port.name, type: port.type, linkIds: clone(port.linkIds),
            })),
            nodes: this._nodes.map(node => node.serialize()),
            links: [...this.links.values()].map(link => [
                link.id,
                this.runtimeStringIds && Number(link.origin_id) >= 0
                    ? Number(link.origin_id)
                    : link.origin_id,
                link.origin_slot,
                this.runtimeStringIds && Number(link.target_id) >= 0
                    ? Number(link.target_id)
                    : link.target_id,
                link.target_slot,
                link.type,
            ]),
        };
        if (this.nativeUndefinedBoundaryPinned && this.id === "scope-def") {
            definition.inputNode.pinned = undefined;
            definition.outputNode.pinned = undefined;
            definition.reroutes = undefined;
            for (const port of [...definition.inputs, ...definition.outputs]) {
                port.localized_name = undefined;
                port.label = undefined;
                port.dir = undefined;
                port.shape = undefined;
                port.color_off = undefined;
                port.color_on = undefined;
                port.pos = undefined;
            }
            definition.nodes[0].showAdvanced = undefined;
            definition.nodes[0].inputs[0].slot_index = undefined;
            definition.nodes[0].outputs[0].pos = undefined;
        }
        if (this.nativeDefinedOptionalFields && this.id === "scope-def") {
            definition.inputNode.pinned = true;
            definition.outputNode.pinned = true;
            definition.inputs[0].label = "Defined input label";
            definition.inputs[0].dir = 1;
            definition.outputs[0].color_on = "#abcdef";
            definition.outputs[0].pos = [490, 50];
        }
        if (this.nativeInvalidArray && this.id === "scope-def") {
            definition.extra.nativeInvalidArray = [undefined];
        }
        return definition;
    }
}


class NativeRootGraph {
    constructor(
        snapshot,
        metrics,
        runtimeStringIds = false,
        nativeUndefinedBoundaryPinned = false,
        nativeInvalidArray = false,
        nativeDefinedOptionalFields = false,
    ) {
        this.base = clone(snapshot);
        this.extra = clone(snapshot.extra || {});
        this.state = {
            lastNodeId: snapshot.last_node_id,
            lastLinkId: snapshot.last_link_id,
            lastGroupId: 0,
            lastRerouteId: 0,
        };
        this.definitionOrder = snapshot.definitions.subgraphs.map(item => item.id);
        this.subgraphs = new Map(snapshot.definitions.subgraphs.map(definition => (
            [definition.id, new NativeSubgraph(
                definition,
                this.state,
                metrics,
                runtimeStringIds,
                nativeUndefinedBoundaryPinned,
                nativeInvalidArray,
                nativeDefinedOptionalFields,
            )]
        )));
        for (const graph of this.subgraphs.values()) {
            for (const node of graph._nodes) {
                if (this.subgraphs.has(node.type)) node.subgraph = this.subgraphs.get(node.type);
            }
        }
        this._nodes = snapshot.nodes.map(raw => new NativeScopeNode(raw, runtimeStringIds));
        for (const node of this._nodes) {
            if (this.subgraphs.has(node.type)) node.subgraph = this.subgraphs.get(node.type);
        }
        this.links = new Map();
    }

    serialize() {
        return {
            ...clone(this.base),
            last_node_id: this.state.lastNodeId,
            last_link_id: this.state.lastLinkId,
            nodes: this._nodes.map(node => node.serialize()),
            links: [],
            definitions: {
                subgraphs: this.definitionOrder.map(id => this.subgraphs.get(id).asSerialisable()),
            },
            extra: clone(this.extra),
        };
    }

    change() {}
    setDirtyCanvas() {}
}


function schemaContext(key, nodeType, schemaHash) {
    return {
        key,
        ref: key.startsWith("new:")
            ? { alias: key.slice(4) }
            : { node_id: Number(key.slice(9)) },
        node_type: nodeType,
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
    };
}


async function nativeRegisteredFixture({
    failConnectAt = null,
    runtimeStringIds = false,
    nativeUndefinedBoundaryPinned = false,
    nativeInvalidArray = false,
    nativeDefinedOptionalFields = false,
} = {}) {
    const metrics = {
        runtimeConnects: 0,
        inputBoundaryConnects: 0,
        outputBoundaryConnects: 0,
        nodeDisconnects: 0,
        outputBoundaryDisconnects: 0,
        restores: 0,
    };
    const workflow = { key: "native-scoped-workflow", changeTracker: { changeCount: 0 } };
    const workflowStore = { activeWorkflow: workflow };
    const canvas = {
        read_only: false,
        emitBeforeChange() {},
        emitAfterChange() {},
        setDirty() {},
    };
    const app = {
        canvas,
        extensionManager: { workflow: workflowStore },
        async loadGraphData(snapshot, _clean, _view, activeWorkflow) {
            metrics.restores += 1;
            const rebuilt = new NativeRootGraph(
                snapshot,
                metrics,
                runtimeStringIds,
                nativeUndefinedBoundaryPinned,
                nativeInvalidArray,
                nativeDefinedOptionalFields,
            );
            this.graph = rebuilt;
            this.rootGraph = rebuilt;
            workflowStore.activeWorkflow = activeWorkflow;
        },
    };
    const initialRoot = new NativeRootGraph(
        rootWorkflow(),
        metrics,
        runtimeStringIds,
        nativeUndefinedBoundaryPinned,
        nativeInvalidArray,
        nativeDefinedOptionalFields,
    );
    app.graph = initialRoot;
    app.rootGraph = initialRoot;
    const FL_API = await loadBrowserClass("web/js/fl_api.js", "FL_API", {
        app,
        api: { dispatchCustomEvent() {} },
        LiteGraph: {
            createNode(nodeType) {
                if (nodeType !== "Replacement") return null;
                return new NativeScopeNode(rawNode(null, nodeType, REPLACEMENT_HASH));
            },
        },
        GRAPH_PRECONDITION_SCHEMA: "fl-mcp.workflow-graph-hash.v1",
        canonicalWorkflowJSON,
        workflowGraphHash,
        workflowGraphHashExcludingExtra,
        nodeIdsEqual: typedEqual,
        findNonOverlappingPosition: value => ({ x: value.x, y: value.y }),
        getGraphInsertionOrigin: () => ({ x: 0, y: 0 }),
        convertComfyWidgetToInput: async () => false,
        formatImageWidgetRef: value => String(value),
        nestedImageRefForNode: () => null,
        normalizeMaskRegion: value => value,
        parseImageWidgetRef: () => null,
        summarizeMaskPixels: () => ({}),
    });
    const flApi = new FL_API();
    flApi.pauseAutoQueue = () => ({ mode: "disabled" });
    flApi.restoreAutoQueue = () => {};
    flApi.restoreNestedImageReferences = () => {};
    flApi.getNodeDefinitions = async () => ({ Pass: {}, Replacement: {} });
    const identity = flApi.getActiveWorkflowIdentity();
    const requestHarness = {
        captureWorkflow: () => app.graph.serialize(),
        definition: () => jsonTransportClone(
            app.rootGraph.subgraphs.get("scope-def").asSerialisable(),
        ),
    };
    const request = await replaceRequest(requestHarness);
    request.plan.expected_workflow_identity = identity;
    if (failConnectAt !== null) {
        app.rootGraph.subgraphs.get("scope-def").failRuntimeConnectAt = failConnectAt;
    }
    const contexts = new Map([
        ["existing:1", schemaContext("existing:1", "Pass", PASS_HASH)],
        ["new:replacement", schemaContext("new:replacement", "Replacement", REPLACEMENT_HASH)],
    ]);
    const ToolExecutor = await loadBrowserClass("web/js/tool_executor.js", "ToolExecutor", {
        FL_API: class {},
        QueryExecutor: class {},
        WORKFLOW_APPLICATION_PROPERTY: "application",
        WORKFLOW_REFINEMENT_PROPERTY: "refinement",
        WORKFLOW_GRAPH_PATCH_PROPERTY: "fl_mcp_workflow_graph_patch",
        applyWorkflowPlanAtomic: async () => ({}),
        applyWorkflowRefinementAtomic: async () => ({}),
        applyWorkflowGraphPatchAtomic,
        buildGraphPatchSchemaContexts: async () => contexts,
        enrichGraphPatchNode,
        setTimeout: callback => { callback(); return 1; },
    });
    const executor = Object.create(ToolExecutor.prototype);
    executor.flApi = flApi;
    executor.queryExecutor = {};
    executor.toolHandlers = executor._registerHandlers();
    return { app, executor, flApi, metrics, request };
}


test("registered ToolExecutor applies GraphPatch v3 through the actual native FL_API scope runtime", async () => {
    const fixture = await nativeRegisteredFixture();
    const siblingBefore = fixture.app.rootGraph.subgraphs.get("sibling-def").asSerialisable();

    const result = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(result.success, true, JSON.stringify(result.error));
    assert.equal(result.patch_schema, SCOPED_WORKFLOW_GRAPH_PATCH_SCHEMA);
    assert.deepEqual(result.aliases, { replacement: 301 });
    assert.equal(fixture.metrics.nodeDisconnects, 1);
    assert.equal(fixture.metrics.outputBoundaryDisconnects, 1);
    assert.equal(fixture.metrics.inputBoundaryConnects, 1);
    assert.equal(fixture.metrics.outputBoundaryConnects, 1);
    const selected = fixture.app.rootGraph.subgraphs.get("scope-def").asSerialisable();
    assert.deepEqual(selected.nodes.map(node => [node.id, node.type]), [[301, "Replacement"]]);
    assert.deepEqual(selected.links.map(parseLink).map(link => [link.sourceId, link.targetId]), [
        [-10, 301],
        [301, -20],
    ]);
    const serialized = fixture.app.graph.serialize();
    for (const definition of serialized.definitions.subgraphs) {
        assert.equal(definition.state.lastNodeId, serialized.last_node_id);
        assert.equal(definition.state.lastLinkId, serialized.last_link_id);
    }
    const siblingAfter = fixture.app.rootGraph.subgraphs.get("sibling-def").asSerialisable();
    siblingBefore.state.lastNodeId = serialized.last_node_id;
    siblingBefore.state.lastLinkId = serialized.last_link_id;
    assert.deepEqual(siblingAfter, siblingBefore);
    assert.ok(serialized.extra[GRAPH_PATCH_LEDGER_KEY]);

    const runtimeConnects = fixture.metrics.runtimeConnects;
    const retry = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);
    assert.equal(retry.success, true, JSON.stringify(retry.error));
    assert.equal(retry.already_applied, true);
    assert.equal(fixture.metrics.runtimeConnects, runtimeConnects);
});


test("native scoped apply omits optional undefined boundary fields exactly like JSON transport", async () => {
    const fixture = await nativeRegisteredFixture({
        nativeUndefinedBoundaryPinned: true,
    });
    const nativeBefore = fixture.app.rootGraph.subgraphs.get("scope-def").asSerialisable();
    assert.equal(Object.hasOwn(nativeBefore.inputNode, "pinned"), true);
    assert.equal(nativeBefore.inputNode.pinned, undefined);
    assert.equal(Object.hasOwn(nativeBefore.outputNode, "pinned"), true);
    assert.equal(nativeBefore.outputNode.pinned, undefined);
    assert.equal(Object.hasOwn(nativeBefore, "reroutes"), true);
    assert.equal(nativeBefore.reroutes, undefined);
    assert.equal(nativeBefore.inputs[0].label, undefined);
    assert.equal(nativeBefore.outputs[0].color_on, undefined);
    assert.equal(nativeBefore.nodes[0].showAdvanced, undefined);
    assert.equal(nativeBefore.nodes[0].inputs[0].slot_index, undefined);

    const first = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);
    const retry = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(first.success, true, JSON.stringify(first.error));
    assert.equal(retry.success, true, JSON.stringify(retry.error));
    assert.equal(retry.already_applied, true);
    const nativeAfter = fixture.app.rootGraph.subgraphs.get("scope-def").asSerialisable();
    assert.equal(Object.hasOwn(nativeAfter.inputNode, "pinned"), true);
    assert.equal(nativeAfter.inputNode.pinned, undefined);
    assert.equal(Object.hasOwn(nativeAfter.outputNode, "pinned"), true);
    assert.equal(nativeAfter.outputNode.pinned, undefined);
    assert.equal(nativeAfter.reroutes, undefined);
    assert.equal(nativeAfter.inputs[0].label, undefined);
    assert.equal(nativeAfter.outputs[0].color_on, undefined);
    assert.equal(nativeAfter.nodes[0].showAdvanced, undefined);
    assert.equal(nativeAfter.nodes[0].inputs[0].slot_index, undefined);
});


test("native scoped projection preserves defined optional fields exactly", async () => {
    const fixture = await nativeRegisteredFixture({ nativeDefinedOptionalFields: true });
    const before = fixture.app.rootGraph.subgraphs.get("scope-def").asSerialisable();
    assert.equal(before.inputNode.pinned, true);
    assert.equal(before.outputNode.pinned, true);
    assert.equal(before.inputs[0].label, "Defined input label");
    assert.equal(before.inputs[0].dir, 1);
    assert.equal(before.outputs[0].color_on, "#abcdef");
    assert.deepEqual(before.outputs[0].pos, [490, 50]);
    assert.deepEqual(before.reroutes, [{ id: 9, pos: [7, 8], sentinel: "scope-def" }]);

    const result = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(result.success, true, JSON.stringify(result.error));
    const after = fixture.app.rootGraph.subgraphs.get("scope-def").asSerialisable();
    assert.equal(after.inputNode.pinned, true);
    assert.equal(after.outputNode.pinned, true);
    assert.equal(after.inputs[0].label, "Defined input label");
    assert.equal(after.inputs[0].dir, 1);
    assert.equal(after.outputs[0].color_on, "#abcdef");
    assert.deepEqual(after.outputs[0].pos, [490, 50]);
    assert.deepEqual(after.reroutes, [{ id: 9, pos: [7, 8], sentinel: "scope-def" }]);
});


test("native scoped projection rejects undefined array entries before mutation", async () => {
    const fixture = await nativeRegisteredFixture({ nativeInvalidArray: true });

    const result = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "non_json_scoped_definition");
    assert.equal(fixture.metrics.runtimeConnects, 0);
    assert.equal(fixture.metrics.nodeDisconnects, 0);
    assert.equal(fixture.metrics.outputBoundaryDisconnects, 0);
});


test("native scoped projection classifies over-depth metadata before mutation", async () => {
    const fixture = await nativeRegisteredFixture();
    let deep = { leaf: true };
    for (let index = 0; index < 70; index += 1) deep = { child: deep };
    fixture.app.rootGraph.subgraphs.get("scope-def").base.extra.deep = deep;
    fixture.request.plan.expected_graph_hash = await workflowGraphHash(
        fixture.app.graph.serialize(),
    );
    fixture.request.plan.scope.definition_hash = "e".repeat(64);

    const result = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "scoped_definition_hash_depth_exceeded");
    assert.equal(fixture.metrics.runtimeConnects, 0);
    assert.equal(fixture.metrics.nodeDisconnects, 0);
    assert.equal(fixture.metrics.outputBoundaryDisconnects, 0);
});


test("native scoped projection meters omitted undefined object members", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness);
    const flood = {};
    for (let index = 0; index < 200_001; index += 1) {
        flood[`optional_${index}`] = undefined;
    }
    const resolveScopedGraph = harness.resolveScopedGraph.bind(harness);
    harness.resolveScopedGraph = descriptor => {
        const adapter = resolveScopedGraph(descriptor);
        const nativeDefinition = adapter.captureDefinition();
        nativeDefinition.extra.undefinedFlood = flood;
        return {
            ...adapter,
            captureDefinition: () => nativeDefinition,
        };
    };

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "scoped_definition_hash_fact_limit_exceeded");
    assert.equal(harness.mutations, 0);
});


test("scoped GraphPatch projects serialized integer IDs across live string-node replace and retry", async () => {
    const fixture = await nativeRegisteredFixture({ runtimeStringIds: true });
    const beforeScope = fixture.app.rootGraph.subgraphs.get("scope-def");
    assert.deepEqual(beforeScope._nodes.map(node => node.id), ["1"]);

    const first = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);
    const retry = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(first.success, true, JSON.stringify(first.error));
    assert.deepEqual(first.aliases, { replacement: 301 });
    assert.deepEqual(first.created_node_ids, [301]);
    assert.deepEqual(first.removed_node_ids, [1]);
    const afterScope = fixture.app.rootGraph.subgraphs.get("scope-def");
    assert.deepEqual(afterScope._nodes.map(node => node.id), ["301"]);
    assert.deepEqual(afterScope.asSerialisable().nodes.map(node => node.id), [301]);
    assert.deepEqual(afterScope.asSerialisable().links.map(parseLink).map(link => (
        [link.sourceId, link.targetId]
    )), [[-10, 301], [301, -20]]);
    assert.equal(retry.success, true, JSON.stringify(retry.error));
    assert.equal(retry.already_applied, true);
    assert.equal(afterScope._nodes.length, 1);
});


test("actual native scoped connection failure restores the exact full root through ToolExecutor", async () => {
    const fixture = await nativeRegisteredFixture({ failConnectAt: 2 });
    const before = fixture.app.graph.serialize();

    const result = await fixture.executor.toolHandlers.apply_workflow_graph_patch(fixture.request);

    assert.equal(result.success, false);
    assert.equal(result.rollback.complete, true);
    assert.equal(fixture.metrics.restores, 1);
    assert.deepEqual(fixture.app.graph.serialize(), before);
});


test("GraphPatch v3 replaces a unique nested scope through both boundaries and retries idempotently", async () => {
    const harness = new ScopedHarness();
    const siblingBefore = clone(harness.definition("sibling-def"));
    const outerBefore = clone(harness.definition("outer-def"));
    const request = await replaceRequest(harness);

    const first = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(first.success, true, JSON.stringify(first.error));
    assert.equal(first.patch_schema, SCOPED_WORKFLOW_GRAPH_PATCH_SCHEMA);
    assert.deepEqual(first.aliases, { replacement: 301 });
    assert.deepEqual(first.created_node_ids, [301]);
    assert.deepEqual(first.removed_node_ids, [1]);
    siblingBefore.state.lastNodeId = 301;
    siblingBefore.state.lastLinkId = 4;
    outerBefore.state.lastNodeId = 301;
    outerBefore.state.lastLinkId = 4;
    assert.deepEqual(harness.definition("sibling-def"), siblingBefore);
    assert.deepEqual(harness.definition("outer-def"), outerBefore);
    assert.deepEqual(harness.definition().nodes.map(node => [node.id, node.type]), [[301, "Replacement"]]);
    assert.deepEqual(harness.definition().links.map(parseLink).map(link => (
        [link.sourceId, link.targetId]
    )), [[-10, 301], [301, -20]]);
    assert.deepEqual(harness.definition().groups, [{
        title: "scope-def-group", bounding: [1, 2, 3, 4],
    }]);
    assert.deepEqual(harness.definition().reroutes, [{ id: 9, pos: [7, 8], sentinel: "scope-def" }]);
    assert.deepEqual(harness.workflow.extra.sentinel, "root-extra");

    const mutationCount = harness.mutations;
    const second = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(second.success, true, JSON.stringify(second.error));
    assert.equal(second.applied, false);
    assert.equal(second.already_applied, true);
    assert.equal(harness.mutations, mutationCount);
});


test("GraphPatch v3 clones an isolated scoped node without changing either boundary edge", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness, {
        create_nodes: [{
            alias: "replacement",
            node_type: "Replacement",
            schema_hash: REPLACEMENT_HASH,
            values: {},
            layout_hint: { x: 600, y: 200, width: 260, height: 150 },
        }],
        remove_edges: [],
        add_edges: [],
        remove_nodes: [],
        expected_delta: {
            created_node_count: 1,
            updated_node_count: 0,
            removed_node_count: 0,
            added_edge_count: 0,
            removed_edge_count: 0,
            final_node_count: 2,
            final_edge_count: 2,
        },
    });

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, true, JSON.stringify(result.error));
    assert.deepEqual(harness.definition().nodes.map(node => [node.id, node.type]), [
        [1, "Pass"],
        [301, "Replacement"],
    ]);
    assert.deepEqual(harness.definition().links.map(parseLink).map(link => [link.sourceId, link.targetId]), [
        [-10, 1],
        [1, -20],
    ]);
});


test("GraphPatch v3 removes only the declared isolated node from a nested definition", async () => {
    const harness = new ScopedHarness();
    const isolated = rawNode(2, "Pass", PASS_HASH);
    isolated.pos = [700, 300];
    harness.definition().nodes.push(isolated);
    const request = await replaceRequest(harness, {
        assertions: {
            nodes: [{ ref: { node_id: 2 }, node_type: "Pass", schema_hash: PASS_HASH }],
            edges: [],
        },
        create_nodes: [],
        remove_edges: [],
        add_edges: [],
        remove_nodes: [{
            ref: { node_id: 2 },
            node_type: "Pass",
            schema_hash: PASS_HASH,
            expected_incident_edges: [],
        }],
        expected_delta: {
            created_node_count: 0,
            updated_node_count: 0,
            removed_node_count: 1,
            added_edge_count: 0,
            removed_edge_count: 0,
            final_node_count: 1,
            final_edge_count: 2,
        },
    });

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, true, JSON.stringify(result.error));
    assert.deepEqual(harness.definition().nodes.map(node => node.id), [1]);
    assert.deepEqual(harness.definition().links.map(parseLink).map(link => [link.sourceId, link.targetId]), [
        [-10, 1],
        [1, -20],
    ]);
});


test("GraphPatch v3 requires explicit shared mode and the complete reused-instance path set", async () => {
    const secondPath = [{ container_node_id: 400, subgraph_id: "scope-def" }];
    for (const editMode of ["instance", "shared_definition"]) {
        const harness = new ScopedHarness();
        harness.workflow.nodes.push(containerNode(400, "scope-def"));
        const request = await replaceRequest(harness);
        request.plan.scope.edit_mode = editMode;
        request.plan.scope.affected_scope_paths = [path(), secondPath];

        const result = await applyWorkflowGraphPatchAtomic(request, harness);

        if (editMode === "instance") {
            assert.equal(result.success, false);
            assert.equal(result.error.code, "instance_detach_not_supported");
            assert.equal(harness.mutations, 0);
        } else {
            assert.equal(result.success, true, JSON.stringify(result.error));
            assert.deepEqual(harness.definition().nodes.map(node => node.type), ["Replacement"]);
        }
    }
});


test("GraphPatch v3 rejects stale path, definition, and boundary facts before any effect", async () => {
    for (const mutate of [
        request => { request.plan.scope.scope_path[1].container_node_id = 999; },
        request => { request.plan.scope.definition_hash = "e".repeat(64); },
        request => {
            const wrong = "00000000-0000-7000-8000-000000000099";
            for (const collection of [request.plan.assertions.edges, request.plan.remove_edges]) {
                collection[0].source.ref.scope_input.slot_id = wrong;
            }
        },
    ]) {
        const harness = new ScopedHarness();
        const request = await replaceRequest(harness);
        mutate(request);
        const result = await applyWorkflowGraphPatchAtomic(request, harness);
        assert.equal(result.success, false);
        assert.equal(result.rollback.attempted, false);
        assert.equal(harness.mutations, 0);
    }
});


test("GraphPatch v3 closes a root race before scoped effects", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness);
    harness.raceOnResolve = true;

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "concurrent_workflow_edit");
    assert.equal(result.rollback.attempted, false);
    assert.equal(harness.mutations, 0);
});


test("GraphPatch v3 refuses unsafe shared counters before scoped effects", async () => {
    const harness = new ScopedHarness();
    const unsafe = Number.MAX_SAFE_INTEGER + 1;
    harness.workflow.last_node_id = unsafe;
    for (const definition of harness.workflow.definitions.subgraphs) {
        definition.state.lastNodeId = unsafe;
    }
    const request = await replaceRequest(harness);

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "scoped_shared_counter_mismatch");
    assert.equal(result.rollback.attempted, false);
    assert.equal(harness.mutations, 0);
});


test("GraphPatch v3 restores the complete root after a native scoped failure", async () => {
    const harness = new ScopedHarness();
    const before = harness.captureWorkflow();
    const request = await replaceRequest(harness);
    harness.failConnectAt = 2;

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, false);
    assert.equal(result.rollback.complete, true);
    assert.equal(harness.restoreCalls, 1);
    assert.deepEqual(harness.captureWorkflow(), before);
});


test("GraphPatch v3 detects sibling definition counter damage and rolls back", async () => {
    const harness = new ScopedHarness();
    const before = harness.captureWorkflow();
    const request = await replaceRequest(harness);
    harness.sabotageSiblingCounter = true;

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "scoped_shared_counter_mismatch");
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(harness.captureWorkflow(), before);
});


test("GraphPatch v3 rejects conflicting mapping-link aliases and boundary overflow pre-effect", async () => {
    const exactMapping = {
        id: 1,
        link_id: 1,
        origin_id: -10,
        source_id: -10,
        source_node_id: -10,
        from_node_id: -10,
        origin_slot: 0,
        source_slot: 0,
        source_output_index: 0,
        target_id: 1,
        target_node_id: 1,
        to_node_id: 1,
        target_slot: 0,
        target_input_index: 0,
        type: "IMAGE",
        link_type: "IMAGE",
    };
    for (const malformedLink of [
        { ...exactMapping, link_id: 9 },
        { ...exactMapping, source_node_id: 9 },
        { ...exactMapping, source_output_index: 9 },
        { ...exactMapping, target_node_id: 9 },
        { ...exactMapping, target_input_index: 9 },
        { ...exactMapping, link_type: "MASK" },
        [1, -10, 3, 1, 0, "IMAGE"],
    ]) {
        const harness = new ScopedHarness();
        harness.definition().links[0] = malformedLink;
        harness.definition().inputs[0].linkIds = [1];
        const request = await replaceRequest(harness);
        const result = await applyWorkflowGraphPatchAtomic(request, harness);
        assert.equal(result.success, false);
        assert.equal(result.rollback.attempted, false);
        assert.equal(harness.mutations, 0);
    }
});


test("GraphPatch v3 accepts identical typed mapping-link aliases", async () => {
    const harness = new ScopedHarness();
    harness.definition().links[0] = {
        id: 1,
        link_id: 1,
        origin_id: -10,
        source_id: -10,
        source_node_id: -10,
        from_node_id: -10,
        origin_slot: 0,
        source_slot: 0,
        source_output_index: 0,
        target_id: 1,
        target_node_id: 1,
        to_node_id: 1,
        target_slot: 0,
        target_input_index: 0,
        type: "IMAGE",
        link_type: "IMAGE",
    };
    const request = await replaceRequest(harness);

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, true, JSON.stringify(result.error));
});


test("GraphPatch v3 rejects public private-boundary refs and wrong-direction boundaries", async () => {
    for (const mutate of [
        request => { request.plan.add_edges[0].source.ref = { node_id: GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID }; },
        request => { request.plan.add_edges[0].source.ref = { scope_output: boundary("scope_output") }; },
        request => { request.plan.add_edges[1].target.ref = { node_id: GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID }; },
    ]) {
        const harness = new ScopedHarness();
        const request = await replaceRequest(harness);
        mutate(request);
        const result = await applyWorkflowGraphPatchAtomic(request, harness);
        assert.equal(result.success, false);
        assert.equal(harness.mutations, 0);
    }
});


test("GraphPatch v3 accepts backend-valid canonical UUID versions beyond v1-v5", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness);

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, true, JSON.stringify(result.error));
});


test("GraphPatch v3 refuses a direct private boundary bypass even after normalization", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness, {
        create_nodes: [],
        add_edges: [edge(
            { scope_input: boundary("scope_input") },
            { scope_output: boundary("scope_output") },
        )],
        remove_nodes: [{
            ref: { node_id: 1 },
            node_type: "Pass",
            schema_hash: PASS_HASH,
            expected_incident_edges: [
                edge({ scope_input: boundary("scope_input") }, { node_id: 1 }),
                edge({ node_id: 1 }, { scope_output: boundary("scope_output") }),
            ],
        }],
        expected_delta: {
            created_node_count: 0,
            updated_node_count: 0,
            removed_node_count: 1,
            added_edge_count: 1,
            removed_edge_count: 2,
            final_node_count: 0,
            final_edge_count: 1,
        },
    });

    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "direct_scope_boundary_edge_unsupported");
    assert.equal(result.rollback.attempted, false);
    assert.equal(harness.mutations, 0);
});


test("scoped ledger is committed only after exact definition verification", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness);
    const result = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(result.success, true);
    const entry = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY].entries[request.application_id];
    assert.match(entry.result_definition_hash, /^[0-9a-f]{64}$/);
    assert.equal(entry.result_definition_hash, await workflowScopeDefinitionHash(harness.definition()));
});


test("imported GraphPatch ledgers reject duplicate, missing, orphan, oversized, and malformed entries", async () => {
    const original = new ScopedHarness();
    const request = await replaceRequest(original);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, original)).success, true);
    const baseline = original.captureWorkflow();
    const targetId = request.application_id;
    const targetEntry = baseline.extra[GRAPH_PATCH_LEDGER_KEY].entries[targetId];
    const corruptions = [
        ledger => { ledger.order.push(targetId); },
        ledger => { ledger.order.push("missing-ledger-entry"); },
        ledger => { ledger.entries["orphan-ledger-entry"] = clone(targetEntry); },
        ledger => {
            for (let index = 0; index < 64; index += 1) {
                const id = `bounded-ledger-entry-${String(index).padStart(3, "0")}`;
                ledger.order.push(id);
                ledger.entries[id] = clone(targetEntry);
            }
        },
        ledger => {
            const id = "malformed-ledger-entry";
            ledger.order.push(id);
            ledger.entries[id] = { patch_hash: PATCH_HASH };
        },
        ledger => {
            ledger.entries[targetId].removed_node_ids = Array.from(
                { length: 101 },
                (_, index) => index + 1,
            );
        },
        ledger => { ledger.entries[targetId].removed_node_ids = ["x".repeat(4_097)]; },
    ];

    for (const corrupt of corruptions) {
        const harness = new ScopedHarness(baseline);
        corrupt(harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY]);
        const result = await applyWorkflowGraphPatchAtomic(request, harness);
        assert.equal(result.success, false);
        assert.equal(result.error.code, "invalid_graph_patch_ledger");
        assert.equal(result.rollback.attempted, false);
        assert.equal(harness.mutations, 0);
    }
});


test("idempotent retry requires ledger aliases and node ID arrays to match the plan exactly", async () => {
    const original = new ScopedHarness();
    const request = await replaceRequest(original);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, original)).success, true);
    const baseline = original.captureWorkflow();
    const corruptions = [
        entry => { entry.aliases = {}; entry.created_node_ids = []; },
        entry => { entry.aliases.extra = 99; entry.created_node_ids.push(99); },
        entry => { entry.removed_node_ids = []; },
    ];

    for (const corrupt of corruptions) {
        const harness = new ScopedHarness(baseline);
        corrupt(harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY].entries[request.application_id]);
        const result = await applyWorkflowGraphPatchAtomic(request, harness);
        assert.equal(result.success, false);
        assert.equal(result.error.code, "graph_patch_idempotency_conflict");
        assert.equal(harness.mutations, 0);
    }
});


test("a forged scoped ledger cannot hide wrong created layout or definition state", async () => {
    const original = new ScopedHarness();
    const request = await replaceRequest(original);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, original)).success, true);
    const baseline = original.captureWorkflow();
    for (const corrupt of [
        harness => { harness.definition().nodes[0].pos[0] += 1; },
        harness => { harness.definition().nodes[0].type = "Pass"; },
    ]) {
        const harness = new ScopedHarness(baseline);
        corrupt(harness);
        const ledger = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
        const entry = ledger.entries[request.application_id];
        entry.result_definition_hash = await workflowScopeDefinitionHash(harness.definition());
        const content = harness.captureWorkflow();
        delete content.extra[GRAPH_PATCH_LEDGER_KEY];
        entry.result_content_hash = await workflowGraphHash(content);

        const result = await applyWorkflowGraphPatchAtomic(request, harness);

        assert.equal(result.success, false);
        assert.equal(result.error.code, "graph_patch_idempotency_conflict");
        assert.equal(harness.mutations, 0);
    }
});


test("a forged scoped retry cannot hide retained asserted node or edge substitution", async () => {
    const original = new ScopedHarness();
    const retained = addRetainedScopedConnection(original);
    const request = await replaceRequest(original);
    request.plan.assertions.nodes.push(
        { ref: { node_id: 2 }, node_type: "Pass", schema_hash: PASS_HASH },
        { ref: { node_id: 3 }, node_type: "Pass", schema_hash: PASS_HASH },
    );
    request.plan.assertions.edges.push(retained);
    request.plan.expected_delta.final_node_count = 3;
    request.plan.expected_delta.final_edge_count = 3;
    assert.equal((await applyWorkflowGraphPatchAtomic(request, original)).success, true);
    const baseline = original.captureWorkflow();
    const corruptions = [
        {
            code: "graph_patch_asserted_node_mismatch",
            apply(harness) {
                harness.definition().nodes.find(node => typedEqual(node.id, 2)).schema_hash = "f".repeat(64);
            },
        },
        {
            code: "graph_patch_asserted_edge_mismatch",
            apply(harness) {
                const retainedLink = harness.definition().links.find(raw => parseLink(raw).id === 3);
                retainedLink[1] = 3;
                retainedLink[3] = 2;
                harness.refreshManifests();
            },
        },
    ];

    for (const corruption of corruptions) {
        const harness = new ScopedHarness(baseline);
        corruption.apply(harness);
        const ledger = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
        const entry = ledger.entries[request.application_id];
        entry.result_definition_hash = await workflowScopeDefinitionHash(harness.definition());
        const content = harness.captureWorkflow();
        delete content.extra[GRAPH_PATCH_LEDGER_KEY];
        entry.result_content_hash = await workflowGraphHash(content);

        const retry = await applyWorkflowGraphPatchAtomic(request, harness);

        assert.equal(retry.success, false);
        assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
        assert.ok(retry.error.details.issues.some(issue => issue.code === corruption.code));
        assert.equal(harness.mutations, 0);
    }
});


test("a forged scoped retry cannot attach an undeclared edge to a created node", async () => {
    const harness = new ScopedHarness();
    addRetainedScopedConnection(harness);
    const request = await replaceRequest(harness);
    request.plan.expected_delta.final_node_count = 3;
    request.plan.expected_delta.final_edge_count = 3;
    assert.equal((await applyWorkflowGraphPatchAtomic(request, harness)).success, true);
    const mutationCount = harness.mutations;

    const ledger = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const entry = ledger.entries[request.application_id];
    const replacementId = entry.aliases.replacement;
    const retainedLink = harness.definition().links.find(raw => parseLink(raw).id === 3);
    retainedLink[1] = replacementId;
    harness.refreshManifests();
    entry.result_definition_hash = await workflowScopeDefinitionHash(harness.definition());
    const content = harness.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);

    const retry = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_created_incident_edge_mismatch"
        && issue.alias === "replacement"
    )));
    assert.equal(harness.mutations, mutationCount);
});


test("a substantive scoped ledger-only import cannot claim an unchanged pre-patch definition", async () => {
    const authority = new ScopedHarness();
    const request = await replaceRequest(authority);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, authority)).success, true);
    const forged = new ScopedHarness();
    forged.workflow.extra[GRAPH_PATCH_LEDGER_KEY] = clone(
        authority.workflow.extra[GRAPH_PATCH_LEDGER_KEY],
    );
    const entry = forged.workflow.extra[GRAPH_PATCH_LEDGER_KEY]
        .entries[request.application_id];
    entry.result_content_hash = request.plan.expected_graph_hash;
    entry.result_definition_hash = request.plan.scope.definition_hash;

    const retry = await applyWorkflowGraphPatchAtomic(request, forged);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_created_node_mismatch"
        || issue.code === "graph_patch_removed_node_present"
        || issue.code === "graph_patch_edge_mismatch"
    )));
    assert.equal(forged.mutations, 0);
});


test("a same-value and same-layout scoped update retries idempotently", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness, {
        create_nodes: [],
        update_nodes: [{
            ref: { node_id: 1 },
            node_type: "Pass",
            schema_hash: PASS_HASH,
            expected_values: {},
            set_values: {},
            layout_hint: { x: 40, y: 60, width: 220, height: 120 },
        }],
        remove_edges: [],
        add_edges: [],
        remove_nodes: [],
        expected_delta: {
            created_node_count: 0,
            updated_node_count: 1,
            removed_node_count: 0,
            added_edge_count: 0,
            removed_edge_count: 0,
            final_node_count: 1,
            final_edge_count: 2,
        },
    });

    const first = await applyWorkflowGraphPatchAtomic(request, harness);
    assert.equal(first.success, true, JSON.stringify(first.error));
    const entry = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY].entries[request.application_id];
    const second = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(entry.result_content_hash, request.plan.expected_graph_hash);
    assert.equal(entry.result_definition_hash, request.plan.scope.definition_hash);
    assert.equal(second.success, true, JSON.stringify(second.error));
    assert.equal(second.already_applied, true);
});


test("scoped retry proves a retained public boundary assertion after v3 lowering", async () => {
    const harness = new ScopedHarness();
    harness.definition().nodes.push(rawNode(2, "Pass", PASS_HASH));
    harness.refreshManifests();
    const request = await replaceRequest(harness, {
        remove_edges: [],
        add_edges: [],
        remove_nodes: [],
        expected_delta: {
            created_node_count: 1,
            updated_node_count: 0,
            removed_node_count: 0,
            added_edge_count: 0,
            removed_edge_count: 0,
            final_node_count: 3,
            final_edge_count: 2,
        },
    });
    assert.equal((await applyWorkflowGraphPatchAtomic(request, harness)).success, true);

    const retainedEntry = harness.definition().links.find(raw => parseLink(raw).id === 1);
    retainedEntry[3] = 2;
    harness.refreshManifests();
    const ledger = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const entry = ledger.entries[request.application_id];
    entry.result_definition_hash = await workflowScopeDefinitionHash(harness.definition());
    const content = harness.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);
    const mutationCount = harness.mutations;

    const retry = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_asserted_edge_mismatch"
    )));
    assert.equal(harness.mutations, mutationCount);
});


test("scoped retry rejects a string node and boundary edges substituted for numeric plan IDs", async () => {
    const harness = new ScopedHarness();
    const request = await replaceRequest(harness, {
        remove_edges: [],
        add_edges: [],
        remove_nodes: [],
        expected_delta: {
            created_node_count: 1,
            updated_node_count: 0,
            removed_node_count: 0,
            added_edge_count: 0,
            removed_edge_count: 0,
            final_node_count: 2,
            final_edge_count: 2,
        },
    });
    assert.equal((await applyWorkflowGraphPatchAtomic(request, harness)).success, true);

    const retained = harness.definition().nodes.find(node => typedEqual(node.id, 1));
    retained.id = "1";
    for (const raw of harness.definition().links) {
        if (typedEqual(raw[1], 1)) raw[1] = "1";
        if (typedEqual(raw[3], 1)) raw[3] = "1";
    }
    harness.refreshManifests();
    const exactScopeAdapter = harness.scopeAdapter.bind(harness);
    harness.scopeAdapter = descriptor => {
        const adapter = exactScopeAdapter(descriptor);
        const exactGetNode = adapter.getNode.bind(adapter);
        adapter.getNode = id => {
            const exact = exactGetNode(id);
            if (exact) return exact;
            const coerced = harness.definition().nodes.find(node => String(node.id) === String(id));
            return coerced ? harness.nodeFacts(coerced) : null;
        };
        return adapter;
    };
    const ledger = harness.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const entry = ledger.entries[request.application_id];
    entry.result_definition_hash = await workflowScopeDefinitionHash(harness.definition());
    const content = harness.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);
    const mutationCount = harness.mutations;

    const retry = await applyWorkflowGraphPatchAtomic(request, harness);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_asserted_node_mismatch"
    )));
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_asserted_edge_mismatch"
    )));
    assert.equal(harness.mutations, mutationCount);
});
