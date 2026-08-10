/** Browser-side verification of the exact /object_info contracts pinned by GraphPatch. */

export const NODE_SCHEMA_HASH_SCHEMA = "fl-mcp.comfy-node-schema-contract.v1";


function isRecord(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
}


function jsonType(value) {
    if (value === null) return "null";
    if (Array.isArray(value)) return "array";
    if (typeof value === "number") return "number";
    if (typeof value === "boolean") return "boolean";
    if (typeof value === "string") return "string";
    if (isRecord(value)) return "object";
    return typeof value;
}


function canonicalize(value) {
    if (Array.isArray(value)) return value.map(canonicalize);
    if (!isRecord(value)) return value;
    return Object.fromEntries(
        Object.keys(value)
            .sort()
            .map(key => [key, canonicalize(value[key])]),
    );
}


async function sha256Hex(value) {
    const bytes = new TextEncoder().encode(value);
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
    throw new Error("SHA-256 is unavailable in this browser");
}


/** Match backend/node_library.py exactly: only direct input metadata defaults are volatile. */
export function normalizeNodeSchemaContract(nodeInfo) {
    const normalized = structuredClone(nodeInfo);
    const inputs = normalized?.input;
    if (!isRecord(inputs)) return normalized;
    for (const group of Object.values(inputs)) {
        if (!isRecord(group)) continue;
        for (const spec of Object.values(group)) {
            if (
                !Array.isArray(spec)
                || spec.length < 2
                || !isRecord(spec[1])
                || !Object.prototype.hasOwnProperty.call(spec[1], "default")
            ) continue;
            spec[1].default = {
                "$contract": "widget-default",
                json_type: jsonType(spec[1].default),
            };
        }
    }
    return normalized;
}


export async function nodeSchemaHash(nodeType, nodeInfo) {
    const payload = canonicalize({
        hash_schema: NODE_SCHEMA_HASH_SCHEMA,
        node_type: nodeType,
        schema: normalizeNodeSchemaContract(nodeInfo),
    });
    return await sha256Hex(JSON.stringify(payload));
}


function refKey(ref) {
    if (isRecord(ref) && Object.keys(ref).length === 1) {
        if (Object.prototype.hasOwnProperty.call(ref, "alias")) return `new:${ref.alias}`;
        if (Object.prototype.hasOwnProperty.call(ref, "node_id")) {
            return `existing:${String(ref.node_id)}`;
        }
    }
    throw new Error("GraphPatch schema context received an invalid node reference.");
}


function isExactScopeBoundaryRef(ref, kind) {
    return (
        isRecord(ref)
        && Object.keys(ref).length === 1
        && isRecord(ref[kind])
    );
}


function addNodeContext(contexts, ref, nodeType, schemaHash) {
    const key = refKey(ref);
    const existing = contexts.get(key);
    if (existing && (existing.node_type !== nodeType || existing.schema_hash !== schemaHash)) {
        throw new Error(`GraphPatch node schema facts conflict for ${key}.`);
    }
    if (!existing) {
        contexts.set(key, {
            key,
            ref: structuredClone(ref),
            node_type: nodeType,
            schema_hash: schemaHash,
            schema_inputs: [],
            schema_outputs: [],
            dynamic_selector_names: [],
            dynamic_input_roots: [],
        });
    }
}


const DYNAMIC_INPUT_TYPES = new Set([
    "COMFY_AUTOGROW_V3",
    "COMFY_DYNAMICCOMBO_V3",
    "COMFY_DYNAMICSLOT_V3",
]);


function rootInputsOfType(nodeSchema, acceptedTypes) {
    const result = [];
    const inputs = nodeSchema?.input;
    if (!isRecord(inputs)) return result;
    for (const group of ["required", "optional"]) {
        if (!isRecord(inputs[group])) continue;
        for (const [name, spec] of Object.entries(inputs[group])) {
            if (Array.isArray(spec) && acceptedTypes.has(spec[0])) result.push(name);
        }
    }
    return [...new Set(result)].sort();
}


function dynamicSelectorNames(nodeSchema) {
    return rootInputsOfType(nodeSchema, new Set(["COMFY_DYNAMICCOMBO_V3"]));
}


function dynamicInputRoots(nodeSchema) {
    return rootInputsOfType(nodeSchema, DYNAMIC_INPUT_TYPES);
}


function inputKey(input) {
    return [input.index, input.occurrence_index, input.name, input.type].join("\u0000");
}


function addInputContext(contexts, endpoint) {
    const context = contexts.get(refKey(endpoint.ref));
    if (!context) throw new Error("GraphPatch target input has no node schema context.");
    const input = {
        index: endpoint.input_index,
        occurrence_index: endpoint.occurrence_index,
        name: endpoint.input,
        type: endpoint.type,
        kind: ["convert_widget", "widget"].includes(endpoint.mode) ? "widget" : "socket",
        socket_index: endpoint.socket_index,
    };
    const key = inputKey(input);
    const sameIndex = context.schema_inputs.find(item => (
        item.index === input.index && item.occurrence_index === input.occurrence_index
    ));
    if (sameIndex && inputKey(sameIndex) !== key) {
        throw new Error(`GraphPatch target input schema facts conflict for ${context.key}.`);
    }
    if (!context.schema_inputs.some(item => inputKey(item) === key)) {
        context.schema_inputs.push(input);
    }
}


function addOutputContext(contexts, endpoint) {
    const context = contexts.get(refKey(endpoint.ref));
    if (!context) throw new Error("GraphPatch source output has no node schema context.");
    const output = {
        index: endpoint.output_index,
        name: endpoint.output,
        type: endpoint.type,
    };
    const existing = context.schema_outputs.find(item => item.index === output.index);
    if (existing && (existing.name !== output.name || existing.type !== output.type)) {
        throw new Error(`GraphPatch source output schema facts conflict for ${context.key}.`);
    }
    if (!existing) context.schema_outputs.push(output);
}


/**
 * Index the backend-verified schema facts and require every type to remain browser-loaded.
 * The backend is authoritative for schema hashes: JSON.parse erases Python's int-vs-float
 * distinction (for example 1.0 becomes 1), so recomputing that Python hash in JavaScript
 * would create false drift failures. Exact live names, types, indexes, widgets, and sockets
 * are still checked by the GraphPatch executor before and after mutation.
 */
export async function buildGraphPatchSchemaContexts(plan, catalog, schemaContracts) {
    if (!isRecord(plan) || !isRecord(catalog) || !isRecord(schemaContracts)) {
        throw new TypeError(
            "A canonical GraphPatch plan, /object_info catalog, and backend schema contracts are required.",
        );
    }
    const contexts = new Map();
    for (const item of plan.assertions?.nodes || []) {
        addNodeContext(contexts, item.ref, item.node_type, item.schema_hash);
    }
    for (const item of plan.create_nodes || []) {
        addNodeContext(contexts, { alias: item.alias }, item.node_type, item.schema_hash);
    }
    for (const item of [...(plan.update_nodes || []), ...(plan.remove_nodes || [])]) {
        addNodeContext(contexts, item.ref, item.node_type, item.schema_hash);
    }
    for (const context of contexts.values()) {
        const nodeInfo = catalog[context.node_type];
        if (!isRecord(nodeInfo)) {
            throw new Error(`Node type ${context.node_type} is absent from browser /object_info.`);
        }
        const expected = schemaContracts[context.node_type];
        if (
            !isRecord(expected)
            || expected.schema_hash !== context.schema_hash
            || !isRecord(expected.schema)
        ) {
            throw new Error(`Backend schema contract is missing or inconsistent for ${context.node_type}.`);
        }
        if (
            JSON.stringify(canonicalize(normalizeNodeSchemaContract(nodeInfo)))
            !== JSON.stringify(canonicalize(expected.schema))
        ) {
            throw new Error(`Node schema differs between backend and browser for ${context.node_type}.`);
        }
        context.dynamic_selector_names = dynamicSelectorNames(expected.schema);
        context.dynamic_input_roots = dynamicInputRoots(expected.schema);
    }
    const allEdges = [
        ...(plan.assertions?.edges || []),
        ...(plan.remove_edges || []),
        ...(plan.add_edges || []),
        ...(plan.remove_nodes || []).flatMap(item => item.expected_incident_edges || []),
    ];
    for (const edge of allEdges) {
        if (!isExactScopeBoundaryRef(edge.source?.ref, "scope_input")) {
            addOutputContext(contexts, edge.source);
        }
        if (!isExactScopeBoundaryRef(edge.target?.ref, "scope_output")) {
            addInputContext(contexts, edge.target);
        }
    }
    for (const attachment of plan.attachments || []) {
        addInputContext(contexts, {
            ref: attachment.ref,
            input_index: attachment.input_index,
            occurrence_index: 0,
            input: attachment.input,
            type: attachment.type,
            mode: "widget",
        });
    }
    for (const context of contexts.values()) {
        context.schema_inputs.sort((left, right) => (
            left.index - right.index
            || left.occurrence_index - right.occurrence_index
            || left.name.localeCompare(right.name)
        ));
        context.schema_outputs.sort((left, right) => left.index - right.index);
    }
    return contexts;
}


/** Attach trusted plan/catalog facts to one independently observed live node. */
export function enrichGraphPatchNode(node, context) {
    if (!node || !context) return node;
    if (String(node.node_type || node.type) !== context.node_type) {
        throw new Error(`Live node type does not match ${context.node_type}.`);
    }
    const schemaInputs = structuredClone(context.schema_inputs);
    const liveInputs = (node.live_inputs || []).map(input => {
        const schema = schemaInputs.find(item => (
            item.kind === "socket" && item.socket_index === input.socket_index
        ));
        return schema ? {
            ...structuredClone(input),
            schema_index: schema.index,
            occurrence_index: schema.occurrence_index,
            resolved_type: schema.type,
        } : structuredClone(input);
    });
    const outputs = (node.outputs || []).map(output => {
        const schema = context.schema_outputs.find(item => item.index === output.index);
        return schema ? { ...structuredClone(output), resolved_type: schema.type } : structuredClone(output);
    });
    const widgets = (node.widgets || []).map(widget => ({ ...structuredClone(widget) }));
    const occurrences = new Map();
    for (const widget of widgets) {
        const occurrence = occurrences.get(widget.name) || 0;
        occurrences.set(widget.name, occurrence + 1);
        const schema = schemaInputs.find(item => (
            item.name === widget.name && item.occurrence_index === occurrence
        ));
        if (schema) {
            widget.schema_index = schema.index;
            widget.occurrence_index = schema.occurrence_index;
            widget.input_type = schema.type;
        }
    }
    return {
        ...node,
        schema_hash: context.schema_hash,
        schema_inputs: schemaInputs,
        live_inputs: liveInputs,
        outputs,
        widgets,
        dynamic_selector_names: structuredClone(context.dynamic_selector_names || []),
        dynamic_input_roots: structuredClone(context.dynamic_input_roots || []),
    };
}


export function graphPatchRefKey(ref) {
    return refKey(ref);
}
