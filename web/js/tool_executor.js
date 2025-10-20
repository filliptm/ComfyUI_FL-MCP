/**
 * Tool Executor - Handles tool execution requests from the backend
 * 
 * This module receives tool execution requests via WebSocket, routes them to
 * the appropriate FL_API methods, and sends results back to the backend.
 * 
 * @module tool_executor
 */

import { FL_API } from "./fl_api.js";
import { QueryExecutor } from "./query_executor.js";

/**
 * ToolExecutor class - Executes tools and manages execution history
 */
export class ToolExecutor {
    constructor(wsClient) {
        this.wsClient = wsClient;
        this.flApi = new FL_API();
        this.queryExecutor = new QueryExecutor();
        this.executionLog = [];
        this.maxLogEntries = 100;
        
        // Register tool handlers
        this.toolHandlers = this._registerHandlers();
        
        console.log("[ToolExecutor] Initialized with", Object.keys(this.toolHandlers).length, "tools");
    }

    /**
     * Register all tool handlers
     * @private
     */
    _registerHandlers() {
        return {
            // Query & Analysis
            "query_workflow": this._handleQueryWorkflow.bind(this),
            "workflow_overview": this._handleWorkflowOverview.bind(this),
            "workflow_diagram": this._handleWorkflowDiagram.bind(this),
            
            // Node Management
            "find_node": this._handleFindNode.bind(this),
            "create_node": this._handleCreateNode.bind(this),
            "remove_nodes": this._handleRemoveNodes.bind(this),
            "bypass_nodes": this._handleBypassNodes.bind(this),
            "unbypass_nodes": this._handleUnbypassNodes.bind(this),
            "pin_nodes": this._handlePinNodes.bind(this),
            "unpin_nodes": this._handleUnpinNodes.bind(this),
            "select_nodes": this._handleSelectNodes.bind(this),
            "get_selected_nodes": this._handleGetSelectedNodes.bind(this),
            
            // Node Manipulation
            "get_node_values": this._handleGetNodeValues.bind(this),
            "set_node_values": this._handleSetNodeValues.bind(this),
            "connect_nodes": this._handleConnectNodes.bind(this),
            "get_node_slots": this._handleGetNodeSlots.bind(this),
            "connect_nodes_batch": this._handleConnectNodesBatch.bind(this),
            "auto_connect_workflow": this._handleAutoConnectWorkflow.bind(this),
            
            // Layout Management
            "get_node_rect": this._handleGetNodeRect.bind(this),
            "get_layout": this._handleGetLayout.bind(this),
            "set_node_rect": this._handleSetNodeRect.bind(this),
            "modify_layout": this._handleModifyLayout.bind(this),
            "position_node_left": this._handlePositionNodeLeft.bind(this),
            "position_node_right": this._handlePositionNodeRight.bind(this),
            "position_node_top": this._handlePositionNodeTop.bind(this),
            "position_node_bottom": this._handlePositionNodeBottom.bind(this),
            "move_node_right": this._handleMoveNodeRight.bind(this),
            "move_node_bottom": this._handleMoveNodeBottom.bind(this),
            
            // Workflow Control
            "queue_workflow": this._handleQueueWorkflow.bind(this),
            "cancel_workflow": this._handleCancelWorkflow.bind(this),
            "enable_auto_queue": this._handleEnableAutoQueue.bind(this),
            "disable_auto_queue": this._handleDisableAutoQueue.bind(this),
            "set_batch_count": this._handleSetBatchCount.bind(this),
            "get_queue_status": this._handleGetQueueStatus.bind(this),
            
            // System Control
            "disable_sleep": this._handleDisableSleep.bind(this),
            "enable_sleep": this._handleEnableSleep.bind(this),
            "disable_screensaver": this._handleDisableScreensaver.bind(this),
            "enable_screensaver": this._handleEnableScreensaver.bind(this),
            "send_images": this._handleSendImages.bind(this),
            
            // Utilities
            "generate_seed": this._handleGenerateSeed.bind(this),
            "generate_float": this._handleGenerateFloat.bind(this),
            "generate_int": this._handleGenerateInt.bind(this),
            "random_choice": this._handleRandomChoice.bind(this)
        };
    }

    /**
     * Execute a tool request
     * @param {object} message - Tool request message from backend
     */
    async executeToolRequest(message) {
        const { request_id, tool_name, parameters } = message;
        const startTime = performance.now();
        
        console.log(`[ToolExecutor] 🚀 START: ${tool_name} (request_id: ${request_id})`);
        console.log(`[ToolExecutor] Parameters:`, parameters);
        
        try {
            // Find handler
            const handler = this.toolHandlers[tool_name];
            if (!handler) {
                throw new Error(`Unknown tool: ${tool_name}`);
            }
            
            // Execute handler
            console.log(`[ToolExecutor] Executing handler for ${tool_name}...`);
            const result = await handler(parameters);
            const executionTime = performance.now() - startTime;
            
            console.log(`[ToolExecutor] Handler completed for ${tool_name}, execution time: ${executionTime.toFixed(2)}ms`);
            
            // Log execution
            this._logExecution({
                request_id,
                tool_name,
                parameters,
                success: true,
                result,
                execution_time_ms: executionTime
            });
            
            // Send success result
            console.log(`[ToolExecutor] 📤 SENDING RESULT: ${tool_name} (request_id: ${request_id})`);
            await this.wsClient.send({
                type: "tool_result",
                request_id: request_id,
                success: true,
                data: result,
                execution_time_ms: executionTime
            });
            
            console.log(
                `[ToolExecutor] ✅ SUCCESS: ${tool_name} ` +
                `(${executionTime.toFixed(2)}ms)`
            );
            
        } catch (error) {
            const executionTime = performance.now() - startTime;
            
            console.error(`[ToolExecutor] ❌ ERROR in ${tool_name}:`, error);
            
            // Log error
            this._logExecution({
                request_id,
                tool_name,
                parameters,
                success: false,
                error: error.message,
                execution_time_ms: executionTime
            });
            
            // Send error result
            console.log(`[ToolExecutor] 📤 SENDING ERROR RESULT: ${tool_name} (request_id: ${request_id})`);
            await this.wsClient.send({
                type: "tool_result",
                request_id: request_id,
                success: false,
                error: error.message,
                execution_time_ms: executionTime
            });
            
            console.error(
                `[ToolExecutor] ❌ ERROR: ${tool_name} - ${error.message} ` +
                `(${executionTime.toFixed(2)}ms)`
            );
        }
    }

    /**
     * Log tool execution
     * @private
     */
    _logExecution(entry) {
        entry.timestamp = new Date().toISOString();
        this.executionLog.push(entry);
        
        // Keep only last N entries
        if (this.executionLog.length > this.maxLogEntries) {
            this.executionLog.shift();
        }
    }

    /**
     * Get execution log
     * @param {number} limit - Number of entries to return (default: all)
     * @returns {Array} Execution log entries
     */
    getExecutionLog(limit = null) {
        if (limit === null) {
            return [...this.executionLog];
        }
        return this.executionLog.slice(-limit);
    }

    /**
     * Clear execution log
     */
    clearExecutionLog() {
        this.executionLog = [];
        console.log("[ToolExecutor] Execution log cleared");
    }

    // ==================== QUERY & ANALYSIS HANDLERS ====================

    async _handleQueryWorkflow(params) {
        return this.queryExecutor.execute(params);
    }

    async _handleWorkflowOverview(params) {
        return this.queryExecutor.getWorkflowOverview();
    }

    async _handleWorkflowDiagram(params) {
        const { node_ids } = params;
        
        if (node_ids) {
            // Get specific nodes
            const nodes = node_ids.map(id => this.queryExecutor.getNodeById(id)).filter(n => n !== null);
            return { diagram: this.queryExecutor.generateDiagram(nodes) };
        } else {
            // Get all nodes
            const nodes = this.queryExecutor.getAllNodes();
            return { diagram: this.queryExecutor.generateDiagram(nodes) };
        }
    }

    // ==================== NODE MANAGEMENT HANDLERS ====================

    async _handleFindNode(params) {
        const { node_id, node_type, title, find_last } = params;
        
        let query;
        if (node_id !== undefined) {
            query = node_id;
        } else if (node_type !== undefined) {
            query = node_type;
        } else if (title !== undefined) {
            query = title;
        } else {
            throw new Error("Must provide node_id, node_type, or title");
        }
        
        const node = this.flApi.find(query, find_last || false);
        
        if (!node) {
            return { found: false, node: null };
        }
        
        return {
            found: true,
            node: {
                id: node.id,
                type: node.comfyClass || node.type,
                title: node.title,
                position: { x: node.pos[0], y: node.pos[1] },
                size: { width: node.size[0], height: node.size[1] },
                mode: node.mode
            }
        };
    }

    async _handleCreateNode(params) {
        const { node_type, parameters, position } = params;
        return this.flApi.create(node_type, parameters || {}, position || null);
    }

    async _handleRemoveNodes(params) {
        const { node_ids } = params;
        const count = this.flApi.remove(node_ids);
        return { removed_count: count };
    }

    async _handleBypassNodes(params) {
        const { node_ids } = params;
        const count = this.flApi.bypass(node_ids);
        return { bypassed_count: count };
    }

    async _handleUnbypassNodes(params) {
        const { node_ids } = params;
        const count = this.flApi.unbypass(node_ids);
        return { unbypassed_count: count };
    }

    async _handlePinNodes(params) {
        const { node_ids } = params;
        const count = this.flApi.pin(node_ids);
        return { pinned_count: count };
    }

    async _handleUnpinNodes(params) {
        const { node_ids } = params;
        const count = this.flApi.unpin(node_ids);
        return { unpinned_count: count };
    }

    async _handleSelectNodes(params) {
        const { node_ids } = params;
        const count = this.flApi.selectNodes(node_ids);
        return { selected_count: count };
    }

    async _handleGetSelectedNodes(params) {
        const nodes = this.flApi.getSelectedNodes();
        return { nodes };
    }

    // ==================== NODE MANIPULATION HANDLERS ====================

    async _handleGetNodeValues(params) {
        const { node_id } = params;
        const values = this.flApi.getValues(node_id);
        return { node_id, values };
    }

    async _handleSetNodeValues(params) {
        const { node_id, values } = params;
        const updatedValues = this.flApi.setValues(node_id, values);
        return { node_id, values: updatedValues };
    }

    async _handleConnectNodes(params) {
        const { 
            source_node_id, 
            source_slot, 
            target_node_id, 
            target_slot,
            auto_match,
            match_strategy
        } = params;
        
        const options = {
            auto_match: auto_match !== false,  // Default true
            match_strategy: match_strategy || "type"
        };
        
        const result = this.flApi.connect(
            source_node_id,
            source_slot !== undefined ? source_slot : null,
            target_node_id,
            target_slot !== undefined ? target_slot : null,
            options
        );
        
        return { 
            connected: true,
            connection: result
        };
    }

    async _handleGetNodeSlots(params) {
        const { node_id } = params;
        return this.flApi.getNodeSlots(node_id);
    }

    async _handleConnectNodesBatch(params) {
        const { connections, auto_match, stop_on_error } = params;
        
        const options = {
            auto_match: auto_match !== false,
            stop_on_error: stop_on_error || false
        };
        
        return this.flApi.connectBatch(connections, options);
    }

    async _handleAutoConnectWorkflow(params) {
        const { node_ids, strategy } = params;
        return this.flApi.autoConnectWorkflow(node_ids, strategy || "sequential");
    }

    // ==================== LAYOUT MANAGEMENT HANDLERS ====================

    async _handleGetNodeRect(params) {
        const { node_id } = params;
        const rect = this.flApi.getRect(node_id);
        return { node_id, rect };
    }

    async _handleGetLayout(params) {
        const { node_ids } = params;
        const layout = this.flApi.getLayout(node_ids);
        return { layout };
    }

    async _handleSetNodeRect(params) {
        const { node_id, x, y, width, height } = params;
        const rect = this.flApi.setRect(
            node_id,
            x !== undefined ? x : null,
            y !== undefined ? y : null,
            width !== undefined ? width : null,
            height !== undefined ? height : null
        );
        return { node_id, rect };
    }

    async _handleModifyLayout(params) {
        const { node_rects } = params;
        const results = this.flApi.modifyLayout(node_rects);
        return { 
            results,
            total_processed: results.length,
            successful: results.filter(r => r.success).length,
            failed: results.filter(r => !r.success).length
        };
    }

    async _handlePositionNodeLeft(params) {
        const { target_node_id, anchor_node_id, margin } = params;
        this.flApi.positionLeft(target_node_id, anchor_node_id, margin || 32);
        return { positioned: true };
    }

    async _handlePositionNodeRight(params) {
        const { target_node_id, anchor_node_id, margin } = params;
        this.flApi.positionRight(target_node_id, anchor_node_id, margin || 32);
        return { positioned: true };
    }

    async _handlePositionNodeTop(params) {
        const { target_node_id, anchor_node_id, margin } = params;
        this.flApi.positionTop(target_node_id, anchor_node_id, margin || 64);
        return { positioned: true };
    }

    async _handlePositionNodeBottom(params) {
        const { target_node_id, anchor_node_id, margin } = params;
        this.flApi.positionBottom(target_node_id, anchor_node_id, margin || 64);
        return { positioned: true };
    }

    async _handleMoveNodeRight(params) {
        const { node_id, margin } = params;
        this.flApi.moveRight(node_id, margin || 32);
        return { moved: true };
    }

    async _handleMoveNodeBottom(params) {
        const { node_id, margin } = params;
        this.flApi.moveBottom(node_id, margin || 64);
        return { moved: true };
    }

    // ==================== WORKFLOW CONTROL HANDLERS ====================

    async _handleQueueWorkflow(params) {
        const { batch_count } = params;
        return await this.flApi.queueWorkflow(batch_count || null);
    }

    async _handleCancelWorkflow(params) {
        return await this.flApi.cancelWorkflow();
    }

    async _handleEnableAutoQueue(params) {
        return this.flApi.enableAutoQueue();
    }

    async _handleDisableAutoQueue(params) {
        return this.flApi.disableAutoQueue();
    }

    async _handleSetBatchCount(params) {
        const { count } = params;
        return this.flApi.setBatchCount(count);
    }

    async _handleGetQueueStatus(params) {
        return this.flApi.getQueueStatus();
    }

    // ==================== SYSTEM CONTROL HANDLERS ====================

    async _handleDisableSleep(params) {
        return await this.flApi.disableSleep();
    }

    async _handleEnableSleep(params) {
        return await this.flApi.enableSleep();
    }

    async _handleDisableScreensaver(params) {
        return await this.flApi.disableScreensaver();
    }

    async _handleEnableScreensaver(params) {
        return await this.flApi.enableScreensaver();
    }

    async _handleSendImages(params) {
        const { url, field, file_paths } = params;
        return await this.flApi.sendImages(url, field, file_paths);
    }

    // ==================== UTILITY HANDLERS ====================

    async _handleGenerateSeed(params) {
        const seed = this.flApi.generateSeed();
        return { seed };
    }

    async _handleGenerateFloat(params) {
        const { min, max } = params;
        const value = this.flApi.generateFloat(min, max);
        return { value };
    }

    async _handleGenerateInt(params) {
        const { min, max } = params;
        const value = this.flApi.generateInt(min, max);
        return { value };
    }

    async _handleRandomChoice(params) {
        const { items } = params;
        const choice = this.flApi.randomChoice(items);
        return { choice };
    }
}
