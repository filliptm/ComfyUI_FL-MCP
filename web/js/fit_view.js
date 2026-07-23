const FIT_VIEW_PATCH = Symbol.for("fl-mcp-fit-view");

function itemBounds(items, padding = 10) {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    for (const item of items) {
        const rect = item?.boundingRect;
        if (!rect) continue;
        minX = Math.min(minX, rect[0]);
        minY = Math.min(minY, rect[1]);
        maxX = Math.max(maxX, rect[0] + rect[2]);
        maxY = Math.max(maxY, rect[1] + rect[3]);
    }
    if (![minX, minY, maxX, maxY].every(Number.isFinite)) return null;
    return [
        minX - padding,
        minY - padding,
        maxX - minX + padding * 2,
        maxY - minY + padding * 2,
    ];
}

export function fitBoundsToViewport(bounds, viewport, options = {}) {
    const {
        canvasWidth,
        canvasHeight,
        left,
        top,
        width,
        height,
    } = viewport;
    const zoom = options.zoom ?? 0.75;
    const maxScale = options.maxScale ?? 10;
    if (
        !(zoom > 0)
        || ![canvasWidth, canvasHeight, left, top, width, height]
            .every(Number.isFinite)
        || canvasWidth <= 0
        || canvasHeight <= 0
        || width <= 0
        || height <= 0
    ) {
        return null;
    }

    const targetScale = Math.min(
        zoom * width / Math.max(bounds[2], 300),
        zoom * height / Math.max(bounds[3], 300),
        maxScale,
    );
    if (!(targetScale > 0)) return null;

    const fittedWidth = zoom * canvasWidth / targetScale;
    const fittedHeight = zoom * canvasHeight / targetScale;
    const boundsCenterX = bounds[0] + bounds[2] / 2;
    const boundsCenterY = bounds[1] + bounds[3] / 2;
    const canvasCenterX = canvasWidth / 2;
    const canvasCenterY = canvasHeight / 2;
    const viewportCenterX = left + width / 2;
    const viewportCenterY = top + height / 2;
    const fittedCenterX = boundsCenterX
        + (canvasCenterX - viewportCenterX) / targetScale;
    const fittedCenterY = boundsCenterY
        + (canvasCenterY - viewportCenterY) / targetScale;

    return [
        fittedCenterX - fittedWidth / 2,
        fittedCenterY - fittedHeight / 2,
        fittedWidth,
        fittedHeight,
    ];
}

function findGraphPanel(host, root) {
    // ComfyUI has changed the splitter wrapper classes across frontend releases.
    // Walk upward until we reach the first common layout ancestor instead of
    // depending on a private wrapper class such as `.splitter-overlay-root`.
    let ancestor = host.parentElement;
    while (ancestor) {
        const graphPanel = ancestor.querySelector?.(".graph-canvas-panel");
        if (graphPanel) return graphPanel;
        ancestor = ancestor.parentElement;
    }

    // Keep a document-level fallback for hosts rendered through a portal and
    // for older ComfyUI frontend layouts.
    return root.querySelector?.(".graph-canvas-panel") || null;
}

function renViewport(app, root) {
    const host = root.querySelector(".fl-chat-panel-host");
    const graphPanel = host && findGraphPanel(host, root);
    const canvasElement = app.canvas?.canvas;
    if (!host || !graphPanel || !canvasElement) return null;

    const canvasRect = canvasElement.getBoundingClientRect();
    const graphRect = graphPanel.getBoundingClientRect();
    const visibleLeft = Math.max(canvasRect.left, graphRect.left);
    const visibleTop = Math.max(canvasRect.top, graphRect.top);
    const visibleRight = Math.min(canvasRect.right, graphRect.right);
    const visibleBottom = Math.min(canvasRect.bottom, graphRect.bottom);
    const width = visibleRight - visibleLeft;
    const height = visibleBottom - visibleTop;
    if (width <= 0 || height <= 0) return null;

    const viewport = {
        canvasWidth: canvasRect.width,
        canvasHeight: canvasRect.height,
        left: visibleLeft - canvasRect.left,
        top: visibleTop - canvasRect.top,
        width,
        height,
    };
    const isFullCanvas = (
        Math.abs(viewport.left) < 1
        && Math.abs(viewport.top) < 1
        && Math.abs(viewport.width - viewport.canvasWidth) < 1
        && Math.abs(viewport.height - viewport.canvasHeight) < 1
    );
    return isFullCanvas ? null : viewport;
}

export function installRenAwareFitView(app, root = document) {
    const canvas = app.canvas;
    if (
        !canvas
        || canvas[FIT_VIEW_PATCH]
        || typeof canvas.fitViewToSelectionAnimated !== "function"
    ) {
        return false;
    }

    const nativeFitView = canvas.fitViewToSelectionAnimated;
    canvas.fitViewToSelectionAnimated = function (options = {}) {
        const viewport = renViewport(app, root);
        if (!viewport || typeof this.animateToBounds !== "function") {
            return nativeFitView.call(this, options);
        }

        const items = this.selectedItems?.size
            ? Array.from(this.selectedItems)
            : Array.from(this.positionableItems || []);
        const bounds = itemBounds(items);
        const adjustedBounds = bounds && fitBoundsToViewport(bounds, viewport, {
            zoom: options?.zoom,
            maxScale: this.ds?.max_scale,
        });
        if (!adjustedBounds) return nativeFitView.call(this, options);
        return this.animateToBounds(adjustedBounds, options);
    };
    canvas[FIT_VIEW_PATCH] = nativeFitView;
    return true;
}
