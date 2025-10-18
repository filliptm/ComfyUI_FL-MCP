/**
 * WebSocket Client for FL_JS Agentic System
 * 
 * Features:
 * - Session-based connection with handshake protocol
 * - Automatic reconnection with exponential backoff
 * - Message queueing during disconnection
 * - Event-driven architecture for message handling
 */

/**
 * Simple EventEmitter implementation
 */
class EventEmitter {
    constructor() {
        this.events = {};
    }
    
    on(event, listener) {
        if (!this.events[event]) {
            this.events[event] = [];
        }
        this.events[event].push(listener);
    }
    
    emit(event, ...args) {
        if (this.events[event]) {
            this.events[event].forEach(listener => listener(...args));
        }
    }
    
    off(event, listenerToRemove) {
        if (!this.events[event]) return;
        this.events[event] = this.events[event].filter(listener => listener !== listenerToRemove);
    }
}

class WSClient extends EventEmitter {
    constructor(sessionId, config = {}) {
        super();
        this.sessionId = sessionId;
        this.ws = null;
        
        // Configuration
        this.config = {
            url: config.url || 'ws://localhost:8000/ws',
            maxReconnectAttempts: config.maxReconnectAttempts || 5,
            initialReconnectDelay: config.initialReconnectDelay || 1000, // 1 second
            maxReconnectDelay: config.maxReconnectDelay || 30000, // 30 seconds
            clientVersion: config.clientVersion || '1.0.0',
        };
        
        // State
        this.connected = false;
        this.handshakeComplete = false;
        this.reconnectAttempts = 0;
        this.reconnectDelay = this.config.initialReconnectDelay;
        this.reconnectTimeout = null;
        this.messageQueue = [];
        
        // ComfyUI API reference
        this.comfyApi = null;
        
        console.log('[WSClient] Initialized with session:', this.sessionId);
    }

    /**
     * Connect to WebSocket server
     */
    connect() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            console.log('[WSClient] Already connected');
            return;
        }
        
        console.log(`[WSClient] Connecting to ${this.config.url}...`);
        
        try {
            this.ws = new WebSocket(this.config.url);
            
            this.ws.onopen = () => this.handleOpen();
            this.ws.onclose = (event) => this.handleClose(event);
            this.ws.onerror = (error) => this.handleError(error);
            this.ws.onmessage = (event) => this.handleMessage(event);
            
        } catch (error) {
            console.error('[WSClient] Connection error:', error);
            this.scheduleReconnect();
        }
    }

    /**
     * Handle WebSocket open event
     */
    handleOpen() {
        console.log('[WSClient] WebSocket connected');
        this.connected = true;
        
        // Send handshake
        this.sendHandshake();
        
        this.emit('connected');
    }

    /**
     * Send handshake message
     */
    sendHandshake() {
        const handshake = {
            type: 'handshake',
            session_id: this.sessionId,
            client_version: this.config.clientVersion,
        };
        
        console.log('[WSClient] Sending handshake:', handshake);
        this.ws.send(JSON.stringify(handshake));
    }

    /**
     * Handle WebSocket close event
     */
    handleClose(event) {
        console.log('[WSClient] WebSocket closed:', event.code, event.reason);
        this.connected = false;
        this.handshakeComplete = false;
        
        this.emit('disconnected', event);
        
        // Attempt reconnection if not a clean close
        if (event.code !== 1000) {
            this.scheduleReconnect();
        }
    }

    /**
     * Handle WebSocket error
     */
    handleError(error) {
        console.error('[WSClient] WebSocket error:', error);
        this.emit('error', error);
    }

    /**
     * Handle incoming WebSocket message
     */
    handleMessage(event) {
        try {
            const message = JSON.parse(event.data);
            console.log('[WSClient] Received message:', message.type);
            
            // Route message based on type
            switch (message.type) {
                case 'handshake_ack':
                    this.handleHandshakeAck(message);
                    break;
                    
                case 'agent_response':
                    this.emit('agent_response', message);
                    break;
                    
                case 'tool_request':
                    this.emit('tool_request', message);
                    break;
                    
                case 'typing_indicator':
                    this.emit('typing_indicator', message);
                    break;
                    
                case 'error':
                    this.emit('error', message);
                    break;
                    
                default:
                    console.warn('[WSClient] Unknown message type:', message.type);
            }
            
        } catch (error) {
            console.error('[WSClient] Error parsing message:', error);
        }
    }

    /**
     * Handle handshake acknowledgment
     */
    handleHandshakeAck(message) {
        console.log('[WSClient] Handshake acknowledged:', message.status);
        this.handshakeComplete = true;
        this.reconnectAttempts = 0;
        this.reconnectDelay = this.config.initialReconnectDelay;
        
        // Flush queued messages
        this.flushMessageQueue();
        
        this.emit('handshake_ack', message);
    }

    /**
     * Send a message to the server
     */
    send(message) {
        // Add session_id if not present
        if (!message.session_id) {
            message.session_id = this.sessionId;
        }
        
        // Queue message if not connected or handshake not complete
        if (!this.connected || !this.handshakeComplete) {
            console.log('[WSClient] Queueing message:', message.type);
            this.messageQueue.push(message);
            return;
        }
        
        try {
            this.ws.send(JSON.stringify(message));
            console.log('[WSClient] Sent message:', message.type);
        } catch (error) {
            console.error('[WSClient] Error sending message:', error);
            this.messageQueue.push(message);
        }
    }

    /**
     * Flush queued messages
     */
    flushMessageQueue() {
        if (this.messageQueue.length === 0) {
            return;
        }
        
        console.log(`[WSClient] Flushing ${this.messageQueue.length} queued messages`);
        
        while (this.messageQueue.length > 0) {
            const message = this.messageQueue.shift();
            this.send(message);
        }
    }

    /**
     * Schedule reconnection attempt
     */
    scheduleReconnect() {
        if (this.reconnectAttempts >= this.config.maxReconnectAttempts) {
            console.error('[WSClient] Max reconnection attempts reached');
            this.emit('max_reconnect_reached');
            return;
        }
        
        this.reconnectAttempts++;
        
        console.log(
            `[WSClient] Scheduling reconnect attempt ${this.reconnectAttempts}/${this.config.maxReconnectAttempts} ` +
            `in ${this.reconnectDelay}ms`
        );
        
        this.reconnectTimeout = setTimeout(() => {
            this.connect();
        }, this.reconnectDelay);
        
        // Exponential backoff
        this.reconnectDelay = Math.min(
            this.reconnectDelay * 2,
            this.config.maxReconnectDelay
        );
    }

    /**
     * Disconnect from server
     */
    disconnect() {
        if (this.reconnectTimeout) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }
        
        if (this.ws) {
            this.ws.close(1000, 'Client disconnect');
            this.ws = null;
        }
        
        this.connected = false;
        this.handshakeComplete = false;
    }

    /**
     * Get client state
     */
    getState() {
        return {
            connected: this.connected,
            handshakeComplete: this.handshakeComplete,
            reconnectAttempts: this.reconnectAttempts,
            queuedMessages: this.messageQueue.length,
        };
    }
    
    /**
     * Setup listeners for ComfyUI API events
     * Call this after ComfyUI's API is initialized
     */
    setupComfyListeners(comfyApi) {
        this.comfyApi = comfyApi;
        console.log('[WSClient] Setting up ComfyUI event listeners');
        
        // Error events
        this.comfyApi.addEventListener("execution_error", (event) => {
            console.error('[WSClient] ComfyUI execution error:', event.detail);
            this.send({
                type: "comfy_error",
                data: {
                    error_type: "execution_error",
                    ...event.detail,
                    timestamp: Date.now()
                }
            });
        });
        
        this.comfyApi.addEventListener("execution_interrupted", (event) => {
            console.warn('[WSClient] ComfyUI execution interrupted:', event.detail);
            this.send({
                type: "comfy_error",
                data: {
                    error_type: "execution_interrupted",
                    ...event.detail,
                    timestamp: Date.now()
                }
            });
        });
        
        // Queue status
        this.comfyApi.addEventListener("status", (event) => {
            this.send({
                type: "queue_status",
                data: event.detail
            });
        });
        
        // Execution tracking
        this.comfyApi.addEventListener("execution_start", (event) => {
            console.log('[WSClient] Execution started:', event.detail.prompt_id);
            this.send({
                type: "execution_event",
                event: "start",
                data: event.detail
            });
        });
        
        this.comfyApi.addEventListener("executing", (event) => {
            this.send({
                type: "execution_event",
                event: "executing",
                data: {run_id: event.detail}
            });
        });
        
        this.comfyApi.addEventListener("execution_cached", (event) => {
            console.log('[WSClient] Execution cached:', event.detail);
            this.send({
                type: "execution_event",
                event: "cached",
                data: event.detail
            });
        });
        
        this.comfyApi.addEventListener("execution_success", (event) => {
            console.log('[WSClient] Execution succeeded:', event.detail.prompt_id);
            this.send({
                type: "execution_event",
                event: "success",
                data: event.detail
            });
        });
        
        console.log('[WSClient] ComfyUI event listeners registered');
    }
}

export default WSClient;
