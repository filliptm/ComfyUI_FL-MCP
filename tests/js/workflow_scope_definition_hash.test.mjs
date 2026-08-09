import assert from "node:assert/strict";
import test from "node:test";

import {
    workflowScopeDefinitionHash,
} from "../../web/js/workflow_graph_patch_apply.js";


const GOLDENS = [
    [{ x: 1e-7 }, "6fe21de92ebeca874cd63605595cbc888309ca35d4e1b3233701b93674fde4a0"],
    [{ x: 1e-6 }, "20857fe3e561827e80e0bc0c980fc55fb76fe8fa083b961fd0b7701b3a9c4c9d"],
    [{ x: 1e20 }, "9481add5f0e8b965b2fff70e0e56dc40df36150bd072b1fc44265e2d3392e9a3"],
    [
        { x: 18446744073709552000 },
        "882dfa5af02c3bb6368ac6bfb67ae4f542e7134bdf8da43d0ba868ed4ed9ca4b",
    ],
    [{ x: -0 }, "86c114de3d4a984398811df119dc51205d7e54292927f8ca6a98e5c630075287"],
    [
        { extra: { "\ue000": "bmp", "\u{10000}": "astral" } },
        "ee77d87f4a21863fb57ee42246864af31c22fef375d5eb7cdc871da4210f6cae",
    ],
    [
        { nested: [null, true, false, "a\n\0", { "😀": "ok" }] },
        "a1396cf88511d623523a42052856cb1d3f6158ea8f4056b5abbcb467ed592b10",
    ],
];


test("scoped definition hash v2 matches Python golden vectors", async () => {
    for (const [definition, expected] of GOLDENS) {
        assert.equal(await workflowScopeDefinitionHash(definition), expected);
    }
    assert.equal(
        await workflowScopeDefinitionHash({ x: -0 }),
        await workflowScopeDefinitionHash({ x: 0 }),
    );
    assert.equal(
        await workflowScopeDefinitionHash({ x: 1 }),
        await workflowScopeDefinitionHash({ x: 1.0 }),
    );
});


test("scoped definition hash rejects every non-strict-JSON value", async () => {
    const sparse = [];
    sparse.length = 1;
    const decorated = [];
    decorated.extra = true;
    const cyclic = {};
    cyclic.self = cyclic;
    const inherited = Object.create({ inherited: true });
    inherited.value = 1;
    const symbolKey = { x: 1 };
    symbolKey[Symbol("hidden")] = true;

    for (const definition of [
        { x: Infinity },
        { x: NaN },
        { x: "\ud800" },
        { x: new Date(0) },
        { x: undefined },
        { x: [undefined] },
        { x: () => true },
        { x: 1n },
        { x: Symbol("value") },
        { x: sparse },
        { x: decorated },
        { x: cyclic },
        { x: inherited },
        symbolKey,
    ]) {
        await assert.rejects(workflowScopeDefinitionHash(definition), error => (
            error.code === "non_json_scoped_definition"
        ));
    }
});
