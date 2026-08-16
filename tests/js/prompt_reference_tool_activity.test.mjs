import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { summarizeToolStep } from "../../web/js/chat_ui_helpers.js";
import { getToolConfig } from "../../web/js/tool_activity.js";


const root = new URL("../../", import.meta.url);


test("prompt-reference tools have concise activity labels and exact outcome summaries", () => {
    assert.deepEqual(getToolConfig("view_node_mask"), {
        icon: "🎭",
        label: "Inspect Image for Masking",
        description: "Loading the source image and current alpha; an empty mask is valid",
        iconClass: "pi pi-eye",
        runningLabel: "Inspecting source image for masking",
    });
    assert.deepEqual(getToolConfig("view_prompt_reference_image"), {
        icon: "👁️",
        label: "Inspect Reference",
        description: "Loading the exact image connected to the reference input",
        iconClass: "pi pi-eye",
        runningLabel: "Inspecting connected reference image",
    });
    assert.deepEqual(getToolConfig("update_connected_prompt"), {
        icon: "✍️",
        label: "Update Prompt",
        description: "Updating the exact connected prompt value",
        iconClass: "pi pi-pencil",
        runningLabel: "Updating connected prompt",
    });

    assert.equal(summarizeToolStep({
        name: "view_prompt_reference_image",
        status: "done",
        result: JSON.stringify([{
            type: "text",
            text: JSON.stringify({ consumer_input: "image_2", producer_node_id: 33 }),
        }, {
            type: "image",
            data: "[image content shown to Ren]",
        }]),
    }), "Inspected image connected to image_2");
    assert.equal(summarizeToolStep({
        name: "update_connected_prompt",
        status: "done",
        result: JSON.stringify({ producer_node_id: 34, verified: true }),
    }), "Updated exact prompt on node 34");

    assert.equal(summarizeToolStep({
        name: "view_prompt_reference_image",
        status: "failed",
    }), "Couldn’t inspect reference image");
    assert.equal(summarizeToolStep({
        name: "update_connected_prompt",
        status: "failed",
    }), "Couldn’t update connected prompt");
});


test("README prompt-reference inventory and advertised count match public MCP tools", async () => {
    const [readme, server] = await Promise.all([
        readFile(new URL("README.md", root), "utf8"),
        readFile(new URL("backend/mcp_server.py", root), "utf8"),
    ]);
    const publicToolCount = [...server.matchAll(/^@mcp\.tool\(\)/gm)].length;
    assert.ok(publicToolCount > 0, "expected public MCP tool decorators");

    const highlightedCount = Number(readme.match(/\*\*(\d+) MCP tools\*\*/)?.[1]);
    const inventoryCount = Number(
        readme.match(/FL-MCP currently exposes \*\*(\d+) tools\*\*/)?.[1],
    );
    assert.equal(highlightedCount, publicToolCount);
    assert.equal(inventoryCount, publicToolCount);

    for (const toolName of ["view_prompt_reference_image", "update_connected_prompt"]) {
        assert.match(server, new RegExp(
            `@mcp\\.tool\\(\\)\\nasync def ${toolName}\\(`,
        ));
        const inventoryRows = readme.match(
            new RegExp("^\\| `" + toolName + "` \\|", "gm"),
        ) || [];
        assert.equal(
            inventoryRows.length,
            1,
            `expected exactly one README inventory row for ${toolName}`,
        );
    }
});
