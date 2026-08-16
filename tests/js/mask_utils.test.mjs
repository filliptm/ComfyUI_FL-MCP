import assert from "node:assert/strict";
import test from "node:test";

import {
    buildExactMaskComposeFormData,
    drawMaskRegionPath,
    formatImageWidgetRef,
    MASK_POLYGON_MAX_POINTS,
    MASK_POLYGON_MIN_POINTS,
    nestedImageRefForNode,
    normalizeMaskRegion,
    parseImageWidgetRef,
    summarizeMaskPixels,
} from "../../web/js/mask_utils.js";


test("exact mask compose form sends the attested source Blob and alpha separately", async () => {
    const source = new Blob([Uint8Array.from([137, 80, 78, 71, 1, 2, 3])], {
        type: "image/png",
    });
    const alpha = new Blob([Uint8Array.from([137, 80, 78, 71, 9, 8, 7])], {
        type: "image/png",
    });
    const attestation = {
        sha256: "a".repeat(64),
        size_bytes: source.size,
        width: 3584,
        height: 1536,
    };

    const form = buildExactMaskComposeFormData(source, alpha, attestation);

    assert.deepEqual(
        Array.from(new Uint8Array(await form.get("source").arrayBuffer())),
        Array.from(new Uint8Array(await source.arrayBuffer())),
    );
    assert.deepEqual(
        Array.from(new Uint8Array(await form.get("alpha").arrayBuffer())),
        Array.from(new Uint8Array(await alpha.arrayBuffer())),
    );
    assert.equal(form.get("expected_sha256"), attestation.sha256);
    assert.equal(form.get("expected_size_bytes"), String(source.size));
    assert.equal(form.get("expected_width"), "3584");
    assert.equal(form.get("expected_height"), "1536");
    assert.deepEqual([...form.keys()], [
        "source",
        "alpha",
        "expected_sha256",
        "expected_size_bytes",
        "expected_width",
        "expected_height",
    ]);
});


test("exact mask compose form rejects a Blob that no longer matches attested size", () => {
    const source = new Blob(["source"], { type: "image/png" });
    const alpha = new Blob(["alpha"], { type: "image/png" });

    assert.throws(
        () => buildExactMaskComposeFormData(source, alpha, {
            sha256: "b".repeat(64),
            size_bytes: source.size + 1,
            width: 10,
            height: 10,
        }),
        /attestation is invalid/,
    );
});


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


test("polygon mask regions scale deterministically and retain exact points", () => {
    assert.equal(MASK_POLYGON_MIN_POINTS, 3);
    assert.equal(MASK_POLYGON_MAX_POINTS, 64);
    assert.deepEqual(
        normalizeMaskRegion(
            {
                shape: "polygon",
                x: null,
                y: null,
                width: null,
                height: null,
                points: [
                    { x: 0.1, y: 0.2 },
                    { x: 0.8, y: 0.25 },
                    { x: 0.6, y: 0.9 },
                ],
                operation: "erase",
                feather: 4,
            },
            "normalized",
            1000,
            500,
        ),
        {
            x: 100,
            y: 100,
            width: 700,
            height: 350,
            points: [
                { x: 100, y: 100 },
                { x: 800, y: 125 },
                { x: 600, y: 450 },
            ],
            shape: "polygon",
            operation: "erase",
            feather: 4,
        },
    );
});


test("polygon mask regions reject ambiguous, unbounded, and degenerate geometry", () => {
    const polygon = points => ({ shape: "polygon", points });
    assert.throws(
        () => normalizeMaskRegion(
            polygon([{ x: 0, y: 0 }, { x: 1, y: 1 }]),
            "pixels",
            100,
            100,
        ),
        /require 3-64 points/,
    );
    assert.throws(
        () => normalizeMaskRegion(
            polygon(Array.from({ length: 65 }, (_, index) => ({
                x: 50 + 20 * Math.cos(index * Math.PI * 2 / 65),
                y: 50 + 20 * Math.sin(index * Math.PI * 2 / 65),
            }))),
            "pixels",
            100,
            100,
        ),
        /require 3-64 points/,
    );
    assert.throws(
        () => normalizeMaskRegion(
            polygon([{ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 20, y: 20 }]),
            "pixels",
            100,
            100,
        ),
        /nonzero area/,
    );
    assert.throws(
        () => normalizeMaskRegion(
            polygon([{ x: 0, y: 0 }, { x: 101, y: 0 }, { x: 0, y: 10 }]),
            "pixels",
            100,
            100,
        ),
        /falls outside/,
    );
    assert.throws(
        () => normalizeMaskRegion(
            polygon([{ x: 0, y: 0 }, { x: Number.NaN, y: 10 }, { x: 10, y: 0 }]),
            "pixels",
            100,
            100,
        ),
        /finite number/,
    );
    assert.throws(
        () => normalizeMaskRegion(
            {
                ...polygon([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 0, y: 10 }]),
                x: 0,
            },
            "pixels",
            100,
            100,
        ),
        /use points instead/,
    );
    assert.throws(
        () => normalizeMaskRegion(
            {
                shape: "rectangle",
                x: 0,
                y: 0,
                width: 10,
                height: 10,
                points: [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 0, y: 10 }],
            },
            "pixels",
            100,
            100,
        ),
        /cannot include polygon points/,
    );
});


test("polygon paths close deterministically while rectangle and ellipse drawing stay intact", () => {
    const calls = [];
    const context = Object.fromEntries(
        ["beginPath", "moveTo", "lineTo", "closePath", "ellipse", "rect"].map(name => [
            name,
            (...args) => calls.push([name, ...args]),
        ]),
    );
    drawMaskRegionPath(context, {
        shape: "polygon",
        points: [{ x: 2, y: 3 }, { x: 8, y: 4 }, { x: 5, y: 9 }],
    });
    assert.deepEqual(calls, [
        ["beginPath"],
        ["moveTo", 2, 3],
        ["lineTo", 8, 4],
        ["lineTo", 5, 9],
        ["closePath"],
    ]);

    calls.length = 0;
    drawMaskRegionPath(context, {
        shape: "rectangle",
        x: 1,
        y: 2,
        width: 3,
        height: 4,
    });
    assert.deepEqual(calls, [["beginPath"], ["rect", 1, 2, 3, 4]]);

    calls.length = 0;
    drawMaskRegionPath(context, {
        shape: "ellipse",
        x: 10,
        y: 20,
        width: 30,
        height: 40,
    });
    assert.deepEqual(calls, [
        ["beginPath"],
        ["ellipse", 25, 40, 15, 20, 0, 0, Math.PI * 2],
    ]);
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
