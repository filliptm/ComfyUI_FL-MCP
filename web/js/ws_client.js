/**
 * WebSocket Client for ComfyUI FL-MCP
 * 
 * Features:
 * - Session-based connection with handshake protocol
 * - Automatic reconnection with exponential backoff
 * - Message queueing during disconnection
 * - Event-driven architecture for message handling
 */

const CONNECTION_REPLACED_CLOSE_CODE = 4000;
const CONNECTION_REPLACED_CLOSE_REASON = 'replaced by a newer connection';


export function canonicalSupportedTools(toolNames) {
    if (!Array.isArray(toolNames)) {
        throw new TypeError('Supported tools must be an array');
    }
    const normalized = toolNames.map(toolName => {
        if (typeof toolName !== 'string' || toolName.length === 0) {
            throw new TypeError('Supported tool names must be non-empty strings');
        }
        return toolName;
    });
    return [...new Set(normalized)].sort();
}


export async function supportedToolsManifestHash(toolNames) {
    const canonicalTools = canonicalSupportedTools(toolNames);
    const subtle = globalThis.crypto?.subtle;
    if (!subtle) {
        throw new Error('Web Crypto is required to attest frontend tool capabilities');
    }
    const digest = await subtle.digest(
        'SHA-256',
        new TextEncoder().encode(JSON.stringify(canonicalTools)),
    );
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}


export function canonicalToolContractRevisions(toolNames, revisions = null) {
    const supportedTools = canonicalSupportedTools(toolNames);
    const supplied = revisions == null
        ? Object.fromEntries(supportedTools.map(toolName => [toolName, 1]))
        : revisions;
    if (
        supplied === null
        || typeof supplied !== 'object'
        || Array.isArray(supplied)
    ) {
        throw new TypeError('Tool contract revisions must be an object');
    }
    const revisionKeys = Object.keys(supplied).sort();
    if (
        revisionKeys.length !== supportedTools.length
        || revisionKeys.some((toolName, index) => toolName !== supportedTools[index])
    ) {
        throw new TypeError('Tool contract revisions must exactly cover supported tools');
    }
    return Object.fromEntries(revisionKeys.map(toolName => {
        const revision = supplied[toolName];
        if (!Number.isInteger(revision) || revision < 1) {
            throw new TypeError(
                `Tool contract revision for ${toolName} must be a positive integer`,
            );
        }
        return [toolName, revision];
    }));
}


export async function toolContractManifestHash(toolNames, revisions = null) {
    const canonicalRevisions = canonicalToolContractRevisions(toolNames, revisions);
    const subtle = globalThis.crypto?.subtle;
    if (!subtle) {
        throw new Error('Web Crypto is required to attest frontend tool contracts');
    }
    const digest = await subtle.digest(
        'SHA-256',
        new TextEncoder().encode(JSON.stringify(canonicalRevisions)),
    );
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}


function isConnectionReplacementClose(event) {
    return (
        event?.code === CONNECTION_REPLACED_CLOSE_CODE
        && event?.reason === CONNECTION_REPLACED_CLOSE_REASON
    );
}


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
            url: config.url || 'ws://127.0.0.1:8000/ws',
            maxReconnectAttempts: config.maxReconnectAttempts || 5,
            initialReconnectDelay: config.initialReconnectDelay || 1000, // 1 second
            maxReconnectDelay: config.maxReconnectDelay || 30000, // 30 seconds
            clientVersion: config.clientVersion || '1.0.0',
            clientId: config.clientId || 'browser',
            supportedTools: null,
            toolManifestHash: null,
            toolContractRevisions: null,
            toolContractManifestHash: null,
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
        
        // Warn if using default URL (config wasn't provided)
        if (!config.url) {
            console.warn('[FL-MCP WS] Using default WebSocket URL. Config should be fetched from /api/config');
        }
        
        console.log('[FL-MCP WS] Initialized with session:', this.sessionId);
        console.log('[FL-MCP WS] WebSocket URL:', this.config.url);
    }

    /**
     * Pin the exact browser handler manifest advertised on every handshake.
     * This must be called from the instantiated ToolExecutor before connect().
     */
    async setSupportedTools(toolNames, revisions = null) {
        const supportedTools = canonicalSupportedTools(toolNames);
        const toolManifestHash = await supportedToolsManifestHash(supportedTools);
        const toolContractRevisions = canonicalToolContractRevisions(
            supportedTools,
            revisions,
        );
        const toolContractHash = await toolContractManifestHash(
            supportedTools,
            toolContractRevisions,
        );
        this.config.supportedTools = Object.freeze(supportedTools);
        this.config.toolManifestHash = toolManifestHash;
        this.config.toolContractRevisions = Object.freeze(toolContractRevisions);
        this.config.toolContractManifestHash = toolContractHash;
        return {
            supported_tools: [...supportedTools],
            tool_manifest_hash: toolManifestHash,
            tool_contract_revisions: {...toolContractRevisions},
            tool_contract_manifest_hash: toolContractHash,
        };
    }

    /**
     * Connect to WebSocket server
     */
    connect() {
        if (
            this.ws
            && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)
        ) {
            console.log('[FL-MCP WS] Already connected or connecting');
            return;
        }

        if (this.reconnectTimeout) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }
        
        console.log(`[FL-MCP WS] Connecting to ${this.config.url}...`);
        
        try {
            const websocket = new WebSocket(this.config.url);
            this.ws = websocket;

            websocket.onopen = () => this.handleOpen(websocket);
            websocket.onclose = (event) => this.handleClose(websocket, event);
            websocket.onerror = (error) => this.handleError(websocket, error);
            websocket.onmessage = (event) => this.handleMessage(websocket, event);
            
        } catch (error) {
            console.error('[FL-MCP WS] Connection error:', error);
            this.scheduleReconnect();
        }
    }

    /**
     * Handle WebSocket open event
     */
    handleOpen(websocket) {
        if (this.ws !== websocket) return;
        console.log('[FL-MCP WS] WebSocket connected');
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
            connection_type: 'frontend',
            client_id: this.config.clientId,
            ...(this.config.supportedTools ? {
                supported_tools: [...this.config.supportedTools],
                tool_manifest_hash: this.config.toolManifestHash,
                tool_contract_revisions: {...this.config.toolContractRevisions},
                tool_contract_manifest_hash: this.config.toolContractManifestHash,
            } : {}),
        };
        
        console.log('[FL-MCP WS] Sending handshake:', handshake);
        this.ws.send(JSON.stringify(handshake));
    }

    /**
     * Handle WebSocket close event
     */
    handleClose(websocket, event) {
        if (this.ws !== websocket) return;
        console.log('[FL-MCP WS] WebSocket closed:', event.code, event.reason);
        this.ws = null;
        this.connected = false;
        this.handshakeComplete = false;
        
        this.emit('disconnected', event);

        // A newer browser bridge now owns this session. Reconnecting this
        // displaced client would evict the winner and make both tabs fight in
        // a permanent one-second replacement loop.
        if (isConnectionReplacementClose(event)) {
            console.info(
                '[FL-MCP WS] A newer browser connection owns this session; staying disconnected'
            );
            return;
        }

        // Attempt reconnection for genuine failures, but not clean closes.
        if (event.code !== 1000) {
            this.scheduleReconnect();
        }
    }

    /**
     * Handle WebSocket error
     */
    handleError(websocket, error) {
        if (this.ws !== websocket) return;
        console.error('[FL-MCP WS] WebSocket error:', error);
        this.emit('error', error);
    }

    /**
     * Handle incoming WebSocket message
     */
    handleMessage(websocket, event) {
        if (this.ws !== websocket) return;
        try {
            const message = JSON.parse(event.data);
            console.log('[FL-MCP WS] Received message:', message.type);
            
            switch (message.type) {
                case 'handshake_ack':
                    this.handleHandshakeAck(message);
                    break;

                case 'tool_request':
                    this.emit('tool_request', message);
                    break;

                case 'tool_report':
                    this.emit('tool_report', message);
                    break;

                case 'error':
                    this.emit('error', message);
                    break;

                default:
                    console.warn('[FL-MCP WS] Unknown message type:', message.type);
            }
            
        } catch (error) {
            console.error('[FL-MCP WS] Error parsing message:', error);
        }
    }

    isConnectedOrConnecting() {
        return Boolean(
            this.ws
            && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)
        );
    }

    /**
     * Handle handshake acknowledgment
     */
    handleHandshakeAck(message) {
        console.log('[FL-MCP WS] Handshake acknowledged:', message.status);
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
            console.log('[FL-MCP WS] Queueing message:', message.type);
            this.messageQueue.push(message);
            return;
        }
        
        try {
            this.ws.send(JSON.stringify(message));
            console.log('[FL-MCP WS] Sent message:', message.type);
        } catch (error) {
            console.error('[FL-MCP WS] Error sending message:', error);
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
        
        console.log(`[FL-MCP WS] Flushing ${this.messageQueue.length} queued messages`);
        
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
            console.error('[FL-MCP WS] Max reconnection attempts reached');
            this.emit('max_reconnect_reached');
            return;
        }
        
        this.reconnectAttempts++;
        
        console.log(
            `[FL-MCP WS] Scheduling reconnect attempt ${this.reconnectAttempts}/${this.config.maxReconnectAttempts} ` +
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
        console.log('[FL-MCP WS] Setting up ComfyUI event listeners');
        
        // Error events
        this.comfyApi.addEventListener("execution_error", (event) => {
            console.error('[FL-MCP WS] ComfyUI execution error:', event.detail);
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
            console.warn('[FL-MCP WS] ComfyUI execution interrupted:', event.detail);
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
            console.log('[FL-MCP WS] Execution started:', event.detail.prompt_id);
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
            console.log('[FL-MCP WS] Execution cached:', event.detail);
            this.send({
                type: "execution_event",
                event: "cached",
                data: event.detail
            });
        });
        
        this.comfyApi.addEventListener("execution_success", (event) => {
            console.log('[FL-MCP WS] Execution succeeded:', event.detail.prompt_id);
            this.send({
                type: "execution_event",
                event: "success",
                data: event.detail
            });
        });
        
        console.log('[FL-MCP WS] ComfyUI event listeners registered');
    }
}

export default WSClient;
