import assert from "node:assert/strict";
import test from "node:test";

import {
    canStackToolSteps,
    isNearBottom,
    modelProviderSummary,
    starterPrompts,
    summarizeToolStep,
    technicalText,
    toolDisplayImages,
    toolStackState,
} from "../../web/js/chat_ui_helpers.js";


test("near-bottom detection uses the 48px follow threshold", () => {
    assert.equal(isNearBottom({
        scrollHeight: 1000,
        scrollTop: 652,
        clientHeight: 300,
    }), true);
    assert.equal(isNearBottom({
        scrollHeight: 1000,
        scrollTop: 651,
        clientHeight: 300,
    }), false);
});

test("model provider summary presents compact provider and model identity", () => {
    assert.deepEqual(modelProviderSummary({
        provider: "claude_subscription",
        model: "sonnet",
        presets: {
            claude_subscription: {
                label: "Claude subscription",
                default_model: "sonnet",
                models: [{ id: "sonnet", label: "Claude Sonnet (recommended)" }],
            },
        },
    }), {
        id: "claude_subscription",
        mark: "CL",
        providerLabel: "Claude",
        modelLabel: "Claude Sonnet",
    });

    assert.deepEqual(modelProviderSummary({
        provider: "custom",
        model: "private-model",
        presets: { custom: { label: "Custom endpoint" } },
    }), {
        id: "custom",
        mark: "API",
        providerLabel: "Custom",
        modelLabel: "private-model",
    });
});


test("tool summaries expose human outcomes for core canvas operations", () => {
    assert.equal(summarizeToolStep({
        name: "create_nodes",
        status: "done",
        arguments: '{"nodes":[{},{}]}',
    }), "Created 2 nodes");
    assert.equal(summarizeToolStep({
        name: "connect_nodes_batch",
        status: "done",
        result: '{"results":[{"success":true},{"success":false}]}',
    }), "Connected 1 link");
    assert.equal(summarizeToolStep({
        name: "workflow_overview",
        status: "done",
        result: '{"node_count":12}',
    }), "Inspected 12 nodes");
});

test("image review and mask summaries report the visible outcome", () => {
    assert.equal(summarizeToolStep({
        name: "view_output_image",
        status: "done",
        result: JSON.stringify({
            selectedOutputIndex: 3,
            availableOutputCount: 4,
            originalSize: { width: 768, height: 768 },
        }),
    }), "Reviewed final output · 768×768");

    const maskResult = [{
        type: "text",
        text: JSON.stringify({
            node_id: 12,
            title: "LOAD & MASK IMAGE",
            mask: { coveragePercent: 4 },
        }),
    }, {
        type: "image",
        data: "[image content shown to Ren]",
    }];
    assert.equal(summarizeToolStep({
        name: "view_node_mask",
        status: "done",
        result: JSON.stringify(maskResult),
    }), "Inspected mask on LOAD & MASK IMAGE · 4% covered");

    assert.equal(summarizeToolStep({
        name: "edit_node_mask",
        status: "done",
        arguments: JSON.stringify({ request: {
            clear_existing: true,
            regions: [{ operation: "paint" }, { operation: "paint" }],
        } }),
        result: JSON.stringify({ mask: { coveragePercent: 3.125 } }),
    }), "Replaced mask with 2 mask regions · 3.13% covered");

    assert.equal(summarizeToolStep({
        name: "view_output_image",
        status: "failed",
    }), "Couldn’t review output image");

    assert.equal(summarizeToolStep({
        name: "confirm_mask_review",
        status: "done",
    }), "Mask approved for workflow");
});

test("web research summaries identify provider, cost, and fetched content", () => {
    assert.equal(summarizeToolStep({
        name: "web_search",
        status: "done",
        result: JSON.stringify({
            provider: "free",
            results: [{}, {}, {}],
            credits_used: 0,
        }),
    }), "Searched Free web · 3 sources");
    assert.equal(summarizeToolStep({
        name: "web_search",
        status: "done",
        result: JSON.stringify({
            provider: "tavily",
            results: [{}],
            credits_used: 2,
        }),
    }), "Searched Tavily · 1 source · 2 credits");
    assert.equal(summarizeToolStep({
        name: "web_fetch_page",
        status: "done",
        result: JSON.stringify({
            title: "Example reference",
            contentLength: 12345,
            fromCache: true,
            images: [{ url: "https://example.com/one.png" }, { url: "https://example.com/two.png" }],
        }),
    }), "Read Example reference from cache · 12,345 chars · 2 images");
    assert.equal(summarizeToolStep({
        name: "web_search",
        status: "failed",
    }), "Couldn’t search the web");
});

test("tool image candidates preserve source order and reject unsafe URLs", () => {
    assert.deepEqual(toolDisplayImages({
        name: "web_fetch_page",
        result: JSON.stringify({
            images: [{
                url: "https://images.example/hero.png",
                source_url: "https://example.com/article",
                alt: "Primary reference",
                width: 1200,
                height: 800,
            }, {
                url: "javascript:alert(1)",
                alt: "Unsafe",
            }, {
                url: "https://images.example/hero.png",
                alt: "Duplicate",
            }, {
                url: "https://images.example/detail.webp",
                title: "Detail",
            }],
        }),
    }), [{
        kind: "web",
        url: "https://images.example/hero.png",
        sourceUrl: "https://example.com/article",
        title: "",
        alt: "Primary reference",
        width: 1200,
        height: 800,
    }, {
        kind: "web",
        url: "https://images.example/detail.webp",
        sourceUrl: "",
        title: "Detail",
        alt: "Detail",
        width: null,
        height: null,
    }]);

    assert.deepEqual(toolDisplayImages({
        name: "view_output_image",
        result: JSON.stringify({
            image: { filename: "final.png", subfolder: "run", type: "output" },
        }),
    }), [{
        kind: "comfy",
        filename: "final.png",
        subfolder: "run",
        type: "output",
        title: "Generated output",
        alt: "Generated ComfyUI output",
    }]);
});

test("consecutive identical tool calls stack and retain the strongest state", () => {
    assert.equal(
        canStackToolSteps({ name: "modify_layout" }, { name: "modify_layout" }),
        true,
    );
    assert.equal(
        canStackToolSteps({ name: "modify_layout" }, { name: "workflow_overview" }),
        false,
    );

    const completed = toolStackState([
        { name: "modify_layout", status: "done", result: "one" },
        { name: "modify_layout", status: "done", result: "two" },
        { name: "modify_layout", status: "done", result: "three" },
    ]);
    assert.equal(completed.count, 3);
    assert.equal(completed.status, "done");
    assert.equal(completed.step.result, "three");

    const mixed = toolStackState([
        { name: "modify_layout", status: "done" },
        { name: "modify_layout", status: "failed" },
        { name: "modify_layout", status: "running" },
    ]);
    assert.equal(mixed.status, "running");
});


test("technical detail is capped in the interface only", () => {
    const original = "x".repeat(20_100);
    const displayed = technicalText(original);

    assert.ok(displayed.length < original.length + 100);
    assert.match(displayed, /truncated in the interface/);
    assert.equal(original.length, 20_100);
});


test("starter prompts reflect connection, selection, and canvas population", () => {
    assert.match(starterPrompts({ connected: false })[0], /reconnect/i);
    assert.match(starterPrompts({
        connected: true,
        nodeCount: 4,
        selectedCount: 2,
    })[0], /selected/i);
    assert.match(starterPrompts({
        connected: true,
        nodeCount: 4,
        selectedCount: 0,
    })[0], /workflow/i);
    assert.match(starterPrompts({
        connected: true,
        nodeCount: 0,
        selectedCount: 0,
    })[0], /text-to-image/i);
});
