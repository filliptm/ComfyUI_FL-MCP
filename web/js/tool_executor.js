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
        
        // Set session ID on FL_API for screenshot naming
        if (wsClient.sessionId) {
            this.flApi.setSessionId(wsClient.sessionId);
        }
        
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
            "create_nodes_batch": this._handleCreateNodesBatch.bind(this),
            "remove_nodes": this._handleRemoveNodes.bind(this),
            "bypass_nodes": this._handleBypassNodes.bind(this),
            "unbypass_nodes": this._handleUnbypassNodes.bind(this),
            "pin_nodes": this._handlePinNodes.bind(this),
            "unpin_nodes": this._handleUnpinNodes.bind(this),
            "select_nodes": this._handleSelectNodes.bind(this),
            "get_selected_nodes": this._handleGetSelectedNodes.bind(this),
            "focus_on_nodes": this._handleFocusOnNodes.bind(this),
            "take_screenshot": this._handleTakeScreenshot.bind(this),
            
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

    async _handleCreateNodesBatch(params) {
        const { nodes } = params;

        console.log(`[ToolExecutor] Batch creating ${nodes.length} nodes`);
        const startTime = performance.now();

        // Create all nodes synchronously in one loop - no await between iterations
        const results = [];
        for (const nodeSpec of nodes) {
            try {
                // Convert flattened schema (x, y) to position dict for fl_api
                let position = null;
                if (nodeSpec.x !== undefined || nodeSpec.y !== undefined) {
                    position = {
                        x: nodeSpec.x ?? 0,
                        y: nodeSpec.y ?? 0
                    };
                }

                const result = this.flApi.create(
                    nodeSpec.node_type,
                    {}, // No parameters in simplified schema
                    position
                );
                results.push({
                    success: true,
                    node_id: result.node_id,
                    node_type: nodeSpec.node_type
                });
            } catch (error) {
                console.error(`[ToolExecutor] Failed to create node ${nodeSpec.node_type}:`, error);
                results.push({
                    success: false,
                    node_type: nodeSpec.node_type,
                    error: error.message
                });
            }
        }

        const elapsed = performance.now() - startTime;
        console.log(`[ToolExecutor] Batch created ${results.length} nodes in ${elapsed.toFixed(2)}ms`);

        return results;
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

    /**
     * Handle focus_on_nodes tool request
     * @private
     */
    async _handleFocusOnNodes(params) {
        try {
            const { node_ids } = params;
            const result = this.flApi.fitView(node_ids);
            return result;
        } catch (error) {
            throw new Error(`Failed to fit view: ${error.message}`);
        }
    }

    /**
     * Handle take_screenshot tool request
     * @private
     */
    async _handleTakeScreenshot(params) {
        try {
            const { format = 'jpeg', quality = 0.9 } = params;
            
            // Take screenshot
            const screenshotData = await this.flApi.takeScreenshot(format, quality);
            
            // Send screenshot data to backend via WebSocket
            await this.wsClient.send({
                type: 'screenshot',
                session_id: this.wsClient.sessionId,
                ...screenshotData
            });
            
            // Return result (backend will save the file)
            const ext = format === 'png' ? 'png' : 'jpg';
            return {
                success: true,
                screenshot_id: screenshotData.screenshot_id,
                filename: `${screenshotData.screenshot_id}.${ext}`,
                format: format,
                size_bytes: screenshotData.size_bytes
            };
            
        } catch (error) {
            throw new Error(`Failed to take screenshot: ${error.message}`);
        }
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
        try {
            // Detect mode
            const isAutoLayout = params.auto_layout === true;
            const hasManualLayout = params.node_rects != null;
            
            // CASE: Neither mode specified
            if (!isAutoLayout && !hasManualLayout) {
                console.warn('[ToolExecutor] modify_layout: No layout mode specified');
                return [];
            }
            
            // MODE 1: Auto-layout
            if (isAutoLayout) {
                const options = {
                    auto_layout: true,
                    node_ids: params.node_ids || null,
                    strategy: params.strategy || null,
                    spacing_multiplier: params.spacing_multiplier || null
                };
                
                const results = await this.flApi.modifyLayout(null, options);
                
                const successful = results.filter(r => r.success).length;
                const failed = results.filter(r => !r.success).length;
                console.log(`[ToolExecutor] Auto-layout complete: ${results.length} nodes (${successful} success, ${failed} failed)`);
                
                return results;
            }
            
            // MODE 2: Manual layout
            if (hasManualLayout) {
                // Safely handle empty array
                if (!Array.isArray(params.node_rects) || params.node_rects.length === 0) {
                    console.warn('[ToolExecutor] modify_layout: Empty node_rects array');
                    return [];
                }
                
                // Convert flattened List[NodeRect] to Dict[int, NodeRect] for fl_api
                // Backend sends: [{node_id: 1, x: 10, y: 20}, {node_id: 2, x: 30, y: 40}]
                // fl_api expects: {1: {x: 10, y: 20}, 2: {x: 30, y: 40}}
                const rectsDict = {};
                for (const rect of params.node_rects) {
                    if (rect && rect.node_id != null) {
                        const { node_id, ...rectData } = rect;
                        rectsDict[node_id] = rectData;
                    }
                }
                
                const results = await this.flApi.modifyLayout(rectsDict, {});
                
                const successful = results.filter(r => r.success).length;
                const failed = results.filter(r => !r.success).length;
                console.log(`[ToolExecutor] Modified layout: ${results.length} nodes (${successful} success, ${failed} failed)`);
                
                return results;
            }
            
        } catch (error) {
            console.error('[ToolExecutor] modify_layout error:', error);
            // Return structured error instead of throwing
            return [
                {
                    success: false,
                    error: error.message || String(error)
                }
            ];
        }
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
