import assert from "node:assert/strict";
import test from "node:test";

import { nodeIdsEqual, nodeMatchesQuery } from "../../web/js/node_identity.js";


test("node IDs compare across numeric and string workflow formats", () => {
    assert.equal(nodeIdsEqual(34, "34"), true);
    assert.equal(nodeIdsEqual("34", 34), true);
    assert.equal(nodeIdsEqual("34", "35"), false);
});


test("missing node IDs never match", () => {
    assert.equal(nodeIdsEqual(null, null), false);
    assert.equal(nodeIdsEqual(undefined, 34), false);
});


test("explicit lookup criteria do not confuse numeric titles with node IDs", () => {
    const node = { id: 34, title: "35", type: "KSampler", comfyClass: "KSampler" };

    assert.equal(nodeMatchesQuery(node, { by: "id", value: "34" }), true);
    assert.equal(nodeMatchesQuery(node, { by: "id", value: "35" }), false);
    assert.equal(nodeMatchesQuery(node, { by: "title", value: "35" }), true);
    assert.equal(nodeMatchesQuery(node, { by: "title", value: "34" }), false);
});


test("explicit type lookup checks both LiteGraph type fields", () => {
    assert.equal(nodeMatchesQuery({ type: "PreviewImage" }, { by: "type", value: "PreviewImage" }), true);
    assert.equal(nodeMatchesQuery({ comfyClass: "SaveImage" }, { by: "type", value: "SaveImage" }), true);
});
