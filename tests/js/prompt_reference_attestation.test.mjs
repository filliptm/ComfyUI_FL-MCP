import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const root = new URL("../../", import.meta.url);


async function loadToolExecutor() {
    let source = await readFile(new URL("web/js/tool_executor.js", root), "utf8");
    source = source.replace(
        /^import\s+[\s\S]*?\s+from\s+["'][^"']+["'];\s*/gm,
        "",
    );
    source = source.replace("export class ToolExecutor", "class ToolExecutor");
    source += "\nglobalThis.__ToolExecutor = ToolExecutor;\n";
    const context = vm.createContext({
        console: { log() {}, warn() {}, error() {}, debug() {} },
        structuredClone,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        performance: { now: () => 0 },
    });
    vm.runInContext(source, context, { filename: "web/js/tool_executor.js" });
    return context.__ToolExecutor;
}


function referenceParams() {
    return {
        expected_workflow_identity: "workflow-a",
        expected_graph_hash: "a".repeat(64),
        node_id: 34,
        widget_name: "value",
        value: "new private prompt",
        expected_current_value: "old private prompt",
        expected_reference_node_id: 10,
        expected_reference_image: {
            filename: "character.png",
            subfolder: "references",
            type: "input",
        },
        expected_reference_attestation: {
            sha256: "b".repeat(64),
            size_bytes: 123456,
            width: 3000,
            height: 1800,
        },
    };
}


test("exact prompt transaction verifies one reference byte snapshot immediately before mutation", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    const params = referenceParams();
    const calls = [];
    let prompt = params.expected_current_value;
    const guard = { expectedGraphHash: params.expected_graph_hash };
    executor.flApi = {
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => guard,
        getWorkflowNode: nodeId => ({ node_id: nodeId, values: { value: prompt } }),
        getNodeImageRef: (nodeId) => {
            calls.push(["read-reference", nodeId]);
            return { node_id: nodeId, image: structuredClone(params.expected_reference_image) };
        },
        verifyComfyImageExact: async (image, attestation) => {
            calls.push(["verify-reference", structuredClone(image), structuredClone(attestation)]);
            return structuredClone(attestation);
        },
        assertWorkflowMutationGuard: async value => {
            assert.equal(value, guard);
            calls.push(["reattest-graph"]);
        },
        setValuesExact: async (_nodeId, values) => {
            calls.push(["set-prompt"]);
            prompt = values.value;
            return { applied: ["value"] };
        },
        acceptWorkflowMutationGuard: async () => "c".repeat(64),
    };

    const result = await executor._setNodeValuesExactTransaction(params);

    assert.equal(result.success, true);
    assert.equal(prompt, params.value);
    assert.deepEqual(calls, [
        ["read-reference", 10],
        [
            "verify-reference",
            params.expected_reference_image,
            params.expected_reference_attestation,
        ],
        ["reattest-graph"],
        ["read-reference", 10],
        ["set-prompt"],
    ]);
});


test("same-reference overwrite detected by the browser has zero prompt mutation", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    const params = referenceParams();
    let setCalls = 0;
    let verifyCalls = 0;
    const overwrite = new Error("The reference image bytes changed after inspection.");
    overwrite.code = "reference_image_precondition_failed";
    executor.flApi = {
        pinActiveWorkflow: identity => ({ identity }),
        createWorkflowMutationGuard: async () => ({
            expectedGraphHash: params.expected_graph_hash,
        }),
        getWorkflowNode: nodeId => ({
            node_id: nodeId,
            values: { value: params.expected_current_value },
        }),
        getNodeImageRef: nodeId => ({
            node_id: nodeId,
            image: structuredClone(params.expected_reference_image),
        }),
        verifyComfyImageExact: async () => {
            verifyCalls += 1;
            throw overwrite;
        },
        assertWorkflowMutationGuard: async () => {},
        setValuesExact: async () => {
            setCalls += 1;
            return { applied: ["value"] };
        },
    };

    await assert.rejects(
        executor._setNodeValuesExactTransaction(params),
        error => error?.code === "reference_image_precondition_failed",
    );
    assert.equal(verifyCalls, 1);
    assert.equal(setCalls, 0);
});


test("reference image verification fetches and decodes one no-store blob", async () => {
    const source = await readFile(new URL("web/js/fl_api.js", root), "utf8");
    const helperStart = source.indexOf("async _loadComfyImageExact");
    const helperEnd = source.indexOf("\n    async _loadComfyImageChannel", helperStart);
    const helper = source.slice(helperStart, helperEnd);
    const verifyStart = source.indexOf("async verifyComfyImageExact");
    const verifyEnd = source.indexOf("\n    restoreNestedImageReferences", verifyStart);
    const verify = source.slice(verifyStart, verifyEnd);

    assert.notEqual(helperStart, -1);
    assert.notEqual(verifyStart, -1);
    assert.equal((helper.match(/api\.fetchApi\(/g) || []).length, 1);
    assert.equal((helper.match(/response\.blob\(\)/g) || []).length, 1);
    assert.match(helper, /cache:\s*"no-store"/);
    assert.match(helper, /sha256BlobHex\(blob\)/);
    assert.match(helper, /createImageBitmap\(blob\)/);
    assert.equal((verify.match(/_loadComfyImageExact\(/g) || []).length, 1);
    assert.match(verify, /snapshot\?\.image\?\.close\?\.\(\)/);
});
