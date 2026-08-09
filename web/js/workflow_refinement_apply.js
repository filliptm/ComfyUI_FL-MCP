/** Atomic, rollback-safe workflow graph refinement with legacy linear-splice support. */

import {
    canonicalWorkflowJSON,
    REFINEMENT_LEDGER_KEY,
    workflowGraphHash,
} from "./graph_precondition.js";
import { nodeIdsEqual } from "./node_identity.js";

export const WORKFLOW_REFINEMENT_PROPERTY = "fl_mcp_workflow_refinement";
export const WORKFLOW_REFINEMENT_SCHEMA = "fl-mcp.workflow-refinement.v1";

const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const REFINEMENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const REFINEMENT_OPERATIONS = new Set(["append", "insert", "replace", "delete"]);
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
    // ComfyUI may expose one node as 52 in serialized workflow JSON and as
    // "52" through the live LiteGraph API. Those are the same node identity.
    return String(value);
}


function idsEqual(left, right) {
    return nodeIdsEqual(left, right);
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


function indexedSlot(node, field, index) {
    const source = isRecord(node?.serialized_node) ? node.serialized_node : node;
    const slots = source?.[field];
    if (Array.isArray(slots)) return slots[index] ?? null;
    if (!isRecord(slots)) return null;
    if (Object.prototype.hasOwnProperty.call(slots, String(index))) {
        return slots[String(index)];
    }
    const names = Object.keys(slots).sort();
    return slots[names[index]] ?? null;
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


function normalizeExistingInput(value, label) {
    if (
        !isRecord(value)
        || value.source_node_id === undefined
        || value.source_node_id === null
        || typeof value.source_node_type !== "string"
        || !value.source_node_type
        || !SHA256_PATTERN.test(String(value.source_schema_hash || ""))
        || !isSlotIndex(value.source_output_index)
        || typeof value.source_output !== "string"
        || !value.source_output
        || typeof value.target_alias !== "string"
        || !value.target_alias
        || !isSlotIndex(value.target_input_index)
        || typeof value.target_input !== "string"
        || !value.target_input
        || typeof value.type !== "string"
        || !value.type
    ) {
        throw refinementError(
            "invalid_replacement_graph",
            `${label} is not an exact existing-node input mapping.`,
        );
    }
    return {
        source_node_id: value.source_node_id,
        source_node_type: value.source_node_type,
        source_schema_hash: value.source_schema_hash,
        source_output_index: value.source_output_index,
        source_output: value.source_output,
        target_alias: value.target_alias,
        target_input_index: value.target_input_index,
        target_input: value.target_input,
        type: value.type,
    };
}


function validateReplacementTargetInputs(replacement) {
    const aliases = new Set(replacement.nodes.map(node => node.alias));
    const targets = [];
    for (const connection of replacement.connections) {
        targets.push({
            alias: connection.target_alias,
            input_index: connection.target_input_index,
            label: "replacement connection",
        });
    }
    if (replacement.input) {
        targets.push({
            alias: replacement.input.target_alias,
            input_index: replacement.input.target_input_index,
            label: "replacement boundary input",
        });
    }
    for (const [index, mapping] of [
        ...(replacement.primary_input ? [["primary", replacement.primary_input]] : []),
        ...replacement.side_inputs.map((mapping, index) => [`side input ${index}`, mapping]),
    ]) {
        targets.push({
            alias: mapping.target_alias,
            input_index: mapping.target_input_index,
            label: index,
        });
    }

    const occupied = new Set();
    for (const target of targets) {
        if (!aliases.has(target.alias)) {
            throw refinementError(
                "invalid_replacement_graph",
                `${target.label} targets unknown alias ${target.alias}.`,
            );
        }
        const key = `${target.alias}|${target.input_index}`;
        if (occupied.has(key)) {
            throw refinementError(
                "replacement_target_input_occupied",
                `Replacement input ${target.alias}[${target.input_index}] is assigned more than once.`,
            );
        }
        occupied.add(key);
    }
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
            "Non-delete refinements require an ordered created-node spine.",
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

    const rawSideInputs = Object.prototype.hasOwnProperty.call(replacement, "side_inputs")
        ? replacement.side_inputs
        : [];
    if (!Array.isArray(rawSideInputs)) {
        throw refinementError(
            "invalid_replacement_graph",
            "Replacement side inputs must be an array.",
        );
    }
    if (rawSideInputs.length > 100) {
        throw refinementError(
            "invalid_replacement_graph",
            "Replacement side inputs cannot exceed 100 mappings.",
        );
    }
    const sideInputs = rawSideInputs.map((value, index) => (
        normalizeExistingInput(value, `Replacement side input ${index}`)
    ));
    const normalized = {
        nodes,
        connections,
        input: null,
        primary_input: null,
        side_inputs: sideInputs,
        output: null,
    };

    if (request.operation === "append") {
        if (replacement.input !== null) {
            throw refinementError(
                "invalid_replacement_graph",
                "Append refinements must not define a disconnected boundary input.",
            );
        }
        normalized.primary_input = normalizeExistingInput(
            replacement.primary_input,
            "Replacement primary input",
        );
        if (normalized.primary_input.target_alias !== nodes[0].alias) {
            throw refinementError(
                "invalid_replacement_graph",
                "The append primary input must target the first replacement node.",
            );
        }
        if (replacement.output !== null && replacement.output !== undefined) {
            const output = replacement.output;
            if (
                !isRecord(output)
                || output.source_alias !== nodes[nodes.length - 1].alias
                || !isSlotIndex(output.source_output_index)
                || typeof output.source_output !== "string"
                || !output.source_output
                || typeof output.type !== "string"
                || !output.type
            ) {
                throw refinementError(
                    "invalid_replacement_graph",
                    "An append output must describe an exact output on the final node.",
                );
            }
            normalized.output = {
                source_alias: output.source_alias,
                source_output_index: output.source_output_index,
                source_output: output.source_output,
                type: output.type,
            };
        }
    } else {
        if (
            replacement.primary_input !== null
            && replacement.primary_input !== undefined
        ) {
            throw refinementError(
                "invalid_replacement_chain",
                "Legacy linear refinements cannot define an external primary input.",
            );
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
        normalized.input = {
            target_alias: input.target_alias,
            target_input_index: input.target_input_index,
            target_input: input.target_input ?? null,
            type: input.type ?? null,
        };
        normalized.output = {
            source_alias: output.source_alias,
            source_output_index: output.source_output_index,
            source_output: output.source_output ?? null,
            type: output.type ?? null,
        };
    }
    validateReplacementTargetInputs(normalized);
    return normalized;
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
    if (
        plan.operation !== "append"
        && pathConnections.length !== pathNodes.length + 1
    ) {
        throw refinementError(
            "invalid_expected_path",
            "A linear path requires one more connection than internal nodes.",
        );
    }
    if (
        plan.operation === "append"
        && (pathNodes.length !== 0 || pathConnections.length !== 0)
    ) {
        throw refinementError(
            "invalid_expected_path",
            "Append refinements require an empty expected path.",
        );
    }
    if (plan.operation === "insert" && pathNodes.length !== 0) {
        throw refinementError(
            "invalid_expected_path",
            "Insert refinements require one direct boundary edge and no internal nodes.",
        );
    }
    if (
        plan.operation !== "insert"
        && plan.operation !== "append"
        && pathNodes.length === 0
    ) {
        throw refinementError(
            "invalid_expected_path",
            "Replace and delete refinements require at least one internal node.",
        );
    }

    if (plan.operation !== "append") {
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
        || stored.order.some(id => (
            typeof id !== "string"
            || !Object.prototype.hasOwnProperty.call(stored.entries, id)
            || !stored.entries[id]
        ))
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


async function observeExistingInputs(request, adapter) {
    const mappings = [
        ...(request.replacement?.primary_input ? [request.replacement.primary_input] : []),
        ...(request.replacement?.side_inputs || []),
    ];
    const issues = [];
    const observedById = new Map();
    const removedIds = new Set(
        request.expected_path.nodes.map(node => idKey(node.node_id)),
    );
    for (const mapping of mappings) {
        const key = idKey(mapping.source_node_id);
        if (removedIds.has(key)) {
            issues.push({
                code: "external_source_would_be_removed",
                node_id: mapping.source_node_id,
                message: `External source node ${mapping.source_node_id} is inside the path being removed.`,
            });
            continue;
        }
        let observed = observedById.get(key);
        if (!observed) {
            observed = await adapter.getNode(mapping.source_node_id);
            observedById.set(key, observed);
        }
        if (!observed) {
            issues.push({
                code: "external_source_missing",
                node_id: mapping.source_node_id,
                message: `External source node ${mapping.source_node_id} is missing.`,
            });
        } else if (nodeType(observed) !== mapping.source_node_type) {
            issues.push({
                code: "external_source_type_mismatch",
                node_id: mapping.source_node_id,
                message: `Expected ${mapping.source_node_type} at external source node ${mapping.source_node_id}.`,
            });
        } else {
            const output = indexedSlot(observed, "outputs", mapping.source_output_index);
            if (
                !isRecord(output)
                || output.name !== mapping.source_output
                || output.type !== mapping.type
            ) {
                issues.push({
                    code: "external_source_output_mismatch",
                    node_id: mapping.source_node_id,
                    source_output_index: mapping.source_output_index,
                    message: `External source ${mapping.source_node_id}.${mapping.source_output} no longer has the exact planned name and type.`,
                });
            }
        }
    }
    return { valid: issues.length === 0, issues };
}


function graphVertex(kind, value) {
    return `${kind}:${kind === "node" ? idKey(value) : value}`;
}


function plannedTopologyReferences(request, siblingConnections) {
    const references = siblingConnections.map(connection => ({
        source: graphVertex("node", connection.source_node_id),
        target: graphVertex("node", connection.target_node_id),
    }));
    const aliasVertex = alias => graphVertex("alias", alias);
    const nodeVertex = id => graphVertex("node", id);
    const replacement = request.replacement;

    if (request.operation === "delete") {
        const first = request.expected_path.connections[0];
        const last = request.expected_path.connections.at(-1);
        references.push({ source: nodeVertex(first.source_node_id), target: nodeVertex(last.target_node_id) });
        return references;
    }
    if (request.operation === "append") {
        references.push({
            source: nodeVertex(replacement.primary_input.source_node_id),
            target: aliasVertex(replacement.primary_input.target_alias),
        });
    } else {
        const first = request.expected_path.connections[0];
        const last = request.expected_path.connections.at(-1);
        references.push({
            source: nodeVertex(first.source_node_id),
            target: aliasVertex(replacement.input.target_alias),
        });
        references.push({
            source: aliasVertex(replacement.output.source_alias),
            target: nodeVertex(last.target_node_id),
        });
    }
    for (const sideInput of replacement.side_inputs) {
        references.push({
            source: nodeVertex(sideInput.source_node_id),
            target: aliasVertex(sideInput.target_alias),
        });
    }
    for (const connection of replacement.connections) {
        references.push({
            source: aliasVertex(connection.source_alias),
            target: aliasVertex(connection.target_alias),
        });
    }
    return references;
}


function findTopologyCycle(references) {
    const adjacency = new Map();
    const indegree = new Map();
    for (const { source, target } of references) {
        const targets = adjacency.get(source) || [];
        targets.push(target);
        adjacency.set(source, targets);
        if (!adjacency.has(target)) adjacency.set(target, []);
        if (!indegree.has(source)) indegree.set(source, 0);
        indegree.set(target, (indegree.get(target) || 0) + 1);
    }
    const ready = [...indegree.entries()]
        .filter(([, degree]) => degree === 0)
        .map(([vertex]) => vertex);
    let visited = 0;
    while (ready.length > 0) {
        const vertex = ready.pop();
        visited += 1;
        for (const target of adjacency.get(vertex) || []) {
            const next = indegree.get(target) - 1;
            indegree.set(target, next);
            if (next === 0) ready.push(target);
        }
    }
    return visited !== indegree.size;
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
    const replacement = request.replacement;
    if (request.operation === "append") {
        const connections = [plannedConnection(
            replacement.primary_input.source_node_id,
            aliases[replacement.primary_input.target_alias],
            replacement.primary_input,
        )];
        for (const sideInput of replacement.side_inputs) {
            connections.push(plannedConnection(
                sideInput.source_node_id,
                aliases[sideInput.target_alias],
                sideInput,
            ));
        }
        for (const internal of replacement.connections) {
            connections.push(plannedConnection(
                aliases[internal.source_alias],
                aliases[internal.target_alias],
                internal,
            ));
        }
        return connections;
    }

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
    for (const sideInput of replacement.side_inputs) {
        connections.push(plannedConnection(
            sideInput.source_node_id,
            aliases[sideInput.target_alias],
            sideInput,
        ));
    }
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


const MUTATION_OWNED_WORKFLOW_FIELDS = new Set([
    "nodes",
    "links",
    "last_node_id",
    "last_link_id",
    "revision",
]);


function comparableWorkflowExtra(extra) {
    if (extra === undefined || extra === null) return {};
    if (!isRecord(extra)) return extra;
    return Object.fromEntries(
        Object.entries(extra)
            .filter(([key]) => key !== "ds" && key !== REFINEMENT_LEDGER_KEY)
            .map(([key, value]) => [key, clone(value)]),
    );
}


function comparableWorkflowState(state) {
    if (state === undefined || state === null) return {};
    if (!isRecord(state)) return state;
    return Object.fromEntries(
        Object.entries(state)
            .filter(([key]) => key !== "lastNodeId" && key !== "lastLinkId")
            .map(([key, value]) => [key, clone(value)]),
    );
}


function changedWorkflowEnvelopeFields(beforeSnapshot, observedSnapshot) {
    const keys = new Set([
        ...Object.keys(isRecord(beforeSnapshot) ? beforeSnapshot : {}),
        ...Object.keys(isRecord(observedSnapshot) ? observedSnapshot : {}),
    ]);
    const changed = [];
    for (const key of keys) {
        if (MUTATION_OWNED_WORKFLOW_FIELDS.has(key)) continue;
        const beforeValue = key === "extra"
            ? comparableWorkflowExtra(beforeSnapshot?.extra)
            : key === "state"
                ? comparableWorkflowState(beforeSnapshot?.state)
                : beforeSnapshot?.[key];
        const observedValue = key === "extra"
            ? comparableWorkflowExtra(observedSnapshot?.extra)
            : key === "state"
                ? comparableWorkflowState(observedSnapshot?.state)
                : observedSnapshot?.[key];
        if (!valuesEqual(observedValue, beforeValue)) changed.push(key);
    }
    return changed.sort();
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
    const observedSnapshot = await adapter.captureWorkflow();
    const observedNodeFacts = snapshotNodeFacts(observedSnapshot);
    const changedWorkflowFields = changedWorkflowEnvelopeFields(
        beforeSnapshot,
        observedSnapshot,
    );
    if (changedWorkflowFields.length > 0) {
        issues.push({
            code: "workflow_metadata_changed",
            fields: changedWorkflowFields,
            message: "A workflow-level field outside the refined graph changed during refinement.",
        });
    }
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
            message: "The resulting graph does not contain exactly the preserved and planned connections.",
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
                message: `Expected exactly one planned graph connection; found ${matches.length}.`,
            });
        }
    }

    const removedIds = request.expected_path.nodes.map(node => node.node_id);
    const expectedNodeKeys = new Set(snapshotNodeFacts(beforeSnapshot).keys());
    for (const id of removedIds) expectedNodeKeys.delete(idKey(id));
    for (const id of Object.values(aliases)) expectedNodeKeys.add(idKey(id));
    if (
        observedNodeFacts.size !== expectedNodeKeys.size
        || [...expectedNodeKeys].some(key => !observedNodeFacts.has(key))
    ) {
        issues.push({
            code: "refinement_node_set_mismatch",
            message: "The resulting graph contains a missing or undeclared node.",
        });
    }
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

    if (request.operation === "append" && request.replacement?.output) {
        const output = request.replacement.output;
        const observedNode = await adapter.getNode(aliases[output.source_alias]);
        const observedOutput = indexedSlot(
            observedNode,
            "outputs",
            output.source_output_index,
        );
        if (
            !isRecord(observedOutput)
            || observedOutput.name !== output.source_output
            || observedOutput.type !== output.type
        ) {
            issues.push({
                code: "replacement_output_mismatch",
                alias: output.source_alias,
                message: `The final output ${output.source_alias}.${output.source_output} is not exact.`,
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
 * Apply one hash-pinned graph refinement through a pure canvas adapter. Created
 * nodes form an ordered spine, while exact existing sources may fan into it.
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
        const existing = Object.prototype.hasOwnProperty.call(
            ledger.entries,
            request.application_id,
        )
            ? ledger.entries[request.application_id]
            : null;
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

        const { allConnections, pathObservation, inputObservation } = await withAdapterReadGuard(
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
                const observedInputs = await observeExistingInputs(request, adapter);
                return {
                    allConnections: observedConnections,
                    pathObservation: observedPath,
                    inputObservation: observedInputs,
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
        if (!inputObservation.valid) {
            verification = inputObservation;
            throw refinementError(
                "external_input_mismatch",
                "An exact existing-node graph input is no longer available.",
                { issues: inputObservation.issues },
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
        if (findTopologyCycle(plannedTopologyReferences(request, siblingConnections))) {
            throw refinementError(
                "refinement_cycle",
                "The planned workflow graph would contain a cycle.",
            );
        }

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
