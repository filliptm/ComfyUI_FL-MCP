import assert from "node:assert/strict";
import test from "node:test";

import {
    imageRefsEqual,
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
    assert.deepEqual(
        parseImageWidgetRef("blake3:abc123"),
        { filename: "blake3:abc123", subfolder: "", type: "input" },
    );
});


test("image references compare normalized type and subfolder defaults", () => {
    assert.equal(
        imageRefsEqual(
            { filename: "mask.png" },
            { filename: "mask.png", subfolder: "", type: "input" },
        ),
        true,
    );
    assert.equal(
        imageRefsEqual(
            { filename: "blake3:source" },
            { filename: "fl-mcp-mask.png" },
        ),
        false,
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
