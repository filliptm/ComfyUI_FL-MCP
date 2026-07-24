export class ChatClient {
    constructor(baseUrl = "") {
        this.baseUrl = baseUrl.replace(/\/$/, "");
        this.abortController = null;
        this.runId = null;
        this.conversationId = null;
    }

    async request(path, options = {}) {
        const response = await fetch(`${this.baseUrl}${path}`, {
            ...options,
            headers: {
                "Content-Type": "application/json",
                ...(options.headers || {}),
            },
        });
        if (!response.ok) {
            let detail = `${response.status} ${response.statusText}`;
            try {
                const payload = await response.json();
                detail = payload.detail || payload.error || detail;
            } catch (_) {
                // Keep the HTTP status when a response has no JSON body.
            }
            throw new Error(detail);
        }
        if (response.status === 204) return null;
        return response.json();
    }

    status(sessionId) {
        return this.request(`/api/chat/status?session_id=${encodeURIComponent(sessionId)}`);
    }

    settings() {
        return this.request("/api/chat/settings");
    }

    updateSettings(changes) {
        return this.request("/api/chat/settings", {
            method: "PATCH",
            body: JSON.stringify(changes),
        });
    }

    models() {
        return this.request("/api/chat/models");
    }

    setCredential(provider, credential) {
        return this.request(`/api/chat/credentials/${encodeURIComponent(provider)}`, {
            method: "PUT",
            body: JSON.stringify({ credential }),
        });
    }

    clearCredential(provider) {
        return this.request(`/api/chat/credentials/${encodeURIComponent(provider)}`, {
            method: "DELETE",
        });
    }

    startClaudeLogin() {
        return this.request("/api/chat/claude/login", {
            method: "POST",
            body: JSON.stringify({}),
        });
    }

    refreshClaudeStatus() {
        return this.request("/api/chat/claude/refresh", {
            method: "POST",
            body: JSON.stringify({}),
        });
    }

    startCodexLogin() {
        return this.request("/api/chat/codex/login", {
            method: "POST",
            body: JSON.stringify({}),
        });
    }

    refreshCodexStatus() {
        return this.request("/api/chat/codex/refresh", {
            method: "POST",
            body: JSON.stringify({}),
        });
    }

    listConversations(view = "active") {
        return this.request(
            `/api/chat/conversations?view=${encodeURIComponent(view)}`,
        );
    }

    loadConversation(conversationId) {
        return this.request(`/api/chat/conversations/${encodeURIComponent(conversationId)}`);
    }

    createConversation() {
        return this.request("/api/chat/conversations", {
            method: "POST",
            body: JSON.stringify({}),
        });
    }

    updateConversation(conversationId, changes) {
        return this.request(`/api/chat/conversations/${encodeURIComponent(conversationId)}`, {
            method: "PATCH",
            body: JSON.stringify(changes),
        });
    }

    deleteConversation(conversationId) {
        return this.request(`/api/chat/conversations/${encodeURIComponent(conversationId)}`, {
            method: "DELETE",
        });
    }

    async startRun({ sessionId, conversationId, message, onEvent, onReady }) {
        this.abortController = new AbortController();
        const response = await fetch(`${this.baseUrl}/api/chat/runs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                sessionId,
                conversationId: conversationId || null,
                message,
            }),
            signal: this.abortController.signal,
        });
        if (!response.ok) {
            let detail = `${response.status} ${response.statusText}`;
            try {
                const payload = await response.json();
                detail = payload.detail || payload.error || detail;
            } catch (_) {
                // Keep status.
            }
            throw new Error(detail);
        }
        this.runId = response.headers.get("X-FL-MCP-Run-Id");
        this.conversationId = response.headers.get("X-FL-MCP-Conversation-Id");
        onReady?.({
            runId: this.runId,
            conversationId: this.conversationId,
        });
        await this.consumeSSE(response.body, onEvent);
    }

    async attach(runId, onEvent) {
        this.runId = runId;
        this.abortController = new AbortController();
        const response = await fetch(
            `${this.baseUrl}/api/chat/runs/${encodeURIComponent(runId)}/stream`,
            { signal: this.abortController.signal },
        );
        if (!response.ok) {
            throw new Error(`Unable to reattach to run: ${response.status}`);
        }
        await this.consumeSSE(response.body, onEvent);
    }

    async consumeSSE(body, onEvent) {
        if (!body) throw new Error("Streaming response body is unavailable.");
        const reader = body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let boundary = buffer.indexOf("\n\n");
            while (boundary >= 0) {
                const block = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                for (const line of block.split(/\r?\n/)) {
                    if (!line.startsWith("data:")) continue;
                    const payload = line.slice(5).trim();
                    if (!payload) continue;
                    onEvent?.(JSON.parse(payload));
                }
                boundary = buffer.indexOf("\n\n");
            }
        }
    }

    async cancel() {
        if (!this.runId) return false;
        await this.request(`/api/chat/runs/${encodeURIComponent(this.runId)}/cancel`, {
            method: "POST",
            body: JSON.stringify({}),
        });
        return true;
    }

    approve(approvalId, decision) {
        return this.request(`/api/chat/approvals/${encodeURIComponent(approvalId)}`, {
            method: "POST",
            body: JSON.stringify({ decision }),
        });
    }

    detach() {
        this.abortController?.abort();
        this.abortController = null;
    }
}
