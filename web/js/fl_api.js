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
    canonicalWorkflowJSON,
    GRAPH_PRECONDITION_SCHEMA,
    workflowGraphHash,
    workflowGraphHashExcludingExtra,
} from "./graph_precondition.js";
import {
    buildExactMaskComposeFormData,
    drawMaskRegionPath,
    formatImageWidgetRef,
    nestedImageRefForNode,
    normalizeMaskRegion,
    parseImageWidgetRef,
    summarizeMaskPixels,
} from "./mask_utils.js";
import {
    executionProvenanceFromSubmission,
    prepareExecutionSubmission,
    recoverQueueOperationFromPayloads,
    submissionCarriesExecutionProvenance,
} from "./execution_provenance.js";
import { captureAuthenticatedQueue } from "./queue_capture.js";

const WORKFLOW_IDENTITY_SCHEMA = "fl-mcp.workflow-instance.v1";
const GRAPH_PATCH_LEDGER_KEY = "fl_mcp_graph_patch_ledger";
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CREATED_NODE_NORMALIZATION_STABLE_FRAMES = 3;
const CREATED_NODE_NORMALIZATION_QUIET_MS = 200;
const CREATED_NODE_NORMALIZATION_MAX_TURNS = 512;
const CREATED_NODE_NORMALIZATION_VISIBLE_TIMEOUT_MS = 5000;
const CREATED_NODE_NORMALIZATION_HIDDEN_TIMEOUT_MS = 15000;
const CREATED_NODE_NORMALIZATION_SAMPLE_MS = 40;
const CREATED_NODE_NORMALIZATION_FRAME_WATCHDOG_MS = 250;
const CREATED_NODE_NORMALIZATION_REVISION_SENTINEL =
    "__fl_mcp_created_node_normalization_revision__";

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


function branchNavigationError(code, message, details = null) {
    const error = new Error(message);
    error.code = code;
    if (details !== null) error.details = details;
    return error;
}


function graphPatchScopeError(code, message, details = null) {
    const error = new Error(message);
    error.code = code;
    if (details !== null) error.details = details;
    return error;
}


function typedValuesEqual(left, right) {
    return typeof left === typeof right && Object.is(left, right);
}


function typedNodeKey(nodeId) {
    return `${typeof nodeId}:${String(nodeId)}`;
}


function maskSourcePreconditionError(message) {
    const error = new Error(message);
    error.code = "mask_source_precondition_failed";
    return error;
}


async function sha256BlobHex(blob) {
    if (typeof globalThis.crypto?.subtle?.digest !== "function") {
        throw maskSourcePreconditionError(
            "SHA-256 is unavailable; the exact mask source cannot be verified.",
        );
    }
    const bytes = await blob.arrayBuffer();
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0"))
        .join("");
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

    /** Create one node and return its canonical serialized workflow ID. */
    createWorkflowNodeExact(nodeType, parameters = {}, position = null, options = {}, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        const created = this.create(nodeType, parameters, position, options);
        if (pin) this.assertActiveWorkflow(pin);
        const graph = app.graph;
        const liveMatches = this._graphNodes(graph).filter(node => (
            typedValuesEqual(node?.id, created.id)
        ));
        if (liveMatches.length !== 1) {
            throw this._nodeProjectionError(
                "workflow_node_projection_ambiguous",
                `Created workflow node ${String(created.id)} does not identify one live node.`,
                created.id,
                liveMatches.length,
            );
        }
        const authority = structuredClone(graph.serialize());
        const projection = this._projectRuntimeNode(
            graph,
            authority,
            liveMatches[0],
            { label: "Created workflow" },
        );
        return {
            ...created,
            id: projection.serializedId,
            node_id: projection.serializedId,
        };
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

    /** Persist metadata using one canonical serialized workflow node ID. */
    setWorkflowNodePropertyExact(nodeId, propertyName, value, pin = null) {
        const node = this._workflowNodeFromSerializedId(nodeId, pin);
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
        const observation = await this._stableWorkflowGuardObservation(pin, null, "start");
        return { pin, expectedGraphHash: observation.graphHash };
    }

    /** Detect a same-tab user edit before it can be folded into an agent mutation. */
    async assertWorkflowMutationGuard(guard) {
        if (!guard?.pin || typeof guard.expectedGraphHash !== "string") {
            throw new Error("A workflow mutation guard is required.");
        }
        const observation = await this._stableWorkflowGuardObservation(
            guard.pin,
            guard.expectedGraphHash,
            "assert",
        );
        if (observation.graphHash !== guard.expectedGraphHash) {
            throw this._workflowMutationGuardError(
                guard.expectedGraphHash,
                observation.graphHash,
            );
        }
        return observation.graphHash;
    }

    /** Accept exactly one completed agent mutation as the next guarded state. */
    async acceptWorkflowMutationGuard(guard) {
        if (!guard?.pin) throw new Error("A workflow mutation guard is required.");
        const observation = await this._stableWorkflowGuardObservation(
            guard.pin,
            guard.expectedGraphHash,
            "accept",
        );
        this._advanceWorkflowMutationGuard(guard, observation);
        return observation.graphHash;
    }

    /** Capture the synchronous post-create state before deferred frontend hooks run. */
    captureCreatedNodeNormalizationCheckpoint(guard, target) {
        if (!guard?.pin || typeof guard.expectedGraphHash !== "string") {
            throw new Error("A workflow mutation guard is required.");
        }
        const normalizedTarget = {
            node_id: target?.node_id,
            node_type: target?.node_type,
            definition_id: target?.definition_id ?? null,
        };
        if (
            normalizedTarget.node_id === undefined
            || normalizedTarget.node_id === null
            || typeof normalizedTarget.node_type !== "string"
            || !normalizedTarget.node_type
            || (
                normalizedTarget.definition_id !== null
                && (
                    typeof normalizedTarget.definition_id !== "string"
                    || !normalizedTarget.definition_id
                )
            )
        ) {
            throw new Error("An exact created-node normalization target is required.");
        }
        const observation = this._captureWorkflowGuardObservation(guard.pin);
        const projection = this._createdNodeNormalizationProjection(
            observation.snapshot,
            normalizedTarget,
        );
        return {
            expectedGraphHash: guard.expectedGraphHash,
            target: normalizedTarget,
            outsideGraphToken: projection.outsideGraphToken,
            revisions: projection.revisions,
            graphToken: observation.graphToken,
        };
    }

    /** Accept only bounded frontend normalization of one freshly-created node. */
    async acceptCreatedNodeNormalization(guard, checkpoint) {
        if (
            !guard?.pin
            || !checkpoint
            || checkpoint.expectedGraphHash !== guard.expectedGraphHash
        ) {
            throw new Error("A current created-node normalization checkpoint is required.");
        }
        const hidden = globalThis.document?.visibilityState === "hidden";
        const timeoutMs = hidden
            ? CREATED_NODE_NORMALIZATION_HIDDEN_TIMEOUT_MS
            : CREATED_NODE_NORMALIZATION_VISIBLE_TIMEOUT_MS;
        const deadline = this._createdNodeNormalizationNow() + timeoutMs;
        let previousGraphToken = checkpoint.graphToken;
        let stableFrames = 0;
        let quietSince = hidden ? null : this._createdNodeNormalizationNow();

        for (
            let turn = 0;
            turn < CREATED_NODE_NORMALIZATION_MAX_TURNS;
            turn += 1
        ) {
            await this._waitForCreatedNodeNormalizationTurn();
            const observedAt = this._createdNodeNormalizationNow();
            if (observedAt > deadline) break;
            const observation = this._captureWorkflowGuardObservation(guard.pin);
            const projection = this._createdNodeNormalizationProjection(
                observation.snapshot,
                checkpoint.target,
            );
            if (!this._createdNodeNormalizationRevisionsMatch(
                checkpoint.revisions,
                projection.revisions,
            )) {
                const actualGraphHash = await workflowGraphHash(observation.snapshot);
                throw this._workflowMutationGuardError(
                    guard.expectedGraphHash,
                    actualGraphHash,
                    {
                        phase: "created_node_normalization",
                        reason: "workflow_revision_changed",
                        node_id: checkpoint.target.node_id,
                        definition_id: checkpoint.target.definition_id,
                    },
                );
            }
            if (projection.outsideGraphToken !== checkpoint.outsideGraphToken) {
                const actualGraphHash = await workflowGraphHash(observation.snapshot);
                throw this._workflowMutationGuardError(
                    guard.expectedGraphHash,
                    actualGraphHash,
                    {
                        phase: "created_node_normalization",
                        reason: "change_outside_created_node",
                        node_id: checkpoint.target.node_id,
                        definition_id: checkpoint.target.definition_id,
                    },
                );
            }

            // A truly hidden document cannot prove anything: browsers pause
            // rAF entirely while hidden, so the graph token could be stale
            // and about to jump the moment the tab is shown again. A merely
            // *unfocused-but-visible* tab is different - rAF can legitimately
            // starve there (OS/browser throttling), but the graph token is a
            // live data-model snapshot, not a paint; a missed frame on a
            // visible tab is not evidence anything is still changing, so it
            // must not block wall-clock quiescence from counting.
            if (globalThis.document?.visibilityState === "hidden") {
                previousGraphToken = observation.graphToken;
                stableFrames = 0;
                quietSince = null;
                continue;
            }
            if (observation.graphToken !== previousGraphToken) {
                previousGraphToken = observation.graphToken;
                stableFrames = 0;
                quietSince = observedAt;
                continue;
            }
            if (quietSince === null) quietSince = observedAt;
            stableFrames += 1;
            if (
                stableFrames < CREATED_NODE_NORMALIZATION_STABLE_FRAMES
                || observedAt - quietSince < CREATED_NODE_NORMALIZATION_QUIET_MS
            ) continue;

            const graphHash = await workflowGraphHash(observation.snapshot);
            if (this._createdNodeNormalizationNow() > deadline) break;
            const recaptured = this._captureWorkflowGuardObservation(guard.pin);
            const recapturedProjection = this._createdNodeNormalizationProjection(
                recaptured.snapshot,
                checkpoint.target,
            );
            if (!this._createdNodeNormalizationRevisionsMatch(
                checkpoint.revisions,
                recapturedProjection.revisions,
            )) {
                const actualGraphHash = await workflowGraphHash(recaptured.snapshot);
                throw this._workflowMutationGuardError(
                    guard.expectedGraphHash,
                    actualGraphHash,
                    {
                        phase: "created_node_normalization",
                        reason: "workflow_revision_changed_during_hash",
                        node_id: checkpoint.target.node_id,
                        definition_id: checkpoint.target.definition_id,
                    },
                );
            }
            if (recapturedProjection.outsideGraphToken !== checkpoint.outsideGraphToken) {
                const actualGraphHash = await workflowGraphHash(recaptured.snapshot);
                throw this._workflowMutationGuardError(
                    guard.expectedGraphHash,
                    actualGraphHash,
                    {
                        phase: "created_node_normalization",
                        reason: "change_outside_created_node_during_hash",
                        node_id: checkpoint.target.node_id,
                        definition_id: checkpoint.target.definition_id,
                    },
                );
            }
            if (recaptured.graphToken !== observation.graphToken) {
                previousGraphToken = recaptured.graphToken;
                stableFrames = 0;
                quietSince = globalThis.document?.visibilityState === "hidden"
                    ? null
                    : this._createdNodeNormalizationNow();
                continue;
            }

            this._advanceWorkflowMutationGuard(guard, {
                ...observation,
                graphHash,
            });
            return graphHash;
        }

        const error = new Error(
            "The created node did not reach a stable frontend-normalized state.",
        );
        error.code = "frontend_normalization_timeout";
        error.details = {
            phase: "created_node_normalization",
            node_id: checkpoint.target.node_id,
            definition_id: checkpoint.target.definition_id,
            timeout_ms: timeoutMs,
        };
        throw error;
    }

    _captureWorkflowGuardObservation(pin) {
        this.assertActiveWorkflow(pin);
        const snapshot = structuredClone(app.graph.serialize());
        const graphToken = canonicalWorkflowJSON(snapshot);
        this.assertActiveWorkflow(pin);
        return { snapshot, graphToken };
    }

    async _stableWorkflowGuardObservation(pin, expectedGraphHash, phase) {
        const observation = this._captureWorkflowGuardObservation(pin);
        const graphHash = await workflowGraphHash(observation.snapshot);
        const recaptured = this._captureWorkflowGuardObservation(pin);
        if (recaptured.graphToken !== observation.graphToken) {
            const actualGraphHash = await workflowGraphHash(recaptured.snapshot);
            throw this._workflowMutationGuardError(
                expectedGraphHash,
                actualGraphHash,
                { phase, reason: "graph_changed_during_hash" },
            );
        }
        return { ...observation, graphHash };
    }

    _advanceWorkflowMutationGuard(guard, observation) {
        guard.expectedGraphHash = observation.graphHash;
    }

    _workflowMutationGuardError(expectedGraphHash, actualGraphHash, details = {}) {
        const error = new Error(
            "The canvas changed outside the guarded refinement transaction.",
        );
        error.code = "concurrent_workflow_edit";
        error.details = {
            expected_graph_hash: expectedGraphHash,
            actual_graph_hash: actualGraphHash,
            ...details,
        };
        return error;
    }

    _createdNodeNormalizationProjection(snapshot, target) {
        const projected = { ...snapshot };
        let sourceOwningGraph = snapshot;
        let owningGraph = projected;
        if (target.definition_id !== null) {
            const definitions = this._serializedSubgraphDefinitions(snapshot);
            const matches = definitions.filter(definition => (
                String(definition?.id ?? "") === target.definition_id
            ));
            if (matches.length !== 1) {
                throw this._workflowMutationGuardError(
                    null,
                    null,
                    {
                        phase: "created_node_normalization",
                        reason: "definition_identity_changed",
                        node_id: target.node_id,
                        definition_id: target.definition_id,
                    },
                );
            }
            [sourceOwningGraph] = matches;
            owningGraph = { ...sourceOwningGraph };
            const rawDefinitions = snapshot?.definitions?.subgraphs;
            if (Array.isArray(rawDefinitions)) {
                const definitionIndex = rawDefinitions.indexOf(sourceOwningGraph);
                const copiedDefinitions = [...rawDefinitions];
                copiedDefinitions[definitionIndex] = owningGraph;
                projected.definitions = {
                    ...snapshot.definitions,
                    subgraphs: copiedDefinitions,
                };
            } else {
                const definitionEntry = Object.entries(rawDefinitions || {})
                    .find(([, definition]) => definition === sourceOwningGraph);
                if (!definitionEntry) {
                    throw this._workflowMutationGuardError(
                        null,
                        null,
                        {
                            phase: "created_node_normalization",
                            reason: "definition_identity_changed",
                            node_id: target.node_id,
                            definition_id: target.definition_id,
                        },
                    );
                }
                projected.definitions = {
                    ...snapshot.definitions,
                    subgraphs: {
                        ...rawDefinitions,
                        [definitionEntry[0]]: owningGraph,
                    },
                };
            }
        }
        const nodes = Array.isArray(sourceOwningGraph?.nodes)
            ? [...sourceOwningGraph.nodes]
            : [];
        const matches = nodes
            .map((node, index) => ({ node, index }))
            .filter(item => typedValuesEqual(item.node?.id, target.node_id));
        if (matches.length !== 1 || matches[0].node?.type !== target.node_type) {
            throw this._workflowMutationGuardError(
                null,
                null,
                {
                    phase: "created_node_normalization",
                    reason: "created_node_identity_changed",
                    node_id: target.node_id,
                    definition_id: target.definition_id,
                },
            );
        }
        nodes[matches[0].index] = {
            id: structuredClone(target.node_id),
            type: target.node_type,
            fl_mcp_created_node_normalization: true,
        };
        owningGraph.nodes = nodes;

        const revisions = [this._createdNodeNormalizationRevisionFact(snapshot, "root")];
        if (sourceOwningGraph !== snapshot) {
            revisions.push(this._createdNodeNormalizationRevisionFact(
                sourceOwningGraph,
                `definition:${target.definition_id}`,
            ));
        }
        if (revisions[0].controlled) {
            projected.revision = CREATED_NODE_NORMALIZATION_REVISION_SENTINEL;
        }
        if (sourceOwningGraph !== snapshot && revisions[1].controlled) {
            owningGraph.revision = CREATED_NODE_NORMALIZATION_REVISION_SENTINEL;
        }
        return {
            outsideGraphToken: canonicalWorkflowJSON(projected),
            revisions,
        };
    }

    _createdNodeNormalizationRevisionFact(graph, scope) {
        const present = Boolean(
            graph
            && typeof graph === "object"
            && Object.prototype.hasOwnProperty.call(graph, "revision")
        );
        const value = present ? graph.revision : null;
        return {
            scope,
            present,
            controlled: present && Number.isSafeInteger(value) && value >= 0,
            value,
        };
    }

    _createdNodeNormalizationRevisionsMatch(expected, actual) {
        if (!Array.isArray(expected) || !Array.isArray(actual)) return false;
        if (expected.length !== actual.length) return false;
        return expected.every((item, index) => {
            const candidate = actual[index];
            if (
                item?.scope !== candidate?.scope
                || item?.present !== candidate?.present
                || item?.controlled !== candidate?.controlled
            ) return false;
            if (item.controlled) return candidate.value >= item.value;
            return Object.is(candidate.value, item.value);
        });
    }

    _createdNodeNormalizationNow() {
        const monotonic = globalThis.performance?.now?.();
        return Number.isFinite(monotonic) ? monotonic : Date.now();
    }

    _waitForCreatedNodeNormalizationTurn() {
        return new Promise(resolve => {
            let finished = false;
            let sampleTimerId = null;
            let watchdogTimerId = null;
            let afterFrameTimerId = null;
            let frameId = null;
            const finish = observation => {
                if (finished) return;
                finished = true;
                if (sampleTimerId !== null) globalThis.clearTimeout(sampleTimerId);
                if (watchdogTimerId !== null) globalThis.clearTimeout(watchdogTimerId);
                if (afterFrameTimerId !== null) globalThis.clearTimeout(afterFrameTimerId);
                if (
                    frameId !== null
                    && typeof globalThis.cancelAnimationFrame === "function"
                ) globalThis.cancelAnimationFrame(frameId);
                resolve(observation);
            };
            sampleTimerId = globalThis.setTimeout(() => {
                sampleTimerId = null;
                const visible = globalThis.document?.visibilityState !== "hidden";
                if (!visible || typeof globalThis.requestAnimationFrame !== "function") {
                    finish({ visible, frameObserved: false });
                    return;
                }
                watchdogTimerId = globalThis.setTimeout(
                    () => finish({
                        visible: globalThis.document?.visibilityState !== "hidden",
                        frameObserved: false,
                    }),
                    CREATED_NODE_NORMALIZATION_FRAME_WATCHDOG_MS,
                );
                frameId = globalThis.requestAnimationFrame(() => {
                    frameId = null;
                    afterFrameTimerId = globalThis.setTimeout(() => finish({
                        visible: globalThis.document?.visibilityState !== "hidden",
                        frameObserved: true,
                    }), 0);
                });
            }, CREATED_NODE_NORMALIZATION_SAMPLE_MS);
        });
    }

    /** Return every current graph edge in one normalized, name-enriched shape. */
    listWorkflowConnections(pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        const graph = app.graph;
        const authority = structuredClone(graph.serialize());
        return this._workflowLinkValues()
            .filter(Boolean)
            .map(link => {
                const source = this._findRuntimeEndpointNode(graph, link.origin_id, {
                    serializedGraph: authority,
                });
                const target = this._findRuntimeEndpointNode(graph, link.target_id, {
                    serializedGraph: authority,
                });
                if (!source || !target) {
                    throw this._nodeProjectionError(
                        "workflow_node_projection_missing",
                        "A workflow connection endpoint has no exact live node.",
                        !source ? link.origin_id : link.target_id,
                    );
                }
                const sourceProjection = this._projectRuntimeNode(graph, authority, source);
                const targetProjection = this._projectRuntimeNode(graph, authority, target);
                return {
                    source_node_id: sourceProjection.serializedId,
                    source_output_index: link.origin_slot,
                    source_output: source?.outputs?.[link.origin_slot]?.name ?? null,
                    target_node_id: targetProjection.serializedId,
                    target_input_index: link.target_slot,
                    target_input: target?.inputs?.[link.target_slot]?.name ?? null,
                    type: link.type ?? null,
                };
            });
    }

    /** Return the exact live facts needed by atomic workflow refinement. */
    getWorkflowNode(nodeId, pin = null) {
        const projection = this._workflowNodeProjection(nodeId, pin, true);
        if (!projection) return null;
        const { node } = projection;
        const serializedNode = projection.serializedNode
            || this._serializeRuntimeNode(node);
        return {
            id: projection.serializedId,
            node_id: projection.serializedId,
            node_type: node.comfyClass || node.type,
            type: node.comfyClass || node.type,
            title: node.title,
            values: this.getValues(node),
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

    /**
     * Resolve one exact native Subgraph and expose the same mutation surface
     * used by the root GraphPatch adapter. Boundary runtime IDs are private to
     * the caller and map only to inputNode/outputNode slot APIs.
     */
    createWorkflowGraphPatchScopeRuntime(descriptor, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        const scope = descriptor?.scope;
        if (
            !scope
            || typeof scope !== "object"
            || !Array.isArray(scope.scope_path)
            || scope.scope_path.length === 0
            || typeof scope.definition_id !== "string"
            || !scope.definition_id
            || !Number.isInteger(descriptor?.input_runtime_id)
            || !Number.isInteger(descriptor?.output_runtime_id)
        ) {
            throw graphPatchScopeError(
                "invalid_scoped_graph_patch_adapter",
                "An exact scoped GraphPatch runtime descriptor is required.",
            );
        }
        const rootGraph = app.rootGraph || app.graph;
        if (!rootGraph) {
            throw graphPatchScopeError("scoped_graph_unavailable", "The root graph is unavailable.");
        }
        const graph = this._resolveGraphPatchScopeExact(rootGraph, scope.scope_path);
        if (String(graph?.id ?? "") !== scope.definition_id) {
            throw graphPatchScopeError(
                "scoped_definition_mismatch",
                "The native Subgraph ID differs from the scoped authority.",
            );
        }
        const runtime = Object.freeze({
            graph,
            rootGraph,
            pin,
            inputRuntimeId: descriptor.input_runtime_id,
            outputRuntimeId: descriptor.output_runtime_id,
            inputNodeType: descriptor.input_node_type,
            outputNodeType: descriptor.output_node_type,
            inputSchemaHash: descriptor.input_schema_hash,
            outputSchemaHash: descriptor.output_schema_hash,
        });
        return {
            captureDefinition: () => this._captureGraphPatchScopeDefinition(runtime),
            captureWorkflow: () => this._captureGraphPatchScopeProjection(runtime),
            getNode: nodeId => this._getGraphPatchScopeNode(runtime, nodeId),
            listConnections: () => this._listGraphPatchScopeConnections(runtime),
            createNode: planned => this._createGraphPatchScopeNode(runtime, planned),
            setNodeValuesExact: (nodeId, values) => (
                this._setGraphPatchScopeValuesExact(runtime, nodeId, values)
            ),
            setNodeMetadata: (nodeId, propertyName, value) => (
                this._setGraphPatchScopeNodeProperty(runtime, nodeId, propertyName, value)
            ),
            setNodeLayoutExact: (nodeId, layout) => (
                this._setGraphPatchScopeRect(runtime, nodeId, layout)
            ),
            convertWidgetToInput: (nodeId, expected) => (
                this._convertGraphPatchScopeWidget(runtime, nodeId, expected)
            ),
            disconnectConnection: expected => (
                this._disconnectGraphPatchScopeConnection(runtime, expected)
            ),
            connectNodes: (sourceId, targetId, connection) => (
                this._connectGraphPatchScopeNodes(runtime, sourceId, targetId, connection)
            ),
            removeNodes: nodeIds => this._removeGraphPatchScopeNodes(runtime, nodeIds),
        };
    }

    _resolveGraphPatchScopeExact(rootGraph, scopePath) {
        let graph = rootGraph;
        const rootSnapshot = structuredClone(rootGraph.serialize());
        let serializedGraph = rootSnapshot;
        for (const [index, step] of scopePath.entries()) {
            const container = this._resolveSerializedNodeProjection(
                graph,
                serializedGraph,
                step.container_node_id,
                {
                    missingCode: "scoped_path_not_found",
                    ambiguousCode: "scoped_path_ambiguous",
                    label: `scope_path[${index}]`,
                },
            ).node;
            if (
                !container.subgraph
                || (
                    typeof container.isSubgraphNode === "function"
                    && !container.isSubgraphNode()
                )
                || container.type !== step.subgraph_id
                || String(container.subgraph.id ?? "") !== step.subgraph_id
            ) {
                throw graphPatchScopeError(
                    "scoped_path_definition_mismatch",
                    `scope_path[${index}] is not the attested subgraph container.`,
                );
            }
            const registered = typeof rootGraph.subgraphs?.get === "function"
                ? rootGraph.subgraphs.get(step.subgraph_id)
                : null;
            if (registered && registered !== container.subgraph) {
                throw graphPatchScopeError(
                    "scoped_path_ambiguous",
                    `scope_path[${index}] conflicts with the root definition registry.`,
                );
            }
            const definitions = this._serializedSubgraphDefinitions(rootSnapshot)
                .filter(definition => String(definition?.id ?? "") === step.subgraph_id);
            if (definitions.length !== 1) {
                throw graphPatchScopeError(
                    definitions.length === 0
                        ? "scoped_path_not_found"
                        : "scoped_path_ambiguous",
                    `scope_path[${index}] does not resolve to one serialized definition.`,
                );
            }
            graph = container.subgraph;
            serializedGraph = definitions[0];
        }
        return graph;
    }

    _assertGraphPatchScopeRuntime(runtime) {
        if (runtime.pin) this.assertActiveWorkflow(runtime.pin);
        if ((app.rootGraph || app.graph) !== runtime.rootGraph) {
            throw graphPatchScopeError(
                "scoped_root_identity_changed",
                "The native root graph changed during scoped mutation.",
            );
        }
        return runtime.graph;
    }

    _scopeGraphLinkValues(graph) {
        const links = graph?.links;
        if (!links) return [];
        if (typeof links.values === "function") return Array.from(links.values());
        return Object.values(links);
    }

    _scopeGraphNode(runtime, nodeId) {
        if ([runtime.inputRuntimeId, runtime.outputRuntimeId].some(id => typedValuesEqual(id, nodeId))) {
            return null;
        }
        const definition = this._captureGraphPatchScopeDefinition(runtime);
        return this._resolveSerializedNodeProjection(
            runtime.graph,
            definition,
            nodeId,
            {
                allowMissing: true,
                missingCode: "scoped_node_not_found",
                ambiguousCode: "ambiguous_scoped_node_identity",
                label: "Scoped workflow",
            },
        )?.node || null;
    }

    _captureGraphPatchScopeDefinition(runtime) {
        const graph = this._assertGraphPatchScopeRuntime(runtime);
        if (typeof graph.asSerialisable !== "function") {
            throw graphPatchScopeError(
                "scoped_serialization_unavailable",
                "The native Subgraph cannot provide its exact definition serialization.",
            );
        }
        return structuredClone(graph.asSerialisable());
    }

    _scopeVirtualNode(runtime, kind) {
        const input = kind === "input";
        const graph = runtime.graph;
        const slots = input ? graph.inputs : graph.outputs;
        const bounding = input ? graph.inputNode?.boundingRect : graph.outputNode?.boundingRect;
        const inputs = input ? [] : slots.map(slot => ({
            name: slot.name,
            type: slot.type,
            link: slot.linkIds?.[0] ?? null,
        }));
        const outputs = input ? slots.map(slot => ({
            name: slot.name,
            type: slot.type,
            links: structuredClone(slot.linkIds || []),
        })) : [];
        return {
            id: input ? runtime.inputRuntimeId : runtime.outputRuntimeId,
            type: input ? runtime.inputNodeType : runtime.outputNodeType,
            schema_hash: input ? runtime.inputSchemaHash : runtime.outputSchemaHash,
            inputs,
            outputs,
            widgets_values: [],
            properties: {},
            pos: [bounding?.[0] ?? 0, bounding?.[1] ?? 0],
            size: [bounding?.[2] ?? 75, bounding?.[3] ?? 100],
            flags: {},
            mode: 0,
        };
    }

    _captureGraphPatchScopeProjection(runtime) {
        const definition = this._captureGraphPatchScopeDefinition(runtime);
        return {
            version: definition.version,
            state: structuredClone(definition.state || {}),
            nodes: [
                this._scopeVirtualNode(runtime, "input"),
                ...structuredClone(definition.nodes || []),
                this._scopeVirtualNode(runtime, "output"),
            ],
            links: structuredClone(definition.links || []),
        };
    }

    _scopeRerouteManifests(runtime, node, inputs, outputs) {
        if (
            (node.comfyClass || node.type) !== "Reroute"
            || inputs.length !== 1
            || outputs.length !== 1
            || inputs[0].name !== ""
            || outputs[0].name !== ""
            || inputs[0].type !== "*"
        ) return { inputs, outputs };
        const types = new Set(
            this._scopeGraphLinkValues(runtime.graph)
                .filter(link => (
                    typedValuesEqual(link.origin_id, node.id)
                    || typedValuesEqual(link.target_id, node.id)
                ))
                .map(link => link.type)
                .filter(type => typeof type === "string" && type && type !== "*"),
        );
        if (types.size !== 1) {
            throw graphPatchScopeError(
                "scoped_reroute_type_ambiguous",
                `Reroute ${String(node.id)} does not have one exact resolved physical type.`,
            );
        }
        const [type] = types;
        return {
            inputs: [{ ...inputs[0], name: "__fl_mcp_reroute_input_0__", type }],
            outputs: [{ ...outputs[0], name: "__fl_mcp_reroute_output_0__", type }],
        };
    }

    _scopeNodeFacts(runtime, node) {
        const definition = this._captureGraphPatchScopeDefinition(runtime);
        const projection = this._projectRuntimeNode(
            runtime.graph,
            definition,
            node,
            {
                missingCode: "scoped_node_not_found",
                ambiguousCode: "ambiguous_scoped_node_identity",
                label: "Scoped workflow",
            },
        );
        const serializedNode = projection.serializedNode
            || this._serializeRuntimeNode(node);
        const rawInputs = (node.inputs || []).map((input, socketIndex) => ({
            socket_index: socketIndex,
            name: input.name,
            type: input.type,
            link: input.link ?? null,
        }));
        const rawOutputs = (node.outputs || []).map((output, index) => ({
            index,
            name: output.name,
            type: output.type,
            links: structuredClone(output.links || []),
        }));
        const manifests = this._scopeRerouteManifests(runtime, node, rawInputs, rawOutputs);
        const values = Object.fromEntries(
            (node.widgets || [])
                .filter(widget => widget.name && widget.value !== undefined)
                .map(widget => [widget.name, structuredClone(widget.value)]),
        );
        return {
            id: projection.serializedId,
            node_id: projection.serializedId,
            node_type: node.comfyClass || node.type,
            type: node.comfyClass || node.type,
            title: node.title,
            values,
            properties: structuredClone(node.properties || {}),
            position: { x: node.pos[0], y: node.pos[1] },
            size: { width: node.size[0], height: node.size[1] },
            outputs: manifests.outputs,
            live_inputs: manifests.inputs,
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

    _getGraphPatchScopeNode(runtime, nodeId) {
        this._assertGraphPatchScopeRuntime(runtime);
        if (typedValuesEqual(nodeId, runtime.inputRuntimeId)) {
            const node = this._scopeVirtualNode(runtime, "input");
            return {
                ...node,
                node_id: node.id,
                node_type: node.type,
                position: { x: node.pos[0], y: node.pos[1] },
                size: { width: node.size[0], height: node.size[1] },
                live_inputs: structuredClone(node.inputs),
                serialized_node: structuredClone(node),
            };
        }
        if (typedValuesEqual(nodeId, runtime.outputRuntimeId)) {
            const node = this._scopeVirtualNode(runtime, "output");
            return {
                ...node,
                node_id: node.id,
                node_type: node.type,
                position: { x: node.pos[0], y: node.pos[1] },
                size: { width: node.size[0], height: node.size[1] },
                live_inputs: structuredClone(node.inputs),
                serialized_node: structuredClone(node),
            };
        }
        const node = this._scopeGraphNode(runtime, nodeId);
        return node ? this._scopeNodeFacts(runtime, node) : null;
    }

    _scopeSlotName(runtime, nodeId, slotIndex, outputSide) {
        if (typedValuesEqual(nodeId, runtime.inputRuntimeId)) {
            return runtime.graph.inputs?.[slotIndex]?.name ?? null;
        }
        if (typedValuesEqual(nodeId, runtime.outputRuntimeId)) {
            return runtime.graph.outputs?.[slotIndex]?.name ?? null;
        }
        const node = this._scopeGraphNode(runtime, nodeId);
        if (!node) return null;
        const facts = this._scopeNodeFacts(runtime, node);
        const manifest = outputSide ? facts.outputs : facts.live_inputs;
        const item = outputSide
            ? manifest.find(slot => slot.index === slotIndex)
            : manifest.find(slot => slot.socket_index === slotIndex);
        return item?.name ?? null;
    }

    _scopeSerializedEndpointId(runtime, endpointId) {
        if (
            typedValuesEqual(endpointId, runtime.inputRuntimeId)
            || typedValuesEqual(endpointId, runtime.graph.inputNode?.id)
        ) return runtime.inputRuntimeId;
        if (
            typedValuesEqual(endpointId, runtime.outputRuntimeId)
            || typedValuesEqual(endpointId, runtime.graph.outputNode?.id)
        ) return runtime.outputRuntimeId;
        const node = this._findRuntimeEndpointNode(runtime.graph, endpointId, {
            ambiguousCode: "ambiguous_scoped_node_identity",
            label: "Scoped workflow",
            serializedGraph: this._captureGraphPatchScopeDefinition(runtime),
        });
        if (!node) {
            throw graphPatchScopeError(
                "scoped_node_not_found",
                `Scoped connection endpoint ${String(endpointId)} is missing.`,
            );
        }
        return this._projectRuntimeNode(
            runtime.graph,
            this._captureGraphPatchScopeDefinition(runtime),
            node,
            {
                missingCode: "scoped_node_not_found",
                ambiguousCode: "ambiguous_scoped_node_identity",
                label: "Scoped workflow",
            },
        ).serializedId;
    }

    _scopeRuntimeEndpointId(runtime, serializedId) {
        if (typedValuesEqual(serializedId, runtime.inputRuntimeId)) {
            return runtime.graph.inputNode?.id ?? runtime.inputRuntimeId;
        }
        if (typedValuesEqual(serializedId, runtime.outputRuntimeId)) {
            return runtime.graph.outputNode?.id ?? runtime.outputRuntimeId;
        }
        const node = this._scopeGraphNode(runtime, serializedId);
        if (!node) {
            throw graphPatchScopeError(
                "scoped_node_not_found",
                `Scoped node ${String(serializedId)} is missing.`,
            );
        }
        return node.id;
    }

    _listGraphPatchScopeConnections(runtime) {
        const graph = this._assertGraphPatchScopeRuntime(runtime);
        return this._scopeGraphLinkValues(graph).filter(Boolean).map(link => {
            const sourceId = this._scopeSerializedEndpointId(runtime, link.origin_id);
            const targetId = this._scopeSerializedEndpointId(runtime, link.target_id);
            return {
                source_node_id: sourceId,
                source_output_index: link.origin_slot,
                source_output: this._scopeSlotName(runtime, sourceId, link.origin_slot, true),
                target_node_id: targetId,
                target_input_index: link.target_slot,
                target_input: this._scopeSlotName(runtime, targetId, link.target_slot, false),
                type: link.type ?? null,
            };
        });
    }

    _createGraphPatchScopeNode(runtime, planned) {
        const graph = this._assertGraphPatchScopeRuntime(runtime);
        const node = LiteGraph.createNode(planned.node_type);
        if (!node) throw new Error(`Node type not found: ${planned.node_type}`);
        const occupied = this._graphNodes(graph).map(existing => ({
            x: existing.pos[0],
            y: existing.pos[1],
            width: existing.size[0],
            height: existing.size[1],
        }));
        const origin = getGraphInsertionOrigin(occupied);
        node.pos = [
            planned.layout_hint?.x ?? origin.x,
            planned.layout_hint?.y ?? origin.y,
        ];
        graph.add(node);
        if (!planned.layout_hint) {
            const adjusted = findNonOverlappingPosition({
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1],
            }, occupied);
            node.pos = [adjusted.x, adjusted.y];
        }
        this._markGraphChanged();
        const projection = this._projectRuntimeNode(
            graph,
            this._captureGraphPatchScopeDefinition(runtime),
            node,
            {
                missingCode: "scoped_node_not_found",
                ambiguousCode: "ambiguous_scoped_node_identity",
                label: "Created scoped workflow",
            },
        );
        return {
            id: projection.serializedId,
            node_id: projection.serializedId,
            type: node.comfyClass || node.type,
        };
    }

    async _setGraphPatchScopeValuesExact(runtime, nodeId, values) {
        const node = this._scopeGraphNode(runtime, nodeId);
        if (!node) throw new Error(`Scoped node not found: ${String(nodeId)}`);
        const pending = new Map(Object.entries(values || {}));
        const applied = [];
        let stalledRounds = 0;
        while (pending.size > 0 && stalledRounds < 5) {
            let progress = false;
            for (const [name, value] of [...pending.entries()]) {
                const matches = (node.widgets || []).filter(widget => widget.name === name);
                if (matches.length > 1) {
                    throw new Error(
                        `Expected one live widget named ${name} on scoped node ${String(node.id)}; found ${matches.length}.`,
                    );
                }
                if (matches.length === 0) continue;
                this._setWidgetValueExact(node, matches[0], structuredClone(value));
                await Promise.resolve();
                const observed = (node.widgets || []).filter(widget => widget.name === name);
                if (
                    observed.length !== 1
                    || JSON.stringify(observed[0].value) !== JSON.stringify(value)
                ) throw new Error(`Widget ${name} did not retain its exact requested value.`);
                pending.delete(name);
                applied.push(name);
                progress = true;
            }
            if (progress) stalledRounds = 0;
            else {
                stalledRounds += 1;
                await new Promise(resolve => {
                    if (typeof globalThis.requestAnimationFrame === "function") {
                        globalThis.requestAnimationFrame(() => resolve());
                    } else setTimeout(resolve, 0);
                });
            }
        }
        if (applied.length > 0) this._markGraphChanged();
        return { applied };
    }

    _setGraphPatchScopeNodeProperty(runtime, nodeId, propertyName, value) {
        const node = this._scopeGraphNode(runtime, nodeId);
        if (!node) throw new Error(`Scoped node not found: ${String(nodeId)}`);
        node.properties = node.properties || {};
        node.properties[propertyName] = structuredClone(value);
        this._markGraphChanged();
        return value;
    }

    _setGraphPatchScopeRect(runtime, nodeId, rect) {
        const node = this._scopeGraphNode(runtime, nodeId);
        if (!node) throw new Error(`Scoped node not found: ${String(nodeId)}`);
        if (typeof rect.x === "number") node.pos[0] = rect.x;
        if (typeof rect.y === "number") node.pos[1] = rect.y;
        if (typeof rect.width === "number") node.size[0] = rect.width;
        if (typeof rect.height === "number") node.size[1] = rect.height;
        this._markGraphChanged();
        return { x: node.pos[0], y: node.pos[1], width: node.size[0], height: node.size[1] };
    }

    async _convertGraphPatchScopeWidget(runtime, nodeId, expected) {
        const node = this._scopeGraphNode(runtime, nodeId);
        if (!node) throw new Error(`Scoped node not found: ${String(nodeId)}`);
        const existing = (node.inputs || [])
            .map((input, socketIndex) => ({ input, socketIndex }))
            .filter(item => item.input.name === expected.input && item.input.type === expected.type);
        if (existing[expected.occurrence_index]) {
            return { socket_index: existing[expected.occurrence_index].socketIndex };
        }
        const widgets = (node.widgets || []).filter(widget => widget.name === expected.input);
        const widget = widgets[expected.occurrence_index];
        if (!widget) throw new Error(`Scoped widget ${expected.input} is unavailable.`);
        const beforeInputs = new Set(node.inputs || []);
        const converted = typeof convertComfyWidgetToInput === "function"
            ? await convertComfyWidgetToInput(node, widget)
            : typeof node.convertWidgetToInput === "function"
                ? await node.convertWidgetToInput(widget)
                : false;
        if (converted === false) throw new Error(`Scoped node ${String(node.id)} refused widget conversion.`);
        let selected = null;
        for (let attempt = 0; attempt < 6 && !selected; attempt += 1) {
            await Promise.resolve();
            const exact = (node.inputs || [])
                .map((input, socketIndex) => ({ input, socketIndex }))
                .filter(item => (
                    !beforeInputs.has(item.input)
                    && item.input.name === expected.input
                    && item.input.type === expected.type
                ));
            selected = exact.length === 1 ? exact[0] : null;
            if (!selected && typeof globalThis.requestAnimationFrame === "function") {
                await new Promise(resolve => globalThis.requestAnimationFrame(() => resolve()));
            }
        }
        if (!selected) throw new Error(`Scoped widget ${expected.input} did not create one exact socket.`);
        this._markGraphChanged();
        return { socket_index: selected.socketIndex };
    }

    _disconnectGraphPatchScopeConnection(runtime, expected) {
        const graph = this._assertGraphPatchScopeRuntime(runtime);
        if (
            typedValuesEqual(expected.source_node_id, runtime.inputRuntimeId)
            && typedValuesEqual(expected.target_node_id, runtime.outputRuntimeId)
        ) {
            throw graphPatchScopeError(
                "direct_scope_boundary_edge_unsupported",
                "Direct input-to-output boundary links have no supported native mutation primitive.",
            );
        }
        const matches = this._scopeGraphLinkValues(graph).filter(link => (
            typedValuesEqual(
                this._scopeSerializedEndpointId(runtime, link.origin_id),
                expected.source_node_id,
            )
            && link.origin_slot === expected.source_output_index
            && typedValuesEqual(
                this._scopeSerializedEndpointId(runtime, link.target_id),
                expected.target_node_id,
            )
            && link.target_slot === expected.target_input_index
        ));
        if (matches.length !== 1) {
            throw new Error(`Expected one exact scoped connection to disconnect; found ${matches.length}.`);
        }
        if (typedValuesEqual(expected.source_node_id, runtime.inputRuntimeId)) {
            const target = this._scopeGraphNode(runtime, expected.target_node_id);
            if (!target?.disconnectInput?.(expected.target_input_index)) {
                throw new Error("The scoped input boundary connection could not be disconnected.");
            }
        } else if (typedValuesEqual(expected.target_node_id, runtime.outputRuntimeId)) {
            const slot = graph.outputs?.[expected.target_input_index];
            if (!slot || typeof slot.disconnect !== "function") {
                throw new Error("The scoped output boundary connection cannot be disconnected.");
            }
            slot.disconnect();
        } else {
            const target = this._scopeGraphNode(runtime, expected.target_node_id);
            if (!target?.disconnectInput?.(expected.target_input_index)) {
                throw new Error("The scoped internal connection could not be disconnected.");
            }
        }
        const remaining = this._scopeGraphLinkValues(graph).filter(link => (
            typedValuesEqual(
                this._scopeSerializedEndpointId(runtime, link.origin_id),
                expected.source_node_id,
            )
            && link.origin_slot === expected.source_output_index
            && typedValuesEqual(
                this._scopeSerializedEndpointId(runtime, link.target_id),
                expected.target_node_id,
            )
            && link.target_slot === expected.target_input_index
        ));
        if (remaining.length !== 0) throw new Error("The exact scoped connection persisted after disconnect.");
        this._markGraphChanged();
        return { disconnected: true, ...expected };
    }

    _connectGraphPatchScopeNodes(runtime, sourceId, targetId, connection) {
        const graph = this._assertGraphPatchScopeRuntime(runtime);
        const sourceSlot = connection?.source_output_index;
        const targetSlot = connection?.target_input_index;
        if (!Number.isInteger(sourceSlot) || sourceSlot < 0 || !Number.isInteger(targetSlot) || targetSlot < 0) {
            throw new Error("Exact scoped connection indices are required.");
        }
        if (
            typedValuesEqual(sourceId, runtime.inputRuntimeId)
            && typedValuesEqual(targetId, runtime.outputRuntimeId)
        ) {
            throw graphPatchScopeError(
                "direct_scope_boundary_edge_unsupported",
                "Direct input-to-output boundary links are not supported by the native Subgraph API.",
            );
        }
        let link;
        if (typedValuesEqual(sourceId, runtime.inputRuntimeId)) {
            const boundary = graph.inputNode?.slots?.[sourceSlot];
            const target = this._scopeGraphNode(runtime, targetId);
            const input = target?.inputs?.[targetSlot];
            if (!boundary || !target || !input || input.link != null) {
                throw new Error("The exact scoped input-boundary target is unavailable.");
            }
            link = boundary.connect(input, target);
        } else if (typedValuesEqual(targetId, runtime.outputRuntimeId)) {
            const boundary = graph.outputNode?.slots?.[targetSlot];
            const source = this._scopeGraphNode(runtime, sourceId);
            const output = source?.outputs?.[sourceSlot];
            if (!boundary || !source || !output || boundary.linkIds?.length) {
                throw new Error("The exact scoped output-boundary source is unavailable.");
            }
            link = boundary.connect(output, source);
        } else {
            const source = this._scopeGraphNode(runtime, sourceId);
            const target = this._scopeGraphNode(runtime, targetId);
            if (!source || !target || target.inputs?.[targetSlot]?.link != null) {
                throw new Error("The exact scoped internal endpoints are unavailable.");
            }
            link = source.connect(sourceSlot, target, targetSlot);
        }
        if (
            !link
            || !typedValuesEqual(
                this._scopeSerializedEndpointId(runtime, link.origin_id),
                sourceId,
            )
            || link.origin_slot !== sourceSlot
            || !typedValuesEqual(
                this._scopeSerializedEndpointId(runtime, link.target_id),
                targetId,
            )
            || link.target_slot !== targetSlot
        ) {
            throw new Error("ComfyUI rejected or redirected the exact scoped connection.");
        }
        this._markGraphChanged();
        return {
            id: link.id ?? null,
            source_node_id: sourceId,
            source_output_index: link.origin_slot,
            source_output: this._scopeSlotName(runtime, sourceId, link.origin_slot, true),
            target_node_id: targetId,
            target_input_index: link.target_slot,
            target_input: this._scopeSlotName(runtime, targetId, link.target_slot, false),
            type: link.type ?? null,
        };
    }

    _removeGraphPatchScopeNodes(runtime, nodeIds) {
        const graph = this._assertGraphPatchScopeRuntime(runtime);
        let removed = 0;
        for (const nodeId of nodeIds) {
            const node = this._scopeGraphNode(runtime, nodeId);
            if (!node) continue;
            graph.remove(node);
            removed += 1;
        }
        if (removed > 0) this._markGraphChanged();
        return { removed };
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
        const source = this._workflowNodeFromSerializedId(expected.source_node_id, pin);
        const target = this._workflowNodeFromSerializedId(expected.target_node_id, pin);
        const authority = structuredClone(app.graph.serialize());
        const matches = this._workflowLinkEntries().filter(([, link]) => (
            link
            && this._findRuntimeEndpointNode(app.graph, link.origin_id, {
                serializedGraph: authority,
            }) === source
            && link.origin_slot === expected.source_output_index
            && this._findRuntimeEndpointNode(app.graph, link.target_id, {
                serializedGraph: authority,
            }) === target
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
        const expectedSnapshot = structuredClone(snapshot);
        const expectedCanonical = canonicalWorkflowJSON(expectedSnapshot);
        const expectedGraphHash = await workflowGraphHash(expectedSnapshot);
        await app.loadGraphData(
            structuredClone(expectedSnapshot),
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
        const restoredSnapshot = structuredClone(app.graph.serialize());
        const restoredGraphHash = await workflowGraphHash(restoredSnapshot);
        if (
            canonicalWorkflowJSON(restoredSnapshot) !== expectedCanonical
            || restoredGraphHash !== expectedGraphHash
        ) {
            throw new Error(
                "The workflow snapshot did not restore exactly; preview hydration was skipped.",
            );
        }
        // Hydration must happen only after exact rollback verification. This
        // helper is deliberately presentation-only and cannot change anything
        // emitted by graph.serialize().
        this.hydrateNestedImagePreviews();
        this.assertActiveWorkflow(pin);
        this._markCanvasDirty();
        return {
            restored: true,
            workflow_identity_verified: true,
            snapshot_restored: true,
            hash_verified: true,
            graph_hash: restoredGraphHash,
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

    /** Remove nodes addressed only by canonical serialized workflow IDs. */
    removeWorkflowNodesExact(nodeIds, pin = null) {
        if (!Array.isArray(nodeIds)) throw new Error("Exact workflow node IDs are required.");
        const nodes = nodeIds.map(nodeId => this._workflowNodeFromSerializedId(nodeId, pin));
        for (const node of nodes) app.graph.remove(node);
        if (nodes.length > 0) this._markGraphChanged();
        return { removed: nodes.length };
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
     * Select and reveal one backend-resolved workflow branch without ever
     * resolving titles, node types, or partial scope paths in the browser.
     * All fallible request, workflow, scope, and node checks happen before the
     * first canvas effect.
     */
    async navigateWorkflowBranchExact(request) {
        const branchId = request?.branch_id;
        const expectedWorkflowIdentity = request?.expected_workflow_identity;
        const expectedGraphHash = request?.expected_graph_hash;
        const rawScopePath = request?.scope_path;
        const requestedNodeIds = request?.node_ids;
        if (typeof branchId !== "string" || !branchId || branchId.length > 512) {
            throw branchNavigationError(
                "invalid_branch_navigation",
                "A canonical branch ID is required for branch navigation.",
            );
        }
        if (
            typeof expectedWorkflowIdentity !== "string"
            || !expectedWorkflowIdentity
            || !SHA256_PATTERN.test(String(expectedGraphHash || ""))
            || !Array.isArray(rawScopePath)
            || !Array.isArray(requestedNodeIds)
            || requestedNodeIds.length === 0
        ) {
            throw branchNavigationError(
                "invalid_branch_navigation",
                "Exact workflow identity, graph hash, scope path, and node IDs are required.",
            );
        }

        const scopePath = rawScopePath.map((segment, index) => {
            const containerNodeId = segment?.container_node_id;
            const subgraphId = segment?.subgraph_id;
            const validContainerId = (
                (typeof containerNodeId === "number" && Number.isInteger(containerNodeId))
                || (typeof containerNodeId === "string" && containerNodeId.length > 0)
            );
            if (
                !segment
                || typeof segment !== "object"
                || Array.isArray(segment)
                || !validContainerId
                || typeof subgraphId !== "string"
                || !subgraphId
            ) {
                throw branchNavigationError(
                    "invalid_branch_navigation",
                    `scope_path[${index}] is not an exact subgraph scope segment.`,
                );
            }
            return {
                container_node_id: containerNodeId,
                subgraph_id: subgraphId,
            };
        });

        const requestedKeys = new Set();
        for (const [index, nodeId] of requestedNodeIds.entries()) {
            const validNodeId = (
                (typeof nodeId === "number" && Number.isInteger(nodeId))
                || (typeof nodeId === "string" && nodeId.length > 0)
            );
            if (!validNodeId) {
                throw branchNavigationError(
                    "invalid_branch_navigation",
                    `node_ids[${index}] is not an exact node ID.`,
                );
            }
            const key = `${typeof nodeId}:${String(nodeId)}`;
            if (requestedKeys.has(key)) {
                throw branchNavigationError(
                    "branch_node_ambiguous",
                    `Branch navigation repeats node ID ${String(nodeId)}.`,
                    { node_id: nodeId },
                );
            }
            requestedKeys.add(key);
        }

        const pin = this.pinActiveWorkflow(expectedWorkflowIdentity);
        const initialRootState = await this._verifyBranchRootHash(
            pin,
            expectedGraphHash,
        );
        const rootGraph = initialRootState.rootGraph;
        const targetScope = this._resolveBranchGraphScope(
            rootGraph,
            scopePath,
            initialRootState.snapshot,
        );
        const targetGraph = targetScope.graph;
        const nodes = requestedNodeIds.map(nodeId => (
            this._findExactNodeInGraph(
                targetGraph,
                nodeId,
                "branch",
                targetScope.serializedGraph,
            )
        ));

        const canvas = app.canvas;
        if (
            !canvas
            || (canvas.graph !== targetGraph && typeof canvas.setGraph !== "function")
            || (
                typeof canvas.selectItems !== "function"
                && typeof canvas.selectNodes !== "function"
            )
        ) {
            throw branchNavigationError(
                "branch_navigation_unavailable",
                "The current ComfyUI canvas cannot navigate to an exact branch.",
            );
        }

        // Hash once more after every async validation step. Scope/node
        // resolution above is synchronous, so no stale branch can slip between
        // this check and the first canvas effect.
        const preEffectRootState = await this._verifyBranchRootHash(
            pin,
            expectedGraphHash,
            rootGraph,
        );
        this._assertBranchRootState(pin, preEffectRootState);
        const previousCanvasState = this._captureCanvasNavigationState(canvas);
        let canvasEffectStarted = false;
        try {
            canvasEffectStarted = true;
            if (canvas.graph !== targetGraph) canvas.setGraph(targetGraph);
            if (canvas.graph !== targetGraph) {
                throw branchNavigationError(
                    "branch_navigation_verification_failed",
                    "ComfyUI did not activate the exact branch scope.",
                );
            }

            if (typeof canvas.selectItems === "function") {
                canvas.selectItems(nodes, false);
            } else {
                canvas.selectNodes(nodes, false);
            }
            const fitMethod = await this._fitExactCanvasSelection(nodes);
            this.assertActiveWorkflow(pin);

            this._assertExactBranchCanvasState(canvas, targetGraph, nodes);
            const finalRootState = await this._verifyBranchRootHash(
                pin,
                expectedGraphHash,
                rootGraph,
            );
            // The final digest is asynchronous too. Close both remaining race
            // windows synchronously before returning success: the root graph,
            // resolved scope, active canvas graph, and exact selection must all
            // still be the attested objects.
            this._assertBranchRootState(pin, finalRootState);
            const finalScope = this._resolveBranchGraphScope(
                rootGraph,
                scopePath,
                finalRootState.snapshot,
            );
            if (finalScope.graph !== targetGraph) {
                throw branchNavigationError(
                    "branch_navigation_verification_failed",
                    "The resolved branch scope changed during navigation.",
                );
            }
            this._assertExactBranchCanvasState(canvas, targetGraph, nodes);
            this._markCanvasDirty();
            return {
                branch_id: branchId,
                workflow_identity: expectedWorkflowIdentity,
                graph_hash: finalRootState.graphHash,
                scope_path: structuredClone(scopePath),
                scope_graph_id: targetGraph.id ?? null,
                selected_node_ids: structuredClone(requestedNodeIds),
                selected_count: nodes.length,
                fitted_count: nodes.length,
                fit_method: fitMethod,
                queued: false,
            };
        } catch (error) {
            if (canvasEffectStarted) {
                const restore = this._restoreCanvasNavigationState(
                    canvas,
                    previousCanvasState,
                );
                if (!restore.valid && error && typeof error === "object") {
                    let originalDetails = null;
                    if (error.details !== undefined) {
                        try {
                            originalDetails = structuredClone(error.details);
                        } catch (_) {
                            originalDetails = String(error.details);
                        }
                    }
                    error.details = {
                        code: "branch_navigation_restore_failed",
                        message: "The prior canvas graph, selection, or viewport could not be restored exactly.",
                        issues: restore.issues,
                        cause: {
                            code: error.code || "branch_navigation_failed",
                            message: String(error.message || error),
                            details: originalDetails,
                        },
                    };
                }
            }
            throw error;
        }
    }

    /**
     * Get currently selected nodes with full details
     * @returns {Array<object>} Array of selected node objects
     */
    getSelectedNodes(pin = null) {
        try {
            if (pin) this.assertActiveWorkflow(pin);
            const selectedNodes = Object.values(app.canvas.selected_nodes || {});
            const authority = pin ? structuredClone(app.graph.serialize()) : null;
            const result = [];
            
            for (const node of selectedNodes) {
                const projection = pin
                    ? this._projectRuntimeNode(app.graph, authority, node, {
                        label: "Selected workflow",
                    })
                    : null;
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
                    id: projection?.serializedId ?? node.id,
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
            if (pin) this.assertActiveWorkflow(pin);
            
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
    async setValuesExact(nodeId, values, pin = null) {
        const node = this._workflowNodeFromSerializedId(nodeId, pin);
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
    async convertWidgetToInputExact(nodeId, expected, pin = null) {
        const node = this._workflowNodeFromSerializedId(nodeId, pin);
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
    assignAttachmentExact(nodeId, attachment, pin = null) {
        const node = this._workflowNodeFromSerializedId(nodeId, pin);
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

    verifyAttachmentExact(nodeId, attachment, pin = null) {
        const projection = this._workflowNodeProjection(nodeId, pin, true);
        if (!projection) return false;
        const node = projection.node;
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

    _nodeImageRefs(node, canonicalNodeId, {
        includePending = true,
        allowMissing = false,
    } = {}) {
        const pendingReview = [...this.pendingMaskReviews.values()].find(
            value => typedValuesEqual(value.nodeId, canonicalNodeId),
        );
        const imageWidget = node.widgets?.find(widget => widget.name === "image");
        const widgetRef = parseImageWidgetRef(imageWidget?.value);
        const refs = [];
        const seen = new Set();
        const addRef = (image, imageIndex, pending = false) => {
            if (!image?.filename) return;
            const normalized = structuredClone({
                filename: String(image.filename),
                subfolder: String(image.subfolder || ""),
                type: String(image.type || (imageIndex === null ? "input" : "output")),
            });
            const key = JSON.stringify([
                normalized.type,
                normalized.subfolder,
                normalized.filename,
            ]);
            if (seen.has(key)) return;
            seen.add(key);
            refs.push({
                node_id: canonicalNodeId,
                node_type: node.comfyClass || node.type,
                title: node.title,
                image: normalized,
                image_index: imageIndex,
                pending_review: pending,
                ...(pending && pendingReview?.originalImage
                    ? { original_image: structuredClone(pendingReview.originalImage) }
                    : {}),
            });
        };

        // A pending review is the exact image the next mask edit will use, so it
        // replaces the committed image widget in this current-source projection.
        if (includePending && pendingReview?.image) {
            addRef(pendingReview.image, null, true);
        } else {
            addRef(widgetRef, null, false);
        }
        for (const [imageIndex, image] of (node.images || []).entries()) {
            addRef(image, imageIndex, false);
        }
        if (!refs.length) {
            if (allowMissing) return null;
            throw new Error(`Node ${canonicalNodeId} does not reference a ComfyUI image`);
        }
        return refs;
    }

    _nodeImageRef(node, canonicalNodeId, options = {}) {
        return this._nodeImageRefs(node, canonicalNodeId, options)?.[0] || null;
    }

    getNodeImageRef(nodeId, pin = null, { includePending = true } = {}) {
        const projection = pin ? this._workflowNodeProjection(nodeId, pin, false) : null;
        const node = projection?.node || this._findNode(nodeId);
        if (!node) {
            throw new Error(`Node not found: ${nodeId}`);
        }
        return this._nodeImageRef(
            node,
            projection?.serializedId ?? node.id,
            { includePending },
        );
    }

    /** Return a stable, bounded page of exact image references on the active canvas. */
    getCanvasImageRefs({
        nodeIds = null,
        offset = 0,
        limit = 8,
    } = {}, pin = null) {
        if (pin) this.assertActiveWorkflow(pin);
        if (!Number.isSafeInteger(offset) || offset < 0) {
            throw new Error("Canvas image offset must be a nonnegative safe integer.");
        }
        if (!Number.isSafeInteger(limit) || limit < 1 || limit > 8) {
            throw new Error("Canvas image limit must be between 1 and 8.");
        }
        if (
            nodeIds !== null
            && (
                !Array.isArray(nodeIds)
                || nodeIds.length < 1
                || nodeIds.length > 8
            )
        ) {
            throw new Error("Canvas image node_ids must contain between 1 and 8 exact IDs.");
        }
        if (
            Array.isArray(nodeIds)
            && new Set(nodeIds.map(typedNodeKey)).size !== nodeIds.length
        ) {
            throw new Error("Canvas image node_ids cannot contain duplicate exact IDs.");
        }

        const graph = app.graph;
        const authority = structuredClone(graph.serialize());
        const runtimeFacts = this._graphNodes(graph).map(node => ({
            node,
            serialized: this._serializeRuntimeNode(node),
        }));
        const bucketById = values => {
            const buckets = new Map();
            for (const value of values) {
                const nodeId = value?.id;
                const key = typedNodeKey(nodeId);
                const bucket = buckets.get(key) || [];
                bucket.push(value);
                buckets.set(key, bucket);
            }
            return buckets;
        };
        const authorityById = bucketById(this._serializedGraphNodes(authority));
        const runtimeByProjectedId = bucketById(runtimeFacts.map(fact => ({
            ...fact,
            id: fact.serialized?.id ?? fact.node?.id,
        })));
        const candidates = runtimeFacts
            .flatMap(({ node, serialized }) => {
                const canonicalNodeId = serialized?.id ?? node?.id;
                const results = this._nodeImageRefs(node, canonicalNodeId, {
                    includePending: true,
                    allowMissing: true,
                });
                if (!results) return [];
                const serializedPosition = serialized?.pos;
                const x = Number.isFinite(Number(serializedPosition?.[0]))
                    ? Number(serializedPosition[0])
                    : Number.isFinite(Number(node?.pos?.[0]))
                        ? Number(node.pos[0])
                        : 0;
                const y = Number.isFinite(Number(serializedPosition?.[1]))
                    ? Number(serializedPosition[1])
                    : Number.isFinite(Number(node?.pos?.[1]))
                        ? Number(node.pos[1])
                        : 0;
                return results.map(result => ({
                    node,
                    serialized,
                    canonicalNodeId,
                    x,
                    y,
                    result,
                }));
            });

        const verifyCandidateProjection = candidate => {
            const key = typedNodeKey(candidate.canonicalNodeId);
            const projectedMatches = runtimeByProjectedId.get(key) || [];
            const authorityMatches = authorityById.get(key) || [];
            if (projectedMatches.length !== 1 || authorityMatches.length !== 1) {
                throw this._nodeProjectionError(
                    projectedMatches.length > 1 || authorityMatches.length > 1
                        ? "workflow_node_projection_ambiguous"
                        : "workflow_node_projection_missing",
                    `Canvas image node ${String(candidate.canonicalNodeId)} has no exact projection.`,
                    candidate.canonicalNodeId,
                    Math.max(projectedMatches.length, authorityMatches.length),
                );
            }
            const liveType = candidate.node.comfyClass || candidate.node.type;
            const projectedType = candidate.serialized?.type ?? liveType;
            const authorityType = authorityMatches[0].type ?? projectedType;
            if (
                (liveType != null && projectedType != null && liveType !== projectedType)
                || (
                    projectedType != null
                    && authorityType != null
                    && projectedType !== authorityType
                )
            ) {
                throw this._nodeProjectionError(
                    "workflow_node_projection_ambiguous",
                    `Canvas image node ${String(candidate.canonicalNodeId)} conflicts with type authority.`,
                    candidate.canonicalNodeId,
                    1,
                );
            }
            return authorityMatches[0].id;
        };

        let ordered = candidates.sort((left, right) => (
            left.y - right.y
            || left.x - right.x
            || typedNodeKey(left.canonicalNodeId)
                .localeCompare(typedNodeKey(right.canonicalNodeId))
        ));
        if (Array.isArray(nodeIds)) {
            ordered = nodeIds.flatMap(nodeId => {
                const matches = candidates.filter(candidate => (
                    typedValuesEqual(candidate.canonicalNodeId, nodeId)
                ));
                if (!matches.length) {
                    const error = new Error(
                        `Canvas image node ${String(nodeId)} is unavailable or has no image.`,
                    );
                    error.code = "canvas_image_node_unavailable";
                    throw error;
                }
                return matches;
            });
        }

        const grouped = [];
        const groupedByImage = new Map();
        for (const candidate of ordered) {
            const image = candidate.result.image;
            const imageKey = JSON.stringify([
                String(image?.type || "input"),
                String(image?.subfolder || ""),
                String(image?.filename || ""),
            ]);
            let group = groupedByImage.get(imageKey);
            if (!group) {
                group = {
                    image: structuredClone(image),
                    pending_review: Boolean(candidate.result.pending_review),
                    sources: [],
                };
                groupedByImage.set(imageKey, group);
                grouped.push(group);
            }
            group.pending_review ||= Boolean(candidate.result.pending_review);
            group.sources.push(candidate);
        }

        const selected = grouped.slice(offset, offset + limit);
        const images = selected.map((group, pageIndex) => {
            const publicSources = group.sources.slice(0, 16).map(candidate => {
                return {
                    node_id: verifyCandidateProjection(candidate),
                    node_type: candidate.result.node_type,
                    title: candidate.result.title,
                    position: { x: candidate.x, y: candidate.y },
                    image_index: candidate.result.image_index,
                };
            });
            return {
                page_index: offset + pageIndex,
                image: group.image,
                pending_review: group.pending_review,
                source_count: group.sources.length,
                sources: publicSources,
                sources_truncated: group.sources.length > publicSources.length,
            };
        });
        if (pin) this.assertActiveWorkflow(pin);
        const nextOffset = offset + images.length;
        return {
            images,
            total_count: grouped.length,
            offset,
            limit,
            has_more: nextOffset < grouped.length,
            next_offset: nextOffset < grouped.length ? nextOffset : null,
            deduplicated: true,
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
            ([, value]) => typedValuesEqual(value.nodeId, node.id)
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

    async editNodeMask(
        nodeId,
        regions,
        coordinateSpace = "pixels",
        clearExisting = false,
        exactContext = null,
    ) {
        const workflowPin = exactContext?.workflowPin || null;
        const node = workflowPin
            ? this._workflowNodeFromSerializedId(nodeId, workflowPin)
            : this._findNode(nodeId);
        if (!node) {
            throw new Error(`Node not found: ${nodeId}`);
        }
        const imageWidget = node.widgets?.find(widget => widget.name === "image");
        if (!imageWidget) {
            throw new Error(`Node ${nodeId} has no image widget to receive the edited mask`);
        }

        // In an exact workflow transaction `nodeId` is the canonical serialized
        // ID. The live LiteGraph node may expose a string projection for that
        // same numeric ID, so pending review authority must never be keyed by
        // or return the runtime node.id.
        const reviewNodeId = workflowPin ? nodeId : node.id;
        const existingReviewEntry = [...this.pendingMaskReviews.entries()].find(
            ([, value]) => typedValuesEqual(value.nodeId, reviewNodeId)
        );
        const existingReview = existingReviewEntry?.[1];
        const originalImage = existingReview?.originalImage
            || this.getNodeImageRef(nodeId, workflowPin).image;
        const source = existingReview?.image || originalImage;
        const committedImage = this.getNodeImageRef(
            nodeId,
            workflowPin,
            { includePending: false },
        ).image;
        if (!exactContext?.expectedSourceAttestation) {
            throw maskSourcePreconditionError(
                "An exact source-byte attestation is required before editing a mask.",
            );
        }
        // This runs under ToolExecutor's shared canvas mutation lock. Fetch one
        // source blob, hash those exact bytes before drawing, decode it once,
        // and derive both color and alpha from that single bitmap.
        const {
            blob: sourceBlob,
            image: sourceImage,
            attestation: sourceAttestation,
        } = (
            await this._loadComfyImageExact(source, exactContext.expectedSourceAttestation)
        );
        const sourceWidth = sourceImage.width;
        const sourceHeight = sourceImage.height;

        const maskCanvas = document.createElement("canvas");
        maskCanvas.width = sourceImage.width;
        maskCanvas.height = sourceImage.height;
        const maskContext = maskCanvas.getContext("2d");
        const alphaCanvas = document.createElement("canvas");
        alphaCanvas.width = sourceImage.width;
        alphaCanvas.height = sourceImage.height;
        const alphaContext = alphaCanvas.getContext("2d");
        alphaContext.drawImage(sourceImage, 0, 0);
        const sourceAlpha = alphaContext.getImageData(
            0,
            0,
            sourceImage.width,
            sourceImage.height,
        );
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
            drawMaskRegionPath(regionContext, region);
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
        const outputAlphaCanvas = document.createElement("canvas");
        outputAlphaCanvas.width = sourceWidth;
        outputAlphaCanvas.height = sourceHeight;
        const outputAlphaContext = outputAlphaCanvas.getContext("2d");
        const outputAlphaPixels = outputAlphaContext.createImageData(sourceWidth, sourceHeight);
        for (let index = 0; index < outputAlphaPixels.data.length; index += 4) {
            outputAlphaPixels.data[index] = 255;
            outputAlphaPixels.data[index + 1] = 255;
            outputAlphaPixels.data[index + 2] = 255;
            outputAlphaPixels.data[index + 3] = 255 - editedMask.data[index + 3];
        }
        outputAlphaContext.putImageData(outputAlphaPixels, 0, 0);
        const alphaBlob = await this._canvasToBlob(outputAlphaCanvas);
        sourceImage.close?.();

        // Canvas is allowed to construct alpha, but never the execution RGB:
        // drawing transparent source pixels would premultiply and destroy their
        // hidden color. The Comfy route composes this alpha over the same exact
        // immutable source Blob that was hashed above.
        const formData = buildExactMaskComposeFormData(
            sourceBlob,
            alphaBlob,
            sourceAttestation,
        );
        const response = await api.fetchApi("/fl_mcp/mask/compose", {
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
        if (
            !uploaded?.name
            || uploaded.source_sha256 !== sourceAttestation.sha256
            || uploaded.width !== sourceWidth
            || uploaded.height !== sourceHeight
        ) {
            throw new Error("Mask compose response did not attest the exact full-size source");
        }
        const image = {
            filename: uploaded.name,
            subfolder: uploaded.subfolder || "",
            type: uploaded.type || "input",
        };
        // Review the immutable composed file's RGB channel. It makes a cleared
        // old mask visible again without letting alpha premultiplication turn
        // its recovered pixels black; magenta remains presentation-only.
        const reviewBaseImage = await this._loadComfyImageChannel(image, "rgb");
        const reviewCanvas = document.createElement("canvas");
        reviewCanvas.width = sourceWidth;
        reviewCanvas.height = sourceHeight;
        const reviewContext = reviewCanvas.getContext("2d");
        reviewContext.drawImage(reviewBaseImage, 0, 0);
        reviewBaseImage.close?.();
        const highlightCanvas = document.createElement("canvas");
        highlightCanvas.width = sourceWidth;
        highlightCanvas.height = sourceHeight;
        const highlightContext = highlightCanvas.getContext("2d");
        highlightContext.fillStyle = "#ff00a8";
        highlightContext.fillRect(0, 0, highlightCanvas.width, highlightCanvas.height);
        highlightContext.globalCompositeOperation = "destination-in";
        highlightContext.drawImage(maskCanvas, 0, 0);
        reviewContext.globalAlpha = 0.62;
        reviewContext.drawImage(highlightCanvas, 0, 0);
        const reviewPreview = await this._canvasToImage(reviewCanvas);
        const reviewToken = crypto.randomUUID();
        if (exactContext) {
            await this.assertWorkflowMutationGuard(exactContext.mutationGuard);
            const currentSource = this.getNodeImageRef(nodeId, workflowPin).image;
            if (!(
                currentSource?.filename === exactContext.expectedSourceImage?.filename
                && (currentSource?.subfolder || "")
                    === (exactContext.expectedSourceImage?.subfolder || "")
                && (currentSource?.type || "input")
                    === (exactContext.expectedSourceImage?.type || "input")
            )) {
                throw new Error("The exact mask source image changed before preview creation.");
            }
        }
        if (this.pendingMaskReviews.size === 0) {
            this.maskReviewAutoQueueState = this._pauseAutoQueueForMaskReview();
        }
        if (existingReviewEntry) {
            this._releaseMaskReviewPreview(existingReview);
        }
        this.pendingMaskReviews.set(typedNodeKey(reviewNodeId), {
            token: reviewToken,
            nodeId: reviewNodeId,
            image,
            originalImage,
            committedImage,
            previewUrl: reviewPreview.url,
            workflowIdentity: workflowPin?.identity || null,
            graphHash: exactContext?.mutationGuard?.expectedGraphHash || null,
            sourceImage: structuredClone(source),
            sourceAttestation: structuredClone(sourceAttestation),
        });
        node.imgs = [reviewPreview.image];
        node.imageIndex = 0;
        app.canvas?.selectNodes?.([node]);
        app.canvas?.centerOnNode?.(node);
        this._markCanvasDirty();

        return {
            success: true,
            node_id: reviewNodeId,
            source_image: source,
            image,
            image_size: { width: sourceWidth, height: sourceHeight },
            source_attestation: sourceAttestation,
            coordinate_space: coordinateSpace,
            clear_existing: clearExisting,
            regions: normalizedRegions,
            mask: maskSummary,
            preview_visible: true,
            review_required: true,
            review_token: reviewToken,
        };
    }

    async confirmMaskReview(nodeId, reviewToken) {
        const pendingEntry = [...this.pendingMaskReviews.entries()].find(
            ([, value]) => typedValuesEqual(value.nodeId, nodeId)
        );
        const pending = pendingEntry?.[1];
        if (!pending) {
            throw new Error(`There is no edited mask on node ${nodeId} waiting for review`);
        }
        if (pending.token !== reviewToken) {
            throw new Error("This mask review is stale; inspect the latest mask before approving it");
        }
        const workflowPin = pending.workflowIdentity
            ? this.pinActiveWorkflow(pending.workflowIdentity)
            : null;
        let confirmGuard = null;
        if (workflowPin && pending.graphHash) {
            confirmGuard = await this.createWorkflowMutationGuard(workflowPin);
            if (confirmGuard.expectedGraphHash !== pending.graphHash) {
                throw new Error(
                    "The workflow changed after mask editing; inspect and edit the mask again.",
                );
            }
        }
        const node = workflowPin
            ? this._workflowNodeFromSerializedId(pending.nodeId, workflowPin)
            : this._findNode(pending.nodeId);
        if (!node) {
            throw new Error(`Node not found: ${pending.nodeId}`);
        }
        if (workflowPin) {
            const currentCommitted = this.getNodeImageRef(
                pending.nodeId,
                workflowPin,
                { includePending: false },
            ).image;
            if (!(
                currentCommitted?.filename === pending.committedImage?.filename
                && (currentCommitted?.subfolder || "")
                    === (pending.committedImage?.subfolder || "")
                && (currentCommitted?.type || "input")
                    === (pending.committedImage?.type || "input")
            )) {
                throw new Error(
                    "The mask target source changed after editing; inspect and edit it again.",
                );
            }
        }
        let committedGraphHash = pending.graphHash;
        try {
            this._assignImageToNode(node, pending.image);
            const committed = this.getNodeImageRef(
                pending.nodeId,
                workflowPin,
                { includePending: false },
            ).image;
            if (!(
                committed?.filename === pending.image?.filename
                && (committed?.subfolder || "") === (pending.image?.subfolder || "")
                && (committed?.type || "input") === (pending.image?.type || "input")
            )) {
                throw new Error("The approved mask image did not retain its exact value.");
            }
            if (confirmGuard) {
                committedGraphHash = await this.acceptWorkflowMutationGuard(confirmGuard);
            }
        } catch (assignmentError) {
            let rollbackComplete = false;
            try {
                this._assignImageToNode(node, pending.committedImage);
                const restoredImage = this.getNodeImageRef(
                    pending.nodeId,
                    workflowPin,
                    { includePending: false },
                ).image;
                const restoredGuard = workflowPin
                    ? await this.createWorkflowMutationGuard(workflowPin)
                    : null;
                rollbackComplete = Boolean(
                    restoredImage?.filename === pending.committedImage?.filename
                    && (restoredImage?.subfolder || "")
                        === (pending.committedImage?.subfolder || "")
                    && (restoredImage?.type || "input")
                        === (pending.committedImage?.type || "input")
                    && (!restoredGuard || restoredGuard.expectedGraphHash === pending.graphHash)
                );
            } catch (_) {
                rollbackComplete = false;
            }
            if (!rollbackComplete) {
                const compromised = new Error(
                    "Mask approval failed and the original source could not be restored exactly.",
                );
                compromised.code = "workflow_state_compromised";
                compromised.details = {
                    workflow_identity: pending.workflowIdentity,
                    mutation_quarantined: true,
                    recovery: "reload_or_reopen_workflow",
                    retryable: false,
                };
                throw compromised;
            }
            throw assignmentError;
        }
        const cleanupWarnings = [];
        try {
            this._releaseMaskReviewPreview(pending);
        } catch (error) {
            cleanupWarnings.push({
                phase: "release_mask_preview",
                message: String(error?.message || error),
            });
        }
        this.pendingMaskReviews.delete(pendingEntry[0]);
        // Notify the committed widget change while mask review still owns the
        // auto-queue pause. Restoring an enabled prior state before this call
        // could queue the workflow immediately on approval.
        try {
            this._markGraphChanged();
        } catch (error) {
            cleanupWarnings.push({
                phase: "notify_graph_changed",
                message: String(error?.message || error),
            });
        }
        if (this.pendingMaskReviews.size === 0) {
            try {
                this._restoreAutoQueueAfterMaskReview(this.maskReviewAutoQueueState);
            } catch (error) {
                // The mask commit is already verified. Preserve that success and
                // leave auto-queue fail-safe disabled rather than reporting an
                // unknown mutation outcome.
                try {
                    this._pauseAutoQueueForMaskReview();
                } catch (_) {
                    // Best effort only; surface the original cleanup error below.
                }
                cleanupWarnings.push({
                    phase: "restore_auto_queue",
                    message: String(error?.message || error),
                });
            }
            this.maskReviewAutoQueueState = null;
        }
        return {
            success: true,
            node_id: pending.nodeId,
            image: pending.image,
            review_token: pending.token,
            approved: true,
            queued: false,
            workflow_identity: pending.workflowIdentity,
            graph_hash: committedGraphHash,
            ...(cleanupWarnings.length > 0 ? { cleanup_warnings: cleanupWarnings } : {}),
            message: "The user approved this mask for workflow execution.",
        };
    }

    getPendingMaskReviewReceipt(nodeId) {
        const pending = [...this.pendingMaskReviews.values()].find(
            value => typedValuesEqual(value.nodeId, nodeId),
        );
        if (!pending) return null;
        return {
            node_id: pending.nodeId,
            review_token: pending.token,
            image: structuredClone(pending.image),
            source_image: structuredClone(pending.sourceImage),
            workflow_identity: pending.workflowIdentity,
            graph_hash: pending.graphHash,
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
        const sourceNode = this._workflowNodeFromSerializedId(sourceId, pin);
        const targetNode = this._workflowNodeFromSerializedId(targetId, pin);

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
        const authority = structuredClone(app.graph.serialize());
        const endpointsMatch = (
            this._findRuntimeEndpointNode(app.graph, link.origin_id, {
                serializedGraph: authority,
            }) === sourceNode
            && link.origin_slot === sourceSlot
            && this._findRuntimeEndpointNode(app.graph, link.target_id, {
                serializedGraph: authority,
            }) === targetNode
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
            source_node_id: sourceId,
            source_output_index: link.origin_slot,
            source_output: sourceNode.outputs[sourceSlot]?.name ?? null,
            target_node_id: targetId,
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

    /** Set a node rectangle through one canonical serialized workflow ID. */
    setWorkflowNodeRectExact(nodeId, rect, pin = null) {
        const node = this._workflowNodeFromSerializedId(nodeId, pin);
        if (typeof rect.x === "number") node.pos[0] = rect.x;
        if (typeof rect.y === "number") node.pos[1] = rect.y;
        if (typeof rect.width === "number") node.size[0] = rect.width;
        if (typeof rect.height === "number") node.size[1] = rect.height;
        this._markGraphChanged();
        return {
            x: node.pos[0],
            y: node.pos[1],
            width: node.size[0],
            height: node.size[1],
        };
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

    getActiveWorkflowContext() {
        const workflowStore = this._unwrap(app.extensionManager?.workflow);
        const workflow = this._unwrap(workflowStore?.activeWorkflow);
        if (!workflow) return null;

        const workflowId = this._workflowId(workflow) || app.rootGraph?.id;
        if (!workflowId) return null;

        return {
            id: String(workflowId),
            name: workflow.name || workflow.filename || workflow.fullFilename || workflow.key || "Untitled Workflow",
            path: workflow.path || null,
            temporary: Boolean(workflow.isTemporary)
        };
    }

    async activateWorkflowContext(workflowId) {
        const workflowStore = this._unwrap(app.extensionManager?.workflow);
        const openWorkflows = this._unwrap(workflowStore?.openWorkflows) || [];
        const workflow = Array.from(openWorkflows).find(item => (
            String(this._serializeWorkflowTab(item)?.id || "") === String(workflowId)
        ));
        if (!workflow) {
            throw new Error("The conversation's workflow is not open");
        }
        if (typeof workflowStore?.openWorkflow !== "function") {
            throw new Error("ComfyUI workflow switching is unavailable");
        }
        await workflowStore.openWorkflow(workflow);
        return this.getActiveWorkflowContext();
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
    async _boundedQueueRecoveryJson(path, maxBytes = 8 * 1024 * 1024) {
        const response = await api.fetchApi(path, { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`Queue recovery read failed (${response.status}).`);
        }
        const declared = Number(response.headers.get("content-length"));
        if (Number.isFinite(declared) && declared > maxBytes) {
            throw new Error("Queue recovery response exceeds the byte limit.");
        }
        if (!response.body?.getReader) {
            throw new Error("Queue recovery requires a bounded response stream.");
        }
        const reader = response.body.getReader();
        const chunks = [];
        let total = 0;
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                if (!(value instanceof Uint8Array)) {
                    throw new Error("Queue recovery returned an invalid byte stream.");
                }
                total += value.byteLength;
                if (total > maxBytes) {
                    throw new Error("Queue recovery response exceeds the byte limit.");
                }
                chunks.push(value);
            }
        } finally {
            reader.releaseLock();
        }
        const bytes = new Uint8Array(total);
        let offset = 0;
        for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.byteLength;
        }
        return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    }

    async recoverQueuedOperation(operationId, operationRequestHash, { promptId = null } = {}) {
        let queue;
        let history;
        try {
            [queue, history] = await Promise.all([
                this._boundedQueueRecoveryJson("/queue"),
                this._boundedQueueRecoveryJson(
                    promptId
                        ? `/history/${encodeURIComponent(promptId)}`
                        : "/history?max_items=100",
                ),
            ]);
        } catch (cause) {
            const error = new Error(
                "The prior queue outcome cannot be checked safely; it was not queued again.",
                { cause },
            );
            error.code = "queue_recovery_unavailable";
            throw error;
        }
        const recovered = await recoverQueueOperationFromPayloads(
            { queue, history },
            { operationId, operationRequestHash },
        );
        if (!recovered || (promptId && recovered.prompt_id !== promptId)) {
            const error = new Error("No matching queued execution exists for this operation.");
            error.code = "narrow_edit_operation_not_found";
            throw error;
        }
        return recovered;
    }

    async attestQueuedOperation(operationId, operationRequestHash, promptId) {
        try {
            const recovered = await this.recoverQueuedOperation(
                operationId,
                operationRequestHash,
                { promptId },
            );
            return recovered.prompt_id === promptId;
        } catch {
            return false;
        }
    }

    async queueWorkflow(batchCount = null, operation = null) {
        let queueSubmissionAttempted = false;
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
            if (effectiveBatchCount !== 1) {
                throw new Error(
                    "Ren queues exactly one workflow at a time so each execution has one auditable provenance record. Set batch count to 1 and retry.",
                );
            }
            let queueResult = null;
            let executionProvenance = null;

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
                const originalGraphToPrompt = app.graphToPrompt;
                let renQueueRequestActive = false;
                const graphToPromptWithProvenance = async (...args) => {
                    const prompt = await originalGraphToPrompt.apply(app, args);
                    if (!renQueueRequestActive) return prompt;
                    const prepared = await prepareExecutionSubmission(prompt, operation);
                    executionProvenance = prepared.provenance;
                    return prepared.submission;
                };
                app.graphToPrompt = graphToPromptWithProvenance;
                try {
                    const captured = await captureAuthenticatedQueue(
                        api,
                        () => app.queuePrompt(0, effectiveBatchCount),
                        {
                            onRequestActiveChange: active => {
                                renQueueRequestActive = active;
                            },
                            onSubmitting: () => {
                                queueSubmissionAttempted = true;
                            },
                            shouldCapture: (args, { requestActive }) => (
                                requestActive
                                && submissionCarriesExecutionProvenance(
                                    args[1],
                                    executionProvenance,
                                )
                            ),
                            prepare: async args => ({
                                args,
                                metadata: executionProvenanceFromSubmission(args[1]),
                            }),
                        },
                    );
                    queueResult = captured.result;
                    executionProvenance = captured.metadata;
                    if (
                        typeof queueResult?.prompt_id !== "string"
                        && queueResult?.node_errors
                        && Object.keys(queueResult.node_errors).length
                    ) {
                        return {
                            queued: false,
                            batch_count: effectiveBatchCount,
                            prompt_id: null,
                            queue_number: null,
                            node_errors: { redacted_validation_failure: true },
                        };
                    }
                    if (
                        !captured.accepted
                        || typeof queueResult?.prompt_id !== "string"
                        || queueResult.prompt_id.length === 0
                        || !executionProvenance
                    ) {
                        throw new Error("ComfyUI did not accept the workflow for queueing.");
                    }
                } finally {
                    renQueueRequestActive = false;
                    if (app.graphToPrompt === graphToPromptWithProvenance) {
                        app.graphToPrompt = originalGraphToPrompt;
                    }
                }
            } else {
                const prompt = await app.graphToPrompt();
                const prepared = await prepareExecutionSubmission(prompt, operation);
                for (let i = 0; i < effectiveBatchCount; i++) {
                    queueSubmissionAttempted = true;
                    queueResult = await api.queuePrompt(0, prepared.submission);
                }
                executionProvenance = prepared.provenance;
            }

            if (
                typeof queueResult?.prompt_id !== "string"
                && queueResult?.node_errors
                && Object.keys(queueResult.node_errors).length
            ) {
                return {
                    queued: false,
                    batch_count: effectiveBatchCount,
                    prompt_id: null,
                    queue_number: null,
                    node_errors: { redacted_validation_failure: true },
                };
            }
            if (typeof queueResult?.prompt_id !== "string" || queueResult.prompt_id.length === 0) {
                throw new Error("ComfyUI did not return an accepted prompt ID.");
            }

            console.log(`[FL_API] Queued workflow (batch: ${effectiveBatchCount})`);
            
            // Return comprehensive queue information
            return { 
                queued: true, 
                batch_count: effectiveBatchCount,
                prompt_id: queueResult.prompt_id,
                queue_number: queueResult.number,
                node_errors: queueResult.node_errors
                    && Object.keys(queueResult.node_errors).length
                    ? { redacted_validation_warning: true }
                    : {},
                execution_provenance: executionProvenance,
            };
        } catch (error) {
            const knownPromptRejection = (
                queueSubmissionAttempted
                && error?.status === 400
                && error?.response
                && typeof error.response === "object"
                && (
                    error.response.error
                    || (
                        error.response.node_errors
                        && typeof error.response.node_errors === "object"
                    )
                )
                && typeof error.response.prompt_id !== "string"
            );
            if (knownPromptRejection) {
                return {
                    queued: false,
                    batch_count: 1,
                    prompt_id: null,
                    queue_number: null,
                    node_errors: { redacted_validation_failure: true },
                };
            }
            console.error("[FL_API] queueWorkflow failed with a redacted error code.");
            if (queueSubmissionAttempted && operation) {
                try {
                    return await this.recoverQueuedOperation(
                        operation.operationId,
                        operation.operationRequestHash,
                    );
                } catch (recoveryError) {
                    if (recoveryError?.code !== "narrow_edit_operation_not_found") {
                        throw recoveryError;
                    }
                    const unknown = new Error(
                        "The queue response was lost and no accepted execution is visible yet. "
                        + "The operation is tombstoned and will not be queued again automatically.",
                        { cause: error },
                    );
                    unknown.code = "queue_outcome_unknown";
                    throw unknown;
                }
            }
            if (!queueSubmissionAttempted && !error?.code) {
                error.code = "queue_submission_not_started";
            }
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

    _captureBranchRootState(pin) {
        this.assertActiveWorkflow(pin);
        const rootGraphBefore = app.rootGraph || app.graph;
        if (!rootGraphBefore) {
            throw branchNavigationError(
                "branch_scope_not_found",
                "The active root graph is unavailable.",
            );
        }
        const snapshot = this.captureWorkflowSnapshot(pin);
        const rootGraphAfter = app.rootGraph || app.graph;
        this.assertActiveWorkflow(pin);
        if (rootGraphAfter !== rootGraphBefore) {
            throw branchNavigationError(
                "branch_navigation_precondition_failed",
                "The active root graph changed while the branch was being verified.",
                { reason: "root_graph_identity_changed" },
            );
        }
        return {
            rootGraph: rootGraphAfter,
            snapshot,
            token: canonicalWorkflowJSON(snapshot),
        };
    }

    async _verifyBranchRootHash(pin, expectedGraphHash, expectedRootGraph = null) {
        const before = this._captureBranchRootState(pin);
        if (expectedRootGraph && before.rootGraph !== expectedRootGraph) {
            throw branchNavigationError(
                "branch_navigation_precondition_failed",
                "The active root graph instance no longer matches the discovered branch.",
                { reason: "root_graph_identity_changed" },
            );
        }
        const actualGraphHash = await workflowGraphHash(before.snapshot);
        const after = this._captureBranchRootState(pin);
        if (
            after.rootGraph !== before.rootGraph
            || (expectedRootGraph && after.rootGraph !== expectedRootGraph)
            || after.token !== before.token
        ) {
            throw branchNavigationError(
                "branch_navigation_precondition_failed",
                "The active graph changed while its branch hash was being verified.",
                {
                    reason: "graph_changed_during_hash",
                    expected_graph_hash: expectedGraphHash,
                    verified_graph_hash: actualGraphHash,
                },
            );
        }
        if (actualGraphHash !== expectedGraphHash) {
            throw branchNavigationError(
                "branch_navigation_precondition_failed",
                "The active graph no longer matches the discovered branch.",
                {
                    reason: "graph_hash_mismatch",
                    expected_graph_hash: expectedGraphHash,
                    actual_graph_hash: actualGraphHash,
                },
            );
        }
        return { ...after, graphHash: actualGraphHash };
    }

    _assertBranchRootState(pin, expected) {
        const observed = this._captureBranchRootState(pin);
        if (
            observed.rootGraph !== expected.rootGraph
            || observed.token !== expected.token
        ) {
            throw branchNavigationError(
                "branch_navigation_precondition_failed",
                "The active graph changed after branch verification.",
                {
                    reason: "graph_changed_after_hash",
                    verified_graph_hash: expected.graphHash,
                },
            );
        }
        return observed;
    }

    _assertExactBranchCanvasState(canvas, targetGraph, nodes) {
        const selectedItems = this._canvasSelectionItems(canvas);
        if (
            canvas.graph !== targetGraph
            || selectedItems.length !== nodes.length
            || nodes.some(node => !selectedItems.includes(node))
        ) {
            throw branchNavigationError(
                "branch_navigation_verification_failed",
                "The canvas did not retain the exact branch scope and selection.",
            );
        }
    }

    _graphNodes(graph) {
        if (Array.isArray(graph?._nodes)) return graph._nodes;
        if (Array.isArray(graph?.nodes)) return graph.nodes;
        return [];
    }

    _serializedGraphNodes(serializedGraph) {
        return Array.isArray(serializedGraph?.nodes) ? serializedGraph.nodes : [];
    }

    _serializeRuntimeNode(node) {
        if (!node || typeof node.serialize !== "function") return null;
        const serialized = node.serialize();
        return serialized && typeof serialized === "object" && !Array.isArray(serialized)
            ? structuredClone(serialized)
            : null;
    }

    _nodeProjectionError(code, message, nodeId, matchCount = null) {
        const error = new Error(message);
        error.code = code;
        error.details = {
            node_id: nodeId,
            ...(matchCount === null ? {} : { match_count: matchCount }),
        };
        return error;
    }

    /**
     * Resolve a serialized workflow ID to one live node. Modern ComfyUI keeps
     * runtime NodeIds as strings while node.serialize().id canonicalizes
     * numeric IDs. The serialized record is the wire authority; String()-based
     * lookup is deliberately forbidden because 1 and "1" can coexist live.
     */
    _resolveSerializedNodeProjection(graph, serializedGraph, nodeId, options = {}) {
        const missingCode = options.missingCode || "workflow_node_projection_missing";
        const ambiguousCode = options.ambiguousCode || "workflow_node_projection_ambiguous";
        const label = options.label || "Workflow";
        const runtimeNodes = this._graphNodes(graph);
        const authorityMatches = this._serializedGraphNodes(serializedGraph).filter(node => (
            typedValuesEqual(node?.id, nodeId)
        ));
        const projectedMatches = runtimeNodes
            .map(node => ({ node, serializedNode: this._serializeRuntimeNode(node) }))
            .filter(item => typedValuesEqual(item.serializedNode?.id, nodeId));

        if (projectedMatches.length > 1 || authorityMatches.length > 1) {
            throw this._nodeProjectionError(
                ambiguousCode,
                `${label} node ${String(nodeId)} has an ambiguous serialized projection.`,
                nodeId,
                Math.max(projectedMatches.length, authorityMatches.length),
            );
        }
        if (projectedMatches.length === 1) {
            if (authorityMatches.length !== 1) {
                throw this._nodeProjectionError(
                    missingCode,
                    `${label} node ${String(nodeId)} is absent from serialized authority.`,
                    nodeId,
                );
            }
            const projected = projectedMatches[0];
            const authority = authorityMatches[0];
            const liveType = projected.node.comfyClass || projected.node.type;
            const projectedType = projected.serializedNode.type ?? liveType;
            const authorityType = authority.type ?? projectedType;
            if (
                (liveType != null && projectedType != null && liveType !== projectedType)
                || (
                    projectedType != null
                    && authorityType != null
                    && projectedType !== authorityType
                )
            ) {
                throw this._nodeProjectionError(
                    ambiguousCode,
                    `${label} node ${String(nodeId)} conflicts with serialized node type authority.`,
                    nodeId,
                    1,
                );
            }
            return {
                node: projected.node,
                serializedNode: structuredClone(authority),
                serializedId: authority.id,
            };
        }

        // Compatibility for older/custom graph objects that do not expose a
        // node serializer. Exact typed identity remains safe; coercion does not.
        const exactRuntimeMatches = runtimeNodes.filter(node => (
            typedValuesEqual(node?.id, nodeId)
        ));
        if (exactRuntimeMatches.length > 1) {
            throw this._nodeProjectionError(
                ambiguousCode,
                `${label} node ${String(nodeId)} is ambiguous.`,
                nodeId,
                exactRuntimeMatches.length,
            );
        }
        if (exactRuntimeMatches.length === 1) {
            const exactNode = exactRuntimeMatches[0];
            if (this._serializeRuntimeNode(exactNode)) {
                throw this._nodeProjectionError(
                    missingCode,
                    `${label} node ${String(nodeId)} is not a serialized workflow ID.`,
                    nodeId,
                );
            }
            const authority = authorityMatches[0] || null;
            const liveType = exactNode.comfyClass || exactNode.type;
            if (authority?.type != null && liveType != null && authority.type !== liveType) {
                throw this._nodeProjectionError(
                    ambiguousCode,
                    `${label} node ${String(nodeId)} conflicts with serialized node type authority.`,
                    nodeId,
                    1,
                );
            }
            return {
                node: exactNode,
                serializedNode: authority ? structuredClone(authority) : null,
                serializedId: authority?.id ?? nodeId,
            };
        }
        if (options.allowMissing) return null;
        throw this._nodeProjectionError(
            missingCode,
            `${label} node ${String(nodeId)} is missing.`,
            nodeId,
        );
    }

    _projectRuntimeNode(graph, serializedGraph, node, options = {}) {
        const serialized = this._serializeRuntimeNode(node);
        const serializedId = serialized?.id ?? node?.id;
        const projected = this._resolveSerializedNodeProjection(
            graph,
            serializedGraph,
            serializedId,
            options,
        );
        if (projected.node !== node) {
            throw this._nodeProjectionError(
                options.ambiguousCode || "workflow_node_projection_ambiguous",
                `${options.label || "Workflow"} node ${String(serializedId)} projects to a different live node.`,
                serializedId,
                2,
            );
        }
        return projected;
    }

    _findRuntimeEndpointNode(graph, endpointId, options = {}) {
        const matches = this._graphNodes(graph).filter(node => (
            typedValuesEqual(node?.id, endpointId)
        ));
        if (matches.length === 1) return matches[0];
        if (matches.length > 1) {
            throw this._nodeProjectionError(
                options.ambiguousCode || "workflow_node_projection_ambiguous",
                `${options.label || "Workflow"} runtime endpoint ${String(endpointId)} is ambiguous.`,
                endpointId,
                matches.length,
            );
        }
        if (options.serializedGraph) {
            return this._resolveSerializedNodeProjection(
                graph,
                options.serializedGraph,
                endpointId,
                {
                    allowMissing: true,
                    missingCode: options.missingCode,
                    ambiguousCode: options.ambiguousCode,
                    label: options.label,
                },
            )?.node || null;
        }
        return null;
    }

    _workflowNodeProjection(nodeId, pin = null, allowMissing = false) {
        if (pin) this.assertActiveWorkflow(pin);
        const graph = app.graph;
        const authority = structuredClone(graph.serialize());
        const projection = this._resolveSerializedNodeProjection(
            graph,
            authority,
            nodeId,
            { allowMissing },
        );
        if (pin) this.assertActiveWorkflow(pin);
        return projection;
    }

    _workflowNodeFromSerializedId(nodeId, pin = null) {
        return this._workflowNodeProjection(nodeId, pin, false).node;
    }

    _findExactNodeInGraph(graph, nodeId, label = "scope", serializedGraph = null) {
        return this._resolveSerializedNodeProjection(graph, serializedGraph, nodeId, {
            missingCode: label === "branch" ? "branch_node_missing" : "branch_scope_not_found",
            ambiguousCode: label === "branch" ? "branch_node_ambiguous" : "branch_scope_ambiguous",
            label: label === "branch" ? "Branch" : "Scope",
        }).node;
    }

    _serializedSubgraphDefinitions(rootSnapshot) {
        const definitions = rootSnapshot?.definitions?.subgraphs;
        if (Array.isArray(definitions)) return definitions;
        if (definitions && typeof definitions === "object") {
            return Object.values(definitions);
        }
        return [];
    }

    _resolveBranchGraphScope(rootGraph, scopePath, rootSnapshot) {
        let graph = rootGraph;
        let serializedGraph = rootSnapshot;
        const resolvedContainers = [];
        for (const [index, segment] of scopePath.entries()) {
            const exactNode = this._findExactNodeInGraph(
                graph,
                segment.container_node_id,
                "scope",
                serializedGraph,
            );
            if (
                (typeof exactNode.isSubgraphNode === "function" && !exactNode.isSubgraphNode())
                || !exactNode.subgraph
            ) {
                throw branchNavigationError(
                    "branch_scope_not_found",
                    `scope_path[${index}] is not a subgraph container.`,
                    { container_node_id: segment.container_node_id },
                );
            }
            if (String(exactNode.subgraph.id ?? "") !== segment.subgraph_id) {
                throw branchNavigationError(
                    "branch_scope_not_found",
                    `scope_path[${index}] resolves to a different subgraph.`,
                    {
                        container_node_id: segment.container_node_id,
                        expected_subgraph_id: segment.subgraph_id,
                        actual_subgraph_id: exactNode.subgraph.id ?? null,
                    },
                );
            }
            const registeredSubgraph = typeof rootGraph.subgraphs?.get === "function"
                ? rootGraph.subgraphs.get(segment.subgraph_id)
                : null;
            if (registeredSubgraph && registeredSubgraph !== exactNode.subgraph) {
                throw branchNavigationError(
                    "branch_scope_ambiguous",
                    `scope_path[${index}] conflicts with the registered subgraph definition.`,
                );
            }
            const serializedDefinitions = this._serializedSubgraphDefinitions(rootSnapshot)
                .filter(definition => String(definition?.id ?? "") === segment.subgraph_id);
            if (
                serializedDefinitions.length !== 1
                && !(serializedDefinitions.length === 0 && !this._serializeRuntimeNode(exactNode))
            ) {
                throw branchNavigationError(
                    serializedDefinitions.length === 0
                        ? "branch_scope_not_found"
                        : "branch_scope_ambiguous",
                    `scope_path[${index}] does not resolve to one serialized subgraph definition.`,
                    {
                        subgraph_id: segment.subgraph_id,
                        match_count: serializedDefinitions.length,
                    },
                );
            }
            resolvedContainers.push(exactNode);
            graph = exactNode.subgraph;
            serializedGraph = serializedDefinitions[0] || null;
        }

        if (scopePath.length > 0 && typeof rootGraph.resolveSubgraphIdPath === "function") {
            try {
                const nativeNodes = rootGraph.resolveSubgraphIdPath(
                    resolvedContainers.map(node => node.id),
                );
                if (
                    !Array.isArray(nativeNodes)
                    || nativeNodes.length !== resolvedContainers.length
                    || nativeNodes.some((node, index) => node !== resolvedContainers[index])
                ) {
                    throw branchNavigationError(
                        "branch_scope_ambiguous",
                        "The native subgraph resolver disagrees with serialized scope authority.",
                    );
                }
            } catch (error) {
                if (error?.code === "branch_scope_ambiguous") throw error;
                // Older/custom builds may not expose a usable native resolver;
                // the exact serialized projection above remains authoritative.
            }
        }
        return { graph, serializedGraph };
    }

    _canvasSelectionItems(canvas) {
        if (canvas?.selectedItems && typeof canvas.selectedItems.values === "function") {
            return Array.from(canvas.selectedItems.values());
        }
        return Object.values(canvas?.selected_nodes || {});
    }

    _captureCanvasNavigationState(canvas) {
        const offset = canvas?.ds?.offset;
        return {
            graph: canvas?.graph || null,
            selectedItems: this._canvasSelectionItems(canvas),
            viewport: {
                scale: Number.isFinite(canvas?.ds?.scale) ? canvas.ds.scale : null,
                offset: Array.isArray(offset) && offset.length >= 2
                    ? [offset[0], offset[1]]
                    : null,
            },
        };
    }

    _restoreCanvasNavigationState(canvas, state) {
        const issues = [];
        try {
            if (state.graph && canvas.graph !== state.graph) canvas.setGraph(state.graph);
        } catch (error) {
            issues.push({
                field: "graph",
                reason: "restore_threw",
                message: String(error?.message || error),
            });
        }
        try {
            if (typeof canvas.selectItems === "function") {
                canvas.selectItems(state.selectedItems, false);
            } else if (typeof canvas.selectNodes === "function") {
                canvas.selectNodes(state.selectedItems, false);
            }
        } catch (error) {
            issues.push({
                field: "selection",
                reason: "restore_threw",
                message: String(error?.message || error),
            });
        }
        try {
            if (state.viewport.scale !== null && canvas?.ds) {
                canvas.ds.scale = state.viewport.scale;
            }
            if (state.viewport.offset && Array.isArray(canvas?.ds?.offset)) {
                canvas.ds.offset[0] = state.viewport.offset[0];
                canvas.ds.offset[1] = state.viewport.offset[1];
            }
            canvas?.setDirty?.(true, true);
        } catch (error) {
            issues.push({
                field: "viewport",
                reason: "restore_threw",
                message: String(error?.message || error),
            });
        }

        if (canvas.graph !== state.graph) {
            issues.push({
                field: "graph",
                reason: "identity_mismatch",
                expected_graph_id: state.graph?.id ?? null,
                actual_graph_id: canvas.graph?.id ?? null,
            });
        }
        const selectedItems = this._canvasSelectionItems(canvas);
        if (
            selectedItems.length !== state.selectedItems.length
            || state.selectedItems.some(item => !selectedItems.includes(item))
        ) {
            issues.push({
                field: "selection",
                reason: "identity_mismatch",
                expected_node_ids: state.selectedItems.map(item => item?.id ?? null),
                actual_node_ids: selectedItems.map(item => item?.id ?? null),
            });
        }
        if (
            state.viewport.scale !== null
            && canvas?.ds?.scale !== state.viewport.scale
        ) {
            issues.push({
                field: "viewport.scale",
                reason: "value_mismatch",
                expected: state.viewport.scale,
                actual: canvas?.ds?.scale ?? null,
            });
        }
        if (
            state.viewport.offset
            && (
                !Array.isArray(canvas?.ds?.offset)
                || canvas.ds.offset[0] !== state.viewport.offset[0]
                || canvas.ds.offset[1] !== state.viewport.offset[1]
            )
        ) {
            issues.push({
                field: "viewport.offset",
                reason: "value_mismatch",
                expected: structuredClone(state.viewport.offset),
                actual: Array.isArray(canvas?.ds?.offset)
                    ? [canvas.ds.offset[0], canvas.ds.offset[1]]
                    : null,
            });
        }
        return { valid: issues.length === 0, issues };
    }

    async _fitExactCanvasSelection(nodes) {
        const canvas = app.canvas;
        if (typeof canvas?.fitViewToSelectionAnimated === "function") {
            await canvas.fitViewToSelectionAnimated();
            return "native_selection";
        }
        const commandBridge = this._getCommandBridge();
        if (typeof commandBridge?.execute === "function") {
            await commandBridge.execute("Comfy.Canvas.FitView");
            return "native_command";
        }
        this._fitNodesFallback(nodes);
        return "fallback";
    }

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

    async _loadComfyImageExact(ref, expectedAttestation) {
        const expected = expectedAttestation || {};
        if (
            !SHA256_PATTERN.test(String(expected.sha256 || ""))
            || !Number.isSafeInteger(expected.size_bytes)
            || expected.size_bytes <= 0
            || !Number.isSafeInteger(expected.width)
            || expected.width <= 0
            || !Number.isSafeInteger(expected.height)
            || expected.height <= 0
        ) {
            throw maskSourcePreconditionError(
                "The expected mask source byte attestation is invalid.",
            );
        }
        const params = new URLSearchParams({
            filename: ref.filename,
            subfolder: ref.subfolder || "",
            type: ref.type || "input",
        });
        params.set("rand", globalThis.crypto?.randomUUID?.() || String(Date.now()));
        // Exactly one authenticated source GET. Both the digest and decoded
        // bitmap below are derived from this same immutable Blob instance.
        const response = await api.fetchApi(`/view?${params.toString()}`, {
            cache: "no-store",
        });
        if (!response.ok) {
            throw maskSourcePreconditionError(
                `Failed to load the exact mask source (${response.status}).`,
            );
        }
        const blob = await response.blob();
        if (blob.size !== expected.size_bytes) {
            throw maskSourcePreconditionError(
                "The mask source byte size changed after inspection.",
            );
        }
        const sha256 = await sha256BlobHex(blob);
        if (sha256 !== expected.sha256) {
            throw maskSourcePreconditionError(
                "The mask source bytes changed after inspection.",
            );
        }
        const image = await createImageBitmap(blob);
        if (image.width !== expected.width || image.height !== expected.height) {
            image.close?.();
            throw maskSourcePreconditionError(
                "The decoded mask source dimensions changed after inspection.",
            );
        }
        return {
            blob,
            image,
            attestation: {
                sha256,
                size_bytes: blob.size,
                width: image.width,
                height: image.height,
            },
        };
    }

    async _loadComfyImageChannel(ref, channel = "rgba") {
        if (!["rgb", "rgba"].includes(channel)) {
            throw new Error(`Unsupported ComfyUI image channel: ${channel}`);
        }
        const params = new URLSearchParams({
            filename: ref.filename,
            subfolder: ref.subfolder || "",
            type: ref.type || "input",
            channel,
        });
        params.set("rand", globalThis.crypto?.randomUUID?.() || String(Date.now()));
        const response = await api.fetchApi(`/view?${params.toString()}`, {
            cache: "no-store",
        });
        if (!response.ok) {
            throw new Error(`Failed to load the composed mask preview (${response.status}).`);
        }
        return await createImageBitmap(await response.blob());
    }

    /**
     * Re-attest one reference image from a single immutable browser snapshot.
     * The decoded pixels are closed immediately and never enter graph or log state.
     */
    async verifyComfyImageExact(ref, expectedAttestation) {
        let snapshot = null;
        try {
            if (
                !ref
                || typeof ref.filename !== "string"
                || !ref.filename
                || (ref.subfolder !== undefined && typeof ref.subfolder !== "string")
                || (ref.type !== undefined && typeof ref.type !== "string")
            ) {
                throw new Error("The expected reference image is invalid.");
            }
            snapshot = await this._loadComfyImageExact(ref, expectedAttestation);
            return structuredClone(snapshot.attestation);
        } catch (cause) {
            const error = new Error(
                "The reference image bytes no longer match the inspected snapshot.",
            );
            error.code = "reference_image_precondition_failed";
            error.cause = cause;
            throw error;
        } finally {
            snapshot?.image?.close?.();
        }
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

    hydrateNestedImagePreviews(nodes = app.graph?._nodes || []) {
        let hydrated = 0;
        for (const node of nodes) {
            const image = nestedImageRefForNode(node);
            if (!image) continue;
            // Workflow loading already restored the canonical widget and
            // serialized node state. Only hydrate LiteGraph's presentation
            // cache here so rollback hashes cannot be changed by preview work.
            this._loadNodeImagePreview(node, image);
            hydrated++;
        }
        return hydrated;
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
        preview.onload = () => {
            // Never attach an incomplete or failed image to LiteGraph. Some
            // ComfyUI renderers retain stale canvas frames when drawing one.
            node.imgs = [preview];
            node.imageIndex = 0;
            this._markCanvasDirty();
        };
        preview.onerror = () => {
            console.warn(`[FL_API] Image preview failed for node ${node.id}`);
        };
        // Canvas nodes need enough source detail for mask review. Chat cards
        // keep using the separate bounded /fl_mcp/image/thumbnail endpoint.
        // The canonical widget reference remains the execution authority.
        preview.src = api.apiURL(`/view?${params.toString()}`);
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
            id: this._workflowId(workflow),
            name: workflow.name || workflow.path || workflow.filename || "Untitled",
            path: workflow.path || null,
            filename: workflow.filename || null,
            is_modified: Boolean(workflow.isModified || workflow.modified || workflow.dirty)
        };
    }

    _workflowId(workflow) {
        const activeState = this._unwrap(workflow?.activeState) || {};
        if (activeState.id) return String(activeState.id);
        if (typeof workflow?.content === "string") {
            try {
                const contentId = JSON.parse(workflow.content)?.id;
                if (contentId) return String(contentId);
            } catch (_) {
                // Fall back to the tab identity while malformed content is loading.
            }
        }
        const fallback = workflow?.id || workflow?.key || workflow?.path;
        return fallback ? String(fallback) : null;
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
