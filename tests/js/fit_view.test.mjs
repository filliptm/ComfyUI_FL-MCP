import assert from "node:assert/strict";
import test from "node:test";

import {
    fitBoundsToViewport,
    installRenAwareFitView,
} from "../../web/js/fit_view.js";


function nativeResult(bounds, contentBounds, canvasWidth, canvasHeight, zoom = 0.75) {
    const scale = Math.min(
        zoom * canvasWidth / Math.max(bounds[2], 300),
        zoom * canvasHeight / Math.max(bounds[3], 300),
        10,
    );
    const offset = [
        -bounds[0] - bounds[2] / 2 + canvasWidth / 2 / scale,
        -bounds[1] - bounds[3] / 2 + canvasHeight / 2 / scale,
    ];
    return {
        scale,
        center: [
            (contentBounds[0] + contentBounds[2] / 2 + offset[0]) * scale,
            (contentBounds[1] + contentBounds[3] / 2 + offset[1]) * scale,
        ],
    };
}


test("fit bounds use the visible canvas beside a right-side Ren panel", () => {
    const viewport = {
        canvasWidth: 1200,
        canvasHeight: 800,
        left: 0,
        top: 0,
        width: 800,
        height: 800,
    };
    const bounds = [100, 100, 400, 200];
    const adjusted = fitBoundsToViewport(bounds, viewport);
    const fitted = nativeResult(
        adjusted,
        bounds,
        viewport.canvasWidth,
        viewport.canvasHeight,
    );

    assert.equal(fitted.scale, 1.5);
    assert.deepEqual(fitted.center.map(Math.round), [400, 400]);
});


test("fit bounds center nodes to the right of a left-side Ren panel", () => {
    const viewport = {
        canvasWidth: 1200,
        canvasHeight: 800,
        left: 400,
        top: 0,
        width: 800,
        height: 800,
    };
    const bounds = [100, 100, 400, 200];
    const adjusted = fitBoundsToViewport(bounds, viewport);
    const fitted = nativeResult(
        adjusted,
        bounds,
        viewport.canvasWidth,
        viewport.canvasHeight,
    );

    assert.equal(fitted.scale, 1.5);
    assert.deepEqual(fitted.center.map(Math.round), [800, 400]);
});


test("native Fit View uses the current ComfyUI splitter hierarchy", () => {
    const calls = [];
    const canvasElement = {
        getBoundingClientRect: () => ({
            left: 0,
            top: 0,
            right: 1200,
            bottom: 800,
            width: 1200,
            height: 800,
        }),
    };
    const graphPanel = {
        getBoundingClientRect: () => ({
            left: 0,
            top: 0,
            right: 800,
            bottom: 800,
            width: 800,
            height: 800,
        }),
    };
    const splitter = {
        parentElement: null,
        querySelector: (selector) => (
            selector === ".graph-canvas-panel" ? graphPanel : null
        ),
    };
    const sidebarPanel = {
        parentElement: splitter,
        querySelector: () => null,
    };
    const host = {
        parentElement: sidebarPanel,
    };
    const app = {
        canvas: {
            canvas: canvasElement,
            ds: { max_scale: 10 },
            selectedItems: new Set([{
                boundingRect: [100, 100, 400, 200],
            }]),
            positionableItems: [],
            fitViewToSelectionAnimated: () => calls.push("native"),
            animateToBounds: (bounds) => calls.push(bounds),
        },
    };
    let visibleHost = host;
    const root = { querySelector: () => visibleHost };

    assert.equal(installRenAwareFitView(app, root), true);
    app.canvas.fitViewToSelectionAnimated();
    assert.equal(calls.length, 1);
    assert.ok(Array.isArray(calls[0]));

    visibleHost = null;
    app.canvas.fitViewToSelectionAnimated();
    assert.deepEqual(calls, [calls[0], "native"]);
    assert.equal(installRenAwareFitView(app, root), false);
});


test("Fit View falls back to a document-level graph panel for portal layouts", () => {
    const calls = [];
    const canvasElement = {
        getBoundingClientRect: () => ({
            left: 0,
            top: 0,
            right: 1200,
            bottom: 800,
            width: 1200,
            height: 800,
        }),
    };
    const graphPanel = {
        getBoundingClientRect: () => ({
            left: 400,
            top: 0,
            right: 1200,
            bottom: 800,
            width: 800,
            height: 800,
        }),
    };
    const host = { parentElement: null };
    const root = {
        querySelector: (selector) => {
            if (selector === ".fl-chat-panel-host") return host;
            if (selector === ".graph-canvas-panel") return graphPanel;
            return null;
        },
    };
    const app = {
        canvas: {
            canvas: canvasElement,
            ds: { max_scale: 10 },
            selectedItems: new Set([{
                boundingRect: [100, 100, 400, 200],
            }]),
            positionableItems: [],
            fitViewToSelectionAnimated: () => calls.push("native"),
            animateToBounds: (bounds) => calls.push(bounds),
        },
    };

    assert.equal(installRenAwareFitView(app, root), true);
    app.canvas.fitViewToSelectionAnimated();

    assert.equal(calls.length, 1);
    assert.ok(Array.isArray(calls[0]));
    const fitted = nativeResult(calls[0], [90, 90, 420, 220], 1200, 800);
    assert.deepEqual(fitted.center.map(Math.round), [800, 400]);
});
