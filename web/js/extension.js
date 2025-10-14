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
            const wsClient = new WSClient(sessionId, {
                url: 'ws://localhost:8000/ws',  // TODO: Make configurable
                heartbeatInterval: 30000,
                maxReconnectAttempts: 5,
            });
            
            // Initialize tool executor
            const toolExecutor = new ToolExecutor(wsClient);
            console.log("[FL_JS] Tool executor initialized");
            
            // Set up event handlers
            wsClient.onConnect = () => {
                console.log("[FL_JS] Connected to backend server");
            };
            
            wsClient.onDisconnect = (event) => {
                console.log("[FL_JS] Disconnected from backend server:", event.code);
            };
            
            wsClient.onHandshakeAck = (message) => {
                console.log("[FL_JS] Handshake complete:", message.status);
                if (message.status === 'reconnected') {
                    console.log("[FL_JS] Reconnected to existing session");
                }
            };
            
            wsClient.onAgentResponse = (message) => {
                console.log("[FL_JS] Agent response received:", message.content);
                // TODO: Display in chat UI (Phase 4)
            };
            
            wsClient.onToolRequest = async (message) => {
                console.log("[FL_JS] Tool request received:", message.tool_name);
                // Execute tool via tool executor
                await toolExecutor.executeToolRequest(message);
            };
            
            wsClient.onTypingIndicator = (message) => {
                console.log("[FL_JS] Agent is typing...");
                // TODO: Show typing indicator in UI (Phase 4)
            };
            
            wsClient.onError = (error) => {
                console.error("[FL_JS] Error:", error);
                // TODO: Display error in UI (Phase 4)
            };
            
            wsClient.onMaxReconnectReached = () => {
                console.error("[FL_JS] Max reconnection attempts reached. Please check backend server.");
                // TODO: Display error in UI (Phase 4)
            };
            
            // Store instances globally for other modules
            window.FL_JS = {
                sessionManager,
                wsClient,
                toolExecutor,
                app,
                version: '0.1.5',
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

console.log("[FL_JS] Extension module loaded");
