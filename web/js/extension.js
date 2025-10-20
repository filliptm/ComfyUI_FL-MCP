/**
 * FL_JS Agentic System - ComfyUI Extension
 * 
 * Provides AI-powered workflow assistance via natural language chat interface.
 * Requires FL_JS backend server to be running.
 * 
 * Backend server must be started separately:
 *     cd backend
 *     python server.py
 */

import { app } from "../../scripts/app.js";
import SessionManager from "./session_manager.js";
import WSClient from "./ws_client.js";
import { ToolExecutor } from "./tool_executor.js";
import { ChatUI } from "./chat_ui.js";
import { DiagramGenerator } from "./diagram_generator.js";

let chatUI = null;
let wsClient = null;
let toolExecutor = null;
let diagramGenerator = null;

/**
 * Get tool configuration for display (breadcrumb and chat bubbles)
 * @param {string} toolName - Name of the tool
 * @returns {object} - Icon, label, and description for the tool
 */
function getToolConfig(toolName) {
    const toolConfigs = {
        // Query & Understanding
        "workflow_overview": {
            icon: "🔍",
            label: "Overview",
            description: "Observing the essence of the workflow"
        },
        "query_workflow": {
            icon: "🔎",
            label: "Query",
            description: "Tracing patterns in the graph"
        },
        "workflow_diagram": {
            icon: "📐",
            label: "Diagram",
            description: "Sketching the architecture of thought"
        },

        // Node Creation & Modification
        "create_node": {
            icon: "✨",
            label: "Create",
            description: "Manifesting new possibilities"
        },
        "remove_nodes": {
            icon: "🗑️",
            label: "Remove",
            description: "Clearing the path"
        },
        "connect_nodes": {
            icon: "🔗",
            label: "Connect",
            description: "Weaving connections"
        },

        // Selection & Focus
        "select_nodes": {
            icon: "👁️",
            label: "Select",
            description: "Focusing attention on the essential"
        },
        "find_node": {
            icon: "🎯",
            label: "Find",
            description: "Seeking the heart of the matter"
        },

        // Layout & Organization
        "modify_layout": {
            icon: "🏗️",
            label: "Layout",
            description: "Arranging the flow"
        },
        "position_node_left": {
            icon: "⬅️",
            label: "Position",
            description: "Guiding elements into place"
        },
        "position_node_right": {
            icon: "➡️",
            label: "Position",
            description: "Guiding elements into place"
        },
        "position_node_top": {
            icon: "⬆️",
            label: "Position",
            description: "Guiding elements into place"
        },
        "position_node_bottom": {
            icon: "⬇️",
            label: "Position",
            description: "Guiding elements into place"
        },

        // Workflow Execution
        "queue_workflow": {
            icon: "🚀",
            label: "Queue",
            description: "Setting creation in motion"
        },
        "cancel_workflow": {
            icon: "⏹️",
            label: "Cancel",
            description: "Gently pausing the process"
        },
        "get_queue_status": {
            icon: "📊",
            label: "Status",
            description: "Checking the pulse of creation"
        },

        // Value Manipulation
        "set_node_values": {
            icon: "⚙️",
            label: "Set",
            description: "Tuning the harmonics"
        },
        "get_node_values": {
            icon: "📊",
            label: "Get",
            description: "Reading the current state"
        },

        // Utilities & Generation
        "generate_seed": {
            icon: "🌱",
            label: "Seed",
            description: "Planting seeds of randomness"
        },
        "generate_float": {
            icon: "🎲",
            label: "Random",
            description: "Weaving chance into the pattern"
        },
        "random_choice": {
            icon: "🎯",
            label: "Choice",
            description: "Choosing from the field of possibilities"
        },

        // File & Directory Operations
        "list_directory": {
            icon: "📁",
            label: "List",
            description: "Exploring the paths available"
        },
        "read_file": {
            icon: "📜",
            label: "Read",
            description: "Reading the written wisdom"
        },
        "write_file": {
            icon: "✍️",
            label: "Write",
            description: "Inscribing new knowledge"
        },

        // ComfyUI Integration
        "get_extensions": {
            icon: "🧩",
            label: "Extensions",
            description: "Gathering the available tools"
        },
        "get_node_types": {
            icon: "📋",
            label: "Types",
            description: "Cataloging the building blocks"
        },

        // Generic fallback
        "*": {
            icon: "⚡",
            label: "Tool",
            description: "Working with the flow"
        }
    };
    return toolConfigs[toolName] || toolConfigs["*"];
}

app.registerExtension({
    name: "fl_js.agentic_system",
    
    async setup() {
        console.log("[FL_JS] Initializing Agentic System extension...");
        
        try {
            // Initialize session manager
            const sessionManager = new SessionManager();
            const sessionId = sessionManager.getSessionId();
            
            console.log(`[FL_JS] Session ID: ${sessionId}`);
            
            // Initialize WebSocket client
            wsClient = new WSClient(sessionId, {
                url: 'ws://localhost:8000/ws',  // TODO: Make configurable
                heartbeatInterval: 30000,
                maxReconnectAttempts: 5,
            });
            
            // Initialize diagram generator
            diagramGenerator = new DiagramGenerator();
            console.log("[FL_JS] Diagram generator initialized");
            
            // Initialize tool executor
            toolExecutor = new ToolExecutor(wsClient);
            console.log("[FL_JS] Tool executor initialized");
            
            // Set up WebSocket event handlers using new event emitter pattern
            wsClient.on('connected', () => {
                console.log("[FL_JS] Connected to backend server");
            });
            
            wsClient.on('disconnected', (event) => {
                console.log("[FL_JS] Disconnected from backend server:", event.code);

                // Clear active tool chain on disconnect
                try {
                    window.FL_JS?.chatUI?.clearToolChain();
                } catch (error) {
                    console.warn('[FL_JS] Could not clear tool chain on disconnect:', error);
                }
            });
            
            wsClient.on('handshake_ack', (message) => {
                console.log("[FL_JS] Handshake complete:", message.status);
                if (message.status === 'reconnected') {
                    console.log("[FL_JS] Reconnected to existing session");
                }
                
                // Setup ComfyUI listeners after handshake
                if (window.app && window.app.api) {
                    wsClient.setupComfyListeners(window.app.api);
                } else {
                    console.warn('[FL_JS] ComfyUI API not available yet, will retry');
                    setTimeout(() => {
                        if (window.app && window.app.api) {
                            wsClient.setupComfyListeners(window.app.api);
                        } else {
                            console.error('[FL_JS] ComfyUI API still not available');
                        }
                    }, 1000);
                }
            });
            
            wsClient.on('agent_response', (message) => {
                console.log("[FL_JS] Agent response received:", message.content);

                // Tool executions stay in chat history - no need to hide them
            });
            
            wsClient.on('tool_request', async (message) => {
                console.log("[FL_JS] ⚡ TOOL REQUEST EVENT FIRED:", message.tool_name, 'request_id:', message.request_id);

                const toolConfig = getToolConfig(message.tool_name);

                // Add tool to breadcrumb chain in chat
                try {
                    window.FL_JS?.chatUI?.startToolInChain(
                        message.tool_name,
                        toolConfig.icon,
                        toolConfig.label
                    );
                } catch (error) {
                    console.warn('[FL_JS] Could not start tool in breadcrumb chain:', error);
                }

                console.log("[FL_JS] ⚡ Calling toolExecutor.executeToolRequest...");
                try {
                    await toolExecutor.executeToolRequest(message);
                    console.log("[FL_JS] ⚡ toolExecutor.executeToolRequest completed");

                    // Mark tool as complete in breadcrumb chain
                    try {
                        window.FL_JS?.chatUI?.completeToolInChain(message.tool_name);
                    } catch (error) {
                        console.warn('[FL_JS] Could not complete tool in chain:', error);
                    }
                } catch (error) {
                    console.error("[FL_JS] ❌ Error in tool execution:", error);
                }
            });
            
            wsClient.on('typing_indicator', (message) => {
                console.log("[FL_JS] Agent is typing...");
            });
            
            wsClient.on('error', (error) => {
                console.error("[FL_JS] Error:", error);

                // Clear active tool chain on error
                try {
                    window.FL_JS?.chatUI?.clearToolChain();
                } catch (error) {
                    console.warn('[FL_JS] Could not clear tool chain on error:', error);
                }
            });

            wsClient.on('max_reconnect_reached', () => {
                console.error("[FL_JS] Max reconnection attempts reached. Please check backend server.");

                // Clear active tool chain on max reconnect
                try {
                    window.FL_JS?.chatUI?.clearToolChain();
                } catch (error) {
                    console.warn('[FL_JS] Could not clear tool chain on max reconnect:', error);
                }
            });
            
            // Store instances globally for other modules
            window.FL_JS = {
                sessionManager,
                wsClient,
                toolExecutor,
                diagramGenerator,
                chatUI: null, // Will be set when sidebar is rendered
                app,
                version: '0.3.0',
            };
            
            // Connect to backend
            console.log("[FL_JS] Connecting to backend server...");
            wsClient.connect();
            
            console.log("[FL_JS] Extension initialized successfully!");
            console.log("[FL_JS] Note: Backend server must be running (cd backend && python server.py)");
            
        } catch (error) {
            console.error("[FL_JS] Failed to initialize extension:", error);
        }
    },
    
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        // Hook for modifying node definitions before registration
        // Currently unused, but available for future enhancements
    },
    
    async nodeCreated(node) {
        // Hook for when a node instance is created
        // Currently unused, but available for future enhancements
    },
});

// Register sidebar tab
app.registerExtension({
    name: "fl_js.sidebar",
    
    async setup() {
        // Wait for app to be ready
        await new Promise(resolve => {
            if (app.extensionManager) {
                resolve();
            } else {
                const interval = setInterval(() => {
                    if (app.extensionManager) {
                        clearInterval(interval);
                        resolve();
                    }
                }, 100);
            }
        });

        // Custom Stylesheet
        const style = document.createElement("link");
        style.rel = "stylesheet";
        style.href = new URL("./style.css", import.meta.url);
        document.head.appendChild(style);
        
        console.log("[FL_JS] Registering sidebar tab...");
        
        try {
            app.extensionManager.registerSidebarTab({
                id: "fl_js_assistant",
                icon: "pi pi-comments",
                title: "FL_JS Ren",
                tooltip: "Ren: connect your flow",
                type: "custom",
                render: (el) => {
                    console.log("[FL_JS] Rendering sidebar tab...");

                    // Check if we need to reinitialize (container is empty or chatUI was destroyed)
                    if (!chatUI || !el.children.length) {
                        // Clean up existing instance if it exists but container is empty
                        if (chatUI && !el.children.length) {
                            console.log("[FL_JS] Container empty, reinitializing chat UI...");
                            chatUI = null;
                        }

                        // Create new chat UI instance
                        chatUI = new ChatUI(el, wsClient);
                        window.FL_JS.chatUI = chatUI;
                        console.log("[FL_JS] Chat UI initialized with tool activity and breadcrumb chain");
                    }

                    return el;
                },
                destroy: () => {
                    console.log("[FL_JS] Destroying sidebar tab...");
                    if (chatUI) {
                        chatUI.destroy();
                        chatUI = null;
                        window.FL_JS.chatUI = null;
                    }
                }
            });
            
            console.log("[FL_JS] Sidebar tab registered successfully!");
        } catch (error) {
            console.error("[FL_JS] Failed to register sidebar tab:", error);
            console.error("[FL_JS] Note: Make sure you're using a ComfyUI version that supports sidebar tabs");
        }
    }
});

console.log("[FL_JS] Extension module loaded");
