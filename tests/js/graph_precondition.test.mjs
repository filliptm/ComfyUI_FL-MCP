import assert from "node:assert/strict";
import test from "node:test";

import {
    canonicalizeWorkflowForHash,
    canonicalWorkflowJSON,
    GRAPH_PRECONDITION_SCHEMA,
    REFINEMENT_LEDGER_KEY,
    workflowGraphHash,
} from "../../web/js/graph_precondition.js";


function workflow() {
    return {
        version: 0.4,
        last_node_id: 10,
        last_link_id: 20,
        nodes: [
            {
                id: "2",
                type: "EmptyImage",
                pos: [20, 40],
                size: [240, 160],
                widgets_values: [512, 512, 1, 0],
                outputs: [{ name: "IMAGE", type: "IMAGE", links: [20] }],
                properties: { package: "core", role: "source" },
            },
            {
                id: 10,
                type: "SaveImage",
                pos: [420, 40],
                size: [280, 180],
                widgets_values: ["refinement-test"],
                inputs: [{ name: "images", type: "IMAGE", link: 20 }],
                properties: { package: "core", role: "output" },
            },
        ],
        links: [[20, "2", 0, 10, 0, "IMAGE"]],
        groups: [],
        config: {},
        extra: {
            ds: { scale: 0.75, offset: [100, -40] },
            [REFINEMENT_LEDGER_KEY]: {
                "refine-1": { before_hash: "a".repeat(64), plan_hash: "b".repeat(64) },
            },
            theme: "dark",
        },
    };
}


function reverseObjectKeys(value) {
    if (Array.isArray(value)) return value.map(reverseObjectKeys);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(
        Object.keys(value)
            .reverse()
            .map(key => [key, reverseObjectKeys(value[key])]),
    );
}


test("canonical workflow identity ignores map, node, and link collection order", async () => {
    const first = workflow();
    const second = reverseObjectKeys(workflow());
    second.nodes.reverse();
    second.links.reverse();

    assert.equal(canonicalWorkflowJSON(first), canonicalWorkflowJSON(second));
    assert.equal(await workflowGraphHash(first), await workflowGraphHash(second));
    assert.match(await workflowGraphHash(first), /^[0-9a-f]{64}$/);
    assert.equal(GRAPH_PRECONDITION_SCHEMA, "fl-mcp.graph-precondition.v1");
});


test("only root viewport and refinement ledger state are excluded", async () => {
    const first = workflow();
    const transientlyChanged = structuredClone(first);
    transientlyChanged.extra.ds = { scale: 2, offset: [-900, 500] };
    transientlyChanged.extra[REFINEMENT_LEDGER_KEY] = {
        "refine-99": { before_hash: "f".repeat(64) },
    };

    assert.equal(await workflowGraphHash(first), await workflowGraphHash(transientlyChanged));
    const canonical = canonicalizeWorkflowForHash(transientlyChanged);
    assert.equal(Object.hasOwn(canonical.extra, "ds"), false);
    assert.equal(Object.hasOwn(canonical.extra, REFINEMENT_LEDGER_KEY), false);

    const meaningfulExtra = structuredClone(first);
    meaningfulExtra.extra.theme = "light";
    assert.notEqual(await workflowGraphHash(first), await workflowGraphHash(meaningfulExtra));

    const nodeMetadata = structuredClone(first);
    nodeMetadata.nodes[0].properties[REFINEMENT_LEDGER_KEY] = { keep: true };
    assert.notEqual(await workflowGraphHash(first), await workflowGraphHash(nodeMetadata));
});


test("node, widget, link, and layout changes alter the precondition", async () => {
    const base = workflow();
    const changes = [
        graph => { graph.nodes[0].type = "EmptyLatentImage"; },
        graph => { graph.nodes[0].widgets_values[0] = 1024; },
        graph => { graph.links[0][4] = 1; },
        graph => { graph.nodes[1].pos[0] += 10; },
        graph => { graph.nodes[1].size[1] += 20; },
    ];
    const baseHash = await workflowGraphHash(base);

    for (const change of changes) {
        const changed = structuredClone(base);
        change(changed);
        assert.notEqual(await workflowGraphHash(changed), baseHash);
    }
});


test("typed node IDs sort deterministically and remain semantically distinct", async () => {
    const graph = workflow();
    graph.nodes = [
        { id: "2", type: "StringTwo" },
        { id: 10, type: "NumberTen" },
        { id: "10", type: "StringTen" },
        { id: 2, type: "NumberTwo" },
    ];
    graph.links = [];

    assert.deepEqual(
        canonicalizeWorkflowForHash(graph).nodes.map(node => node.id),
        [2, 10, "10", "2"],
    );

    const numeric = workflow();
    numeric.nodes[0].id = 2;
    numeric.links[0][1] = 2;
    const string = structuredClone(numeric);
    string.nodes[0].id = "2";
    string.links[0][1] = "2";
    assert.notEqual(await workflowGraphHash(numeric), await workflowGraphHash(string));
});


test("links sort stably by typed endpoints rather than serialized order", async () => {
    const graph = workflow();
    graph.links = [
        [90, 10, 1, 20, 0, "IMAGE"],
        [50, "2", 0, 20, 0, "IMAGE"],
        [40, 2, 0, 20, 0, "IMAGE"],
        [30, 2, 0, 10, 0, "IMAGE"],
    ];
    const reordered = structuredClone(graph);
    reordered.links.reverse();

    assert.deepEqual(
        canonicalizeWorkflowForHash(graph).links.map(link => link[0]),
        [30, 40, 90, 50],
    );
    assert.equal(await workflowGraphHash(graph), await workflowGraphHash(reordered));
});


test("semantically ordered arrays remain ordered", async () => {
    const first = workflow();
    const second = structuredClone(first);
    second.nodes[0].widgets_values.reverse();
    second.groups = [{ title: "one" }, { title: "two" }];
    const third = structuredClone(second);
    third.groups.reverse();

    assert.notEqual(await workflowGraphHash(first), await workflowGraphHash(second));
    assert.notEqual(await workflowGraphHash(second), await workflowGraphHash(third));
});


test("canonicalization is detached and validates the workflow root", () => {
    const original = workflow();
    const canonical = canonicalizeWorkflowForHash(original);
    canonical.nodes[0].pos[0] = 999;

    assert.notEqual(original.nodes[1].pos[0], 999);
    assert.throws(() => canonicalizeWorkflowForHash(null), /serialized ComfyUI graph/);
    assert.throws(() => canonicalizeWorkflowForHash([]), /serialized ComfyUI graph/);
});
