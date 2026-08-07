/** Deterministic workflow identity for guarded FL-MCP graph refinements. */

export const GRAPH_PRECONDITION_SCHEMA = "fl-mcp.graph-precondition.v1";
export const REFINEMENT_LEDGER_KEY = "fl_mcp_refinement_ledger";


function compareStrings(left, right) {
    if (left < right) return -1;
    if (left > right) return 1;
    return 0;
}


function valueTypeRank(value) {
    if (typeof value === "number") return 0;
    if (typeof value === "string") return 1;
    if (typeof value === "boolean") return 2;
    if (value === null) return 3;
    if (typeof value === "object") return 4;
    if (typeof value === "undefined") return 5;
    return 6;
}


/** Compare IDs/slots without conflating numeric 2 with string "2". */
function compareTypedValues(left, right) {
    const leftRank = valueTypeRank(left);
    const rightRank = valueTypeRank(right);
    if (leftRank !== rightRank) return leftRank - rightRank;
    if (typeof left === "number" && typeof right === "number") {
        if (left < right) return -1;
        if (left > right) return 1;
        return 0;
    }
    if (typeof left === "string" && typeof right === "string") {
        return compareStrings(left, right);
    }
    if (typeof left === "boolean" && typeof right === "boolean") {
        return Number(left) - Number(right);
    }
    return compareStrings(JSON.stringify(left), JSON.stringify(right));
}


function canonicalizeValue(value, path = []) {
    if (Array.isArray(value)) {
        return value.map((item, index) => canonicalizeValue(item, [...path, index]));
    }
    if (value && typeof value === "object") {
        const canonical = {};
        for (const key of Object.keys(value).sort(compareStrings)) {
            const isWorkflowExtra = path.length === 1 && path[0] === "extra";
            if (isWorkflowExtra && (key === "ds" || key === REFINEMENT_LEDGER_KEY)) {
                continue;
            }
            canonical[key] = canonicalizeValue(value[key], [...path, key]);
        }
        return canonical;
    }
    return value;
}


function canonicalTieBreak(left, right) {
    return compareStrings(JSON.stringify(left), JSON.stringify(right));
}


function compareNodes(left, right) {
    const idComparison = compareTypedValues(left?.id, right?.id);
    return idComparison || canonicalTieBreak(left, right);
}


function firstDefined(...values) {
    return values.find(value => value !== undefined);
}


function linkSortParts(link) {
    if (Array.isArray(link)) {
        return {
            sourceId: link[1],
            sourceSlot: link[2],
            targetId: link[3],
            targetSlot: link[4],
            linkId: link[0],
        };
    }
    if (link && typeof link === "object") {
        return {
            sourceId: firstDefined(
                link.origin_id,
                link.source_id,
                link.source_node_id,
                link.from_node_id,
            ),
            sourceSlot: firstDefined(
                link.origin_slot,
                link.source_slot,
                link.source_output_index,
                link.source_output,
            ),
            targetId: firstDefined(
                link.target_id,
                link.target_node_id,
                link.to_node_id,
            ),
            targetSlot: firstDefined(
                link.target_slot,
                link.target_input_index,
                link.target_input,
            ),
            linkId: firstDefined(link.id, link.link_id),
        };
    }
    return {
        sourceId: undefined,
        sourceSlot: undefined,
        targetId: undefined,
        targetSlot: undefined,
        linkId: undefined,
    };
}


function compareLinks(left, right) {
    const leftParts = linkSortParts(left);
    const rightParts = linkSortParts(right);
    for (const key of ["sourceId", "sourceSlot", "targetId", "targetSlot", "linkId"]) {
        const comparison = compareTypedValues(leftParts[key], rightParts[key]);
        if (comparison) return comparison;
    }
    return canonicalTieBreak(left, right);
}


/**
 * Return a detached JSON-compatible workflow with deterministic map and graph order.
 * Only top-level nodes and links are treated as unordered graph collections. Every
 * other array (widgets, position, size, inputs, outputs, groups, and nested data)
 * retains its serialized order.
 */
export function canonicalizeWorkflowForHash(workflow) {
    if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
        throw new TypeError("workflow must be a serialized ComfyUI graph object");
    }
    const canonical = canonicalizeValue(workflow);
    if (Array.isArray(canonical.nodes)) {
        canonical.nodes = [...canonical.nodes].sort(compareNodes);
    }
    if (Array.isArray(canonical.links)) {
        canonical.links = [...canonical.links].sort(compareLinks);
    }
    return canonical;
}


/** Return the compact canonical JSON used by the graph precondition hash. */
export function canonicalWorkflowJSON(workflow) {
    return JSON.stringify(canonicalizeWorkflowForHash(workflow));
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


/** Hash one canonical graph with schema domain separation. */
export async function workflowGraphHash(workflow) {
    const payload = JSON.stringify({
        schema: GRAPH_PRECONDITION_SCHEMA,
        workflow: canonicalizeWorkflowForHash(workflow),
    });
    return sha256Hex(payload);
}
