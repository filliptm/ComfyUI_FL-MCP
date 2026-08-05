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


export function nodeMatchesQuery(node, query) {
    if (typeof query === "object" && query?.by) {
        if (query.by === "id") {
            return nodeIdsEqual(node.id, query.value);
        }
        if (query.by === "title") {
            return node.title === query.value;
        }
        if (query.by === "type") {
            return node.type === query.value || node.comfyClass === query.value;
        }
        return false;
    }

    if (typeof query === "number") {
        return nodeIdsEqual(node.id, query);
    }
    if (typeof query === "string") {
        return nodeIdsEqual(node.id, query) ||
            node.title === query ||
            node.type === query ||
            node.comfyClass === query;
    }
    return false;
}
