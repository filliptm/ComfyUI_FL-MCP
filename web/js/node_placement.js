/**
 * Pure geometry helpers for placing newly-created ComfyUI nodes.
 *
 * LiteGraph gives newly-created nodes a shared default position. These helpers
 * use the node's measured rectangle to keep creation deterministic and
 * collision-free without requiring the model to guess node dimensions.
 */

export const DEFAULT_NODE_GAP = Object.freeze({
    horizontal: 64,
    vertical: 32,
});

function finiteNumber(value, fallback = 0) {
    return Number.isFinite(value) ? value : fallback;
}

function normalizeRect(rect) {
    return {
        x: finiteNumber(rect?.x),
        y: finiteNumber(rect?.y),
        width: Math.max(1, finiteNumber(rect?.width, 1)),
        height: Math.max(1, finiteNumber(rect?.height, 1)),
    };
}

function overlapsWithGap(candidate, occupied, gap) {
    return (
        candidate.x < occupied.x + occupied.width + gap.horizontal
        && candidate.x + candidate.width + gap.horizontal > occupied.x
        && candidate.y < occupied.y + occupied.height + gap.vertical
        && candidate.y + candidate.height + gap.vertical > occupied.y
    );
}

/**
 * Choose an insertion point immediately to the right of the current graph.
 */
export function getGraphInsertionOrigin(occupiedRects, gap = DEFAULT_NODE_GAP) {
    const occupied = (occupiedRects || []).map(normalizeRect);
    if (occupied.length === 0) {
        return { x: 0, y: 0 };
    }

    return {
        x: Math.max(...occupied.map(rect => rect.x + rect.width)) + gap.horizontal,
        y: Math.min(...occupied.map(rect => rect.y)),
    };
}

/**
 * Move a candidate rectangle right until it clears every occupied rectangle.
 *
 * Each iteration clears at least one blocker, so the loop is bounded by the
 * number of occupied rectangles plus a defensive final pass.
 */
export function findNonOverlappingPosition(candidateRect, occupiedRects, gap = DEFAULT_NODE_GAP) {
    const candidate = normalizeRect(candidateRect);
    const occupied = (occupiedRects || []).map(normalizeRect);
    const maxPasses = occupied.length + 2;

    for (let pass = 0; pass < maxPasses; pass += 1) {
        const blockers = occupied.filter(rect => overlapsWithGap(candidate, rect, gap));
        if (blockers.length === 0) {
            return { x: candidate.x, y: candidate.y };
        }

        candidate.x = Math.max(
            candidate.x + 1,
            ...blockers.map(rect => rect.x + rect.width + gap.horizontal),
        );
    }

    // Defensive fallback for malformed geometry: place it beyond the graph.
    const fallback = getGraphInsertionOrigin(occupied, gap);
    return {
        x: Math.max(candidate.x, fallback.x),
        y: candidate.y,
    };
}
