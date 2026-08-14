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


test("run requests include per-message reasoning and search modes", async () => {
    const originalFetch = globalThis.fetch;
    let request;
    globalThis.fetch = async (url, options = {}) => {
        request = { url: String(url), options };
        return new Response("", {
            status: 200,
            headers: {
                "X-FL-MCP-Run-Id": "run-1",
                "X-FL-MCP-Conversation-Id": "conversation-1",
            },
        });
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        await client.startRun({
            sessionId: "session-1",
            message: "Inspect this workflow",
            reasoningEffort: "xhigh",
            searchMode: "tavily_basic",
            workflow: {
                id: "workflow-a",
                name: "A",
                path: "workflows/a.json",
            },
        });
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(request.url, "http://127.0.0.1:18000/api/chat/runs");
    assert.deepEqual(JSON.parse(request.options.body), {
        sessionId: "session-1",
        conversationId: null,
        message: "Inspect this workflow",
        reasoningEffort: "xhigh",
        searchMode: "tavily_basic",
        editMessageId: null,
        attachments: [],
        workflow: {
            id: "workflow-a",
            name: "A",
            path: "workflows/a.json",
        },
    });
});


test("run requests include uploaded ComfyUI image references", async () => {
    const originalFetch = globalThis.fetch;
    let payload;
    globalThis.fetch = async (_url, options = {}) => {
        payload = JSON.parse(options.body);
        return new Response("", {
            status: 200,
            headers: {
                "X-FL-MCP-Run-Id": "run-1",
                "X-FL-MCP-Conversation-Id": "conversation-1",
            },
        });
    };
    try {
        await new ChatClient().startRun({
            sessionId: "session-1",
            message: "Use this",
            attachments: [{
                filename: "reference.png",
                subfolder: "ren-chat/session-1",
                type: "input",
            }],
        });
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.deepEqual(payload.attachments, [{
        filename: "reference.png",
        subfolder: "ren-chat/session-1",
        type: "input",
    }]);
});


test("cancel waits for a starting run and targets its resolved run id", async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    let resolveStart;
    globalThis.fetch = (url, options = {}) => {
        requests.push({ url: String(url), options });
        if (String(url).endsWith("/api/chat/runs")) {
            return new Promise(resolve => {
                resolveStart = () => resolve(new Response("", {
                    status: 200,
                    headers: {
                        "X-FL-MCP-Run-Id": "run-starting",
                        "X-FL-MCP-Conversation-Id": "conversation-1",
                    },
                }));
            });
        }
        return Promise.resolve(new Response(JSON.stringify({ cancelled: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        }));
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        const run = client.startRun({
            sessionId: "session-1",
            message: "Initial request",
        });
        const cancelled = client.cancel();
        await Promise.resolve();
        assert.equal(requests.length, 1);
        resolveStart();
        assert.equal(await cancelled, true);
        await run;
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(
        requests[1].url,
        "http://127.0.0.1:18000/api/chat/runs/run-starting/cancel",
    );
});


test("cancel gets the run id from SSE when CORS hides response headers", async () => {
    const originalFetch = globalThis.fetch;
    const encoder = new TextEncoder();
    const requests = [];
    let streamController;
    globalThis.fetch = (url, options = {}) => {
        requests.push({ url: String(url), options });
        if (String(url).endsWith("/api/chat/runs")) {
            return Promise.resolve(new Response(new ReadableStream({
                start(controller) {
                    streamController = controller;
                },
            }), { status: 200 }));
        }
        return Promise.resolve(new Response(JSON.stringify({ cancelled: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        }));
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        const run = client.startRun({
            sessionId: "session-1",
            message: "Initial request",
        });
        const cancelled = client.cancel();
        await Promise.resolve();
        streamController.enqueue(encoder.encode(
            'data: {"type":"RUN_STARTED","runId":"run-from-event",'
            + '"threadId":"conversation-1"}\n\n',
        ));
        assert.equal(await cancelled, true);
        streamController.close();
        await run;
        assert.equal(client.conversationId, "conversation-1");
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(
        requests[1].url,
        "http://127.0.0.1:18000/api/chat/runs/run-from-event/cancel",
    );
});


test("steering uses the atomic replacement endpoint", async () => {
    const originalFetch = globalThis.fetch;
    let request;
    globalThis.fetch = async (url, options = {}) => {
        request = { url: String(url), options };
        return new Response(
            'data: {"type":"RUN_STARTED","runId":"run-new",'
            + '"threadId":"conversation-1"}\n\n',
            {
                status: 200,
                headers: {
                    "X-FL-MCP-Run-Id": "run-new",
                    "X-FL-MCP-Conversation-Id": "conversation-1",
                },
            },
        );
    };
    try {
        await new ChatClient("http://127.0.0.1:18000").startRun({
            sessionId: "session-1",
            message: "New direction",
            steerRunId: "run old",
        });
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(
        request.url,
        "http://127.0.0.1:18000/api/chat/runs/run%20old/steer",
    );
    assert.equal(request.options.method, "POST");
});


test("web image previews stay on the local Ren backend", () => {
    const client = new ChatClient("http://127.0.0.1:18000");

    assert.equal(
        client.webImagePreviewUrl("https://images.example/reference one.png"),
        "http://127.0.0.1:18000/api/chat/web-images/preview"
        + "?url=https%3A%2F%2Fimages.example%2Freference%20one.png",
    );
});


test("edited requests and version navigation use branch-aware endpoints", async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (url, options = {}) => {
        requests.push({ url: String(url), options });
        if (String(url).endsWith("/api/chat/runs")) {
            return new Response("", {
                status: 200,
                headers: {
                    "X-FL-MCP-Run-Id": "run-1",
                    "X-FL-MCP-Conversation-Id": "conversation-1",
                },
            });
        }
        return new Response(JSON.stringify({ messages: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    };
    try {
        const client = new ChatClient("http://127.0.0.1:18000");
        await client.startRun({
            sessionId: "session-1",
            conversationId: "conversation 1",
            message: "Revised request",
            editMessageId: "message 1",
        });
        await client.selectMessageVersion("conversation 1", "message 1", -1);
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(JSON.parse(requests[0].options.body).editMessageId, "message 1");
    assert.equal(
        requests[1].url,
        "http://127.0.0.1:18000/api/chat/conversations/conversation%201"
        + "/messages/message%201/version",
    );
    assert.equal(requests[1].options.method, "POST");
    assert.deepEqual(JSON.parse(requests[1].options.body), { direction: -1 });
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


test("conversation history loads in bounded cursor pages", async () => {
    const originalFetch = globalThis.fetch;
    let requestedUrl;
    globalThis.fetch = async url => {
        requestedUrl = String(url);
        return new Response(JSON.stringify({ messages: [], hasMore: false }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    };
    try {
        await new ChatClient("http://127.0.0.1:18000").loadConversation(
            "conversation 1",
            { before: "message 40", limit: 25 },
        );
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(
        requestedUrl,
        "http://127.0.0.1:18000/api/chat/conversations/conversation%201"
        + "?limit=25&before=message+40",
    );
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
