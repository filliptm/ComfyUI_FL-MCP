import assert from "node:assert/strict";
import test from "node:test";

import {
    formatImageWidgetRef,
    nestedImageRefForNode,
    normalizeMaskRegion,
    parseImageWidgetRef,
    summarizeMaskPixels,
} from "../../web/js/mask_utils.js";


test("image widget references preserve ComfyUI type and subfolder", () => {
    assert.deepEqual(
        parseImageWidgetRef("fl_mcp_masks/mask.png [input]"),
        { filename: "mask.png", subfolder: "fl_mcp_masks", type: "input" },
    );
    assert.deepEqual(
        parseImageWidgetRef({ filename: "result.png", type: "output" }),
        { filename: "result.png", subfolder: "", type: "output" },
    );
    assert.equal(parseImageWidgetRef("$35-0"), null);
});


test("nested image references survive workflow node rehydration", () => {
    const node = {
        properties: { image: "ren-chat/session-1/reference.png [input]" },
        widgets: [{ name: "image", value: "reference.png" }],
        widgets_values: ["reference.png"],
    };

    const restored = nestedImageRefForNode(node);

    assert.deepEqual(restored, {
        filename: "reference.png",
        subfolder: "ren-chat/session-1",
        type: "input",
    });
    assert.equal(
        formatImageWidgetRef(restored),
        "ren-chat/session-1/reference.png [input]",
    );
});


test("normalized mask regions map to image pixels", () => {
    assert.deepEqual(
        normalizeMaskRegion(
            { x: 0.25, y: 0.1, width: 0.5, height: 0.4, shape: "ellipse", operation: "erase" },
            "normalized",
            1000,
            500,
        ),
        {
            x: 250,
            y: 50,
            width: 500,
            height: 200,
            shape: "ellipse",
            operation: "erase",
            feather: 0,
        },
    );
    assert.throws(
        () => normalizeMaskRegion({ x: 900, y: 0, width: 200, height: 10 }, "pixels", 1000, 500),
        /outside/,
    );
});


test("mask pixel summaries report weighted coverage and bounds", () => {
    const imageData = {
        width: 3,
        height: 2,
        data: new Uint8ClampedArray(3 * 2 * 4),
    };
    imageData.data[(1 * 3 + 1) * 4 + 3] = 255;
    imageData.data[(1 * 3 + 2) * 4 + 3] = 128;

    const summary = summarizeMaskPixels(imageData);

    assert.equal(summary.coverage_percent, 25.033);
    assert.deepEqual(summary.bounds, { x: 1, y: 1, width: 2, height: 1 });
});
