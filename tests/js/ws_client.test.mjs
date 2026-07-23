import assert from "node:assert/strict";
import test from "node:test";

import WSClient from "../../web/js/ws_client.js";


class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    static instances = [];

    constructor(url) {
        this.url = url;
        this.readyState = FakeWebSocket.CONNECTING;
        FakeWebSocket.instances.push(this);
    }

    send() {}

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
