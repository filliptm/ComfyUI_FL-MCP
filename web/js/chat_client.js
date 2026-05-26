/**
 * REST/SSE chat client for Ren.
 *
 * WebSocket remains responsible for tool callbacks and ComfyUI telemetry. This
 * client owns the user-facing chat turn so responses can stream and reattach.
 */
export class ChatClient {
    constructor(sessionId, config = {}) {
        this.sessionId = sessionId;
        this.baseUrl = config.baseUrl || this._baseUrlFromWs(config.wsUrl) || 'http://127.0.0.1:8000';
        this.conversationId = config.conversationId || sessionId;
        this.reader = null;
        this.isStreaming = false;
    }

    _baseUrlFromWs(wsUrl) {
        if (!wsUrl) return null;
        const protocol = wsUrl.startsWith('wss://') ? 'https://' : 'http://';
        const host = wsUrl.replace(/^wss?:\/\//, '').split('/')[0];
        return `${protocol}${host}`;
    }

    async sendMessage(message, onEvent) {
        if (this.isStreaming) {
            throw new Error('Ren is already responding');
        }

        const response = await fetch(`${this.baseUrl}/api/chat/send`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                sessionId: this.sessionId,
                conversationId: this.conversationId,
            }),
        });

        if (!response.ok || !response.body) {
            let detail = `Chat request failed: ${response.status}`;
            try {
                const data = await response.json();
                detail = data.error || detail;
            } catch {
                // Keep the status-based error.
            }
            throw new Error(detail);
        }

        this.isStreaming = true;
        try {
            await this._processStream(response.body, onEvent);
        } finally {
            this.isStreaming = false;
            this.reader = null;
        }
    }

    async attach(conversationId, onEvent) {
        if (this.isStreaming) return;
        this.conversationId = conversationId;

        const response = await fetch(`${this.baseUrl}/api/chat/stream/${encodeURIComponent(conversationId)}`);
        if (!response.ok || !response.body) {
            throw new Error(`Stream attach failed: ${response.status}`);
        }

        this.isStreaming = true;
        try {
            await this._processStream(response.body, onEvent);
        } finally {
            this.isStreaming = false;
            this.reader = null;
        }
    }

    async _processStream(body, onEvent) {
        const reader = body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        this.reader = reader;

        const processFrame = (frame) => {
            for (const line of frame.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                const event = JSON.parse(line.slice(6));
                if (event.type === 'conversation_id' && event.id) {
                    this.conversationId = event.id;
                }
                onEvent?.(event);
            }
        };

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split('\n\n');
            buffer = frames.pop() || '';

            for (const frame of frames) {
                processFrame(frame);
            }
        }

        if (buffer.trim()) {
            processFrame(buffer);
        }
    }

    async cancel(conversationId = this.conversationId) {
        const response = await fetch(`${this.baseUrl}/api/chat/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ conversationId, sessionId: this.sessionId }),
        });

        if (!response.ok && response.status !== 404) {
            throw new Error(`Cancel failed: ${response.status}`);
        }

        try {
            this.reader?.cancel();
        } catch {
            // Best effort. The backend cancel endpoint is authoritative.
        }
    }

    async active() {
        const response = await fetch(`${this.baseUrl}/api/chat/active`);
        if (!response.ok) return [];
        const data = await response.json();
        return data.active || [];
    }

    async listConversations() {
        const response = await fetch(`${this.baseUrl}/api/conversations`);
        if (!response.ok) return [];
        const data = await response.json();
        return data.conversations || [];
    }

    async createConversation(title = 'New chat') {
        const response = await fetch(`${this.baseUrl}/api/conversations`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessionId: this.sessionId, title }),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Create conversation failed: ${response.status}`);
        }
        this.conversationId = data.conversation.id;
        return data.conversation;
    }

    async loadConversation(conversationId) {
        const response = await fetch(`${this.baseUrl}/api/conversations/${encodeURIComponent(conversationId)}`);
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Load conversation failed: ${response.status}`);
        }
        this.conversationId = conversationId;
        return data;
    }

    async updateConversation(conversationId, updates) {
        const response = await fetch(`${this.baseUrl}/api/conversations/${encodeURIComponent(conversationId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Update conversation failed: ${response.status}`);
        }
        return data.conversation;
    }

    async deleteConversation(conversationId) {
        const response = await fetch(`${this.baseUrl}/api/conversations/${encodeURIComponent(conversationId)}`, {
            method: 'DELETE',
        });
        if (!response.ok && response.status !== 404) {
            throw new Error(`Delete conversation failed: ${response.status}`);
        }
        if (this.conversationId === conversationId) {
            this.conversationId = this.sessionId;
        }
    }

    async comfyStatus() {
        const response = await fetch(`${this.baseUrl}/api/comfy/status`);
        if (!response.ok) {
            throw new Error(`Comfy status failed: ${response.status}`);
        }
        return response.json();
    }

    async restartComfy() {
        const response = await fetch(`${this.baseUrl}/api/comfy/restart`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Comfy restart failed: ${response.status}`);
        }
        return data;
    }

    async comfyLogs(limit = 300) {
        const response = await fetch(`${this.baseUrl}/api/comfy/logs?limit=${encodeURIComponent(limit)}`);
        if (!response.ok) {
            throw new Error(`Comfy logs failed: ${response.status}`);
        }
        return response.json();
    }

    async providerStatus() {
        const response = await fetch(`${this.baseUrl}/api/providers/status`);
        if (!response.ok) {
            throw new Error(`Provider status failed: ${response.status}`);
        }
        return response.json();
    }

    async selectProvider(provider, model = null) {
        const response = await fetch(`${this.baseUrl}/api/providers/select`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, model }),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Provider select failed: ${response.status}`);
        }
        return data.status;
    }

    async setProviderKey(provider, apiKey) {
        const response = await fetch(`${this.baseUrl}/api/providers/key`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, apiKey }),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Provider key save failed: ${response.status}`);
        }
        return data.status;
    }

    async clearProviderKey(provider) {
        const response = await fetch(`${this.baseUrl}/api/providers/key/${encodeURIComponent(provider)}`, {
            method: 'DELETE',
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || `Provider key clear failed: ${response.status}`);
        }
        return data.status;
    }

    async setLocalConfig(baseURL, model, apiKey = null) {
        const response = await fetch(`${this.baseUrl}/api/local-llm/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ baseURL, model, apiKey }),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.message || data.error || `Local config failed: ${response.status}`);
        }
        return data.status;
    }

    async clearLocalConfig() {
        const response = await fetch(`${this.baseUrl}/api/local-llm/config`, {
            method: 'DELETE',
        });
        if (!response.ok) {
            throw new Error(`Local config clear failed: ${response.status}`);
        }
    }

    async listLocalModels(baseURL) {
        const url = new URL(`${this.baseUrl}/api/local-llm/models`);
        if (baseURL) {
            url.searchParams.set('baseURL', baseURL);
        }
        const response = await fetch(url);
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.message || data.error || `Local models failed: ${response.status}`);
        }
        const items = Array.isArray(data?.data) ? data.data : [];
        return items
            .map((model) => ({ id: String(model?.id || ''), label: String(model?.id || '') }))
            .filter((model) => model.id);
    }
}

export default ChatClient;
