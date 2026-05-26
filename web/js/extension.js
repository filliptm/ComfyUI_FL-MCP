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
import ChatClient from "./chat_client.js";
import { ToolExecutor } from "./tool_executor.js";
import { ChatUI } from "./chat_ui.js";
import { DiagramGenerator } from "./diagram_generator.js";
import { getToolConfig } from "./tool_activity.js";

let chatUI = null;
let wsClient = null;
let chatClient = null;
let toolExecutor = null;
let diagramGenerator = null;

/**
 * Fetch client configuration from backend
 * @returns {Promise<Object>} Configuration object with ws_url
 */
async function fetchClientConfig() {
    try {
        const response = await fetch('/api/config');
        if (!response.ok) {
            throw new Error(`Config fetch failed: ${response.status}`);
        }
        const config = await response.json();
        console.log('[FL_JS] Fetched client config:', config);
        return config;
    } catch (error) {
        console.warn('[FL_JS] Failed to fetch config, using defaults:', error);
        // Fallback to default port 8000
        return {
            ws_url: 'ws://127.0.0.1:8000/ws',
            version: 'unknown',
            ngrok_mode: false,
        };
    }
}

async function fetchLauncherStatus() {
    try {
        const response = await fetch('/fl_ren/launcher/status');
        if (!response.ok) {
            throw new Error(`Launcher status failed: ${response.status}`);
        }
        return await response.json();
    } catch (error) {
        console.warn('[FL_JS] Ren launcher status unavailable:', error);
        return { backendReachable: false, wsUrl: 'ws://127.0.0.1:8000/ws' };
    }
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
            
            const launcherStatus = await fetchLauncherStatus();

            // Fetch configuration from backend when available
            const config = await fetchClientConfig();
            const wsUrl = launcherStatus.wsUrl || config.ws_url;
            
            // Initialize WebSocket client with dynamic URL
            wsClient = new WSClient(sessionId, {
                url: wsUrl,
                heartbeatInterval: 30000,
                maxReconnectAttempts: 5,
            });

            chatClient = new ChatClient(sessionId, {
                wsUrl,
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
            
            // New handler for Python-only tools (no executor needed)
            wsClient.on('tool_report', (message) => {
                console.log("[FL_JS] 📊 TOOL REPORT EVENT FIRED:", message.tool_name);

                const toolConfig = getToolConfig(message.tool_name);

                // Add to breadcrumb chain
                try {
                    window.FL_JS?.chatUI?.startToolInChain(
                        message.tool_name,
                        toolConfig.icon,
                        toolConfig.label
                    );
                    // Mark as complete immediately since Python tools execute instantly
                    window.FL_JS?.chatUI?.completeToolInChain(message.tool_name);
                } catch (error) {
                    console.warn('[FL_JS] Could not add tool to breadcrumb chain:', error);
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
                chatClient,
                toolExecutor,
                diagramGenerator,
                chatUI: null, // Will be set when sidebar is rendered
                app,
                version: '0.3.0',
            };
            
            // Connect only if the hidden Ren backend is already running. The
            // sidebar can launch it later through Comfy's launcher route.
            if (launcherStatus.backendReachable) {
                console.log("[FL_JS] Connecting to backend server...");
                wsClient.connect();
            } else {
                console.log("[FL_JS] Ren backend is stopped; waiting for user launch.");
            }
            
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
                title: "Ren",
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
                        chatUI = new ChatUI(el, chatClient, wsClient);
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
