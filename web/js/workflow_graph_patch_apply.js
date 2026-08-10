/** Atomic, rollback-safe application of arbitrary acyclic workflow graph patches. */

import {
    canonicalWorkflowJSON,
    workflowGraphHash,
} from "./graph_precondition.js";
import { nodeIdsEqual } from "./node_identity.js";

export const WORKFLOW_GRAPH_PATCH_SCHEMA = "fl-mcp.workflow-graph-patch.v2";
export const SCOPED_WORKFLOW_GRAPH_PATCH_SCHEMA = "fl-mcp.workflow-graph-patch.v3";
export const WORKFLOW_GRAPH_PATCH_PROPERTY = "fl_mcp_workflow_graph_patch";
export const GRAPH_PATCH_LEDGER_KEY = "fl_mcp_graph_patch_ledger";

// These IDs exist only inside the private scoped adapter. They are never
// accepted on the v3 wire and are mapped to ComfyUI's native Subgraph
// inputNode/outputNode connection APIs before any mutation.
export const GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID = -10;
export const GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID = -20;

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const APPLICATION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const ALIAS_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const LEDGER_LIMIT = 64;
const LEDGER_ENTRY_NODE_ID_LIMIT = 100;
const LEDGER_NODE_ID_STRING_LIMIT = 4_096;
const MAX_CHAT_ATTACHMENT_BYTES = 32 * 1024 * 1024;
const MAX_CHAT_ATTACHMENTS = 8;
const MAX_SCOPE_DEPTH = 32;
const MAX_SCOPE_INSTANCES = 8_192;
const SCOPE_INPUT_NODE_TYPE = "__fl_mcp_scope_input__";
const SCOPE_OUTPUT_NODE_TYPE = "__fl_mcp_scope_output__";
const SCOPE_INPUT_SCHEMA_HASH = "0".repeat(64);
const SCOPE_OUTPUT_SCHEMA_HASH = "f".repeat(64);
const SCOPE_DEFINITION_HASH_SCHEMA = "fl-mcp.workflow-scope-definition-hash.v2";
const MAX_SCOPE_DEFINITION_JSON_DEPTH = 64;
const MAX_SCOPE_DEFINITION_JSON_FACTS = 200_000;
const MAX_SCOPE_DEFINITION_JSON_BYTES = 8_388_608;
const WORKFLOW_OWNED_FIELDS = new Set([
    "nodes",
    "links",
    "last_node_id",
    "last_link_id",
    "revision",
]);
const NODE_IDENTITY_FIELDS = new Set([
    "id",
    "node_id",
    "type",
    "node_type",
    "pos",
    "position",
    "size",
    "order",
    "values",
    "serialized_node",
]);


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


function typedValuesEqual(left, right) {
    return typeof left === typeof right && Object.is(left, right);
}


function typedIdKey(value) {
    return `${typeof value}:${String(value)}`;
}


function idKey(value) {
    return String(value);
}


function nodeId(value) {
    return value?.node_id ?? value?.id;
}


function nodeType(value) {
    return value?.node_type ?? value?.type;
}


function isIndex(value) {
    return Number.isInteger(value) && value >= 0;
}


function graphPatchError(code, message, details = null) {
    const error = new Error(message);
    error.code = code;
    if (details !== null) error.details = details;
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
        patch_schema: request?.patch_schema || (
            request?.plan?.operation === "scoped_patch"
                ? SCOPED_WORKFLOW_GRAPH_PATCH_SCHEMA
                : WORKFLOW_GRAPH_PATCH_SCHEMA
        ),
        application_id: request?.application_id ?? null,
        patch_hash: request?.patch_hash ?? null,
        operation: request?.operation || request?.plan?.operation || "patch",
        expected_workflow_identity: request?.plan?.expected_workflow_identity ?? null,
        error: {
            code: error?.code || "graph_patch_failed",
            message: String(error?.message || error),
            ...(error?.details !== undefined ? { details: clone(error.details) } : {}),
        },
        verification: extras.verification || { valid: false, issues: [] },
        rollback: extras.rollback || emptyRollback(),
        queued: false,
    };
}


function normalizeRef(value, label, allowedKinds = new Set(["existing", "new"])) {
    if (!isRecord(value)) {
        throw graphPatchError("invalid_graph_patch", `${label} must be a node reference.`);
    }
    const hasNodeId = Object.prototype.hasOwnProperty.call(value, "node_id");
    const hasAlias = Object.prototype.hasOwnProperty.call(value, "alias");
    if (hasNodeId === hasAlias) {
        throw graphPatchError(
            "invalid_graph_patch",
            `${label} must contain exactly one of node_id or alias.`,
        );
    }
    if (hasNodeId) {
        if (
            !allowedKinds.has("existing")
            || value.node_id === null
            || value.node_id === undefined
            || (typeof value.node_id !== "number" && typeof value.node_id !== "string")
            || String(value.node_id).length === 0
        ) {
            throw graphPatchError("invalid_graph_patch", `${label}.node_id is invalid.`);
        }
        return { node_id: value.node_id };
    }
    if (!allowedKinds.has("new") || !ALIAS_PATTERN.test(String(value.alias || ""))) {
        throw graphPatchError("invalid_graph_patch", `${label}.alias is invalid.`);
    }
    return { alias: value.alias };
}


function refKey(ref) {
    return Object.prototype.hasOwnProperty.call(ref, "node_id")
        ? `existing:${idKey(ref.node_id)}`
        : `new:${ref.alias}`;
}


function normalizeLayout(value, label) {
    if (value === undefined || value === null) return null;
    if (
        !isRecord(value)
        || !Number.isFinite(value.x)
        || !Number.isFinite(value.y)
        || (value.width !== undefined && !Number.isFinite(value.width))
        || (value.height !== undefined && !Number.isFinite(value.height))
    ) {
        throw graphPatchError("invalid_graph_patch", `${label} is not an exact layout hint.`);
    }
    return {
        x: value.x,
        y: value.y,
        ...(value.width !== undefined ? { width: value.width } : {}),
        ...(value.height !== undefined ? { height: value.height } : {}),
    };
}


function normalizeSource(value, label) {
    if (
        !isRecord(value)
        || !isIndex(value.output_index)
        || typeof value.output !== "string"
        || !value.output
        || typeof value.type !== "string"
        || !value.type
    ) {
        throw graphPatchError("invalid_graph_patch", `${label} is not an exact output endpoint.`);
    }
    return {
        ref: normalizeRef(value.ref, `${label}.ref`),
        output_index: value.output_index,
        output: value.output,
        type: value.type,
    };
}


function normalizeTarget(value, label) {
    if (
        !isRecord(value)
        || !isIndex(value.input_index)
        || !isIndex(value.occurrence_index)
        || typeof value.input !== "string"
        || !value.input
        || typeof value.type !== "string"
        || !value.type
        || !["slot", "convert_widget"].includes(value.mode)
    ) {
        throw graphPatchError("invalid_graph_patch", `${label} is not an exact input endpoint.`);
    }
    if (
        (value.mode === "slot" && !isIndex(value.socket_index))
        || (value.mode === "convert_widget" && value.socket_index !== null)
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            `${label}.socket_index does not match ${value.mode} mode.`,
        );
    }
    return {
        ref: normalizeRef(value.ref, `${label}.ref`),
        input_index: value.input_index,
        occurrence_index: value.occurrence_index,
        socket_index: value.socket_index,
        input: value.input,
        type: value.type,
        mode: value.mode,
    };
}


function normalizeEdge(value, label) {
    if (!isRecord(value)) {
        throw graphPatchError("invalid_graph_patch", `${label} must be an exact edge.`);
    }
    return {
        source: normalizeSource(value.source, `${label}.source`),
        target: normalizeTarget(value.target, `${label}.target`),
    };
}


function symbolicEdgeKey(edge) {
    return [
        refKey(edge.source.ref),
        edge.source.output_index,
        edge.source.output,
        refKey(edge.target.ref),
        edge.target.input_index,
        edge.target.occurrence_index,
        edge.target.input,
    ].join("|");
}


function symbolicTargetKey(target) {
    return [
        refKey(target.ref),
        target.input_index,
        target.occurrence_index,
        target.input,
    ].join("|");
}


function normalizeAssertionNode(value, index) {
    if (
        !isRecord(value)
        || typeof value.node_type !== "string"
        || !value.node_type
        || !SHA256_PATTERN.test(String(value.schema_hash || ""))
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            `assertions.nodes[${index}] is not an exact existing-node assertion.`,
        );
    }
    return {
        ref: normalizeRef(value.ref, `assertions.nodes[${index}].ref`, new Set(["existing"])),
        node_type: value.node_type,
        schema_hash: value.schema_hash,
    };
}


function normalizeCreateNode(value, index) {
    if (
        !isRecord(value)
        || !ALIAS_PATTERN.test(String(value.alias || ""))
        || typeof value.node_type !== "string"
        || !value.node_type
        || !SHA256_PATTERN.test(String(value.schema_hash || ""))
        || !isRecord(value.values)
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            `create_nodes[${index}] is not a canonical created node.`,
        );
    }
    return {
        alias: value.alias,
        node_type: value.node_type,
        schema_hash: value.schema_hash,
        values: clone(value.values),
        layout_hint: normalizeLayout(value.layout_hint, `create_nodes[${index}].layout_hint`),
    };
}


function normalizeUpdateNode(value, index) {
    if (
        !isRecord(value)
        || typeof value.node_type !== "string"
        || !value.node_type
        || !SHA256_PATTERN.test(String(value.schema_hash || ""))
        || !isRecord(value.expected_values)
        || !isRecord(value.set_values)
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            `update_nodes[${index}] is not a canonical node update.`,
        );
    }
    return {
        ref: normalizeRef(value.ref, `update_nodes[${index}].ref`, new Set(["existing"])),
        node_type: value.node_type,
        schema_hash: value.schema_hash,
        expected_values: clone(value.expected_values),
        set_values: clone(value.set_values),
        layout_hint: normalizeLayout(value.layout_hint, `update_nodes[${index}].layout_hint`),
    };
}


function normalizeRemoveNode(value, index) {
    if (
        !isRecord(value)
        || typeof value.node_type !== "string"
        || !value.node_type
        || !SHA256_PATTERN.test(String(value.schema_hash || ""))
        || !Array.isArray(value.expected_incident_edges)
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            `remove_nodes[${index}] is not a canonical node removal.`,
        );
    }
    return {
        ref: normalizeRef(value.ref, `remove_nodes[${index}].ref`, new Set(["existing"])),
        node_type: value.node_type,
        schema_hash: value.schema_hash,
        expected_incident_edges: value.expected_incident_edges.map((edge, edgeIndex) => (
            normalizeEdge(edge, `remove_nodes[${index}].expected_incident_edges[${edgeIndex}]`)
        )),
    };
}


function normalizeAttachment(value, index) {
    const safeFilename = (
        typeof value?.filename === "string"
        && value.filename.length > 0
        && value.filename.length <= 512
        && !value.filename.includes("/")
        && !value.filename.includes("\\")
        && ![".", ".."].includes(value.filename)
        && !/^[A-Za-z]:/.test(value.filename)
    );
    const subfolderParts = typeof value?.subfolder === "string"
        ? value.subfolder.split("/")
        : [];
    const safeSubfolder = (
        typeof value?.subfolder === "string"
        && value.subfolder.length > 0
        && value.subfolder.length <= 1024
        && !value.subfolder.includes("\\")
        && subfolderParts[0] === "ren-chat"
        && subfolderParts.every(part => part && part !== "." && part !== "..")
    );
    if (
        !isRecord(value)
        || !isIndex(value.input_index)
        || typeof value.input !== "string"
        || !value.input
        || typeof value.type !== "string"
        || !value.type
        || !safeFilename
        || !safeSubfolder
        || value.file_type !== "input"
        || !Number.isInteger(value.size_bytes)
        || value.size_bytes < 1
        || value.size_bytes > MAX_CHAT_ATTACHMENT_BYTES
        || !SHA256_PATTERN.test(String(value.sha256 || ""))
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            `attachments[${index}] is not an exact attachment binding.`,
        );
    }
    return {
        ref: normalizeRef(value.ref, `attachments[${index}].ref`),
        input_index: value.input_index,
        input: value.input,
        type: value.type,
        filename: value.filename,
        subfolder: value.subfolder,
        file_type: value.file_type,
        size_bytes: value.size_bytes,
        sha256: value.sha256,
    };
}


function normalizeExpectedDelta(value) {
    const fields = [
        "created_node_count",
        "updated_node_count",
        "removed_node_count",
        "added_edge_count",
        "removed_edge_count",
        "final_node_count",
        "final_edge_count",
    ];
    if (!isRecord(value) || fields.some(field => !isIndex(value[field]))) {
        throw graphPatchError("invalid_graph_patch", "expected_delta must contain exact counts.");
    }
    return Object.fromEntries(fields.map(field => [field, value[field]]));
}


function normalizeScopeNodeRef(value, label, allowedKinds = new Set(["existing", "new"])) {
    const ref = normalizeRef(value, label, allowedKinds);
    if (Object.prototype.hasOwnProperty.call(ref, "node_id")) {
        if (
            !(
                (typeof ref.node_id === "number" && Number.isInteger(ref.node_id))
                || (typeof ref.node_id === "string" && ref.node_id.length > 0)
            )
            || [GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID, GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID]
                .includes(ref.node_id)
        ) {
            throw graphPatchError(
                "invalid_scoped_graph_patch",
                `${label}.node_id must be an exact non-reserved typed node ID.`,
            );
        }
    }
    return ref;
}


function normalizeScopeStep(value, label) {
    const containerNodeId = value?.container_node_id;
    if (
        !isRecord(value)
        || !(
            (typeof containerNodeId === "number" && Number.isInteger(containerNodeId))
            || (typeof containerNodeId === "string" && containerNodeId.length > 0)
        )
        || typeof value.subgraph_id !== "string"
        || !value.subgraph_id
        || value.subgraph_id.length > 256
    ) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            `${label} is not an exact scope-path segment.`,
        );
    }
    return {
        container_node_id: containerNodeId,
        subgraph_id: value.subgraph_id,
    };
}


function normalizeScopePath(value, label) {
    if (!Array.isArray(value) || value.length < 1 || value.length > MAX_SCOPE_DEPTH) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            `${label} must contain one to ${MAX_SCOPE_DEPTH} exact scope segments.`,
        );
    }
    return value.map((step, index) => normalizeScopeStep(step, `${label}[${index}]`));
}


function scopePathKey(path) {
    return path.map(step => (
        `${typedIdKey(step.container_node_id)}\u0000${step.subgraph_id}`
    )).join("\u0001");
}


function compareScopePaths(left, right) {
    if (left.length !== right.length) return left.length - right.length;
    return scopePathKey(left).localeCompare(scopePathKey(right));
}


function normalizeScopeAuthority(value) {
    if (
        !isRecord(value)
        || typeof value.definition_id !== "string"
        || !value.definition_id
        || value.definition_id.length > 256
        || !SHA256_PATTERN.test(String(value.definition_hash || ""))
        || !["instance", "shared_definition"].includes(value.edit_mode)
        || !Array.isArray(value.affected_scope_paths)
        || value.affected_scope_paths.length < 1
        || value.affected_scope_paths.length > MAX_SCOPE_INSTANCES
    ) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            "A canonical scoped GraphPatch authority is required.",
        );
    }
    const scopePath = normalizeScopePath(value.scope_path, "scope.scope_path");
    if (scopePath.at(-1).subgraph_id !== value.definition_id) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            "scope.definition_id must equal the terminal scope-path definition.",
        );
    }
    const affected = value.affected_scope_paths.map((path, index) => (
        normalizeScopePath(path, `scope.affected_scope_paths[${index}]`)
    ));
    const keys = affected.map(scopePathKey);
    if (new Set(keys).size !== keys.length) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            "scope.affected_scope_paths must be duplicate-free.",
        );
    }
    affected.sort(compareScopePaths);
    return {
        scope_path: scopePath,
        definition_id: value.definition_id,
        definition_hash: value.definition_hash,
        edit_mode: value.edit_mode,
        affected_scope_paths: affected,
    };
}


function normalizeBoundaryIdentity(value, label) {
    if (
        !isRecord(value)
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
            String(value.slot_id || ""),
        )
        || !isIndex(value.slot_index)
        || typeof value.name !== "string"
        || !value.name
        || value.name.length > 256
        || typeof value.type !== "string"
        || !value.type
        || value.type.length > 256
    ) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            `${label} is not an immutable public boundary identity.`,
        );
    }
    return {
        slot_id: value.slot_id,
        slot_index: value.slot_index,
        name: value.name,
        type: value.type,
    };
}


function normalizeScopedSourceRef(value, label) {
    if (isRecord(value) && Object.keys(value).length === 1 && value.scope_input !== undefined) {
        return { scope_input: normalizeBoundaryIdentity(value.scope_input, `${label}.scope_input`) };
    }
    if (isRecord(value) && (value.scope_output !== undefined || value.scope_input !== undefined)) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            `${label} may use only a source-side scope_input boundary.`,
        );
    }
    return normalizeScopeNodeRef(value, label);
}


function normalizeScopedTargetRef(value, label) {
    if (isRecord(value) && Object.keys(value).length === 1 && value.scope_output !== undefined) {
        return { scope_output: normalizeBoundaryIdentity(value.scope_output, `${label}.scope_output`) };
    }
    if (isRecord(value) && (value.scope_input !== undefined || value.scope_output !== undefined)) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            `${label} may use only a target-side scope_output boundary.`,
        );
    }
    return normalizeScopeNodeRef(value, label);
}


function normalizeScopedSource(value, label) {
    if (
        !isRecord(value)
        || !isIndex(value.output_index)
        || typeof value.output !== "string"
        || !value.output
        || typeof value.type !== "string"
        || !value.type
    ) {
        throw graphPatchError("invalid_scoped_graph_patch", `${label} is not an exact output endpoint.`);
    }
    const ref = normalizeScopedSourceRef(value.ref, `${label}.ref`);
    if (ref.scope_input) {
        const fact = ref.scope_input;
        if (
            value.output_index !== fact.slot_index
            || value.output !== fact.name
            || value.type !== fact.type
        ) {
            throw graphPatchError(
                "invalid_scoped_graph_patch",
                `${label} disagrees with its scope_input boundary fact.`,
            );
        }
    }
    return { ref, output_index: value.output_index, output: value.output, type: value.type };
}


function normalizeScopedTarget(value, label) {
    if (
        !isRecord(value)
        || !isIndex(value.input_index)
        || !isIndex(value.occurrence_index)
        || typeof value.input !== "string"
        || !value.input
        || typeof value.type !== "string"
        || !value.type
        || !["slot", "convert_widget"].includes(value.mode)
        || (value.mode === "slot" && !isIndex(value.socket_index))
        || (value.mode === "convert_widget" && value.socket_index !== null)
    ) {
        throw graphPatchError("invalid_scoped_graph_patch", `${label} is not an exact input endpoint.`);
    }
    const ref = normalizeScopedTargetRef(value.ref, `${label}.ref`);
    if (ref.scope_output) {
        const fact = ref.scope_output;
        if (
            value.mode !== "slot"
            || value.input_index !== fact.slot_index
            || value.occurrence_index !== 0
            || value.socket_index !== fact.slot_index
            || value.input !== fact.name
            || value.type !== fact.type
        ) {
            throw graphPatchError(
                "invalid_scoped_graph_patch",
                `${label} disagrees with its scope_output boundary fact.`,
            );
        }
    }
    return {
        ref,
        input_index: value.input_index,
        occurrence_index: value.occurrence_index,
        socket_index: value.socket_index,
        input: value.input,
        type: value.type,
        mode: value.mode,
    };
}


function normalizeScopedEdge(value, label) {
    if (!isRecord(value)) {
        throw graphPatchError("invalid_scoped_graph_patch", `${label} must be an exact scoped edge.`);
    }
    const source = normalizeScopedSource(value.source, `${label}.source`);
    const target = normalizeScopedTarget(value.target, `${label}.target`);
    if (source.ref.scope_input && target.ref.scope_output) {
        throw graphPatchError(
            "direct_scope_boundary_edge_unsupported",
            `${label} cannot bypass a native Subgraph directly from input to output.`,
        );
    }
    return { source, target };
}


function normalizeScopedRemoveNode(value, index) {
    if (
        !isRecord(value)
        || typeof value.node_type !== "string"
        || !value.node_type
        || !SHA256_PATTERN.test(String(value.schema_hash || ""))
        || !Array.isArray(value.expected_incident_edges)
    ) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            `remove_nodes[${index}] is not a canonical scoped node removal.`,
        );
    }
    return {
        ref: normalizeScopeNodeRef(
            value.ref,
            `remove_nodes[${index}].ref`,
            new Set(["existing"]),
        ),
        node_type: value.node_type,
        schema_hash: value.schema_hash,
        expected_incident_edges: value.expected_incident_edges.map((edge, edgeIndex) => (
            normalizeScopedEdge(edge, `remove_nodes[${index}].expected_incident_edges[${edgeIndex}]`)
        )),
    };
}


function lowerScopeRef(ref) {
    if (ref.scope_input) return { node_id: GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID };
    if (ref.scope_output) return { node_id: GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID };
    return clone(ref);
}


function lowerScopedEdge(edge) {
    return {
        source: { ...clone(edge.source), ref: lowerScopeRef(edge.source.ref) },
        target: { ...clone(edge.target), ref: lowerScopeRef(edge.target.ref) },
    };
}


function normalizeScopedRequest(request) {
    const plan = request?.plan;
    if (
        !isRecord(request)
        || !APPLICATION_ID_PATTERN.test(String(request.application_id || ""))
        || !SHA256_PATTERN.test(String(request.expected_catalog_hash || ""))
        || !SHA256_PATTERN.test(String(request.patch_hash || ""))
        || !isRecord(plan)
        || plan.operation !== "scoped_patch"
        || typeof plan.expected_workflow_identity !== "string"
        || !plan.expected_workflow_identity
        || !SHA256_PATTERN.test(String(plan.expected_graph_hash || ""))
        || !isRecord(plan.assertions)
        || !Array.isArray(plan.assertions.nodes)
        || !Array.isArray(plan.assertions.edges)
        || !Array.isArray(plan.create_nodes)
        || !Array.isArray(plan.update_nodes)
        || !Array.isArray(plan.remove_edges)
        || !Array.isArray(plan.add_edges)
        || !Array.isArray(plan.remove_nodes)
        || !Array.isArray(plan.attachments)
        || plan.attachments.length !== 0
    ) {
        throw graphPatchError(
            "invalid_scoped_graph_patch",
            "A canonical workflow GraphPatch v3 scoped apply request is required.",
        );
    }
    const scope = normalizeScopeAuthority(plan.scope);
    const publicPlan = {
        operation: "scoped_patch",
        expected_workflow_identity: plan.expected_workflow_identity,
        expected_graph_hash: plan.expected_graph_hash,
        scope,
        assertions: {
            nodes: plan.assertions.nodes.map(normalizeAssertionNode),
            edges: plan.assertions.edges.map((edge, index) => (
                normalizeScopedEdge(edge, `assertions.edges[${index}]`)
            )),
        },
        create_nodes: plan.create_nodes.map(normalizeCreateNode),
        update_nodes: plan.update_nodes.map(normalizeUpdateNode),
        remove_edges: plan.remove_edges.map((edge, index) => (
            normalizeScopedEdge(edge, `remove_edges[${index}]`)
        )),
        add_edges: plan.add_edges.map((edge, index) => (
            normalizeScopedEdge(edge, `add_edges[${index}]`)
        )),
        remove_nodes: plan.remove_nodes.map(normalizeScopedRemoveNode),
        attachments: [],
        expected_delta: normalizeExpectedDelta(plan.expected_delta),
    };
    const lowerEdges = edges => edges.map(lowerScopedEdge);
    const loweredPlan = {
        operation: "scoped_patch",
        expected_workflow_identity: publicPlan.expected_workflow_identity,
        expected_graph_hash: publicPlan.expected_graph_hash,
        assertions: {
            nodes: [
                ...publicPlan.assertions.nodes,
                {
                    ref: { node_id: GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID },
                    node_type: SCOPE_INPUT_NODE_TYPE,
                    schema_hash: SCOPE_INPUT_SCHEMA_HASH,
                },
                {
                    ref: { node_id: GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID },
                    node_type: SCOPE_OUTPUT_NODE_TYPE,
                    schema_hash: SCOPE_OUTPUT_SCHEMA_HASH,
                },
            ],
            edges: lowerEdges(publicPlan.assertions.edges),
        },
        create_nodes: clone(publicPlan.create_nodes),
        update_nodes: clone(publicPlan.update_nodes),
        remove_edges: lowerEdges(publicPlan.remove_edges),
        add_edges: lowerEdges(publicPlan.add_edges),
        remove_nodes: publicPlan.remove_nodes.map(item => ({
            ...clone(item),
            expected_incident_edges: lowerEdges(item.expected_incident_edges),
        })),
        attachments: [],
        expected_delta: {
            ...clone(publicPlan.expected_delta),
            final_node_count: publicPlan.expected_delta.final_node_count + 2,
        },
    };
    validateNormalizedPlan(loweredPlan);
    return {
        application_id: request.application_id,
        expected_catalog_hash: request.expected_catalog_hash,
        patch_hash: request.patch_hash,
        patch_schema: SCOPED_WORKFLOW_GRAPH_PATCH_SCHEMA,
        operation: "scoped_patch",
        scope,
        public_plan: publicPlan,
        plan: loweredPlan,
    };
}


function normalizeRequest(request) {
    const plan = request?.plan;
    if (
        !isRecord(request)
        || !APPLICATION_ID_PATTERN.test(String(request.application_id || ""))
        || !SHA256_PATTERN.test(String(request.expected_catalog_hash || ""))
        || !SHA256_PATTERN.test(String(request.patch_hash || ""))
        || !isRecord(plan)
        || plan.operation !== "patch"
        || typeof plan.expected_workflow_identity !== "string"
        || !plan.expected_workflow_identity
        || !SHA256_PATTERN.test(String(plan.expected_graph_hash || ""))
        || !isRecord(plan.assertions)
        || !Array.isArray(plan.assertions.nodes)
        || !Array.isArray(plan.assertions.edges)
        || !Array.isArray(plan.create_nodes)
        || !Array.isArray(plan.update_nodes)
        || !Array.isArray(plan.remove_edges)
        || !Array.isArray(plan.add_edges)
        || !Array.isArray(plan.remove_nodes)
        || !Array.isArray(plan.attachments)
        || plan.attachments.length > MAX_CHAT_ATTACHMENTS
    ) {
        throw graphPatchError(
            "invalid_graph_patch",
            "A canonical workflow GraphPatch v2 apply request is required.",
        );
    }

    const normalized = {
        application_id: request.application_id,
        expected_catalog_hash: request.expected_catalog_hash,
        patch_hash: request.patch_hash,
        patch_schema: WORKFLOW_GRAPH_PATCH_SCHEMA,
        operation: "patch",
        plan: {
            operation: "patch",
            expected_workflow_identity: plan.expected_workflow_identity,
            expected_graph_hash: plan.expected_graph_hash,
            assertions: {
                nodes: plan.assertions.nodes.map(normalizeAssertionNode),
                edges: plan.assertions.edges.map((edge, index) => (
                    normalizeEdge(edge, `assertions.edges[${index}]`)
                )),
            },
            create_nodes: plan.create_nodes.map(normalizeCreateNode),
            update_nodes: plan.update_nodes.map(normalizeUpdateNode),
            remove_edges: plan.remove_edges.map((edge, index) => (
                normalizeEdge(edge, `remove_edges[${index}]`)
            )),
            add_edges: plan.add_edges.map((edge, index) => (
                normalizeEdge(edge, `add_edges[${index}]`)
            )),
            remove_nodes: plan.remove_nodes.map(normalizeRemoveNode),
            attachments: plan.attachments.map(normalizeAttachment),
            expected_delta: normalizeExpectedDelta(plan.expected_delta),
        },
    };
    validateNormalizedPlan(normalized.plan);
    return normalized;
}


function normalizeApplyRequest(request) {
    return request?.plan?.operation === "scoped_patch"
        ? normalizeScopedRequest(request)
        : normalizeRequest(request);
}


function assertUnicodeScalarString(value, path) {
    for (let index = 0; index < value.length; index += 1) {
        const code = value.charCodeAt(index);
        if (code >= 0xd800 && code <= 0xdbff) {
            const next = value.charCodeAt(index + 1);
            if (!(next >= 0xdc00 && next <= 0xdfff)) {
                throw graphPatchError(
                    "non_json_scoped_definition",
                    `The scoped definition contains an invalid UTF-8 string at ${path}.`,
                );
            }
            index += 1;
        } else if (code >= 0xdc00 && code <= 0xdfff) {
            throw graphPatchError(
                "non_json_scoped_definition",
                `The scoped definition contains an invalid UTF-8 string at ${path}.`,
            );
        }
    }
}


function consumeScopeText(value, path, budget) {
    assertUnicodeScalarString(value, path);
    const byteLength = new TextEncoder().encode(value).byteLength;
    budget.textBytes += byteLength;
    if (budget.textBytes > MAX_SCOPE_DEFINITION_JSON_BYTES) {
        throw graphPatchError(
            "scoped_definition_hash_size_exceeded",
            `The scoped definition exceeds the byte limit at ${path}.`,
        );
    }
}


function assertStrictJSON(
    value,
    path = "definition",
    depth = 0,
    budget = { facts: 0, textBytes: 0, active: new Set() },
) {
    if (depth > MAX_SCOPE_DEFINITION_JSON_DEPTH) {
        throw graphPatchError(
            "scoped_definition_hash_depth_exceeded",
            `The scoped definition exceeds the depth limit at ${path}.`,
        );
    }
    budget.facts += 1;
    if (budget.facts > MAX_SCOPE_DEFINITION_JSON_FACTS) {
        throw graphPatchError(
            "scoped_definition_hash_fact_limit_exceeded",
            `The scoped definition exceeds the fact limit at ${path}.`,
        );
    }
    if (value === null || typeof value === "boolean") return;
    if (typeof value === "number") {
        if (!Number.isFinite(value)) {
            throw graphPatchError(
                "non_json_scoped_definition",
                `The scoped definition contains a non-finite number at ${path}.`,
            );
        }
        return;
    }
    if (typeof value === "string") {
        consumeScopeText(value, path, budget);
        return;
    }
    if (Array.isArray(value)) {
        if (budget.active.has(value)) {
            throw graphPatchError(
                "non_json_scoped_definition",
                `The scoped definition contains a cyclic array at ${path}.`,
            );
        }
        if (budget.facts + value.length > MAX_SCOPE_DEFINITION_JSON_FACTS) {
            throw graphPatchError(
                "scoped_definition_hash_fact_limit_exceeded",
                `The scoped definition exceeds the fact limit at ${path}.`,
            );
        }
        const ownKeys = Object.keys(value);
        if (
            ownKeys.length !== value.length
            || ownKeys.some((key, index) => key !== String(index))
        ) {
            throw graphPatchError(
                "non_json_scoped_definition",
                `The scoped definition contains a sparse or decorated array at ${path}.`,
            );
        }
        budget.active.add(value);
        try {
            for (let index = 0; index < value.length; index += 1) {
                assertStrictJSON(value[index], `${path}[${index}]`, depth + 1, budget);
            }
        } finally {
            budget.active.delete(value);
        }
        return;
    }
    if (isRecord(value)) {
        if (
            ![Object.prototype, null].includes(Object.getPrototypeOf(value))
            || Object.getOwnPropertySymbols(value).length > 0
        ) {
            throw graphPatchError(
                "non_json_scoped_definition",
                `The scoped definition contains a non-JSON object at ${path}.`,
            );
        }
        if (budget.active.has(value)) {
            throw graphPatchError(
                "non_json_scoped_definition",
                `The scoped definition contains a cyclic object at ${path}.`,
            );
        }
        const entries = Object.entries(value);
        if (budget.facts + (2 * entries.length) > MAX_SCOPE_DEFINITION_JSON_FACTS) {
            throw graphPatchError(
                "scoped_definition_hash_fact_limit_exceeded",
                `The scoped definition exceeds the fact limit at ${path}.`,
            );
        }
        budget.active.add(value);
        try {
            for (const [key, item] of entries) {
                budget.facts += 1;
                consumeScopeText(key, `${path}.${key}`, budget);
                assertStrictJSON(item, `${path}.${key}`, depth + 1, budget);
            }
        } finally {
            budget.active.delete(value);
        }
        return;
    }
    throw graphPatchError(
        "non_json_scoped_definition",
        `The scoped definition contains a non-JSON value at ${path}.`,
    );
}


function projectNativeScopeDefinition(definition) {
    const active = new Set();
    const textBudget = { textBytes: 0 };
    let facts = 0;

    const consumeFact = path => {
        facts += 1;
        if (facts > MAX_SCOPE_DEFINITION_JSON_FACTS) {
            throw graphPatchError(
                "scoped_definition_hash_fact_limit_exceeded",
                `The scoped definition exceeds the fact limit at ${path}.`,
            );
        }
    };

    const project = (value, path, depth) => {
        if (depth > MAX_SCOPE_DEFINITION_JSON_DEPTH) {
            throw graphPatchError(
                "scoped_definition_hash_depth_exceeded",
                `The scoped definition exceeds the depth limit at ${path}.`,
            );
        }
        consumeFact(path);
        if (typeof value === "string") {
            consumeScopeText(value, path, textBudget);
            return value;
        }
        if (value === null || typeof value === "boolean") {
            return value;
        }
        if (typeof value === "number") {
            if (!Number.isFinite(value)) {
                throw graphPatchError(
                    "non_json_scoped_definition",
                    `The scoped definition contains a non-finite number at ${path}.`,
                );
            }
            return value;
        }
        if (Array.isArray(value)) {
            const ownKeys = Object.keys(value);
            if (
                Object.getPrototypeOf(value) !== Array.prototype
                || Object.getOwnPropertySymbols(value).length > 0
                || ownKeys.length !== value.length
                || ownKeys.some((key, index) => key !== String(index))
            ) {
                throw graphPatchError(
                    "non_json_scoped_definition",
                    `The scoped definition contains a sparse, decorated, or non-JSON array at ${path}.`,
                );
            }
            if (facts + value.length > MAX_SCOPE_DEFINITION_JSON_FACTS) {
                throw graphPatchError(
                    "scoped_definition_hash_fact_limit_exceeded",
                    `The scoped definition exceeds the fact limit at ${path}.`,
                );
            }
            if (active.has(value)) {
                throw graphPatchError(
                    "non_json_scoped_definition",
                    `The scoped definition contains a cyclic array at ${path}.`,
                );
            }
            active.add(value);
            try {
                return value.map((item, index) => project(item, `${path}[${index}]`, depth + 1));
            } finally {
                active.delete(value);
            }
        }
        if (value && typeof value === "object") {
            const prototype = Object.getPrototypeOf(value);
            if (
                ![Object.prototype, null].includes(prototype)
                || Object.getOwnPropertySymbols(value).length > 0
            ) {
                throw graphPatchError(
                    "non_json_scoped_definition",
                    `The scoped definition contains a non-JSON object at ${path}.`,
                );
            }
            if (active.has(value)) {
                throw graphPatchError(
                    "non_json_scoped_definition",
                    `The scoped definition contains a cyclic object at ${path}.`,
                );
            }
            const keys = Object.keys(value);
            if (facts + keys.length > MAX_SCOPE_DEFINITION_JSON_FACTS) {
                throw graphPatchError(
                    "scoped_definition_hash_fact_limit_exceeded",
                    `The scoped definition exceeds the fact limit at ${path}.`,
                );
            }
            active.add(value);
            try {
                const result = prototype === null ? Object.create(null) : {};
                for (const key of keys) {
                    const item = value[key];
                    consumeFact(`${path}.${key}`);
                    consumeScopeText(key, `${path}.${key}`, textBudget);
                    // Native Comfy serializers intentionally emit optional
                    // object fields as undefined. JSON workflow transport
                    // omits those fields, so make that one projection exact.
                    if (item === undefined) continue;
                    result[key] = project(item, `${path}.${key}`, depth + 1);
                }
                return result;
            } finally {
                active.delete(value);
            }
        }
        throw graphPatchError(
            "non_json_scoped_definition",
            `The scoped definition contains a non-JSON value at ${path}.`,
        );
    };

    const projected = project(definition, "definition", 0);
    assertStrictJSON(projected);
    return projected;
}


async function sha256Hex(value) {
    const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
    if (globalThis.crypto?.subtle) {
        const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
        return [...new Uint8Array(digest)]
            .map(byte => byte.toString(16).padStart(2, "0"))
            .join("");
    }
    if (typeof process !== "undefined" && process.versions?.node) {
        const { createHash } = await import("node:crypto");
        return createHash("sha256").update(bytes).digest("hex");
    }
    throw graphPatchError("sha256_unavailable", "SHA-256 is unavailable in this browser.");
}


function compareUnicodeScalarStrings(left, right) {
    const leftPoints = Array.from(left, item => item.codePointAt(0));
    const rightPoints = Array.from(right, item => item.codePointAt(0));
    const length = Math.min(leftPoints.length, rightPoints.length);
    for (let index = 0; index < length; index += 1) {
        if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
    }
    return leftPoints.length - rightPoints.length;
}


function bytesToHex(bytes) {
    return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
}


function canonicalScopeNumber(value) {
    const normalized = Object.is(value, -0) ? 0 : value;
    const buffer = new ArrayBuffer(8);
    new DataView(buffer).setFloat64(0, normalized, false);
    return `d${bytesToHex(new Uint8Array(buffer))};`;
}


function canonicalScopeString(value) {
    const encoded = new TextEncoder().encode(value);
    return `s${encoded.byteLength}:${bytesToHex(encoded)};`;
}


function canonicalScopeValue(value) {
    if (value === null) return "n;";
    if (typeof value === "boolean") return value ? "b1;" : "b0;";
    if (typeof value === "number") return canonicalScopeNumber(value);
    if (typeof value === "string") return canonicalScopeString(value);
    if (Array.isArray(value)) {
        return `a${value.length}:[${value.map(canonicalScopeValue).join("")}];`;
    }
    const keys = Object.keys(value).sort(compareUnicodeScalarStrings);
    return `o${keys.length}:{${keys.map(key => (
        canonicalScopeString(key) + canonicalScopeValue(value[key])
    )).join("")}};`;
}


export async function workflowScopeDefinitionHash(definition) {
    if (!isRecord(definition)) {
        throw graphPatchError(
            "invalid_scoped_definition",
            "A serialized subgraph definition object is required.",
        );
    }
    assertStrictJSON(definition);
    const canonical = canonicalScopeValue({
        schema: SCOPE_DEFINITION_HASH_SCHEMA,
        value: definition,
    });
    const payload = new TextEncoder().encode(canonical);
    if (payload.byteLength > MAX_SCOPE_DEFINITION_JSON_BYTES) {
        throw graphPatchError(
            "scoped_definition_hash_size_exceeded",
            "The canonical scoped definition exceeds the frontend byte limit.",
        );
    }
    return await sha256Hex(payload);
}


function collectionEntries(value, label) {
    if (Array.isArray(value)) return value.map((item, index) => [index, item]);
    if (isRecord(value)) return Object.entries(value);
    throw graphPatchError(
        "invalid_scoped_workflow",
        `${label} must be a serialized array or object.`,
    );
}


function exactNodeId(value, label) {
    if (
        (typeof value === "number" && Number.isInteger(value))
        || (typeof value === "string" && value.length > 0)
    ) return value;
    throw graphPatchError("invalid_scoped_workflow", `${label} is not an exact typed ID.`);
}


function definitionInventory(rootSnapshot) {
    const raw = rootSnapshot?.definitions?.subgraphs ?? [];
    const result = new Map();
    for (const [mappingId, definition] of collectionEntries(raw, "workflow.definitions.subgraphs")) {
        if (!isRecord(definition)) {
            throw graphPatchError(
                "invalid_scoped_workflow",
                "Every serialized subgraph definition must be an object.",
            );
        }
        const id = definition.id ?? (Array.isArray(raw) ? undefined : mappingId);
        if (typeof id !== "string" || !id) {
            throw graphPatchError(
                "invalid_scoped_workflow",
                "Every serialized subgraph definition needs an exact string ID.",
            );
        }
        if (result.has(id)) {
            throw graphPatchError(
                "duplicate_scoped_definition",
                `Subgraph definition ${id} is repeated.`,
            );
        }
        result.set(id, definition);
    }
    return result;
}


function payloadNodes(payload, label) {
    const result = [];
    const seen = new Set();
    for (const [mappingId, node] of collectionEntries(payload?.nodes ?? [], `${label}.nodes`)) {
        if (!isRecord(node)) {
            throw graphPatchError("invalid_scoped_workflow", `${label} contains a non-object node.`);
        }
        const id = exactNodeId(
            node.id ?? (Array.isArray(payload.nodes) ? undefined : mappingId),
            `${label}.nodes.id`,
        );
        const key = typedIdKey(id);
        if (seen.has(key)) {
            throw graphPatchError(
                "ambiguous_scoped_node_identity",
                `${label} repeats typed node ID ${String(id)}.`,
            );
        }
        seen.add(key);
        result.push({ id, node });
    }
    return result;
}


function exactPayloadNode(payload, nodeIdValue, label) {
    const matches = payloadNodes(payload, label).filter(item => (
        typedValuesEqual(item.id, nodeIdValue)
    ));
    if (matches.length !== 1) {
        throw graphPatchError(
            matches.length === 0 ? "scoped_path_not_found" : "scoped_path_ambiguous",
            `${label} does not contain exactly one typed node ${String(nodeIdValue)}.`,
        );
    }
    return matches[0].node;
}


function exactInterfaceSlots(value, label) {
    return collectionEntries(value ?? [], label).map(([, slot], index) => {
        if (
            !isRecord(slot)
            || typeof slot.name !== "string"
            || !slot.name
            || typeof slot.type !== "string"
            || !slot.type
        ) {
            throw graphPatchError(
                "invalid_scoped_interface",
                `${label}[${index}] is not an exact named typed slot.`,
            );
        }
        return { name: slot.name, type: slot.type };
    });
}


function assertContainerInterface(container, definition, label) {
    for (const field of ["inputs", "outputs"]) {
        const actual = exactInterfaceSlots(container?.[field] ?? [], `${label}.${field}`);
        const expected = exactInterfaceSlots(definition?.[field] ?? [], `definition.${field}`);
        if (!valuesEqual(actual, expected)) {
            throw graphPatchError(
                "scoped_container_interface_mismatch",
                `${label}.${field} no longer matches the referenced definition boundary.`,
            );
        }
    }
}


function resolveSerializedScope(rootSnapshot, scope) {
    const definitions = definitionInventory(rootSnapshot);
    let payload = rootSnapshot;
    let definition = null;
    for (const [index, step] of scope.scope_path.entries()) {
        const container = exactPayloadNode(payload, step.container_node_id, `scope_path[${index}]`);
        if (container.type !== step.subgraph_id) {
            throw graphPatchError(
                "scoped_path_definition_mismatch",
                `scope_path[${index}] resolves to a different definition.`,
            );
        }
        definition = definitions.get(step.subgraph_id) || null;
        if (!definition) {
            throw graphPatchError(
                "scoped_definition_missing",
                `Definition ${step.subgraph_id} is unavailable.`,
            );
        }
        assertContainerInterface(container, definition, `scope_path[${index}].container`);
        payload = definition;
    }
    if (!definition || definition.id !== scope.definition_id) {
        throw graphPatchError(
            "scoped_definition_mismatch",
            "The resolved scope does not match its terminal definition authority.",
        );
    }
    return { definitions, definition: projectNativeScopeDefinition(definition) };
}


function boundaryPort(definition, kind, identity) {
    const field = kind === "scope_input" ? "inputs" : "outputs";
    const ports = collectionEntries(definition?.[field] ?? [], `definition.${field}`)
        .map(([, port]) => port);
    const observed = ports[identity.slot_index];
    if (
        !isRecord(observed)
        || observed.id !== identity.slot_id
        || observed.name !== identity.name
        || observed.type !== identity.type
    ) {
        throw graphPatchError(
            "scoped_boundary_mismatch",
            `${kind} ${identity.slot_id} no longer matches index, UUID, name, and type.`,
        );
    }
    return observed;
}


function publicBoundaryRefs(plan) {
    const result = [];
    const add = edge => {
        if (edge.source.ref.scope_input) result.push(["scope_input", edge.source.ref.scope_input]);
        if (edge.target.ref.scope_output) result.push(["scope_output", edge.target.ref.scope_output]);
    };
    for (const edge of [...plan.assertions.edges, ...plan.remove_edges, ...plan.add_edges]) add(edge);
    for (const removal of plan.remove_nodes) {
        for (const edge of removal.expected_incident_edges) add(edge);
    }
    return result;
}


function enumerateScopeInstances(rootSnapshot, definitionId, definitions) {
    const result = [];
    const visit = (payload, path, stack) => {
        if (path.length >= MAX_SCOPE_DEPTH) {
            throw graphPatchError(
                "scoped_path_depth_exceeded",
                `Scoped instance expansion exceeds ${MAX_SCOPE_DEPTH} levels.`,
            );
        }
        for (const { id, node } of payloadNodes(payload, path.length ? "definition" : "workflow")) {
            if (typeof node.type !== "string" || !definitions.has(node.type)) continue;
            const next = [...path, { container_node_id: id, subgraph_id: node.type }];
            const definition = definitions.get(node.type);
            assertContainerInterface(node, definition, `instance.${scopePathKey(next)}`);
            if (node.type === definitionId) {
                result.push(next);
                if (result.length > MAX_SCOPE_INSTANCES) {
                    throw graphPatchError(
                        "scoped_instance_limit_exceeded",
                        `Scoped instance expansion exceeds ${MAX_SCOPE_INSTANCES} paths.`,
                    );
                }
            }
            if (stack.has(node.type)) {
                throw graphPatchError(
                    "scoped_definition_cycle",
                    `Definition recursion through ${node.type} is not mutation-safe.`,
                );
            }
            visit(definition, next, new Set([...stack, node.type]));
        }
    };
    visit(rootSnapshot, [], new Set());
    return result.sort(compareScopePaths);
}


function assertAffectedScopePaths(scope, observedPaths) {
    if (!valuesEqual(scope.affected_scope_paths, observedPaths)) {
        throw graphPatchError(
            "affected_scope_paths_mismatch",
            "The acknowledged affected scope paths are not complete for this definition.",
        );
    }
    if (!observedPaths.some(path => valuesEqual(path, scope.scope_path))) {
        throw graphPatchError(
            "selected_scope_path_missing",
            "The selected scope path is absent from the affected instance inventory.",
        );
    }
    if (scope.edit_mode === "instance" && observedPaths.length !== 1) {
        throw graphPatchError(
            "instance_detach_not_supported",
            "A reused definition cannot be changed in instance mode.",
        );
    }
}


function scopedLinkAlias(link, aliases, label) {
    const present = aliases.filter(alias => Object.prototype.hasOwnProperty.call(link, alias));
    if (present.length === 0) return undefined;
    const value = link[present[0]];
    if (present.slice(1).some(alias => !typedValuesEqual(value, link[alias]))) {
        throw graphPatchError(
            "conflicting_scoped_link_aliases",
            `Scoped link aliases for ${label} must contain one exact typed value.`,
            { aliases: present },
        );
    }
    return value;
}


function validateScopedLinkParts(parts) {
    for (const [field, value] of [
        ["id", parts.id],
        ["source", parts.sourceId],
        ["target", parts.targetId],
    ]) {
        if (
            !(
                (typeof value === "number" && Number.isInteger(value))
                || (typeof value === "string" && value.length > 0)
            )
        ) {
            throw graphPatchError("invalid_scoped_link", `Scoped link ${field} is invalid.`);
        }
    }
    if (!isIndex(parts.sourceSlot) || !isIndex(parts.targetSlot)) {
        throw graphPatchError("invalid_scoped_link", "Scoped link slot indexes are invalid.");
    }
    if (parts.type !== undefined && parts.type !== null && (
        typeof parts.type !== "string" || !parts.type
    )) {
        throw graphPatchError("invalid_scoped_link", "Scoped link type is invalid.");
    }
    return parts;
}


function linkParts(link) {
    if (Array.isArray(link)) {
        if (link.length < 5 || link.length > 6) {
            throw graphPatchError(
                "invalid_scoped_link",
                "Scoped link arrays need five or six exact fields.",
            );
        }
        return validateScopedLinkParts({
            id: link[0],
            sourceId: link[1],
            sourceSlot: link[2],
            targetId: link[3],
            targetSlot: link[4],
            type: link.length === 6 ? link[5] : null,
        });
    }
    if (!isRecord(link)) {
        throw graphPatchError("invalid_scoped_link", "A scoped link must be an array or object.");
    }
    return validateScopedLinkParts({
        id: scopedLinkAlias(link, ["id", "link_id"], "id"),
        sourceId: scopedLinkAlias(
            link,
            ["origin_id", "source_id", "source_node_id", "from_node_id"],
            "source",
        ),
        sourceSlot: scopedLinkAlias(
            link,
            ["origin_slot", "source_slot", "source_output_index"],
            "source slot",
        ),
        targetId: scopedLinkAlias(
            link,
            ["target_id", "target_node_id", "to_node_id"],
            "target",
        ),
        targetSlot: scopedLinkAlias(
            link,
            ["target_slot", "target_input_index"],
            "target slot",
        ),
        type: scopedLinkAlias(link, ["type", "link_type"], "type"),
    });
}


function multiset(values) {
    const result = new Map();
    for (const value of values) {
        const key = typedIdKey(value);
        result.set(key, (result.get(key) || 0) + 1);
    }
    return result;
}


function assertSameMultiset(left, right, label) {
    const leftSet = multiset(left);
    const rightSet = multiset(right);
    if (
        leftSet.size !== rightSet.size
        || [...leftSet].some(([key, count]) => rightSet.get(key) !== count)
    ) {
        throw graphPatchError("scoped_boundary_bookkeeping_mismatch", `${label} linkIds are stale.`);
    }
}


function attestDefinitionBoundaryBookkeeping(definition) {
    const inputs = collectionEntries(definition?.inputs ?? [], "definition.inputs")
        .map(([, port]) => port);
    const outputs = collectionEntries(definition?.outputs ?? [], "definition.outputs")
        .map(([, port]) => port);
    const inputLinks = inputs.map(() => []);
    const outputLinks = outputs.map(() => []);
    for (const raw of collectionEntries(definition?.links ?? [], "definition.links").map(([, link]) => link)) {
        const link = linkParts(raw);
        if (
            typedValuesEqual(link.sourceId, GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID)
            || typedValuesEqual(link.targetId, GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID)
        ) {
            throw graphPatchError(
                "invalid_scoped_boundary_direction",
                "A scoped link uses a virtual boundary in the wrong direction.",
            );
        }
        if (typedValuesEqual(link.sourceId, GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID)) {
            if (link.sourceSlot >= inputLinks.length) {
                throw graphPatchError(
                    "scoped_boundary_slot_out_of_range",
                    "A scoped input-boundary link references an absent slot.",
                );
            }
            inputLinks[link.sourceSlot].push(link.id);
        }
        if (typedValuesEqual(link.targetId, GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID)) {
            if (link.targetSlot >= outputLinks.length) {
                throw graphPatchError(
                    "scoped_boundary_slot_out_of_range",
                    "A scoped output-boundary link references an absent slot.",
                );
            }
            outputLinks[link.targetSlot].push(link.id);
        }
    }
    inputs.forEach((port, index) => {
        if (!Array.isArray(port?.linkIds)) {
            throw graphPatchError("scoped_boundary_bookkeeping_mismatch", `inputs[${index}].linkIds is absent.`);
        }
        assertSameMultiset(port.linkIds, inputLinks[index], `inputs[${index}]`);
    });
    outputs.forEach((port, index) => {
        if (!Array.isArray(port?.linkIds)) {
            throw graphPatchError("scoped_boundary_bookkeeping_mismatch", `outputs[${index}].linkIds is absent.`);
        }
        assertSameMultiset(port.linkIds, outputLinks[index], `outputs[${index}]`);
    });
}


async function attestScopeAuthority(rootSnapshot, request, expectedDefinitionHash) {
    attestSharedDefinitionCounters(rootSnapshot, "current workflow");
    const { definitions, definition } = resolveSerializedScope(rootSnapshot, request.scope);
    const actualHash = await workflowScopeDefinitionHash(definition);
    if (actualHash !== expectedDefinitionHash) {
        throw graphPatchError(
            "scoped_definition_precondition_failed",
            "The selected subgraph definition hash no longer matches the scoped patch.",
            { expected: expectedDefinitionHash, actual: actualHash },
        );
    }
    for (const [kind, identity] of publicBoundaryRefs(request.public_plan)) {
        boundaryPort(definition, kind, identity);
    }
    const affected = enumerateScopeInstances(rootSnapshot, request.scope.definition_id, definitions);
    assertAffectedScopePaths(request.scope, affected);
    attestDefinitionBoundaryBookkeeping(definition);
    for (const { id } of payloadNodes(definition, "definition")) {
        if ([GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID, GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID].includes(id)) {
            throw graphPatchError(
                "scoped_runtime_id_collision",
                "The definition uses an ID reserved by the private scoped adapter.",
            );
        }
    }
    return { definition: clone(definition), definition_hash: actualHash };
}


function exactRootSharedCounter(root, legacyName, stateName, label) {
    const values = [];
    if (Object.prototype.hasOwnProperty.call(root || {}, legacyName)) {
        values.push(root[legacyName]);
    }
    if (isRecord(root?.state) && Object.prototype.hasOwnProperty.call(root.state, stateName)) {
        values.push(root.state[stateName]);
    }
    if (
        values.length === 0
        || values.some(value => !Number.isSafeInteger(value) || value < 0)
        || values.slice(1).some(value => !typedValuesEqual(value, values[0]))
    ) {
        throw graphPatchError(
            "scoped_shared_counter_mismatch",
            `The ${label} does not expose one exact shared ${stateName} counter.`,
        );
    }
    return values[0];
}


function attestSharedDefinitionCounters(root, label) {
    const lastNodeId = exactRootSharedCounter(root, "last_node_id", "lastNodeId", label);
    const lastLinkId = exactRootSharedCounter(root, "last_link_id", "lastLinkId", label);
    const definitions = collectionEntries(
        root?.definitions?.subgraphs ?? [],
        `${label}.definitions.subgraphs`,
    );
    for (const [index, definition] of definitions) {
        if (
            !isRecord(definition?.state)
            || !typedValuesEqual(definition.state.lastNodeId, lastNodeId)
            || !typedValuesEqual(definition.state.lastLinkId, lastLinkId)
        ) {
            throw graphPatchError(
                "scoped_shared_counter_mismatch",
                `Definition ${String(definition?.id ?? index)} diverges from the root shared counters.`,
            );
        }
    }
    return { lastNodeId, lastLinkId };
}


function withoutSharedRootCounters(value) {
    const detached = clone(value);
    if (isRecord(detached)) {
        delete detached.last_node_id;
        delete detached.last_link_id;
        delete detached.revision;
        if (isRecord(detached.state)) {
            delete detached.state.lastNodeId;
            delete detached.state.lastLinkId;
        }
        for (const [, definition] of collectionEntries(
            detached?.definitions?.subgraphs ?? [],
            "workflow.definitions.subgraphs",
        )) {
            if (!isRecord(definition?.state)) continue;
            delete definition.state.lastNodeId;
            delete definition.state.lastLinkId;
        }
    }
    return detached;
}


function replaceDefinitionExact(rootSnapshot, definitionId, replacement) {
    const detached = clone(rootSnapshot);
    const raw = detached?.definitions?.subgraphs;
    let matches = 0;
    if (Array.isArray(raw)) {
        detached.definitions.subgraphs = raw.map(definition => {
            if (definition?.id !== definitionId) return definition;
            matches += 1;
            return clone(replacement);
        });
    } else if (isRecord(raw)) {
        for (const [key, definition] of Object.entries(raw)) {
            const id = definition?.id ?? key;
            if (id !== definitionId) continue;
            matches += 1;
            raw[key] = clone(replacement);
        }
    }
    if (matches !== 1) {
        throw graphPatchError(
            "scoped_definition_ambiguous",
            `Expected one root definition ${definitionId}; found ${matches}.`,
        );
    }
    return detached;
}


function definitionEnvelope(definition) {
    const detached = clone(definition);
    delete detached.nodes;
    delete detached.links;
    delete detached.revision;
    if (isRecord(detached.state)) {
        delete detached.state.lastNodeId;
        delete detached.state.lastLinkId;
    }
    for (const field of ["inputs", "outputs"]) {
        for (const [, port] of collectionEntries(detached[field] ?? [], `definition.${field}`)) {
            if (isRecord(port)) delete port.linkIds;
        }
    }
    return detached;
}


function assertScopedRootPreserved(beforeRoot, afterRoot, definitionId, beforeDefinition, afterDefinition) {
    attestSharedDefinitionCounters(beforeRoot, "baseline workflow");
    attestSharedDefinitionCounters(afterRoot, "refined workflow");
    if (!valuesEqual(definitionEnvelope(beforeDefinition), definitionEnvelope(afterDefinition))) {
        throw graphPatchError(
            "scoped_definition_envelope_changed",
            "The scoped edit changed undeclared definition metadata, ports, groups, reroutes, or extra fields.",
        );
    }
    const outsideBefore = withoutSharedRootCounters(beforeRoot);
    const outsideAfter = withoutSharedRootCounters(
        replaceDefinitionExact(afterRoot, definitionId, beforeDefinition),
    );
    if (!valuesEqual(outsideBefore, outsideAfter)) {
        throw graphPatchError(
            "scoped_root_preservation_failed",
            "The scoped edit changed the root workflow outside its selected definition.",
        );
    }
}


function validateUnique(values, keyFunction, code, message) {
    const seen = new Set();
    for (const value of values) {
        const key = keyFunction(value);
        if (seen.has(key)) throw graphPatchError(code, message);
        seen.add(key);
    }
}


function collectRefs(plan) {
    const refs = [];
    const addEdgeRefs = edge => refs.push(edge.source.ref, edge.target.ref);
    for (const edge of [
        ...plan.assertions.edges,
        ...plan.remove_edges,
        ...plan.add_edges,
    ]) addEdgeRefs(edge);
    for (const item of [...plan.update_nodes, ...plan.remove_nodes, ...plan.attachments]) {
        refs.push(item.ref);
    }
    for (const removal of plan.remove_nodes) {
        for (const edge of removal.expected_incident_edges) addEdgeRefs(edge);
    }
    return refs;
}


function validateNormalizedPlan(plan) {
    validateUnique(
        plan.assertions.nodes,
        item => idKey(item.ref.node_id),
        "duplicate_graph_patch_assertion",
        "Existing node assertions must be unique.",
    );
    validateUnique(
        plan.create_nodes,
        item => item.alias,
        "duplicate_graph_patch_alias",
        "Created-node aliases must be unique.",
    );
    validateUnique(
        plan.update_nodes,
        item => idKey(item.ref.node_id),
        "duplicate_graph_patch_update",
        "Node updates must be unique.",
    );
    validateUnique(
        plan.remove_nodes,
        item => idKey(item.ref.node_id),
        "duplicate_graph_patch_removal",
        "Node removals must be unique.",
    );
    validateUnique(
        plan.assertions.edges,
        symbolicEdgeKey,
        "duplicate_graph_patch_edge",
        "Asserted edges must be unique.",
    );
    validateUnique(
        plan.remove_edges,
        symbolicEdgeKey,
        "duplicate_graph_patch_edge",
        "Removed edges must be unique.",
    );
    validateUnique(
        plan.add_edges,
        symbolicEdgeKey,
        "duplicate_graph_patch_edge",
        "Added edges must be unique.",
    );
    validateUnique(
        plan.add_edges,
        edge => symbolicTargetKey(edge.target),
        "graph_patch_target_occupied",
        "A patch cannot assign one target input more than once.",
    );

    const aliases = new Set(plan.create_nodes.map(item => item.alias));
    const assertions = new Set(plan.assertions.nodes.map(item => idKey(item.ref.node_id)));
    for (const ref of collectRefs(plan)) {
        if (Object.prototype.hasOwnProperty.call(ref, "alias") && !aliases.has(ref.alias)) {
            throw graphPatchError(
                "unknown_graph_patch_alias",
                `Graph patch references undeclared alias ${ref.alias}.`,
            );
        }
        if (
            Object.prototype.hasOwnProperty.call(ref, "node_id")
            && !assertions.has(idKey(ref.node_id))
        ) {
            throw graphPatchError(
                "missing_graph_patch_assertion",
                `Touched existing node ${ref.node_id} has no exact assertion.`,
            );
        }
    }

    const removedIds = new Set(plan.remove_nodes.map(item => idKey(item.ref.node_id)));
    const updatedIds = new Set(plan.update_nodes.map(item => idKey(item.ref.node_id)));
    if ([...removedIds].some(id => updatedIds.has(id))) {
        throw graphPatchError(
            "conflicting_graph_patch_node_operation",
            "A node cannot be updated and removed in the same patch.",
        );
    }
    for (const edge of plan.add_edges) {
        for (const ref of [edge.source.ref, edge.target.ref]) {
            if (
                Object.prototype.hasOwnProperty.call(ref, "node_id")
                && removedIds.has(idKey(ref.node_id))
            ) {
                throw graphPatchError(
                    "graph_patch_edge_touches_removed_node",
                    "An added edge cannot touch a removed node.",
                );
            }
        }
    }
    for (const edge of [...plan.assertions.edges, ...plan.remove_edges]) {
        if (edge.target.mode !== "slot") {
            throw graphPatchError(
                "invalid_graph_patch",
                "Existing asserted and removed edges must target an existing socket.",
            );
        }
        if (
            Object.prototype.hasOwnProperty.call(edge.source.ref, "alias")
            || Object.prototype.hasOwnProperty.call(edge.target.ref, "alias")
        ) {
            throw graphPatchError(
                "invalid_graph_patch",
                "Asserted and removed baseline edges must reference existing nodes.",
            );
        }
    }
    for (const removal of plan.remove_nodes) {
        for (const edge of removal.expected_incident_edges) {
            if (
                edge.target.mode !== "slot"
                || Object.prototype.hasOwnProperty.call(edge.source.ref, "alias")
                || Object.prototype.hasOwnProperty.call(edge.target.ref, "alias")
            ) {
                throw graphPatchError(
                    "invalid_graph_patch",
                    "Expected incident edges must be existing socket-to-socket edges.",
                );
            }
        }
    }
}


function validateAdapter(adapter, plan, includeWorkflowCommit = true) {
    const required = [
        "captureWorkflow",
        "getNode",
        "listConnections",
        "createNode",
        "setNodeValuesExact",
        "setNodeMetadata",
        "disconnectConnection",
        "connectNodes",
        "removeNodes",
    ];
    if (includeWorkflowCommit) required.push("restoreWorkflow", "setWorkflowExtra");
    if (plan.add_edges.some(edge => edge.target.mode === "convert_widget")) {
        required.push("convertWidgetToInput");
    }
    const existingLayoutMayBeTouched = (
        plan.update_nodes.length > 0
        || plan.attachments.some(item => Object.prototype.hasOwnProperty.call(item.ref, "node_id"))
        || [...plan.add_edges, ...plan.remove_edges].some(edge => (
            Object.prototype.hasOwnProperty.call(edge.source.ref, "node_id")
            || Object.prototype.hasOwnProperty.call(edge.target.ref, "node_id")
        ))
    );
    if (
        existingLayoutMayBeTouched
        || [...plan.create_nodes, ...plan.update_nodes].some(item => item.layout_hint)
    ) required.push("setNodeLayoutExact");
    if (plan.attachments.length > 0) {
        required.push("assignAttachmentExact", "verifyAttachmentExact");
    }
    const missing = required.filter(name => typeof adapter?.[name] !== "function");
    if (missing.length > 0) {
        throw graphPatchError(
            "invalid_graph_patch_adapter",
            `Graph patch adapter is missing: ${missing.join(", ")}.`,
        );
    }
}


function validateRootAdapter(adapter, scoped) {
    const required = ["captureWorkflow", "restoreWorkflow", "setWorkflowExtra"];
    if (scoped) required.push("resolveScopedGraph");
    const missing = required.filter(name => typeof adapter?.[name] !== "function");
    if (missing.length > 0) {
        throw graphPatchError(
            "invalid_graph_patch_adapter",
            `Graph patch root adapter is missing: ${missing.join(", ")}.`,
        );
    }
}


async function withReadGuard(adapter, operation) {
    if (typeof adapter.withReadGuard === "function") {
        return await adapter.withReadGuard(operation);
    }
    return await operation();
}


function schemaHashOf(node) {
    if (typeof node?.schema_hash === "string") return node.schema_hash;
    const properties = node?.properties || node?.serialized_node?.properties || {};
    for (const key of [
        WORKFLOW_GRAPH_PATCH_PROPERTY,
        "fl_mcp_workflow_refinement",
        "fl_mcp_workflow_application",
    ]) {
        if (typeof properties?.[key]?.schema_hash === "string") {
            return properties[key].schema_hash;
        }
    }
    return null;
}


function orderedManifest(value) {
    if (Array.isArray(value)) {
        return value.map((item, index) => ({ ...clone(item), index: item?.index ?? index }));
    }
    if (!isRecord(value)) return [];
    return Object.entries(value).map(([name, item], index) => ({
        ...(isRecord(item) ? clone(item) : {}),
        name: item?.name ?? name,
        index: item?.index ?? index,
    }));
}


function nodeSource(node) {
    return isRecord(node?.serialized_node) ? node.serialized_node : node;
}


function nodeOutputs(node) {
    const source = nodeSource(node);
    return orderedManifest(node?.outputs ?? source?.outputs);
}


function liveNodeInputs(node) {
    const source = nodeSource(node);
    return orderedManifest(node?.live_inputs ?? node?.inputs ?? source?.inputs)
        .map((input, socketIndex) => ({ ...input, socket_index: input.socket_index ?? socketIndex }));
}


function schemaNodeInputs(node) {
    const source = nodeSource(node);
    const explicit = node?.schema_inputs ?? source?.schema_inputs;
    if (explicit !== undefined) return orderedManifest(explicit);
    return liveNodeInputs(node).map(input => ({
        name: input.name,
        type: input.type,
        index: input.schema_index ?? input.index,
        kind: "socket",
    }));
}


function nodeWidgets(node) {
    const source = nodeSource(node);
    return orderedManifest(node?.widgets ?? source?.widgets).map((widget, index) => ({
        ...widget,
        widget_index: widget.widget_index ?? index,
    }));
}


function manifestAcceptsType(item, expectedType) {
    return Boolean(
        item?.type === expectedType
        || item?.input_type === expectedType
        || item?.resolved_type === expectedType
        || (
            Array.isArray(item?.accepted_types)
            && item.accepted_types.includes(expectedType)
        )
    );
}


const TRUSTED_POLYMORPHIC_INPUT_TYPES = new Set([
    "*",
    "COMFY_MATCHTYPE_V3",
]);


function liveTypeMarkerAccepts(marker, expectedType, allowPolymorphic) {
    if (typeof marker !== "string" || marker.length === 0) return null;
    if (marker === expectedType) return true;
    if (TRUSTED_POLYMORPHIC_INPUT_TYPES.has(marker)) return allowPolymorphic;
    if (!marker.includes(",")) return false;
    const tokens = marker.split(",");
    if (
        tokens.length < 2
        || tokens.some(token => !/^[A-Z][A-Z0-9_]*$/.test(token))
    ) return false;
    return tokens.includes(expectedType);
}


function liveManifestAcceptsDynamicType(item, expectedType, schemaInput) {
    const schemaPinsConcreteType = schemaInput?.type === expectedType;
    for (const marker of [item?.type, item?.input_type]) {
        const accepted = liveTypeMarkerAccepts(
            marker,
            expectedType,
            schemaPinsConcreteType,
        );
        if (accepted !== null) return accepted;
    }
    if (!Array.isArray(item?.accepted_types)) return false;
    return item.accepted_types.some(marker => (
        liveTypeMarkerAccepts(marker, expectedType, schemaPinsConcreteType) === true
    ));
}


function isDynamicTargetEndpoint(node, endpoint) {
    const source = nodeSource(node);
    const roots = node?.dynamic_input_roots ?? source?.dynamic_input_roots;
    return Array.isArray(roots) && roots.some(root => (
        typeof root === "string"
        && root.length > 0
        && (
            endpoint.input === root
            || endpoint.input.startsWith(`${root}.`)
        )
    ));
}


function exactDynamicTargetInput(node, endpoint, schemaInput) {
    const matches = liveNodeInputs(node).filter(item => item.name === endpoint.input);
    if (matches.length === 0) {
        return { ready: false, reason: "dynamic_target_socket_missing" };
    }
    if (matches.length > 1) {
        throw graphPatchError(
            "graph_patch_slot_mismatch",
            `Dynamic input ${endpoint.input} is ambiguous across ${matches.length} live sockets.`,
        );
    }
    const [input] = matches;
    if (!liveManifestAcceptsDynamicType(input, endpoint.type, schemaInput)) {
        throw graphPatchError(
            "graph_patch_slot_mismatch",
            `Dynamic input ${endpoint.input} at live socket ${input.socket_index} does not accept ${endpoint.type}.`,
        );
    }
    return { ready: true, input, socket_index: input.socket_index };
}


function exactOutput(node, endpoint) {
    const output = nodeOutputs(node).find(item => item.index === endpoint.output_index);
    if (!output) return { ready: false, reason: "source_output_missing" };
    if (output.name !== endpoint.output || !manifestAcceptsType(output, endpoint.type)) {
        throw graphPatchError(
            "graph_patch_slot_mismatch",
            `Output ${endpoint.output_index} does not match ${endpoint.output}:${endpoint.type}.`,
        );
    }
    return { ready: true, output };
}


function schemaInputOccurrence(inputs, endpoint) {
    const sameName = inputs.filter(item => item.name === endpoint.input);
    const explicitlyIndexed = sameName.filter(item => isIndex(item.occurrence_index));
    const selected = explicitlyIndexed.length > 0
        ? explicitlyIndexed.find(item => item.occurrence_index === endpoint.occurrence_index) || null
        : sameName[endpoint.occurrence_index] || null;
    if (!selected) return null;
    if (selected.index !== endpoint.input_index || !manifestAcceptsType(selected, endpoint.type)) {
        throw graphPatchError(
            "graph_patch_slot_mismatch",
            `Input ${endpoint.input} occurrence ${endpoint.occurrence_index} no longer matches schema index ${endpoint.input_index} and type ${endpoint.type}.`,
        );
    }
    return selected;
}


function convertedRetryTargetInput(node, endpoint) {
    const candidates = liveNodeInputs(node).filter(input => (
        input.name === endpoint.input
        && manifestAcceptsType(input, endpoint.type)
    ));
    if (candidates.length === 0) return null;

    const identities = candidates.map(input => {
        const schemaIndices = [input.schema_index, input.input_index]
            .filter(value => value !== undefined && value !== null);
        const schemaIndexValid = (
            schemaIndices.every(isIndex)
            && new Set(schemaIndices).size <= 1
        );
        const occurrenceAvailable = (
            input.occurrence_index !== undefined
            && input.occurrence_index !== null
        );
        return {
            input,
            schema_index: schemaIndices.length > 0 && schemaIndexValid
                ? schemaIndices[0]
                : null,
            schema_index_available: schemaIndices.length > 0,
            schema_index_valid: schemaIndexValid,
            occurrence_index: occurrenceAvailable ? input.occurrence_index : null,
            occurrence_available: occurrenceAvailable,
            occurrence_valid: !occurrenceAvailable || isIndex(input.occurrence_index),
        };
    });
    if (identities.some(identity => (
        !identity.schema_index_valid || !identity.occurrence_valid
    ))) return null;

    if (identities.length === 1) {
        const [identity] = identities;
        if (
            identity.schema_index_available
            && identity.schema_index !== endpoint.input_index
        ) return null;
        if (
            identity.occurrence_available
            && identity.occurrence_index !== endpoint.occurrence_index
        ) return null;
        return identity.input;
    }

    // Duplicate live sockets are safe only when every candidate carries enough
    // schema identity to rule it in or out. An unlabelled duplicate could be the
    // converted endpoint, so never infer its occurrence from live ordering.
    if (identities.some(identity => !identity.schema_index_available)) return null;
    const matchingSchemaIndex = identities.filter(identity => (
        identity.schema_index === endpoint.input_index
    ));
    if (matchingSchemaIndex.length === 1) {
        const [identity] = matchingSchemaIndex;
        if (
            identity.occurrence_available
            && identity.occurrence_index !== endpoint.occurrence_index
        ) return null;
        return identity.input;
    }
    if (
        matchingSchemaIndex.length === 0
        || matchingSchemaIndex.some(identity => !identity.occurrence_available)
    ) return null;
    const matchingOccurrence = matchingSchemaIndex.filter(identity => (
        identity.occurrence_index === endpoint.occurrence_index
    ));
    return matchingOccurrence.length === 1 ? matchingOccurrence[0].input : null;
}


async function resolveAppliedPostconditionEdge(edge, aliases, adapter) {
    if (edge.target.mode !== "convert_widget") {
        return await resolveRuntimeEdge(edge, aliases, adapter);
    }
    const sourceId = resolveRef(edge.source.ref, aliases);
    const targetId = resolveRef(edge.target.ref, aliases);
    if (sourceId === undefined || targetId === undefined) {
        return { ready: false, reason: "node_ref_unresolved" };
    }
    const sourceNode = await adapter.getNode(sourceId);
    const targetNode = await adapter.getNode(targetId);
    if (!sourceNode || !targetNode) return { ready: false, reason: "node_missing" };
    const sourceState = exactOutput(sourceNode, edge.source);
    if (!sourceState.ready) return sourceState;
    const schemaInput = schemaInputOccurrence(schemaNodeInputs(targetNode), edge.target);
    if (!schemaInput) return { ready: false, reason: "target_schema_input_missing" };
    const input = convertedRetryTargetInput(targetNode, edge.target);
    if (!input) return { ready: false, reason: "converted_target_ambiguous" };
    return {
        ready: true,
        sourceId,
        targetId,
        sourceNode,
        targetNode,
        runtime: {
            source_node_id: sourceId,
            source_output_index: edge.source.output_index,
            source_output: edge.source.output,
            target_node_id: targetId,
            target_input_index: input.socket_index,
            target_input: edge.target.input,
            type: edge.source.type,
        },
    };
}


function exactTargetState(node, endpoint, convertedSocketIndex = null) {
    const schemaInput = schemaInputOccurrence(schemaNodeInputs(node), endpoint);
    if (!schemaInput) return { ready: false, reason: "target_schema_input_missing" };
    if (endpoint.mode === "slot") {
        if (isDynamicTargetEndpoint(node, endpoint)) {
            const dynamic = exactDynamicTargetInput(node, endpoint, schemaInput);
            return { ...dynamic, schemaInput };
        }
        const input = liveNodeInputs(node).find(item => item.socket_index === endpoint.socket_index);
        if (!input) return { ready: false, reason: "target_socket_missing" };
        if (input.name !== endpoint.input || !manifestAcceptsType(input, endpoint.type)) {
            throw graphPatchError(
                "graph_patch_slot_mismatch",
                `Socket ${endpoint.socket_index} does not match ${endpoint.input}:${endpoint.type}.`,
            );
        }
        return { ready: true, schemaInput, input, socket_index: endpoint.socket_index };
    }

    if (convertedSocketIndex !== null) {
        const input = liveNodeInputs(node).find(item => item.socket_index === convertedSocketIndex);
        if (!input) return { ready: false, reason: "converted_socket_missing" };
        if (input.name !== endpoint.input || !manifestAcceptsType(input, endpoint.type)) {
            throw graphPatchError(
                "graph_patch_slot_mismatch",
                `Converted socket ${convertedSocketIndex} does not match ${endpoint.input}:${endpoint.type}.`,
            );
        }
        return { ready: true, schemaInput, input, socket_index: convertedSocketIndex };
    }

    const matchingWidgets = nodeWidgets(node).filter(widget => widget.name === endpoint.input);
    const widget = matchingWidgets.find(item => (
        (item.schema_index ?? item.input_index) === endpoint.input_index
        && (
            item.occurrence_index === undefined
            || item.occurrence_index === endpoint.occurrence_index
        )
        && manifestAcceptsType(item, endpoint.type)
    )) || (
        matchingWidgets.length === 1
        && endpoint.occurrence_index === 0
        && matchingWidgets[0].schema_index === undefined
        && matchingWidgets[0].input_index === undefined
        && manifestAcceptsType(matchingWidgets[0], endpoint.type)
            ? matchingWidgets[0]
            : null
    );
    if (!widget) return { ready: false, reason: "convertible_widget_missing", schemaInput };
    return { ready: false, convertible: true, schemaInput, widget };
}


function exactAttachmentState(node, attachment) {
    const matches = schemaNodeInputs(node).filter(item => (
        item.name === attachment.input
        && item.index === attachment.input_index
        && manifestAcceptsType(item, attachment.type)
    ));
    if (matches.length !== 1) {
        return { ready: false, reason: "attachment_schema_input_missing" };
    }
    const input = matches[0];
    if (input.kind !== "widget" && input.widget !== true) {
        return { ready: false, reason: "attachment_target_not_widget" };
    }
    return { ready: true, input };
}


function normalizeObservedConnection(value) {
    if (!isRecord(value)) return null;
    const connection = {
        source_node_id: value.source_node_id,
        source_output_index: value.source_output_index,
        source_output: value.source_output ?? null,
        target_node_id: value.target_node_id,
        target_input_index: value.target_input_index,
        target_input: value.target_input ?? null,
        type: value.type ?? null,
    };
    if (
        connection.source_node_id === undefined
        || connection.source_node_id === null
        || connection.target_node_id === undefined
        || connection.target_node_id === null
        || !isIndex(connection.source_output_index)
        || !isIndex(connection.target_input_index)
    ) return null;
    return connection;
}


function observedConnectionKey(connection) {
    return [
        idKey(connection.source_node_id),
        connection.source_output_index,
        idKey(connection.target_node_id),
        connection.target_input_index,
    ].join("|");
}


function observedExactKey(connection) {
    return [
        observedConnectionKey(connection),
        connection.source_output ?? "",
        connection.target_input ?? "",
        connection.type ?? "",
    ].join("|");
}


function observedTypedExactKey(connection) {
    return JSON.stringify([
        typeof connection.source_node_id,
        connection.source_node_id,
        connection.source_output_index,
        typeof connection.target_node_id,
        connection.target_node_id,
        connection.target_input_index,
        connection.source_output ?? "",
        connection.target_input ?? "",
        connection.type ?? "",
    ]);
}


function orderedRuntimeRemovals(connections) {
    return [...connections].sort((left, right) => {
        const leftTarget = idKey(left.target_node_id);
        const rightTarget = idKey(right.target_node_id);
        if (leftTarget !== rightTarget) return leftTarget < rightTarget ? -1 : 1;
        if (left.target_input_index !== right.target_input_index) {
            // Removing the highest live socket first prevents AUTOGROW inputs
            // from compacting a later removal out from under its exact index.
            return right.target_input_index - left.target_input_index;
        }
        return observedExactKey(left).localeCompare(observedExactKey(right));
    });
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
    return [...left.entries()].every(([key, value]) => right.get(key) === value);
}


function resolveRef(ref, aliases) {
    if (Object.prototype.hasOwnProperty.call(ref, "node_id")) return ref.node_id;
    return aliases[ref.alias];
}


async function resolveRuntimeEdge(edge, aliases, adapter, conversionSockets = new Map()) {
    const sourceId = resolveRef(edge.source.ref, aliases);
    const targetId = resolveRef(edge.target.ref, aliases);
    if (sourceId === undefined || targetId === undefined) {
        return { ready: false, reason: "node_ref_unresolved" };
    }
    const sourceNode = await adapter.getNode(sourceId);
    const targetNode = await adapter.getNode(targetId);
    if (!sourceNode || !targetNode) return { ready: false, reason: "node_missing" };
    const sourceState = exactOutput(sourceNode, edge.source);
    if (!sourceState.ready) return sourceState;
    const conversionKey = symbolicTargetKey(edge.target);
    const targetState = exactTargetState(
        targetNode,
        edge.target,
        conversionSockets.get(conversionKey) ?? null,
    );
    if (!targetState.ready) return { ...targetState, sourceId, targetId, sourceNode, targetNode };
    return {
        ready: true,
        sourceId,
        targetId,
        sourceNode,
        targetNode,
        runtime: {
            source_node_id: sourceId,
            source_output_index: edge.source.output_index,
            source_output: edge.source.output,
            target_node_id: targetId,
            target_input_index: targetState.socket_index,
            target_input: edge.target.input,
            type: edge.source.type,
        },
    };
}


function connectionDetailsMatch(expected, observed) {
    return (
        nodeIdsEqual(expected.source_node_id, observed.source_node_id)
        && expected.source_output_index === observed.source_output_index
        && nodeIdsEqual(expected.target_node_id, observed.target_node_id)
        && expected.target_input_index === observed.target_input_index
        && expected.source_output === observed.source_output
        && expected.target_input === observed.target_input
        && expected.type === observed.type
    );
}


function typedConnectionDetailsMatch(expected, observed) {
    return (
        typedValuesEqual(expected.source_node_id, observed.source_node_id)
        && expected.source_output_index === observed.source_output_index
        && typedValuesEqual(expected.target_node_id, observed.target_node_id)
        && expected.target_input_index === observed.target_input_index
        && expected.source_output === observed.source_output
        && expected.target_input === observed.target_input
        && expected.type === observed.type
    );
}


async function resolveExistingEdge(edge, adapter) {
    const result = await resolveRuntimeEdge(edge, {}, adapter);
    if (!result.ready) {
        throw graphPatchError(
            "graph_patch_assertion_failed",
            `Existing edge endpoint is unavailable (${result.reason}).`,
            { edge },
        );
    }
    return result.runtime;
}


function nodeCount(snapshot) {
    return Array.isArray(snapshot?.nodes) ? snapshot.nodes.length : 0;
}


function snapshotNodeIds(snapshot) {
    return new Set(
        (Array.isArray(snapshot?.nodes) ? snapshot.nodes : [])
            .map(nodeId)
            .filter(value => value !== undefined && value !== null)
            .map(idKey),
    );
}


function assertUnambiguousSnapshotNodeIds(snapshot) {
    const seen = new Map();
    for (const node of Array.isArray(snapshot?.nodes) ? snapshot.nodes : []) {
        const id = nodeId(node);
        if (id === undefined || id === null) continue;
        const key = idKey(id);
        if (seen.has(key)) {
            throw graphPatchError(
                "ambiguous_node_identity",
                `The active workflow contains colliding node IDs for ${key}.`,
                { first: seen.get(key), second: id },
            );
        }
        seen.set(key, id);
    }
}


function snapshotNodeMap(snapshot) {
    const result = new Map();
    for (const node of Array.isArray(snapshot?.nodes) ? snapshot.nodes : []) {
        const id = nodeId(node);
        if (id !== undefined && id !== null) result.set(idKey(id), clone(node));
    }
    return result;
}


function comparableExtra(extra) {
    if (!isRecord(extra)) return extra ?? {};
    return Object.fromEntries(
        Object.entries(extra)
            .filter(([key]) => (
                key !== "ds"
                && key !== GRAPH_PATCH_LEDGER_KEY
            ))
            .map(([key, value]) => [key, clone(value)]),
    );
}


function comparableState(state) {
    if (!isRecord(state)) return state ?? {};
    return Object.fromEntries(
        Object.entries(state)
            .filter(([key]) => key !== "lastNodeId" && key !== "lastLinkId")
            .map(([key, value]) => [key, clone(value)]),
    );
}


function changedEnvelopeFields(beforeSnapshot, afterSnapshot) {
    const keys = new Set([
        ...Object.keys(isRecord(beforeSnapshot) ? beforeSnapshot : {}),
        ...Object.keys(isRecord(afterSnapshot) ? afterSnapshot : {}),
    ]);
    const changed = [];
    for (const key of keys) {
        if (WORKFLOW_OWNED_FIELDS.has(key)) continue;
        const before = key === "extra"
            ? comparableExtra(beforeSnapshot?.extra)
            : key === "state"
                ? comparableState(beforeSnapshot?.state)
                : beforeSnapshot?.[key];
        const after = key === "extra"
            ? comparableExtra(afterSnapshot?.extra)
            : key === "state"
                ? comparableState(afterSnapshot?.state)
                : afterSnapshot?.[key];
        if (!valuesEqual(before, after)) changed.push(key);
    }
    return changed.sort();
}


function nodePosition(node) {
    if (Array.isArray(node?.pos)) return { x: node.pos[0], y: node.pos[1] };
    return node?.position ? clone(node.position) : null;
}


function nodeSize(node) {
    if (Array.isArray(node?.size)) return { width: node.size[0], height: node.size[1] };
    return node?.size ? clone(node.size) : null;
}


function stableNodeFacts(node) {
    const source = nodeSource(node);
    if (!isRecord(source)) return {};
    return Object.fromEntries(
        Object.entries(source)
            .filter(([key]) => !NODE_IDENTITY_FIELDS.has(key))
            .map(([key, value]) => {
                if (key === "inputs" && Array.isArray(value)) {
                    return [key, value.map(item => (
                        isRecord(item)
                            ? Object.fromEntries(
                                Object.entries(item)
                                    .filter(([field]) => field !== "link")
                                    .map(([field, itemValue]) => [field, clone(itemValue)]),
                            )
                            : clone(item)
                    ))];
                }
                if (key === "outputs" && Array.isArray(value)) {
                    return [key, value.map(item => (
                        isRecord(item)
                            ? Object.fromEntries(
                                Object.entries(item)
                                    .filter(([field]) => field !== "links")
                                    .map(([field, itemValue]) => [field, clone(itemValue)]),
                            )
                            : clone(item)
                    ))];
                }
                return [key, clone(value)];
            }),
    );
}


function touchedNodeStructuralFacts(node) {
    const facts = stableNodeFacts(node);
    delete facts.inputs;
    delete facts.outputs;
    delete facts.widgets;
    delete facts.widgets_values;
    return facts;
}


function itemName(value) {
    return typeof value?.name === "string" ? value.name : "";
}


function stripFields(value, fields) {
    if (!isRecord(value)) return clone(value);
    return Object.fromEntries(
        Object.entries(value)
            .filter(([key]) => !fields.has(key))
            .map(([key, itemValue]) => [key, clone(itemValue)]),
    );
}


function inputManifest(node, rules) {
    return liveNodeInputs(node)
        .filter(item => !rules.inputMayChange(item))
        .map(item => stripFields(item, new Set([
            "index",
            "link",
            "socket_index",
        ])));
}


function outputManifest(node) {
    return nodeOutputs(node).map(item => stripFields(item, new Set(["links"])));
}


function widgetManifest(node, rules) {
    return nodeWidgets(node)
        .filter(item => !rules.widgetMayAppearOrDisappear(item))
        .map(item => stripFields(
            item,
            new Set([
                "index",
                "widget_index",
                ...(rules.widgetValueMayChange(item) ? ["value"] : []),
            ]),
        ));
}


function dynamicNumberedPrefix(name) {
    const match = typeof name === "string" ? /^(.*?\D)(\d+)$/.exec(name) : null;
    return match ? match[1] : null;
}


function nameInRoot(name, root, { includeRoot = true } = {}) {
    return Boolean(
        typeof name === "string"
        && typeof root === "string"
        && (
            (includeRoot && name === root)
            || name.startsWith(`${root}.`)
        )
    );
}


function touchedManifestRules(plan, nodeIdValue, beforeNode, update, attachmentNames) {
    const id = idKey(nodeIdValue);
    const targetEdges = [...plan.add_edges, ...plan.remove_edges].filter(edge => (
        Object.prototype.hasOwnProperty.call(edge.target.ref, "node_id")
        && idKey(edge.target.ref.node_id) === id
    ));
    const conversionTargets = targetEdges
        .filter(edge => edge.target.mode === "convert_widget")
        .map(edge => edge.target);
    const numberedPrefixes = new Set(
        targetEdges
            .map(edge => dynamicNumberedPrefix(edge.target.input))
            .filter(Boolean),
    );
    const edgeWidgetRoots = new Set(targetEdges.map(edge => edge.target.input));
    const dynamicSelectorNames = new Set(
        Array.isArray(beforeNode?.dynamic_selector_names)
            ? beforeNode.dynamic_selector_names
            : [],
    );
    for (const widget of nodeWidgets(beforeNode)) {
        if (
            widget.type === "COMFY_DYNAMICCOMBO_V3"
            || widget.input_type === "COMFY_DYNAMICCOMBO_V3"
        ) dynamicSelectorNames.add(widget.name);
    }
    for (const schemaInput of schemaNodeInputs(beforeNode)) {
        if (schemaInput.type === "COMFY_DYNAMICCOMBO_V3") {
            dynamicSelectorNames.add(schemaInput.name);
        }
    }
    const updatedNames = new Set(Object.keys(update?.set_values || {}));
    const selectorRoots = new Set(
        [...updatedNames].filter(name => dynamicSelectorNames.has(name)),
    );
    const mutableWidgetNames = new Set([...updatedNames, ...attachmentNames]);
    const isConverted = item => conversionTargets.some(target => (
        itemName(item) === target.input
        && (item.schema_index ?? item.input_index ?? item.index) === target.input_index
        && (
            item.occurrence_index === undefined
            || item.occurrence_index === target.occurrence_index
        )
    ));
    const inSelectorScope = item => [...selectorRoots].some(root => (
        nameInRoot(itemName(item), root)
    ));
    return {
        inputMayChange(item) {
            const name = itemName(item);
            return (
                isConverted(item)
                || inSelectorScope(item)
                || [...numberedPrefixes].some(prefix => name.startsWith(prefix) && /^\d+$/.test(name.slice(prefix.length)))
            );
        },
        widgetMayAppearOrDisappear(item) {
            const name = itemName(item);
            return (
                isConverted(item)
                || inSelectorScope(item)
                || [...edgeWidgetRoots].some(root => nameInRoot(name, root, { includeRoot: false }))
            );
        },
        widgetValueMayChange(item) {
            return mutableWidgetNames.has(itemName(item));
        },
    };
}


function touchedNodeManifestsMatch(beforeNode, afterNode, rules) {
    return {
        structural: valuesEqual(
            touchedNodeStructuralFacts(afterNode),
            touchedNodeStructuralFacts(beforeNode),
        ),
        inputs: valuesEqual(inputManifest(afterNode, rules), inputManifest(beforeNode, rules)),
        outputs: valuesEqual(outputManifest(afterNode), outputManifest(beforeNode)),
        widgets: valuesEqual(widgetManifest(afterNode, rules), widgetManifest(beforeNode, rules)),
    };
}


function valuesOf(node) {
    return clone(node?.values || {});
}


function valuesWithoutNames(values, names) {
    const result = clone(values || {});
    for (const name of names || []) delete result[name];
    return result;
}


function updatedValuesMatch(beforeNode, afterNode, update, excludedNames = new Set()) {
    const beforeValues = valuesOf(beforeNode);
    const afterValues = valuesOf(afterNode);
    const setValues = clone(update?.set_values || {});
    const beforeWidgets = new Set(nodeWidgets(beforeNode).map(item => item.name));
    const afterWidgets = new Set(nodeWidgets(afterNode).map(item => item.name));
    for (const [name, value] of Object.entries(setValues)) {
        if (excludedNames.has(name)) continue;
        if (!Object.prototype.hasOwnProperty.call(afterValues, name) || !valuesEqual(afterValues[name], value)) {
            return false;
        }
    }
    for (const [name, value] of Object.entries(beforeValues)) {
        if (excludedNames.has(name) || Object.prototype.hasOwnProperty.call(setValues, name)) continue;
        if (!Object.prototype.hasOwnProperty.call(afterValues, name)) {
            if (beforeWidgets.has(name) && !afterWidgets.has(name)) continue;
            return false;
        }
        if (!valuesEqual(afterValues[name], value)) return false;
    }
    for (const name of Object.keys(afterValues)) {
        if (
            excludedNames.has(name)
            || Object.prototype.hasOwnProperty.call(beforeValues, name)
            || Object.prototype.hasOwnProperty.call(setValues, name)
        ) continue;
        if (!afterWidgets.has(name) || beforeWidgets.has(name)) return false;
    }
    return true;
}


function assertValues(observed, expected, label) {
    const values = valuesOf(observed);
    for (const [name, value] of Object.entries(expected || {})) {
        if (!Object.prototype.hasOwnProperty.call(values, name) || !valuesEqual(values[name], value)) {
            throw graphPatchError(
                "graph_patch_value_mismatch",
                `${label}.${name} does not match its exact expected value.`,
            );
        }
    }
}


async function contentGraphHash(snapshot) {
    const detached = clone(snapshot);
    if (isRecord(detached.extra)) delete detached.extra[GRAPH_PATCH_LEDGER_KEY];
    return await workflowGraphHash(detached);
}


function emptyLedger() {
    return { schema: WORKFLOW_GRAPH_PATCH_SCHEMA, order: [], entries: {} };
}


function ledgerNodeId(value) {
    return (
        (typeof value === "number" && Number.isInteger(value))
        || (
            typeof value === "string"
            && value.length > 0
            && value.length <= LEDGER_NODE_ID_STRING_LIMIT
        )
    );
}


function assertLedgerEntry(entry, applicationId) {
    if (!isRecord(entry)) {
        throw graphPatchError("invalid_graph_patch_ledger", "A GraphPatch ledger entry is malformed.");
    }
    const fields = Object.keys(entry).sort();
    const v2Fields = [
        "aliases", "created_node_ids", "patch_hash", "removed_node_ids", "result_content_hash",
    ].sort();
    const v3Fields = [...v2Fields, "result_definition_hash"].sort();
    if (!valuesEqual(fields, v2Fields) && !valuesEqual(fields, v3Fields)) {
        throw graphPatchError(
            "invalid_graph_patch_ledger",
            `GraphPatch ledger entry ${applicationId} has undeclared fields.`,
        );
    }
    if (
        !SHA256_PATTERN.test(String(entry.patch_hash || ""))
        || !SHA256_PATTERN.test(String(entry.result_content_hash || ""))
        || (
            Object.prototype.hasOwnProperty.call(entry, "result_definition_hash")
            && !SHA256_PATTERN.test(String(entry.result_definition_hash || ""))
        )
        || !isRecord(entry.aliases)
        || !Array.isArray(entry.created_node_ids)
        || !Array.isArray(entry.removed_node_ids)
        || Object.keys(entry.aliases).length > LEDGER_ENTRY_NODE_ID_LIMIT
        || entry.created_node_ids.length > LEDGER_ENTRY_NODE_ID_LIMIT
        || entry.removed_node_ids.length > LEDGER_ENTRY_NODE_ID_LIMIT
    ) {
        throw graphPatchError(
            "invalid_graph_patch_ledger",
            `GraphPatch ledger entry ${applicationId} has invalid bounded facts.`,
        );
    }
    const aliasEntries = Object.entries(entry.aliases);
    if (
        aliasEntries.some(([alias, id]) => !ALIAS_PATTERN.test(alias) || !ledgerNodeId(id))
        || entry.created_node_ids.some(id => !ledgerNodeId(id))
        || entry.removed_node_ids.some(id => !ledgerNodeId(id))
        || new Set(entry.created_node_ids.map(typedIdKey)).size !== entry.created_node_ids.length
        || new Set(entry.removed_node_ids.map(typedIdKey)).size !== entry.removed_node_ids.length
    ) {
        throw graphPatchError(
            "invalid_graph_patch_ledger",
            `GraphPatch ledger entry ${applicationId} contains invalid typed IDs.`,
        );
    }
    const aliasIds = multiset(aliasEntries.map(([, id]) => id));
    const createdIds = multiset(entry.created_node_ids);
    if (
        aliasIds.size !== createdIds.size
        || [...aliasIds].some(([key, count]) => createdIds.get(key) !== count)
    ) {
        throw graphPatchError(
            "invalid_graph_patch_ledger",
            `GraphPatch ledger entry ${applicationId} aliases disagree with created IDs.`,
        );
    }
}


function readLedger(snapshot) {
    const value = snapshot?.extra?.[GRAPH_PATCH_LEDGER_KEY];
    if (value === undefined || value === null) return emptyLedger();
    try {
        assertStrictJSON(value, "workflow.extra.fl_mcp_graph_patch_ledger");
    } catch (error) {
        throw graphPatchError(
            "invalid_graph_patch_ledger",
            `GraphPatch ledger exceeds its strict JSON bounds (${String(error?.message || error)}).`,
        );
    }
    if (
        !isRecord(value)
        || value.schema !== WORKFLOW_GRAPH_PATCH_SCHEMA
        || !Array.isArray(value.order)
        || !isRecord(value.entries)
        || value.order.length > LEDGER_LIMIT
        || value.order.some(id => typeof id !== "string" || !APPLICATION_ID_PATTERN.test(id))
        || new Set(value.order).size !== value.order.length
        || Object.keys(value.entries).length !== value.order.length
        || Object.keys(value.entries).some(id => !value.order.includes(id))
    ) {
        throw graphPatchError("invalid_graph_patch_ledger", "GraphPatch ledger is malformed.");
    }
    for (const applicationId of value.order) {
        if (!Object.prototype.hasOwnProperty.call(value.entries, applicationId)) {
            throw graphPatchError("invalid_graph_patch_ledger", "GraphPatch ledger is incomplete.");
        }
        assertLedgerEntry(value.entries[applicationId], applicationId);
    }
    return clone(value);
}


function ledgerWithEntry(ledger, applicationId, entry) {
    const next = clone(ledger);
    next.order = next.order.filter(id => id !== applicationId);
    next.order.push(applicationId);
    next.entries[applicationId] = clone(entry);
    while (next.order.length > LEDGER_LIMIT) {
        const removed = next.order.shift();
        delete next.entries[removed];
    }
    return next;
}


function revealDelayMs(stepCount) {
    if (!Number.isInteger(stepCount) || stepCount <= 0) return 0;
    const budget = Math.min(12_000, Math.max(3_500, stepCount * 300));
    return Math.max(0, Math.min(700, Math.floor(budget / stepCount)));
}


function mutationStepCount(plan) {
    const conversions = plan.add_edges.filter(edge => edge.target.mode === "convert_widget").length;
    return (
        plan.create_nodes.length
        + plan.update_nodes.length
        + plan.remove_edges.length
        + plan.add_edges.length
        + plan.remove_nodes.length
        + plan.attachments.length
        + conversions
    );
}


async function reveal(adapter, step, delayMs) {
    if (typeof adapter.afterMutationStep !== "function") return;
    await adapter.afterMutationStep({ ...clone(step), delay_ms: delayMs });
}


function graphVertex(ref) {
    return refKey(ref);
}


function orderedCreatedNodes(plan) {
    const declarations = new Map(plan.create_nodes.map((item, index) => (
        [item.alias, { item, index }]
    )));
    const dependencies = new Map(plan.create_nodes.map(item => [item.alias, new Set()]));
    const dependents = new Map(plan.create_nodes.map(item => [item.alias, new Set()]));
    for (const edge of plan.add_edges) {
        const sourceAlias = edge.source.ref.alias;
        const targetAlias = edge.target.ref.alias;
        if (!sourceAlias || !targetAlias || sourceAlias === targetAlias) continue;
        if (!dependencies.get(targetAlias)?.has(sourceAlias)) {
            dependencies.get(targetAlias)?.add(sourceAlias);
            dependents.get(sourceAlias)?.add(targetAlias);
        }
    }
    const ready = plan.create_nodes
        .filter(item => dependencies.get(item.alias)?.size === 0)
        .map(item => item.alias);
    const result = [];
    while (ready.length > 0) {
        ready.sort((left, right) => declarations.get(left).index - declarations.get(right).index);
        const alias = ready.shift();
        result.push(declarations.get(alias).item);
        for (const dependent of dependents.get(alias) || []) {
            dependencies.get(dependent).delete(alias);
            if (dependencies.get(dependent).size === 0) ready.push(dependent);
        }
    }
    return result.length === plan.create_nodes.length ? result : [...plan.create_nodes];
}


function orderedAddedEdges(plan, creates) {
    const aliasOrder = new Map(creates.map((item, index) => [item.alias, index]));
    const existingSourceRank = -1;
    const existingTargetRank = creates.length;
    const rank = (ref, fallback) => (
        Object.prototype.hasOwnProperty.call(ref, "alias")
            ? aliasOrder.get(ref.alias) ?? fallback
            : fallback
    );
    return plan.add_edges
        .map((edge, index) => ({ edge, index }))
        .sort((left, right) => {
            const leftSource = rank(left.edge.source.ref, existingSourceRank);
            const rightSource = rank(right.edge.source.ref, existingSourceRank);
            if (leftSource !== rightSource) return leftSource - rightSource;
            const leftTarget = rank(left.edge.target.ref, existingTargetRank);
            const rightTarget = rank(right.edge.target.ref, existingTargetRank);
            if (leftTarget !== rightTarget) return leftTarget - rightTarget;
            if (left.edge.target.input_index !== right.edge.target.input_index) {
                return left.edge.target.input_index - right.edge.target.input_index;
            }
            return left.index - right.index;
        })
        .map(item => item.edge);
}


function findCycle(edges) {
    const adjacency = new Map();
    const indegree = new Map();
    for (const { source, target } of edges) {
        const targets = adjacency.get(source) || [];
        targets.push(target);
        adjacency.set(source, targets);
        if (!adjacency.has(target)) adjacency.set(target, []);
        if (!indegree.has(source)) indegree.set(source, 0);
        indegree.set(target, (indegree.get(target) || 0) + 1);
    }
    const ready = [...indegree.entries()]
        .filter(([, count]) => count === 0)
        .map(([vertex]) => vertex);
    let visited = 0;
    while (ready.length > 0) {
        const vertex = ready.pop();
        visited += 1;
        for (const target of adjacency.get(vertex) || []) {
            const count = indegree.get(target) - 1;
            indegree.set(target, count);
            if (count === 0) ready.push(target);
        }
    }
    return visited !== indegree.size;
}


async function preflight(request, adapter, beforeSnapshot) {
    const plan = request.plan;
    const observedConnections = (await adapter.listConnections())
        .map(normalizeObservedConnection)
        .filter(Boolean);
    const assertionMap = new Map(plan.assertions.nodes.map(item => [idKey(item.ref.node_id), item]));
    const assertedNodes = new Map();
    for (const assertion of plan.assertions.nodes) {
        const observed = await adapter.getNode(assertion.ref.node_id);
        if (!observed) {
            throw graphPatchError(
                "graph_patch_assertion_failed",
                `Asserted node ${assertion.ref.node_id} is missing.`,
            );
        }
        if (
            nodeType(observed) !== assertion.node_type
            || schemaHashOf(observed) !== assertion.schema_hash
        ) {
            throw graphPatchError(
                "graph_patch_assertion_failed",
                `Asserted node ${assertion.ref.node_id} changed type or schema.`,
            );
        }
        assertedNodes.set(idKey(assertion.ref.node_id), clone(observed));
    }
    for (const update of plan.update_nodes) {
        const observed = await adapter.getNode(update.ref.node_id);
        if (
            !observed
            || nodeType(observed) !== update.node_type
            || schemaHashOf(observed) !== update.schema_hash
        ) {
            throw graphPatchError("graph_patch_assertion_failed", "Updated node facts changed.");
        }
        assertValues(observed, update.expected_values, `update_nodes.${update.ref.node_id}`);
    }
    for (const removal of plan.remove_nodes) {
        const observed = await adapter.getNode(removal.ref.node_id);
        if (
            !observed
            || nodeType(observed) !== removal.node_type
            || schemaHashOf(observed) !== removal.schema_hash
        ) {
            throw graphPatchError("graph_patch_assertion_failed", "Removed node facts changed.");
        }
    }
    for (const attachment of plan.attachments) {
        if (!Object.prototype.hasOwnProperty.call(attachment.ref, "node_id")) continue;
        const observed = await adapter.getNode(attachment.ref.node_id);
        const state = observed ? exactAttachmentState(observed, attachment) : null;
        if (!state?.ready) {
            throw graphPatchError(
                "graph_patch_attachment_mismatch",
                `Attachment target ${attachment.ref.node_id}.${attachment.input} is unavailable (${state?.reason || "node_missing"}).`,
            );
        }
    }

    const assertedRuntime = [];
    for (const edge of plan.assertions.edges) assertedRuntime.push(await resolveExistingEdge(edge, adapter));
    for (const expected of assertedRuntime) {
        const matches = observedConnections.filter(item => connectionDetailsMatch(expected, item));
        if (matches.length !== 1) {
            throw graphPatchError(
                "graph_patch_assertion_failed",
                `Expected exactly one asserted edge; found ${matches.length}.`,
                { edge: expected },
            );
        }
    }

    const removedRuntime = [];
    for (const edge of plan.remove_edges) removedRuntime.push(await resolveExistingEdge(edge, adapter));
    for (const expected of removedRuntime) {
        const matches = observedConnections.filter(item => connectionDetailsMatch(expected, item));
        if (matches.length !== 1) {
            throw graphPatchError(
                "graph_patch_remove_edge_mismatch",
                `Expected exactly one removed edge; found ${matches.length}.`,
            );
        }
    }

    const removedKeys = new Set(removedRuntime.map(observedExactKey));
    const removedNodeIds = new Set(plan.remove_nodes.map(item => idKey(item.ref.node_id)));
    for (const removal of plan.remove_nodes) {
        const incident = observedConnections.filter(edge => (
            nodeIdsEqual(edge.source_node_id, removal.ref.node_id)
            || nodeIdsEqual(edge.target_node_id, removal.ref.node_id)
        ));
        const expectedIncident = [];
        for (const edge of removal.expected_incident_edges) {
            expectedIncident.push(await resolveExistingEdge(edge, adapter));
        }
        if (!countsEqual(
            countKeys(incident, observedExactKey),
            countKeys(expectedIncident, observedExactKey),
        )) {
            throw graphPatchError(
                "graph_patch_incident_edge_mismatch",
                `Removed node ${removal.ref.node_id} has undeclared incident edges.`,
            );
        }
        if (expectedIncident.some(edge => !removedKeys.has(observedExactKey(edge)))) {
            throw graphPatchError(
                "graph_patch_incident_edge_mismatch",
                `All incident edges of removed node ${removal.ref.node_id} must be removed.`,
            );
        }
    }

    const remainingConnections = observedConnections.filter(edge => !removedKeys.has(observedExactKey(edge)));
    const occupiedTargets = new Set(remainingConnections.map(edge => (
        `${idKey(edge.target_node_id)}|${edge.target_input_index}`
    )));
    for (const edge of plan.add_edges) {
        if (Object.prototype.hasOwnProperty.call(edge.source.ref, "node_id")) {
            const observed = await adapter.getNode(edge.source.ref.node_id);
            if (!observed) throw graphPatchError("graph_patch_assertion_failed", "Added-edge source is missing.");
            const state = exactOutput(observed, edge.source);
            if (!state.ready) {
                throw graphPatchError("graph_patch_slot_mismatch", "Added-edge source output is unavailable.");
            }
        }
        if (Object.prototype.hasOwnProperty.call(edge.target.ref, "node_id")) {
            const observed = await adapter.getNode(edge.target.ref.node_id);
            if (!observed) throw graphPatchError("graph_patch_assertion_failed", "Added-edge target is missing.");
            const state = exactTargetState(observed, edge.target);
            const pendingDynamicSocket = state.reason === "dynamic_target_socket_missing";
            if (
                !state.ready
                && !(edge.target.mode === "convert_widget" && state.convertible)
                && !pendingDynamicSocket
            ) {
                throw graphPatchError(
                    "graph_patch_slot_mismatch",
                    `Added-edge target is unavailable (${state.reason}).`,
                );
            }
            if (state.ready && edge.target.mode === "slot") {
                const key = `${idKey(edge.target.ref.node_id)}|${state.socket_index}`;
                if (occupiedTargets.has(key)) {
                    throw graphPatchError(
                        "graph_patch_target_occupied",
                        `Target socket ${key} is already connected.`,
                    );
                }
            }
        }
    }

    const baselineTopology = remainingConnections
        .filter(edge => (
            !removedNodeIds.has(idKey(edge.source_node_id))
            && !removedNodeIds.has(idKey(edge.target_node_id))
        ))
        .map(edge => ({
            source: `existing:${idKey(edge.source_node_id)}`,
            target: `existing:${idKey(edge.target_node_id)}`,
        }));
    const plannedTopology = plan.add_edges.map(edge => ({
        source: graphVertex(edge.source.ref),
        target: graphVertex(edge.target.ref),
    }));
    if (findCycle([...baselineTopology, ...plannedTopology])) {
        throw graphPatchError("graph_patch_cycle", "The final workflow graph would contain a cycle.");
    }

    const expected = plan.expected_delta;
    const actualDelta = {
        created_node_count: plan.create_nodes.length,
        updated_node_count: plan.update_nodes.length,
        removed_node_count: plan.remove_nodes.length,
        added_edge_count: plan.add_edges.length,
        removed_edge_count: plan.remove_edges.length,
        final_node_count: nodeCount(beforeSnapshot) - plan.remove_nodes.length + plan.create_nodes.length,
        final_edge_count: observedConnections.length - plan.remove_edges.length + plan.add_edges.length,
    };
    if (!valuesEqual(expected, actualDelta)) {
        throw graphPatchError(
            "graph_patch_delta_mismatch",
            "Expected GraphPatch counts do not match its declared operations and baseline.",
            { expected, actual: actualDelta },
        );
    }

    return {
        observedConnections,
        removedRuntime,
        preservedConnections: remainingConnections.filter(edge => (
            !removedNodeIds.has(idKey(edge.source_node_id))
            && !removedNodeIds.has(idKey(edge.target_node_id))
        )),
        assertionMap,
        assertedNodes,
    };
}


function pendingValueTask(nodeIdValue, values, label, afterComplete = null) {
    return {
        nodeId: nodeIdValue,
        values: clone(values || {}),
        pending: new Map(Object.entries(values || {})),
        label,
        afterComplete,
    };
}


async function advancePendingValues(adapter, task) {
    await adapter.setNodeValuesExact(
        task.nodeId,
        Object.fromEntries([...task.pending.entries()].map(([name, value]) => (
            [name, clone(value)]
        ))),
    );
    const observedValues = valuesOf(await adapter.getNode(task.nodeId));
    let progress = false;
    for (const [name, value] of [...task.pending.entries()]) {
        const exact = (
            Object.prototype.hasOwnProperty.call(observedValues, name)
            && valuesEqual(observedValues[name], value)
        );
        if (!exact) continue;
        task.pending.delete(name);
        progress = true;
    }
    if (task.pending.size === 0) {
        assertValues(await adapter.getNode(task.nodeId), task.values, task.label);
        if (typeof task.afterComplete === "function") await task.afterComplete();
    }
    return progress;
}


async function scheduleGraphDependencies(plan, aliases, adapter, delayMs, valueTasks = []) {
    const creates = orderedCreatedNodes(plan);
    const pending = orderedAddedEdges(plan, creates).map((edge, index) => ({ edge, index }));
    const pendingValues = valueTasks.filter(task => task.pending.size > 0);
    const conversionSockets = new Map();
    const runtimeEdges = [];
    while (pending.length > 0 || pendingValues.length > 0) {
        let progress = false;
        const blockedValues = [];
        for (let valueIndex = 0; valueIndex < pendingValues.length;) {
            const task = pendingValues[valueIndex];
            const advanced = await advancePendingValues(adapter, task);
            progress ||= advanced;
            if (task.pending.size === 0) {
                pendingValues.splice(valueIndex, 1);
                continue;
            }
            blockedValues.push({
                node_id: task.nodeId,
                label: task.label,
                pending: [...task.pending.keys()].sort(),
            });
            valueIndex += 1;
        }
        const blocked = [];
        for (let pendingIndex = 0; pendingIndex < pending.length;) {
            const item = pending[pendingIndex];
            let resolved = await resolveRuntimeEdge(
                item.edge,
                aliases,
                adapter,
                conversionSockets,
            );
            if (
                !resolved.ready
                && item.edge.target.mode === "convert_widget"
                && resolved.convertible
            ) {
                const conversion = await adapter.convertWidgetToInput(
                    resolved.targetId,
                    {
                        input_index: item.edge.target.input_index,
                        occurrence_index: item.edge.target.occurrence_index,
                        input: item.edge.target.input,
                        type: item.edge.target.type,
                    },
                );
                if (!isIndex(conversion?.socket_index)) {
                    throw graphPatchError(
                        "graph_patch_widget_conversion_failed",
                        `Converting ${item.edge.target.input} did not return a live socket index.`,
                    );
                }
                conversionSockets.set(
                    symbolicTargetKey(item.edge.target),
                    conversion.socket_index,
                );
                await reveal(adapter, {
                    phase: "convert_widget",
                    target: clone(item.edge.target),
                    node_id: resolved.targetId,
                    socket_index: conversion.socket_index,
                }, delayMs);
                resolved = await resolveRuntimeEdge(
                    item.edge,
                    aliases,
                    adapter,
                    conversionSockets,
                );
                if (!resolved.ready) {
                    throw graphPatchError(
                        "graph_patch_widget_conversion_failed",
                        `Converted input ${item.edge.target.input} is not exact (${resolved.reason}).`,
                    );
                }
            }
            if (!resolved.ready) {
                blocked.push({ index: item.index, reason: resolved.reason });
                pendingIndex += 1;
                continue;
            }
            await adapter.connectNodes(
                resolved.sourceId,
                resolved.targetId,
                clone(resolved.runtime),
                clone(item.edge),
            );
            const observed = (await adapter.listConnections())
                .map(normalizeObservedConnection)
                .filter(Boolean)
                .filter(connection => connectionDetailsMatch(resolved.runtime, connection));
            if (observed.length !== 1) {
                throw graphPatchError(
                    "graph_patch_connection_failed",
                    `Added edge ${item.index} was not observed exactly after connection.`,
                );
            }
            runtimeEdges.push(resolved.runtime);
            pending.splice(pendingIndex, 1);
            progress = true;
            await reveal(adapter, {
                phase: "connection",
                edge: clone(item.edge),
                connection: clone(resolved.runtime),
            }, delayMs);
        }
        if (!progress && (pending.length > 0 || pendingValues.length > 0)) {
            const code = pending.length > 0 && pendingValues.length > 0
                ? "graph_patch_dependency_deadlock"
                : pending.length > 0
                    ? "dynamic_socket_unavailable"
                    : "graph_patch_value_application_failed";
            throw graphPatchError(
                code,
                pending.length > 0 && pendingValues.length > 0
                    ? "No remaining GraphPatch value or edge can unlock the other dependency."
                    : pending.length > 0
                        ? "No remaining GraphPatch edge has an exact ready source and target slot."
                        : "No remaining GraphPatch widget value can materialize exactly.",
                { blocked, blocked_values: blockedValues },
            );
        }
    }
    return { runtimeEdges, conversionSockets };
}


function expectedLayout(item) {
    return item?.layout_hint || null;
}


function existingLayoutRefs(plan) {
    const refs = [
        ...plan.update_nodes.map(item => item.ref),
        ...plan.attachments.map(item => item.ref),
        ...[...plan.add_edges, ...plan.remove_edges].flatMap(edge => [
            edge.source.ref,
            edge.target.ref,
        ]),
    ];
    const removed = new Set(plan.remove_nodes.map(item => idKey(item.ref.node_id)));
    return new Map(
        refs
            .filter(ref => Object.prototype.hasOwnProperty.call(ref, "node_id"))
            .filter(ref => !removed.has(idKey(ref.node_id)))
            .map(ref => [idKey(ref.node_id), ref.node_id]),
    );
}


function exactExistingLayout(beforeNode, layoutHint = null) {
    const position = nodePosition(beforeNode);
    const size = nodeSize(beforeNode);
    if (!position || !size) return null;
    return {
        x: layoutHint?.x ?? position.x,
        y: layoutHint?.y ?? position.y,
        width: layoutHint?.width ?? size.width,
        height: layoutHint?.height ?? size.height,
    };
}


function layoutMatches(node, expected) {
    if (!node || !expected) return false;
    const position = nodePosition(node);
    const size = nodeSize(node);
    return Boolean(
        position?.x === expected.x
        && position?.y === expected.y
        && (expected.width === undefined || size?.width === expected.width)
        && (expected.height === undefined || size?.height === expected.height)
    );
}


async function restoreDeclaredLayouts(plan, adapter, beforeSnapshot, aliases) {
    const beforeNodes = snapshotNodeMap(beforeSnapshot);
    const updates = new Map(plan.update_nodes.map(item => [idKey(item.ref.node_id), item]));
    for (const [id, runtimeId] of existingLayoutRefs(plan)) {
        const expected = exactExistingLayout(beforeNodes.get(id), updates.get(id)?.layout_hint || null);
        const observed = await adapter.getNode(runtimeId);
        if (expected && !layoutMatches(observed, expected)) {
            await adapter.setNodeLayoutExact(runtimeId, clone(expected));
        }
    }
    for (const created of plan.create_nodes) {
        if (!created.layout_hint) continue;
        const runtimeId = aliases[created.alias];
        const observed = await adapter.getNode(runtimeId);
        if (!layoutMatches(observed, created.layout_hint)) {
            await adapter.setNodeLayoutExact(runtimeId, clone(created.layout_hint));
        }
    }
}


async function verifyPatch(
    request,
    adapter,
    beforeSnapshot,
    preflightResult,
    aliases,
    runtimeAddedEdges,
    conversionSockets,
) {
    const issues = [];
    const plan = request.plan;
    const afterSnapshot = await adapter.captureWorkflow();
    const changedEnvelope = changedEnvelopeFields(beforeSnapshot, afterSnapshot);
    if (changedEnvelope.length > 0) {
        issues.push({
            code: "workflow_metadata_changed",
            fields: changedEnvelope,
            message: "A workflow field outside the declared graph patch changed.",
        });
    }

    const observedConnections = (await adapter.listConnections())
        .map(normalizeObservedConnection)
        .filter(Boolean);
    const expectedConnections = [
        ...preflightResult.preservedConnections,
        ...runtimeAddedEdges,
    ];
    if (!countsEqual(
        countKeys(observedConnections, observedExactKey),
        countKeys(expectedConnections, observedExactKey),
    )) {
        issues.push({
            code: "graph_patch_topology_mismatch",
            message: "The final topology is not exactly baseline - removals + additions.",
        });
    }
    for (const expected of expectedConnections) {
        const count = observedConnections.filter(item => connectionDetailsMatch(expected, item)).length;
        if (count !== 1) {
            issues.push({
                code: "graph_patch_edge_mismatch",
                edge: clone(expected),
                message: `Expected one exact final edge; found ${count}.`,
            });
        }
    }
    for (const edge of plan.add_edges) {
        try {
            const resolved = await resolveRuntimeEdge(
                edge,
                aliases,
                adapter,
                conversionSockets,
            );
            if (!resolved.ready) {
                issues.push({
                    code: "graph_patch_slot_manifest_mismatch",
                    edge: clone(edge),
                    message: `A final endpoint manifest is unavailable (${resolved.reason}).`,
                });
            }
        } catch (error) {
            issues.push({
                code: error.code || "graph_patch_slot_manifest_mismatch",
                edge: clone(edge),
                message: String(error.message || error),
            });
        }
    }

    const expectedNodeIds = snapshotNodeIds(beforeSnapshot);
    for (const removal of plan.remove_nodes) expectedNodeIds.delete(idKey(removal.ref.node_id));
    for (const id of Object.values(aliases)) expectedNodeIds.add(idKey(id));
    const observedNodeIds = snapshotNodeIds(afterSnapshot);
    if (
        expectedNodeIds.size !== observedNodeIds.size
        || [...expectedNodeIds].some(id => !observedNodeIds.has(id))
    ) {
        issues.push({
            code: "graph_patch_node_set_mismatch",
            message: "The final node set contains a missing or undeclared node.",
        });
    }
    if (
        observedNodeIds.size !== plan.expected_delta.final_node_count
        || observedConnections.length !== plan.expected_delta.final_edge_count
    ) {
        issues.push({
            code: "graph_patch_delta_mismatch",
            message: "The observed final graph counts do not match expected_delta.",
        });
    }

    const beforeNodes = snapshotNodeMap(beforeSnapshot);
    const afterNodes = snapshotNodeMap(afterSnapshot);
    const removedIds = new Set(plan.remove_nodes.map(item => idKey(item.ref.node_id)));
    const updatedIds = new Set(plan.update_nodes.map(item => idKey(item.ref.node_id)));
    const updatesById = new Map(plan.update_nodes.map(item => [idKey(item.ref.node_id), item]));
    const convertedExisting = new Set(
        plan.add_edges
            .filter(edge => (
                edge.target.mode === "convert_widget"
                && Object.prototype.hasOwnProperty.call(edge.target.ref, "node_id")
            ))
            .map(edge => idKey(edge.target.ref.node_id)),
    );
    const attachedExisting = new Set(
        plan.attachments
            .filter(item => Object.prototype.hasOwnProperty.call(item.ref, "node_id"))
            .map(item => idKey(item.ref.node_id)),
    );
    const attachmentsByExistingId = new Map();
    for (const attachment of plan.attachments) {
        if (!Object.prototype.hasOwnProperty.call(attachment.ref, "node_id")) continue;
        const id = idKey(attachment.ref.node_id);
        const names = attachmentsByExistingId.get(id) || new Set();
        names.add(attachment.input);
        attachmentsByExistingId.set(id, names);
    }
    const reconfiguredExisting = new Set(
        [...plan.add_edges, ...plan.remove_edges]
            .filter(edge => Object.prototype.hasOwnProperty.call(edge.target.ref, "node_id"))
            .map(edge => idKey(edge.target.ref.node_id)),
    );
    for (const [id, beforeNode] of beforeNodes) {
        if (removedIds.has(id)) continue;
        const afterNode = afterNodes.get(id);
        if (!afterNode || nodeType(afterNode) !== nodeType(beforeNode)) {
            issues.push({ code: "graph_patch_existing_node_changed", node_id: id });
            continue;
        }
        const touched = (
            updatedIds.has(id)
            || convertedExisting.has(id)
            || attachedExisting.has(id)
            || reconfiguredExisting.has(id)
        );
        if (!touched && (
            !valuesEqual(stableNodeFacts(afterNode), stableNodeFacts(beforeNode))
            || !valuesEqual(nodePosition(afterNode), nodePosition(beforeNode))
            || !valuesEqual(nodeSize(afterNode), nodeSize(beforeNode))
            || !valuesEqual(valuesOf(afterNode), valuesOf(beforeNode))
        )) {
            issues.push({
                code: "graph_patch_existing_node_changed",
                node_id: id,
                message: `Unrelated node ${id} values, metadata, or layout changed.`,
            });
        } else if (touched) {
            const update = updatesById.get(id) || null;
            const attachmentNames = attachmentsByExistingId.get(id) || new Set();
            const beforeObserved = preflightResult.assertedNodes.get(id) || beforeNode;
            const afterObserved = await adapter.getNode(nodeId(beforeNode)) || afterNode;
            const rules = touchedManifestRules(
                plan,
                id,
                beforeObserved,
                update,
                attachmentNames,
            );
            const manifests = touchedNodeManifestsMatch(beforeObserved, afterObserved, rules);
            for (const [manifest, exact] of Object.entries(manifests)) {
                if (exact) continue;
                issues.push({
                    code: "graph_patch_existing_node_changed",
                    node_id: id,
                    manifest,
                    message: `Touched node ${id} changed its unrelated ${manifest} manifest.`,
                });
            }
            const valuesMatch = update
                ? updatedValuesMatch(beforeObserved, afterObserved, update, attachmentNames)
                : valuesEqual(
                    valuesWithoutNames(valuesOf(afterObserved), attachmentNames),
                    valuesWithoutNames(valuesOf(beforeObserved), attachmentNames),
                );
            if (!valuesMatch) {
                issues.push({
                    code: "graph_patch_value_mismatch",
                    node_id: id,
                    message: `Touched node ${id} changed an undeclared value.`,
                });
            }
            const expectedPosition = update?.layout_hint
                ? { x: update.layout_hint.x, y: update.layout_hint.y }
                : nodePosition(beforeNode);
            const expectedSize = update?.layout_hint
                ? {
                    width: update.layout_hint.width ?? nodeSize(beforeNode)?.width,
                    height: update.layout_hint.height ?? nodeSize(beforeNode)?.height,
                }
                : nodeSize(beforeNode);
            if (
                !valuesEqual(nodePosition(afterNode), expectedPosition)
                || !valuesEqual(nodeSize(afterNode), expectedSize)
            ) {
                issues.push({
                    code: "graph_patch_layout_mismatch",
                    node_id: id,
                    message: `Touched node ${id} changed outside its declared layout.`,
                });
            }
        }
    }

    for (const update of plan.update_nodes) {
        const observed = await adapter.getNode(update.ref.node_id);
        try {
            const attachmentNames = attachmentsByExistingId.get(idKey(update.ref.node_id)) || new Set();
            const beforeObserved = preflightResult.assertedNodes.get(idKey(update.ref.node_id));
            if (!updatedValuesMatch(beforeObserved, observed, update, attachmentNames)) {
                throw graphPatchError(
                    "graph_patch_value_mismatch",
                    `update_nodes.${update.ref.node_id} changed an undeclared value.`,
                );
            }
        } catch (error) {
            issues.push({ code: error.code, message: error.message });
        }
        if (update.layout_hint) {
            const position = observed?.position || nodePosition(observed);
            const size = observed?.size || nodeSize(observed);
            const layoutMatches = (
                position?.x === update.layout_hint.x
                && position?.y === update.layout_hint.y
                && (update.layout_hint.width === undefined || size?.width === update.layout_hint.width)
                && (update.layout_hint.height === undefined || size?.height === update.layout_hint.height)
            );
            if (!layoutMatches) issues.push({ code: "graph_patch_layout_mismatch", node_id: update.ref.node_id });
        }
    }

    for (const attachment of plan.attachments) {
        const id = resolveRef(attachment.ref, aliases);
        try {
            if (!await adapter.verifyAttachmentExact(id, clone(attachment))) {
                throw graphPatchError(
                    "graph_patch_attachment_mismatch",
                    `Attachment ${attachment.input} did not persist exactly.`,
                );
            }
        } catch (error) {
            issues.push({ code: error.code, message: error.message });
        }
    }

    for (const created of plan.create_nodes) {
        const id = aliases[created.alias];
        const observed = await adapter.getNode(id);
        if (
            !observed
            || nodeType(observed) !== created.node_type
            || schemaHashOf(observed) !== created.schema_hash
        ) {
            issues.push({ code: "graph_patch_created_node_mismatch", alias: created.alias });
            continue;
        }
        try {
            assertValues(observed, created.values, `create_nodes.${created.alias}`);
        } catch (error) {
            issues.push({ code: error.code, message: error.message });
        }
        const metadata = observed?.properties?.[WORKFLOW_GRAPH_PATCH_PROPERTY];
        if (
            metadata?.schema !== request.patch_schema
            || metadata?.application_id !== request.application_id
            || metadata?.patch_hash !== request.patch_hash
            || metadata?.alias !== created.alias
            || metadata?.schema_hash !== created.schema_hash
        ) {
            issues.push({ code: "graph_patch_created_metadata_mismatch", alias: created.alias });
        }
        const layout = expectedLayout(created);
        if (layout) {
            const position = observed?.position || nodePosition(observed);
            const size = observed?.size || nodeSize(observed);
            if (
                position?.x !== layout.x
                || position?.y !== layout.y
                || (layout.width !== undefined && size?.width !== layout.width)
                || (layout.height !== undefined && size?.height !== layout.height)
            ) issues.push({ code: "graph_patch_layout_mismatch", alias: created.alias });
        }
    }

    return {
        valid: issues.length === 0,
        issues,
        node_count: observedNodeIds.size,
        edge_count: observedConnections.length,
        preserved_edge_count: preflightResult.preservedConnections.length,
    };
}


async function attestAlreadyApplied(request, adapter, entry) {
    const issues = [];
    const aliases = isRecord(entry?.aliases) ? entry.aliases : {};
    const expectedEntryFields = [
        "aliases",
        "created_node_ids",
        "patch_hash",
        "removed_node_ids",
        "result_content_hash",
        ...(request.operation === "scoped_patch" ? ["result_definition_hash"] : []),
    ].sort();
    if (
        !isRecord(entry)
        || !valuesEqual(Object.keys(entry).sort(), expectedEntryFields)
    ) {
        issues.push({ code: "graph_patch_ledger_shape_mismatch" });
    }
    const expectedAliasNames = request.plan.create_nodes.map(item => item.alias).sort();
    if (!valuesEqual(Object.keys(aliases).sort(), expectedAliasNames)) {
        issues.push({ code: "graph_patch_ledger_alias_mismatch" });
    }
    const expectedCreatedIds = request.plan.create_nodes.map(item => aliases[item.alias]);
    if (
        expectedCreatedIds.some(id => id === undefined)
        || !Array.isArray(entry?.created_node_ids)
        || !valuesEqual(entry.created_node_ids, expectedCreatedIds)
    ) {
        issues.push({ code: "graph_patch_ledger_created_ids_mismatch" });
    }
    const expectedRemovedIds = request.plan.remove_nodes.map(item => item.ref.node_id);
    if (
        !Array.isArray(entry?.removed_node_ids)
        || !valuesEqual(entry.removed_node_ids, expectedRemovedIds)
    ) {
        issues.push({ code: "graph_patch_ledger_removed_ids_mismatch" });
    }
    const observedConnections = (await adapter.listConnections())
        .map(normalizeObservedConnection)
        .filter(Boolean);
    const snapshot = await adapter.captureWorkflow();
    const observedNodeIds = snapshotNodeIds(snapshot);
    if (
        observedNodeIds.size !== request.plan.expected_delta.final_node_count
        || observedConnections.length !== request.plan.expected_delta.final_edge_count
    ) {
        issues.push({ code: "graph_patch_delta_mismatch" });
    }
    const removedNodeIds = new Set(
        request.plan.remove_nodes.map(item => idKey(item.ref.node_id)),
    );
    for (const assertion of request.plan.assertions.nodes) {
        if (removedNodeIds.has(idKey(assertion.ref.node_id))) continue;
        const node = await adapter.getNode(assertion.ref.node_id);
        if (
            !node
            || !typedValuesEqual(nodeId(node), assertion.ref.node_id)
            || nodeType(node) !== assertion.node_type
            || schemaHashOf(node) !== assertion.schema_hash
        ) {
            issues.push({
                code: "graph_patch_asserted_node_mismatch",
                node_id: assertion.ref.node_id,
            });
        }
    }
    const removedEdgeKeys = new Set(request.plan.remove_edges.map(symbolicEdgeKey));
    for (const edge of request.plan.assertions.edges) {
        if (removedEdgeKeys.has(symbolicEdgeKey(edge))) continue;
        try {
            const resolved = await resolveRuntimeEdge(edge, {}, adapter);
            const count = resolved.ready
                ? observedConnections.filter(item => (
                    typedConnectionDetailsMatch(resolved.runtime, item)
                )).length
                : 0;
            if (count !== 1) {
                issues.push({ code: "graph_patch_asserted_edge_mismatch", edge: clone(edge) });
            }
        } catch (error) {
            issues.push({
                code: "graph_patch_asserted_edge_mismatch",
                edge: clone(edge),
                reason: error.code || "graph_patch_slot_mismatch",
            });
        }
    }
    for (const created of request.plan.create_nodes) {
        const id = aliases[created.alias];
        const node = id === undefined ? null : await adapter.getNode(id);
        if (
            !node
            || !typedValuesEqual(nodeId(node), id)
            || nodeType(node) !== created.node_type
            || schemaHashOf(node) !== created.schema_hash
        ) {
            issues.push({ code: "graph_patch_created_node_mismatch", alias: created.alias });
            continue;
        }
        try {
            assertValues(node, created.values, `create_nodes.${created.alias}`);
        } catch (error) {
            issues.push({ code: error.code || "graph_patch_value_mismatch", alias: created.alias });
        }
        const metadata = node?.properties?.[WORKFLOW_GRAPH_PATCH_PROPERTY];
        if (
            metadata?.schema !== request.patch_schema
            || metadata?.application_id !== request.application_id
            || metadata?.patch_hash !== request.patch_hash
            || metadata?.alias !== created.alias
            || metadata?.schema_hash !== created.schema_hash
        ) issues.push({ code: "graph_patch_created_metadata_mismatch", alias: created.alias });
        if (created.layout_hint && !layoutMatches(node, created.layout_hint)) {
            issues.push({ code: "graph_patch_layout_mismatch", alias: created.alias });
        }
    }
    for (const update of request.plan.update_nodes) {
        const node = await adapter.getNode(update.ref.node_id);
        if (
            !node
            || !typedValuesEqual(nodeId(node), update.ref.node_id)
            || nodeType(node) !== update.node_type
            || schemaHashOf(node) !== update.schema_hash
            || !updatedValuesMatch(node, node, update)
        ) issues.push({ code: "graph_patch_update_mismatch", node_id: update.ref.node_id });
        if (update.layout_hint && !layoutMatches(node, update.layout_hint)) {
            issues.push({ code: "graph_patch_layout_mismatch", node_id: update.ref.node_id });
        }
    }
    for (const removal of request.plan.remove_nodes) {
        if (await adapter.getNode(removal.ref.node_id)) {
            issues.push({ code: "graph_patch_removed_node_present", node_id: removal.ref.node_id });
        }
    }
    for (const attachment of request.plan.attachments) {
        const id = resolveRef(attachment.ref, aliases);
        try {
            if (!await adapter.verifyAttachmentExact(id, clone(attachment))) {
                issues.push({
                    code: "graph_patch_attachment_mismatch",
                    input: attachment.input,
                });
            }
        } catch (error) {
            issues.push({
                code: error.code || "graph_patch_attachment_mismatch",
                input: attachment.input,
            });
        }
    }
    const resolvedAddedEdges = [];
    for (const edge of request.plan.add_edges) {
        try {
            const resolved = await resolveAppliedPostconditionEdge(edge, aliases, adapter);
            const count = resolved.ready
                ? observedConnections.filter(item => (
                    typedConnectionDetailsMatch(resolved.runtime, item)
                )).length
                : 0;
            if (count !== 1) {
                issues.push({ code: "graph_patch_edge_mismatch", edge: clone(edge) });
            } else {
                resolvedAddedEdges.push({ edge, runtime: resolved.runtime });
            }
        } catch (error) {
            issues.push({
                code: "graph_patch_edge_mismatch",
                edge: clone(edge),
                reason: error.code || "graph_patch_slot_mismatch",
            });
        }
    }
    for (const created of request.plan.create_nodes) {
        const id = aliases[created.alias];
        if (id === undefined) continue;
        const observedIncident = observedConnections.filter(connection => (
            typedValuesEqual(connection.source_node_id, id)
            || typedValuesEqual(connection.target_node_id, id)
        ));
        const expectedIncident = resolvedAddedEdges
            .filter(item => (
                item.edge.source.ref.alias === created.alias
                || item.edge.target.ref.alias === created.alias
            ))
            .map(item => item.runtime);
        if (!countsEqual(
            countKeys(observedIncident, observedTypedExactKey),
            countKeys(expectedIncident, observedTypedExactKey),
        )) {
            issues.push({
                code: "graph_patch_created_incident_edge_mismatch",
                alias: created.alias,
            });
        }
    }
    for (const edge of request.plan.remove_edges) {
        const resolved = await resolveRuntimeEdge(edge, {}, adapter);
        if (
            resolved.ready
            && observedConnections.some(item => (
                typedConnectionDetailsMatch(resolved.runtime, item)
            ))
        ) issues.push({ code: "graph_patch_removed_edge_present", edge: clone(edge) });
    }
    if (issues.length > 0) {
        throw graphPatchError(
            "graph_patch_idempotency_conflict",
            "The persisted GraphPatch ledger does not match the current graph postconditions.",
            { issues },
        );
    }
    return { valid: true, issues: [], idempotency_verified: true };
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
        rollback.snapshot_restored = canonicalWorkflowJSON(restored) === canonicalWorkflowJSON(snapshot);
    } catch (error) {
        rollback.errors.push(String(error?.message || error));
    }
    rollback.complete = rollback.snapshot_restored && rollback.hash_verified;
    return rollback;
}


/**
 * Apply one canonical root GraphPatch v2 or definition-scoped GraphPatch v3.
 * Both contracts use the same mutation scheduler and verifier. The complete
 * root workflow remains the rollback, race, commit, and idempotency authority.
 */
export async function applyWorkflowGraphPatchAtomic(rawRequest, adapter) {
    let request;
    try {
        request = normalizeApplyRequest(rawRequest);
        validateRootAdapter(adapter, request.operation === "scoped_patch");
        if (request.operation === "patch") validateAdapter(adapter, request.plan);
    } catch (error) {
        return failureResult(rawRequest, error);
    }

    let beforeRootSnapshot;
    let beforeGraphHash;
    let graphAdapter = adapter;
    let graphBeforeSnapshot;
    let scopeBeforeDefinition = null;
    let mutationStarted = false;
    let verification = { valid: false, issues: [] };
    const aliases = {};
    const createdNodeIds = [];
    const removedNodeIds = request.plan.remove_nodes.map(item => item.ref.node_id);
    try {
        beforeRootSnapshot = await adapter.captureWorkflow();
        assertUnambiguousSnapshotNodeIds(beforeRootSnapshot);
        beforeGraphHash = await workflowGraphHash(beforeRootSnapshot);
        const ledger = readLedger(beforeRootSnapshot);
        const existing = Object.prototype.hasOwnProperty.call(
            ledger.entries,
            request.application_id,
        ) ? ledger.entries[request.application_id] : null;
        if (existing?.patch_hash !== undefined && existing.patch_hash !== request.patch_hash) {
            throw graphPatchError(
                "graph_patch_idempotency_conflict",
                "This application ID is bound to a different GraphPatch hash.",
            );
        }
        if (existing) {
            const currentContentHash = await contentGraphHash(beforeRootSnapshot);
            if (currentContentHash !== existing.result_content_hash) {
                throw graphPatchError(
                    "graph_patch_idempotency_conflict",
                    "The graph changed after this GraphPatch was applied.",
                );
            }
        } else if (beforeGraphHash !== request.plan.expected_graph_hash) {
            throw graphPatchError(
                "graph_patch_precondition_failed",
                "The active graph hash no longer matches this GraphPatch.",
                { expected: request.plan.expected_graph_hash, actual: beforeGraphHash },
            );
        }

        if (request.operation === "scoped_patch") {
            const expectedDefinitionHash = existing?.result_definition_hash
                || request.scope.definition_hash;
            if (!SHA256_PATTERN.test(String(expectedDefinitionHash || ""))) {
                throw graphPatchError(
                    "graph_patch_idempotency_conflict",
                    "The scoped ledger lacks an exact result definition hash.",
                );
            }
            const scopeFacts = await attestScopeAuthority(
                beforeRootSnapshot,
                request,
                expectedDefinitionHash,
            );
            scopeBeforeDefinition = scopeFacts.definition;
            graphAdapter = await adapter.resolveScopedGraph({
                scope: clone(request.scope),
                input_runtime_id: GRAPH_PATCH_SCOPE_INPUT_RUNTIME_ID,
                output_runtime_id: GRAPH_PATCH_SCOPE_OUTPUT_RUNTIME_ID,
                input_node_type: SCOPE_INPUT_NODE_TYPE,
                output_node_type: SCOPE_OUTPUT_NODE_TYPE,
                input_schema_hash: SCOPE_INPUT_SCHEMA_HASH,
                output_schema_hash: SCOPE_OUTPUT_SCHEMA_HASH,
            });
            validateAdapter(graphAdapter, request.plan, false);
            if (typeof graphAdapter?.captureDefinition !== "function") {
                throw graphPatchError(
                    "invalid_graph_patch_adapter",
                    "The scoped adapter cannot capture its exact native definition.",
                );
            }
            const nativeDefinition = projectNativeScopeDefinition(
                await graphAdapter.captureDefinition(),
            );
            if (
                !valuesEqual(nativeDefinition, scopeBeforeDefinition)
                || await workflowScopeDefinitionHash(nativeDefinition) !== expectedDefinitionHash
            ) {
                throw graphPatchError(
                    "scoped_native_definition_mismatch",
                    "The native Subgraph object differs from the serialized scope authority.",
                );
            }
            graphBeforeSnapshot = await graphAdapter.captureWorkflow();
            assertUnambiguousSnapshotNodeIds(graphBeforeSnapshot);

            // Close the async path/hash/native-resolution race before either a
            // local graph mutation or a shared root counter can change.
            const preEffectRoot = await adapter.captureWorkflow();
            if (!valuesEqual(preEffectRoot, beforeRootSnapshot)) {
                throw graphPatchError(
                    "concurrent_workflow_edit",
                    "The root workflow changed during scoped preflight.",
                );
            }
        } else {
            graphBeforeSnapshot = beforeRootSnapshot;
        }

        if (existing) {
            verification = await withReadGuard(
                graphAdapter,
                () => attestAlreadyApplied(request, graphAdapter, existing),
            );
            return {
                success: true,
                applied: false,
                already_applied: true,
                patch_schema: request.patch_schema,
                application_id: request.application_id,
                patch_hash: request.patch_hash,
                operation: request.operation,
                expected_workflow_identity: request.plan.expected_workflow_identity,
                graph_hash: beforeGraphHash,
                aliases: clone(existing.aliases || {}),
                created_node_ids: clone(existing.created_node_ids || []),
                removed_node_ids: clone(existing.removed_node_ids || []),
                verification,
                rollback: emptyRollback(),
                queued: false,
            };
        }

        const preflightResult = await withReadGuard(
            graphAdapter,
            () => preflight(request, graphAdapter, graphBeforeSnapshot),
        );
        const delayMs = revealDelayMs(mutationStepCount(request.plan));
        const initialNodeIds = snapshotNodeIds(graphBeforeSnapshot);
        const valueTasks = [];

        for (const connection of orderedRuntimeRemovals(preflightResult.removedRuntime)) {
            mutationStarted = true;
            await graphAdapter.disconnectConnection(clone(connection));
            await reveal(graphAdapter, { phase: "disconnect", connection: clone(connection) }, delayMs);
        }

        for (const created of orderedCreatedNodes(request.plan)) {
            mutationStarted = true;
            const observed = await graphAdapter.createNode({
                alias: created.alias,
                node_type: created.node_type,
                schema_hash: created.schema_hash,
                layout_hint: clone(created.layout_hint),
            });
            const id = nodeId(observed);
            if (
                id === undefined
                || id === null
                || initialNodeIds.has(idKey(id))
                || createdNodeIds.some(value => nodeIdsEqual(value, id))
            ) {
                throw graphPatchError(
                    "graph_patch_node_creation_failed",
                    `Creating ${created.alias} did not return a fresh node ID.`,
                );
            }
            aliases[created.alias] = id;
            createdNodeIds.push(id);
            await graphAdapter.setNodeMetadata(id, {
                schema: request.patch_schema,
                application_id: request.application_id,
                patch_hash: request.patch_hash,
                alias: created.alias,
                schema_hash: created.schema_hash,
            });
            const finishCreatedValues = async () => {
                if (created.layout_hint) {
                    await graphAdapter.setNodeLayoutExact(id, clone(created.layout_hint));
                }
            };
            if (Object.keys(created.values).length > 0) {
                valueTasks.push(pendingValueTask(
                    id,
                    created.values,
                    `create_nodes.${created.alias}`,
                    finishCreatedValues,
                ));
            } else {
                await finishCreatedValues();
            }
            await reveal(graphAdapter, { phase: "node", node_id: id, alias: created.alias }, delayMs);
        }

        for (const update of request.plan.update_nodes) {
            mutationStarted = true;
            const finishUpdatedValues = async () => {
                if (update.layout_hint) {
                    await graphAdapter.setNodeLayoutExact(update.ref.node_id, clone(update.layout_hint));
                }
                await reveal(graphAdapter, { phase: "update", node_id: update.ref.node_id }, delayMs);
            };
            if (Object.keys(update.set_values).length > 0) {
                valueTasks.push(pendingValueTask(
                    update.ref.node_id,
                    update.set_values,
                    `update_nodes.${update.ref.node_id}`,
                    finishUpdatedValues,
                ));
            } else {
                await finishUpdatedValues();
            }
        }

        if (request.plan.add_edges.length > 0 || valueTasks.length > 0) mutationStarted = true;
        const { runtimeEdges, conversionSockets } = await scheduleGraphDependencies(
            request.plan,
            aliases,
            graphAdapter,
            delayMs,
            valueTasks,
        );
        if (runtimeEdges.length > 0) mutationStarted = true;

        for (const attachment of request.plan.attachments) {
            mutationStarted = true;
            const targetId = resolveRef(attachment.ref, aliases);
            const beforeAssignment = await graphAdapter.getNode(targetId);
            const targetState = beforeAssignment
                ? exactAttachmentState(beforeAssignment, attachment)
                : null;
            if (!targetState?.ready) {
                throw graphPatchError(
                    "graph_patch_attachment_mismatch",
                    `Attachment target ${attachment.input} is unavailable (${targetState?.reason || "node_missing"}).`,
                );
            }
            await graphAdapter.assignAttachmentExact(targetId, clone(attachment));
            if (!await graphAdapter.verifyAttachmentExact(targetId, clone(attachment))) {
                throw graphPatchError(
                    "graph_patch_attachment_mismatch",
                    `Attachment ${attachment.input} did not persist exactly.`,
                );
            }
            await reveal(graphAdapter, { phase: "attachment", node_id: targetId, input: attachment.input }, delayMs);
        }

        for (const removal of request.plan.remove_nodes) {
            mutationStarted = true;
            await graphAdapter.removeNodes([removal.ref.node_id]);
            await reveal(graphAdapter, { phase: "remove", node_id: removal.ref.node_id }, delayMs);
        }

        await restoreDeclaredLayouts(
            request.plan,
            graphAdapter,
            graphBeforeSnapshot,
            aliases,
        );

        verification = await withReadGuard(
            graphAdapter,
            () => verifyPatch(
                request,
                graphAdapter,
                graphBeforeSnapshot,
                preflightResult,
                aliases,
                runtimeEdges,
                conversionSockets,
            ),
        );
        if (!verification.valid) {
            throw graphPatchError(
                "post_graph_patch_verification_failed",
                "The final graph did not match the exact declared GraphPatch delta.",
                { issues: verification.issues },
            );
        }

        let refinedRootSnapshot;
        let resultDefinitionHash = null;
        if (request.operation === "scoped_patch") {
            refinedRootSnapshot = await adapter.captureWorkflow();
            const afterDefinition = resolveSerializedScope(
                refinedRootSnapshot,
                request.scope,
            ).definition;
            const nativeDefinition = projectNativeScopeDefinition(
                await graphAdapter.captureDefinition(),
            );
            if (!valuesEqual(nativeDefinition, afterDefinition)) {
                throw graphPatchError(
                    "scoped_native_definition_mismatch",
                    "The mutated native Subgraph differs from the root serialization.",
                );
            }
            attestDefinitionBoundaryBookkeeping(afterDefinition);
            assertScopedRootPreserved(
                beforeRootSnapshot,
                refinedRootSnapshot,
                request.scope.definition_id,
                scopeBeforeDefinition,
                afterDefinition,
            );
            resultDefinitionHash = await workflowScopeDefinitionHash(afterDefinition);
        } else {
            refinedRootSnapshot = await adapter.captureWorkflow();
        }
        const resultContentHash = await contentGraphHash(refinedRootSnapshot);
        const canonicalCreatedNodeIds = request.plan.create_nodes.map(item => aliases[item.alias]);
        const entry = {
            patch_hash: request.patch_hash,
            result_content_hash: resultContentHash,
            aliases: clone(aliases),
            created_node_ids: clone(canonicalCreatedNodeIds),
            removed_node_ids: clone(removedNodeIds),
            ...(resultDefinitionHash ? { result_definition_hash: resultDefinitionHash } : {}),
        };
        const nextLedger = ledgerWithEntry(ledger, request.application_id, entry);
        mutationStarted = true;
        await adapter.setWorkflowExtra(GRAPH_PATCH_LEDGER_KEY, nextLedger);
        const committedSnapshot = await adapter.captureWorkflow();
        const committedContentHash = await contentGraphHash(committedSnapshot);
        const committedEntry = readLedger(committedSnapshot).entries[request.application_id];
        if (committedContentHash !== resultContentHash || !valuesEqual(committedEntry, entry)) {
            throw graphPatchError(
                "graph_patch_commit_verification_failed",
                "The GraphPatch ledger or final content hash did not persist exactly.",
            );
        }

        return {
            success: true,
            applied: true,
            already_applied: false,
            patch_schema: request.patch_schema,
            application_id: request.application_id,
            patch_hash: request.patch_hash,
            operation: request.operation,
            expected_workflow_identity: request.plan.expected_workflow_identity,
            previous_graph_hash: beforeGraphHash,
            graph_hash: await workflowGraphHash(committedSnapshot),
            aliases,
            created_node_ids: canonicalCreatedNodeIds,
            removed_node_ids: removedNodeIds,
            verification,
            reveal_delay_ms: delayMs,
            rollback: emptyRollback(),
            queued: false,
        };
    } catch (error) {
        const rollback = mutationStarted && beforeRootSnapshot && beforeGraphHash
            ? await restoreSnapshot(adapter, beforeRootSnapshot, beforeGraphHash)
            : emptyRollback();
        return failureResult(request, error, { verification, rollback });
    }
}
