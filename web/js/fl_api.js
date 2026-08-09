/**
 * FL_API - ComfyUI browser API wrapper
 * 
 * This module provides a promise-based API for interacting with ComfyUI workflows
 * through ComfyUI frontend APIs. It handles type conversions, error handling,
 * and provides a consistent interface for the tool executor.
 * 
 * @module fl_api
 */

import { app } from "../../../../scripts/app.js";
import { api } from "../../../../scripts/api.js";
import { convertToInput as convertComfyWidgetToInput } from "../../../extensions/core/widgetInputs.js";
import {
    findNonOverlappingPosition,
    getGraphInsertionOrigin,
} from "./node_placement.js";
import { nodeIdsEqual } from "./node_identity.js";
import {
    GRAPH_PRECONDITION_SCHEMA,
    workflowGraphHash,
    workflowGraphHashExcludingExtra,
} from "./graph_precondition.js";
import {
    formatImageWidgetRef,
    nestedImageRefForNode,
    normalizeMaskRegion,
    parseImageWidgetRef,
    summarizeMaskPixels,
} from "./mask_utils.js";

const WORKFLOW_IDENTITY_SCHEMA = "fl-mcp.workflow-instance.v1";
const GRAPH_PATCH_LEDGER_KEY = "fl_mcp_graph_patch_ledger";

const WORKFLOW_IDENTITY_TOKENS = new WeakMap();
const WORKFLOW_IDENTITY_SESSION = (() => {
    if (typeof globalThis.crypto?.randomUUID === "function") {
        return globalThis.crypto.randomUUID();
    }
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
})();
let workflowIdentitySequence = 0;


function workflowIdentityFor(workflow) {
    if (!workflow || (typeof workflow !== "object" && typeof workflow !== "function")) {
        throw new Error("A ComfyUI workflow object is required for exact identity.");
    }
    let identity = WORKFLOW_IDENTITY_TOKENS.get(workflow);
    if (!identity) {
        workflowIdentitySequence += 1;
        identity = `fl-mcp-workflow:${WORKFLOW_IDENTITY_SESSION}:${workflowIdentitySequence}`;
        WORKFLOW_IDENTITY_TOKENS.set(workflow, identity);
    }
    return identity;
}

/**
 * FL_API class - Wrapper for workflow manipulation functions
 */
export class FL_API {
    constructor() {
        console.log("[FL_API] Initialized");
        this.sessionId = null;  // Will be set by extension
        this.layoutEngine = null;  // Lazy loaded when auto-layout is used
        this.pendingMaskReviews = new Map();
        this.maskReviewAutoQueueState = null;
    }

    /**
     * Set session ID for screenshot naming
     * @param {string} sessionId - Session ID
     */
    setSessionId(sessionId) {
        this.sessionId = sessionId;
        console.log(`[FL_API] Session ID set: ${sessionId}`);
    }

    // ==================== NODE MANAGEMENT ====================

    /**
     * Find a node by ID, type, or title
     * @param {number|string|object} query - Node ID, type, title, or node object
     * @param {boolean} findLast - If true, search from end of array
     * @returns {object|null} Node object or null if not found
     */
    find(query, findLast = false) {
        try {
            if (findLast) {
                return this._findLast(query);
            }
            return this._find(query);
        } catch (error) {
            console.error("[FL_API] find error:", error);
            return null;
        }
    }

    /**
     * Create a new node
     * @param {string} nodeType - ComfyUI node class name
     * @param {object} parameters - Node parameter values {key: value}
     * @param {object|null} position - Optional position {x, y}
     * @param {object} options - Optional creation behavior, including preferred_size
     * @returns {object} Created node object
     */
    create(nodeType, parameters = {}, position = null, options = {}) {
        try {
            const node = LiteGraph.createNode(nodeType);
            if (!node) {
                throw new Error(`Node type not found: ${nodeType}`);
            }

            // Set parameter values
            if (node.widgets && Object.keys(parameters).length > 0) {
                for (const [key, value] of Object.entries(parameters)) {
                    const widget = node.widgets.find(w => w.name === key);
                    if (widget) {
                        this._setWidgetValue(node, widget, value);
                    }
                }
            }

            const preferredSize = options?.preferred_size;
            if (
                Number.isFinite(preferredSize?.width)
                && Number.isFinite(preferredSize?.height)
            ) {
                const size = [
                    Math.max(Number(node.size?.[0] || 0), preferredSize.width),
                    Math.max(Number(node.size?.[1] || 0), preferredSize.height),
                ];
                if (typeof node.setSize === "function") node.setSize(size);
                else node.size = size;
            }

            const occupiedRects = (app.graph?._nodes || []).map(existingNode => ({
                x: existingNode.pos[0],
                y: existingNode.pos[1],
                width: existingNode.size[0],
                height: existingNode.size[1],
            }));
            const insertionOrigin = getGraphInsertionOrigin(occupiedRects);
            const requestedPosition = {
                x: Number.isFinite(position?.x) ? position.x : insertionOrigin.x,
                y: Number.isFinite(position?.y) ? position.y : insertionOrigin.y,
            };
            node.pos = [requestedPosition.x, requestedPosition.y];

            // Add to graph
            app.graph.add(node);

            // Use the final LiteGraph size after onAdded/widget setup. If the
            // requested rectangle is occupied, keep its y intent and move it
            // right just far enough to clear every existing node.
            const finalPosition = findNonOverlappingPosition({
                x: requestedPosition.x,
                y: requestedPosition.y,
                width: node.size[0],
                height: node.size[1],
            }, occupiedRects);
            node.pos = [finalPosition.x, finalPosition.y];
            this._markGraphChanged();

            console.log(`[FL_API] Created node: ${nodeType} (id: ${node.id})`);
            return {
                id: node.id,
                type: node.comfyClass || node.type,
                title: node.title,
                position: { x: node.pos[0], y: node.pos[1] },
                size: { width: node.size[0], height: node.size[1] },
                placement_adjusted: (
                    node.pos[0] !== requestedPosition.x
                    || node.pos[1] !== requestedPosition.y
                ),
            };
        } catch (error) {
            console.error("[FL_API] create error:", error);
            throw error;
        }
    }

    /** Return current-canvas nodes whose structured property field matches. */
    findNodesByProperty(propertyName, fieldName, expectedValue) {
        return (app.graph?._nodes || [])
            .filter(node => node.properties?.[propertyName]?.[fieldName] === expectedValue)
            .map(node => ({
                id: node.id,
                node_id: node.id,
                node_type: node.comfyClass || node.type,
                metadata: node.properties[propertyName],
                position: { x: node.pos[0], y: node.pos[1] },
                size: { width: node.size[0], height: node.size[1] },
            }));
    }

    /** Persist structured workflow-application metadata on one canvas node. */
    setNodeProperty(nodeId, propertyName, value) {
        const node = this._findNode(nodeId);
        if (!node) throw new Error(`Node not found: ${nodeId}`);
        node.properties = node.properties || {};
        node.properties[propertyName] = value;
        this._markGraphChanged();
        return value;
    }

    nodeExists(nodeId) {
        return this._findNode(nodeId) !== null;
    }

    /** Return link values across modern Map-backed and legacy object-backed LiteGraph builds. */
    _workflowLinkValues() {
        const links = app.graph?.links;
        if (!links) return [];
        if (typeof links.values === "function") return Array.from(links.values());
        return Object.values(links);
    }

    /** Return link entries across modern Map-backed and legacy object-backed LiteGraph builds. */
    _workflowLinkEntries() {
        const links = app.graph?.links;
        if (!links) return [];
        if (typeof links.entries === "function") return Array.from(links.entries());
        return Object.entries(links);
    }

    /** Return every graph connection touching one of the supplied node IDs. */
    getConnectionsForNodeIds(nodeIds) {
        const selected = new Set((nodeIds || []).map(id => String(id)));
        return this._workflowLinkValues()
            .filter(link => (
                link
                && (selected.has(String(link.origin_id)) || selected.has(String(link.target_id)))
            ))
            .map(link => {
                const source = this._findNode(link.origin_id);
                const target = this._findNode(link.target_id);
                return {
                    source_node_id: link.origin_id,
                    source_output_index: link.origin_slot,
                    source_output: source?.outputs?.[link.origin_slot]?.name ?? null,
                    target_node_id: link.target_id,
                    target_input_index: link.target_slot,
                    target_input: target?.inputs?.[link.target_slot]?.name ?? null,
                };
            });
    }

    _getActiveWorkflow() {
        const workflowStore = this._unwrap(app.extensionManager?.workflow);
        return this._unwrap(workflowStore?.activeWorkflow) || null;
    }

    /** Return the opaque session identity of the exact active ComfyWorkflow object. */
    getActiveWorkflowIdentity() {
        const workflow = this._getActiveWorkflow();
        if (!workflow) {
            throw new Error("The active ComfyUI workflow identity is unavailable.");
        }
        return workflowIdentityFor(workflow);
    }

    /** Pin the exact ComfyWorkflow object only when its compiled identity still matches. */
    pinActiveWorkflow(expectedIdentity) {
        const workflow = this._getActiveWorkflow();
        if (!workflow) {
            throw new Error("The active ComfyUI workflow identity is unavailable.");
        }
        const actualIdentity = workflowIdentityFor(workflow);
        if (
            typeof expectedIdentity !== "string"
            || !expectedIdentity
            || expectedIdentity !== actualIdentity
        ) {
            const error = new Error(
                "The active workflow instance no longer matches the compiled refinement.",
            );
            error.code = "workflow_identity_precondition_failed";
            error.details = {
                expected_workflow_identity: expectedIdentity ?? null,
                actual_workflow_identity: actualIdentity,
                workflow_identity_schema: WORKFLOW_IDENTITY_SCHEMA,
            };
            throw error;
        }
        return Object.freeze({
            workflow,
            identity: actualIdentity,
            identitySchema: WORKFLOW_IDENTITY_SCHEMA,
        });
    }

    /** Refuse to read or mutate a workflow other than the one pinned at transaction start. */
    assertActiveWorkflow(pin) {
        if (!pin?.workflow) throw new Error("A pinned ComfyUI workflow identity is required.");
        const activeWorkflow = this._getActiveWorkflow();
        if (activeWorkflow !== pin.workflow) {
            const expected = pin.identity || "original workflow";
            throw new Error(`The active workflow changed during refinement (expected ${expected}).`);
        }
        return activeWorkflow;
    }

    /** Run one synchronous adapter operation only while its original workflow remains active. */
    withActiveWorkflow(pin, operation) {
        this.assertActiveWorkflow(pin);
        return operation();
    }

    /** Start one Comfy change-tracker transaction for the complete refinement. */
    beginWorkflowChangeTransaction(pin) {
        this.assertActiveWorkflow(pin);
        const canvas = app.canvas;
        if (
            typeof canvas?.emitBeforeChange !== "function"
            || typeof canvas?.emitAfterChange !== "function"
        ) {
            throw new Error("The current ComfyUI canvas cannot provide a balanced change transaction.");
        }
        const tracker = pin.workflow?.changeTracker || null;
        const trackerChangeCountBefore = Number.isInteger(tracker?.changeCount)
            ? tracker.changeCount
            : null;
        const canvasReadOnlyBefore = Boolean(canvas.read_only);
        canvas.read_only = true;
        try {
            canvas.emitBeforeChange();
        } catch (error) {
            canvas.read_only = canvasReadOnlyBefore;
            throw error;
        }
        return {
            pin,
            canvas,
            tracker,
            trackerChangeCountBefore,
            canvasReadOnlyBefore,
            ended: false,
        };
    }

    /** Finish the exact change-tracker transaction, including guarded failure paths. */
    endWorkflowChangeTransaction(transaction) {
        if (!transaction || transaction.ended) return;
        try {
            if (this._getActiveWorkflow() === transaction.pin.workflow) {
                transaction.canvas.emitAfterChange();
                return { closed: true, workflow_identity_verified: true };
            }

            // Never call afterChange() on an inactive tracker: current ComfyUI
            // treats its resulting canvas capture as an invariant violation. If
            // rollback itself could not reactivate the pinned workflow, unwind
            // only the counter opened above and preserve the structured failure.
            if (
                transaction.tracker
                && transaction.trackerChangeCountBefore !== null
                && Number.isInteger(transaction.tracker.changeCount)
            ) {
                transaction.tracker.changeCount = transaction.trackerChangeCountBefore;
            }
            console.warn(
                "[FL-MCP] The pinned workflow was inactive while closing its change transaction; "
                + "the transaction was cancelled without capturing another workflow.",
            );
            return { closed: false, workflow_identity_verified: false };
        } finally {
            transaction.canvas.read_only = transaction.canvasReadOnlyBefore;
            transaction.ended = true;
        }
    }

    /** Pin the graph content expected between staged refinement operations. */
    async createWorkflowMutationGuard(pin) {
        this.assertActiveWorkflow(pin);
        const expectedGraphHash = await workflowGraphHash(app.graph.serialize());
        this.assertActiveWorkflow(pin);
        return { pin, expectedGraphHash };
    }

    /** Detect a same-tab user edit before it can be folded into an agent mutation. */
    async assertWorkflowMutationGuard(guard) {
        if (!guard?.pin || typeof guard.expectedGraphHash !== "string") {
            throw new Error("A workflow mutation guard is required.");
        }
        this.assertActiveWorkflow(guard.pin);
        const actualGraphHash = await workflowGraphHash(app.graph.serialize());
        this.assertActiveWorkflow(guard.pin);
        if (actualGraphHash !== guard.expectedGraphHash) {
            const error = new Error(
                "The canvas changed outside the guarded refinement transaction.",
            );
            error.code = "concurrent_workflow_edit";
            error.details = {
                expected_graph_hash: guard.expectedGraphHash,
                actual_graph_hash: actualGraphHash,
            };
            throw error;
        }
        return actualGraphHash;
    }

    /** Accept exactly one completed agent mutation as the next guarded state. */
    async acceptWorkflowMutationGuard(guard) {
        if (!guard?.pin) throw new Error("A workflow mutation guard is required.");
        this.assertActiveWorkflow(guard.pin);
        const expectedGraphHash = await workflowGraphHash(app.graph.serialize());
        this.assertActiveWorkflow(guard.pin);
        guard.expectedGraphHash = expectedGraphHash;
        return expectedGraphHash;
    }

    /** Return every current graph edge in one normalized, name-enriched shape. */
    listWorkflowConnections(pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        return this._workflowLinkValues()
            .filter(Boolean)
            .map(link => {
                const source = this._findNode(link.origin_id);
                const target = this._findNode(link.target_id);
                return {
                    source_node_id: link.origin_id,
                    source_output_index: link.origin_slot,
                    source_output: source?.outputs?.[link.origin_slot]?.name ?? null,
                    target_node_id: link.target_id,
                    target_input_index: link.target_slot,
                    target_input: target?.inputs?.[link.target_slot]?.name ?? null,
                    type: link.type ?? null,
                };
            });
    }

    /** Return the exact live facts needed by atomic workflow refinement. */
    getWorkflowNode(nodeId, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        const node = this._findNode(nodeId);
        if (!node) return null;
        const serializedNode = typeof node.serialize === "function"
            ? structuredClone(node.serialize())
            : null;
        return {
            id: node.id,
            node_id: node.id,
            node_type: node.comfyClass || node.type,
            type: node.comfyClass || node.type,
            title: node.title,
            values: this.getValues(node.id),
            properties: structuredClone(node.properties || {}),
            position: { x: node.pos[0], y: node.pos[1] },
            size: { width: node.size[0], height: node.size[1] },
            outputs: (node.outputs || []).map((output, index) => ({
                index,
                name: output.name,
                type: output.type,
                links: structuredClone(output.links || []),
            })),
            live_inputs: (node.inputs || []).map((input, socketIndex) => ({
                socket_index: socketIndex,
                name: input.name,
                type: input.type,
                link: input.link ?? null,
            })),
            widgets: (node.widgets || []).map((widget, widgetIndex) => ({
                widget_index: widgetIndex,
                name: widget.name,
                type: widget.type,
                input_type: widget.options?.input_type ?? widget.options?.type ?? null,
                value: structuredClone(widget.value),
            })),
            serialized_node: serializedNode,
        };
    }

    /** Fetch one fresh browser-visible /object_info generation for touched node types. */
    async getNodeDefinitions(nodeTypes = []) {
        const response = await api.fetchApi("/object_info", { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`Could not read browser node definitions (${response.status}).`);
        }
        const catalog = await response.json();
        const selected = {};
        for (const nodeType of new Set(nodeTypes)) {
            if (!catalog?.[nodeType] || typeof catalog[nodeType] !== "object") {
                throw new Error(`Node type ${nodeType} is not registered in this ComfyUI tab.`);
            }
            selected[nodeType] = catalog[nodeType];
        }
        return selected;
    }

    /** Remove exactly one expected link; never delegate target replacement to LiteGraph. */
    disconnectWorkflowConnection(expected, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        const matches = this._workflowLinkEntries().filter(([, link]) => (
            link
            && nodeIdsEqual(link.origin_id, expected.source_node_id)
            && link.origin_slot === expected.source_output_index
            && nodeIdsEqual(link.target_id, expected.target_node_id)
            && link.target_slot === expected.target_input_index
        ));
        if (matches.length !== 1) {
            throw new Error(
                `Expected exactly one workflow connection to disconnect; found ${matches.length}.`,
            );
        }
        const [storedId, link] = matches[0];
        const linkId = link.id ?? (/^-?\d+$/.test(storedId) ? Number(storedId) : storedId);
        if (typeof app.graph?.removeLink === "function") {
            app.graph.removeLink(linkId);
        } else {
            const target = this._findNode(link.target_id);
            if (!target || typeof target.disconnectInput !== "function") {
                throw new Error("The current ComfyUI graph cannot disconnect an exact link.");
            }
            target.disconnectInput(link.target_slot);
        }
        this._markGraphChanged();
        return { disconnected: true, ...expected };
    }

    /** Capture a complete editable graph snapshot for transactional rollback. */
    captureWorkflowSnapshot(pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        return structuredClone(app.graph.serialize());
    }

    /** Restore a complete graph snapshot into the same active canvas. */
    async restoreWorkflowSnapshot(snapshot, pin) {
        if (!snapshot || typeof snapshot !== "object") {
            throw new Error("A serialized workflow snapshot is required for rollback.");
        }
        if (!pin?.workflow) throw new Error("The original ComfyUI workflow identity is required.");
        await app.loadGraphData(
            structuredClone(snapshot),
            false,
            false,
            pin.workflow,
            {
                checkForRerouteMigration: false,
                skipAssetScans: true,
                silentAssetErrors: true,
                deferWarnings: true,
            },
        );
        this.assertActiveWorkflow(pin);
        this.restoreNestedImageReferences();
        this.assertActiveWorkflow(pin);
        this._markGraphChanged();
        return {
            restored: true,
            workflow_identity_verified: true,
            graph_hash: await workflowGraphHash(app.graph.serialize()),
            graph_hash_schema: GRAPH_PRECONDITION_SCHEMA,
        };
    }

    /** Persist bounded refinement idempotency metadata in workflow-level extra state. */
    setWorkflowExtra(key, value, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        if (!key || typeof key !== "string") throw new Error("Workflow extra key is required.");
        app.graph.extra = app.graph.extra || {};
        app.graph.extra[key] = structuredClone(value);
        this._markGraphChanged();
        return value;
    }

    /**
     * Remove nodes from workflow
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of removed nodes
     */
    remove(nodeIds) {
        try {
            let removed = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node) {
                    app.graph.remove(node);
                    removed++;
                }
            }
            if (removed > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Removed ${removed} node(s)`);
            return { removed };
        } catch (error) {
            console.error("[FL_API] remove error:", error);
            throw error;
        }
    }

    /**
     * Bypass (mute) nodes
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of bypassed nodes
     */
    bypass(nodeIds) {
        try {
            let bypassed = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node && node.mode !== 4) {  // 4 = bypassed
                    node.mode = 4;
                    bypassed++;
                }
            }
            if (bypassed > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Bypassed ${bypassed} node(s)`);
            return { bypassed };
        } catch (error) {
            console.error("[FL_API] bypass error:", error);
            throw error;
        }
    }

    /**
     * Unbypass (unmute) nodes
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of unbypassed nodes
     */
    unbypass(nodeIds) {
        try {
            let unbypassed = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node && node.mode === 4) {  // 4 = bypassed
                    node.mode = 0;  // 0 = normal
                    unbypassed++;
                }
            }
            if (unbypassed > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Unbypassed ${unbypassed} node(s)`);
            return { unbypassed };
        } catch (error) {
            console.error("[FL_API] unbypass error:", error);
            throw error;
        }
    }

    /**
     * Pin nodes to prevent movement
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of pinned nodes
     */
    pin(nodeIds) {
        try {
            let pinned = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node) {
                    node.flags = node.flags || {};
                    node.flags.pinned = true;
                    pinned++;
                }
            }
            if (pinned > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Pinned ${pinned} node(s)`);
            return { pinned };
        } catch (error) {
            console.error("[FL_API] pin error:", error);
            throw error;
        }
    }

    /**
     * Unpin nodes to allow movement
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of unpinned nodes
     */
    unpin(nodeIds) {
        try {
            let unpinned = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node && node.flags && node.flags.pinned) {
                    node.flags.pinned = false;
                    unpinned++;
                }
            }
            if (unpinned > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Unpinned ${unpinned} node(s)`);
            return { unpinned };
        } catch (error) {
            console.error("[FL_API] unpin error:", error);
            throw error;
        }
    }

    /**
     * Select nodes in the UI
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of selected nodes
     */
    selectNodes(nodeIds) {
        try {
            // Clear current selection
            app.canvas.selectNodes([]);

            // Find and select nodes
            const nodes = [];
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node) {
                    nodes.push(node);
                }
            }

            if (nodes.length > 0) {
                app.canvas.selectNodes(nodes);
            }
            this._markCanvasDirty();

            console.log(`[FL_API] Selected ${nodes.length} node(s)`);
            return { selected: nodes.length };
        } catch (error) {
            console.error("[FL_API] selectNodes error:", error);
            throw error;
        }
    }

    /**
     * Get currently selected nodes with full details
     * @returns {Array<object>} Array of selected node objects
     */
    getSelectedNodes() {
        try {
            const selectedNodes = Object.values(app.canvas.selected_nodes || {});
            const result = [];
            
            for (const node of selectedNodes) {
                // Extract parameters from widgets
                const parameters = {};
                if (node.widgets) {
                    for (const widget of node.widgets) {
                        if (widget.name && widget.value !== undefined) {
                            parameters[widget.name] = widget.value;
                        }
                    }
                }
                
                // Extract input slot info
                const inputs = [];
                if (node.inputs) {
                    for (const input of node.inputs) {
                        inputs.push({
                            name: input.name,
                            type: input.type,
                            link: input.link || null
                        });
                    }
                }
                
                // Extract output slot info
                const outputs = [];
                if (node.outputs) {
                    for (const output of node.outputs) {
                        outputs.push({
                            name: output.name,
                            type: output.type,
                            links: output.links || []
                        });
                    }
                }
                
                result.push({
                    id: node.id,
                    title: node.title,
                    type: node.comfyClass || node.type,
                    position: { x: node.pos[0], y: node.pos[1] },
                    size: { width: node.size[0], height: node.size[1] },
                    mode: node.mode || 0,
                    parameters: parameters,
                    inputs: inputs,
                    outputs: outputs
                });
            }
            
            console.log(`[FL_API] Retrieved ${result.length} selected node(s)`);
            return result;
        } catch (error) {
            console.error("[FL_API] getSelectedNodes error:", error);
            throw error;
        }
    }

    /**
     * Fit view to selected nodes or all nodes
     * @param {Array<number>|null} nodeIds - Optional array of node IDs to fit (null for selected)
     * @returns {object} Result with count of fitted nodes
     */
    async fitView(nodeIds = null) {
        try {
            const canvas = app.canvas;
            let nodes;

            if (nodeIds === null) {
                // Use currently selected nodes
                nodes = Object.values(canvas.selected_nodes || {});

                if (nodes.length === 0) {
                    console.warn("[FL_API] No nodes selected, fitting all nodes");
                    nodes = app.graph._nodes;  // Use all nodes
                }
            } else if (Array.isArray(nodeIds) && nodeIds.length > 0) {
                // Find specified nodes
                nodes = nodeIds
                    .map(id => this._findNode(id))
                    .filter(n => n !== null);
                
                if (nodes.length === 0) {
                    throw new Error(`None of the specified node IDs found: ${nodeIds}`);
                }
            } else {
                // Empty array = fit all nodes
                nodes = app.graph._nodes;
            }

            if (nodeIds === null && nodes.length > 0) {
                // Keep the user's existing selection.
            } else if (Array.isArray(nodeIds) && nodeIds.length > 0) {
                canvas.selectNodes(nodes);
            } else {
                canvas.selectNodes([]);
            }

            const commandBridge = this._getCommandBridge();
            if (commandBridge?.execute) {
                await commandBridge.execute("Comfy.Canvas.FitView");
            } else if (nodes.length > 0) {
                this._fitNodesFallback(nodes);
            }

            // Mark canvas for redraw
            canvas.setDirty(true, true);

            const count = nodes.length;
            console.log(`[FL_API] Fit view to ${count} node(s)`);

            return {
                fitted_count: count,
                node_ids: nodes.map(n => n.id),
                command_id: commandBridge?.execute ? "Comfy.Canvas.FitView" : null,
                method: commandBridge?.execute ? "native_command" : "fallback"
            };
        } catch (error) {
            console.error("[FL_API] fitView error:", error);
            throw error;
        }
    }

    _fitNodesFallback(nodes) {
        const canvas = app.canvas;
        if (nodes.length === 1) {
            canvas.centerOnNode(nodes[0]);
            return;
        }

        let minX = Infinity, minY = Infinity;
        let maxX = -Infinity, maxY = -Infinity;

        for (const node of nodes) {
            minX = Math.min(minX, node.pos[0]);
            minY = Math.min(minY, node.pos[1]);
            maxX = Math.max(maxX, node.pos[0] + node.size[0]);
            maxY = Math.max(maxY, node.pos[1] + node.size[1]);
        }

        const width = Math.max(1, maxX - minX);
        const height = Math.max(1, maxY - minY);
        const padding = 96;
        const viewportWidth = Math.max(1, canvas.canvas.width - padding * 2);
        const viewportHeight = Math.max(1, canvas.canvas.height - padding * 2);
        const targetZoom = Math.min(viewportWidth / width, viewportHeight / height, 1.0);
        const centerX = (minX + maxX) / 2;
        const centerY = (minY + maxY) / 2;

        canvas.ds.scale = targetZoom;
        canvas.ds.offset[0] = canvas.canvas.width / 2 / targetZoom - centerX;
        canvas.ds.offset[1] = canvas.canvas.height / 2 / targetZoom - centerY;
    }

    /**
     * Take a screenshot of the canvas
     * @param {string} format - Image format ('jpeg' or 'png')
     * @param {number} quality - JPEG quality (0.0-1.0)
     * @returns {Promise<object>} Screenshot data with id, format, size
     */
    async takeScreenshot(format = 'jpeg', quality = 0.9) {
        try {
            // Get canvas element
            const canvasElement = app.canvas.canvas;
            if (!canvasElement) {
                throw new Error('Canvas element not found');
            }

            await this.waitForCanvasStable();
            
            console.log(`[FL_API] Taking screenshot (${format}, quality: ${quality})`);
            
            // Convert canvas to blob
            const mimeType = format === 'png' ? 'image/png' : 'image/jpeg';
            const blob = await new Promise((resolve, reject) => {
                canvasElement.toBlob(
                    (blob) => {
                        if (blob) {
                            resolve(blob);
                        } else {
                            reject(new Error('Failed to create blob from canvas'));
                        }
                    },
                    mimeType,
                    quality
                );
            });
            
            // Convert blob to base64
            const base64Data = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
            
            // Generate screenshot ID
            const timestamp = Date.now();
            const sessionId = this.sessionId || 'unknown';
            const screenshotId = `screenshot_${timestamp}_${sessionId.substring(0, 8)}`;
            
            console.log(`[FL_API] Screenshot captured: ${screenshotId} (${blob.size} bytes)`);
            
            return {
                screenshot_id: screenshotId,
                format: format,
                size_bytes: blob.size,
                base64_data: base64Data
            };
            
        } catch (error) {
            console.error('[FL_API] Screenshot error:', error);
            throw error;
        }
    }

    async waitForCanvasStable(timeoutMs = 1200) {
        const canvas = app.canvas;
        const nextFrame = () => new Promise(resolve => {
            if (typeof requestAnimationFrame === "function") {
                requestAnimationFrame(resolve);
            } else {
                setTimeout(resolve, 16);
            }
        });
        const snapshot = () => [
            Number(canvas?.ds?.scale || 0),
            Number(canvas?.ds?.offset?.[0] || 0),
            Number(canvas?.ds?.offset?.[1] || 0),
        ];
        const previewsReady = () => (app.graph?._nodes || []).every(node =>
            (node.imgs || []).every(image => image.complete !== false)
        );

        const started = Date.now();
        let previous = snapshot();
        let stableFrames = 0;
        while (Date.now() - started < timeoutMs && stableFrames < 3) {
            await nextFrame();
            const current = snapshot();
            const transformStable = current.every(
                (value, index) => Math.abs(value - previous[index]) < 0.0001
            );
            stableFrames = transformStable && previewsReady() ? stableFrames + 1 : 0;
            previous = current;
        }

        // Force a complete foreground/background redraw so toBlob never sees
        // stale frames left behind by Fit View's animated transform.
        canvas?.setDirty?.(true, true);
        canvas?.draw?.(true, true);
        await nextFrame();
        canvas?.draw?.(true, true);
    }

    // ==================== NODE MANIPULATION ====================

    /**
     * Get node parameter values
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @returns {object} Parameter values {key: value}
     */
    getValues(nodeId) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            const values = {};
            if (node.widgets) {
                for (const widget of node.widgets) {
                    if (widget.name && widget.value !== undefined) {
                        values[widget.name] = widget.value;
                    }
                }
            }

            console.log(`[FL_API] Retrieved values for node ${node.id}`);
            return values;
        } catch (error) {
            console.error("[FL_API] getValues error:", error);
            throw error;
        }
    }

    /**
     * Set node parameter values
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @param {object} values - Parameter values {key: value}
     * @returns {object} Result with count of set parameters
     */
    setValues(nodeId, values) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            let set = 0;
            if (node.widgets) {
                for (const [key, value] of Object.entries(values)) {
                    const widget = node.widgets.find(w => w.name === key);
                    if (widget) {
                        this._setWidgetValue(node, widget, value);
                        set++;
                    }
                }
            }
            if (set > 0) {
                this._markGraphChanged();
            }

            console.log(`[FL_API] Set ${set} value(s) on node ${node.id}`);
            return { set };
        } catch (error) {
            console.error("[FL_API] setValues error:", error);
            throw error;
        }
    }

    /** Apply every requested widget exactly or fail for transactional rollback. */
    async setValuesExact(nodeId, values) {
        const node = this._findNode(nodeId);
        if (!node) throw new Error(`Node not found: ${nodeId}`);
        const pending = new Map(Object.entries(values || {}));
        const applied = [];
        let stalledRounds = 0;
        while (pending.size > 0 && stalledRounds < 5) {
            let progress = false;
            for (const [name, value] of [...pending.entries()]) {
                const matches = (node.widgets || []).filter(widget => widget.name === name);
                if (matches.length > 1) {
                    throw new Error(
                        `Expected one live widget named ${name} on node ${node.id}; found ${matches.length}.`,
                    );
                }
                if (matches.length === 0) continue;
                this._setWidgetValueExact(node, matches[0], structuredClone(value));
                await Promise.resolve();
                const observed = (node.widgets || []).filter(widget => widget.name === name);
                if (
                    observed.length !== 1
                    || JSON.stringify(observed[0].value) !== JSON.stringify(value)
                ) {
                    throw new Error(`Widget ${name} did not retain its exact requested value.`);
                }
                pending.delete(name);
                applied.push(name);
                progress = true;
            }
            if (progress) {
                stalledRounds = 0;
                continue;
            }
            stalledRounds += 1;
            await new Promise(resolve => {
                if (typeof globalThis.requestAnimationFrame === "function") {
                    globalThis.requestAnimationFrame(() => resolve());
                } else {
                    setTimeout(resolve, 0);
                }
            });
        }
        // GraphPatch owns the outer dependency fixpoint. A dotted widget may
        // legitimately be absent until another selector value has been
        // applied, so report the names that made progress and let the executor
        // retry the remainder after the selector settles. Duplicate widgets
        // and values that fail to persist still fail immediately above.
        if (applied.length > 0) this._markGraphChanged();
        return { applied };
    }

    /** Promote one exact primitive widget to a live input socket. */
    async convertWidgetToInputExact(nodeId, expected) {
        const node = this._findNode(nodeId);
        if (!node) throw new Error(`Node not found: ${nodeId}`);
        const existingInputs = (node.inputs || [])
            .map((input, socketIndex) => ({ input, socketIndex }))
            .filter(item => item.input.name === expected.input && item.input.type === expected.type);
        if (existingInputs[expected.occurrence_index]) {
            return { socket_index: existingInputs[expected.occurrence_index].socketIndex };
        }
        const widgets = (node.widgets || []).filter(widget => widget.name === expected.input);
        const widget = widgets[expected.occurrence_index];
        if (!widget) {
            throw new Error(
                `Widget ${expected.input} occurrence ${expected.occurrence_index} is unavailable.`,
            );
        }
        const beforeInputs = new Set(node.inputs || []);
        const converted = typeof convertComfyWidgetToInput === "function"
            ? await convertComfyWidgetToInput(node, widget)
            : typeof node.convertWidgetToInput === "function"
                ? await node.convertWidgetToInput(widget)
                : false;
        if (converted === false) {
            throw new Error(`Node ${node.id} refused to convert widget ${expected.input}.`);
        }
        let selected = null;
        for (let attempt = 0; attempt < 6 && !selected; attempt += 1) {
            await Promise.resolve();
            const exactNew = (node.inputs || [])
                .map((input, socketIndex) => ({ input, socketIndex }))
                .filter(item => (
                    !beforeInputs.has(item.input)
                    && item.input.name === expected.input
                    && item.input.type === expected.type
                ));
            const exactAll = (node.inputs || [])
                .map((input, socketIndex) => ({ input, socketIndex }))
                .filter(item => item.input.name === expected.input && item.input.type === expected.type);
            selected = exactNew.length === 1
                ? exactNew[0]
                : exactAll[expected.occurrence_index] || null;
            if (!selected && typeof globalThis.requestAnimationFrame === "function") {
                await new Promise(resolve => globalThis.requestAnimationFrame(() => resolve()));
            }
        }
        if (!selected) {
            throw new Error(
                `Converting ${expected.input} did not create one exact ${expected.type} socket.`,
            );
        }
        this._markGraphChanged();
        return { socket_index: selected.socketIndex };
    }

    /** Assign one already-validated Comfy image reference to an exact widget. */
    assignAttachmentExact(nodeId, attachment) {
        const node = this._findNode(nodeId);
        if (!node) throw new Error(`Node not found: ${nodeId}`);
        const widgets = (node.widgets || []).filter(widget => widget.name === attachment.input);
        if (widgets.length !== 1) {
            throw new Error(
                `Expected one attachment widget named ${attachment.input}; found ${widgets.length}.`,
            );
        }
        const image = {
            filename: attachment.filename,
            subfolder: attachment.subfolder || "",
            type: attachment.file_type || "input",
        };
        const value = formatImageWidgetRef(image);
        if (!value) throw new Error("The attachment image reference is invalid.");
        const widget = widgets[0];
        const optionValues = widget.options?.values;
        if (Array.isArray(optionValues) && !optionValues.includes(value)) optionValues.push(value);
        this._setWidgetValueExact(node, widget, value);
        if (Array.isArray(node.widgets_values)) {
            const index = node.widgets.indexOf(widget);
            if (index >= 0) node.widgets_values[index] = value;
        }
        this._markGraphChanged();
        return { assigned: true, value };
    }

    verifyAttachmentExact(nodeId, attachment) {
        const node = this._findNode(nodeId);
        if (!node) return false;
        const widgets = (node.widgets || []).filter(widget => widget.name === attachment.input);
        if (widgets.length !== 1) return false;
        const observed = parseImageWidgetRef(widgets[0].value);
        return Boolean(
            observed
            && observed.filename === attachment.filename
            && (observed.subfolder || "") === (attachment.subfolder || "")
            && (observed.type || "input") === (attachment.file_type || "input")
        );
    }

    getNodeImageRef(nodeId) {
        const node = this._findNode(nodeId);
        if (!node) {
            throw new Error(`Node not found: ${nodeId}`);
        }
        const imageWidget = node.widgets?.find(widget => widget.name === "image");
        const widgetRef = parseImageWidgetRef(imageWidget?.value);
        const nodeImage = node.images?.[0];
        const image = widgetRef || (nodeImage?.filename ? {
            filename: nodeImage.filename,
            subfolder: nodeImage.subfolder || "",
            type: nodeImage.type || "output",
        } : null);
        if (!image) {
            throw new Error(`Node ${nodeId} does not reference a ComfyUI image`);
        }
        return {
            node_id: node.id,
            node_type: node.comfyClass || node.type,
            title: node.title,
            image,
        };
    }

    async uploadChatImage(file, subfolder) {
        if (!(file instanceof Blob) || !String(file.type || "").startsWith("image/")) {
            throw new Error("Choose a PNG, JPEG, or WebP image.");
        }
        const originalName = String(file.name || "image.png");
        const safeName = originalName.replace(/[^a-zA-Z0-9._-]+/g, "-").slice(-160)
            || "image.png";
        const uploadName = `${Date.now()}-${crypto.randomUUID().slice(0, 8)}-${safeName}`;
        const formData = new FormData();
        formData.append("image", file, uploadName);
        formData.append("type", "input");
        formData.append("subfolder", subfolder);
        formData.append("overwrite", "false");
        const response = await api.fetchApi("/upload/image", {
            method: "POST",
            body: formData,
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(
                `Image upload failed (${response.status}${detail ? `: ${detail}` : ""})`
            );
        }
        const uploaded = await response.json();
        if (!uploaded?.name) {
            throw new Error("Image upload response did not include a filename.");
        }
        return {
            filename: uploaded.name,
            subfolder: uploaded.subfolder || subfolder,
            type: uploaded.type || "input",
        };
    }

    placeChatImageInNode(image, nodeId = null, { focus = true } = {}) {
        const hasExplicitNode = nodeId !== null && nodeId !== undefined;
        let node = hasExplicitNode ? this._findNode(nodeId) : null;
        if (hasExplicitNode && !node) {
            throw new Error(`Node not found: ${nodeId}`);
        }
        if (!node) {
            const selected = Object.values(app.canvas?.selected_nodes || {}).filter(
                candidate => candidate.widgets?.some(widget => widget.name === "image")
            );
            if (selected.length !== 1) {
                throw new Error(
                    selected.length === 0
                        ? "Select one Load Image node on the canvas first."
                        : "Select only one Load Image node before placing the image."
                );
            }
            [node] = selected;
        }
        const imageWidget = node.widgets?.find(widget => widget.name === "image");
        if (!imageWidget) {
            throw new Error(`Node ${node.id} has no image widget.`);
        }
        if (!image?.filename || (image.type || "input") !== "input") {
            throw new Error("The chat attachment is not a valid ComfyUI input image.");
        }
        const normalized = {
            filename: String(image.filename),
            subfolder: String(image.subfolder || ""),
            type: "input",
        };
        const previousImage = parseImageWidgetRef(imageWidget.value);

        const pendingEntry = [...this.pendingMaskReviews.entries()].find(
            ([, value]) => nodeIdsEqual(value.nodeId, node.id)
        );
        if (pendingEntry) {
            this._releaseMaskReviewPreview(pendingEntry[1]);
            this.pendingMaskReviews.delete(pendingEntry[0]);
            if (this.pendingMaskReviews.size === 0) {
                this._restoreAutoQueueAfterMaskReview(this.maskReviewAutoQueueState);
                this.maskReviewAutoQueueState = null;
            }
        }
        this._assignImageToNode(node, normalized);
        if (focus) {
            app.canvas?.selectNodes?.([node]);
            app.canvas?.centerOnNode?.(node);
        }
        this._markGraphChanged();
        return {
            success: true,
            node_id: node.id,
            node_type: node.comfyClass || node.type,
            title: node.title,
            image: normalized,
            previous_image: previousImage,
            queued: false,
            message: "Image assigned to the node. The workflow was not queued.",
        };
    }

    async editNodeMask(nodeId, regions, coordinateSpace = "pixels", clearExisting = false) {
        const node = this._findNode(nodeId);
        if (!node) {
            throw new Error(`Node not found: ${nodeId}`);
        }
        const imageWidget = node.widgets?.find(widget => widget.name === "image");
        if (!imageWidget) {
            throw new Error(`Node ${nodeId} has no image widget to receive the edited mask`);
        }

        const existingReviewEntry = [...this.pendingMaskReviews.entries()].find(
            ([, value]) => nodeIdsEqual(value.nodeId, node.id)
        );
        const existingReview = existingReviewEntry?.[1];
        const originalImage = existingReview?.originalImage
            || this.getNodeImageRef(nodeId).image;
        const source = existingReview?.image || originalImage;
        const [rgbImage, alphaImage] = await Promise.all([
            this._loadComfyImage(source, "rgb"),
            this._loadComfyImage(source, "a"),
        ]);
        if (rgbImage.width !== alphaImage.width || rgbImage.height !== alphaImage.height) {
            throw new Error("Image RGB and alpha dimensions do not match");
        }

        const maskCanvas = document.createElement("canvas");
        maskCanvas.width = rgbImage.width;
        maskCanvas.height = rgbImage.height;
        const maskContext = maskCanvas.getContext("2d");
        const alphaCanvas = document.createElement("canvas");
        alphaCanvas.width = alphaImage.width;
        alphaCanvas.height = alphaImage.height;
        const alphaContext = alphaCanvas.getContext("2d");
        alphaContext.drawImage(alphaImage, 0, 0);
        const sourceAlpha = alphaContext.getImageData(0, 0, alphaImage.width, alphaImage.height);
        const maskPixels = maskContext.createImageData(maskCanvas.width, maskCanvas.height);
        for (let index = 0; index < maskPixels.data.length; index += 4) {
            maskPixels.data[index] = 255;
            maskPixels.data[index + 1] = 255;
            maskPixels.data[index + 2] = 255;
            maskPixels.data[index + 3] = clearExisting ? 0 : 255 - sourceAlpha.data[index + 3];
        }
        maskContext.putImageData(maskPixels, 0, 0);

        const normalizedRegions = regions.map(region =>
            normalizeMaskRegion(region, coordinateSpace, maskCanvas.width, maskCanvas.height)
        );
        for (const region of normalizedRegions) {
            const regionCanvas = document.createElement("canvas");
            regionCanvas.width = maskCanvas.width;
            regionCanvas.height = maskCanvas.height;
            const regionContext = regionCanvas.getContext("2d");
            regionContext.fillStyle = "white";
            regionContext.filter = region.feather > 0 ? `blur(${region.feather}px)` : "none";
            regionContext.beginPath();
            if (region.shape === "ellipse") {
                regionContext.ellipse(
                    region.x + region.width / 2,
                    region.y + region.height / 2,
                    region.width / 2,
                    region.height / 2,
                    0,
                    0,
                    Math.PI * 2
                );
            } else {
                regionContext.rect(region.x, region.y, region.width, region.height);
            }
            regionContext.fill();

            maskContext.save();
            maskContext.globalCompositeOperation = region.operation === "erase"
                ? "destination-out"
                : "source-over";
            maskContext.drawImage(regionCanvas, 0, 0);
            maskContext.restore();
        }

        const editedMask = maskContext.getImageData(0, 0, maskCanvas.width, maskCanvas.height);
        const maskSummary = summarizeMaskPixels(editedMask);
        const reviewCanvas = document.createElement("canvas");
        reviewCanvas.width = rgbImage.width;
        reviewCanvas.height = rgbImage.height;
        const reviewContext = reviewCanvas.getContext("2d");
        reviewContext.drawImage(rgbImage, 0, 0);
        const highlightCanvas = document.createElement("canvas");
        highlightCanvas.width = rgbImage.width;
        highlightCanvas.height = rgbImage.height;
        const highlightContext = highlightCanvas.getContext("2d");
        highlightContext.fillStyle = "#ff00a8";
        highlightContext.fillRect(0, 0, highlightCanvas.width, highlightCanvas.height);
        highlightContext.globalCompositeOperation = "destination-in";
        highlightContext.drawImage(maskCanvas, 0, 0);
        reviewContext.globalAlpha = 0.62;
        reviewContext.drawImage(highlightCanvas, 0, 0);
        const uploadCanvas = document.createElement("canvas");
        uploadCanvas.width = rgbImage.width;
        uploadCanvas.height = rgbImage.height;
        const uploadContext = uploadCanvas.getContext("2d");
        uploadContext.drawImage(rgbImage, 0, 0);
        const uploadPixels = uploadContext.getImageData(0, 0, uploadCanvas.width, uploadCanvas.height);
        for (let index = 0; index < uploadPixels.data.length; index += 4) {
            uploadPixels.data[index + 3] = 255 - editedMask.data[index + 3];
        }
        uploadContext.putImageData(uploadPixels, 0, 0);

        const blob = await this._canvasToBlob(uploadCanvas);
        const filename = `fl-mcp-mask-${Date.now()}-${crypto.randomUUID().slice(0, 8)}.png`;
        const formData = new FormData();
        formData.append("image", blob, filename);
        formData.append("type", "input");
        formData.append("subfolder", "fl_mcp_masks");
        formData.append("original_ref", JSON.stringify(source));
        const response = await api.fetchApi("/upload/mask", {
            method: "POST",
            body: formData,
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(
                `Mask upload failed (${response.status}${detail ? `: ${detail}` : ""})`
            );
        }
        const uploaded = await response.json();
        if (!uploaded?.name) {
            throw new Error("Mask upload response did not include a filename");
        }
        const image = {
            filename: uploaded.name,
            subfolder: uploaded.subfolder || "",
            type: uploaded.type || "input",
        };
        const reviewPreview = await this._canvasToImage(reviewCanvas);
        const reviewToken = crypto.randomUUID();
        if (this.pendingMaskReviews.size === 0) {
            this.maskReviewAutoQueueState = this._pauseAutoQueueForMaskReview();
        }
        if (existingReviewEntry) {
            this._releaseMaskReviewPreview(existingReview);
        }
        this.pendingMaskReviews.set(String(node.id), {
            token: reviewToken,
            nodeId: node.id,
            image,
            originalImage,
            previewUrl: reviewPreview.url,
        });
        node.imgs = [reviewPreview.image];
        node.imageIndex = 0;
        app.canvas?.selectNodes?.([node]);
        app.canvas?.centerOnNode?.(node);
        this._markCanvasDirty();

        return {
            success: true,
            node_id: node.id,
            source_image: source,
            image,
            image_size: { width: rgbImage.width, height: rgbImage.height },
            coordinate_space: coordinateSpace,
            clear_existing: clearExisting,
            regions: normalizedRegions,
            mask: maskSummary,
            preview_visible: true,
            review_required: true,
            review_token: reviewToken,
        };
    }

    confirmMaskReview(nodeId, reviewToken) {
        const pendingEntry = [...this.pendingMaskReviews.entries()].find(
            ([, value]) => nodeIdsEqual(value.nodeId, nodeId)
        );
        const pending = pendingEntry?.[1];
        if (!pending) {
            throw new Error(`There is no edited mask on node ${nodeId} waiting for review`);
        }
        if (pending.token !== reviewToken) {
            throw new Error("This mask review is stale; inspect the latest mask before approving it");
        }
        const node = this._findNode(pending.nodeId);
        if (!node) {
            throw new Error(`Node not found: ${pending.nodeId}`);
        }
        this._assignImageToNode(node, pending.image);
        this._releaseMaskReviewPreview(pending);
        this.pendingMaskReviews.delete(pendingEntry[0]);
        if (this.pendingMaskReviews.size === 0) {
            this._restoreAutoQueueAfterMaskReview(this.maskReviewAutoQueueState);
            this.maskReviewAutoQueueState = null;
        }
        this._markGraphChanged();
        return {
            success: true,
            node_id: pending.nodeId,
            image: pending.image,
            review_token: pending.token,
            approved: true,
            message: "The user approved this mask for workflow execution.",
        };
    }

    discardMaskReviews() {
        const reviews = [...this.pendingMaskReviews.values()];
        for (const pending of reviews) {
            const node = this._findNode(pending.nodeId);
            if (node) {
                const imageWidget = node.widgets?.find(widget => widget.name === "image");
                const currentImage = parseImageWidgetRef(imageWidget?.value)
                    || pending.originalImage;
                if (currentImage) {
                    this._loadNodeImagePreview(node, currentImage);
                }
            }
            this._releaseMaskReviewPreview(pending);
        }
        this.pendingMaskReviews.clear();
        if (reviews.length > 0) {
            this._restoreAutoQueueAfterMaskReview(this.maskReviewAutoQueueState);
            this.maskReviewAutoQueueState = null;
            this._markCanvasDirty();
        }
        return reviews.length;
    }

    /**
     * Get slot information for a node
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @returns {object} Slot information
     */
    getNodeSlots(nodeId) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }
            
            const inputs = [];
            if (node.inputs) {
                for (let i = 0; i < node.inputs.length; i++) {
                    const input = node.inputs[i];
                    const slotInfo = {
                        name: input.name,
                        type: input.type,
                        index: i,
                        connected: input.link !== null && input.link !== undefined
                    };
                    
                    // Add connection details if connected
                    if (slotInfo.connected && node.graph.links[input.link]) {
                        const link = node.graph.links[input.link];
                        slotInfo.connected_from = {
                            node_id: link.origin_id,
                            slot_index: link.origin_slot
                        };
                    }
                    
                    inputs.push(slotInfo);
                }
            }
            
            const outputs = [];
            if (node.outputs) {
                for (let i = 0; i < node.outputs.length; i++) {
                    const output = node.outputs[i];
                    const slotInfo = {
                        name: output.name,
                        type: output.type,
                        index: i,
                        connected: output.links && output.links.length > 0,
                        connected_to: []
                    };
                    
                    // Add connection details if connected
                    if (slotInfo.connected) {
                        for (const linkId of output.links) {
                            const link = node.graph.links[linkId];
                            if (link) {
                                slotInfo.connected_to.push({
                                    node_id: link.target_id,
                                    slot_index: link.target_slot
                                });
                            }
                        }
                    }
                    
                    outputs.push(slotInfo);
                }
            }
            
            console.log(`[FL_API] Retrieved slots for node ${node.id}`);
            return {
                node_id: node.id,
                type: node.comfyClass || node.type,
                title: node.title,
                inputs,
                outputs
            };
        } catch (error) {
            console.error("[FL_API] getNodeSlots error:", error);
            throw error;
        }
    }

    /**
     * Connect two nodes with optional auto-matching
     * @param {number|string|object} sourceId - Source node
     * @param {string|number|null} sourceSlot - Source slot name/index (null for auto)
     * @param {number|string|object} targetId - Target node
     * @param {string|number|null} targetSlot - Target slot name/index (null for auto)
     * @param {object} options - Connection options {auto_match, match_strategy}
     * @returns {object} Connection details
     */
    connect(sourceId, sourceSlot = null, targetId, targetSlot = null, options = {}) {
        try {
            const sourceNode = this._findNode(sourceId);
            const targetNode = this._findNode(targetId);

            if (!sourceNode || !targetNode) {
                throw new Error("Source or target node not found");
            }

            // Options
            const autoMatch = options.auto_match !== false;  // Default true
            const matchStrategy = options.match_strategy || "type";  // Default "type"

            // Helper for case-insensitive slot name comparison
            const normalizeSlotName = (name) => String(name).toLowerCase().trim();

            // Find output slot
            let outputSlotIndex;
            let outputSlotName;
            let outputSlotType;
            
            if (typeof sourceSlot === "number") {
                // Direct index provided
                outputSlotIndex = sourceSlot;
                if (sourceNode.outputs && sourceNode.outputs[sourceSlot]) {
                    outputSlotName = sourceNode.outputs[sourceSlot].name;
                    outputSlotType = sourceNode.outputs[sourceSlot].type;
                }
            } else if (typeof sourceSlot === "string" && sourceNode.outputs) {
                // Slot name provided - find by name (case-insensitive)
                const normalizedSource = normalizeSlotName(sourceSlot);
                const output = sourceNode.outputs.find(o => 
                    normalizeSlotName(o.name) === normalizedSource
                );
                if (output) {
                    outputSlotIndex = sourceNode.findOutputSlot(output.name);
                    outputSlotName = output.name;
                    outputSlotType = output.type;
                }
            }

            // Find input slot
            let inputSlotIndex;
            let inputSlotName;
            let inputSlotType;
            
            if (typeof targetSlot === "number") {
                // Direct index provided
                inputSlotIndex = targetSlot;
                if (targetNode.inputs && targetNode.inputs[targetSlot]) {
                    inputSlotName = targetNode.inputs[targetSlot].name;
                    inputSlotType = targetNode.inputs[targetSlot].type;
                }
            } else if (typeof targetSlot === "string" && targetNode.inputs) {
                // Slot name provided - find by name (case-insensitive)
                const normalizedTarget = normalizeSlotName(targetSlot);
                const input = targetNode.inputs.find(i => 
                    normalizeSlotName(i.name) === normalizedTarget
                );
                if (input) {
                    inputSlotIndex = targetNode.findInputSlot(input.name);
                    inputSlotName = input.name;
                    inputSlotType = input.type;
                }
            }

            // Auto-matching if enabled and slots not found
            if (autoMatch) {
                // Auto-match output slot if not found
                if (outputSlotIndex === undefined && sourceNode.outputs && sourceNode.outputs.length > 0) {
                    if (matchStrategy === "first") {
                        // Use first output
                        outputSlotIndex = 0;
                        outputSlotName = sourceNode.outputs[0].name;
                        outputSlotType = sourceNode.outputs[0].type;
                    } else if (matchStrategy === "type" && inputSlotType) {
                        // Match by type if we know the input type
                        const matchingOutput = sourceNode.outputs.find(o => o.type === inputSlotType);
                        if (matchingOutput) {
                            outputSlotIndex = sourceNode.findOutputSlot(matchingOutput.name);
                            outputSlotName = matchingOutput.name;
                            outputSlotType = matchingOutput.type;
                        } else {
                            // Fallback to first if no type match
                            outputSlotIndex = 0;
                            outputSlotName = sourceNode.outputs[0].name;
                            outputSlotType = sourceNode.outputs[0].type;
                        }
                    }
                }

                // Auto-match input slot if not found
                if (inputSlotIndex === undefined && targetNode.inputs && targetNode.inputs.length > 0) {
                    if (matchStrategy === "first") {
                        // Use first available (unconnected) input
                        const availableInput = targetNode.inputs.find(i => !i.link);
                        if (availableInput) {
                            inputSlotIndex = targetNode.findInputSlot(availableInput.name);
                            inputSlotName = availableInput.name;
                            inputSlotType = availableInput.type;
                        } else {
                            // All connected, use first
                            inputSlotIndex = 0;
                            inputSlotName = targetNode.inputs[0].name;
                            inputSlotType = targetNode.inputs[0].type;
                        }
                    } else if (matchStrategy === "type" && outputSlotType) {
                        // Match by type if we know the output type
                        const matchingInput = targetNode.inputs.find(i => 
                            i.type === outputSlotType && !i.link  // Prefer unconnected
                        );
                        if (matchingInput) {
                            inputSlotIndex = targetNode.findInputSlot(matchingInput.name);
                            inputSlotName = matchingInput.name;
                            inputSlotType = matchingInput.type;
                        } else {
                            // Try connected slots if no unconnected match
                            const anyMatchingInput = targetNode.inputs.find(i => i.type === outputSlotType);
                            if (anyMatchingInput) {
                                inputSlotIndex = targetNode.findInputSlot(anyMatchingInput.name);
                                inputSlotName = anyMatchingInput.name;
                                inputSlotType = anyMatchingInput.type;
                            }
                        }
                    }
                }
            }

            // Check if we have both slots
            if (typeof outputSlotIndex !== "number" || typeof inputSlotIndex !== "number") {
                // Build detailed error message
                const availableOutputs = sourceNode.outputs ? 
                    sourceNode.outputs.map(o => `"${o.name}" (${o.type})`).join(", ") : "none";
                const availableInputs = targetNode.inputs ?
                    targetNode.inputs.map(i => `"${i.name}" (${i.type})${i.link ? ' [connected]' : ''}`).join(", ") : "none";

                const errorMsg = [
                    `Could not find matching slots for connection.`,
                    `Attempted: source="${sourceSlot || 'auto'}" → target="${targetSlot || 'auto'}"`,
                    `Source node ${sourceNode.id} (${sourceNode.comfyClass || sourceNode.type}) outputs: ${availableOutputs}`,
                    `Target node ${targetNode.id} (${targetNode.comfyClass || targetNode.type}) inputs: ${availableInputs}`,
                    ``,
                    `TIP: Use get_node_slots(node_id) to discover exact slot names.`
                ].join("\n");

                throw new Error(errorMsg);
            }

            // Make the connection
            sourceNode.connect(outputSlotIndex, targetNode, inputSlotIndex);
            this._markGraphChanged();
            
            const connectionInfo = {
                source_node_id: sourceNode.id,
                source_slot: outputSlotName,
                source_slot_index: outputSlotIndex,
                target_node_id: targetNode.id,
                target_slot: inputSlotName,
                target_slot_index: inputSlotIndex,
                type: outputSlotType || inputSlotType
            };
            
            console.log(
                `[FL_API] Connected: ${sourceNode.id}[${outputSlotIndex}] "${outputSlotName}" -> ` +
                `${targetNode.id}[${inputSlotIndex}] "${inputSlotName}" (${connectionInfo.type})`
            );
            
            return connectionInfo;
        } catch (error) {
            console.error("[FL_API] connect error:", error);
            throw error;
        }
    }

    /**
     * Connect one refinement edge without auto-matching, target replacement, or
     * accepting a LiteGraph hook that silently redirects the compiled slots.
     */
    connectWorkflowNodesExact(sourceId, targetId, connection, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        const sourceNode = this._findNode(sourceId);
        const targetNode = this._findNode(targetId);
        if (!sourceNode || !targetNode) {
            throw new Error("Source or target node not found for exact workflow connection.");
        }

        const sourceSlot = connection?.source_output_index;
        const targetSlot = connection?.target_input_index;
        if (!Number.isInteger(sourceSlot) || sourceSlot < 0) {
            throw new Error(`Invalid exact source output index: ${sourceSlot}.`);
        }
        if (!Number.isInteger(targetSlot) || targetSlot < 0) {
            throw new Error(`Invalid exact target input index: ${targetSlot}.`);
        }
        if (!Array.isArray(sourceNode.outputs) || sourceSlot >= sourceNode.outputs.length) {
            throw new Error(`Exact source output ${sourceSlot} is out of bounds for node ${sourceNode.id}.`);
        }
        if (!Array.isArray(targetNode.inputs) || targetSlot >= targetNode.inputs.length) {
            throw new Error(`Exact target input ${targetSlot} is out of bounds for node ${targetNode.id}.`);
        }
        if (targetNode.inputs[targetSlot]?.link != null) {
            throw new Error(`Exact target input ${targetNode.id}[${targetSlot}] is already connected.`);
        }

        const link = sourceNode.connect(sourceSlot, targetNode, targetSlot);
        if (!link || typeof link !== "object") {
            throw new Error(
                `LiteGraph rejected exact connection ${sourceNode.id}[${sourceSlot}] -> `
                + `${targetNode.id}[${targetSlot}].`,
            );
        }
        const endpointsMatch = (
            nodeIdsEqual(link.origin_id, sourceNode.id)
            && link.origin_slot === sourceSlot
            && nodeIdsEqual(link.target_id, targetNode.id)
            && link.target_slot === targetSlot
        );
        if (!endpointsMatch) {
            throw new Error(
                `LiteGraph redirected exact connection ${sourceNode.id}[${sourceSlot}] -> `
                + `${targetNode.id}[${targetSlot}].`,
            );
        }

        this._markGraphChanged();
        return {
            id: link.id ?? null,
            source_node_id: link.origin_id,
            source_output_index: link.origin_slot,
            source_output: sourceNode.outputs[sourceSlot]?.name ?? null,
            target_node_id: link.target_id,
            target_input_index: link.target_slot,
            target_input: targetNode.inputs[targetSlot]?.name ?? null,
            type: link.type ?? null,
        };
    }

    /**
     * Connect multiple node pairs in batch
     * @param {Array<object>} connections - Array of connection specs
     * @param {object} options - Options {auto_match, stop_on_error}
     * @returns {object} Batch result
     */
    connectBatch(connections, options = {}) {
        try {
            const autoMatch = options.auto_match !== false;
            const stopOnError = options.stop_on_error || false;
            
            const results = [];
            let successful = 0;
            let failed = 0;
            
            for (const conn of connections) {
                try {
                    const connectOptions = {
                        auto_match: autoMatch,
                        match_strategy: "type"
                    };

                    // Support both old (source_slot) and new (source_slot_name) field names
                    const sourceSlot = conn.source_slot_name ?? conn.source_slot ?? null;
                    const targetSlot = conn.target_slot_name ?? conn.target_slot ?? null;

                    const result = this.connect(
                        conn.source_node_id,
                        sourceSlot,
                        conn.target_node_id,
                        targetSlot,
                        connectOptions
                    );
                    
                    results.push({
                        success: true,
                        connection: result
                    });
                    successful++;
                } catch (error) {
                    results.push({
                        success: false,
                        error: error.message,
                        attempted: conn
                    });
                    failed++;
                    
                    if (stopOnError) {
                        break;
                    }
                }
            }
            
            console.log(`[FL_API] Batch connect: ${successful} succeeded, ${failed} failed`);
            return {
                total: connections.length,
                successful,
                failed,
                results
            };
        } catch (error) {
            console.error("[FL_API] connectBatch error:", error);
            throw error;
        }
    }

    /**
     * Auto-connect nodes in sequence or by type matching
     * @param {Array<number|string>} nodeIds - Array of node IDs
     * @param {string} strategy - "sequential" or "type_match"
     * @returns {object} Auto-connect result
     */
    autoConnectWorkflow(nodeIds, strategy = "sequential") {
        try {
            const connections = [];
            const failed = [];
            
            if (strategy === "sequential") {
                // Connect nodes in sequence: A→B→C→D
                for (let i = 0; i < nodeIds.length - 1; i++) {
                    const sourceId = nodeIds[i];
                    const targetId = nodeIds[i + 1];
                    
                    try {
                        const result = this.connect(
                            sourceId,
                            null,  // Auto-match source slot
                            targetId,
                            null,  // Auto-match target slot
                            { auto_match: true, match_strategy: "type" }
                        );
                        
                        connections.push({
                            source: result.source_node_id,
                            target: result.target_node_id,
                            source_slot: result.source_slot,
                            target_slot: result.target_slot,
                            type: result.type
                        });
                    } catch (error) {
                        failed.push({
                            source: sourceId,
                            target: targetId,
                            error: error.message
                        });
                    }
                }
            } else if (strategy === "type_match") {
                // Find all compatible type matches between all nodes
                const nodes = nodeIds.map(id => this._findNode(id)).filter(n => n !== null);
                
                for (let i = 0; i < nodes.length; i++) {
                    const sourceNode = nodes[i];
                    if (!sourceNode.outputs) continue;
                    
                    for (const output of sourceNode.outputs) {
                        // Find compatible inputs in other nodes
                        for (let j = 0; j < nodes.length; j++) {
                            if (i === j) continue;  // Skip self
                            
                            const targetNode = nodes[j];
                            if (!targetNode.inputs) continue;
                            
                            const matchingInput = targetNode.inputs.find(inp => 
                                inp.type === output.type && !inp.link  // Unconnected and matching type
                            );
                            
                            if (matchingInput) {
                                try {
                                    const result = this.connect(
                                        sourceNode.id,
                                        output.name,
                                        targetNode.id,
                                        matchingInput.name,
                                        { auto_match: false }
                                    );
                                    
                                    connections.push({
                                        source: result.source_node_id,
                                        target: result.target_node_id,
                                        source_slot: result.source_slot,
                                        target_slot: result.target_slot,
                                        type: result.type
                                    });
                                } catch (error) {
                                    failed.push({
                                        source: sourceNode.id,
                                        target: targetNode.id,
                                        source_slot: output.name,
                                        target_slot: matchingInput.name,
                                        error: error.message
                                    });
                                }
                            }
                        }
                    }
                }
            }
            
            console.log(`[FL_API] Auto-connect (${strategy}): ${connections.length} connections made`);
            return {
                connections_made: connections.length,
                connections,
                failed
            };
        } catch (error) {
            console.error("[FL_API] autoConnectWorkflow error:", error);
            throw error;
        }
    }

    // ==================== LAYOUT MANAGEMENT ====================

    /**
     * Get node rectangle (position and size)
     * @param {number|string|object} nodeId - Node ID
     * @returns {object} {x, y, width, height}
     */
    getRect(nodeId) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            const rect = {
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1]
            };

            console.log(`[FL_API] Got rect for node ${node.id}`);
            return rect;
        } catch (error) {
            console.error("[FL_API] getRect error:", error);
            throw error;
        }
    }

    /**
     * Get layout (rects) for all nodes or specific nodes
     * @param {Array<number|string>|null} nodeIds - Optional array of node IDs or titles (null for all)
     * @returns {object} {nodes: Array<{node_id, title, type, rect}>, count: number}
     */
    getLayout(nodeIds = null) {
        try {
            // Safety check for graph
            if (!app.graph || !app.graph._nodes) {
                console.warn("[FL_API] Graph not ready");
                return { nodes: [], count: 0 };
            }
            
            // Get nodes to process
            const nodes = nodeIds 
                ? nodeIds.map(id => this._findNode(id)).filter(n => n !== null)
                : app.graph._nodes;
            
            // Collect layout data
            const layout = nodes.map(node => ({
                node_id: node.id,
                title: node.title,
                type: node.comfyClass || node.type,
                rect: {
                    x: node.pos[0],
                    y: node.pos[1],
                    width: node.size[0],
                    height: node.size[1]
                }
            }));
            
            console.log(`[FL_API] Got layout for ${layout.length} node(s)`);
            return { nodes: layout, count: layout.length };
        } catch (error) {
            console.error("[FL_API] getLayout error:", error);
            throw error;
        }
    }

    /**
     * Modify layout for multiple nodes by setting their rectangles or using auto-layout
     * @param {object} nodeRects - rect objects mapped by nodeId {nodeId: {x, y, width, height}}
     * @param {object} options - Optional auto-layout parameters {auto_layout, node_ids, strategy, spacing_multiplier}
     * @returns {Array<object>} Array of results with updated rectangles or errors
     */
    async modifyLayout(nodeRects = null, options = {}) {
        try {
            // MODE 1: Auto-layout
            if (options.auto_layout === true) {
                console.log(`[FL_API] Auto-layout requested with strategy: ${options.strategy || 'flow_horizontal'}`);
                
                // Lazy load LayoutEngine
                if (!this.layoutEngine) {
                    const { LayoutEngine } = await import('./layout_engine.js');
                    this.layoutEngine = new LayoutEngine();
                    console.log("[FL_API] LayoutEngine loaded");
                }

                // Configure spacing
                if (options.spacing_multiplier !== undefined && options.spacing_multiplier !== null) {
                    this.layoutEngine.setSpacingMultiplier(options.spacing_multiplier);
                } else {
                    // Reset to default if not specified
                    this.layoutEngine.setSpacingMultiplier(1.0);
                }

                // Run layout engine
                const layout = this.layoutEngine.arrangeNodes(
                    options.node_ids || null,
                    options.strategy || "flow_horizontal",
                    {}
                );

                // Apply calculated positions using setRect
                const results = [];
                for (const item of layout) {
                    try {
                        const updatedRect = this.setRect(item.node_id, {
                            x: item.x,
                            y: item.y,
                            width: item.width,
                            height: item.height
                        });
                        results.push({
                            node_id: item.node_id,
                            rect: updatedRect,
                            success: true
                        });
                    } catch (error) {
                        console.error(`[FL_API] Auto-layout: Error setting rect for node ${item.node_id}:`, error);
                        results.push({
                            node_id: item.node_id,
                            success: false,
                            error: error.message
                        });
                    }
                }

                console.log(`[FL_API] Auto-layout complete: ${results.length} nodes arranged`);
                return results;
            }

            // MODE 2: Manual layout (existing behavior)
            if (!nodeRects || typeof nodeRects !== 'object') {
                console.log('[FL_API] modifyLayout: No node rects provided');
                return [];
            }

            const results = [];
            let processed = 0;
            let successful = 0;
            let failed = 0;

            // Process each node
            for (const [nodeIdStr, rect] of Object.entries(nodeRects)) {
                const nodeId = parseInt(nodeIdStr, 10);
                processed++;

                try {
                    // Call setRect and collect result
                    const updatedRect = this.setRect(nodeId, rect);
                    results.push({
                        node_id: nodeId,
                        rect: updatedRect,
                        success: true
                    });
                    successful++;
                } catch (error) {
                    console.error(`[FL_API] modifyLayout: Error setting rect for node ${nodeId}:`, error);
                    results.push({
                        node_id: nodeId,
                        success: false,
                        error: error.message
                    });
                    failed++;
                }
            }

            console.log(`[FL_API] modifyLayout: Processed ${processed} nodes (${successful} successful, ${failed} failed)`);
            return results;
            
        } catch (error) {
            console.error('[FL_API] modifyLayout error:', error);
            throw error;
        }
    }


    /**
     * Set node rectangle (position and/or size)
     * @param {number|string|object} nodeId - Node ID
     * @param {object} rect - {x, y, width, height} (all optional)
     * @returns {object} Updated rectangle
     */
    setRect(nodeId, rect) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            if (typeof rect.x === "number") node.pos[0] = rect.x;
            if (typeof rect.y === "number") node.pos[1] = rect.y;
            if (typeof rect.width === "number") node.size[0] = rect.width;
            if (typeof rect.height === "number") node.size[1] = rect.height;
            this._markGraphChanged();

            const updated = {
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1]
            };

            console.log(`[FL_API] Set rect for node ${node.id}`);
            return updated;
        } catch (error) {
            console.error("[FL_API] setRect error:", error);
            throw error;
        }
    }

    /**
     * Position node to the left of another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     * @returns {object} Updated position
     */
    positionLeft(targetNodeId, anchorNodeId, margin = 32) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0] - targetNode.size[0] - margin;
            const y = anchorNode.pos[1];

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} left of ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionLeft error:", error);
            throw error;
        }
    }

    /**
     * Position node to the right of another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     * @returns {object} Updated position
     */
    positionRight(targetNodeId, anchorNodeId, margin = 32) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0] + anchorNode.size[0] + margin;
            const y = anchorNode.pos[1];

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} right of ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionRight error:", error);
            throw error;
        }
    }

    /**
     * Position node above another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     * @returns {object} Updated position
     */
    positionTop(targetNodeId, anchorNodeId, margin = 64) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0];
            const y = anchorNode.pos[1] - targetNode.size[1] - margin;

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} above ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionTop error:", error);
            throw error;
        }
    }

    /**
     * Position node below another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     */
    positionBottom(targetNodeId, anchorNodeId, margin = 64) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0];
            const y = anchorNode.pos[1] + anchorNode.size[1] + margin;

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} below ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionBottom error:", error);
            throw error;
        }
    }

    /**
     * Move node to the right, avoiding collisions
     * @param {number|string|object} nodeId - Node to move
     * @param {number} margin - Collision margin
     * @returns {object} New position
     */
    moveRight(nodeId, margin = 32) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            // Find rightmost overlapping node
            let maxRight = node.pos[0] + node.size[0];
            for (const otherNode of app.graph._nodes) {
                if (otherNode.id === node.id) continue;

                // Check if vertically overlapping
                const nodeTop = node.pos[1];
                const nodeBottom = node.pos[1] + node.size[1];
                const otherTop = otherNode.pos[1];
                const otherBottom = otherNode.pos[1] + otherNode.size[1];

                if (!(nodeBottom < otherTop || nodeTop > otherBottom)) {
                    // Vertically overlapping
                    const otherRight = otherNode.pos[0] + otherNode.size[0];
                    if (otherRight > maxRight) {
                        maxRight = otherRight;
                    }
                }
            }

            const x = maxRight + margin;
            node.pos[0] = x;
            this._markGraphChanged();

            console.log(`[FL_API] Moved node ${node.id} right to x=${x}`);
            return { x, y: node.pos[1] };
        } catch (error) {
            console.error("[FL_API] moveRight error:", error);
            throw error;
        }
    }

    /**
     * Move node downward, avoiding collisions
     * @param {number|string|object} nodeId - Node to move
     * @param {number} margin - Collision margin
     * @returns {object} New position
     */
    moveBottom(nodeId, margin = 64) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            // Find bottommost overlapping node
            let maxBottom = node.pos[1] + node.size[1];
            for (const otherNode of app.graph._nodes) {
                if (otherNode.id === node.id) continue;

                // Check if horizontally overlapping
                const nodeLeft = node.pos[0];
                const nodeRight = node.pos[0] + node.size[0];
                const otherLeft = otherNode.pos[0];
                const otherRight = otherNode.pos[0] + otherNode.size[0];

                if (!(nodeRight < otherLeft || nodeLeft > otherRight)) {
                    // Horizontally overlapping
                    const otherBottom = otherNode.pos[1] + otherNode.size[1];
                    if (otherBottom > maxBottom) {
                        maxBottom = otherBottom;
                    }
                }
            }

            const y = maxBottom + margin;
            node.pos[1] = y;
            this._markGraphChanged();

            console.log(`[FL_API] Moved node ${node.id} down to y=${y}`);
            return { x: node.pos[0], y };
        } catch (error) {
            console.error("[FL_API] moveBottom error:", error);
            throw error;
        }
    }

    // ==================== WORKFLOW CONTROL ====================

    listCommands() {
        const commandBridge = this._getCommandBridge();
        const rawCommands = this._unwrap(commandBridge?.commands) || this._unwrap(app.extensionManager?.commands) || [];
        const commands = Array.isArray(rawCommands)
            ? rawCommands
            : rawCommands.values
                ? Array.from(rawCommands.values())
                : Object.values(rawCommands);

        return {
            count: commands.length,
            commands: commands.map((command) => this._serializeCommand(command)).filter(Boolean)
        };
    }

    async executeCommand(commandId) {
        const commandBridge = this._getCommandBridge();
        if (!commandBridge?.execute) {
            throw new Error("ComfyUI command bridge is not available");
        }

        await commandBridge.execute(commandId);
        this._markCanvasDirty();
        return { success: true, command_id: commandId };
    }

    listKeybindings() {
        const commands = this.listCommands().commands;
        return {
            count: commands.length,
            keybindings: commands.map((command) => ({
                id: command.id,
                label: command.label,
                keybinding: command.keybinding
            }))
        };
    }

    async getCurrentWorkflowJSON(apiFormat = false) {
        const activeWorkflow = this._getActiveWorkflow();
        if (!activeWorkflow) {
            throw new Error("The active ComfyUI workflow identity is unavailable.");
        }
        const workflowIdentity = workflowIdentityFor(activeWorkflow);
        const assertSameWorkflow = () => {
            if (this._getActiveWorkflow() !== activeWorkflow) {
                const error = new Error("The active workflow changed while it was being read.");
                error.code = "workflow_identity_changed_during_read";
                throw error;
            }
        };
        if (apiFormat) {
            // Capture the guarded canvas identity before graphToPrompt can apply
            // virtual-node transformations to the live graph.
            const editableWorkflow = app.graph.serialize();
            const prompt = await app.graphToPrompt();
            const graphHash = await workflowGraphHash(editableWorkflow);
            const graphPatchContentHash = await workflowGraphHashExcludingExtra(
                editableWorkflow,
                [GRAPH_PATCH_LEDGER_KEY],
            );
            assertSameWorkflow();
            return {
                api_format: true,
                output: prompt.output,
                workflow: prompt.workflow,
                workflow_identity: workflowIdentity,
                workflow_identity_schema: WORKFLOW_IDENTITY_SCHEMA,
                graph_hash: graphHash,
                graph_hash_schema: GRAPH_PRECONDITION_SCHEMA,
                graph_patch_content_hash: graphPatchContentHash,
            };
        }

        const workflow = app.graph.serialize();
        const graphHash = await workflowGraphHash(workflow);
        const graphPatchContentHash = await workflowGraphHashExcludingExtra(
            workflow,
            [GRAPH_PATCH_LEDGER_KEY],
        );
        assertSameWorkflow();
        return {
            api_format: false,
            workflow,
            workflow_identity: workflowIdentity,
            workflow_identity_schema: WORKFLOW_IDENTITY_SCHEMA,
            graph_hash: graphHash,
            graph_hash_schema: GRAPH_PRECONDITION_SCHEMA,
            graph_patch_content_hash: graphPatchContentHash,
        };
    }

    async loadWorkflowJSON(workflow, name = null, clean = true, restoreView = true) {
        if (!workflow || typeof workflow !== "object") {
            throw new Error("workflow must be a workflow JSON object");
        }

        await app.loadGraphData(workflow, clean, restoreView, name || null);
        this.restoreNestedImageReferences();
        this._markGraphChanged();
        return {
            success: true,
            name,
            node_count: app.graph?._nodes?.length || 0
        };
    }

    async getWorkflowTabs() {
        const workflowStore = this._unwrap(app.extensionManager?.workflow);
        const openWorkflows = this._unwrap(workflowStore?.openWorkflows) || [];
        const activeWorkflow = this._unwrap(workflowStore?.activeWorkflow);

        return {
            active: activeWorkflow ? this._serializeWorkflowTab(activeWorkflow) : null,
            tabs: Array.from(openWorkflows).map((workflow) => this._serializeWorkflowTab(workflow)).filter(Boolean)
        };
    }

    async listWorkflowFiles() {
        if (api.listUserDataFullInfo) {
            return {
                files: await api.listUserDataFullInfo("workflows")
            };
        }

        const response = await api.fetchApi("/userdata?dir=workflows&recurse=true&split=false&full_info=true");
        if (!response.ok && response.status !== 404) {
            throw new Error(`Failed to list workflows: ${response.status} ${response.statusText}`);
        }
        return { files: response.status === 404 ? [] : await response.json() };
    }

    async readWorkflowFile(path) {
        const normalizedPath = this._normalizeWorkflowPath(path);
        const response = await api.getUserData(normalizedPath);
        if (!response.ok) {
            throw new Error(`Failed to read workflow '${normalizedPath}': ${response.status} ${response.statusText}`);
        }
        return {
            path: normalizedPath,
            workflow: await response.json()
        };
    }

    async saveCurrentWorkflow(path, overwrite = true) {
        const normalizedPath = this._normalizeWorkflowPath(path);
        const workflow = app.graph.serialize();
        const response = await api.storeUserData(normalizedPath, workflow, {
            overwrite,
            stringify: true,
            throwOnError: false,
            full_info: true
        });
        if (!response.ok) {
            throw new Error(`Failed to save workflow '${normalizedPath}': ${response.status} ${response.statusText}`);
        }
        const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
        return { success: true, path: normalizedPath, file: data };
    }

    async renameWorkflowFile(path, dest, overwrite = true) {
        const sourcePath = this._normalizeWorkflowPath(path);
        const destPath = this._normalizeWorkflowPath(dest);
        const response = await api.moveUserData(sourcePath, destPath, { overwrite });
        if (!response.ok) {
            throw new Error(`Failed to rename workflow '${sourcePath}' to '${destPath}': ${response.status} ${response.statusText}`);
        }
        const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
        return { success: true, path: sourcePath, dest: destPath, file: data };
    }

    async deleteWorkflowFile(path) {
        const normalizedPath = this._normalizeWorkflowPath(path);
        const response = await api.deleteUserData(normalizedPath);
        if (!response.ok) {
            throw new Error(`Failed to delete workflow '${normalizedPath}': ${response.status} ${response.statusText}`);
        }
        return { success: true, path: normalizedPath };
    }

    async closeCurrentWorkflow() {
        return this.executeCommand("Workspace.CloseWorkflow");
    }

    async duplicateCurrentWorkflow() {
        return this.executeCommand("Comfy.DuplicateWorkflow");
    }

    /**
     * Queue workflow for execution
     * @param {number|null} batchCount - Batch count (null for current)
     * @returns {object} Queue result with prompt_id, queue_number, and node_errors
     */
    async queueWorkflow(batchCount = null) {
        try {
            if (this.pendingMaskReviews.size > 0) {
                const pending = this.pendingMaskReviews.values().next().value;
                throw new Error(
                    `Mask review required for node ${pending.nodeId}. `
                    + "Ask the user to approve the visible magenta mask before queueing."
                );
            }
            const queueSettings = this._getQueueSettings();
            if (batchCount !== null) {
                this.setBatchCount(batchCount);
            }

            const effectiveBatchCount = queueSettings?.batchCount || parseInt(app.ui?.batchCount?.value || "1", 10) || 1;
            let queueResult = null;

            // Modern ComfyUI injects the signed-in user's auth token in
            // app.queuePrompt(), immediately before it calls api.queuePrompt().
            // Calling the lower-level API directly skips that step and makes
            // partner/API nodes report "Please login first" even when the user
            // is already signed in. Capture the response while retaining the
            // official authenticated queue path.
            if (typeof app.queuePrompt === "function") {
                for (let attempt = 0; app.processingQueue && attempt < 200; attempt++) {
                    await new Promise(resolve => setTimeout(resolve, 25));
                }
                if (app.processingQueue) {
                    throw new Error("ComfyUI is still preparing another queue request. Try again shortly.");
                }
                const originalQueuePrompt = api.queuePrompt;
                const capturedResults = [];
                let capturedError = null;
                api.queuePrompt = async (...args) => {
                    try {
                        const result = await originalQueuePrompt.apply(api, args);
                        capturedResults.push(result);
                        return result;
                    } catch (error) {
                        capturedError = error;
                        throw error;
                    }
                };
                try {
                    const accepted = await app.queuePrompt(0, effectiveBatchCount);
                    if (capturedError) throw capturedError;
                    queueResult = capturedResults.at(-1) || null;
                    if (!accepted || !queueResult) {
                        throw new Error("ComfyUI did not accept the workflow for queueing.");
                    }
                } finally {
                    api.queuePrompt = originalQueuePrompt;
                }
            } else {
                const prompt = await app.graphToPrompt();
                for (let i = 0; i < effectiveBatchCount; i++) {
                    queueResult = await api.queuePrompt(0, prompt);
                }
            }

            console.log(`[FL_API] Queued workflow (batch: ${effectiveBatchCount})`);
            console.log(`[FL_API] Queue result:`, queueResult);
            
            // Return comprehensive queue information
            return { 
                queued: true, 
                batch_count: effectiveBatchCount,
                prompt_id: queueResult.prompt_id,
                queue_number: queueResult.number,
                node_errors: queueResult.node_errors || {}
            };
        } catch (error) {
            console.error("[FL_API] queueWorkflow error:", error);
            throw error;
        }
    }
    /**
     * Cancel workflow execution
     * @returns {object} Cancel result
     */
    cancelWorkflow() {
        try {
            api.interrupt();
            console.log("[FL_API] Cancelled workflow");
            return { cancelled: true };
        } catch (error) {
            console.error("[FL_API] cancelWorkflow error:", error);
            throw error;
        }
    }

    /**
     * Enable auto-queue mode
     * @returns {object} Result
     */
    enableAutoQueue() {
        try {
            const queueSettings = this._getQueueSettings();
            if (queueSettings) {
                queueSettings.mode = "instant";
            } else if (app.ui) {
                app.ui.autoQueueEnabled = true;
            }
            console.log("[FL_API] Enabled auto-queue");
            return { enabled: true, mode: queueSettings?.mode || "instant" };
        } catch (error) {
            console.error("[FL_API] enableAutoQueue error:", error);
            throw error;
        }
    }

    /**
     * Disable auto-queue mode
     * @returns {object} Result
     */
    disableAutoQueue() {
        try {
            const queueSettings = this._getQueueSettings();
            if (queueSettings) {
                queueSettings.mode = "disabled";
            } else if (app.ui) {
                app.ui.autoQueueEnabled = false;
            }
            console.log("[FL_API] Disabled auto-queue");
            return { enabled: false, mode: queueSettings?.mode || "disabled" };
        } catch (error) {
            console.error("[FL_API] disableAutoQueue error:", error);
            throw error;
        }
    }

    /**
     * Set batch count
     * @param {number} count - Batch count
     * @returns {object} Result
     */
    setBatchCount(count) {
        try {
            const parsed = Math.max(1, parseInt(count, 10) || 1);
            const queueSettings = this._getQueueSettings();
            if (queueSettings) {
                queueSettings.batchCount = parsed;
            } else if (app.ui?.batchCount) {
                app.ui.batchCount.value = parsed;
            }
            console.log(`[FL_API] Set batch count to ${count}`);
            return { count: parsed };
        } catch (error) {
            console.error("[FL_API] setBatchCount error:", error);
            throw error;
        }
    }

    /**
     * Get queue status
     * @returns {object} Queue status
     */
    async getQueueStatus() {
        try {
            const queue = await api.getQueue();
            const queueSettings = this._getQueueSettings();
            const mode = queueSettings?.mode || (app.ui?.autoQueueEnabled ? "instant" : "disabled");
            const batchCount = queueSettings?.batchCount || parseInt(app.ui?.batchCount?.value || "1", 10) || 1;
            console.log("[FL_API] Retrieved queue status");
            return {
                running: queue.Running || [],
                pending: queue.Pending || [],
                auto_queue_enabled: mode !== "disabled",
                auto_queue_mode: mode,
                batch_count: batchCount
            };
        } catch (error) {
            console.error("[FL_API] getQueueStatus error:", error);
            throw error;
        }
    }

    // ==================== SYSTEM CONTROL ====================
    // THIS IS ALL MOVED TO backend/mcp_server.py 'cause this is python level shit.

    /**
     * Send images to external URL
     * @param {string} url - Target URL
     * @param {string} field - Form field name
     * @param {Array} filePaths - File paths or PreviewImage nodes
     * @returns {object} Result
     */
    async sendImages(url, field, filePaths) {
        try {
            // Placeholder - would need actual implementation
            console.log(`[FL_API] sendImages to ${url} (field: ${field})`);
            return { sent: filePaths.length, url, field };
        } catch (error) {
            console.error("[FL_API] sendImages error:", error);
            throw error;
        }
    }

    // ==================== UTILITY ====================

    /**
     * Generate random seed
     * @returns {object} {seed}
     */
    generateSeed() {
        const seed = Math.floor(Math.random() * 1000000000000000);
        console.log(`[FL_API] Generated seed: ${seed}`);
        return { seed };
    }

    /**
     * Generate random float
     * @param {number} min - Minimum value
     * @param {number} max - Maximum value
     * @returns {object} {value}
     */
    generateFloat(min, max) {
        const value = Math.random() * (max - min) + min;
        console.log(`[FL_API] Generated float: ${value}`);
        return { value };
    }

    /**
     * Generate random integer
     * @param {number} min - Minimum value
     * @param {number} max - Maximum value
     * @returns {object} {value}
     */
    generateInt(min, max) {
        const value = Math.floor(Math.random() * (max - min + 1)) + min;
        console.log(`[FL_API] Generated int: ${value}`);
        return { value };
    }

    /**
     * Pick random item from list
     * @param {Array} items - Items to choose from
     * @returns {object} {value}
     */
    randomChoice(items) {
        const value = items[Math.floor(Math.random() * items.length)];
        console.log(`[FL_API] Random choice: ${value}`);
        return { value };
    }

    // ==================== INTERNAL HELPERS ====================

    _unwrap(value) {
        if (value && typeof value === "object" && "value" in value) {
            return value.value;
        }
        return value;
    }

    _getCommandBridge() {
        return this._unwrap(app.extensionManager?.command);
    }

    _getQueueSettings() {
        return this._unwrap(app.extensionManager?.queueSettings) || null;
    }

    _markCanvasDirty() {
        try {
            app.canvas?.setDirty?.(true, true);
        } catch (error) {
            console.debug("[FL_API] Could not mark canvas dirty:", error);
        }
        try {
            app.graph?.setDirtyCanvas?.(true, true);
        } catch (error) {
            console.debug("[FL_API] Could not mark graph canvas dirty:", error);
        }
    }

    _markGraphChanged() {
        this._markCanvasDirty();
        try {
            app.graph?.change?.();
        } catch (error) {
            console.debug("[FL_API] Could not notify graph change:", error);
        }
        try {
            api?.dispatchCustomEvent?.("graphChanged");
        } catch (error) {
            console.debug("[FL_API] Could not dispatch graphChanged:", error);
        }
    }

    async _loadComfyImage(ref, channel) {
        const params = new URLSearchParams({
            filename: ref.filename,
            subfolder: ref.subfolder || "",
            type: ref.type || "input",
            channel,
        });
        params.set("rand", String(Date.now()));
        const response = await api.fetchApi(`/view?${params.toString()}`);
        if (!response.ok) {
            throw new Error(`Failed to load ${channel} image channel (${response.status})`);
        }
        const blob = await response.blob();
        return await createImageBitmap(blob);
    }

    restoreNestedImageReferences(nodes = app.graph?._nodes || []) {
        let restored = 0;
        for (const node of nodes) {
            const image = nestedImageRefForNode(node);
            if (!image) continue;
            this._assignImageToNode(node, image, { notify: false });
            restored++;
        }
        if (restored > 0) this._markCanvasDirty();
        return restored;
    }

    _assignImageToNode(node, image, { notify = true } = {}) {
        const imageWidget = node.widgets?.find(widget => widget.name === "image");
        if (!imageWidget) {
            throw new Error(`Node ${node.id} has no image widget.`);
        }
        const widgetValue = formatImageWidgetRef(image);
        if (!widgetValue) {
            throw new Error(`Node ${node.id} received an invalid image reference.`);
        }
        const optionValues = imageWidget.options?.values;
        if (Array.isArray(optionValues) && !optionValues.includes(widgetValue)) {
            // Nested inputs are executable but absent from ComfyUI's top-level
            // Load Image choices, so keep the canonical value visibly valid.
            optionValues.push(widgetValue);
        }
        if (notify) this._setWidgetValue(node, imageWidget, widgetValue);
        else imageWidget.value = widgetValue;
        node.images = [image];
        this._loadNodeImagePreview(node, image);
        node.properties = node.properties || {};
        node.properties.image = widgetValue;
        if (node.widgets_values && node.widgets) {
            const widgetIndex = node.widgets.indexOf(imageWidget);
            if (widgetIndex >= 0) node.widgets_values[widgetIndex] = widgetValue;
        }
    }

    _loadNodeImagePreview(node, ref) {
        const params = new URLSearchParams({
            filename: ref.filename,
            subfolder: ref.subfolder || "",
            type: ref.type || "input",
        });
        const preview = new Image();
        let usingOriginalFallback = false;
        preview.onload = () => {
            // Never attach an incomplete or failed image to LiteGraph. Some
            // ComfyUI renderers retain stale canvas frames when drawing one.
            node.imgs = [preview];
            node.imageIndex = 0;
            this._markCanvasDirty();
        };
        preview.onerror = () => {
            if (usingOriginalFallback) {
                console.warn(`[FL_API] Image preview failed for node ${node.id}`);
                return;
            }
            // During plugin upgrades the refreshed frontend can briefly run
            // against an older Python process without the thumbnail route.
            usingOriginalFallback = true;
            const originalParams = new URLSearchParams(params);
            originalParams.set("rand", String(Date.now()));
            preview.src = api.apiURL(`/view?${originalParams.toString()}`);
        };
        // Canvas rendering uses a bounded cached preview. The original path
        // remains in the widget value above and is what graphToPrompt executes.
        preview.src = api.apiURL(`/fl_mcp/image/thumbnail?${params.toString()}`);
    }

    _releaseMaskReviewPreview(review) {
        if (review?.previewUrl) URL.revokeObjectURL(review.previewUrl);
    }

    _canvasToBlob(canvas) {
        return new Promise((resolve, reject) => {
            canvas.toBlob(blob => {
                if (blob) resolve(blob);
                else reject(new Error("Failed to encode mask image"));
            }, "image/png");
        });
    }

    async _canvasToImage(canvas) {
        const blob = await this._canvasToBlob(canvas);
        const url = URL.createObjectURL(blob);
        try {
            const image = new Image();
            image.src = url;
            if (typeof image.decode === "function") {
                await image.decode();
            } else {
                await new Promise((resolve, reject) => {
                    image.onload = resolve;
                    image.onerror = () => reject(new Error("Failed to load mask review preview"));
                });
            }
            return { image, url };
        } catch (error) {
            URL.revokeObjectURL(url);
            throw error;
        }
    }

    _pauseAutoQueueForMaskReview() {
        const queueSettings = this._getQueueSettings();
        if (queueSettings) {
            const state = { kind: "queueSettings", mode: queueSettings.mode };
            queueSettings.mode = "disabled";
            return state;
        }
        if (app.ui && "autoQueueEnabled" in app.ui) {
            const state = { kind: "legacy", enabled: Boolean(app.ui.autoQueueEnabled) };
            app.ui.autoQueueEnabled = false;
            return state;
        }
        return null;
    }

    pauseAutoQueue() {
        return this._pauseAutoQueueForMaskReview();
    }

    _restoreAutoQueueAfterMaskReview(state) {
        if (state?.kind === "queueSettings") {
            const queueSettings = this._getQueueSettings();
            if (queueSettings) queueSettings.mode = state.mode;
        } else if (state?.kind === "legacy" && app.ui) {
            app.ui.autoQueueEnabled = state.enabled;
        }
    }

    restoreAutoQueue(state) {
        this._restoreAutoQueueAfterMaskReview(state);
    }

    _setWidgetValue(node, widget, value) {
        const oldValue = widget.value;
        widget.value = value;

        try {
            widget.callback?.call(widget, value, app.canvas, node, app.canvas?.graph_mouse, {});
        } catch (error) {
            console.debug(`[FL_API] Widget callback failed for ${widget.name}:`, error);
        }

        try {
            node.onWidgetChanged?.(widget.name, value, oldValue, widget);
        } catch (error) {
            console.debug(`[FL_API] Node widget change hook failed for ${widget.name}:`, error);
        }
    }

    _setWidgetValueExact(node, widget, value) {
        const oldValue = widget.value;
        widget.value = value;
        widget.callback?.call(widget, value, app.canvas, node, app.canvas?.graph_mouse, {});
        node.onWidgetChanged?.(widget.name, value, oldValue, widget);
    }

    _serializeCommand(command) {
        if (!command) {
            return null;
        }

        let keybinding = null;
        try {
            const rawKeybinding = this._unwrap(command.keybinding);
            const combo = rawKeybinding?.combo || rawKeybinding;
            keybinding = rawKeybinding ? {
                key: combo?.key,
                ctrl: Boolean(combo?.ctrl),
                alt: Boolean(combo?.alt),
                shift: Boolean(combo?.shift),
                meta: Boolean(combo?.meta),
                combo: combo?.toString?.() || rawKeybinding?.toString?.() || null
            } : null;
        } catch (error) {
            console.debug(`[FL_API] Could not serialize keybinding for ${command.id}:`, error);
        }

        return {
            id: command.id,
            label: command.label || command.name || command.id,
            tooltip: command.tooltip || null,
            icon: command.icon || null,
            source: command.source || null,
            version_added: command.versionAdded || null,
            keybinding
        };
    }

    _serializeWorkflowTab(workflow) {
        if (!workflow) {
            return null;
        }

        return {
            id: workflow.id || workflow.key || null,
            name: workflow.name || workflow.path || workflow.filename || "Untitled",
            path: workflow.path || null,
            filename: workflow.filename || null,
            is_modified: Boolean(workflow.isModified || workflow.modified || workflow.dirty)
        };
    }

    _normalizeWorkflowPath(path) {
        if (!path || typeof path !== "string") {
            throw new Error("Workflow path is required");
        }

        let normalized = path.replace(/^\/+/, "");
        if (!normalized.startsWith("workflows/")) {
            normalized = `workflows/${normalized}`;
        }
        if (!normalized.endsWith(".json")) {
            normalized = `${normalized}.json`;
        }
        return normalized;
    }

    /**
     * Find node by various criteria
     * @private
     */
    _findNode(query) {
        return this._find(query);
    }

    /**
     * Find node by various criteria (from end)
     * @private
     */
    _find(query) {
        if (typeof query === "object" && query?.id !== undefined && !query.by) {
            return query;
        }

        if (typeof query === "number") {
            return app.graph._nodes.find(n => nodeIdsEqual(n.id, query)) || null;
        }

        if (typeof query === "string") {
            return app.graph._nodes.find(n => nodeIdsEqual(n.id, query)) ||
                   app.graph._nodes.find(n => n.title === query) ||
                   app.graph._nodes.find(n => n.type === query || n.comfyClass === query) ||
                   null;
        }

        return null;
    }

    /**
     * Find node by various criteria (from end of array)
     * @private
     */
    _findLast(query) {
        if (typeof query === "object" && query?.id !== undefined && !query.by) {
            return query;
        }

        const nodes = app.graph._nodes;

        if (typeof query === "number") {
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodeIdsEqual(nodes[i].id, query)) return nodes[i];
            }
            return null;
        }

        if (typeof query === "string") {
            // Try ID first so serialized string IDs remain addressable.
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodeIdsEqual(nodes[i].id, query)) return nodes[i];
            }
            // Try title first
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodes[i].title === query) return nodes[i];
            }
            // Then type
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodes[i].type === query || nodes[i].comfyClass === query) return nodes[i];
            }
            return null;
        }

        return null;
    }
}
