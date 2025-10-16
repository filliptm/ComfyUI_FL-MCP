/**
 * FL_API - Clean wrapper around legacy FL_JS functions
 * 
 * This module provides a promise-based API for interacting with ComfyUI workflows
 * through the legacy FL_JS functions. It handles type conversions, error handling,
 * and provides a consistent interface for the tool executor.
 * 
 * @module fl_api
 */

import { app } from "../../../../scripts/app.js";
import { api } from "../../../../scripts/api.js";

/**
 * FL_API class - Wrapper for FL_JS workflow manipulation functions
 */
export class FL_API {
    constructor() {
        console.log("[FL_API] Initialized");
    }

    // ==================== NODE MANAGEMENT ====================

    /**
     * Find a node by ID, type, or title
     * @param {number|string|object} query - Node ID, type, title, or node object
     * @param {boolean} findLast - If true, search from end of array
     * @returns {object|null} Node object or null if not found
     */
    find(query, findLast = false) {
        try {
            if (findLast) {
                return this._findLast(query);
            }
            return this._find(query);
        } catch (error) {
            console.error("[FL_API] find error:", error);
            return null;
        }
    }

    /**
     * Create a new node
     * @param {string} nodeType - ComfyUI node class name
     * @param {object} parameters - Node parameter values {key: value}
     * @param {object|null} position - Optional position {x, y}
     * @returns {object} Created node object
     */
    create(nodeType, parameters = {}, position = null) {
        try {
            const node = LiteGraph.createNode(nodeType);
            if (!node) {
                throw new Error(`Node type not found: ${nodeType}`);
            }

            // Set parameter values
            if (node.widgets && Object.keys(parameters).length > 0) {
                for (const [key, value] of Object.entries(parameters)) {
                    const widget = node.widgets.find(w => w.name === key);
                    if (widget) {
                        widget.value = value;
                    }
                }
            }

            // Add to graph
            app.graph.add(node);

            // Set position if provided
            if (position && typeof position.x === "number" && typeof position.y === "number") {
                node.pos[0] = position.x;
                node.pos[1] = position.y;
            }

            console.log(`[FL_API] Created node: ${nodeType} (ID: ${node.id})`);
            
            return {
                id: node.id,
                type: nodeType,
                title: node.title,
                position: { x: node.pos[0], y: node.pos[1] },
                size: { width: node.size[0], height: node.size[1] }
            };
        } catch (error) {
            console.error("[FL_API] create error:", error);
            throw error;
        }
    }

    /**
     * Remove nodes from the workflow
     * @param {Array<number|string|object>} nodeIds - Array of node IDs, titles, or node objects
     * @returns {number} Number of nodes removed
     */
    remove(nodeIds) {
        try {
            if (!Array.isArray(nodeIds)) {
                nodeIds = [nodeIds];
            }

            let removedCount = 0;
            for (const nodeId of nodeIds) {
                const node = this._findNode(nodeId);
                if (node) {
                    app.graph.remove(node);
                    removedCount++;
                }
            }

            console.log(`[FL_API] Removed ${removedCount} node(s)`);
            return removedCount;
        } catch (error) {
            console.error("[FL_API] remove error:", error);
            throw error;
        }
    }

    /**
     * Bypass (mute) nodes
     * @param {Array<number|string|object>} nodeIds - Array of node IDs
     * @returns {number} Number of nodes bypassed
     */
    bypass(nodeIds) {
        try {
            if (!Array.isArray(nodeIds)) {
                nodeIds = [nodeIds];
            }

            let bypassedCount = 0;
            for (const nodeId of nodeIds) {
                const node = this._findNode(nodeId);
                if (node) {
                    node.mode = 4; // Bypass mode
                    bypassedCount++;
                }
            }

            console.log(`[FL_API] Bypassed ${bypassedCount} node(s)`);
            return bypassedCount;
        } catch (error) {
            console.error("[FL_API] bypass error:", error);
            throw error;
        }
    }

    /**
     * Unbypass (unmute) nodes
     * @param {Array<number|string|object>} nodeIds - Array of node IDs
     * @returns {number} Number of nodes unbypassed
     */
    unbypass(nodeIds) {
        try {
            if (!Array.isArray(nodeIds)) {
                nodeIds = [nodeIds];
            }

            let unbypassedCount = 0;
            for (const nodeId of nodeIds) {
                const node = this._findNode(nodeId);
                if (node) {
                    node.mode = 0; // Normal mode
                    unbypassedCount++;
                }
            }

            console.log(`[FL_API] Unbypassed ${unbypassedCount} node(s)`);
            return unbypassedCount;
        } catch (error) {
            console.error("[FL_API] unbypass error:", error);
            throw error;
        }
    }

    /**
     * Pin nodes (prevent movement)
     * @param {Array<number|string|object>} nodeIds - Array of node IDs
     * @returns {number} Number of nodes pinned
     */
    pin(nodeIds) {
        try {
            if (!Array.isArray(nodeIds)) {
                nodeIds = [nodeIds];
            }

            let pinnedCount = 0;
            for (const nodeId of nodeIds) {
                const node = this._findNode(nodeId);
                if (node && node.pin) {
                    node.pin(true);
                    pinnedCount++;
                }
            }

            console.log(`[FL_API] Pinned ${pinnedCount} node(s)`);
            return pinnedCount;
        } catch (error) {
            console.error("[FL_API] pin error:", error);
            throw error;
        }
    }

    /**
     * Unpin nodes (allow movement)
     * @param {Array<number|string|object>} nodeIds - Array of node IDs
     * @returns {number} Number of nodes unpinned
     */
    unpin(nodeIds) {
        try {
            if (!Array.isArray(nodeIds)) {
                nodeIds = [nodeIds];
            }

            let unpinnedCount = 0;
            for (const nodeId of nodeIds) {
                const node = this._findNode(nodeId);
                if (node && node.pin) {
                    node.pin(false);
                    unpinnedCount++;
                }
            }

            console.log(`[FL_API] Unpinned ${unpinnedCount} node(s)`);
            return unpinnedCount;
        } catch (error) {
            console.error("[FL_API] unpin error:", error);
            throw error;
        }
    }

    /**
     * Select nodes in the UI
     * @param {Array<number|string|object>} nodeIds - Array of node IDs
     * @returns {number} Number of nodes selected
     */
    select(nodeIds) {
        try {
            if (!Array.isArray(nodeIds)) {
                nodeIds = [nodeIds];
            }

            const nodes = nodeIds.map(id => this._findNode(id)).filter(n => n !== null);
            
            app.canvas.deselectAll();
            app.canvas.selectNodes(nodes);

            console.log(`[FL_API] Selected ${nodes.length} node(s)`);
            return nodes.length;
        } catch (error) {
            console.error("[FL_API] select error:", error);
            throw error;
        }
    }

    // ==================== NODE MANIPULATION ====================

    /**
     * Get node parameter values
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @returns {object} Parameter values {key: value}
     */
    getValues(nodeId) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            const result = {};
            if (node.widgets) {
                for (const widget of node.widgets) {
                    result[widget.name] = widget.value;
                }
            }

            return result;
        } catch (error) {
            console.error("[FL_API] getValues error:", error);
            throw error;
        }
    }

    /**
     * Set node parameter values
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @param {object} values - Parameter values {key: value}
     * @returns {object} Updated parameter values
     */
    setValues(nodeId, values) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            if (node.widgets) {
                for (const [key, value] of Object.entries(values)) {
                    const widget = node.widgets.find(w => w.name === key);
                    if (widget) {
                        widget.value = value;
                    }
                }
            }

            console.log(`[FL_API] Set values for node ${node.id}`);
            return this.getValues(nodeId);
        } catch (error) {
            console.error("[FL_API] setValues error:", error);
            throw error;
        }
    }

    /**
     * Connect two nodes
     * @param {number|string|object} sourceId - Source node
     * @param {string|number} sourceSlot - Source slot name or index
     * @param {number|string|object} targetId - Target node
     * @param {string|number|null} targetSlot - Target slot name or index (optional)
     * @returns {boolean} True if connection successful
     */
    connect(sourceId, sourceSlot, targetId, targetSlot = null) {
        try {
            const sourceNode = this._findNode(sourceId);
            const targetNode = this._findNode(targetId);

            if (!sourceNode || !targetNode) {
                throw new Error("Source or target node not found");
            }

            // If target slot not specified, use source slot name
            if (targetSlot === null) {
                targetSlot = sourceSlot;
            }

            // Convert slot names to uppercase/lowercase for matching
            const sourceSlotName = typeof sourceSlot === "string" ? sourceSlot.toUpperCase() : null;
            const targetSlotName = typeof targetSlot === "string" ? targetSlot.toLowerCase() : null;

            // Find output slot
            let outputSlotIndex;
            if (typeof sourceSlot === "number") {
                outputSlotIndex = sourceSlot;
            } else if (sourceSlotName && sourceNode.outputs) {
                const output = sourceNode.outputs.find(o => o.name.toUpperCase() === sourceSlotName);
                if (output) {
                    outputSlotIndex = sourceNode.findOutputSlot(output.name);
                }
            }

            // Find input slot
            let inputSlotIndex;
            if (typeof targetSlot === "number") {
                inputSlotIndex = targetSlot;
            } else if (targetSlotName && targetNode.inputs) {
                const input = targetNode.inputs.find(i => i.name.toLowerCase() === targetSlotName);
                if (input) {
                    inputSlotIndex = targetNode.findInputSlot(input.name);
                }
            }

            // Attempt auto-matching by type if slots not found
            if (outputSlotIndex === undefined && sourceNode.outputs) {
                const firstOutput = sourceNode.outputs[0];
                if (firstOutput) {
                    outputSlotIndex = 0;
                    // Try to find matching input by type
                    if (inputSlotIndex === undefined && targetNode.inputs) {
                        const matchingInput = targetNode.inputs.find(i => i.type === firstOutput.type);
                        if (matchingInput) {
                            inputSlotIndex = targetNode.findInputSlot(matchingInput.name);
                        }
                    }
                }
            }

            if (typeof outputSlotIndex === "number" && typeof inputSlotIndex === "number") {
                sourceNode.connect(outputSlotIndex, targetNode.id, inputSlotIndex);
                console.log(
                    `[FL_API] Connected: ${sourceNode.id}[${outputSlotIndex}] -> ` +
                    `${targetNode.id}[${inputSlotIndex}]`
                );
                return true;
            }

            throw new Error("Could not find matching slots for connection");
        } catch (error) {
            console.error("[FL_API] connect error:", error);
            throw error;
        }
    }

    // ==================== LAYOUT MANAGEMENT ====================

    /**
     * Get node rectangle (position and size)
     * @param {number|string|object} nodeId - Node ID
     * @returns {object} {x, y, width, height}
     */
    getRect(nodeId) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            return {
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1]
            };
        } catch (error) {
            console.error("[FL_API] getRect error:", error);
            throw error;
        }
    }

    /**
     * Set node rectangle (position and/or size)
     * @param {number|string|object} nodeId - Node ID
     * @param {number|null} x - X position (null to keep current)
     * @param {number|null} y - Y position (null to keep current)
     * @param {number|null} width - Width (null to keep current)
     * @param {number|null} height - Height (null to keep current)
     * @returns {object} Updated rectangle
     */
    setRect(nodeId, x = null, y = null, width = null, height = null) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            if (x !== null) node.pos[0] = x;
            if (y !== null) node.pos[1] = y;
            if (width !== null) node.size[0] = width;
            if (height !== null) node.size[1] = height;

            if (width !== null || height !== null) {
                node.onResize(node.size);
            }

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Set rect for node ${node.id}`);
            return this.getRect(nodeId);
        } catch (error) {
            console.error("[FL_API] setRect error:", error);
            throw error;
        }
    }

    /**
     * Position node relative to another node (left)
     * @param {number|string|object} targetId - Node to move
     * @param {number|string|object} anchorId - Reference node
     * @param {number} margin - Margin between nodes (default: 32)
     */
    putOnLeft(targetId, anchorId, margin = 32) {
        try {
            const targetNode = this._findNode(targetId);
            const anchorNode = this._findNode(anchorId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            targetNode.pos[0] = anchorNode.pos[0] - targetNode.size[0] - margin;
            targetNode.pos[1] = anchorNode.pos[1];

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Positioned node ${targetNode.id} left of ${anchorNode.id}`);
        } catch (error) {
            console.error("[FL_API] putOnLeft error:", error);
            throw error;
        }
    }

    /**
     * Position node relative to another node (right)
     */
    putOnRight(targetId, anchorId, margin = 32) {
        try {
            const targetNode = this._findNode(targetId);
            const anchorNode = this._findNode(anchorId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            targetNode.pos[0] = anchorNode.pos[0] + anchorNode.size[0] + margin;
            targetNode.pos[1] = anchorNode.pos[1];

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Positioned node ${targetNode.id} right of ${anchorNode.id}`);
        } catch (error) {
            console.error("[FL_API] putOnRight error:", error);
            throw error;
        }
    }

    /**
     * Position node relative to another node (top)
     */
    putOnTop(targetId, anchorId, margin = 64) {
        try {
            const targetNode = this._findNode(targetId);
            const anchorNode = this._findNode(anchorId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            targetNode.pos[0] = anchorNode.pos[0];
            targetNode.pos[1] = anchorNode.pos[1] - targetNode.size[1] - margin;

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Positioned node ${targetNode.id} top of ${anchorNode.id}`);
        } catch (error) {
            console.error("[FL_API] putOnTop error:", error);
            throw error;
        }
    }

    /**
     * Position node relative to another node (bottom)
     */
    putOnBottom(targetId, anchorId, margin = 64) {
        try {
            const targetNode = this._findNode(targetId);
            const anchorNode = this._findNode(anchorId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            targetNode.pos[0] = anchorNode.pos[0];
            targetNode.pos[1] = anchorNode.pos[1] + anchorNode.size[1] + margin;

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Positioned node ${targetNode.id} bottom of ${anchorNode.id}`);
        } catch (error) {
            console.error("[FL_API] putOnBottom error:", error);
            throw error;
        }
    }

    /**
     * Move node to the right, avoiding collisions
     */
    moveToRight(targetId, margin = 32) {
        try {
            const targetNode = this._findNode(targetId);
            if (!targetNode) {
                throw new Error(`Node not found: ${targetId}`);
            }

            let moved = true;
            let iterations = 0;
            const maxIterations = 100; // Prevent infinite loops

            while (moved && iterations < maxIterations) {
                moved = false;
                iterations++;

                for (const node of app.graph._nodes) {
                    if (node.id === targetNode.id) continue;

                    const collides = this._checkCollision(targetNode, node);
                    if (collides) {
                        targetNode.pos[0] = node.pos[0] + node.size[0] + margin;
                        moved = true;
                    }
                }
            }

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Moved node ${targetNode.id} to right (${iterations} iterations)`);
        } catch (error) {
            console.error("[FL_API] moveToRight error:", error);
            throw error;
        }
    }

    /**
     * Move node to the bottom, avoiding collisions
     */
    moveToBottom(targetId, margin = 64) {
        try {
            const targetNode = this._findNode(targetId);
            if (!targetNode) {
                throw new Error(`Node not found: ${targetId}`);
            }

            let moved = true;
            let iterations = 0;
            const maxIterations = 100;

            while (moved && iterations < maxIterations) {
                moved = false;
                iterations++;

                for (const node of app.graph._nodes) {
                    if (node.id === targetNode.id) continue;

                    const collides = this._checkCollision(targetNode, node);
                    if (collides) {
                        targetNode.pos[1] = node.pos[1] + node.size[1] + margin;
                        moved = true;
                    }
                }
            }

            // 🔥 Force canvas refresh
            this._refreshCanvas();

            console.log(`[FL_API] Moved node ${targetNode.id} to bottom (${iterations} iterations)`);
        } catch (error) {
            console.error("[FL_API] moveToBottom error:", error);
            throw error;
        }
    }

    // ==================== WORKFLOW CONTROL ====================

    /**
     * Queue workflow execution
     * @param {number} batchCount - Number of times to execute (default: current batch count)
     * @returns {Promise<object>} Queue result
     */
    async queueWorkflow(batchCount = null) {
        try {
            const count = batchCount !== null ? batchCount : this.getBatchCount();
            await app.queuePrompt(0, count);
            console.log(`[FL_API] Queued workflow (batch: ${count})`);
            return { queued: true, batchCount: count };
        } catch (error) {
            console.error("[FL_API] queueWorkflow error:", error);
            throw error;
        }
    }

    /**
     * Cancel current workflow execution
     * @returns {Promise<object>} Cancel result
     */
    async cancelWorkflow() {
        try {
            await api.interrupt();
            console.log("[FL_API] Cancelled workflow");
            return { cancelled: true };
        } catch (error) {
            console.error("[FL_API] cancelWorkflow error:", error);
            throw error;
        }
    }

    /**
     * Enable auto-queue mode
     */
    enableAutoQueue() {
        try {
            app.extensionManager.queueSettings.mode = "instant";
            console.log("[FL_API] Auto-queue enabled");
            return { autoQueue: true, mode: "instant" };
        } catch (error) {
            console.error("[FL_API] enableAutoQueue error:", error);
            throw error;
        }
    }

    /**
     * Disable auto-queue mode
     */
    disableAutoQueue() {
        try {
            app.extensionManager.queueSettings.mode = "disabled";
            console.log("[FL_API] Auto-queue disabled");
            return { autoQueue: false, mode: "disabled" };
        } catch (error) {
            console.error("[FL_API] disableAutoQueue error:", error);
            throw error;
        }
    }

    /**
     * Set batch count
     * @param {number} count - Batch count
     */
    setBatchCount(count) {
        try {
            app.extensionManager.queueSettings.batchCount = count;
            console.log(`[FL_API] Batch count set to ${count}`);
            return { batchCount: count };
        } catch (error) {
            console.error("[FL_API] setBatchCount error:", error);
            throw error;
        }
    }

    /**
     * Get batch count
     * @returns {number} Current batch count
     */
    getBatchCount() {
        try {
            return app.extensionManager.queueSettings.batchCount;
        } catch (error) {
            console.error("[FL_API] getBatchCount error:", error);
            return 1;
        }
    }

    /**
     * Get queue status
     * @returns {object} Queue status information
     */
    getQueueStatus() {
        try {
            const mode = app.extensionManager.queueSettings.mode;
            const batchCount = app.extensionManager.queueSettings.batchCount;
            
            return {
                mode: mode,
                autoQueue: mode !== "disabled",
                batchCount: batchCount
            };
        } catch (error) {
            console.error("[FL_API] getQueueStatus error:", error);
            throw error;
        }
    }

    // ==================== SYSTEM CONTROL ====================

    /**
     * Disable system sleep
     */
    async disableSleep() {
        try {
            const response = await api.fetchApi("/shinich39/event-handler/disable-sleep", {
                method: "GET"
            });
            if (response.status !== 200) {
                throw new Error(response.statusText);
            }
            console.log("[FL_API] System sleep disabled");
            return { sleepDisabled: true };
        } catch (error) {
            console.error("[FL_API] disableSleep error:", error);
            throw error;
        }
    }

    /**
     * Enable system sleep
     */
    async enableSleep() {
        try {
            const response = await api.fetchApi("/shinich39/event-handler/enable-sleep", {
                method: "GET"
            });
            if (response.status !== 200) {
                throw new Error(response.statusText);
            }
            console.log("[FL_API] System sleep enabled");
            return { sleepEnabled: true };
        } catch (error) {
            console.error("[FL_API] enableSleep error:", error);
            throw error;
        }
    }

    /**
     * Disable screensaver
     */
    async disableScreensaver() {
        try {
            const response = await api.fetchApi("/shinich39/event-handler/disable-screen-saver", {
                method: "GET"
            });
            if (response.status !== 200) {
                throw new Error(response.statusText);
            }
            console.log("[FL_API] Screensaver disabled");
            return { screensaverDisabled: true };
        } catch (error) {
            console.error("[FL_API] disableScreensaver error:", error);
            throw error;
        }
    }

    /**
     * Enable screensaver
     */
    async enableScreensaver() {
        try {
            const response = await api.fetchApi("/shinich39/event-handler/enable-screen-saver", {
                method: "GET"
            });
            if (response.status !== 200) {
                throw new Error(response.statusText);
            }
            console.log("[FL_API] Screensaver enabled");
            return { screensaverEnabled: true };
        } catch (error) {
            console.error("[FL_API] enableScreensaver error:", error);
            throw error;
        }
    }

    /**
     * Send images to external URL
     * @param {string} url - Target URL
     * @param {string} field - Form field name
     * @param {Array<string|object>} filePaths - File paths or PreviewImage nodes
     */
    async sendImages(url, field, filePaths) {
        try {
            // Process filePaths to extract actual paths
            const paths = [];
            for (const item of filePaths) {
                if (typeof item === "string") {
                    paths.push(item);
                } else if (typeof item === "object" && item.comfyClass === "PreviewImage" && item.imgs) {
                    for (const img of item.imgs) {
                        paths.push(this._getPathFromImg(img));
                    }
                }
            }

            if (paths.length === 0) {
                throw new Error("No images found");
            }

            const response = await api.fetchApi("/shinich39/event-handler/send-images", {
                method: "POST",
                body: JSON.stringify({ url, field, files: paths })
            });

            if (response.status !== 200) {
                throw new Error(response.statusText);
            }

            console.log(`[FL_API] Sent ${paths.length} image(s) to ${url}`);
            return { sent: true, count: paths.length };
        } catch (error) {
            console.error("[FL_API] sendImages error:", error);
            throw error;
        }
    }

    // ==================== UTILITIES ====================

    /**
     * Generate random seed
     * @returns {number} Random seed value
     */
    generateSeed() {
        const MIN_SEED = 0;
        const MAX_SEED = parseInt("0xffffffffffffffff", 16);
        const STEPS_OF_SEED = 10;
        
        const max = Math.min(1125899906842624, MAX_SEED);
        const min = Math.max(-1125899906842624, MIN_SEED);
        const range = (max - min) / (STEPS_OF_SEED / 10);
        
        return Math.floor(Math.random() * range) * (STEPS_OF_SEED / 10) + min;
    }

    /**
     * Generate random float
     * @param {number} min - Minimum value
     * @param {number} max - Maximum value
     * @returns {number} Random float
     */
    generateFloat(min, max) {
        if (typeof min !== "number") min = Number.MIN_SAFE_INTEGER;
        if (typeof max !== "number") max = Number.MAX_SAFE_INTEGER;
        return Math.random() * (max - min) + min;
    }

    /**
     * Generate random integer
     * @param {number} min - Minimum value
     * @param {number} max - Maximum value
     * @returns {number} Random integer
     */
    generateInt(min, max) {
        return Math.floor(this.generateFloat(min, max));
    }

    /**
     * Pick random item from array
     * @param {Array} items - Array of items
     * @returns {*} Random item
     */
    randomChoice(items) {
        if (!Array.isArray(items) || items.length === 0) {
            throw new Error("Items must be a non-empty array");
        }
        return items[this.generateInt(0, items.length)];
    }

    // ==================== PRIVATE HELPERS ====================

    /**
     * Force canvas refresh after layout changes
     * @private
     */
    _refreshCanvas() {
        try {
            app.canvas.setDirty(true, true);
        } catch (error) {
            console.warn("[FL_API] Could not refresh canvas:", error);
        }
    }

    _match(node, query) {
        if (typeof query === "number") {
            return node.id === query;
        } else if (typeof query === "string") {
            return node.title === query || node.comfyClass === query || node.type === query;
        } else if (typeof query === "object" && query.id !== undefined) {
            return node.id === query.id;
        }
        return false;
    }

    _find(query) {
        for (let i = 0; i < app.graph._nodes.length; i++) {
            const node = app.graph._nodes[i];
            if (this._match(node, query)) {
                return node;
            }
        }
        return null;
    }

    _findLast(query) {
        for (let i = app.graph._nodes.length - 1; i >= 0; i--) {
            const node = app.graph._nodes[i];
            if (this._match(node, query)) {
                return node;
            }
        }
        return null;
    }

    _findNode(n) {
        if (typeof n === "number" || typeof n === "string") {
            return this._find(n);
        } else if (typeof n === "object") {
            return n;
        }
        return null;
    }

    _checkCollision(node1, node2) {
        const left1 = node1.pos[0];
        const right1 = node1.pos[0] + node1.size[0];
        const top1 = node1.pos[1];
        const bottom1 = node1.pos[1] + node1.size[1];

        const left2 = node2.pos[0];
        const right2 = node2.pos[0] + node2.size[0];
        const top2 = node2.pos[1];
        const bottom2 = node2.pos[1] + node2.size[1];

        const collisionX = left1 < right2 && right1 > left2;
        const collisionY = top1 < bottom2 && bottom1 > top2;

        return collisionX && collisionY;
    }

    _getPathFromImg(img) {
        const url = new URL(img.src);
        let filename = url.searchParams.get("filename") || "";
        let subdir = url.searchParams.get("subfolder") || "";
        let dir = url.searchParams.get("type") || "";
        
        if (filename) filename = "/" + filename;
        if (subdir) subdir = "/" + subdir;
        if (dir) dir = "/" + dir;
        
        return `ComfyUI${dir}${subdir}${filename}`;
    }
}
