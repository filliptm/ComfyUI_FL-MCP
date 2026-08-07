/** Atomic, rollback-safe insertion, replacement, and deletion of one linear graph chain. */

import {
    canonicalWorkflowJSON,
    REFINEMENT_LEDGER_KEY,
    workflowGraphHash,
} from "./graph_precondition.js";

export const WORKFLOW_REFINEMENT_PROPERTY = "fl_mcp_workflow_refinement";
export const WORKFLOW_REFINEMENT_SCHEMA = "fl-mcp.workflow-refinement.v1";

const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const REFINEMENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const REFINEMENT_OPERATIONS = new Set(["insert", "replace", "delete"]);
const REFINEMENT_LEDGER_LIMIT = 64;


function isRecord(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
}


function clone(value) {
    return structuredClone(value);
}


function canonicalValue(value) {
    if (Array.isArray(value)) return value.map(canonicalValue);
    if (value && typeof value === "object") {
        return Object.fromEntries(
            Object.keys(value).sort().map(key => [key, canonicalValue(value[key])]),
        );
    }
    return value;
}


function valuesEqual(left, right) {
    return JSON.stringify(canonicalValue(left)) === JSON.stringify(canonicalValue(right));
}


function idKey(value) {
    return `${typeof value}:${JSON.stringify(value)}`;
}


function idsEqual(left, right) {
    return idKey(left) === idKey(right);
}


function nodeId(value) {
    return value?.node_id ?? value?.id;
}


function nodeType(value) {
    return value?.node_type ?? value?.type;
}


function isSlotIndex(value) {
    return Number.isInteger(value) && value >= 0;
}


function normalizeConnection(connection) {
    if (!isRecord(connection)) return null;
    const normalized = {
        source_node_id: connection.source_node_id,
        source_output_index: connection.source_output_index,
        source_output: connection.source_output ?? null,
        target_node_id: connection.target_node_id,
        target_input_index: connection.target_input_index,
        target_input: connection.target_input ?? null,
        type: connection.type ?? null,
    };
    if (
        normalized.source_node_id === undefined
        || normalized.source_node_id === null
        || normalized.target_node_id === undefined
        || normalized.target_node_id === null
        || !isSlotIndex(normalized.source_output_index)
        || !isSlotIndex(normalized.target_input_index)
    ) {
        return null;
    }
    return normalized;
}


function connectionKey(connection) {
    return [
        idKey(connection.source_node_id),
        connection.source_output_index,
        idKey(connection.target_node_id),
        connection.target_input_index,
    ].join("|");
}


function connectionDetailsMatch(expected, observed) {
    if (connectionKey(expected) !== connectionKey(observed)) return false;
    for (const field of ["source_output", "target_input", "type"]) {
        if (expected[field] !== null && expected[field] !== observed[field]) return false;
    }
    return true;
}


function exactConnectionKey(connection) {
    return [
        connectionKey(connection),
        connection.source_output ?? "",
        connection.target_input ?? "",
        connection.type ?? "",
    ].join("|");
}


function countKeys(values, keyFunction) {
    const counts = new Map();
    for (const value of values) {
        const key = keyFunction(value);
        counts.set(key, (counts.get(key) || 0) + 1);
    }
    return counts;
}


function countsEqual(left, right) {
    if (left.size !== right.size) return false;
    return [...left].every(([key, count]) => right.get(key) === count);
}


function refinementError(code, message, details = null) {
    const error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
}


function emptyRollback() {
    return {
        attempted: false,
        complete: true,
        snapshot_restored: false,
        hash_verified: false,
        expected_graph_hash: null,
        restored_graph_hash: null,
        errors: [],
    };
}


function failureResult(request, error, extras = {}) {
    return {
        success: false,
        applied: false,
        already_applied: false,
        refinement_schema: WORKFLOW_REFINEMENT_SCHEMA,
        application_id: request?.application_id ?? null,
        refinement_hash: request?.refinement_hash ?? null,
        operation: request?.operation ?? null,
        expected_workflow_identity: (
            request?.expected_workflow_identity
            ?? request?.plan?.expected_workflow_identity
            ?? null
        ),
        error: {
            code: error?.code || "workflow_refinement_failed",
            message: String(error?.message || error),
            ...(error?.details ? { details: error.details } : {}),
        },
        verification: extras.verification || { valid: false, issues: [] },
        rollback: extras.rollback || emptyRollback(),
        queued: false,
    };
}


function validateAdapter(adapter) {
    const required = [
        "captureWorkflow",
        "restoreWorkflow",
        "getNode",
        "listConnections",
        "createNode",
        "setNodeMetadata",
        "disconnectConnection",
        "connectNodes",
        "removeNodes",
        "setWorkflowExtra",
    ];
    const missing = required.filter(name => typeof adapter?.[name] !== "function");
    if (missing.length > 0) {
        throw refinementError(
            "invalid_refinement_adapter",
            `The workflow refinement adapter is missing: ${missing.join(", ")}.`,
        );
    }
}


async function withAdapterReadGuard(adapter, operation) {
    if (typeof adapter?.withReadGuard === "function") {
        return await adapter.withReadGuard(operation);
    }
    return await operation();
}


function normalizePathNode(value) {
    if (!isRecord(value)) return null;
    const id = nodeId(value);
    const type = nodeType(value);
    if (id === undefined || id === null || typeof type !== "string" || !type) return null;
    return { node_id: id, node_type: type };
}


function normalizeReplacementConnection(value) {
    if (!isRecord(value)) return null;
    if (
        typeof value.source_alias !== "string"
        || !value.source_alias
        || typeof value.target_alias !== "string"
        || !value.target_alias
        || !isSlotIndex(value.source_output_index)
        || !isSlotIndex(value.target_input_index)
    ) {
        return null;
    }
    return {
        source_alias: value.source_alias,
        source_output_index: value.source_output_index,
        source_output: value.source_output ?? null,
        target_alias: value.target_alias,
        target_input_index: value.target_input_index,
        target_input: value.target_input ?? null,
        type: value.type ?? null,
    };
}


function normalizeReplacement(request) {
    const replacement = request.replacement;
    if (request.operation === "delete") {
        if (replacement !== undefined && replacement !== null) {
            throw refinementError(
                "invalid_refinement_payload",
                "Delete refinements must not define replacement nodes.",
            );
        }
        return null;
    }
    if (!isRecord(replacement) || !Array.isArray(replacement.nodes)) {
        throw refinementError(
            "invalid_refinement_payload",
            "Insert and replace refinements require an ordered replacement chain.",
        );
    }
    if (replacement.nodes.length === 0) {
        throw refinementError(
            "invalid_replacement_chain",
            "A replacement chain must contain at least one node.",
        );
    }

    const nodes = replacement.nodes.map((value, index) => {
        if (
            !isRecord(value)
            || typeof value.alias !== "string"
            || !value.alias
            || typeof value.node_type !== "string"
            || !value.node_type
            || !SHA256_PATTERN.test(String(value.schema_hash || ""))
            || (value.values !== undefined && !isRecord(value.values))
        ) {
            throw refinementError(
                "invalid_replacement_chain",
                `Replacement node ${index} is not a canonical planned node.`,
            );
        }
        return {
            alias: value.alias,
            node_type: value.node_type,
            schema_hash: value.schema_hash,
            values: clone(value.values || {}),
        };
    });
    if (new Set(nodes.map(node => node.alias)).size !== nodes.length) {
        throw refinementError(
            "invalid_replacement_chain",
            "Replacement aliases must be unique.",
        );
    }

    const connections = (replacement.connections || []).map((value, index) => {
        const connection = normalizeReplacementConnection(value);
        if (!connection) {
            throw refinementError(
                "invalid_replacement_chain",
                `Replacement connection ${index} is not exact.`,
            );
        }
        return connection;
    });
    if (connections.length !== nodes.length - 1) {
        throw refinementError(
            "invalid_replacement_chain",
            "Replacement nodes must form one strict linear chain.",
        );
    }
    for (let index = 0; index < connections.length; index += 1) {
        if (
            connections[index].source_alias !== nodes[index].alias
            || connections[index].target_alias !== nodes[index + 1].alias
        ) {
            throw refinementError(
                "invalid_replacement_chain",
                "Replacement connections must follow node order without branches.",
            );
        }
    }

    const input = replacement.input;
    const output = replacement.output;
    if (
        !isRecord(input)
        || input.target_alias !== nodes[0].alias
        || !isSlotIndex(input.target_input_index)
        || !isRecord(output)
        || output.source_alias !== nodes[nodes.length - 1].alias
        || !isSlotIndex(output.source_output_index)
    ) {
        throw refinementError(
            "invalid_replacement_chain",
            "Replacement input and output must terminate on the first and last node.",
        );
    }
    return {
        nodes,
        connections,
        input: {
            target_alias: input.target_alias,
            target_input_index: input.target_input_index,
            target_input: input.target_input ?? null,
            type: input.type ?? null,
        },
        output: {
            source_alias: output.source_alias,
            source_output_index: output.source_output_index,
            source_output: output.source_output ?? null,
            type: output.type ?? null,
        },
    };
}


function normalizeRequest(request) {
    const plan = request?.plan;
    if (
        !isRecord(request)
        || !REFINEMENT_ID_PATTERN.test(String(request.application_id || ""))
        || !SHA256_PATTERN.test(String(request.refinement_hash || ""))
        || !isRecord(plan)
        || !REFINEMENT_OPERATIONS.has(plan.operation)
        || typeof plan.expected_workflow_identity !== "string"
        || !plan.expected_workflow_identity
        || !SHA256_PATTERN.test(String(plan.expected_graph_hash || ""))
        || !isRecord(plan.expected_path)
        || !Array.isArray(plan.expected_path.nodes)
        || !Array.isArray(plan.expected_path.connections)
    ) {
        throw refinementError(
            "invalid_refinement_payload",
            "A canonical workflow refinement request is required.",
        );
    }

    const pathNodes = plan.expected_path.nodes.map((value, index) => {
        const node = normalizePathNode(value);
        if (!node) {
            throw refinementError(
                "invalid_expected_path",
                `Expected path node ${index} is invalid.`,
            );
        }
        return node;
    });
    if (new Set(pathNodes.map(node => idKey(node.node_id))).size !== pathNodes.length) {
        throw refinementError("invalid_expected_path", "Expected path node IDs must be unique.");
    }

    const pathConnections = plan.expected_path.connections.map((value, index) => {
        const connection = normalizeConnection(value);
        if (!connection) {
            throw refinementError(
                "invalid_expected_path",
                `Expected path connection ${index} is invalid.`,
            );
        }
        return connection;
    });
    if (pathConnections.length !== pathNodes.length + 1) {
        throw refinementError(
            "invalid_expected_path",
            "A linear path requires one more connection than internal nodes.",
        );
    }
    if (plan.operation === "insert" && pathNodes.length !== 0) {
        throw refinementError(
            "invalid_expected_path",
            "Insert refinements require one direct boundary edge and no internal nodes.",
        );
    }
    if (plan.operation !== "insert" && pathNodes.length === 0) {
        throw refinementError(
            "invalid_expected_path",
            "Replace and delete refinements require at least one internal node.",
        );
    }

    const sequence = [
        pathConnections[0].source_node_id,
        ...pathNodes.map(node => node.node_id),
        pathConnections[pathConnections.length - 1].target_node_id,
    ];
    if (
        pathNodes.some(node => (
            idsEqual(node.node_id, sequence[0])
            || idsEqual(node.node_id, sequence[sequence.length - 1])
        ))
        || idsEqual(sequence[0], sequence[sequence.length - 1])
    ) {
        throw refinementError(
            "invalid_expected_path",
            "Path boundary nodes must be distinct from its internal nodes and each other.",
        );
    }
    for (let index = 0; index < pathConnections.length; index += 1) {
        const connection = pathConnections[index];
        if (
            !idsEqual(connection.source_node_id, sequence[index])
            || !idsEqual(connection.target_node_id, sequence[index + 1])
        ) {
            throw refinementError(
                "invalid_expected_path",
                "Expected path connections must follow the declared node order exactly.",
            );
        }
    }

    return {
        application_id: request.application_id,
        refinement_hash: request.refinement_hash,
        operation: plan.operation,
        expected_workflow_identity: plan.expected_workflow_identity,
        expected_graph_hash: plan.expected_graph_hash,
        expected_path: { nodes: pathNodes, connections: pathConnections },
        replacement: normalizeReplacement({
            operation: plan.operation,
            replacement: plan.replacement,
        }),
    };
}


function emptyLedger() {
    return {
        schema: WORKFLOW_REFINEMENT_SCHEMA,
        order: [],
        entries: {},
    };
}


function readLedger(snapshot) {
    const stored = snapshot?.extra?.[REFINEMENT_LEDGER_KEY];
    if (stored === undefined || stored === null) return emptyLedger();
    if (
        !isRecord(stored)
        || stored.schema !== WORKFLOW_REFINEMENT_SCHEMA
        || !Array.isArray(stored.order)
        || !isRecord(stored.entries)
        || stored.order.some(id => typeof id !== "string" || !stored.entries[id])
    ) {
        throw refinementError(
            "invalid_refinement_ledger",
            "The workflow refinement idempotency ledger is malformed.",
        );
    }
    return clone(stored);
}


function ledgerWithEntry(ledger, refinementId, entry) {
    const next = clone(ledger);
    next.order = next.order.filter(id => id !== refinementId);
    next.order.push(refinementId);
    next.entries[refinementId] = clone(entry);
    while (next.order.length > REFINEMENT_LEDGER_LIMIT) {
        const removed = next.order.shift();
        delete next.entries[removed];
    }
    return next;
}


async function observeExpectedPath(request, adapter, allConnections) {
    const issues = [];
    const observedNodes = [];
    for (const expected of request.expected_path.nodes) {
        const observed = await adapter.getNode(expected.node_id);
        if (!observed) {
            issues.push({
                code: "expected_path_node_missing",
                node_id: expected.node_id,
                message: `Expected path node ${expected.node_id} is missing.`,
            });
        } else if (nodeType(observed) !== expected.node_type) {
            issues.push({
                code: "expected_path_node_type_mismatch",
                node_id: expected.node_id,
                message: `Expected ${expected.node_type} at node ${expected.node_id}.`,
            });
        }
        observedNodes.push(observed);
    }

    const observedPathConnections = [];
    for (const expected of request.expected_path.connections) {
        const matches = allConnections.filter(observed => (
            connectionDetailsMatch(expected, observed)
        ));
        if (matches.length !== 1) {
            issues.push({
                code: "expected_path_connection_mismatch",
                connection: expected,
                message: `Expected exactly one matching path connection; found ${matches.length}.`,
            });
        } else {
            observedPathConnections.push(matches[0]);
        }
    }

    const internalIds = new Set(
        request.expected_path.nodes.map(node => idKey(node.node_id)),
    );
    const expectedTouching = request.expected_path.connections.filter(connection => (
        internalIds.has(idKey(connection.source_node_id))
        || internalIds.has(idKey(connection.target_node_id))
    ));
    const expectedKeys = countKeys(expectedTouching, connectionKey);
    const touchingInternal = allConnections.filter(connection => (
        internalIds.has(idKey(connection.source_node_id))
        || internalIds.has(idKey(connection.target_node_id))
    ));
    if (!countsEqual(countKeys(touchingInternal, connectionKey), expectedKeys)) {
        issues.push({
            code: "expected_path_not_strictly_linear",
            message: "An internal path node has an undeclared input, output, or branch.",
        });
    }

    return {
        valid: issues.length === 0,
        issues,
        nodes: observedNodes,
        connections: observedPathConnections,
    };
}


function plannedConnection(sourceId, targetId, specification) {
    return {
        source_node_id: sourceId,
        source_output_index: specification.source_output_index,
        source_output: specification.source_output ?? null,
        target_node_id: targetId,
        target_input_index: specification.target_input_index,
        target_input: specification.target_input ?? null,
        type: specification.type ?? null,
    };
}


function desiredConnections(request, aliases, pathConnections) {
    const firstPath = pathConnections[0];
    const lastPath = pathConnections[pathConnections.length - 1];
    if (request.operation === "delete") {
        return [{
            source_node_id: firstPath.source_node_id,
            source_output_index: firstPath.source_output_index,
            source_output: firstPath.source_output,
            target_node_id: lastPath.target_node_id,
            target_input_index: lastPath.target_input_index,
            target_input: lastPath.target_input,
            type: lastPath.type ?? firstPath.type,
        }];
    }

    const replacement = request.replacement;
    const connections = [plannedConnection(
        firstPath.source_node_id,
        aliases[replacement.input.target_alias],
        {
            source_output_index: firstPath.source_output_index,
            source_output: firstPath.source_output,
            target_input_index: replacement.input.target_input_index,
            target_input: replacement.input.target_input,
            type: replacement.input.type ?? firstPath.type,
        },
    )];
    for (const internal of replacement.connections) {
        connections.push(plannedConnection(
            aliases[internal.source_alias],
            aliases[internal.target_alias],
            internal,
        ));
    }
    connections.push(plannedConnection(
        aliases[replacement.output.source_alias],
        lastPath.target_node_id,
        {
            source_output_index: replacement.output.source_output_index,
            source_output: replacement.output.source_output,
            target_input_index: lastPath.target_input_index,
            target_input: lastPath.target_input,
            type: replacement.output.type ?? lastPath.type,
        },
    ));
    return connections;
}


const NODE_TOPOLOGY_FIELDS = new Set([
    "id",
    "node_id",
    "type",
    "node_type",
    "pos",
    "position",
    "size",
    "order",
    "serialized_node",
]);


function withoutLinkIds(slots, linkField) {
    if (!Array.isArray(slots)) return clone(slots);
    return slots.map(slot => {
        if (!isRecord(slot)) return clone(slot);
        return Object.fromEntries(
            Object.entries(slot)
                .filter(([key]) => key !== linkField)
                .map(([key, value]) => [key, clone(value)]),
        );
    });
}


function nonTopologyNodeFacts(node) {
    const source = isRecord(node?.serialized_node) ? node.serialized_node : node;
    if (!isRecord(source)) return {};
    return Object.fromEntries(
        Object.entries(source)
            .filter(([key]) => !NODE_TOPOLOGY_FIELDS.has(key))
            .map(([key, value]) => {
                if (key === "inputs") return [key, withoutLinkIds(value, "link")];
                if (key === "outputs") return [key, withoutLinkIds(value, "links")];
                return [key, clone(value)];
            }),
    );
}


function snapshotNodeFacts(snapshot) {
    const facts = new Map();
    for (const node of Array.isArray(snapshot?.nodes) ? snapshot.nodes : []) {
        const id = nodeId(node);
        if (id === undefined || id === null) continue;
        const position = Array.isArray(node.pos)
            ? { x: node.pos[0], y: node.pos[1] }
            : node.position;
        const size = Array.isArray(node.size)
            ? { width: node.size[0], height: node.size[1] }
            : node.size;
        facts.set(idKey(id), {
            node_id: id,
            node_type: nodeType(node),
            non_topology: nonTopologyNodeFacts(node),
            position: position ? clone(position) : null,
            size: size ? clone(size) : null,
        });
    }
    return facts;
}


async function verifyRefinement(
    request,
    adapter,
    beforeSnapshot,
    siblingConnections,
    desired,
    aliases,
) {
    const issues = [];
    const observedRaw = await adapter.listConnections();
    const observed = Array.isArray(observedRaw)
        ? observedRaw.map(normalizeConnection).filter(Boolean)
        : [];
    const expectedTopology = [...siblingConnections, ...desired];
    if (!countsEqual(
        countKeys(observed, connectionKey),
        countKeys(expectedTopology, connectionKey),
    )) {
        issues.push({
            code: "refinement_topology_mismatch",
            message: "The resulting graph does not contain exactly the preserved siblings and new chain.",
        });
    }
    const observedExactCounts = countKeys(observed, exactConnectionKey);
    for (const sibling of siblingConnections) {
        const key = exactConnectionKey(sibling);
        if (!observedExactCounts.get(key)) {
            issues.push({
                code: "sibling_connection_changed",
                connection: sibling,
                message: "A connection outside the refined path changed.",
            });
        }
    }
    for (const connection of desired) {
        const matches = observed.filter(candidate => connectionDetailsMatch(connection, candidate));
        if (matches.length !== 1) {
            issues.push({
                code: "refinement_connection_mismatch",
                connection,
                message: `Expected exactly one new chain connection; found ${matches.length}.`,
            });
        }
    }

    const removedIds = request.expected_path.nodes.map(node => node.node_id);
    for (const id of removedIds) {
        if (await adapter.getNode(id)) {
            issues.push({
                code: "obsolete_node_not_removed",
                node_id: id,
                message: `Obsolete node ${id} remains on the canvas.`,
            });
        }
    }

    const originalFacts = snapshotNodeFacts(beforeSnapshot);
    const removedKeys = new Set(removedIds.map(idKey));
    for (const fact of originalFacts.values()) {
        if (removedKeys.has(idKey(fact.node_id))) continue;
        const observedNode = await adapter.getNode(fact.node_id);
        if (!observedNode || (
            fact.node_type !== undefined
            && fact.node_type !== null
            && nodeType(observedNode) !== fact.node_type
        )) {
            issues.push({
                code: "sibling_node_changed",
                node_id: fact.node_id,
                message: `Unrelated node ${fact.node_id} was removed or changed type.`,
            });
            continue;
        }
        const siblingFactsChanged = (
            !valuesEqual(nonTopologyNodeFacts(observedNode), fact.non_topology)
            || (fact.position !== null && !valuesEqual(observedNode.position, fact.position))
            || (fact.size !== null && !valuesEqual(observedNode.size, fact.size))
        );
        if (siblingFactsChanged) {
            issues.push({
                code: "sibling_node_changed",
                node_id: fact.node_id,
                message: `Unrelated node ${fact.node_id} values, metadata, or layout changed.`,
            });
        }
    }

    for (const planned of request.replacement?.nodes || []) {
        const id = aliases[planned.alias];
        const observedNode = await adapter.getNode(id);
        const metadata = observedNode?.properties?.[WORKFLOW_REFINEMENT_PROPERTY];
        if (!observedNode || nodeType(observedNode) !== planned.node_type) {
            issues.push({
                code: "replacement_node_mismatch",
                alias: planned.alias,
                message: `Replacement node ${planned.alias} is missing or has the wrong type.`,
            });
            continue;
        }
        const observedValues = observedNode.values || {};
        for (const [name, value] of Object.entries(planned.values || {})) {
            if (
                !Object.prototype.hasOwnProperty.call(observedValues, name)
                || !valuesEqual(observedValues[name], value)
            ) {
                issues.push({
                    code: "replacement_values_mismatch",
                    alias: planned.alias,
                    input_name: name,
                    message: `Replacement node ${planned.alias}.${name} changed during application.`,
                });
            }
        }
        if (
            metadata?.schema !== WORKFLOW_REFINEMENT_SCHEMA
            || metadata?.application_id !== request.application_id
            || metadata?.refinement_hash !== request.refinement_hash
            || metadata?.alias !== planned.alias
            || metadata?.schema_hash !== planned.schema_hash
        ) {
            issues.push({
                code: "replacement_metadata_mismatch",
                alias: planned.alias,
                message: `Replacement node ${planned.alias} lacks exact refinement metadata.`,
            });
        }
    }

    return {
        valid: issues.length === 0,
        issues,
        connection_count: observed.length,
        preserved_sibling_connection_count: siblingConnections.length,
    };
}


async function restoreSnapshot(adapter, snapshot, expectedGraphHash) {
    const rollback = {
        attempted: true,
        complete: false,
        snapshot_restored: false,
        hash_verified: false,
        expected_graph_hash: expectedGraphHash,
        restored_graph_hash: null,
        errors: [],
    };
    try {
        await adapter.restoreWorkflow(clone(snapshot));
    } catch (error) {
        rollback.errors.push(String(error?.message || error));
    }
    try {
        const restored = await adapter.captureWorkflow();
        rollback.restored_graph_hash = await workflowGraphHash(restored);
        rollback.hash_verified = rollback.restored_graph_hash === expectedGraphHash;
        rollback.snapshot_restored = (
            canonicalWorkflowJSON(restored) === canonicalWorkflowJSON(snapshot)
            && valuesEqual(
                restored?.extra?.[REFINEMENT_LEDGER_KEY],
                snapshot?.extra?.[REFINEMENT_LEDGER_KEY],
            )
        );
    } catch (error) {
        rollback.errors.push(String(error?.message || error));
    }
    rollback.complete = rollback.snapshot_restored && rollback.hash_verified;
    return rollback;
}


/**
 * Apply one hash-pinned, strictly linear chain splice through a pure canvas adapter.
 * The engine never queues execution and restores the complete pre-mutation snapshot
 * after every failure that may have touched the graph.
 */
export async function applyWorkflowRefinementAtomic(rawRequest, adapter) {
    let request;
    try {
        request = normalizeRequest(rawRequest);
        validateAdapter(adapter);
    } catch (error) {
        return failureResult(rawRequest, error);
    }

    let beforeSnapshot;
    let beforeGraphHash;
    let mutationStarted = false;
    let verification = { valid: false, issues: [] };
    const aliases = {};
    const createdNodeIds = [];
    const removedNodeIds = request.expected_path.nodes.map(node => node.node_id);
    try {
        beforeSnapshot = await adapter.captureWorkflow();
        beforeGraphHash = await workflowGraphHash(beforeSnapshot);
        const ledger = readLedger(beforeSnapshot);
        const existing = ledger.entries[request.application_id];
        if (existing) {
            if (existing.refinement_hash !== request.refinement_hash) {
                throw refinementError(
                    "refinement_idempotency_conflict",
                    "This refinement ID is already bound to a different refinement hash.",
                );
            }
            if (existing.result_graph_hash !== beforeGraphHash) {
                throw refinementError(
                    "refinement_idempotency_conflict",
                    "The graph changed after this refinement was applied.",
                );
            }
            return {
                success: true,
                applied: false,
                already_applied: true,
                refinement_schema: WORKFLOW_REFINEMENT_SCHEMA,
                application_id: request.application_id,
                refinement_hash: request.refinement_hash,
                operation: request.operation,
                expected_workflow_identity: request.expected_workflow_identity,
                graph_hash: beforeGraphHash,
                aliases: clone(existing.aliases || {}),
                created_node_ids: clone(existing.created_node_ids || []),
                removed_node_ids: clone(existing.removed_node_ids || []),
                verification: { valid: true, issues: [], idempotency_verified: true },
                rollback: emptyRollback(),
                queued: false,
            };
        }
        if (beforeGraphHash !== request.expected_graph_hash) {
            throw refinementError(
                "graph_precondition_failed",
                "The canvas graph hash no longer matches the compiled refinement.",
                {
                    expected_graph_hash: request.expected_graph_hash,
                    actual_graph_hash: beforeGraphHash,
                },
            );
        }

        const { allConnections, pathObservation } = await withAdapterReadGuard(
            adapter,
            async () => {
                const rawConnections = await adapter.listConnections();
                if (!Array.isArray(rawConnections)) {
                    throw refinementError(
                        "invalid_refinement_adapter",
                        "The adapter did not return a workflow connection list.",
                    );
                }
                const observedConnections = rawConnections.map((value, index) => {
                    const connection = normalizeConnection(value);
                    if (!connection) {
                        throw refinementError(
                            "invalid_refinement_adapter",
                            `Observed workflow connection ${index} is not normalized.`,
                        );
                    }
                    return connection;
                });
                const observedPath = await observeExpectedPath(
                    request,
                    adapter,
                    observedConnections,
                );
                return {
                    allConnections: observedConnections,
                    pathObservation: observedPath,
                };
            },
        );
        if (!pathObservation.valid) {
            verification = pathObservation;
            throw refinementError(
                "expected_path_mismatch",
                "The exact compiled path is not present as one strict linear chain.",
                { issues: pathObservation.issues },
            );
        }
        const oldPathKeys = countKeys(pathObservation.connections, connectionKey);
        const siblingConnections = allConnections.filter(connection => {
            const key = connectionKey(connection);
            const remaining = oldPathKeys.get(key) || 0;
            if (remaining === 0) return true;
            oldPathKeys.set(key, remaining - 1);
            return false;
        });

        for (const connection of pathObservation.connections) {
            mutationStarted = true;
            await adapter.disconnectConnection(connection);
            if (typeof adapter.afterMutationStep === "function") {
                await adapter.afterMutationStep({ phase: "disconnect", connection: clone(connection) });
            }
        }

        const initialNodeIds = new Set(snapshotNodeFacts(beforeSnapshot).keys());
        for (const planned of request.replacement?.nodes || []) {
            mutationStarted = true;
            const created = await adapter.createNode(planned);
            const id = nodeId(created);
            if (id === undefined || id === null || initialNodeIds.has(idKey(id))) {
                throw refinementError(
                    "replacement_node_creation_failed",
                    `Creating ${planned.alias} did not return a fresh node ID.`,
                );
            }
            if (createdNodeIds.some(createdId => idsEqual(createdId, id))) {
                throw refinementError(
                    "replacement_node_creation_failed",
                    `Creating ${planned.alias} reused a replacement node ID.`,
                );
            }
            aliases[planned.alias] = id;
            createdNodeIds.push(id);
            await adapter.setNodeMetadata(id, {
                schema: WORKFLOW_REFINEMENT_SCHEMA,
                application_id: request.application_id,
                refinement_hash: request.refinement_hash,
                alias: planned.alias,
                schema_hash: planned.schema_hash,
            });
            if (typeof adapter.afterMutationStep === "function") {
                await adapter.afterMutationStep({
                    phase: "node",
                    alias: planned.alias,
                    node_id: id,
                });
            }
        }

        const desired = desiredConnections(request, aliases, pathObservation.connections);
        for (const connection of desired) {
            mutationStarted = true;
            await adapter.connectNodes(
                connection.source_node_id,
                connection.target_node_id,
                connection,
            );
            if (typeof adapter.afterMutationStep === "function") {
                await adapter.afterMutationStep({
                    phase: "connection",
                    connection: clone(connection),
                });
            }
        }

        if (removedNodeIds.length > 0) {
            mutationStarted = true;
            await adapter.removeNodes([...removedNodeIds].reverse());
            if (typeof adapter.afterMutationStep === "function") {
                await adapter.afterMutationStep({
                    phase: "remove",
                    node_ids: clone(removedNodeIds),
                });
            }
        }

        verification = await withAdapterReadGuard(
            adapter,
            () => verifyRefinement(
                request,
                adapter,
                beforeSnapshot,
                siblingConnections,
                desired,
                aliases,
            ),
        );
        if (!verification.valid) {
            throw refinementError(
                "post_refinement_verification_failed",
                "The refined graph did not preserve the exact expected topology.",
                { issues: verification.issues },
            );
        }

        const refinedSnapshot = await adapter.captureWorkflow();
        const resultGraphHash = await workflowGraphHash(refinedSnapshot);
        const entry = {
            refinement_hash: request.refinement_hash,
            operation: request.operation,
            expected_graph_hash: request.expected_graph_hash,
            result_graph_hash: resultGraphHash,
            aliases: clone(aliases),
            created_node_ids: clone(createdNodeIds),
            removed_node_ids: clone(removedNodeIds),
        };
        const nextLedger = ledgerWithEntry(ledger, request.application_id, entry);
        mutationStarted = true;
        await adapter.setWorkflowExtra(REFINEMENT_LEDGER_KEY, nextLedger);

        const committedSnapshot = await adapter.captureWorkflow();
        const committedHash = await workflowGraphHash(committedSnapshot);
        const committedEntry = readLedger(committedSnapshot).entries[request.application_id];
        if (committedHash !== resultGraphHash || !valuesEqual(committedEntry, entry)) {
            throw refinementError(
                "refinement_commit_verification_failed",
                "The idempotency ledger or committed graph hash did not persist exactly.",
            );
        }

        return {
            success: true,
            applied: true,
            already_applied: false,
            refinement_schema: WORKFLOW_REFINEMENT_SCHEMA,
            application_id: request.application_id,
            refinement_hash: request.refinement_hash,
            operation: request.operation,
            expected_workflow_identity: request.expected_workflow_identity,
            previous_graph_hash: beforeGraphHash,
            graph_hash: resultGraphHash,
            aliases,
            created_node_ids: createdNodeIds,
            removed_node_ids: removedNodeIds,
            verification,
            rollback: emptyRollback(),
            queued: false,
        };
    } catch (error) {
        const rollback = mutationStarted && beforeSnapshot && beforeGraphHash
            ? await restoreSnapshot(adapter, beforeSnapshot, beforeGraphHash)
            : emptyRollback();
        return failureResult(request, error, { verification, rollback });
    }
}
