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
        this.sessionId = null;  // Will be set by extension
        this.layoutEngine = null;  // Lazy loaded when auto-layout is used
    }

    /**
     * Set session ID for screenshot naming
     * @param {string} sessionId - Session ID
     */
    setSessionId(sessionId) {
        this.sessionId = sessionId;
        console.log(`[FL_API] Session ID set: ${sessionId}`);
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
                        this._setWidgetValue(node, widget, value);
                    }
                }
            }

            // Set position if provided
            if (position && typeof position.x === "number" && typeof position.y === "number") {
                node.pos = [position.x, position.y];
            }

            // Add to graph
            app.graph.add(node);
            this._markGraphChanged();

            console.log(`[FL_API] Created node: ${nodeType} (id: ${node.id})`);
            return {
                id: node.id,
                type: node.comfyClass || node.type,
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
     * Remove nodes from workflow
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of removed nodes
     */
    remove(nodeIds) {
        try {
            let removed = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node) {
                    app.graph.remove(node);
                    removed++;
                }
            }
            if (removed > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Removed ${removed} node(s)`);
            return { removed };
        } catch (error) {
            console.error("[FL_API] remove error:", error);
            throw error;
        }
    }

    /**
     * Bypass (mute) nodes
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of bypassed nodes
     */
    bypass(nodeIds) {
        try {
            let bypassed = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node && node.mode !== 4) {  // 4 = bypassed
                    node.mode = 4;
                    bypassed++;
                }
            }
            if (bypassed > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Bypassed ${bypassed} node(s)`);
            return { bypassed };
        } catch (error) {
            console.error("[FL_API] bypass error:", error);
            throw error;
        }
    }

    /**
     * Unbypass (unmute) nodes
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of unbypassed nodes
     */
    unbypass(nodeIds) {
        try {
            let unbypassed = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node && node.mode === 4) {  // 4 = bypassed
                    node.mode = 0;  // 0 = normal
                    unbypassed++;
                }
            }
            if (unbypassed > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Unbypassed ${unbypassed} node(s)`);
            return { unbypassed };
        } catch (error) {
            console.error("[FL_API] unbypass error:", error);
            throw error;
        }
    }

    /**
     * Pin nodes to prevent movement
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of pinned nodes
     */
    pin(nodeIds) {
        try {
            let pinned = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node) {
                    node.flags = node.flags || {};
                    node.flags.pinned = true;
                    pinned++;
                }
            }
            if (pinned > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Pinned ${pinned} node(s)`);
            return { pinned };
        } catch (error) {
            console.error("[FL_API] pin error:", error);
            throw error;
        }
    }

    /**
     * Unpin nodes to allow movement
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of unpinned nodes
     */
    unpin(nodeIds) {
        try {
            let unpinned = 0;
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node && node.flags && node.flags.pinned) {
                    node.flags.pinned = false;
                    unpinned++;
                }
            }
            if (unpinned > 0) {
                this._markGraphChanged();
            }
            console.log(`[FL_API] Unpinned ${unpinned} node(s)`);
            return { unpinned };
        } catch (error) {
            console.error("[FL_API] unpin error:", error);
            throw error;
        }
    }

    /**
     * Select nodes in the UI
     * @param {Array<number|string>} nodeIds - Array of node IDs or titles
     * @returns {object} Result with count of selected nodes
     */
    selectNodes(nodeIds) {
        try {
            // Clear current selection
            app.canvas.selectNodes([]);

            // Find and select nodes
            const nodes = [];
            for (const id of nodeIds) {
                const node = this._findNode(id);
                if (node) {
                    nodes.push(node);
                }
            }

            if (nodes.length > 0) {
                app.canvas.selectNodes(nodes);
            }
            this._markCanvasDirty();

            console.log(`[FL_API] Selected ${nodes.length} node(s)`);
            return { selected: nodes.length };
        } catch (error) {
            console.error("[FL_API] selectNodes error:", error);
            throw error;
        }
    }

    /**
     * Get currently selected nodes with full details
     * @returns {Array<object>} Array of selected node objects
     */
    getSelectedNodes() {
        try {
            const selectedNodes = Object.values(app.canvas.selected_nodes || {});
            const result = [];
            
            for (const node of selectedNodes) {
                // Extract parameters from widgets
                const parameters = {};
                if (node.widgets) {
                    for (const widget of node.widgets) {
                        if (widget.name && widget.value !== undefined) {
                            parameters[widget.name] = widget.value;
                        }
                    }
                }
                
                // Extract input slot info
                const inputs = [];
                if (node.inputs) {
                    for (const input of node.inputs) {
                        inputs.push({
                            name: input.name,
                            type: input.type,
                            link: input.link || null
                        });
                    }
                }
                
                // Extract output slot info
                const outputs = [];
                if (node.outputs) {
                    for (const output of node.outputs) {
                        outputs.push({
                            name: output.name,
                            type: output.type,
                            links: output.links || []
                        });
                    }
                }
                
                result.push({
                    id: node.id,
                    title: node.title,
                    type: node.comfyClass || node.type,
                    position: { x: node.pos[0], y: node.pos[1] },
                    size: { width: node.size[0], height: node.size[1] },
                    mode: node.mode || 0,
                    parameters: parameters,
                    inputs: inputs,
                    outputs: outputs
                });
            }
            
            console.log(`[FL_API] Retrieved ${result.length} selected node(s)`);
            return result;
        } catch (error) {
            console.error("[FL_API] getSelectedNodes error:", error);
            throw error;
        }
    }

    /**
     * Fit view to selected nodes or all nodes
     * @param {Array<number>|null} nodeIds - Optional array of node IDs to fit (null for selected)
     * @returns {object} Result with count of fitted nodes
     */
    fitView(nodeIds = null) {
        try {
            const canvas = app.canvas;
            let nodes;
            
            if (nodeIds === null) {
                // Use currently selected nodes
                nodes = Object.values(canvas.selected_nodes || {});
                
                if (nodes.length === 0) {
                    console.warn("[FL_API] No nodes selected, fitting all nodes");
                    nodes = app.graph._nodes;  // Use all nodes
                }
            } else if (Array.isArray(nodeIds) && nodeIds.length > 0) {
                // Find specified nodes
                nodes = nodeIds
                    .map(id => this._findNode(id))
                    .filter(n => n !== null);
                
                if (nodes.length === 0) {
                    throw new Error(`None of the specified node IDs found: ${nodeIds}`);
                }
            } else {
                // Empty array = fit all nodes
                nodes = app.graph._nodes;
            }
            
            // FIT NODES
            if (nodes.length > 0){
                if (nodes.length === 1) // Single node: just center on it
                    canvas.centerOnNode(nodes[0]);
            
                // Multiple nodes: calculate bounding box and fit to view
                let minX = Infinity, minY = Infinity;
                let maxX = -Infinity, maxY = -Infinity;
                
                for (const node of nodes) {
                    minX = Math.min(minX, node.pos[0]);
                    minY = Math.min(minY, node.pos[1]);
                    maxX = Math.max(maxX, node.pos[0] + node.size[0]);
                    maxY = Math.max(maxY, node.pos[1] + node.size[1]);
                }
                
                const centerX = (minX + maxX) / 2;
                const centerY = (minY + maxY) / 2;
                const width = maxX - minX;
                const height = maxY - minY;
                
                // Calculate zoom to fit with 10% padding
                const zoomX = canvas.canvas.width / width - 0.1;
                const zoomY = canvas.canvas.height / height - 0.1;
                const targetZoom = Math.min(zoomX, zoomY, 1.0);  // Don't zoom in past 100%
                
                // Apply viewport transform
                // canvas.ds.offset[0] = -centerX + canvas.canvas.width / 2 / targetZoom;
                // canvas.ds.offset[1] = -centerY + canvas.canvas.height / 2 / targetZoom;
                // canvas.ds.scale = targetZoom;
                canvas.setZoom(targetZoom, [canvas.canvas.width / 2, canvas.canvas.height / 2]);
            }
            
            // Mark canvas for redraw
            canvas.setDirty(true, true);
            
            const count = nodes.length;
            console.log(`[FL_API] Fit view to ${count} node(s)`);
            
            return { 
                fitted_count: count,
                node_ids: nodes.map(n => n.id)
            };
        } catch (error) {
            console.error("[FL_API] fitView error:", error);
            throw error;
        }
    }

    /**
     * Take a screenshot of the canvas
     * @param {string} format - Image format ('jpeg' or 'png')
     * @param {number} quality - JPEG quality (0.0-1.0)
     * @returns {Promise<object>} Screenshot data with id, format, size
     */
    async takeScreenshot(format = 'jpeg', quality = 0.9) {
        try {
            // Get canvas element
            const canvasElement = app.canvas.canvas;
            if (!canvasElement) {
                throw new Error('Canvas element not found');
            }
            
            console.log(`[FL_API] Taking screenshot (${format}, quality: ${quality})`);
            
            // Convert canvas to blob
            const mimeType = format === 'png' ? 'image/png' : 'image/jpeg';
            const blob = await new Promise((resolve, reject) => {
                canvasElement.toBlob(
                    (blob) => {
                        if (blob) {
                            resolve(blob);
                        } else {
                            reject(new Error('Failed to create blob from canvas'));
                        }
                    },
                    mimeType,
                    quality
                );
            });
            
            // Convert blob to base64
            const base64Data = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
            
            // Generate screenshot ID
            const timestamp = Date.now();
            const sessionId = this.sessionId || 'unknown';
            const screenshotId = `screenshot_${timestamp}_${sessionId.substring(0, 8)}`;
            
            console.log(`[FL_API] Screenshot captured: ${screenshotId} (${blob.size} bytes)`);
            
            return {
                screenshot_id: screenshotId,
                format: format,
                size_bytes: blob.size,
                base64_data: base64Data
            };
            
        } catch (error) {
            console.error('[FL_API] Screenshot error:', error);
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

            const values = {};
            if (node.widgets) {
                for (const widget of node.widgets) {
                    if (widget.name && widget.value !== undefined) {
                        values[widget.name] = widget.value;
                    }
                }
            }

            console.log(`[FL_API] Retrieved values for node ${node.id}`);
            return values;
        } catch (error) {
            console.error("[FL_API] getValues error:", error);
            throw error;
        }
    }

    /**
     * Set node parameter values
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @param {object} values - Parameter values {key: value}
     * @returns {object} Result with count of set parameters
     */
    setValues(nodeId, values) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            let set = 0;
            if (node.widgets) {
                for (const [key, value] of Object.entries(values)) {
                    const widget = node.widgets.find(w => w.name === key);
                    if (widget) {
                        this._setWidgetValue(node, widget, value);
                        set++;
                    }
                }
            }
            if (set > 0) {
                this._markGraphChanged();
            }

            console.log(`[FL_API] Set ${set} value(s) on node ${node.id}`);
            return { set };
        } catch (error) {
            console.error("[FL_API] setValues error:", error);
            throw error;
        }
    }

    /**
     * Get slot information for a node
     * @param {number|string|object} nodeId - Node ID, title, or object
     * @returns {object} Slot information
     */
    getNodeSlots(nodeId) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }
            
            const inputs = [];
            if (node.inputs) {
                for (let i = 0; i < node.inputs.length; i++) {
                    const input = node.inputs[i];
                    const slotInfo = {
                        name: input.name,
                        type: input.type,
                        index: i,
                        connected: input.link !== null && input.link !== undefined
                    };
                    
                    // Add connection details if connected
                    if (slotInfo.connected && node.graph.links[input.link]) {
                        const link = node.graph.links[input.link];
                        slotInfo.connected_from = {
                            node_id: link.origin_id,
                            slot_index: link.origin_slot
                        };
                    }
                    
                    inputs.push(slotInfo);
                }
            }
            
            const outputs = [];
            if (node.outputs) {
                for (let i = 0; i < node.outputs.length; i++) {
                    const output = node.outputs[i];
                    const slotInfo = {
                        name: output.name,
                        type: output.type,
                        index: i,
                        connected: output.links && output.links.length > 0,
                        connected_to: []
                    };
                    
                    // Add connection details if connected
                    if (slotInfo.connected) {
                        for (const linkId of output.links) {
                            const link = node.graph.links[linkId];
                            if (link) {
                                slotInfo.connected_to.push({
                                    node_id: link.target_id,
                                    slot_index: link.target_slot
                                });
                            }
                        }
                    }
                    
                    outputs.push(slotInfo);
                }
            }
            
            console.log(`[FL_API] Retrieved slots for node ${node.id}`);
            return {
                node_id: node.id,
                type: node.comfyClass || node.type,
                title: node.title,
                inputs,
                outputs
            };
        } catch (error) {
            console.error("[FL_API] getNodeSlots error:", error);
            throw error;
        }
    }

    /**
     * Connect two nodes with optional auto-matching
     * @param {number|string|object} sourceId - Source node
     * @param {string|number|null} sourceSlot - Source slot name/index (null for auto)
     * @param {number|string|object} targetId - Target node
     * @param {string|number|null} targetSlot - Target slot name/index (null for auto)
     * @param {object} options - Connection options {auto_match, match_strategy}
     * @returns {object} Connection details
     */
    connect(sourceId, sourceSlot = null, targetId, targetSlot = null, options = {}) {
        try {
            const sourceNode = this._findNode(sourceId);
            const targetNode = this._findNode(targetId);

            if (!sourceNode || !targetNode) {
                throw new Error("Source or target node not found");
            }

            // Options
            const autoMatch = options.auto_match !== false;  // Default true
            const matchStrategy = options.match_strategy || "type";  // Default "type"

            // Helper for case-insensitive slot name comparison
            const normalizeSlotName = (name) => String(name).toLowerCase().trim();

            // Find output slot
            let outputSlotIndex;
            let outputSlotName;
            let outputSlotType;
            
            if (typeof sourceSlot === "number") {
                // Direct index provided
                outputSlotIndex = sourceSlot;
                if (sourceNode.outputs && sourceNode.outputs[sourceSlot]) {
                    outputSlotName = sourceNode.outputs[sourceSlot].name;
                    outputSlotType = sourceNode.outputs[sourceSlot].type;
                }
            } else if (typeof sourceSlot === "string" && sourceNode.outputs) {
                // Slot name provided - find by name (case-insensitive)
                const normalizedSource = normalizeSlotName(sourceSlot);
                const output = sourceNode.outputs.find(o => 
                    normalizeSlotName(o.name) === normalizedSource
                );
                if (output) {
                    outputSlotIndex = sourceNode.findOutputSlot(output.name);
                    outputSlotName = output.name;
                    outputSlotType = output.type;
                }
            }

            // Find input slot
            let inputSlotIndex;
            let inputSlotName;
            let inputSlotType;
            
            if (typeof targetSlot === "number") {
                // Direct index provided
                inputSlotIndex = targetSlot;
                if (targetNode.inputs && targetNode.inputs[targetSlot]) {
                    inputSlotName = targetNode.inputs[targetSlot].name;
                    inputSlotType = targetNode.inputs[targetSlot].type;
                }
            } else if (typeof targetSlot === "string" && targetNode.inputs) {
                // Slot name provided - find by name (case-insensitive)
                const normalizedTarget = normalizeSlotName(targetSlot);
                const input = targetNode.inputs.find(i => 
                    normalizeSlotName(i.name) === normalizedTarget
                );
                if (input) {
                    inputSlotIndex = targetNode.findInputSlot(input.name);
                    inputSlotName = input.name;
                    inputSlotType = input.type;
                }
            }

            // Auto-matching if enabled and slots not found
            if (autoMatch) {
                // Auto-match output slot if not found
                if (outputSlotIndex === undefined && sourceNode.outputs && sourceNode.outputs.length > 0) {
                    if (matchStrategy === "first") {
                        // Use first output
                        outputSlotIndex = 0;
                        outputSlotName = sourceNode.outputs[0].name;
                        outputSlotType = sourceNode.outputs[0].type;
                    } else if (matchStrategy === "type" && inputSlotType) {
                        // Match by type if we know the input type
                        const matchingOutput = sourceNode.outputs.find(o => o.type === inputSlotType);
                        if (matchingOutput) {
                            outputSlotIndex = sourceNode.findOutputSlot(matchingOutput.name);
                            outputSlotName = matchingOutput.name;
                            outputSlotType = matchingOutput.type;
                        } else {
                            // Fallback to first if no type match
                            outputSlotIndex = 0;
                            outputSlotName = sourceNode.outputs[0].name;
                            outputSlotType = sourceNode.outputs[0].type;
                        }
                    }
                }

                // Auto-match input slot if not found
                if (inputSlotIndex === undefined && targetNode.inputs && targetNode.inputs.length > 0) {
                    if (matchStrategy === "first") {
                        // Use first available (unconnected) input
                        const availableInput = targetNode.inputs.find(i => !i.link);
                        if (availableInput) {
                            inputSlotIndex = targetNode.findInputSlot(availableInput.name);
                            inputSlotName = availableInput.name;
                            inputSlotType = availableInput.type;
                        } else {
                            // All connected, use first
                            inputSlotIndex = 0;
                            inputSlotName = targetNode.inputs[0].name;
                            inputSlotType = targetNode.inputs[0].type;
                        }
                    } else if (matchStrategy === "type" && outputSlotType) {
                        // Match by type if we know the output type
                        const matchingInput = targetNode.inputs.find(i => 
                            i.type === outputSlotType && !i.link  // Prefer unconnected
                        );
                        if (matchingInput) {
                            inputSlotIndex = targetNode.findInputSlot(matchingInput.name);
                            inputSlotName = matchingInput.name;
                            inputSlotType = matchingInput.type;
                        } else {
                            // Try connected slots if no unconnected match
                            const anyMatchingInput = targetNode.inputs.find(i => i.type === outputSlotType);
                            if (anyMatchingInput) {
                                inputSlotIndex = targetNode.findInputSlot(anyMatchingInput.name);
                                inputSlotName = anyMatchingInput.name;
                                inputSlotType = anyMatchingInput.type;
                            }
                        }
                    }
                }
            }

            // Check if we have both slots
            if (typeof outputSlotIndex !== "number" || typeof inputSlotIndex !== "number") {
                // Build detailed error message
                const availableOutputs = sourceNode.outputs ? 
                    sourceNode.outputs.map(o => `"${o.name}" (${o.type})`).join(", ") : "none";
                const availableInputs = targetNode.inputs ?
                    targetNode.inputs.map(i => `"${i.name}" (${i.type})${i.link ? ' [connected]' : ''}`).join(", ") : "none";

                const errorMsg = [
                    `Could not find matching slots for connection.`,
                    `Attempted: source="${sourceSlot || 'auto'}" → target="${targetSlot || 'auto'}"`,
                    `Source node ${sourceNode.id} (${sourceNode.comfyClass || sourceNode.type}) outputs: ${availableOutputs}`,
                    `Target node ${targetNode.id} (${targetNode.comfyClass || targetNode.type}) inputs: ${availableInputs}`,
                    ``,
                    `TIP: Use get_node_slots(node_id) to discover exact slot names.`
                ].join("\n");

                throw new Error(errorMsg);
            }

            // Make the connection
            sourceNode.connect(outputSlotIndex, targetNode, inputSlotIndex);
            this._markGraphChanged();
            
            const connectionInfo = {
                source_node_id: sourceNode.id,
                source_slot: outputSlotName,
                source_slot_index: outputSlotIndex,
                target_node_id: targetNode.id,
                target_slot: inputSlotName,
                target_slot_index: inputSlotIndex,
                type: outputSlotType || inputSlotType
            };
            
            console.log(
                `[FL_API] Connected: ${sourceNode.id}[${outputSlotIndex}] "${outputSlotName}" -> ` +
                `${targetNode.id}[${inputSlotIndex}] "${inputSlotName}" (${connectionInfo.type})`
            );
            
            return connectionInfo;
        } catch (error) {
            console.error("[FL_API] connect error:", error);
            throw error;
        }
    }

    /**
     * Connect multiple node pairs in batch
     * @param {Array<object>} connections - Array of connection specs
     * @param {object} options - Options {auto_match, stop_on_error}
     * @returns {object} Batch result
     */
    connectBatch(connections, options = {}) {
        try {
            const autoMatch = options.auto_match !== false;
            const stopOnError = options.stop_on_error || false;
            
            const results = [];
            let successful = 0;
            let failed = 0;
            
            for (const conn of connections) {
                try {
                    const connectOptions = {
                        auto_match: autoMatch,
                        match_strategy: "type"
                    };

                    // Support both old (source_slot) and new (source_slot_name) field names
                    const sourceSlot = conn.source_slot_name ?? conn.source_slot ?? null;
                    const targetSlot = conn.target_slot_name ?? conn.target_slot ?? null;

                    const result = this.connect(
                        conn.source_node_id,
                        sourceSlot,
                        conn.target_node_id,
                        targetSlot,
                        connectOptions
                    );
                    
                    results.push({
                        success: true,
                        connection: result
                    });
                    successful++;
                } catch (error) {
                    results.push({
                        success: false,
                        error: error.message,
                        attempted: conn
                    });
                    failed++;
                    
                    if (stopOnError) {
                        break;
                    }
                }
            }
            
            console.log(`[FL_API] Batch connect: ${successful} succeeded, ${failed} failed`);
            return {
                total: connections.length,
                successful,
                failed,
                results
            };
        } catch (error) {
            console.error("[FL_API] connectBatch error:", error);
            throw error;
        }
    }

    /**
     * Auto-connect nodes in sequence or by type matching
     * @param {Array<number|string>} nodeIds - Array of node IDs
     * @param {string} strategy - "sequential" or "type_match"
     * @returns {object} Auto-connect result
     */
    autoConnectWorkflow(nodeIds, strategy = "sequential") {
        try {
            const connections = [];
            const failed = [];
            
            if (strategy === "sequential") {
                // Connect nodes in sequence: A→B→C→D
                for (let i = 0; i < nodeIds.length - 1; i++) {
                    const sourceId = nodeIds[i];
                    const targetId = nodeIds[i + 1];
                    
                    try {
                        const result = this.connect(
                            sourceId,
                            null,  // Auto-match source slot
                            targetId,
                            null,  // Auto-match target slot
                            { auto_match: true, match_strategy: "type" }
                        );
                        
                        connections.push({
                            source: result.source_node_id,
                            target: result.target_node_id,
                            source_slot: result.source_slot,
                            target_slot: result.target_slot,
                            type: result.type
                        });
                    } catch (error) {
                        failed.push({
                            source: sourceId,
                            target: targetId,
                            error: error.message
                        });
                    }
                }
            } else if (strategy === "type_match") {
                // Find all compatible type matches between all nodes
                const nodes = nodeIds.map(id => this._findNode(id)).filter(n => n !== null);
                
                for (let i = 0; i < nodes.length; i++) {
                    const sourceNode = nodes[i];
                    if (!sourceNode.outputs) continue;
                    
                    for (const output of sourceNode.outputs) {
                        // Find compatible inputs in other nodes
                        for (let j = 0; j < nodes.length; j++) {
                            if (i === j) continue;  // Skip self
                            
                            const targetNode = nodes[j];
                            if (!targetNode.inputs) continue;
                            
                            const matchingInput = targetNode.inputs.find(inp => 
                                inp.type === output.type && !inp.link  // Unconnected and matching type
                            );
                            
                            if (matchingInput) {
                                try {
                                    const result = this.connect(
                                        sourceNode.id,
                                        output.name,
                                        targetNode.id,
                                        matchingInput.name,
                                        { auto_match: false }
                                    );
                                    
                                    connections.push({
                                        source: result.source_node_id,
                                        target: result.target_node_id,
                                        source_slot: result.source_slot,
                                        target_slot: result.target_slot,
                                        type: result.type
                                    });
                                } catch (error) {
                                    failed.push({
                                        source: sourceNode.id,
                                        target: targetNode.id,
                                        source_slot: output.name,
                                        target_slot: matchingInput.name,
                                        error: error.message
                                    });
                                }
                            }
                        }
                    }
                }
            }
            
            console.log(`[FL_API] Auto-connect (${strategy}): ${connections.length} connections made`);
            return {
                connections_made: connections.length,
                connections,
                failed
            };
        } catch (error) {
            console.error("[FL_API] autoConnectWorkflow error:", error);
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

            const rect = {
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1]
            };

            console.log(`[FL_API] Got rect for node ${node.id}`);
            return rect;
        } catch (error) {
            console.error("[FL_API] getRect error:", error);
            throw error;
        }
    }

    /**
     * Get layout (rects) for all nodes or specific nodes
     * @param {Array<number|string>|null} nodeIds - Optional array of node IDs or titles (null for all)
     * @returns {object} {nodes: Array<{node_id, title, type, rect}>, count: number}
     */
    getLayout(nodeIds = null) {
        try {
            // Safety check for graph
            if (!app.graph || !app.graph._nodes) {
                console.warn("[FL_API] Graph not ready");
                return { nodes: [], count: 0 };
            }
            
            // Get nodes to process
            const nodes = nodeIds 
                ? nodeIds.map(id => this._findNode(id)).filter(n => n !== null)
                : app.graph._nodes;
            
            // Collect layout data
            const layout = nodes.map(node => ({
                node_id: node.id,
                title: node.title,
                type: node.comfyClass || node.type,
                rect: {
                    x: node.pos[0],
                    y: node.pos[1],
                    width: node.size[0],
                    height: node.size[1]
                }
            }));
            
            console.log(`[FL_API] Got layout for ${layout.length} node(s)`);
            return { nodes: layout, count: layout.length };
        } catch (error) {
            console.error("[FL_API] getLayout error:", error);
            throw error;
        }
    }

    /**
     * Modify layout for multiple nodes by setting their rectangles or using auto-layout
     * @param {object} nodeRects - rect objects mapped by nodeId {nodeId: {x, y, width, height}}
     * @param {object} options - Optional auto-layout parameters {auto_layout, node_ids, strategy, spacing_multiplier}
     * @returns {Array<object>} Array of results with updated rectangles or errors
     */
    async modifyLayout(nodeRects = null, options = {}) {
        try {
            // MODE 1: Auto-layout
            if (options.auto_layout === true) {
                console.log(`[FL_API] Auto-layout requested with strategy: ${options.strategy || 'flow_horizontal'}`);
                
                // Lazy load LayoutEngine
                if (!this.layoutEngine) {
                    const { LayoutEngine } = await import('./layout_engine.js');
                    this.layoutEngine = new LayoutEngine();
                    console.log("[FL_API] LayoutEngine loaded");
                }

                // Configure spacing
                if (options.spacing_multiplier !== undefined && options.spacing_multiplier !== null) {
                    this.layoutEngine.setSpacingMultiplier(options.spacing_multiplier);
                } else {
                    // Reset to default if not specified
                    this.layoutEngine.setSpacingMultiplier(1.0);
                }

                // Run layout engine
                const layout = this.layoutEngine.arrangeNodes(
                    options.node_ids || null,
                    options.strategy || "flow_horizontal",
                    {}
                );

                // Apply calculated positions using setRect
                const results = [];
                for (const item of layout) {
                    try {
                        const updatedRect = this.setRect(item.node_id, {
                            x: item.x,
                            y: item.y,
                            width: item.width,
                            height: item.height
                        });
                        results.push({
                            node_id: item.node_id,
                            rect: updatedRect,
                            success: true
                        });
                    } catch (error) {
                        console.error(`[FL_API] Auto-layout: Error setting rect for node ${item.node_id}:`, error);
                        results.push({
                            node_id: item.node_id,
                            success: false,
                            error: error.message
                        });
                    }
                }

                console.log(`[FL_API] Auto-layout complete: ${results.length} nodes arranged`);
                return results;
            }

            // MODE 2: Manual layout (existing behavior)
            if (!nodeRects || typeof nodeRects !== 'object') {
                console.log('[FL_API] modifyLayout: No node rects provided');
                return [];
            }

            const results = [];
            let processed = 0;
            let successful = 0;
            let failed = 0;

            // Process each node
            for (const [nodeIdStr, rect] of Object.entries(nodeRects)) {
                const nodeId = parseInt(nodeIdStr, 10);
                processed++;

                try {
                    // Call setRect and collect result
                    const updatedRect = this.setRect(nodeId, rect);
                    results.push({
                        node_id: nodeId,
                        rect: updatedRect,
                        success: true
                    });
                    successful++;
                } catch (error) {
                    console.error(`[FL_API] modifyLayout: Error setting rect for node ${nodeId}:`, error);
                    results.push({
                        node_id: nodeId,
                        success: false,
                        error: error.message
                    });
                    failed++;
                }
            }

            console.log(`[FL_API] modifyLayout: Processed ${processed} nodes (${successful} successful, ${failed} failed)`);
            return results;
            
        } catch (error) {
            console.error('[FL_API] modifyLayout error:', error);
            throw error;
        }
    }


    /**
     * Set node rectangle (position and/or size)
     * @param {number|string|object} nodeId - Node ID
     * @param {object} rect - {x, y, width, height} (all optional)
     * @returns {object} Updated rectangle
     */
    setRect(nodeId, rect) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            if (typeof rect.x === "number") node.pos[0] = rect.x;
            if (typeof rect.y === "number") node.pos[1] = rect.y;
            if (typeof rect.width === "number") node.size[0] = rect.width;
            if (typeof rect.height === "number") node.size[1] = rect.height;
            this._markGraphChanged();

            const updated = {
                x: node.pos[0],
                y: node.pos[1],
                width: node.size[0],
                height: node.size[1]
            };

            console.log(`[FL_API] Set rect for node ${node.id}`);
            return updated;
        } catch (error) {
            console.error("[FL_API] setRect error:", error);
            throw error;
        }
    }

    /**
     * Position node to the left of another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     * @returns {object} Updated position
     */
    positionLeft(targetNodeId, anchorNodeId, margin = 32) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0] - targetNode.size[0] - margin;
            const y = anchorNode.pos[1];

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} left of ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionLeft error:", error);
            throw error;
        }
    }

    /**
     * Position node to the right of another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     * @returns {object} Updated position
     */
    positionRight(targetNodeId, anchorNodeId, margin = 32) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0] + anchorNode.size[0] + margin;
            const y = anchorNode.pos[1];

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} right of ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionRight error:", error);
            throw error;
        }
    }

    /**
     * Position node above another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     * @returns {object} Updated position
     */
    positionTop(targetNodeId, anchorNodeId, margin = 64) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0];
            const y = anchorNode.pos[1] - targetNode.size[1] - margin;

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} above ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionTop error:", error);
            throw error;
        }
    }

    /**
     * Position node below another
     * @param {number|string|object} targetNodeId - Node to position
     * @param {number|string|object} anchorNodeId - Reference node
     * @param {number} margin - Margin between nodes
     */
    positionBottom(targetNodeId, anchorNodeId, margin = 64) {
        try {
            const targetNode = this._findNode(targetNodeId);
            const anchorNode = this._findNode(anchorNodeId);

            if (!targetNode || !anchorNode) {
                throw new Error("Target or anchor node not found");
            }

            const x = anchorNode.pos[0];
            const y = anchorNode.pos[1] + anchorNode.size[1] + margin;

            targetNode.pos = [x, y];
            this._markGraphChanged();

            console.log(`[FL_API] Positioned node ${targetNode.id} below ${anchorNode.id}`);
            return { x, y };
        } catch (error) {
            console.error("[FL_API] positionBottom error:", error);
            throw error;
        }
    }

    /**
     * Move node to the right, avoiding collisions
     * @param {number|string|object} nodeId - Node to move
     * @param {number} margin - Collision margin
     * @returns {object} New position
     */
    moveRight(nodeId, margin = 32) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            // Find rightmost overlapping node
            let maxRight = node.pos[0] + node.size[0];
            for (const otherNode of app.graph._nodes) {
                if (otherNode.id === node.id) continue;

                // Check if vertically overlapping
                const nodeTop = node.pos[1];
                const nodeBottom = node.pos[1] + node.size[1];
                const otherTop = otherNode.pos[1];
                const otherBottom = otherNode.pos[1] + otherNode.size[1];

                if (!(nodeBottom < otherTop || nodeTop > otherBottom)) {
                    // Vertically overlapping
                    const otherRight = otherNode.pos[0] + otherNode.size[0];
                    if (otherRight > maxRight) {
                        maxRight = otherRight;
                    }
                }
            }

            const x = maxRight + margin;
            node.pos[0] = x;
            this._markGraphChanged();

            console.log(`[FL_API] Moved node ${node.id} right to x=${x}`);
            return { x, y: node.pos[1] };
        } catch (error) {
            console.error("[FL_API] moveRight error:", error);
            throw error;
        }
    }

    /**
     * Move node downward, avoiding collisions
     * @param {number|string|object} nodeId - Node to move
     * @param {number} margin - Collision margin
     * @returns {object} New position
     */
    moveBottom(nodeId, margin = 64) {
        try {
            const node = this._findNode(nodeId);
            if (!node) {
                throw new Error(`Node not found: ${nodeId}`);
            }

            // Find bottommost overlapping node
            let maxBottom = node.pos[1] + node.size[1];
            for (const otherNode of app.graph._nodes) {
                if (otherNode.id === node.id) continue;

                // Check if horizontally overlapping
                const nodeLeft = node.pos[0];
                const nodeRight = node.pos[0] + node.size[0];
                const otherLeft = otherNode.pos[0];
                const otherRight = otherNode.pos[0] + otherNode.size[0];

                if (!(nodeRight < otherLeft || nodeLeft > otherRight)) {
                    // Horizontally overlapping
                    const otherBottom = otherNode.pos[1] + otherNode.size[1];
                    if (otherBottom > maxBottom) {
                        maxBottom = otherBottom;
                    }
                }
            }

            const y = maxBottom + margin;
            node.pos[1] = y;
            this._markGraphChanged();

            console.log(`[FL_API] Moved node ${node.id} down to y=${y}`);
            return { x: node.pos[0], y };
        } catch (error) {
            console.error("[FL_API] moveBottom error:", error);
            throw error;
        }
    }

    // ==================== WORKFLOW CONTROL ====================

    listCommands() {
        const commandBridge = this._getCommandBridge();
        const rawCommands = this._unwrap(commandBridge?.commands) || this._unwrap(app.extensionManager?.commands) || [];
        const commands = Array.isArray(rawCommands)
            ? rawCommands
            : rawCommands.values
                ? Array.from(rawCommands.values())
                : Object.values(rawCommands);

        return {
            count: commands.length,
            commands: commands.map((command) => this._serializeCommand(command)).filter(Boolean)
        };
    }

    async executeCommand(commandId) {
        const commandBridge = this._getCommandBridge();
        if (!commandBridge?.execute) {
            throw new Error("ComfyUI command bridge is not available");
        }

        await commandBridge.execute(commandId);
        this._markCanvasDirty();
        return { success: true, command_id: commandId };
    }

    listKeybindings() {
        const commands = this.listCommands().commands;
        return {
            count: commands.length,
            keybindings: commands.map((command) => ({
                id: command.id,
                label: command.label,
                keybinding: command.keybinding
            }))
        };
    }

    async getCurrentWorkflowJSON(apiFormat = false) {
        if (apiFormat) {
            const prompt = await app.graphToPrompt();
            return {
                api_format: true,
                output: prompt.output,
                workflow: prompt.workflow
            };
        }

        return {
            api_format: false,
            workflow: app.graph.serialize()
        };
    }

    async loadWorkflowJSON(workflow, name = null, clean = true, restoreView = true) {
        if (!workflow || typeof workflow !== "object") {
            throw new Error("workflow must be a workflow JSON object");
        }

        await app.loadGraphData(workflow, clean, restoreView, name || null);
        this._markGraphChanged();
        return {
            success: true,
            name,
            node_count: app.graph?._nodes?.length || 0
        };
    }

    async getWorkflowTabs() {
        const workflowStore = this._unwrap(app.extensionManager?.workflow);
        const openWorkflows = this._unwrap(workflowStore?.openWorkflows) || [];
        const activeWorkflow = this._unwrap(workflowStore?.activeWorkflow);

        return {
            active: activeWorkflow ? this._serializeWorkflowTab(activeWorkflow) : null,
            tabs: Array.from(openWorkflows).map((workflow) => this._serializeWorkflowTab(workflow)).filter(Boolean)
        };
    }

    async listWorkflowFiles() {
        if (api.listUserDataFullInfo) {
            return {
                files: await api.listUserDataFullInfo("workflows")
            };
        }

        const response = await api.fetchApi("/userdata?dir=workflows&recurse=true&split=false&full_info=true");
        if (!response.ok && response.status !== 404) {
            throw new Error(`Failed to list workflows: ${response.status} ${response.statusText}`);
        }
        return { files: response.status === 404 ? [] : await response.json() };
    }

    async readWorkflowFile(path) {
        const normalizedPath = this._normalizeWorkflowPath(path);
        const response = await api.getUserData(normalizedPath);
        if (!response.ok) {
            throw new Error(`Failed to read workflow '${normalizedPath}': ${response.status} ${response.statusText}`);
        }
        return {
            path: normalizedPath,
            workflow: await response.json()
        };
    }

    async saveCurrentWorkflow(path, overwrite = true) {
        const normalizedPath = this._normalizeWorkflowPath(path);
        const workflow = app.graph.serialize();
        const response = await api.storeUserData(normalizedPath, workflow, {
            overwrite,
            stringify: true,
            throwOnError: false,
            full_info: true
        });
        if (!response.ok) {
            throw new Error(`Failed to save workflow '${normalizedPath}': ${response.status} ${response.statusText}`);
        }
        const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
        return { success: true, path: normalizedPath, file: data };
    }

    async renameWorkflowFile(path, dest, overwrite = true) {
        const sourcePath = this._normalizeWorkflowPath(path);
        const destPath = this._normalizeWorkflowPath(dest);
        const response = await api.moveUserData(sourcePath, destPath, { overwrite });
        if (!response.ok) {
            throw new Error(`Failed to rename workflow '${sourcePath}' to '${destPath}': ${response.status} ${response.statusText}`);
        }
        const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
        return { success: true, path: sourcePath, dest: destPath, file: data };
    }

    async deleteWorkflowFile(path) {
        const normalizedPath = this._normalizeWorkflowPath(path);
        const response = await api.deleteUserData(normalizedPath);
        if (!response.ok) {
            throw new Error(`Failed to delete workflow '${normalizedPath}': ${response.status} ${response.statusText}`);
        }
        return { success: true, path: normalizedPath };
    }

    async closeCurrentWorkflow() {
        return this.executeCommand("Workspace.CloseWorkflow");
    }

    async duplicateCurrentWorkflow() {
        return this.executeCommand("Comfy.DuplicateWorkflow");
    }

    /**
     * Queue workflow for execution
     * @param {number|null} batchCount - Batch count (null for current)
     * @returns {object} Queue result with prompt_id, queue_number, and node_errors
     */
    async queueWorkflow(batchCount = null) {
        try {
            const queueSettings = this._getQueueSettings();
            if (batchCount !== null) {
                this.setBatchCount(batchCount);
            }

            const effectiveBatchCount = queueSettings?.batchCount || parseInt(app.ui?.batchCount?.value || "1", 10) || 1;
            const prompt = await app.graphToPrompt();
            let queueResult = null;

            for (let i = 0; i < effectiveBatchCount; i++) {
                queueResult = await api.queuePrompt(0, prompt);
            }

            console.log(`[FL_API] Queued workflow (batch: ${effectiveBatchCount})`);
            console.log(`[FL_API] Queue result:`, queueResult);
            
            // Return comprehensive queue information
            return { 
                queued: true, 
                batch_count: effectiveBatchCount,
                prompt_id: queueResult.prompt_id,
                queue_number: queueResult.number,
                node_errors: queueResult.node_errors || {}
            };
        } catch (error) {
            console.error("[FL_API] queueWorkflow error:", error);
            throw error;
        }
    }
    /**
     * Cancel workflow execution
     * @returns {object} Cancel result
     */
    cancelWorkflow() {
        try {
            api.interrupt();
            console.log("[FL_API] Cancelled workflow");
            return { cancelled: true };
        } catch (error) {
            console.error("[FL_API] cancelWorkflow error:", error);
            throw error;
        }
    }

    /**
     * Enable auto-queue mode
     * @returns {object} Result
     */
    enableAutoQueue() {
        try {
            const queueSettings = this._getQueueSettings();
            if (queueSettings) {
                queueSettings.mode = "instant";
            } else if (app.ui) {
                app.ui.autoQueueEnabled = true;
            }
            console.log("[FL_API] Enabled auto-queue");
            return { enabled: true, mode: queueSettings?.mode || "instant" };
        } catch (error) {
            console.error("[FL_API] enableAutoQueue error:", error);
            throw error;
        }
    }

    /**
     * Disable auto-queue mode
     * @returns {object} Result
     */
    disableAutoQueue() {
        try {
            const queueSettings = this._getQueueSettings();
            if (queueSettings) {
                queueSettings.mode = "disabled";
            } else if (app.ui) {
                app.ui.autoQueueEnabled = false;
            }
            console.log("[FL_API] Disabled auto-queue");
            return { enabled: false, mode: queueSettings?.mode || "disabled" };
        } catch (error) {
            console.error("[FL_API] disableAutoQueue error:", error);
            throw error;
        }
    }

    /**
     * Set batch count
     * @param {number} count - Batch count
     * @returns {object} Result
     */
    setBatchCount(count) {
        try {
            const parsed = Math.max(1, parseInt(count, 10) || 1);
            const queueSettings = this._getQueueSettings();
            if (queueSettings) {
                queueSettings.batchCount = parsed;
            } else if (app.ui?.batchCount) {
                app.ui.batchCount.value = parsed;
            }
            console.log(`[FL_API] Set batch count to ${count}`);
            return { count: parsed };
        } catch (error) {
            console.error("[FL_API] setBatchCount error:", error);
            throw error;
        }
    }

    /**
     * Get queue status
     * @returns {object} Queue status
     */
    async getQueueStatus() {
        try {
            const queue = await api.getQueue();
            const queueSettings = this._getQueueSettings();
            const mode = queueSettings?.mode || (app.ui?.autoQueueEnabled ? "instant" : "disabled");
            const batchCount = queueSettings?.batchCount || parseInt(app.ui?.batchCount?.value || "1", 10) || 1;
            console.log("[FL_API] Retrieved queue status");
            return {
                running: queue.Running || [],
                pending: queue.Pending || [],
                auto_queue_enabled: mode !== "disabled",
                auto_queue_mode: mode,
                batch_count: batchCount
            };
        } catch (error) {
            console.error("[FL_API] getQueueStatus error:", error);
            throw error;
        }
    }

    // ==================== SYSTEM CONTROL ====================
    // THIS IS ALL MOVED TO backend/mcp_server.py 'cause this is python level shit.

    /**
     * Send images to external URL
     * @param {string} url - Target URL
     * @param {string} field - Form field name
     * @param {Array} filePaths - File paths or PreviewImage nodes
     * @returns {object} Result
     */
    async sendImages(url, field, filePaths) {
        try {
            // Placeholder - would need actual implementation
            console.log(`[FL_API] sendImages to ${url} (field: ${field})`);
            return { sent: filePaths.length, url, field };
        } catch (error) {
            console.error("[FL_API] sendImages error:", error);
            throw error;
        }
    }

    // ==================== UTILITY ====================

    /**
     * Generate random seed
     * @returns {object} {seed}
     */
    generateSeed() {
        const seed = Math.floor(Math.random() * 1000000000000000);
        console.log(`[FL_API] Generated seed: ${seed}`);
        return { seed };
    }

    /**
     * Generate random float
     * @param {number} min - Minimum value
     * @param {number} max - Maximum value
     * @returns {object} {value}
     */
    generateFloat(min, max) {
        const value = Math.random() * (max - min) + min;
        console.log(`[FL_API] Generated float: ${value}`);
        return { value };
    }

    /**
     * Generate random integer
     * @param {number} min - Minimum value
     * @param {number} max - Maximum value
     * @returns {object} {value}
     */
    generateInt(min, max) {
        const value = Math.floor(Math.random() * (max - min + 1)) + min;
        console.log(`[FL_API] Generated int: ${value}`);
        return { value };
    }

    /**
     * Pick random item from list
     * @param {Array} items - Items to choose from
     * @returns {object} {value}
     */
    randomChoice(items) {
        const value = items[Math.floor(Math.random() * items.length)];
        console.log(`[FL_API] Random choice: ${value}`);
        return { value };
    }

    // ==================== INTERNAL HELPERS ====================

    _unwrap(value) {
        if (value && typeof value === "object" && "value" in value) {
            return value.value;
        }
        return value;
    }

    _getCommandBridge() {
        return this._unwrap(app.extensionManager?.command);
    }

    _getQueueSettings() {
        return this._unwrap(app.extensionManager?.queueSettings) || null;
    }

    _markCanvasDirty() {
        try {
            app.canvas?.setDirty?.(true, true);
        } catch (error) {
            console.debug("[FL_API] Could not mark canvas dirty:", error);
        }
        try {
            app.graph?.setDirtyCanvas?.(true, true);
        } catch (error) {
            console.debug("[FL_API] Could not mark graph canvas dirty:", error);
        }
    }

    _markGraphChanged() {
        this._markCanvasDirty();
        try {
            app.graph?.change?.();
        } catch (error) {
            console.debug("[FL_API] Could not notify graph change:", error);
        }
        try {
            api?.dispatchCustomEvent?.("graphChanged");
        } catch (error) {
            console.debug("[FL_API] Could not dispatch graphChanged:", error);
        }
    }

    _setWidgetValue(node, widget, value) {
        const oldValue = widget.value;
        widget.value = value;

        try {
            widget.callback?.call(widget, value, app.canvas, node, app.canvas?.graph_mouse, {});
        } catch (error) {
            console.debug(`[FL_API] Widget callback failed for ${widget.name}:`, error);
        }

        try {
            node.onWidgetChanged?.(widget.name, value, oldValue, widget);
        } catch (error) {
            console.debug(`[FL_API] Node widget change hook failed for ${widget.name}:`, error);
        }
    }

    _serializeCommand(command) {
        if (!command) {
            return null;
        }

        let keybinding = null;
        try {
            const rawKeybinding = this._unwrap(command.keybinding);
            const combo = rawKeybinding?.combo || rawKeybinding;
            keybinding = rawKeybinding ? {
                key: combo?.key,
                ctrl: Boolean(combo?.ctrl),
                alt: Boolean(combo?.alt),
                shift: Boolean(combo?.shift),
                meta: Boolean(combo?.meta),
                combo: combo?.toString?.() || rawKeybinding?.toString?.() || null
            } : null;
        } catch (error) {
            console.debug(`[FL_API] Could not serialize keybinding for ${command.id}:`, error);
        }

        return {
            id: command.id,
            label: command.label || command.name || command.id,
            tooltip: command.tooltip || null,
            icon: command.icon || null,
            source: command.source || null,
            version_added: command.versionAdded || null,
            keybinding
        };
    }

    _serializeWorkflowTab(workflow) {
        if (!workflow) {
            return null;
        }

        return {
            id: workflow.id || workflow.key || null,
            name: workflow.name || workflow.path || workflow.filename || "Untitled",
            path: workflow.path || null,
            filename: workflow.filename || null,
            is_modified: Boolean(workflow.isModified || workflow.modified || workflow.dirty)
        };
    }

    _normalizeWorkflowPath(path) {
        if (!path || typeof path !== "string") {
            throw new Error("Workflow path is required");
        }

        let normalized = path.replace(/^\/+/, "");
        if (!normalized.startsWith("workflows/")) {
            normalized = `workflows/${normalized}`;
        }
        if (!normalized.endsWith(".json")) {
            normalized = `${normalized}.json`;
        }
        return normalized;
    }

    /**
     * Find node by various criteria
     * @private
     */
    _findNode(query) {
        if (typeof query === "object" && query.id !== undefined) {
            return query;  // Already a node object
        }

        if (typeof query === "number") {
            // Find by ID
            return app.graph._nodes.find(n => n.id === query) || null;
        }

        if (typeof query === "string") {
            // Try as title first, then type
            return app.graph._nodes.find(n => n.title === query) ||
                   app.graph._nodes.find(n => n.type === query || n.comfyClass === query) ||
                   null;
        }

        return null;
    }

    /**
     * Find node by various criteria (from end)
     * @private
     */
    _find(query) {
        // if (query == null) return null;
        if (typeof query === "object" && query.id !== undefined) {
            return query;
        }

        if (typeof query === "number") {
            return app.graph._nodes.find(n => n.id === query) || null;
        }

        if (typeof query === "string") {
            return app.graph._nodes.find(n => n.title === query) ||
                   app.graph._nodes.find(n => n.type === query || n.comfyClass === query) ||
                   null;
        }

        return null;
    }

    /**
     * Find node by various criteria (from end of array)
     * @private
     */
    _findLast(query) {
        if (typeof query === "object" && query.id !== undefined) {
            return query;
        }

        const nodes = app.graph._nodes;

        if (typeof query === "number") {
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodes[i].id === query) return nodes[i];
            }
            return null;
        }

        if (typeof query === "string") {
            // Try title first
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodes[i].title === query) return nodes[i];
            }
            // Then type
            for (let i = nodes.length - 1; i >= 0; i--) {
                if (nodes[i].type === query || nodes[i].comfyClass === query) return nodes[i];
            }
            return null;
        }

        return null;
    }
}
