import assert from "node:assert/strict";
import test from "node:test";

import { nodeIdsEqual } from "../../web/js/node_identity.js";


test("node IDs compare across numeric and string workflow formats", () => {
    assert.equal(nodeIdsEqual(34, "34"), true);
    assert.equal(nodeIdsEqual("34", 34), true);
    assert.equal(nodeIdsEqual("34", "35"), false);
});


test("missing node IDs never match", () => {
    assert.equal(nodeIdsEqual(null, null), false);
    assert.equal(nodeIdsEqual(undefined, 34), false);
});
