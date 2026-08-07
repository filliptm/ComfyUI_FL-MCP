import assert from "node:assert/strict";
import test from "node:test";

import {
    isNearBottom,
    modelProviderSummary,
    starterPrompts,
    summarizeToolStep,
    technicalText,
    toolDisplayImages,
    toolHistorySummary,
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
    assert.equal(summarizeToolStep({
        name: "queue_workflow",
        status: "done",
        result: '{"status":"completed"}',
    }), "Completed workflow");
    assert.equal(summarizeToolStep({
        name: "queue_workflow",
        status: "done",
        result: '{"status":"timeout"}',
    }), "Workflow wait timed out");
    assert.equal(summarizeToolStep({
        name: "node_library_status",
        status: "done",
        result: '{"catalog":{"node_count":1701}}',
    }), "Cataloged 1,701 loaded nodes");
    assert.equal(summarizeToolStep({
        name: "plan_workflow",
        status: "done",
        result: '{"valid":true,"plan":{"nodes":[{},{}]},"error_count":0}',
    }), "Validated workflow plan · 2 nodes");
    assert.equal(summarizeToolStep({
        name: "plan_workflow",
        status: "done",
        result: '{"valid":false,"plan":{"nodes":[{}]},"error_count":2}',
    }), "Plan needs fixes · 2 errors");
    assert.equal(summarizeToolStep({
        name: "apply_workflow_plan",
        status: "done",
        result: '{"success":true,"applied":true,"node_count":2}',
    }), "Applied workflow plan · 2 nodes");
    assert.equal(summarizeToolStep({
        name: "apply_workflow_plan",
        status: "done",
        result: '{"success":false,"rollback":{"attempted":true,"complete":true,"attempted_node_ids":[11,12]}}',
    }), "Workflow apply failed · rolled back 2 nodes");
    assert.equal(summarizeToolStep({
        name: "registry_search_packages",
        status: "done",
        result: '{"count":3,"results":[{},{},{}]}',
    }), "Found 3 Registry packages");
    assert.equal(summarizeToolStep({
        name: "registry_get_package",
        status: "done",
        arguments: '{"request":{"package_id":"example.pack"}}',
        result: '{"ok":true,"package":{"name":"Example Pack","registry_url":"https://registry.comfy.org/example","github_url":"https://github.com/example/pack"}}',
    }), "Inspected Example Pack on Registry");
    assert.equal(summarizeToolStep({
        name: "registry_search_packages",
        status: "failed",
    }), "Couldn’t search official Comfy Registry");
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

    assert.deepEqual(toolDisplayImages({
        name: "view_chat_image",
        result: JSON.stringify({
            image: { filename: "reference.png", subfolder: "ren-chat/session", type: "input" },
            displayImages: [{
                kind: "comfy",
                filename: "reference.png",
                subfolder: "ren-chat/session",
                type: "input",
            }],
        }),
    }), []);
});

test("tool images survive Codex MCP wrappers with null outer structured content", () => {
    const reviewedOutput = {
        success: true,
        selectedOutputIndex: 0,
        availableOutputCount: 1,
        image: { filename: "ComfyUI_00117.png", subfolder: "", type: "output" },
        originalSize: { width: 6336, height: 2688 },
    };
    const result = JSON.stringify({
        content: [{
            type: "text",
            text: JSON.stringify({
                content: [
                    { type: "text", text: JSON.stringify(reviewedOutput) },
                    { type: "image", data: "[image content shown to Ren]" },
                ],
                structuredContent: reviewedOutput,
            }),
        }],
        structuredContent: null,
    });

    assert.deepEqual(toolDisplayImages({ name: "view_output_image", result }), [{
        kind: "comfy",
        filename: "ComfyUI_00117.png",
        subfolder: "",
        type: "output",
        title: "Generated output",
        alt: "Generated ComfyUI output",
    }]);
    assert.equal(summarizeToolStep({
        name: "view_output_image",
        status: "done",
        result,
    }), "Reviewed final output · 6336×2688");
});

test("large tool histories summarize every individual call without grouping", () => {
    const steps = [
        { name: "find_node", status: "done" },
        { name: "find_node", status: "retried" },
        { name: "connect_nodes_batch", status: "running" },
        { name: "queue_workflow", status: "failed" },
        { name: "wait", status: "interrupted" },
    ];

    assert.deepEqual(toolHistorySummary(steps), {
        total: 5,
        running: 1,
        done: 1,
        retried: 1,
        failed: 1,
        interrupted: 1,
        active: steps[2],
    });
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
