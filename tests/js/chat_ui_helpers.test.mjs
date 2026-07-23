import assert from "node:assert/strict";
import test from "node:test";

import {
    canStackToolSteps,
    isNearBottom,
    modelProviderSummary,
    starterPrompts,
    summarizeToolStep,
    technicalText,
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
