/**
 * Layout Engine - Automatic node layout algorithms
 *
 * This module provides intelligent automatic layout algorithms for ComfyUI workflows.
 * It analyzes node connections and dimensions to calculate optimal non-overlapping positions.
 *
 * @module layout_engine
 */

import { app } from "../../../../scripts/app.js";

/**
 * LayoutEngine class - Calculates optimal node positions
 */
export class LayoutEngine {
    constructor() {
        this.app = app;

        // Default spacing values (can be modified via multiplier).
        // These are the GAPS between nodes, not including node width/height.
        // Kept compact so auto-layout produces tight, readable results.
        this.baseSpacing = {
            horizontal: 50,
            vertical: 30
        };

        this.spacingMultiplier = 1.0;

        console.log("[LayoutEngine] Initialized");
    }

    /**
     * Set spacing multiplier for all layouts
     * @param {number} multiplier - Spacing multiplier (1.0 = default, 1.5 = 50% more space)
     */
    setSpacingMultiplier(multiplier) {
        this.spacingMultiplier = multiplier;
        console.log(`[LayoutEngine] Spacing multiplier set to ${multiplier}`);
    }

    /**
     * Get current spacing values with multiplier applied
     * @returns {object} Spacing object with horizontal and vertical values
     */
    getSpacing() {
        return {
            horizontal: this.baseSpacing.horizontal * this.spacingMultiplier,
            vertical: this.baseSpacing.vertical * this.spacingMultiplier
        };
    }

    /**
     * Main entry point - arrange nodes using specified strategy
     * @param {Array<number>|null} nodeIds - Node IDs to arrange (null = all nodes)
     * @param {string} strategy - Layout strategy ("flow_horizontal", "flow_vertical", "grid")
     * @param {object} options - Additional options
     * @returns {Array} Layout result with positions for each node
     */
    arrangeNodes(nodeIds, strategy = "flow_horizontal", options = {}) {
        try {
            console.log(`[LayoutEngine] Arranging nodes with strategy: ${strategy}`);

            // Get nodes to arrange
            const nodes = this._getNodes(nodeIds);
            if (nodes.length === 0) {
                console.log("[LayoutEngine] No nodes to arrange");
                return [];
            }

            console.log(`[LayoutEngine] Arranging ${nodes.length} nodes`);

            // Build graph structure
            const graph = this._buildGraph(nodes);

            // Select and execute layout strategy
            let layout;
            switch (strategy) {
                case "flow_horizontal":
                    layout = this._flowHorizontal(graph);
                    break;
                case "flow_vertical":
                    layout = this._flowVertical(graph);
                    break;
                case "grid":
                    layout = this._grid(graph);
                    break;
                default:
                    console.warn(`[LayoutEngine] Unknown strategy "${strategy}", using flow_horizontal`);
                    layout = this._flowHorizontal(graph);
            }

            // Apply layout to actual nodes
            this._applyLayout(layout);

            console.log(`[LayoutEngine] Layout complete: ${layout.length} nodes positioned`);
            return layout;

        } catch (error) {
            console.error("[LayoutEngine] Error arranging nodes:", error);
            throw error;
        }
    }

    /**
     * Pre-calculate positions for nodes that don't exist yet.
     * This allows creating nodes directly at their final positions.
     *
     * NOTE: Because the nodes don't exist yet there are no real edges to
     * trace.  The old implementation passed an empty-edges mockGraph into
     * _flowHorizontal which caused _assignColumns to give every node
     * depth=0 — all nodes piled into column 0 and overlapped.
     *
     * Fix: when no edge hints are present we use a simple sequential
     * horizontal layout instead, which is almost always correct for a
     * freshly-created node chain.
     *
     * @param {Array} nodeSpecs - Array of node specifications with types
     * @param {string} strategy - Layout strategy
     * @returns {Array} Positions for each node [{x, y}, {x, y}, ...]
     */
    preCalculatePositions(nodeSpecs, strategy = "flow_horizontal") {
        try {
            console.log(`[LayoutEngine] Pre-calculating positions for ${nodeSpecs.length} nodes`);

            const spacing = this.getSpacing();

            // Realistic default node dimensions for ComfyUI nodes.
            // (actual sizes vary but these prevent gross overlap at creation time)
            const DEFAULT_NODE_WIDTH = 315;
            const DEFAULT_NODE_HEIGHT = 200;

            // When there are no edge hints, every node would get assigned
            // depth=0 by _assignColumns (no inputs to trace), so they all
            // overlap in the same column.  Fall back to a clean sequential
            // horizontal row instead.
            const hasEdgeHints = nodeSpecs.some(s => s.inputs && s.inputs.length > 0);
            if (!hasEdgeHints) {
                let xPos = 0;
                return nodeSpecs.map(() => {
                    const pos = { x: xPos, y: 0 };
                    xPos += DEFAULT_NODE_WIDTH + spacing.horizontal;
                    return pos;
                });
            }

            // Build a mock graph with estimated dimensions (used when edge
            // hints are available so we can do a proper topological layout)
            const mockGraph = {
                nodes: nodeSpecs.map((spec, index) => ({
                    id: index,
                    type: spec.node_type,
                    width: DEFAULT_NODE_WIDTH,
                    height: DEFAULT_NODE_HEIGHT,
                    inputs: [],
                    outputs: []
                })),
                edges: [],
                nodeMap: new Map()
            };

            // Build nodeMap
            mockGraph.nodes.forEach(node => {
                mockGraph.nodeMap.set(node.id, node);
            });

            // Calculate layout based on strategy
            let layout;
            switch (strategy) {
                case "flow_horizontal":
                    layout = this._flowHorizontal(mockGraph);
                    break;
                case "flow_vertical":
                    layout = this._flowVertical(mockGraph);
                    break;
                case "grid":
                    layout = this._grid(mockGraph);
                    break;
                default:
                    layout = this._flowHorizontal(mockGraph);
            }

            // Extract just x,y positions
            const positions = layout.map(item => ({
                x: item.x,
                y: item.y
            }));

            console.log(`[LayoutEngine] Pre-calculated ${positions.length} positions`);
            return positions;

        } catch (error) {
            console.error("[LayoutEngine] Error pre-calculating positions:", error);
            // Fallback: return simple cascade positions
            return nodeSpecs.map((_, index) => ({
                x: index * 50,
                y: index * 50
            }));
        }
    }

    /**
     * Get nodes from graph
     * @private
     * @param {Array<number>|null} nodeIds - Node IDs (null = all nodes)
     * @returns {Array} Array of LiteGraph node objects
     */
    _getNodes(nodeIds) {
        if (!this.app.graph || !this.app.graph._nodes) {
            return [];
        }

        if (nodeIds === null || nodeIds === undefined) {
            // Return all nodes
            return [...this.app.graph._nodes];
        }

        // Return specific nodes
        const nodes = [];
        for (const id of nodeIds) {
            const node = this.app.graph._nodes.find(n => n.id === id);
            if (node) {
                nodes.push(node);
            }
        }
        return nodes;
    }

    /**
     * Build graph structure with node and connection information
     * @private
     * @param {Array} nodes - LiteGraph node objects
     * @returns {object} Graph structure with nodes and edges
     */
    _buildGraph(nodes) {
        const graph = {
            nodes: [],
            edges: [],
            nodeMap: new Map()
        };

        // Build node list with metadata
        for (const node of nodes) {
            const nodeData = {
                id: node.id,
                title: node.title || node.type,
                type: node.comfyClass || node.type,
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1],
                inputs: [],
                outputs: []
            };

            graph.nodes.push(nodeData);
            graph.nodeMap.set(node.id, nodeData);
        }

        // Build edges (connections)
        for (const node of nodes) {
            if (node.inputs) {
                for (let i = 0; i < node.inputs.length; i++) {
                    const input = node.inputs[i];
                    if (input.link !== null && input.link !== undefined) {
                        const link = this.app.graph.links[input.link];
                        if (link && graph.nodeMap.has(link.origin_id)) {
                            // Add edge
                            graph.edges.push({
                                from: link.origin_id,
                                to: node.id,
                                fromSlot: link.origin_slot,
                                toSlot: i
                            });

                            // Update node metadata
                            const fromNode = graph.nodeMap.get(link.origin_id);
                            const toNode = graph.nodeMap.get(node.id);
                            if (fromNode) fromNode.outputs.push(node.id);
                            if (toNode) toNode.inputs.push(link.origin_id);
                        }
                    }
                }
            }
        }

        // Deduplicate inputs/outputs
        for (const nodeData of graph.nodes) {
            nodeData.inputs = [...new Set(nodeData.inputs)];
            nodeData.outputs = [...new Set(nodeData.outputs)];
        }

        return graph;
    }

    /**
     * Flow horizontal layout (left-to-right)
     * @private
     * @param {object} graph - Graph structure
     * @returns {Array} Layout result
     */
    _flowHorizontal(graph) {
        const spacing = this.getSpacing();

        // 1. Topological sort to determine order
        const sorted = this._topologicalSort(graph);

        // 2. Assign columns based on depth from source nodes
        const columns = this._assignColumns(graph, sorted);

        // 3. Calculate column widths (max node width in each column)
        const columnWidths = [];
        for (let col = 0; col < columns.length; col++) {
            let maxWidth = 0;
            for (const nodeId of columns[col]) {
                const node = graph.nodeMap.get(nodeId);
                if (node && node.width > maxWidth) {
                    maxWidth = node.width;
                }
            }
            columnWidths.push(maxWidth);
        }

        // 4. Calculate x positions for each column
        const columnX = [];
        let xOffset = 0;
        for (let col = 0; col < columnWidths.length; col++) {
            columnX.push(xOffset);
            xOffset += columnWidths[col] + spacing.horizontal;
        }

        // 5. Position nodes within columns (stack vertically)
        const layout = [];
        for (let col = 0; col < columns.length; col++) {
            let yOffset = 0;

            for (const nodeId of columns[col]) {
                const node = graph.nodeMap.get(nodeId);
                if (!node) continue;

                layout.push({
                    node_id: nodeId,
                    x: columnX[col],
                    y: yOffset,
                    width: node.width,
                    height: node.height
                });

                yOffset += node.height + spacing.vertical;
            }
        }

        return layout;
    }

    /**
     * Flow vertical layout (top-to-bottom)
     * @private
     * @param {object} graph - Graph structure
     * @returns {Array} Layout result
     */
    _flowVertical(graph) {
        const spacing = this.getSpacing();

        // Similar to horizontal but rotated 90 degrees
        const sorted = this._topologicalSort(graph);
        const rows = this._assignColumns(graph, sorted); // Reuse column logic for rows

        // Calculate row heights
        const rowHeights = [];
        for (let row = 0; row < rows.length; row++) {
            let maxHeight = 0;
            for (const nodeId of rows[row]) {
                const node = graph.nodeMap.get(nodeId);
                if (node && node.height > maxHeight) {
                    maxHeight = node.height;
                }
            }
            rowHeights.push(maxHeight);
        }

        // Calculate y positions for each row
        const rowY = [];
        let yOffset = 0;
        for (let row = 0; row < rowHeights.length; row++) {
            rowY.push(yOffset);
            yOffset += rowHeights[row] + spacing.vertical;
        }

        // Position nodes within rows (stack horizontally)
        const layout = [];
        for (let row = 0; row < rows.length; row++) {
            let xOffset = 0;

            for (const nodeId of rows[row]) {
                const node = graph.nodeMap.get(nodeId);
                if (!node) continue;

                layout.push({
                    node_id: nodeId,
                    x: xOffset,
                    y: rowY[row],
                    width: node.width,
                    height: node.height
                });

                xOffset += node.width + spacing.horizontal;
            }
        }

        return layout;
    }

    /**
     * Grid layout (simple grid)
     * @private
     * @param {object} graph - Graph structure
     * @returns {Array} Layout result
     */
    _grid(graph) {
        const spacing = this.getSpacing();

        // Calculate grid dimensions
        const nodeCount = graph.nodes.length;
        const cols = Math.ceil(Math.sqrt(nodeCount));

        // Find max node dimensions for uniform grid
        let maxWidth = 0;
        let maxHeight = 0;
        for (const node of graph.nodes) {
            if (node.width > maxWidth) maxWidth = node.width;
            if (node.height > maxHeight) maxHeight = node.height;
        }

        // Position nodes in grid
        const layout = [];
        let col = 0;
        let row = 0;

        for (const node of graph.nodes) {
            layout.push({
                node_id: node.id,
                x: col * (maxWidth + spacing.horizontal),
                y: row * (maxHeight + spacing.vertical),
                width: node.width,
                height: node.height
            });

            col++;
            if (col >= cols) {
                col = 0;
                row++;
            }
        }

        return layout;
    }

    /**
     * Topological sort of nodes
     * @private
     * @param {object} graph - Graph structure
     * @returns {Array} Sorted node IDs
     */
    _topologicalSort(graph) {
        const sorted = [];
        const visited = new Set();
        const visiting = new Set();

        const visit = (nodeId) => {
            if (visited.has(nodeId)) return;
            if (visiting.has(nodeId)) {
                // Cycle detected - just skip for now
                return;
            }

            visiting.add(nodeId);

            const node = graph.nodeMap.get(nodeId);
            if (node) {
                // Visit all inputs first (upstream nodes)
                for (const inputId of node.inputs) {
                    visit(inputId);
                }
            }

            visiting.delete(nodeId);
            visited.add(nodeId);
            sorted.push(nodeId);
        };

        // Visit all nodes
        for (const node of graph.nodes) {
            visit(node.id);
        }

        return sorted;
    }

    /**
     * Assign nodes to columns based on depth from source nodes
     * @private
     * @param {object} graph - Graph structure
     * @param {Array} sorted - Topologically sorted node IDs
     * @returns {Array} Array of columns, each containing node IDs
     */
    _assignColumns(graph, sorted) {
        const depths = new Map();

        // Calculate depth for each node
        const calculateDepth = (nodeId, visited = new Set()) => {
            if (depths.has(nodeId)) {
                return depths.get(nodeId);
            }

            if (visited.has(nodeId)) {
                // Cycle detected
                return 0;
            }

            visited.add(nodeId);

            const node = graph.nodeMap.get(nodeId);
            if (!node || node.inputs.length === 0) {
                // Source node
                depths.set(nodeId, 0);
                return 0;
            }

            // Depth is max depth of inputs + 1
            let maxDepth = -1;
            for (const inputId of node.inputs) {
                const inputDepth = calculateDepth(inputId, new Set(visited));
                if (inputDepth > maxDepth) {
                    maxDepth = inputDepth;
                }
            }

            const depth = maxDepth + 1;
            depths.set(nodeId, depth);
            return depth;
        };

        // Calculate depths
        for (const nodeId of sorted) {
            calculateDepth(nodeId);
        }

        // Group nodes by depth into columns
        const maxDepth = Math.max(...Array.from(depths.values()), 0);
        const columns = [];
        for (let i = 0; i <= maxDepth; i++) {
            columns.push([]);
        }

        for (const [nodeId, depth] of depths.entries()) {
            columns[depth].push(nodeId);
        }

        return columns;
    }

    /**
     * Apply calculated layout to actual nodes
     * @private
     * @param {Array} layout - Layout result
     */
    _applyLayout(layout) {
        for (const item of layout) {
            const node = this.app.graph._nodes.find(n => n.id === item.node_id);
            if (node) {
                node.pos[0] = item.x;
                node.pos[1] = item.y;
                // Note: we don't change size, only position
            }
        }

        // Trigger canvas redraw
        if (this.app.canvas) {
            this.app.canvas.setDirty(true, true);
        }
    }
}
