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
import {
    applyWorkflowPlanAtomic,
    WORKFLOW_APPLICATION_PROPERTY,
} from "./workflow_plan_apply.js";
import {
    applyWorkflowRefinementAtomic,
    WORKFLOW_REFINEMENT_PROPERTY,
} from "./workflow_refinement_apply.js";
import {
    applyWorkflowGraphPatchAtomic,
    WORKFLOW_GRAPH_PATCH_PROPERTY,
} from "./workflow_graph_patch_apply.js";
import {
    buildGraphPatchSchemaContexts,
    enrichGraphPatchNode,
} from "./node_schema_contract.js";
import {
    canonicalNarrowOperationHash,
    narrowEditOperationLedger,
} from "./narrow_edit_idempotency.js";
import {
    completeDurableQueueOperation,
    discardDurableQueueOperation,
    readDurableQueueOperation,
    reserveDurableQueueOperation,
} from "./queue_operation_store.js";

const WORKFLOW_REVEAL_DELAYS_MS = Object.freeze({
    node: 500,
    connection: 500,
});

const CANVAS_MUTATION_TOOL_NAMES = new Set([
    "frontend_execute_command",
    "create_node",
    "create_nodes_batch",
    "apply_workflow_plan",
    "apply_workflow_refinement",
    "apply_workflow_graph_patch",
    "navigate_workflow_branch",
    "remove_nodes",
    "bypass_nodes",
    "unbypass_nodes",
    "pin_nodes",
    "unpin_nodes",
    "place_chat_image_in_node",
    "edit_node_mask",
    "confirm_mask_review",
    "set_node_values",
    "set_node_values_exact",
    "connect_nodes",
    "connect_nodes_batch",
    "auto_connect_workflow",
    "set_node_rect",
    "modify_layout",
    "position_node_left",
    "position_node_right",
    "position_node_top",
    "position_node_bottom",
    "move_node_right",
    "move_node_bottom",
    "queue_workflow",
    "enable_auto_queue",
    "disable_auto_queue",
    "set_batch_count",
    "workflow_load_json",
    "workflow_save_current",
    "workflow_rename_file",
    "workflow_delete_file",
    "workflow_close_current",
    "workflow_duplicate_current",
]);

let canvasMutationActive = false;
// Survive ToolExecutor reconstruction after a websocket reconnect. A full page
// reload is an intentional recovery boundary and naturally clears this module
// state together with the potentially stale graph runtime.
let workflowMutationQuarantine = null;

const TOOL_CONTRACT_REVISION = Symbol("flMcpToolContractRevision");


function withToolContractRevision(handler, revision) {
    Object.defineProperty(handler, TOOL_CONTRACT_REVISION, {
        value: revision,
        enumerable: false,
        configurable: false,
        writable: false,
    });
    return handler;
}


function toolContractRevision(handler) {
    return handler?.[TOOL_CONTRACT_REVISION] ?? 1;
}


/** Own the one frontend canvas mutation slot or fail before any delayed work is queued. */
async function withCanvasMutationLock(operation) {
    if (canvasMutationActive) {
        const error = new Error(
            "Another canvas mutation is already active; retry after it finishes.",
        );
        error.code = "canvas_mutation_busy";
        error.details = { retryable: true };
        throw error;
    }
    canvasMutationActive = true;
    try {
        return await operation();
    } finally {
        canvasMutationActive = false;
    }
}

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
        // A GraphPatch that mutated the canvas but could not prove an exact
        // rollback leaves the active workflow unsafe for any further write or
        // queue action.  Keep the quarantine in the browser process, where all
        // canvas mutations converge, until the user reloads/switches workflow.
        this.workflowMutationQuarantine = workflowMutationQuarantine;
        
        // Set session ID on FL_API for screenshot naming
        if (wsClient.sessionId) {
            this.flApi.setSessionId(wsClient.sessionId);
        }
        
        // Register tool handlers
        this.toolHandlers = this._registerHandlers();
        
        console.log("[ToolExecutor] Initialized with", Object.keys(this.toolHandlers).length, "tools");
    }

    /**
     * Return the exact handler manifest implemented by this runtime instance.
     */
    getSupportedTools() {
        return Object.keys(this.toolHandlers).sort();
    }

    /**
     * Return per-handler wire contract revisions from the same registered map.
     * A revision changes when an existing tool name gains execution semantics
     * that an older browser runtime cannot safely provide.
     */
    getToolContractRevisions() {
        return Object.fromEntries(
            Object.keys(this.toolHandlers)
                .sort()
                .map(toolName => [
                    toolName,
                    toolContractRevision(this.toolHandlers[toolName]),
                ]),
        );
    }

    /**
     * Register all tool handlers
     * @private
     */
    _registerHandlers() {
        const handlers = {
            // Query & Analysis
            "query_workflow": this._handleQueryWorkflow.bind(this),
            "workflow_overview": this._handleWorkflowOverview.bind(this),
            "workflow_diagram": this._handleWorkflowDiagram.bind(this),
            "frontend_list_commands": this._handleFrontendListCommands.bind(this),
            "frontend_execute_command": this._handleFrontendExecuteCommand.bind(this),
            "frontend_list_keybindings": this._handleFrontendListKeybindings.bind(this),
            
            // Node Management
            "find_node": this._handleFindNode.bind(this),
            "create_node": this._handleCreateNode.bind(this),
            "create_nodes_batch": this._handleCreateNodesBatch.bind(this),
            "apply_workflow_plan": this._handleApplyWorkflowPlan.bind(this),
            "apply_workflow_refinement": this._handleApplyWorkflowRefinement.bind(this),
            "apply_workflow_graph_patch": withToolContractRevision(
                this._handleApplyWorkflowGraphPatch.bind(this),
                3,
            ),
            "remove_nodes": this._handleRemoveNodes.bind(this),
            "bypass_nodes": this._handleBypassNodes.bind(this),
            "unbypass_nodes": this._handleUnbypassNodes.bind(this),
            "pin_nodes": this._handlePinNodes.bind(this),
            "unpin_nodes": this._handleUnpinNodes.bind(this),
            "select_nodes": this._handleSelectNodes.bind(this),
            "get_selected_nodes": withToolContractRevision(
                this._handleGetSelectedNodes.bind(this),
                2,
            ),
            "focus_on_nodes": this._handleFocusOnNodes.bind(this),
            "navigate_workflow_branch": this._handleNavigateWorkflowBranch.bind(this),
            "take_screenshot": this._handleTakeScreenshot.bind(this),
            
            // Node Manipulation
            "get_node_values": this._handleGetNodeValues.bind(this),
            "get_node_values_exact": this._handleGetNodeValuesExact.bind(this),
            "get_node_image_ref": withToolContractRevision(
                this._handleGetNodeImageRef.bind(this),
                2,
            ),
            "get_canvas_image_refs": withToolContractRevision(
                this._handleGetCanvasImageRefs.bind(this),
                1,
            ),
            "place_chat_image_in_node": this._handlePlaceChatImageInNode.bind(this),
            "edit_node_mask": withToolContractRevision(
                this._handleEditNodeMask.bind(this),
                5,
            ),
            "confirm_mask_review": withToolContractRevision(
                this._handleConfirmMaskReview.bind(this),
                3,
            ),
            "recover_narrow_operation": this._handleRecoverNarrowOperation.bind(this),
            "set_node_values": this._handleSetNodeValues.bind(this),
            "set_node_values_exact": withToolContractRevision(
                this._handleSetNodeValuesExact.bind(this),
                4,
            ),
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
            "queue_workflow": withToolContractRevision(
                this._handleQueueWorkflow.bind(this),
                3,
            ),
            "cancel_workflow": this._handleCancelWorkflow.bind(this),
            "enable_auto_queue": this._handleEnableAutoQueue.bind(this),
            "disable_auto_queue": this._handleDisableAutoQueue.bind(this),
            "set_batch_count": this._handleSetBatchCount.bind(this),
            "get_queue_status": this._handleGetQueueStatus.bind(this),
            "workflow_get_current_json": this._handleWorkflowGetCurrentJSON.bind(this),
            "workflow_load_json": this._handleWorkflowLoadJSON.bind(this),
            "workflow_get_tabs": this._handleWorkflowGetTabs.bind(this),
            "workflow_list_files": this._handleWorkflowListFiles.bind(this),
            "workflow_read_file": this._handleWorkflowReadFile.bind(this),
            "workflow_save_current": this._handleWorkflowSaveCurrent.bind(this),
            "workflow_rename_file": this._handleWorkflowRenameFile.bind(this),
            "workflow_delete_file": this._handleWorkflowDeleteFile.bind(this),
            "workflow_close_current": this._handleWorkflowCloseCurrent.bind(this),
            "workflow_duplicate_current": this._handleWorkflowDuplicateCurrent.bind(this),
            
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
        for (const toolName of CANVAS_MUTATION_TOOL_NAMES) {
            // Refinement owns the shared lock inside its public handler so direct
            // calls and registered calls follow the same path without nesting.
            if (["apply_workflow_refinement", "apply_workflow_graph_patch"].includes(toolName)) {
                continue;
            }
            const handler = handlers[toolName];
            if (handler) {
                const wrappedHandler = params => {
                    this._assertWorkflowMutationAllowed(toolName);
                    return withCanvasMutationLock(() => handler(params));
                };
                handlers[toolName] = withToolContractRevision(
                    wrappedHandler,
                    toolContractRevision(handler),
                );
            }
        }
        return handlers;
    }

    _assertWorkflowMutationAllowed(toolName) {
        const quarantine = this.workflowMutationQuarantine;
        if (!quarantine) return;
        let activeIdentity = null;
        try {
            activeIdentity = this.flApi.getActiveWorkflowIdentity();
        } catch (_) {
            // An unavailable identity is not proof that the compromised graph
            // was replaced. Keep the quarantine until a positively different
            // workflow identity is observed or the page reloads.
            activeIdentity = quarantine.workflow_identity;
        }
        if (activeIdentity !== quarantine.workflow_identity) {
            this.workflowMutationQuarantine = null;
            workflowMutationQuarantine = null;
            return;
        }
        const error = new Error(
            "The active workflow could not be restored exactly after a failed edit. "
            + "Reload the ComfyUI page or reopen the workflow before changing or running it.",
        );
        error.code = "workflow_state_compromised";
        error.details = {
            tool_name: toolName,
            workflow_identity: activeIdentity,
            failed_application_id: quarantine.application_id,
            failed_patch_hash: quarantine.patch_hash,
            retryable: false,
            recovery: "reload_or_reopen_workflow",
        };
        throw error;
    }

    _quarantineIncompleteGraphPatch(result, expectedWorkflowIdentity) {
        const rollback = result?.rollback;
        if (
            result?.success !== false
            || rollback?.attempted !== true
            || rollback?.complete === true
        ) {
            return result;
        }
        this.workflowMutationQuarantine = Object.freeze({
            workflow_identity: expectedWorkflowIdentity,
            application_id: result?.application_id ?? null,
            patch_hash: result?.patch_hash ?? null,
        });
        workflowMutationQuarantine = this.workflowMutationQuarantine;
        return {
            ...result,
            workflow_state_compromised: true,
            mutation_quarantined: true,
            recovery: "reload_or_reopen_workflow",
        };
    }

    _quarantineExactValueMutation(expectedWorkflowIdentity) {
        this.workflowMutationQuarantine = Object.freeze({
            workflow_identity: expectedWorkflowIdentity,
            application_id: "update-connected-prompt",
            patch_hash: null,
        });
        workflowMutationQuarantine = this.workflowMutationQuarantine;
        const compromised = new Error(
            "The exact prompt transaction could not prove the requested value state. "
            + "Reload the ComfyUI page or reopen the workflow before changing it.",
        );
        compromised.code = "workflow_state_compromised";
        compromised.details = {
            workflow_identity: expectedWorkflowIdentity,
            mutation_quarantined: true,
            retryable: false,
            recovery: "reload_or_reopen_workflow",
        };
        return compromised;
    }

    /**
     * Execute a tool request
     * @param {object} message - Tool request message from backend
     */
    async executeToolRequest(message) {
        const { request_id, tool_name, parameters } = message;
        const startTime = performance.now();
        const hasOperationPayload = Object.prototype.hasOwnProperty.call(
            parameters || {},
            "operation_payload",
        );
        const loggedParameters = tool_name === "set_node_values_exact"
            ? {
                ...parameters,
                value: "[redacted]",
                ...(hasOperationPayload
                    ? { operation_payload: "[redacted-canonical-payload]" }
                    : {}),
                ...(Object.prototype.hasOwnProperty.call(parameters || {}, "expected_current_value")
                    ? { expected_current_value: "[redacted]" }
                    : {}),
            }
            : hasOperationPayload
                ? { ...parameters, operation_payload: "[redacted-canonical-payload]" }
                : parameters;
        
        console.log(`[ToolExecutor] 🚀 START: ${tool_name} (request_id: ${request_id})`);
        console.log(`[ToolExecutor] Parameters:`, loggedParameters);
        
        try {
            const expectedWorkflow = message.workflow;
            if (expectedWorkflow?.id) {
                const activeWorkflow = this.flApi.getActiveWorkflowContext();
                if (!activeWorkflow || activeWorkflow.id !== expectedWorkflow.id) {
                    const activeName = activeWorkflow?.name || "no workflow";
                    throw new Error(
                        `workflow_context_changed: Ren started on "${expectedWorkflow.name || "Workflow"}", `
                        + `but "${activeName}" is active. Retry from the active workflow's Ren chat.`
                    );
                }
            }

            // Find handler
            const handler = this.toolHandlers[tool_name];
            if (!handler) {
                throw new Error(`Unknown tool: ${tool_name}`);
            }
            
            // Execute handler
            console.log(`[ToolExecutor] Executing handler for ${tool_name}...`);
            const result = await handler(parameters);
            const loggedResult = tool_name === "get_node_values_exact"
                ? { ...result, values: "[redacted]" }
                : result;
            const executionTime = performance.now() - startTime;
            
            console.log(`[ToolExecutor] Handler completed for ${tool_name}, execution time: ${executionTime.toFixed(2)}ms`);
            
            // Log execution
            this._logExecution({
                request_id,
                tool_name,
                parameters: loggedParameters,
                success: true,
                result: loggedResult,
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
            const errorCode = typeof error?.code === "string"
                ? error.code
                : "tool_execution_failed";
            const transmittedError = tool_name === "queue_workflow"
                ? (
                    errorCode === "queue_outcome_unknown"
                        ? "The queue outcome is unknown; it was not queued again."
                        : "Workflow queueing failed safely."
                )
                : error.message;
            const errorDetails = tool_name === "queue_workflow"
                ? null
                : error?.details;
            
            if (tool_name === "queue_workflow") {
                console.error(`[ToolExecutor] ❌ ERROR in ${tool_name}: ${errorCode}`);
            } else {
                console.error(`[ToolExecutor] ❌ ERROR in ${tool_name}:`, error);
            }
            
            // Log error
            this._logExecution({
                request_id,
                tool_name,
                parameters: loggedParameters,
                success: false,
                error: transmittedError,
                error_code: errorCode,
                ...(errorDetails ? { error_details: errorDetails } : {}),
                execution_time_ms: executionTime
            });
            
            // Send error result
            console.log(`[ToolExecutor] 📤 SENDING ERROR RESULT: ${tool_name} (request_id: ${request_id})`);
            await this.wsClient.send({
                type: "tool_result",
                request_id: request_id,
                success: false,
                error: transmittedError,
                error_code: errorCode,
                ...(errorDetails ? { error_details: errorDetails } : {}),
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

    async _handleFrontendListCommands(params) {
        return this.flApi.listCommands();
    }

    async _handleFrontendExecuteCommand(params) {
        const { command_id } = params;
        return await this.flApi.executeCommand(command_id);
    }

    async _handleFrontendListKeybindings(params) {
        return this.flApi.listKeybindings();
    }

    // ==================== NODE MANAGEMENT HANDLERS ====================

    async _handleFindNode(params) {
        const { node_id, node_type, title, find_last } = params;
        
        let query;
        if (node_id !== undefined) {
            query = { by: "id", value: node_id };
        } else if (node_type !== undefined) {
            query = { by: "type", value: node_type };
        } else if (title !== undefined) {
            query = { by: "title", value: title };
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
                        x: nodeSpec.x,
                        y: nodeSpec.y
                    };
                }

                const result = this.flApi.create(
                    nodeSpec.node_type,
                    {}, // No parameters in simplified schema
                    position
                );
                results.push({
                    success: true,
                    node_id: result.id,
                    node_type: nodeSpec.node_type,
                    title: result.title,
                    position: result.position,
                    size: result.size,
                    placement_adjusted: result.placement_adjusted,
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

    async _handleApplyWorkflowPlan(params) {
        const autoQueueState = this.flApi.pauseAutoQueue();
        const attachmentAliases = new Set(
            (params?.plan?.attachments || []).map(binding => binding.node_alias),
        );
        const adapter = {
            findApplicationNodes: applicationId => this.flApi.findNodesByProperty(
                WORKFLOW_APPLICATION_PROPERTY,
                "application_id",
                applicationId,
            ),
            createNode: plannedNode => {
                const created = this.flApi.create(
                    plannedNode.node_type,
                    {},
                    null,
                    attachmentAliases.has(plannedNode.alias)
                        ? { preferred_size: { width: 420, height: 340 } }
                        : {},
                );
                if (Object.keys(plannedNode.values || {}).length > 0) {
                    this.flApi.setValues(created.id, plannedNode.values);
                }
                return created;
            },
            setNodeMetadata: (nodeId, metadata) => this.flApi.setNodeProperty(
                nodeId,
                WORKFLOW_APPLICATION_PROPERTY,
                metadata,
            ),
            getNodeValues: nodeId => this.flApi.getValues(nodeId),
            assignAttachment: (nodeId, binding) => this.flApi.placeChatImageInNode(
                binding.image,
                nodeId,
                { focus: false },
            ),
            connectNodes: (sourceId, targetId, connection) => this.flApi.connect(
                sourceId,
                connection.source_output_index,
                targetId,
                connection.target_input,
                { auto_match: false },
            ),
            connectionExists: (sourceId, targetId, connection) => {
                const slots = this.flApi.getNodeSlots(targetId);
                const input = slots.inputs.find(item => item.name === connection.target_input);
                return Boolean(
                    input?.connected
                    && String(input.connected_from?.node_id) === String(sourceId)
                    && input.connected_from?.slot_index === connection.source_output_index
                );
            },
            listConnections: nodeIds => this.flApi.getConnectionsForNodeIds(nodeIds),
            removeNodes: nodeIds => this.flApi.remove(nodeIds),
            nodeExists: nodeId => this.flApi.nodeExists(nodeId),
            // Keep the deterministic transaction visibly legible on the canvas:
            // nodes arrive first, then their validated links are drawn in sequence.
            afterMutationStep: step => new Promise(resolve => setTimeout(
                resolve,
                WORKFLOW_REVEAL_DELAYS_MS[step?.phase] ?? 160,
            )),
        };
        try {
            return await applyWorkflowPlanAtomic(params, adapter);
        } finally {
            this.flApi.restoreAutoQueue(autoQueueState);
        }
    }

    async _handleApplyWorkflowRefinement(params) {
        this._assertWorkflowMutationAllowed("apply_workflow_refinement");
        return await withCanvasMutationLock(
            () => this._applyWorkflowRefinementSerialized(params),
        );
    }

    async _handleApplyWorkflowGraphPatch(params) {
        this._assertWorkflowMutationAllowed("apply_workflow_graph_patch");
        const result = await withCanvasMutationLock(
            () => this._applyWorkflowGraphPatchSerialized(params),
        );
        return this._quarantineIncompleteGraphPatch(
            result,
            params?.plan?.expected_workflow_identity,
        );
    }

    async _applyWorkflowGraphPatchSerialized(params) {
        const expectedWorkflowIdentity = params?.plan?.expected_workflow_identity;
        const workflowPin = this.flApi.pinActiveWorkflow(expectedWorkflowIdentity);
        const plan = params?.plan || {};
        const nodeTypes = new Set([
            ...(plan.assertions?.nodes || []).map(item => item.node_type),
            ...(plan.create_nodes || []).map(item => item.node_type),
            ...(plan.update_nodes || []).map(item => item.node_type),
            ...(plan.remove_nodes || []).map(item => item.node_type),
        ]);
        const catalog = await this.flApi.getNodeDefinitions([...nodeTypes]);
        this.flApi.assertActiveWorkflow(workflowPin);
        const schemaContexts = await buildGraphPatchSchemaContexts(
            plan,
            catalog,
            params?.schema_contracts,
        );
        this.flApi.assertActiveWorkflow(workflowPin);
        const createdContextsById = new Map();
        const contextForId = nodeId => (
            createdContextsById.get(String(nodeId))
            || schemaContexts.get(`existing:${String(nodeId)}`)
            || null
        );
        const autoQueueState = this.flApi.pauseAutoQueue();
        let changeTransaction = null;
        let result;
        let operationError = null;
        try {
            changeTransaction = this.flApi.beginWorkflowChangeTransaction(workflowPin);
            const mutationGuard = await this.flApi.createWorkflowMutationGuard(workflowPin);
            const readGuarded = async operation => {
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                const result = await operation();
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                return result;
            };
            const mutationGuarded = async operation => {
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                const result = await operation();
                await this.flApi.acceptWorkflowMutationGuard(mutationGuard);
                return result;
            };
            const createdNodeGuarded = async (operation, target = {}) => {
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                const result = operation();
                if (result && typeof result.then === "function") {
                    throw new Error("GraphPatch node creation must remain synchronous.");
                }
                const checkpoint = this.flApi.captureCreatedNodeNormalizationCheckpoint(
                    mutationGuard,
                    {
                        node_id: result?.node_id ?? result?.id,
                        node_type: target.node_type,
                        definition_id: target.definition_id ?? null,
                    },
                );
                await this.flApi.acceptCreatedNodeNormalization(
                    mutationGuard,
                    checkpoint,
                );
                return result;
            };
            const adapter = {
                withReadGuard: operation => readGuarded(operation),
                captureWorkflow: () => readGuarded(
                    () => this.flApi.captureWorkflowSnapshot(workflowPin),
                ),
                restoreWorkflow: async snapshot => {
                    const restored = await this.flApi.restoreWorkflowSnapshot(
                        snapshot,
                        workflowPin,
                    );
                    await this.flApi.acceptWorkflowMutationGuard(mutationGuard);
                    return restored;
                },
                getNode: nodeId => readGuarded(() => {
                    const observed = this.flApi.getWorkflowNode(nodeId, workflowPin);
                    return enrichGraphPatchNode(observed, contextForId(nodeId));
                }),
                listConnections: () => readGuarded(
                    () => this.flApi.listWorkflowConnections(workflowPin),
                ),
                createNode: plannedNode => createdNodeGuarded(() => {
                    const created = typeof this.flApi.createWorkflowNodeExact === "function"
                        ? this.flApi.createWorkflowNodeExact(
                            plannedNode.node_type,
                            {},
                            null,
                            {},
                            workflowPin,
                        )
                        : this.flApi.create(plannedNode.node_type, {}, null);
                    const context = schemaContexts.get(`new:${plannedNode.alias}`);
                    if (!context) {
                        throw new Error(`No schema context exists for ${plannedNode.alias}.`);
                    }
                    createdContextsById.set(String(created.id), context);
                    return created;
                }, { node_type: plannedNode.node_type }),
                setNodeValuesExact: (nodeId, values) => mutationGuarded(
                    () => this.flApi.setValuesExact(nodeId, values, workflowPin),
                ),
                setNodeMetadata: (nodeId, metadata) => mutationGuarded(
                    () => typeof this.flApi.setWorkflowNodePropertyExact === "function"
                        ? this.flApi.setWorkflowNodePropertyExact(
                            nodeId,
                            WORKFLOW_GRAPH_PATCH_PROPERTY,
                            metadata,
                            workflowPin,
                        )
                        : this.flApi.setNodeProperty(
                            nodeId,
                            WORKFLOW_GRAPH_PATCH_PROPERTY,
                            metadata,
                        ),
                ),
                setNodeLayoutExact: (nodeId, layout) => mutationGuarded(
                    () => typeof this.flApi.setWorkflowNodeRectExact === "function"
                        ? this.flApi.setWorkflowNodeRectExact(nodeId, layout, workflowPin)
                        : this.flApi.setRect(nodeId, layout),
                ),
                assignAttachmentExact: (nodeId, attachment) => mutationGuarded(
                    () => this.flApi.assignAttachmentExact(nodeId, attachment, workflowPin),
                ),
                verifyAttachmentExact: (nodeId, attachment) => readGuarded(
                    () => this.flApi.verifyAttachmentExact(nodeId, attachment, workflowPin),
                ),
                convertWidgetToInput: (nodeId, expected) => mutationGuarded(
                    () => this.flApi.convertWidgetToInputExact(nodeId, expected, workflowPin),
                ),
                disconnectConnection: edge => mutationGuarded(
                    () => this.flApi.disconnectWorkflowConnection(edge, workflowPin),
                ),
                connectNodes: (sourceId, targetId, connection) => mutationGuarded(
                    () => this.flApi.connectWorkflowNodesExact(
                        sourceId,
                        targetId,
                        connection,
                        workflowPin,
                    ),
                ),
                removeNodes: nodeIds => mutationGuarded(
                    () => typeof this.flApi.removeWorkflowNodesExact === "function"
                        ? this.flApi.removeWorkflowNodesExact(nodeIds, workflowPin)
                        : this.flApi.remove(nodeIds),
                ),
                setWorkflowExtra: (key, value) => mutationGuarded(
                    () => this.flApi.setWorkflowExtra(key, value, workflowPin),
                ),
                resolveScopedGraph: async descriptor => {
                    const runtime = await readGuarded(
                        () => this.flApi.createWorkflowGraphPatchScopeRuntime(
                            descriptor,
                            workflowPin,
                        ),
                    );
                    return {
                        withReadGuard: operation => readGuarded(operation),
                        captureDefinition: () => readGuarded(
                            () => runtime.captureDefinition(),
                        ),
                        captureWorkflow: () => readGuarded(
                            () => runtime.captureWorkflow(),
                        ),
                        getNode: nodeId => readGuarded(() => {
                            const observed = runtime.getNode(nodeId);
                            return enrichGraphPatchNode(observed, contextForId(nodeId));
                        }),
                        listConnections: () => readGuarded(
                            () => runtime.listConnections(),
                        ),
                        createNode: plannedNode => createdNodeGuarded(() => {
                            const created = runtime.createNode(plannedNode);
                            const context = schemaContexts.get(`new:${plannedNode.alias}`);
                            if (!context) {
                                throw new Error(`No schema context exists for ${plannedNode.alias}.`);
                            }
                            createdContextsById.set(String(created.id), context);
                            return created;
                        }, {
                            node_type: plannedNode.node_type,
                            definition_id: descriptor.scope.definition_id,
                        }),
                        setNodeValuesExact: (nodeId, values) => mutationGuarded(
                            () => runtime.setNodeValuesExact(nodeId, values),
                        ),
                        setNodeMetadata: (nodeId, metadata) => mutationGuarded(
                            () => runtime.setNodeMetadata(
                                nodeId,
                                WORKFLOW_GRAPH_PATCH_PROPERTY,
                                metadata,
                            ),
                        ),
                        setNodeLayoutExact: (nodeId, layout) => mutationGuarded(
                            () => runtime.setNodeLayoutExact(nodeId, layout),
                        ),
                        convertWidgetToInput: (nodeId, expected) => mutationGuarded(
                            () => runtime.convertWidgetToInput(nodeId, expected),
                        ),
                        disconnectConnection: edge => mutationGuarded(
                            () => runtime.disconnectConnection(edge),
                        ),
                        connectNodes: (sourceId, targetId, connection) => mutationGuarded(
                            () => runtime.connectNodes(sourceId, targetId, connection),
                        ),
                        removeNodes: nodeIds => mutationGuarded(
                            () => runtime.removeNodes(nodeIds),
                        ),
                        afterMutationStep: async step => {
                            await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                            await new Promise(resolve => setTimeout(
                                resolve,
                                Number.isInteger(step?.delay_ms) ? step.delay_ms : 160,
                            ));
                            await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                        },
                    };
                },
                afterMutationStep: async step => {
                    await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                    await new Promise(resolve => setTimeout(
                        resolve,
                        Number.isInteger(step?.delay_ms) ? step.delay_ms : 160,
                    ));
                    await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                },
            };
            result = await applyWorkflowGraphPatchAtomic(params, adapter);
        } catch (error) {
            operationError = error;
        }

        const cleanupWarnings = [];
        if (changeTransaction) {
            try {
                await this.flApi.endWorkflowChangeTransaction(changeTransaction);
            } catch (error) {
                cleanupWarnings.push({
                    phase: "end_workflow_change_transaction",
                    message: String(error?.message || error),
                });
            }
        }
        try {
            await this.flApi.restoreAutoQueue(autoQueueState);
        } catch (error) {
            cleanupWarnings.push({
                phase: "restore_auto_queue",
                message: String(error?.message || error),
            });
        }

        if (operationError) {
            if (cleanupWarnings.length > 0 && operationError && typeof operationError === "object") {
                operationError.cleanup_warnings = cleanupWarnings;
            }
            throw operationError;
        }
        if (cleanupWarnings.length > 0 && result && typeof result === "object") {
            return { ...result, cleanup_warnings: cleanupWarnings };
        }
        return result;
    }

    async _applyWorkflowRefinementSerialized(params) {
        const expectedWorkflowIdentity = params?.plan?.expected_workflow_identity;
        const workflowPin = this.flApi.pinActiveWorkflow(expectedWorkflowIdentity);
        const autoQueueState = this.flApi.pauseAutoQueue();
        let changeTransaction = null;
        try {
            changeTransaction = this.flApi.beginWorkflowChangeTransaction(workflowPin);
            const mutationGuard = await this.flApi.createWorkflowMutationGuard(workflowPin);
            const readGuarded = async operation => {
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                const result = await operation();
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                return result;
            };
            const mutationGuarded = async operation => {
                await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                const result = await operation();
                await this.flApi.acceptWorkflowMutationGuard(mutationGuard);
                return result;
            };
            const adapter = {
                withReadGuard: operation => readGuarded(operation),
                captureWorkflow: () => readGuarded(
                    () => this.flApi.captureWorkflowSnapshot(workflowPin),
                ),
                restoreWorkflow: async snapshot => {
                    const restored = await this.flApi.restoreWorkflowSnapshot(
                        snapshot,
                        workflowPin,
                    );
                    await this.flApi.acceptWorkflowMutationGuard(mutationGuard);
                    return restored;
                },
                getNode: nodeId => this.flApi.getWorkflowNode(nodeId, workflowPin),
                listConnections: () => this.flApi.listWorkflowConnections(workflowPin),
                createNode: plannedNode => mutationGuarded(() => {
                    const created = typeof this.flApi.createWorkflowNodeExact === "function"
                        ? this.flApi.createWorkflowNodeExact(
                            plannedNode.node_type,
                            {},
                            null,
                            {},
                            workflowPin,
                        )
                        : this.flApi.create(plannedNode.node_type, {}, null);
                    if (Object.keys(plannedNode.values || {}).length > 0) {
                        this.flApi.setValues(created.id, plannedNode.values);
                    }
                    return created;
                }),
                setNodeMetadata: (nodeId, metadata) => mutationGuarded(
                    () => typeof this.flApi.setWorkflowNodePropertyExact === "function"
                        ? this.flApi.setWorkflowNodePropertyExact(
                            nodeId,
                            WORKFLOW_REFINEMENT_PROPERTY,
                            metadata,
                            workflowPin,
                        )
                        : this.flApi.setNodeProperty(
                            nodeId,
                            WORKFLOW_REFINEMENT_PROPERTY,
                            metadata,
                        ),
                ),
                disconnectConnection: edge => mutationGuarded(
                    () => this.flApi.disconnectWorkflowConnection(edge, workflowPin),
                ),
                connectNodes: (sourceId, targetId, connection) => mutationGuarded(
                    () => this.flApi.connectWorkflowNodesExact(
                        sourceId,
                        targetId,
                        connection,
                        workflowPin,
                    ),
                ),
                removeNodes: nodeIds => mutationGuarded(
                    () => typeof this.flApi.removeWorkflowNodesExact === "function"
                        ? this.flApi.removeWorkflowNodesExact(nodeIds, workflowPin)
                        : this.flApi.remove(nodeIds),
                ),
                setWorkflowExtra: (key, value) => mutationGuarded(
                    () => this.flApi.setWorkflowExtra(key, value, workflowPin),
                ),
                afterMutationStep: async step => {
                    await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                    await new Promise(resolve => setTimeout(
                        resolve,
                        WORKFLOW_REVEAL_DELAYS_MS[step?.phase] ?? 160,
                    ));
                    await this.flApi.assertWorkflowMutationGuard(mutationGuard);
                },
            };
            return await applyWorkflowRefinementAtomic(params, adapter);
        } finally {
            try {
                if (changeTransaction) {
                    this.flApi.endWorkflowChangeTransaction(changeTransaction);
                }
            } finally {
                this.flApi.restoreAutoQueue(autoQueueState);
            }
        }
    }

    async _handleRemoveNodes(params) {
        const { node_ids } = params;
        const result = this.flApi.remove(node_ids);
        return { removed_count: result.removed };
    }

    async _handleBypassNodes(params) {
        const { node_ids } = params;
        const result = this.flApi.bypass(node_ids);
        return { bypassed_count: result.bypassed };
    }

    async _handleUnbypassNodes(params) {
        const { node_ids } = params;
        const result = this.flApi.unbypass(node_ids);
        return { unbypassed_count: result.unbypassed };
    }

    async _handlePinNodes(params) {
        const { node_ids } = params;
        const result = this.flApi.pin(node_ids);
        return { pinned_count: result.pinned };
    }

    async _handleUnpinNodes(params) {
        const { node_ids } = params;
        const result = this.flApi.unpin(node_ids);
        return { unpinned_count: result.unpinned };
    }

    async _handleSelectNodes(params) {
        const { node_ids } = params;
        const result = this.flApi.selectNodes(node_ids);
        return { selected_count: result.selected };
    }

    async _handleGetSelectedNodes(params) {
        if (!params?.expected_workflow_identity || !params?.expected_graph_hash) {
            return { nodes: this.flApi.getSelectedNodes() };
        }
        const workflowPin = this.flApi.pinActiveWorkflow(params.expected_workflow_identity);
        const guard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (guard.expectedGraphHash !== params.expected_graph_hash) {
            const error = new Error("The graph changed before the exact selection read.");
            error.code = "graph_hash_precondition_failed";
            throw error;
        }
        const nodes = this.flApi.getSelectedNodes(workflowPin);
        await this.flApi.assertWorkflowMutationGuard(guard);
        return {
            nodes,
            workflow_identity: params.expected_workflow_identity,
            graph_hash: guard.expectedGraphHash,
        };
    }

    async _handleNavigateWorkflowBranch(params) {
        return await this.flApi.navigateWorkflowBranchExact(params);
    }

    /**
     * Handle focus_on_nodes tool request
     * @private
     */
    async _handleFocusOnNodes(params) {
        try {
            const { node_ids } = params;
            const result = await this.flApi.fitView(node_ids);
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
            const {
                format = 'jpeg',
                quality = 0.9,
                fit_view = true,
                node_ids = []
            } = params;

            let fitResult = null;
            if (fit_view) {
                fitResult = await this.flApi.fitView(node_ids);
            }
            
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
                size_bytes: screenshotData.size_bytes,
                fit_view: fit_view,
                fit_result: fitResult
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

    async _handleGetNodeValuesExact(params) {
        const workflowPin = this.flApi.pinActiveWorkflow(
            params.expected_workflow_identity,
        );
        const mutationGuard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (mutationGuard.expectedGraphHash !== params.expected_graph_hash) {
            const error = new Error("The graph changed before the exact value read.");
            error.code = "graph_hash_precondition_failed";
            throw error;
        }
        const node = this.flApi.getWorkflowNode(params.node_id, workflowPin);
        if (!node) {
            throw new Error("The exact node is unavailable.");
        }
        return {
            success: true,
            node_id: node.node_id,
            values: node.values,
            workflow_identity: params.expected_workflow_identity,
            graph_hash: mutationGuard.expectedGraphHash,
        };
    }

    async _handleGetNodeImageRef(params) {
        const workflowPin = this.flApi.pinActiveWorkflow(
            params.expected_workflow_identity,
        );
        const guard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (guard.expectedGraphHash !== params.expected_graph_hash) {
            const error = new Error("The graph changed before the exact image read.");
            error.code = "graph_hash_precondition_failed";
            throw error;
        }
        const result = this.flApi.getNodeImageRef(params.node_id, workflowPin);
        await this.flApi.assertWorkflowMutationGuard(guard);
        return {
            ...result,
            workflow_identity: params.expected_workflow_identity,
            graph_hash: guard.expectedGraphHash,
        };
    }

    async _handleGetCanvasImageRefs(params) {
        const workflowPin = this.flApi.pinActiveWorkflow(
            params.expected_workflow_identity,
        );
        const guard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (guard.expectedGraphHash !== params.expected_graph_hash) {
            const error = new Error("The graph changed before canvas image discovery.");
            error.code = "graph_hash_precondition_failed";
            throw error;
        }
        const page = this.flApi.getCanvasImageRefs({
            nodeIds: params.node_ids ?? null,
            offset: params.offset ?? 0,
            limit: params.limit ?? 8,
        }, workflowPin);
        await this.flApi.assertWorkflowMutationGuard(guard);
        return {
            success: true,
            ...page,
            workflow_identity: params.expected_workflow_identity,
            graph_hash: guard.expectedGraphHash,
        };
    }

    async _handlePlaceChatImageInNode(params) {
        return this.flApi.placeChatImageInNode(params.image, params.node_id);
    }

    async _narrowMutationBinding(tool, params, result) {
        if (tool === "queue_workflow") {
            return {
                operation_id: params.operation_id,
                operation_request_hash: params.operation_request_hash,
                prompt_id: result.prompt_id,
            };
        }
        if (tool === "update_connected_prompt") {
            return {
                workflow_identity: result.workflow_identity,
                graph_hash: result.graph_hash,
                node_id: result.node_id,
                widget_name: result.widget_name,
                value_hash: await canonicalNarrowOperationHash(
                    "prompt_value_binding",
                    { value: params.value },
                ),
            };
        }
        if (tool === "edit_node_mask") {
            return {
                workflow_identity: params.expected_workflow_identity,
                graph_hash: params.expected_graph_hash,
                node_id: result.node_id,
                review_token: result.review_token,
                image: structuredClone(result.image),
                source_image: structuredClone(result.source_image),
            };
        }
        return {
            workflow_identity: result.workflow_identity,
            graph_hash: result.graph_hash,
            node_id: result.node_id,
            review_token: result.review_token,
            image: structuredClone(result.image),
        };
    }

    async _attestNarrowMutation(tool, receipt, binding) {
        if (tool === "queue_workflow") {
            return await this.flApi.attestQueuedOperation(
                binding?.operation_id,
                binding?.operation_request_hash,
                binding?.prompt_id,
            );
        }
        if (!binding?.workflow_identity || !binding?.graph_hash) return false;
        const workflowPin = this.flApi.pinActiveWorkflow(binding.workflow_identity);
        const guard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (guard.expectedGraphHash !== binding.graph_hash) return false;
        if (tool === "update_connected_prompt") {
            const node = this.flApi.getWorkflowNode(binding.node_id, workflowPin);
            const value = node?.values?.[binding.widget_name];
            if (typeof value !== "string") return false;
            return await canonicalNarrowOperationHash(
                "prompt_value_binding",
                { value },
            ) === binding.value_hash;
        }
        if (tool === "edit_node_mask") {
            const pending = this.flApi.getPendingMaskReviewReceipt(binding.node_id);
            return Boolean(
                pending
                && pending.review_token === binding.review_token
                && JSON.stringify(pending.image) === JSON.stringify(binding.image)
                && JSON.stringify(pending.source_image) === JSON.stringify(binding.source_image)
                && receipt.review_required === true
            );
        }
        const committed = this.flApi.getNodeImageRef(
            binding.node_id,
            workflowPin,
            { includePending: false },
        ).image;
        return Boolean(
            receipt.approved === true
            && receipt.review_token === binding.review_token
            && JSON.stringify(committed) === JSON.stringify(binding.image)
        );
    }

    async _runNarrowMutation(tool, params, execute) {
        const operationId = params.operation_id;
        const requestHash = params.operation_request_hash;
        const requestPayload = params.operation_payload;
        return await narrowEditOperationLedger.run({
            tool,
            operationId,
            requestHash,
            requestPayload,
            executionPayload: Object.fromEntries(
                Object.entries(params).filter(([key]) => key !== "operation_payload"),
            ),
            execute,
            receipt: result => result,
            binding: result => this._narrowMutationBinding(tool, params, result),
            attest: (receipt, binding) => this._attestNarrowMutation(
                tool,
                receipt,
                binding,
            ),
        });
    }

    async _handleRecoverNarrowOperation(params) {
        const observed = await canonicalNarrowOperationHash(
            params.tool,
            params.operation_payload,
        );
        if (observed !== params.operation_request_hash) {
            const error = new Error(
                "The canonical mutation arguments do not match operation_request_hash.",
            );
            error.code = "narrow_edit_request_hash_mismatch";
            throw error;
        }
        if (params.tool === "queue_workflow") {
            try {
                const recovered = await this.flApi.recoverQueuedOperation(
                    params.operation_id,
                    params.operation_request_hash,
                );
                return {
                    ...recovered,
                    operation_id: params.operation_id,
                    operation_request_hash: params.operation_request_hash,
                    already_applied: true,
                    queued: true,
                };
            } catch (error) {
                if (error?.code !== "narrow_edit_operation_not_found") throw error;
            }
            const durable = readDurableQueueOperation({
                operationId: params.operation_id,
                operationRequestHash: params.operation_request_hash,
            });
            if (durable?.state === "completed") {
                return {
                    ...durable.receipt,
                    operation_id: params.operation_id,
                    operation_request_hash: params.operation_request_hash,
                    already_applied: true,
                    queued: true,
                };
            }
            if (durable?.state === "pending") {
                const error = new Error(
                    "A prior queue attempt is unresolved; it was not queued again.",
                );
                error.code = "queue_outcome_unknown";
                throw error;
            }
        }
        return await narrowEditOperationLedger.recover({
            tool: params.tool,
            operationId: params.operation_id,
            requestHash: params.operation_request_hash,
            attest: (receipt, binding) => this._attestNarrowMutation(
                params.tool,
                receipt,
                binding,
            ),
        });
    }

    async _handleEditNodeMask(params) {
        if (!params?.operation_id) return await this._handleEditNodeMaskOnce(params);
        return await this._runNarrowMutation("edit_node_mask", params, () => (
            this._handleEditNodeMaskOnce(params)
        ));
    }

    async _handleEditNodeMaskOnce(params) {
        const workflowPin = this.flApi.pinActiveWorkflow(
            params.expected_workflow_identity,
        );
        const mutationGuard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (mutationGuard.expectedGraphHash !== params.expected_graph_hash) {
            const error = new Error("The graph changed after mask inspection.");
            error.code = "graph_hash_precondition_failed";
            throw error;
        }
        const current = this.flApi.getNodeImageRef(params.node_id, workflowPin).image;
        const expected = params.expected_source_image;
        if (!(
            current?.filename === expected?.filename
            && (current?.subfolder || "") === (expected?.subfolder || "")
            && (current?.type || "input") === (expected?.type || "input")
        )) {
            const error = new Error("The exact mask source changed after inspection.");
            error.code = "mask_source_precondition_failed";
            throw error;
        }
        return await this.flApi.editNodeMask(
            params.node_id,
            params.regions,
            params.coordinate_space,
            params.clear_existing,
            {
                workflowPin,
                mutationGuard,
                expectedSourceImage: structuredClone(expected),
                expectedSourceAttestation: structuredClone(
                    params.expected_source_attestation,
                ),
            },
        );
    }

    async _handleConfirmMaskReview(params) {
        if (!params?.operation_id) return await this._handleConfirmMaskReviewOnce(params);
        return await this._runNarrowMutation("confirm_mask_review", params, () => (
            this._handleConfirmMaskReviewOnce(params)
        ));
    }

    async _handleConfirmMaskReviewOnce(params) {
        try {
            return await this.flApi.confirmMaskReview(
                params.node_id,
                params.review_token
            );
        } catch (error) {
            if (error?.code === "workflow_state_compromised") {
                const identity = error?.details?.workflow_identity
                    || this.flApi.getActiveWorkflowIdentity();
                this.workflowMutationQuarantine = Object.freeze({
                    workflow_identity: identity,
                    application_id: "confirm-mask-review",
                    patch_hash: null,
                });
                workflowMutationQuarantine = this.workflowMutationQuarantine;
            }
            throw error;
        }
    }

    async _handleSetNodeValues(params) {
        const { node_id, values } = params;
        const updatedValues = this.flApi.setValues(node_id, values);
        return { node_id, values: updatedValues };
    }

    async _handleSetNodeValuesExact(params) {
        if (!params?.operation_id) return await this._handleSetNodeValuesExactOnce(params);
        return await this._runNarrowMutation("update_connected_prompt", params, () => (
            this._handleSetNodeValuesExactOnce(params)
        ));
    }

    async _handleSetNodeValuesExactOnce(params) {
        const autoQueueState = this.flApi.pauseAutoQueue();
        let result = null;
        let operationError = null;
        try {
            try {
                result = await this._setNodeValuesExactTransaction(params);
            } catch (error) {
                if (
                    params?.quarantine_on_failure === true
                    && error?.code !== "workflow_state_compromised"
                ) {
                    throw this._quarantineExactValueMutation(
                        params.expected_workflow_identity,
                    );
                }
                throw error;
            }
        } catch (error) {
            operationError = error;
        }

        let cleanupWarning = null;
        try {
            this.flApi.restoreAutoQueue(autoQueueState);
        } catch (error) {
            // A completed exact mutation must not become an unknown failure just
            // because restoring the user's prior queue preference failed. Keep
            // auto-queue fail-safe disabled and report the cleanup problem.
            try {
                this.flApi.pauseAutoQueue();
            } catch (_) {
                // Best effort only; the original restore error is authoritative.
            }
            cleanupWarning = {
                phase: "restore_auto_queue",
                message: String(error?.message || error),
            };
        }

        if (operationError) {
            if (cleanupWarning && operationError && typeof operationError === "object") {
                operationError.cleanup_warnings = [cleanupWarning];
            }
            throw operationError;
        }
        return {
            ...result,
            queued: false,
            ...(cleanupWarning ? { cleanup_warnings: [cleanupWarning] } : {}),
        };
    }

    async _setNodeValuesExactTransaction(params) {
        const {
            expected_workflow_identity: expectedWorkflowIdentity,
            expected_graph_hash: expectedGraphHash,
            node_id: nodeId,
            widget_name: widgetName,
            value,
            expected_current_value: expectedCurrentValue,
            expected_result_graph_hash: expectedResultGraphHash,
            expected_reference_node_id: expectedReferenceNodeId,
            expected_reference_image: expectedReferenceImage,
            expected_reference_attestation: expectedReferenceAttestation,
        } = params || {};
        if (typeof widgetName !== "string" || !widgetName || widgetName.length > 256) {
            throw new Error("set_node_values_exact requires one bounded widget_name.");
        }
        const workflowPin = this.flApi.pinActiveWorkflow(expectedWorkflowIdentity);
        const mutationGuard = await this.flApi.createWorkflowMutationGuard(workflowPin);
        if (mutationGuard.expectedGraphHash !== expectedGraphHash) {
            const error = new Error(
                "The active graph changed before the exact value transaction.",
            );
            error.code = "graph_hash_precondition_failed";
            error.details = {
                expected_graph_hash: expectedGraphHash,
                actual_graph_hash: mutationGuard.expectedGraphHash,
            };
            throw error;
        }
        const before = this.flApi.getWorkflowNode(nodeId, workflowPin);
        if (!before) {
            throw new Error("The exact prompt producer node is unavailable.");
        }
        if (!Object.prototype.hasOwnProperty.call(before.values || {}, widgetName)) {
            throw new Error(
                `The exact widget ${widgetName} is unavailable on node ${String(nodeId)}.`,
            );
        }
        if (
            Object.prototype.hasOwnProperty.call(params, "expected_current_value")
            && JSON.stringify(before.values?.[widgetName]) !== JSON.stringify(expectedCurrentValue)
        ) {
            const error = new Error(
                `Widget ${widgetName} changed before the exact value transaction.`,
            );
            error.code = "widget_value_precondition_failed";
            throw error;
        }
        const hasReferencePrecondition = [
            "expected_reference_node_id",
            "expected_reference_image",
            "expected_reference_attestation",
        ].some(name => Object.prototype.hasOwnProperty.call(params, name));
        if (hasReferencePrecondition) {
            if (
                expectedReferenceNodeId === undefined
                || !expectedReferenceImage
                || !expectedReferenceAttestation
            ) {
                const error = new Error(
                    "An exact reference node, image, and byte attestation are required together.",
                );
                error.code = "reference_image_precondition_failed";
                throw error;
            }
            const referenceBeforeResult = this.flApi.getNodeImageRef(
                expectedReferenceNodeId,
                workflowPin,
            );
            if (
                !referenceBeforeResult
                || typeof referenceBeforeResult !== "object"
                || referenceBeforeResult.node_id !== expectedReferenceNodeId
            ) {
                const error = new Error(
                    "The exact connected reference node is unavailable.",
                );
                error.code = "reference_image_precondition_failed";
                throw error;
            }
            const referenceBefore = referenceBeforeResult.image;
            const sameReference = (
                referenceBefore?.filename === expectedReferenceImage.filename
                && (referenceBefore?.subfolder || "")
                    === (expectedReferenceImage.subfolder || "")
                && (referenceBefore?.type || "input")
                    === (expectedReferenceImage.type || "input")
            );
            if (!sameReference) {
                const error = new Error(
                    "The connected reference image changed before the prompt transaction.",
                );
                error.code = "reference_image_precondition_failed";
                throw error;
            }
            // The public handler already owns the shared canvas mutation lock.
            // Verify exactly one no-store Blob here, immediately before the first
            // graph effect, then synchronously close the remaining ref/graph race.
            await this.flApi.verifyComfyImageExact(
                expectedReferenceImage,
                expectedReferenceAttestation,
            );
            await this.flApi.assertWorkflowMutationGuard(mutationGuard);
            const referenceAfterResult = this.flApi.getNodeImageRef(
                expectedReferenceNodeId,
                workflowPin,
            );
            const referenceAfter = referenceAfterResult?.image;
            if (!(
                referenceAfterResult?.node_id === expectedReferenceNodeId
                && referenceAfter?.filename === expectedReferenceImage.filename
                && (referenceAfter?.subfolder || "")
                    === (expectedReferenceImage.subfolder || "")
                && (referenceAfter?.type || "input")
                    === (expectedReferenceImage.type || "input")
            )) {
                const error = new Error(
                    "The connected reference image changed during verification.",
                );
                error.code = "reference_image_precondition_failed";
                throw error;
            }
        }
        const previousValue = structuredClone(before.values?.[widgetName]);
        let mutationAttempted = false;
        try {
            mutationAttempted = true;
            const changed = await this.flApi.setValuesExact(
                nodeId,
                { [widgetName]: value },
                workflowPin,
            );
            if (
                !Array.isArray(changed?.applied)
                || changed.applied.length !== 1
                || changed.applied[0] !== widgetName
            ) {
                throw new Error(
                    `The exact widget ${widgetName} was not applied on node ${String(nodeId)}.`,
                );
            }
            const after = this.flApi.getWorkflowNode(nodeId, workflowPin);
            if (JSON.stringify(after?.values?.[widgetName]) !== JSON.stringify(value)) {
                throw new Error(`Widget ${widgetName} did not retain its exact requested value.`);
            }
            const resultGraphHash = await this.flApi.acceptWorkflowMutationGuard(mutationGuard);
            if (
                typeof expectedResultGraphHash === "string"
                && resultGraphHash !== expectedResultGraphHash
            ) {
                // The requested rollback value may be present, but the full graph did
                // not return to its attested pre-edit state. Preserve that value and
                // quarantine all later writes rather than rolling forward again.
                mutationAttempted = false;
                throw this._quarantineExactValueMutation(expectedWorkflowIdentity);
            }
            return {
                success: true,
                node_id: nodeId,
                widget_name: widgetName,
                applied: [widgetName],
                verified: true,
                workflow_identity: expectedWorkflowIdentity,
                previous_graph_hash: expectedGraphHash,
                graph_hash: resultGraphHash,
            };
        } catch (error) {
            let rollbackComplete = !mutationAttempted;
            if (mutationAttempted) {
                try {
                    const restored = await this.flApi.setValuesExact(
                        nodeId,
                        { [widgetName]: previousValue },
                        workflowPin,
                    );
                    const restoredFacts = this.flApi.getWorkflowNode(nodeId, workflowPin);
                    const restoredGuard = await this.flApi.createWorkflowMutationGuard(
                        workflowPin,
                    );
                    rollbackComplete = (
                        Array.isArray(restored?.applied)
                        && restored.applied.length === 1
                        && restored.applied[0] === widgetName
                        && JSON.stringify(restoredFacts?.values?.[widgetName])
                            === JSON.stringify(previousValue)
                        && restoredGuard.expectedGraphHash === expectedGraphHash
                    );
                } catch (_) {
                    rollbackComplete = false;
                }
            }
            if (!rollbackComplete) {
                throw this._quarantineExactValueMutation(expectedWorkflowIdentity);
            }
            throw error;
        }
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
        return await this._runNarrowMutation("queue_workflow", params, () => (
            (async () => {
                const operation = {
                    operationId: params.operation_id,
                    operationRequestHash: params.operation_request_hash,
                };
                const durable = readDurableQueueOperation(operation);
                if (durable?.state === "completed") return durable.receipt;
                if (durable?.state === "pending") {
                    try {
                        const recovered = await this.flApi.recoverQueuedOperation(
                            operation.operationId,
                            operation.operationRequestHash,
                        );
                        return completeDurableQueueOperation(operation, recovered);
                    } catch (error) {
                        if (error?.code !== "narrow_edit_operation_not_found") throw error;
                        const unknown = new Error(
                            "A prior queue attempt is unresolved; it was not queued again.",
                        );
                        unknown.code = "queue_outcome_unknown";
                        throw unknown;
                    }
                }
                const cached = reserveDurableQueueOperation(operation);
                if (cached) return cached;
                try {
                    const result = await this.flApi.queueWorkflow(
                        batch_count || null,
                        operation,
                    );
                    if (result?.queued === true && typeof result.prompt_id === "string") {
                        return completeDurableQueueOperation(operation, result);
                    }
                    // Validation failed before ComfyUI assigned a prompt ID.
                    discardDurableQueueOperation(operation);
                    return result;
                } catch (error) {
                    if (error?.code === "queue_submission_not_started") {
                        discardDurableQueueOperation(operation);
                    }
                    throw error;
                }
            })()
        ));
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

    async _handleWorkflowGetCurrentJSON(params) {
        return await this.flApi.getCurrentWorkflowJSON(params?.api_format || false);
    }

    async _handleWorkflowLoadJSON(params) {
        return await this.flApi.loadWorkflowJSON(
            params.workflow,
            params.name || null,
            params.clean !== false,
            params.restore_view !== false
        );
    }

    async _handleWorkflowGetTabs(params) {
        return await this.flApi.getWorkflowTabs();
    }

    async _handleWorkflowListFiles(params) {
        return await this.flApi.listWorkflowFiles();
    }

    async _handleWorkflowReadFile(params) {
        return await this.flApi.readWorkflowFile(params.path);
    }

    async _handleWorkflowSaveCurrent(params) {
        return await this.flApi.saveCurrentWorkflow(params.path, params.overwrite !== false);
    }

    async _handleWorkflowRenameFile(params) {
        return await this.flApi.renameWorkflowFile(params.path, params.dest, params.overwrite !== false);
    }

    async _handleWorkflowDeleteFile(params) {
        return await this.flApi.deleteWorkflowFile(params.path);
    }

    async _handleWorkflowCloseCurrent(params) {
        return await this.flApi.closeCurrentWorkflow();
    }

    async _handleWorkflowDuplicateCurrent(params) {
        return await this.flApi.duplicateCurrentWorkflow();
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
