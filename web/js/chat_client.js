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
}

export default ChatClient;
