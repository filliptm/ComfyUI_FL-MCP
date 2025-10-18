/**
 * Query Executor - Executes structured queries against the workflow graph
 * 
 * Implements a JSON-based query DSL for filtering, traversing, and aggregating
 * workflow nodes. Supports multiple result formats including diagrams.
 * 
 * @module query_executor
 */

import { app } from "../../scripts/app.js";

/**
 * QueryExecutor class - Executes queries against the workflow
 */
export class QueryExecutor {
    constructor() {
        this.app = app;
        console.log("[QueryExecutor] Initialized");
    }

    /**
     * Execute a workflow query
     * @param {object} query - Query object
     * @returns {any} Query result
     */
    execute(query) {
        try {
            // Start with all nodes
            let nodes = this.getAllNodes();
            
            // Apply filters
            if (query.filters) {
                nodes = this.applyFilters(nodes, query.filters);
            }
            
            // Apply traversal
            if (query.traversal) {
                nodes = this.applyTraversal(nodes, query.traversal);
            }
            
            // Apply sorting
            if (query.sort) {
                nodes = this.applySort(nodes, query.sort);
            }
            
            // Apply pagination
            if (query.offset || query.limit) {
                const start = query.offset || 0;
                const end = query.limit ? start + query.limit : undefined;
                nodes = nodes.slice(start, end);
            }
            
            // Apply aggregation
            if (query.aggregation) {
                return this.applyAggregation(nodes, query.aggregation);
            }
            
            // Format results
            return this.formatResults(nodes, query);
        } catch (error) {
            console.error("[QueryExecutor] Query execution failed:", error);
            throw error;
        }
    }

    /**
     * Get all nodes from the workflow
     * @returns {Array} Array of serialized nodes
     */
    getAllNodes() {
        const nodes = [];
        const graph = this.app.graph;
        
        if (!graph || !graph._nodes) {
            return nodes;
        }
        
        for (const node of graph._nodes) {
            nodes.push(this.serializeNode(node));
        }
        
        return nodes;
    }

    /**
     * Apply filters to nodes
     * @param {Array} nodes - Nodes to filter
     * @param {object} filterGroup - Filter group specification
     * @returns {Array} Filtered nodes
     */
    applyFilters(nodes, filterGroup) {
        const { operator, filters, groups } = filterGroup;
        
        return nodes.filter(node => {
            // Evaluate individual filters
            const filterResults = (filters || []).map(f => this.evaluateFilter(node, f));
            
            // Evaluate nested groups
            const groupResults = (groups || []).map(g => 
                this.applyFilters([node], g).length > 0
            );
            
            const allResults = [...filterResults, ...groupResults];
            
            // Apply logical operator
            if (operator === 'and') {
                return allResults.every(r => r);
            } else if (operator === 'or') {
                return allResults.some(r => r);
            } else if (operator === 'not') {
                return !allResults.every(r => r);
            }
            return false;
        });
    }

    /**
     * Evaluate a single filter condition
     * @param {object} node - Node to evaluate
     * @param {object} filter - Filter specification
     * @returns {boolean} Filter result
     */
    evaluateFilter(node, filter) {
        const value = this.getNestedValue(node, filter.field);
        const targetValue = filter.value;
        
        switch (filter.operator) {
            case 'equals':
                return value === targetValue;
            case 'not_equals':
                return value !== targetValue;
            case 'contains':
                return String(value).includes(String(targetValue));
            case 'not_contains':
                return !String(value).includes(String(targetValue));
            case 'starts_with':
                return String(value).startsWith(String(targetValue));
            case 'ends_with':
                return String(value).endsWith(String(targetValue));
            case 'gt':
                return value > targetValue;
            case 'lt':
                return value < targetValue;
            case 'gte':
                return value >= targetValue;
            case 'lte':
                return value <= targetValue;
            case 'in':
                return Array.isArray(targetValue) && targetValue.includes(value);
            case 'not_in':
                return Array.isArray(targetValue) && !targetValue.includes(value);
            case 'exists':
                return value !== undefined && value !== null;
            case 'not_exists':
                return value === undefined || value === null;
            default:
                return false;
        }
    }

    /**
     * Get nested value from object using dot notation
     * @param {object} obj - Object to traverse
     * @param {string} path - Dot-separated path
     * @returns {any} Value at path
     */
    getNestedValue(obj, path) {
        return path.split('.').reduce((current, key) => current?.[key], obj);
    }

    /**
     * Apply graph traversal
     * @param {Array} startNodes - Starting nodes
     * @param {object} traversal - Traversal specification
     * @returns {Array} Traversed nodes
     */
    applyTraversal(startNodes, traversal) {
        const visited = new Set();
        const result = [];
        
        const traverse = (node, depth = 0) => {
            if (visited.has(node.id)) return;
            if (traversal.max_depth !== null && depth > traversal.max_depth) return;
            
            visited.add(node.id);
            result.push(node);
            
            // Check stop condition
            if (traversal.stop_at && this.applyFilters([node], traversal.stop_at).length > 0) {
                return;
            }
            
            // Get connected nodes
            const connections = this.getConnections(node, traversal.direction);
            
            for (const connectedId of connections) {
                const connectedNode = this.getNodeById(connectedId);
                if (!connectedNode) continue;
                
                // Filter by node type if specified
                if (traversal.node_types && !traversal.node_types.includes(connectedNode.type)) {
                    continue;
                }
                
                traverse(connectedNode, depth + 1);
            }
        };
        
        for (const node of startNodes) {
            traverse(node);
        }
        
        return result;
    }

    /**
     * Get connected node IDs
     * @param {object} node - Node to get connections from
     * @param {string} direction - Direction ('upstream', 'downstream', 'both')
     * @returns {Array} Array of connected node IDs
     */
    getConnections(node, direction) {
        const connections = [];
        
        if (direction === 'upstream' || direction === 'both') {
            // Get nodes connected to inputs
            for (const input of node.connections.inputs || []) {
                for (const conn of input.connected_to || []) {
                    connections.push(conn.node_id);
                }
            }
        }
        
        if (direction === 'downstream' || direction === 'both') {
            // Get nodes connected to outputs
            for (const output of node.connections.outputs || []) {
                for (const conn of output.connected_to || []) {
                    connections.push(conn.node_id);
                }
            }
        }
        
        return [...new Set(connections)];
    }

    /**
     * Apply sorting
     * @param {Array} nodes - Nodes to sort
     * @param {Array} sortSpecs - Sort specifications
     * @returns {Array} Sorted nodes
     */
    applySort(nodes, sortSpecs) {
        return nodes.sort((a, b) => {
            for (const spec of sortSpecs) {
                const aVal = this.getNestedValue(a, spec.field);
                const bVal = this.getNestedValue(b, spec.field);
                
                if (aVal < bVal) return spec.order === 'asc' ? -1 : 1;
                if (aVal > bVal) return spec.order === 'asc' ? 1 : -1;
            }
            return 0;
        });
    }

    /**
     * Apply aggregation
     * @param {Array} nodes - Nodes to aggregate
     * @param {object} aggregation - Aggregation specification
     * @returns {object} Aggregation result
     */
    applyAggregation(nodes, aggregation) {
        switch (aggregation.type) {
            case 'count':
                return { count: nodes.length };
            
            case 'sum':
                return {
                    sum: nodes.reduce((acc, n) => 
                        acc + (this.getNestedValue(n, aggregation.field) || 0), 0
                    )
                };
            
            case 'avg':
                const sum = nodes.reduce((acc, n) => 
                    acc + (this.getNestedValue(n, aggregation.field) || 0), 0
                );
                return { avg: nodes.length > 0 ? sum / nodes.length : 0 };
            
            case 'min':
                const values = nodes.map(n => this.getNestedValue(n, aggregation.field));
                return { min: Math.min(...values) };
            
            case 'max':
                const maxValues = nodes.map(n => this.getNestedValue(n, aggregation.field));
                return { max: Math.max(...maxValues) };
            
            case 'list':
                return {
                    list: nodes.map(n => this.getNestedValue(n, aggregation.field))
                };
            
            case 'first':
                return nodes.length > 0 ? nodes[0] : null;
            
            case 'last':
                return nodes.length > 0 ? nodes[nodes.length - 1] : null;
            
            default:
                return { count: nodes.length };
        }
    }

    /**
     * Format query results
     * @param {Array} nodes - Nodes to format
     * @param {object} query - Original query
     * @returns {any} Formatted results
     */
    formatResults(nodes, query) {
        const format = query.result_format || 'full';
        
        switch (format) {
            case 'ids':
                return nodes.map(n => n.id);
            
            case 'summary':
                return nodes.map(n => ({
                    id: n.id,
                    type: n.type,
                    title: n.title
                }));
            
            case 'diagram':
                return this.generateDiagram(nodes);
            
            case 'full':
            default:
                return nodes.map(n => {
                    const result = { ...n };
                    if (!query.include_connections) {
                        delete result.connections;
                    }
                    if (!query.include_position) {
                        delete result.position;
                    }
                    return result;
                });
        }
    }

    /**
     * Serialize a ComfyUI node to standard format
     * @param {object} node - LiteGraph node
     * @returns {object} Serialized node
     */
    serializeNode(node) {
        return {
            id: node.id,
            type: node.comfyClass || node.type,
            title: node.title || node.type,
            position: {
                x: node.pos[0],
                y: node.pos[1]
            },
            size: {
                width: node.size[0],
                height: node.size[1]
            },
            mode: node.mode,
            parameters: this.extractParameters(node),
            connections: this.extractConnections(node)
        };
    }

    /**
     * Extract parameters from node widgets
     * @param {object} node - LiteGraph node
     * @returns {object} Parameters object
     */
    extractParameters(node) {
        const params = {};
        if (node.widgets) {
            for (const widget of node.widgets) {
                params[widget.name] = widget.value;
            }
        }
        return params;
    }

    /**
     * Extract connections from node
     * @param {object} node - LiteGraph node
     * @returns {object} Connections object
     */
    extractConnections(node) {
        const inputs = [];
        const outputs = [];
        
        // Extract inputs
        if (node.inputs) {
            for (let i = 0; i < node.inputs.length; i++) {
                const input = node.inputs[i];
                const connectedTo = [];
                
                if (input.link !== null && input.link !== undefined) {
                    const link = node.graph.links[input.link];
                    if (link) {
                        connectedTo.push({
                            node_id: link.origin_id,
                            slot: link.origin_slot
                        });
                    }
                }
                
                inputs.push({
                    slot: input.name,
                    type: input.type,
                    connected_to: connectedTo
                });
            }
        }
        
        // Extract outputs
        if (node.outputs) {
            for (let i = 0; i < node.outputs.length; i++) {
                const output = node.outputs[i];
                const connectedTo = [];
                
                if (output.links) {
                    for (const linkId of output.links) {
                        const link = node.graph.links[linkId];
                        if (link) {
                            connectedTo.push({
                                node_id: link.target_id,
                                slot: link.target_slot
                            });
                        }
                    }
                }
                
                outputs.push({
                    slot: output.name,
                    type: output.type,
                    connected_to: connectedTo
                });
            }
        }
        
        return { inputs, outputs };
    }

    /**
     * Generate Mermaid diagram from nodes
     * @param {Array} nodes - Nodes to include in diagram
     * @returns {string} Mermaid diagram string
     */
    generateDiagram(nodes) {
        let diagram = 'graph LR\n';
        
        // Create a set of node IDs for quick lookup
        const nodeIds = new Set(nodes.map(n => n.id));
        
        for (const node of nodes) {
            const nodeLabel = `N${node.id}["${this.escapeLabel(node.title || node.type)}"]`;
            
            // Add connections
            for (const output of node.connections.outputs || []) {
                for (const conn of output.connected_to || []) {
                    // Only show connections to nodes in the result set
                    if (nodeIds.has(conn.node_id)) {
                        const slotLabel = this.escapeLabel(output.slot);
                        diagram += `  N${node.id} -->|${slotLabel}| N${conn.node_id}\n`;
                    }
                }
            }
        }
        
        return diagram;
    }

    /**
     * Escape label for Mermaid diagram
     * @param {string} label - Label to escape
     * @returns {string} Escaped label
     */
    escapeLabel(label) {
        return String(label)
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    /**
     * Get node by ID
     * @param {number} id - Node ID
     * @returns {object|null} Serialized node or null
     */
    getNodeById(id) {
        const graph = this.app.graph;
        if (!graph || !graph._nodes) return null;
        
        const node = graph._nodes.find(n => n.id === id);
        return node ? this.serializeNode(node) : null;
    }

    /**
     * Check if an input slot is required for a node
     * @param {object} rawNode - Raw LiteGraph node
     * @param {object} input - Input slot
     * @returns {boolean} True if the input is required
     */
    isInputSlotRequired(rawNode, input) {
        // In ComfyUI, most inputs are required unless they have default values or are optional
        // We'll use several heuristics to determine if an input is required:
        
        // 1. Check if the input type ends with "?" (optional marker)
        if (input.type && input.type.endsWith('?')) {
            return false;
        }
        
        // 2. Check common optional slot names
        const optionalSlots = [
            'seed', 'noise_seed', 'control_after_generate', 'force_full_denoise',
            'return_with_leftover_noise', 'disable_noise', 'preview_method',
            'vae_decode', 'tiled', 'tile_size', 'fast', 'steps', 'cfg',
            'sampler_name', 'scheduler', 'positive', 'negative', 'latent_image',
            'denoise', 'model', 'clip', 'vae', 'image', 'mask', 'filename_prefix'
        ];
        
        const slotName = input.name ? input.name.toLowerCase() : '';
        
        // 3. Some slots are commonly optional
        if (optionalSlots.some(optional => slotName.includes(optional))) {
            // But still check if there's a widget with the same name (which would make it not required)
            if (rawNode.widgets) {
                const hasWidget = rawNode.widgets.some(w => w.name && w.name.toLowerCase() === slotName);
                if (hasWidget) {
                    return false;
                }
            }
        }
        
        // 4. Check if there's a corresponding widget that provides a default value
        if (rawNode.widgets && input.name) {
            const correspondingWidget = rawNode.widgets.find(w => w.name === input.name);
            if (correspondingWidget && correspondingWidget.value !== undefined && correspondingWidget.value !== null) {
                return false;
            }
        }
        
        // 5. Some node types have specific patterns
        const nodeType = rawNode.comfyClass || rawNode.type;
        if (nodeType) {
            // Loader nodes typically require their main input
            if (nodeType.includes('Loader') || nodeType.includes('Load')) {
                if (slotName.includes('name') || slotName.includes('path') || slotName.includes('ckpt')) {
                    return true;
                }
            }
            
            // Sampler nodes typically require model, positive, negative, latent
            if (nodeType.includes('Sampler') || nodeType.includes('Sample')) {
                if (['model', 'positive', 'negative', 'latent_image'].includes(slotName)) {
                    return true;
                }
            }
            
            // VAE nodes typically require their inputs
            if (nodeType.includes('VAE')) {
                if (['samples', 'images', 'vae'].includes(slotName)) {
                    return true;
                }
            }
        }
        
        // 6. Default: assume inputs without widgets are required
        // This is a reasonable default for ComfyUI where most data flow inputs are required
        return true;
    }

    /**
     * Get workflow overview with enhanced connection analysis
     * @returns {object} Workflow overview
     */
    getWorkflowOverview() {
        const nodes = this.getAllNodes();
        const rawNodes = this.app.graph ? this.app.graph._nodes : [];
        
        // Count nodes by type
        const typeCount = {};
        for (const node of nodes) {
            typeCount[node.type] = (typeCount[node.type] || 0) + 1;
        }
        
        // Find disconnected nodes (completely isolated)
        const disconnected = nodes.filter(n => {
            const hasInputs = n.connections.inputs.some(i => i.connected_to.length > 0);
            const hasOutputs = n.connections.outputs.some(o => o.connected_to.length > 0);
            return !hasInputs && !hasOutputs;
        });
        
        // Find nodes with missing required slot connections
        const requiredSlotsMissing = [];
        
        for (const node of nodes) {
            // Find the corresponding raw node to access more detailed information
            const rawNode = rawNodes.find(rn => rn.id === node.id);
            if (!rawNode) continue;
            
            const missingSlots = [];
            
            // Check each input slot
            for (const input of node.connections.inputs) {
                // Skip if already connected
                if (input.connected_to.length > 0) {
                    continue;
                }
                
                // Find the corresponding raw input
                const rawInput = rawNode.inputs ? rawNode.inputs.find(ri => ri.name === input.slot) : null;
                if (!rawInput) continue;
                
                // Check if this input is required
                if (this.isInputSlotRequired(rawNode, rawInput)) {
                    missingSlots.push({
                        slot_name: input.slot,
                        slot_type: input.type,
                        reason: 'Required input not connected'
                    });
                }
            }
            
            // If this node has missing required slots, add it to the list
            if (missingSlots.length > 0) {
                requiredSlotsMissing.push({
                    id: node.id,
                    type: node.type,
                    title: node.title,
                    missing_slots: missingSlots,
                    missing_count: missingSlots.length
                });
            }
        }
        
        return {
            total_nodes: nodes.length,
            node_types: typeCount,
            disconnected_nodes: disconnected.map(n => ({
                id: n.id,
                type: n.type,
                title: n.title
            })),
            required_slots_missing: requiredSlotsMissing,
            connection_issues: {
                completely_disconnected: disconnected.length,
                missing_required_connections: requiredSlotsMissing.length,
                total_missing_slots: requiredSlotsMissing.reduce((sum, node) => sum + node.missing_count, 0)
            },
            diagram: this.generateDiagram(nodes)
        };
    }
}
