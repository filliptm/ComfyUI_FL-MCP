import assert from "node:assert/strict";
import test from "node:test";

import {
    buildGraphPatchSchemaContexts,
    enrichGraphPatchNode,
    nodeSchemaHash,
    normalizeNodeSchemaContract,
} from "../../web/js/node_schema_contract.js";


const schema = {
    input: { required: { frame_rate: ["FLOAT", { default: 8.0, min: 1.0 }] } },
    output: ["IMAGE"],
    output_name: ["image"],
    python_module: "custom_nodes.test",
};
const sourceSchema = {
    input: { required: {} },
    output: ["FLOAT"],
    output_name: ["fps"],
    python_module: "nodes",
};


test("browser schema normalization ignores volatile direct widget defaults", async () => {
    assert.match(await nodeSchemaHash("Example", schema), /^[0-9a-f]{64}$/);
    const changedRuntimeDefault = structuredClone(schema);
    changedRuntimeDefault.input.required.frame_rate[1].default = 24.0;
    assert.equal(
        await nodeSchemaHash("Example", changedRuntimeDefault),
        await nodeSchemaHash("Example", schema),
    );
});


test("GraphPatch contexts verify schema hashes and annotate convertible widgets", async () => {
    const hash = await nodeSchemaHash("Example", schema);
    const sourceHash = await nodeSchemaHash("Source", sourceSchema);
    const plan = {
        assertions: { nodes: [], edges: [] },
        create_nodes: [
            { alias: "source", node_type: "Source", schema_hash: sourceHash },
            { alias: "combine", node_type: "Example", schema_hash: hash },
        ],
        update_nodes: [],
        remove_nodes: [],
        remove_edges: [],
        attachments: [],
        add_edges: [{
            source: { ref: { alias: "source" }, output_index: 0, output: "fps", type: "FLOAT" },
            target: {
                ref: { alias: "combine" },
                input_index: 0,
                occurrence_index: 0,
                socket_index: null,
                input: "frame_rate",
                type: "FLOAT",
                mode: "convert_widget",
            },
        }],
    };
    // Source is intentionally omitted: source facts are not needed to validate target manifests.
    const contracts = {
        Example: { schema_hash: hash, schema: normalizeNodeSchemaContract(schema) },
        Source: { schema_hash: sourceHash, schema: normalizeNodeSchemaContract(sourceSchema) },
    };
    const contexts = await buildGraphPatchSchemaContexts(
        plan,
        { Example: schema, Source: sourceSchema },
        contracts,
    );
    const context = contexts.get("new:combine");
    assert.deepEqual(context.schema_inputs, [{
        index: 0,
        occurrence_index: 0,
        name: "frame_rate",
        type: "FLOAT",
        kind: "widget",
        socket_index: null,
    }]);

    const enriched = enrichGraphPatchNode({
        node_type: "Example",
        widgets: [{ name: "frame_rate", value: 8, type: "number" }],
    }, context);
    assert.equal(enriched.schema_hash, hash);
    assert.equal(enriched.widgets[0].schema_index, 0);
    assert.equal(enriched.widgets[0].input_type, "FLOAT");
});


test("GraphPatch contexts expose canonical dynamic-selector names for scoped manifest checks", async () => {
    const dynamicSchema = {
        input: {
            required: {
                model: ["COMFY_DYNAMICCOMBO_V3", {
                    options: [{
                        key: "full",
                        inputs: {
                            reference_images: ["COMFY_AUTOGROW_V3", {
                                template: { reference_image: ["IMAGE"] },
                            }],
                        },
                    }],
                }],
                images: ["COMFY_AUTOGROW_V3", {
                    template: { image: ["IMAGE"] },
                }],
            },
            optional: {
                payload: ["COMFY_DYNAMICSLOT_V3", {}],
            },
        },
        output: ["IMAGE"],
        output_name: ["IMAGE"],
    };
    const hash = await nodeSchemaHash("DynamicNode", dynamicSchema);
    const plan = {
        assertions: {
            nodes: [{ ref: { node_id: 7 }, node_type: "DynamicNode", schema_hash: hash }],
            edges: [],
        },
        create_nodes: [],
        update_nodes: [{ ref: { node_id: 7 }, node_type: "DynamicNode", schema_hash: hash }],
        remove_nodes: [], remove_edges: [], add_edges: [], attachments: [],
    };
    const contexts = await buildGraphPatchSchemaContexts(
        plan,
        { DynamicNode: dynamicSchema },
        {
            DynamicNode: {
                schema_hash: hash,
                schema: normalizeNodeSchemaContract(dynamicSchema),
            },
        },
    );
    const context = contexts.get("existing:7");
    assert.deepEqual(context.dynamic_selector_names, ["model"]);
    assert.deepEqual(context.dynamic_input_roots, ["images", "model", "payload"]);
    const enriched = enrichGraphPatchNode({
        node_type: "DynamicNode",
        outputs: [],
        live_inputs: [],
        widgets: [{ name: "model", type: "combo", value: "full" }],
    }, context);
    assert.deepEqual(enriched.dynamic_selector_names, ["model"]);
    assert.deepEqual(enriched.dynamic_input_roots, ["images", "model", "payload"]);
});


test("GraphPatch contexts fail before mutation for missing or drifted browser schemas", async () => {
    const hash = await nodeSchemaHash("Example", schema);
    const plan = {
        assertions: { nodes: [], edges: [] },
        create_nodes: [{ alias: "combine", node_type: "Example", schema_hash: hash }],
        update_nodes: [], remove_nodes: [], remove_edges: [], add_edges: [], attachments: [],
    };
    const contracts = {
        Example: { schema_hash: hash, schema: normalizeNodeSchemaContract(schema) },
    };
    await assert.rejects(
        buildGraphPatchSchemaContexts(plan, {}, contracts),
        /absent from browser \/object_info/,
    );
    const changed = structuredClone(schema);
    changed.output = ["VIDEO"];
    await assert.rejects(
        buildGraphPatchSchemaContexts(plan, { Example: changed }, contracts),
        /differs between backend and browser/,
    );
});


test("GraphPatch contexts annotate concrete MATCHTYPE input and output facts", async () => {
    const matchSource = { input: { required: {} }, output: ["*"], output_name: ["value"] };
    const matchTarget = { input: { required: { value: ["*", { forceInput: true }] } }, output: [] };
    const sourceHash = await nodeSchemaHash("MatchSource", matchSource);
    const targetHash = await nodeSchemaHash("MatchTarget", matchTarget);
    const plan = {
        assertions: { nodes: [], edges: [] },
        create_nodes: [
            { alias: "source", node_type: "MatchSource", schema_hash: sourceHash },
            { alias: "target", node_type: "MatchTarget", schema_hash: targetHash },
        ],
        update_nodes: [], remove_nodes: [], remove_edges: [], attachments: [],
        add_edges: [{
            source: { ref: { alias: "source" }, output_index: 0, output: "value", type: "IMAGE" },
            target: {
                ref: { alias: "target" }, input_index: 0, occurrence_index: 0,
                socket_index: 0, input: "value", type: "IMAGE", mode: "slot",
            },
        }],
    };
    const catalog = { MatchSource: matchSource, MatchTarget: matchTarget };
    const contracts = {
        MatchSource: { schema_hash: sourceHash, schema: normalizeNodeSchemaContract(matchSource) },
        MatchTarget: { schema_hash: targetHash, schema: normalizeNodeSchemaContract(matchTarget) },
    };
    const contexts = await buildGraphPatchSchemaContexts(plan, catalog, contracts);
    const source = enrichGraphPatchNode({
        node_type: "MatchSource",
        outputs: [{ index: 0, name: "value", type: "*" }],
        live_inputs: [], widgets: [],
    }, contexts.get("new:source"));
    const target = enrichGraphPatchNode({
        node_type: "MatchTarget",
        outputs: [],
        live_inputs: [{ socket_index: 0, name: "value", type: "*" }],
        widgets: [],
    }, contexts.get("new:target"));

    assert.equal(source.outputs[0].resolved_type, "IMAGE");
    assert.equal(target.live_inputs[0].resolved_type, "IMAGE");
    assert.equal(target.live_inputs[0].schema_index, 0);
});
