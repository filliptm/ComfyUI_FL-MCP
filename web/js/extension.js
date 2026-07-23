/**
 * ComfyUI FL-MCP browser bridge.
 *
 * The sidebar exposes Ren, the embedded FL-MCP Assistant. Ren and external
 * MCP clients share this browser bridge and the same MCP tool implementation.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import SessionManager from "./session_manager.js";
import WSClient from "./ws_client.js";
import { ToolExecutor } from "./tool_executor.js";
import { getToolConfig } from "./tool_activity.js";
import { AssistantPanel } from "./chat_panel.js";
import { installRenAwareFitView } from "./fit_view.js";

let wsClient = null;
let toolExecutor = null;
let assistantPanel = null;

function selectedNodeCount() {
    const selected = app.canvas?.selected_nodes;
    if (!selected) return 0;
    if (typeof selected.size === "number") return selected.size;
    if (Array.isArray(selected)) return selected.length;
    return Object.keys(selected).length;
}

function getCanvasContext() {
    return {
        connected: Boolean(wsClient?.connected && wsClient?.handshakeComplete),
        nodeCount: Array.isArray(app.graph?._nodes) ? app.graph._nodes.length : 0,
        selectedCount: selectedNodeCount(),
    };
}

function subscribeCanvasContext(callback) {
    let frame = null;
    const notify = () => {
        if (frame !== null) cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => {
            frame = null;
            callback();
        });
    };
    const canvasElement = app.canvas?.canvas;
    canvasElement?.addEventListener("pointerup", notify);
    window.addEventListener("keyup", notify);
    api.addEventListener("graphChanged", notify);
    api.addEventListener("execution_start", notify);
    return () => {
        if (frame !== null) cancelAnimationFrame(frame);
        canvasElement?.removeEventListener("pointerup", notify);
        window.removeEventListener("keyup", notify);
        api.removeEventListener("graphChanged", notify);
        api.removeEventListener("execution_start", notify);
    };
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
        throw new Error(`${url} failed: ${response.status}`);
    }
    return await response.json();
}

async function fetchClientConfig(baseUrl = "") {
    try {
        return await fetchJson(`${baseUrl}/api/config`);
    } catch (error) {
        console.warn("[FL-MCP] Failed to fetch config, using defaults:", error);
        return {
            ws_url: "ws://127.0.0.1:8000/ws",
            version: "unknown",
            public_url: "http://127.0.0.1:8000",
        };
    }
}

async function fetchLauncherStatus() {
    try {
        return await fetchJson("/fl_mcp/launcher/status");
    } catch (error) {
        console.warn("[FL-MCP] Launcher status unavailable:", error);
        return { backendReachable: false, wsUrl: "ws://127.0.0.1:8000/ws" };
    }
}

class BridgeStatusPanel {
    constructor(container, sessionManager, wsClient, toolExecutor, options = {}) {
        this.container = container;
        this.sessionManager = sessionManager;
        this.wsClient = wsClient;
        this.toolExecutor = toolExecutor;
        this.onBackendStatus = options.onBackendStatus;
        this.launcherStatus = null;
        this.recentTools = [];
        this.pollTimer = null;
        this.render();
        this.refresh();
        this.pollTimer = setInterval(() => this.refresh(), 5000);
    }

    render() {
        this.container.innerHTML = `
            <section class="fl-mcp-panel">
                <header class="fl-mcp-header">
                    <div>
                        <div class="fl-mcp-title">FL-MCP</div>
                        <div class="fl-mcp-subtitle">ComfyUI bridge status</div>
                    </div>
                    <span class="fl-mcp-pill" id="fl-mcp-backend-pill">Checking</span>
                </header>

                <div class="fl-mcp-grid">
                    <div class="fl-mcp-metric">
                        <span class="fl-mcp-label">Backend</span>
                        <strong id="fl-mcp-backend">Unknown</strong>
                    </div>
                    <div class="fl-mcp-metric">
                        <span class="fl-mcp-label">WebSocket</span>
                        <strong id="fl-mcp-ws">Disconnected</strong>
                    </div>
                    <div class="fl-mcp-metric wide">
                        <span class="fl-mcp-label">Session</span>
                        <code id="fl-mcp-session"></code>
                    </div>
                </div>

                <div class="fl-mcp-actions">
                    <button class="fl-mcp-button primary" id="fl-mcp-toggle" type="button">Start backend</button>
                    <button class="fl-mcp-button" id="fl-mcp-reconnect" type="button">Reconnect</button>
                </div>

                <section class="fl-mcp-activity">
                    <div class="fl-mcp-section-title">Recent tool activity</div>
                    <div id="fl-mcp-tools" class="fl-mcp-tools empty">No tool activity yet</div>
                </section>
            </section>
        `;

        this.backendPill = this.container.querySelector("#fl-mcp-backend-pill");
        this.backendText = this.container.querySelector("#fl-mcp-backend");
        this.wsText = this.container.querySelector("#fl-mcp-ws");
        this.sessionText = this.container.querySelector("#fl-mcp-session");
        this.toggleButton = this.container.querySelector("#fl-mcp-toggle");
        this.reconnectButton = this.container.querySelector("#fl-mcp-reconnect");
        this.toolsList = this.container.querySelector("#fl-mcp-tools");

        this.sessionText.textContent = this.sessionManager.getSessionId();
        this.toggleButton.addEventListener("click", () => this.toggleBackend());
        this.reconnectButton.addEventListener("click", () => this.reconnect());
        this.updateConnection();
    }

    async refresh() {
        this.launcherStatus = await fetchLauncherStatus();
        const running = Boolean(this.launcherStatus.backendReachable);
        this.backendText.textContent = running ? "Running" : "Stopped";
        this.backendPill.textContent = running ? "Online" : "Offline";
        this.backendPill.classList.toggle("online", running);
        this.toggleButton.textContent = running ? "Stop backend" : "Start backend";
        if (running && !this.wsClient.isConnectedOrConnecting()) {
            this.wsClient.connect();
        }
        this.updateConnection();
        this.onBackendStatus?.(running);
    }

    updateConnection() {
        const connected = Boolean(this.wsClient?.connected && this.wsClient?.handshakeComplete);
        this.wsText.textContent = connected ? "Connected" : "Disconnected";
        this.reconnectButton.disabled = !this.launcherStatus?.backendReachable;
    }

    addTool(toolName, state = "running") {
        const toolConfig = getToolConfig(toolName);
        this.recentTools.unshift({
            toolName,
            label: toolConfig.label || toolName,
            state,
            timestamp: new Date(),
        });
        this.recentTools = this.recentTools.slice(0, 12);
        this.renderTools();
    }

    completeTool(toolName, success = true) {
        const match = this.recentTools.find((tool) => tool.toolName === toolName && tool.state === "running");
        if (match) {
            match.state = success ? "done" : "failed";
            match.timestamp = new Date();
            this.renderTools();
        }
    }

    renderTools() {
        if (!this.recentTools.length) {
            this.toolsList.className = "fl-mcp-tools empty";
            this.toolsList.textContent = "No tool activity yet";
            return;
        }
        this.toolsList.className = "fl-mcp-tools";
        this.toolsList.innerHTML = this.recentTools.map((tool) => `
            <div class="fl-mcp-tool ${tool.state}">
                <span>${this.escapeHtml(tool.label)}</span>
                <em>${tool.state}</em>
            </div>
        `).join("");
    }

    async toggleBackend() {
        const running = Boolean(this.launcherStatus?.backendReachable);
        const endpoint = running ? "/fl_mcp/launcher/stop" : "/fl_mcp/launcher/start";
        this.toggleButton.disabled = true;
        this.toggleButton.textContent = running ? "Stopping..." : "Starting...";
        try {
            this.launcherStatus = await fetchJson(endpoint, { method: "POST" });
            if (
                !running
                && this.launcherStatus?.backendReachable
                && !this.wsClient.isConnectedOrConnecting()
            ) {
                this.wsClient.connect();
            }
        } catch (error) {
            console.error("[FL-MCP] Backend toggle failed:", error);
        } finally {
            this.toggleButton.disabled = false;
            await this.refresh();
        }
    }

    reconnect() {
        if (this.wsClient?.ws) {
            this.wsClient.ws.close(1000, "manual reconnect");
        }
        this.wsClient.connect();
    }

    destroy() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
        this.container.innerHTML = "";
    }

    escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = String(value);
        return div.innerHTML;
    }
}

app.registerExtension({
    name: "fl_mcp.bridge",

    async setup() {
        console.log("[FL-MCP] Initializing browser bridge");

        try {
            const sessionManager = new SessionManager();
            const sessionId = sessionManager.getSessionId();
            const launcherStatus = await fetchLauncherStatus();
            const config = await fetchClientConfig(launcherStatus.backendUrl || "");
            const wsUrl = launcherStatus.wsUrl || config.ws_url;

            wsClient = new WSClient(sessionId, {
                url: wsUrl,
                heartbeatInterval: 30000,
                maxReconnectAttempts: 5,
                clientVersion: `${config.version}-frontend`,
            });
            toolExecutor = new ToolExecutor(wsClient);

            wsClient.on("connected", () => assistantPanel?.updateConnection());
            wsClient.on("disconnected", () => assistantPanel?.updateConnection());
            wsClient.on("handshake_ack", () => {
                assistantPanel?.updateConnection();
                if (window.app?.api) {
                    wsClient.setupComfyListeners(window.app.api);
                } else {
                    setTimeout(() => {
                        if (window.app?.api) {
                            wsClient.setupComfyListeners(window.app.api);
                        }
                    }, 1000);
                }
            });

            wsClient.on("tool_request", async (message) => {
                assistantPanel?.addTool(message.tool_name, "running");
                try {
                    await toolExecutor.executeToolRequest(message);
                    assistantPanel?.completeTool(message.tool_name, true);
                } catch (error) {
                    console.error("[FL-MCP] Tool execution failed:", error);
                    assistantPanel?.completeTool(message.tool_name, false);
                } finally {
                    assistantPanel?.refreshCanvasContext();
                }
            });

            wsClient.on("tool_report", (message) => {
                assistantPanel?.addTool(message.tool_name, "done");
                assistantPanel?.refreshCanvasContext();
            });

            wsClient.on("error", (error) => {
                console.error("[FL-MCP] WebSocket error:", error);
                assistantPanel?.updateConnection();
            });

            window.FL_MCP = {
                sessionManager,
                wsClient,
                toolExecutor,
                app,
                version: config.version,
                backendUrl: launcherStatus.backendUrl || config.public_url,
            };

            if (launcherStatus.backendReachable) {
                wsClient.connect();
            }
            console.log("[FL-MCP] Browser bridge initialized");
        } catch (error) {
            console.error("[FL-MCP] Failed to initialize browser bridge:", error);
        }
    },
});

app.registerExtension({
    name: "fl_mcp.sidebar",

    async setup() {
        await new Promise((resolve) => {
            if (app.extensionManager) {
                resolve();
                return;
            }
            const interval = setInterval(() => {
                if (app.extensionManager) {
                    clearInterval(interval);
                    resolve();
                }
            }, 100);
        });

        installRenAwareFitView(app);

        const style = document.createElement("link");
        style.rel = "stylesheet";
        style.href = new URL("./style.css", import.meta.url);
        document.head.appendChild(style);

        app.extensionManager.registerSidebarTab({
            id: "fl_mcp_bridge",
            icon: "pi pi-comments",
            title: "Ren",
            tooltip: "Ren: connect your flow with FL-MCP",
            type: "custom",
            render: (el) => {
                // Setup normally installs this once. Re-check when the tab is
                // rendered in case ComfyUI initialized or replaced its canvas
                // after extension setup.
                installRenAwareFitView(app);
                if (!assistantPanel && window.FL_MCP?.sessionManager && wsClient && toolExecutor) {
                    assistantPanel = new AssistantPanel(
                        el,
                        window.FL_MCP.sessionManager,
                        {
                            baseUrl: window.FL_MCP.backendUrl || "",
                            createDiagnostics: (host, hooks) => new BridgeStatusPanel(
                                host,
                                window.FL_MCP.sessionManager,
                                wsClient,
                                toolExecutor,
                                hooks,
                            ),
                            getCanvasContext,
                            subscribeCanvasContext,
                        },
                    );
                    window.FL_MCP.assistantPanel = assistantPanel;
                }
                return el;
            },
            destroy: () => {
                assistantPanel?.destroy();
                assistantPanel = null;
                if (window.FL_MCP) {
                    window.FL_MCP.assistantPanel = null;
                }
            },
        });
    },
});

console.log("[FL-MCP] Extension module loaded");
