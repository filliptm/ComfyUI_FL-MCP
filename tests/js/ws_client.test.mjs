import assert from "node:assert/strict";
import test from "node:test";

import WSClient, {
    canonicalSupportedTools,
    canonicalToolContractRevisions,
    supportedToolsManifestHash,
    toolContractManifestHash,
} from "../../web/js/ws_client.js";


class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    static instances = [];

    constructor(url) {
        this.url = url;
        this.readyState = FakeWebSocket.CONNECTING;
        this.messages = [];
        FakeWebSocket.instances.push(this);
    }

    send(message) {
        this.messages.push(JSON.parse(message));
    }

    close(code = 1000, reason = "") {
        this.readyState = FakeWebSocket.CLOSED;
        this.onclose?.({code, reason});
    }
}


globalThis.WebSocket = FakeWebSocket;


test("connect suppresses duplicate sockets while connecting or open", () => {
    FakeWebSocket.instances = [];
    const client = new WSClient("session", {url: "ws://bridge/ws"});

    client.connect();
    client.connect();
    assert.equal(FakeWebSocket.instances.length, 1);

    FakeWebSocket.instances[0].readyState = FakeWebSocket.OPEN;
    client.connect();
    assert.equal(FakeWebSocket.instances.length, 1);
});


test("events from a stale socket cannot disconnect its replacement", () => {
    FakeWebSocket.instances = [];
    const client = new WSClient("session", {url: "ws://bridge/ws"});

    client.connect();
    const oldSocket = FakeWebSocket.instances[0];
    oldSocket.readyState = FakeWebSocket.CLOSED;
    client.connect();
    const newSocket = FakeWebSocket.instances[1];

    oldSocket.onclose({code: 4000, reason: "replaced"});

    assert.equal(client.ws, newSocket);
    assert.equal(client.isConnectedOrConnecting(), true);
});


test("a browser connection displaced by a newer client does not reconnect", () => {
    FakeWebSocket.instances = [];
    const client = new WSClient("session", {url: "ws://bridge/ws"});
    let disconnectedEvent = null;
    client.on("disconnected", event => {
        disconnectedEvent = event;
    });

    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.readyState = FakeWebSocket.CLOSED;
    socket.onclose({code: 4000, reason: "replaced by a newer connection"});

    assert.equal(client.ws, null);
    assert.equal(client.reconnectAttempts, 0);
    assert.equal(client.reconnectTimeout, null);
    assert.equal(FakeWebSocket.instances.length, 1);
    assert.deepEqual(disconnectedEvent, {
        code: 4000,
        reason: "replaced by a newer connection",
    });
});


test("other abnormal private closes remain retryable", () => {
    FakeWebSocket.instances = [];
    const client = new WSClient("session", {
        url: "ws://bridge/ws",
        initialReconnectDelay: 50,
    });

    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.readyState = FakeWebSocket.CLOSED;
    socket.onclose({code: 4000, reason: "temporary server failure"});

    assert.equal(client.reconnectAttempts, 1);
    assert.notEqual(client.reconnectTimeout, null);
    client.disconnect();
});


test("ordinary abnormal closes reconnect while clean closes stay disconnected", () => {
    FakeWebSocket.instances = [];
    const failedClient = new WSClient("failed-session", {
        url: "ws://bridge/ws",
        initialReconnectDelay: 50,
    });

    failedClient.connect();
    const failedSocket = FakeWebSocket.instances[0];
    failedSocket.readyState = FakeWebSocket.CLOSED;
    failedSocket.onclose({code: 1006, reason: "network lost"});

    assert.equal(failedClient.reconnectAttempts, 1);
    assert.notEqual(failedClient.reconnectTimeout, null);
    failedClient.disconnect();

    const cleanClient = new WSClient("clean-session", {url: "ws://bridge/ws"});
    cleanClient.connect();
    const cleanSocket = FakeWebSocket.instances.at(-1);
    cleanSocket.readyState = FakeWebSocket.CLOSED;
    cleanSocket.onclose({code: 1000, reason: "normal closure"});

    assert.equal(cleanClient.reconnectAttempts, 0);
    assert.equal(cleanClient.reconnectTimeout, null);
});


test("frontend handshake sends an explicit role and stable client identity", () => {
    FakeWebSocket.instances = [];
    const client = new WSClient("session", {
        url: "ws://bridge/ws",
        clientId: "browser-test",
    });
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.readyState = FakeWebSocket.OPEN;
    socket.onopen();

    assert.deepEqual(socket.messages[0], {
        type: "handshake",
        session_id: "session",
        client_version: "1.0.0",
        connection_type: "frontend",
        client_id: "browser-test",
    });
});


test("frontend handshake advertises a sorted handler manifest including GraphPatch", async () => {
    FakeWebSocket.instances = [];
    const client = new WSClient("session", {
        url: "ws://bridge/ws",
        clientId: "browser-current",
    });
    const manifest = await client.setSupportedTools(
        [
            "workflow_get_current_json",
            "find_node",
            "apply_workflow_graph_patch",
            "find_node",
        ],
        {
            workflow_get_current_json: 1,
            find_node: 1,
            apply_workflow_graph_patch: 3,
        },
    );

    assert.deepEqual(manifest, {
        supported_tools: [
            "apply_workflow_graph_patch",
            "find_node",
            "workflow_get_current_json",
        ],
        tool_manifest_hash: "400fe558f797a1ae3fb9d7514a9b1927c9694d47b9384f0c41c1aefcbe162e3c",
        tool_contract_revisions: {
            apply_workflow_graph_patch: 3,
            find_node: 1,
            workflow_get_current_json: 1,
        },
        tool_contract_manifest_hash: "a50992e03368df832d12a5d17cad5ecfe7672223efe5a1c6a79b5dc16a6d1c49",
    });

    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.readyState = FakeWebSocket.OPEN;
    socket.onopen();

    assert.deepEqual(socket.messages[0].supported_tools, manifest.supported_tools);
    assert.equal(socket.messages[0].tool_manifest_hash, manifest.tool_manifest_hash);
    assert.deepEqual(
        socket.messages[0].tool_contract_revisions,
        manifest.tool_contract_revisions,
    );
    assert.equal(
        socket.messages[0].tool_contract_manifest_hash,
        manifest.tool_contract_manifest_hash,
    );
    assert.ok(socket.messages[0].supported_tools.includes("apply_workflow_graph_patch"));
});


test("an older frontend handler set has a distinct deterministic manifest", async () => {
    const currentTools = canonicalSupportedTools([
        "workflow_get_current_json",
        "apply_workflow_graph_patch",
        "find_node",
    ]);
    const staleTools = canonicalSupportedTools([
        "workflow_get_current_json",
        "find_node",
    ]);

    assert.deepEqual(currentTools, [
        "apply_workflow_graph_patch",
        "find_node",
        "workflow_get_current_json",
    ]);
    assert.deepEqual(staleTools, ["find_node", "workflow_get_current_json"]);
    assert.equal(
        await supportedToolsManifestHash(currentTools),
        "400fe558f797a1ae3fb9d7514a9b1927c9694d47b9384f0c41c1aefcbe162e3c",
    );
    assert.equal(
        await supportedToolsManifestHash(staleTools),
        "790dcefe25f032563794f8abca6465f11a951da41baea22e16de663578c40152",
    );
    assert.notEqual(
        await supportedToolsManifestHash(currentTools),
        await supportedToolsManifestHash(staleTools),
    );
});


test("same tool names with a stale GraphPatch contract have a distinct manifest", async () => {
    const tools = [
        "workflow_get_current_json",
        "find_node",
        "apply_workflow_graph_patch",
    ];
    const current = canonicalToolContractRevisions(tools, {
        workflow_get_current_json: 1,
        find_node: 1,
        apply_workflow_graph_patch: 3,
    });
    const stale = canonicalToolContractRevisions(tools, {
        workflow_get_current_json: 1,
        find_node: 1,
        apply_workflow_graph_patch: 2,
    });

    assert.deepEqual(current, {
        apply_workflow_graph_patch: 3,
        find_node: 1,
        workflow_get_current_json: 1,
    });
    assert.equal(
        await toolContractManifestHash(tools, current),
        "a50992e03368df832d12a5d17cad5ecfe7672223efe5a1c6a79b5dc16a6d1c49",
    );
    assert.equal(
        await toolContractManifestHash(tools, stale),
        "6661aaeea5f3593ee103b41b941b5a5234b61c039906a5a75d763833da892292",
    );
    assert.notEqual(
        await toolContractManifestHash(tools, current),
        await toolContractManifestHash(tools, stale),
    );
});


test("tool contract revisions fail closed on incomplete or invalid maps", () => {
    assert.throws(
        () => canonicalToolContractRevisions(
            ["apply_workflow_graph_patch", "find_node"],
            {apply_workflow_graph_patch: 2},
        ),
        /exactly cover supported tools/,
    );
    assert.throws(
        () => canonicalToolContractRevisions(
            ["apply_workflow_graph_patch"],
            {apply_workflow_graph_patch: 0},
        ),
        /positive integer/,
    );
});
