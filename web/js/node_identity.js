/**
 * Compare ComfyUI node IDs across workflows that serialize IDs as either
 * numbers or strings.
 */
export function nodeIdsEqual(left, right) {
    if (left === null || left === undefined || right === null || right === undefined) {
        return false;
    }
    return String(left) === String(right);
}
