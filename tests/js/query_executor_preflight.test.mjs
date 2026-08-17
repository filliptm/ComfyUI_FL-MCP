import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const repositoryRoot = new URL("../../", import.meta.url);

async function loadBrowserClass(relativePath, className, globals = {}) {
    let sourceText = await readFile(new URL(relativePath, repositoryRoot), "utf8");
    sourceText = sourceText.replace(
        /^import\s+[\s\S]*?\s+from\s+["'][^"']+["'];\s*/gm,
        "",
    );
    sourceText = sourceText.replace(`export class ${className}`, `class ${className}`);
    sourceText += `\nglobalThis.__loadedClass = ${className};\n`;
    const context = vm.createContext({
        console: { log() {}, warn() {}, error() {}, debug() {} },
        ...globals,
    });
    vm.runInContext(sourceText, context, { filename: relativePath });
    return context.__loadedClass;
}

// Real /object_info shapes for the exact node types from the live bug report,
// confirmed against a running ComfyUI instance.
const NODE_DEFS = {
    GeminiImageNode: {
        input: {
            required: { prompt: ["STRING", {}] },
            optional: { files: ["GEMINI_FILES", {}] },
        },
    },
    ImageResizeKJv2: {
        input: {
            required: {
                image: ["IMAGE", {}],
                width: ["INT", {}],
                height: ["INT", {}],
                upscale_method: [["nearest", "bilinear"], {}],
                keep_proportion: [["stretch", "resize"], {}],
                pad_color: ["STRING", {}],
                crop_position: [["center", "top"], {}],
                divisible_by: ["INT", {}],
            },
            optional: { mask: ["MASK", {}], device: [["cpu", "gpu"], {}] },
        },
    },
    LoadImage: {
        input: {
            required: { image: [["a.png", "b.png"], {}] },
            optional: {},
        },
    },
    FL_InpaintCrop: {
        input: {
            required: { image: ["IMAGE", {}] },
            optional: { optional_context_mask: ["MASK", {}] },
        },
    },
    KSampler: {
        input: {
            required: {
                model: ["MODEL", {}],
                positive: ["CONDITIONING", {}],
                negative: ["CONDITIONING", {}],
                latent_image: ["LATENT", {}],
            },
            optional: {},
        },
    },
};

function makeGraph(rawNodesSpec) {
    const links = {};
    let nextLinkId = 100;
    const nodes = rawNodesSpec.map(spec => ({
        id: spec.id,
        comfyClass: spec.comfyClass,
        type: spec.comfyClass,
        title: spec.comfyClass,
        pos: [0, 0],
        size: [200, 100],
        mode: 0,
        widgets: spec.widgets || [],
        inputs: spec.inputs.map(input => {
            if (!input.connected) return { name: input.name, type: input.type, link: null };
            const linkId = nextLinkId++;
            links[linkId] = { origin_id: 0, origin_slot: 0 };
            return { name: input.name, type: input.type, link: linkId };
        }),
        outputs: [],
    }));
    const graph = { _nodes: nodes, links };
    for (const node of nodes) node.graph = graph;
    return graph;
}

async function loadQueryExecutor(appGraph, fetchObjectInfo) {
    const QueryExecutor = await loadBrowserClass("web/js/query_executor.js", "QueryExecutor", {
        app: { graph: appGraph },
        api: {
            fetchApi: async () => {
                if (fetchObjectInfo === "fail") {
                    return { ok: false, status: 500 };
                }
                return { ok: true, json: async () => fetchObjectInfo };
            },
        },
    });
    return new QueryExecutor();
}

test("Gemini node's optional 'files' input is not reported as a missing required slot", async () => {
    const graph = makeGraph([
        {
            id: 1,
            comfyClass: "GeminiImageNode",
            inputs: [
                { name: "prompt", type: "STRING", connected: true },
                { name: "files", type: "GEMINI_FILES", connected: false },
            ],
        },
    ]);
    const executor = await loadQueryExecutor(graph, NODE_DEFS);

    const overview = await executor.getWorkflowOverview();

    assert.equal(overview.required_slots_missing.length, 0);
});

test("Image Resize's optional 'mask' input (no matching widget) is not reported as missing", async () => {
    const graph = makeGraph([
        {
            id: 2,
            comfyClass: "ImageResizeKJv2",
            widgets: [{ name: "width", value: 512 }, { name: "height", value: 512 }],
            inputs: [
                { name: "image", type: "IMAGE", connected: true },
                { name: "mask", type: "MASK", connected: false },
            ],
        },
    ]);
    const executor = await loadQueryExecutor(graph, NODE_DEFS);

    const overview = await executor.getWorkflowOverview();

    assert.equal(overview.required_slots_missing.length, 0);
});

test("Image Resize's required-but-widget-backed combo/string/int params are not reported as missing", async () => {
    // Live regression: these are declared "required" in the node's own
    // schema (ComfyUI puts widget-backed params there too), but each also
    // has a matching widget with a value - the widget satisfies the
    // parameter at execution time even though nothing is wired to the slot.
    const graph = makeGraph([
        {
            id: 20,
            comfyClass: "ImageResizeKJv2",
            widgets: [
                { name: "width", value: 512 },
                { name: "height", value: 512 },
                { name: "upscale_method", value: "bilinear" },
                { name: "keep_proportion", value: "stretch" },
                { name: "pad_color", value: "0, 0, 0" },
                { name: "crop_position", value: "center" },
                { name: "divisible_by", value: 2 },
            ],
            inputs: [
                { name: "image", type: "IMAGE", connected: true },
                { name: "upscale_method", type: "COMBO", connected: false },
                { name: "keep_proportion", type: "COMBO", connected: false },
                { name: "pad_color", type: "STRING", connected: false },
                { name: "crop_position", type: "COMBO", connected: false },
                { name: "divisible_by", type: "INT", connected: false },
            ],
        },
    ]);
    const executor = await loadQueryExecutor(graph, NODE_DEFS);

    const overview = await executor.getWorkflowOverview();

    assert.equal(overview.required_slots_missing.length, 0);
});

test("LoadImage's required 'image' combo widget is not reported as missing once a file is selected", async () => {
    const graph = makeGraph([
        {
            id: 21,
            comfyClass: "LoadImage",
            widgets: [{ name: "image", value: "example.png" }],
            inputs: [{ name: "image", type: "COMBO", connected: false }],
        },
    ]);
    const executor = await loadQueryExecutor(graph, NODE_DEFS);

    const overview = await executor.getWorkflowOverview();

    assert.equal(overview.required_slots_missing.length, 0);
});

test("Inpaint Crop's 'optional_context_mask' input is not reported as missing", async () => {
    const graph = makeGraph([
        {
            id: 3,
            comfyClass: "FL_InpaintCrop",
            inputs: [
                { name: "image", type: "IMAGE", connected: true },
                { name: "optional_context_mask", type: "MASK", connected: false },
            ],
        },
    ]);
    const executor = await loadQueryExecutor(graph, NODE_DEFS);

    const overview = await executor.getWorkflowOverview();

    assert.equal(overview.required_slots_missing.length, 0);
});

test("a genuinely required, unconnected input is still reported as missing", async () => {
    const graph = makeGraph([
        {
            id: 4,
            comfyClass: "KSampler",
            inputs: [
                { name: "model", type: "MODEL", connected: false },
                { name: "positive", type: "CONDITIONING", connected: true },
                { name: "negative", type: "CONDITIONING", connected: true },
                { name: "latent_image", type: "LATENT", connected: true },
            ],
        },
    ]);
    const executor = await loadQueryExecutor(graph, NODE_DEFS);

    const overview = await executor.getWorkflowOverview();

    assert.equal(overview.required_slots_missing.length, 1);
    assert.equal(overview.required_slots_missing[0].missing_slots[0].slot_name, "model");
});

test("falls back to the legacy heuristic (not a hard failure) when /object_info is unavailable", async () => {
    const graph = makeGraph([
        {
            id: 5,
            comfyClass: "KSampler",
            inputs: [
                { name: "model", type: "MODEL", connected: false },
                { name: "positive", type: "CONDITIONING", connected: true },
                { name: "negative", type: "CONDITIONING", connected: true },
                { name: "latent_image", type: "LATENT", connected: true },
            ],
        },
    ]);
    const executor = await loadQueryExecutor(graph, "fail");

    const overview = await executor.getWorkflowOverview();

    // "model" isn't in the legacy optional-name list either, so the fallback
    // heuristic still correctly reports it as missing.
    assert.equal(overview.required_slots_missing.length, 1);
    assert.equal(overview.required_slots_missing[0].missing_slots[0].slot_name, "model");
});
