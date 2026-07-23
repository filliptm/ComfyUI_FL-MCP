import assert from "node:assert/strict";
import fs from "node:fs/promises";
import test from "node:test";

import {
    findNonOverlappingPosition,
    getGraphInsertionOrigin,
} from "../../web/js/node_placement.js";


test("empty graphs start node placement at the origin", () => {
    assert.deepEqual(getGraphInsertionOrigin([]), { x: 0, y: 0 });
});


test("automatic insertion starts beyond the right edge of the whole graph", () => {
    const origin = getGraphInsertionOrigin([
        { x: -100, y: 50, width: 200, height: 120 },
        { x: 250, y: -40, width: 315, height: 200 },
    ]);

    assert.deepEqual(origin, { x: 629, y: -40 });
});


test("placement uses real node bounds to clear a chain of collisions", () => {
    const position = findNonOverlappingPosition(
        { x: 0, y: 0, width: 315, height: 260 },
        [
            { x: 0, y: 0, width: 210, height: 100 },
            { x: 274, y: 20, width: 480, height: 420 },
        ],
    );

    assert.deepEqual(position, { x: 818, y: 0 });
});


test("non-overlapping preferred positions are preserved", () => {
    const position = findNonOverlappingPosition(
        { x: 100, y: 500, width: 315, height: 200 },
        [{ x: 100, y: 0, width: 500, height: 200 }],
    );

    assert.deepEqual(position, { x: 100, y: 500 });
});


test("embedded and reusable agent instructions enforce rectangle-aware placement", async () => {
    const [chatPrompt, workflowSkill] = await Promise.all([
        fs.readFile(new URL("../../backend/chat_prompt.md", import.meta.url), "utf8"),
        fs.readFile(new URL("../../skills/workflow-assistant/SKILL.md", import.meta.url), "utf8"),
    ]);

    assert.match(chatPrompt, /Treat nodes as rectangles, not points/);
    assert.match(chatPrompt, /final `position` and measured `size`/);
    assert.match(workflowSkill, /Treat nodes as rectangles/);
    assert.match(workflowSkill, /Never place multiple new nodes at the same coordinates/);
    assert.match(workflowSkill, /Collision avoidance may adjust them/);
});
