import assert from "node:assert/strict";
import test from "node:test";

import { ChatClient } from "../../web/js/chat_client.js";


test("SSE consumer handles chunk boundaries and multiple events", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({
        start(controller) {
            controller.enqueue(encoder.encode('data: {"type":"RUN_STARTED"}\n'));
            controller.enqueue(encoder.encode('\ndata: {"type":"TEXT_MESSAGE_CONTENT","delta":"hi"}\n\n'));
            controller.close();
        },
    });
    const events = [];
    const client = new ChatClient();

    await client.consumeSSE(body, (event) => events.push(event));

    assert.deepEqual(events, [
        { type: "RUN_STARTED" },
        { type: "TEXT_MESSAGE_CONTENT", delta: "hi" },
    ]);
});


test("conversation requests preserve active/archive views and additive updates", async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (url, options = {}) => {
        requests.push({ url: String(url), options });
        return new Response(JSON.stringify({ conversations: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        await client.listConversations("archived");
        await client.updateConversation("conversation 1", {
            archived: true,
            title: "Saved workflow",
        });
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(
        requests[0].url,
        "http://127.0.0.1:18000/api/chat/conversations?view=archived",
    );
    assert.equal(requests[1].options.method, "PATCH");
    assert.deepEqual(JSON.parse(requests[1].options.body), {
        archived: true,
        title: "Saved workflow",
    });
    assert.match(requests[1].url, /conversation%201$/);
});


test("Claude subscription actions use dedicated non-credential endpoints", async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (url, options = {}) => {
        requests.push({ url: String(url), options });
        return new Response(JSON.stringify({ configured: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        await client.startClaudeLogin();
        await client.refreshClaudeStatus();
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.deepEqual(
        requests.map(({ url }) => url),
        [
            "http://127.0.0.1:18000/api/chat/claude/login",
            "http://127.0.0.1:18000/api/chat/claude/refresh",
        ],
    );
    assert.ok(requests.every(({ options }) => options.method === "POST"));
    assert.ok(requests.every(({ options }) => options.body === "{}"));
});


test("Codex subscription actions use dedicated non-credential endpoints", async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (url, options = {}) => {
        requests.push({ url: String(url), options });
        return new Response(JSON.stringify({ configured: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        await client.startCodexLogin();
        await client.refreshCodexStatus();
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.deepEqual(
        requests.map(({ url }) => url),
        [
            "http://127.0.0.1:18000/api/chat/codex/login",
            "http://127.0.0.1:18000/api/chat/codex/refresh",
        ],
    );
    assert.ok(requests.every(({ options }) => options.method === "POST"));
    assert.ok(requests.every(({ options }) => options.body === "{}"));
});


test("approval decisions distinguish allow once from always allow", async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (url, options = {}) => {
        requests.push({ url: String(url), options });
        return new Response(JSON.stringify({
            resolved: true,
            approved: true,
            resolution: "always_allowed",
        }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        await client.approve("approval 1", "always_allow");
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.match(requests[0].url, /approvals\/approval%201$/);
    assert.deepEqual(JSON.parse(requests[0].options.body), {
        decision: "always_allow",
    });
});
