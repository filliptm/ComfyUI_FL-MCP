import { ChatClient } from "./chat_client.js";
import {
    canStackToolSteps,
    isNearBottom,
    modelProviderSummary,
    starterPrompts,
    summarizeToolStep,
    technicalText,
    toolStackState,
} from "./chat_ui_helpers.js";
import { renderMarkdown } from "./safe_markdown.js";
import { getToolConfig } from "./tool_activity.js";

export class AssistantPanel {
    constructor(container, sessionManager, options = {}) {
        this.container = container;
        this.sessionManager = sessionManager;
        this.chat = new ChatClient(options.baseUrl || "");
        this.createDiagnostics = options.createDiagnostics;
        this.getCanvasContext = options.getCanvasContext || (() => ({
            connected: Boolean(this.status?.bridgeConnected),
            nodeCount: 0,
            selectedCount: 0,
        }));
        this.subscribeCanvasContext = options.subscribeCanvasContext;
        this.settings = null;
        this.status = null;
        this.conversations = [];
        this.archivedConversations = [];
        this.historyView = "active";
        this.conversationId = null;
        this.running = false;
        this.initializing = false;
        this.currentAssistant = null;
        this.availableModels = [];
        this.diagnostics = null;
        this.backendRunning = null;
        this.canvasContext = { connected: false, nodeCount: 0, selectedCount: 0 };
        this.followOutput = true;
        this.jumpingToLatest = false;
        this.jumpScrollTimer = null;
        this.activeSheet = null;
        this.sheetReturnFocus = null;
        this.undoTimer = null;
        this.lastFailedMessage = "";
        this.lastArchivedConversation = null;
        this.pendingDeleteConversationId = null;
        this.contextUnsubscribe = null;
        this.render();
        this.bind();
        this.initialize();
    }

    render() {
        this.container.classList.add("fl-chat-panel-host");
        this.container.innerHTML = `
            <section class="fl-chat-layout">
                <div class="fl-chat-topbar">
                    <header class="fl-chat-header">
                        <div class="fl-chat-brand">
                            <div class="fl-chat-title">MCP</div>
                            <div class="fl-chat-status" aria-label="Chat status">
                                <span class="fl-status-indicator"></span>
                                <span class="fl-status-text">Checking…</span>
                            </div>
                        </div>
                        <div class="fl-chat-header-right">
                            <button class="fl-provider-badge" data-action="settings" data-provider="unknown" type="button" title="Open settings" aria-label="Open settings">
                                <span class="fl-provider-mark" aria-hidden="true">AI</span>
                                <span class="fl-provider-copy">
                                    <span class="fl-provider-name">Model</span>
                                    <span class="fl-provider-model">Checking…</span>
                                </span>
                            </button>
                            <button class="fl-icon-button" data-action="new-chat" type="button" title="New chat" aria-label="New chat">
                                <i class="pi pi-plus" aria-hidden="true"></i>
                            </button>
                            <button class="fl-icon-button" data-action="toggle-menu" type="button" title="More options" aria-label="More options" aria-expanded="false">
                                <i class="pi pi-ellipsis-h" aria-hidden="true"></i>
                            </button>
                            <div class="fl-overflow-menu" role="menu" hidden>
                                <button data-action="history" type="button" role="menuitem"><i class="pi pi-history" aria-hidden="true"></i>History</button>
                                <button data-action="settings" type="button" role="menuitem"><i class="pi pi-cog" aria-hidden="true"></i>Settings</button>
                                <button data-action="diagnostics" type="button" role="menuitem"><i class="pi pi-link" aria-hidden="true"></i>Bridge diagnostics</button>
                            </div>
                        </div>
                    </header>

                    <div class="fl-conversation-bar">
                        <button class="fl-conversation-title" data-action="history" type="button" aria-label="Open chat history">
                            <span>History</span>
                            <i class="pi pi-chevron-down" aria-hidden="true"></i>
                        </button>
                    </div>

                    <div class="fl-status-banner" hidden>
                        <i class="pi pi-exclamation-circle" aria-hidden="true"></i>
                        <span class="fl-status-banner-copy"></span>
                        <button class="fl-inline-action" data-action="status-action" type="button"></button>
                    </div>
                </div>

                <div class="fl-chat-messages">
                    <section class="fl-message ren-welcome">
                        <div class="fl-message-content">
                            <div class="fl-welcome-mark"><i class="pi pi-comments" aria-hidden="true"></i></div>
                            <strong>Work directly with your canvas.</strong>
                            <p>Ren can inspect, edit, organize, and run the open ComfyUI workflow.</p>
                            <div class="fl-starter-grid" aria-label="Suggested prompts"></div>
                        </div>
                    </section>
                    <div class="fl-chat-thread"></div>
                </div>

                <div class="fl-chat-bottombar">
                    <button class="fl-jump-latest" data-action="jump-latest" type="button" hidden>
                        Jump to latest <i class="pi pi-arrow-down" aria-hidden="true"></i>
                    </button>

                    <div class="fl-chat-error" role="alert" hidden>
                        <span class="fl-chat-error-copy"></span>
                        <div class="fl-chat-error-actions"></div>
                    </div>

                    <div class="fl-run-status" id="fl-run-drafting-hint" hidden>
                        <span class="fl-run-status-copy"><i class="pi pi-spin pi-spinner" aria-hidden="true"></i><span>Ren is working…</span></span>
                        <button class="fl-inline-action danger" data-action="stop" type="button">Stop</button>
                    </div>

                    <footer class="fl-chat-input-container">
                        <div class="fl-canvas-context">
                            <i class="pi pi-sitemap" aria-hidden="true"></i>
                            <span>Checking canvas…</span>
                        </div>
                        <div class="fl-composer-row">
                            <textarea class="fl-chat-input" rows="1" placeholder="Ask Ren about this workflow…" aria-label="Message"></textarea>
                            <button class="fl-chat-send" data-action="send" type="button" title="Send message (Enter)" aria-label="Send message" disabled>
                                <i class="pi pi-arrow-up" aria-hidden="true"></i>
                            </button>
                        </div>
                    </footer>
                </div>

                <section class="fl-chat-sheet" data-sheet="history" role="dialog" aria-modal="true" aria-labelledby="fl-history-title" hidden>
                    <header class="fl-sheet-header">
                        <button class="fl-icon-button" data-action="close-sheet" type="button" aria-label="Back to chat"><i class="pi pi-arrow-left" aria-hidden="true"></i></button>
                        <h2 id="fl-history-title">History</h2>
                        <button class="fl-icon-button" data-action="new-chat" type="button" aria-label="New chat"><i class="pi pi-plus" aria-hidden="true"></i></button>
                    </header>
                    <div class="fl-sheet-content">
                        <label class="fl-search-field">
                            <i class="pi pi-search" aria-hidden="true"></i>
                            <span class="fl-sr-only">Search conversations</span>
                            <input type="search" data-history-search placeholder="Search conversations">
                        </label>
                        <div class="fl-segmented-control" role="tablist" aria-label="Conversation state">
                            <button class="active" data-action="history-view" data-view="active" type="button" role="tab" aria-selected="true">Active</button>
                            <button data-action="history-view" data-view="archived" type="button" role="tab" aria-selected="false">Archived</button>
                        </div>
                        <div class="fl-history-list"></div>
                    </div>
                </section>

                <section class="fl-chat-sheet" data-sheet="settings" role="dialog" aria-modal="true" aria-labelledby="fl-settings-title" hidden>
                    <header class="fl-sheet-header">
                        <button class="fl-icon-button" data-action="close-sheet" type="button" aria-label="Back to chat"><i class="pi pi-arrow-left" aria-hidden="true"></i></button>
                        <h2 id="fl-settings-title">Settings</h2>
                        <span class="fl-sheet-header-spacer"></span>
                    </header>
                    <div class="fl-sheet-content fl-settings-content">
                        <section class="fl-settings-card fl-settings-card-model" data-settings-section="model" aria-labelledby="fl-settings-model-title">
                            <header class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-sliders-h"></i></span>
                                    <div>
                                        <h3 id="fl-settings-model-title">Model &amp; provider</h3>
                                        <p>Choose how Ren thinks and connects.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-state neutral" data-settings-state="model" role="status">Checking</span>
                            </header>
                            <div class="fl-settings-card-body">
                                <div class="fl-settings-fields">
                                    <label class="fl-field">
                                        <span>Provider</span>
                                        <select class="fl-provider-input" data-setting="provider"></select>
                                    </label>
                                    <label class="fl-field fl-endpoint-field">
                                        <span>Endpoint</span>
                                        <input class="fl-provider-input" data-setting="base_url" type="url" spellcheck="false" placeholder="Provider endpoint">
                                    </label>
                                    <label class="fl-field">
                                        <span>Model</span>
                                        <span class="fl-field-action">
                                            <input class="fl-provider-input" data-setting="model" type="text" list="fl-mcp-model-options" spellcheck="false" placeholder="Choose or enter a model">
                                            <select class="fl-provider-input" data-setting="subscription_model" aria-label="Subscription model" hidden></select>
                                            <button class="fl-secondary-button" data-action="discover-models" type="button">Refresh</button>
                                        </span>
                                    </label>
                                    <datalist id="fl-mcp-model-options"></datalist>
                                    <label class="fl-field fl-credential-field">
                                        <span>API key</span>
                                        <input class="fl-provider-input" data-setting="credential" type="password" autocomplete="off" placeholder="Stored in your OS keychain">
                                    </label>
                                </div>
                                <div class="fl-subscription-connection fl-claude-subscription" hidden>
                                    <div>
                                        <strong>Use your Claude subscription</strong>
                                        <span>Ren uses the Claude Code login already stored on this computer.</span>
                                    </div>
                                    <button class="fl-secondary-button" data-action="claude-login" type="button">Sign in with Claude</button>
                                </div>
                                <div class="fl-subscription-connection fl-codex-subscription" hidden>
                                    <div>
                                        <strong>Use your Codex subscription</strong>
                                        <span>Ren uses the ChatGPT login already stored by Codex on this computer.</span>
                                    </div>
                                    <button class="fl-secondary-button" data-action="codex-login" type="button">Sign in with Codex</button>
                                </div>
                            </div>
                            <footer class="fl-settings-card-footer">
                                <div class="fl-credential-status" role="status" aria-live="polite"></div>
                                <button class="fl-primary-button fl-settings-save" data-action="save-settings" type="button">Save and test</button>
                            </footer>
                        </section>

                        <section class="fl-settings-card fl-settings-card-approvals" data-settings-section="approvals" aria-labelledby="fl-settings-approvals-title">
                            <header class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-shield"></i></span>
                                    <div>
                                        <h3 id="fl-settings-approvals-title">Tool approvals</h3>
                                        <p>Control when Ren asks before acting.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-state neutral" data-settings-state="approvals" role="status">Prompts on</span>
                            </header>
                            <div class="fl-settings-card-body">
                                <label class="fl-approval-toggle">
                                    <input data-setting="approval_bypass" type="checkbox">
                                    <span>
                                        <strong>Bypass all approval prompts</strong>
                                        <span>Run every Ren MCP tool without asking in the chat.</span>
                                    </span>
                                </label>
                                <div class="fl-approval-rules">
                                    <div>
                                        <strong>Always allowed tools</strong>
                                        <span class="fl-approval-rules-copy">None</span>
                                    </div>
                                    <button class="fl-secondary-button" data-action="clear-always-allowed" type="button" hidden>Clear</button>
                                </div>
                                <p class="fl-approval-warning">
                                    <i class="pi pi-shield" aria-hidden="true"></i>
                                    <span>Server-side workflow, file, Git, Manager, and process safety gates still apply.</span>
                                </p>
                            </div>
                        </section>

                        <details class="fl-settings-card fl-settings-disclosure fl-settings-card-diagnostics" data-settings-section="diagnostics">
                            <summary class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-link"></i></span>
                                    <div>
                                        <h3>Bridge diagnostics</h3>
                                        <p>Connection health and recent tool activity.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-summary-state">
                                    <span class="fl-settings-state neutral" data-settings-state="diagnostics" role="status">Checking</span>
                                    <i class="pi pi-chevron-down fl-settings-chevron" aria-hidden="true"></i>
                                </span>
                            </summary>
                            <div class="fl-settings-card-body fl-diagnostics-card-body">
                                <div class="fl-diagnostics-host"></div>
                            </div>
                        </details>
                    </div>
                </section>

                <div class="fl-dialog-scrim" data-confirm-dialog hidden>
                    <section class="fl-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="fl-confirm-title" aria-describedby="fl-confirm-copy">
                        <h2 id="fl-confirm-title">Delete conversation permanently?</h2>
                        <p id="fl-confirm-copy">This removes the conversation and its messages. This cannot be undone.</p>
                        <div>
                            <button class="fl-secondary-button" data-action="cancel-confirm" type="button">Cancel</button>
                            <button class="fl-danger-button" data-action="confirm-delete" type="button">Delete permanently</button>
                        </div>
                    </section>
                </div>

                <div class="fl-toast" role="status" aria-live="polite" hidden>
                    <span></span>
                    <button data-action="undo-archive" type="button">Undo</button>
                </div>
                <div class="fl-sr-only fl-live-region" aria-live="polite" aria-atomic="true"></div>
            </section>
        `;
        this.scrollElement = this.container.querySelector(".fl-chat-messages");
        this.messagesElement = this.container.querySelector(".fl-chat-thread");
        this.welcomeElement = this.container.querySelector(".ren-welcome");
        this.errorElement = this.container.querySelector(".fl-chat-error");
        this.errorCopy = this.container.querySelector(".fl-chat-error-copy");
        this.errorActions = this.container.querySelector(".fl-chat-error-actions");
        this.textarea = this.container.querySelector(".fl-chat-input");
        this.sendButton = this.container.querySelector('[data-action="send"]');
        this.runStatus = this.container.querySelector(".fl-run-status");
        this.runStatusText = this.runStatus.querySelector("span span");
        this.jumpLatestButton = this.container.querySelector(".fl-jump-latest");
        this.conversationTitle = this.container.querySelector(".fl-conversation-title span");
        this.overflowButton = this.container.querySelector('[data-action="toggle-menu"]');
        this.overflowMenu = this.container.querySelector(".fl-overflow-menu");
        this.statusDot = this.container.querySelector(".fl-status-indicator");
        this.statusCopy = this.container.querySelector(".fl-status-text");
        this.providerBadge = this.container.querySelector(".fl-provider-badge");
        this.providerMark = this.container.querySelector(".fl-provider-mark");
        this.providerName = this.container.querySelector(".fl-provider-name");
        this.providerModel = this.container.querySelector(".fl-provider-model");
        this.statusBanner = this.container.querySelector(".fl-status-banner");
        this.statusBannerCopy = this.container.querySelector(".fl-status-banner-copy");
        this.providerSelect = this.container.querySelector('[data-setting="provider"]');
        this.endpointField = this.container.querySelector(".fl-endpoint-field");
        this.baseUrlInput = this.container.querySelector('[data-setting="base_url"]');
        this.modelInput = this.container.querySelector('[data-setting="model"]');
        this.subscriptionModelSelect = this.container.querySelector(
            '[data-setting="subscription_model"]',
        );
        this.credentialInput = this.container.querySelector('[data-setting="credential"]');
        this.credentialField = this.container.querySelector(".fl-credential-field");
        this.claudeSubscription = this.container.querySelector(".fl-claude-subscription");
        this.codexSubscription = this.container.querySelector(".fl-codex-subscription");
        this.credentialStatus = this.container.querySelector(".fl-credential-status");
        this.modelOptions = this.container.querySelector("#fl-mcp-model-options");
        this.modelSettingsState = this.container.querySelector(
            '[data-settings-state="model"]',
        );
        this.approvalBypassInput = this.container.querySelector(
            '[data-setting="approval_bypass"]',
        );
        this.approvalSettingsState = this.container.querySelector(
            '[data-settings-state="approvals"]',
        );
        this.approvalRulesCopy = this.container.querySelector(".fl-approval-rules-copy");
        this.clearApprovalRulesButton = this.container.querySelector(
            '[data-action="clear-always-allowed"]',
        );
        this.approvalWarning = this.container.querySelector(".fl-approval-warning");
        this.approvalWarningCopy = this.approvalWarning.querySelector("span");
        this.diagnosticsSettingsState = this.container.querySelector(
            '[data-settings-state="diagnostics"]',
        );
        this.historyList = this.container.querySelector(".fl-history-list");
        this.historySearch = this.container.querySelector("[data-history-search]");
        this.canvasContextElement = this.container.querySelector(".fl-canvas-context");
        this.toast = this.container.querySelector(".fl-toast");
        this.liveRegion = this.container.querySelector(".fl-live-region");
        this.confirmDialog = this.container.querySelector("[data-confirm-dialog]");
        this.renderStarters();
        if (this.createDiagnostics) {
            this.diagnostics = this.createDiagnostics(
                this.container.querySelector(".fl-diagnostics-host"),
                {
                    onBackendStatus: (running) => this.refreshBackendStatus(running),
                },
            );
        }
        this.contextUnsubscribe = this.subscribeCanvasContext?.(
            () => this.refreshCanvasContext(),
        ) || null;
    }

    bind() {
        this.container.addEventListener("click", (event) => {
            const actionElement = event.target.closest("[data-action]");
            if (!actionElement) return;
            const action = actionElement.dataset.action;
            if (action === "toggle-menu") this.toggleMenu(actionElement);
            if (action === "settings") this.openSheet("settings");
            if (action === "diagnostics") this.openSheet("settings", "diagnostics");
            if (action === "history") this.openSheet("history");
            if (action === "close-sheet") this.closeSheet();
            if (action === "new-chat") this.newConversation();
            if (action === "send") this.send();
            if (action === "stop") this.stop();
            if (action === "jump-latest") this.jumpToLatest();
            if (action === "status-action") this.handleStatusAction();
            if (action === "discover-models") this.discoverModels();
            if (action === "save-settings") this.saveSettings();
            if (action === "claude-login") this.connectClaudeSubscription();
            if (action === "codex-login") this.connectCodexSubscription();
            if (action === "clear-always-allowed") this.clearAlwaysAllowedTools();
            if (action === "history-view") this.setHistoryView(actionElement.dataset.view);
            if (action === "select-conversation") this.selectConversation(actionElement.dataset.conversationId);
            if (action === "rename-conversation") this.renameConversation(actionElement.dataset.conversationId);
            if (action === "save-rename") this.saveConversationRename(actionElement.dataset.conversationId);
            if (action === "cancel-rename") this.renderHistory();
            if (action === "archive-conversation") this.archiveConversation(actionElement.dataset.conversationId);
            if (action === "restore-conversation") this.restoreConversation(actionElement.dataset.conversationId);
            if (action === "delete-conversation") this.openDeleteConfirmation(actionElement.dataset.conversationId);
            if (action === "cancel-confirm") this.closeDeleteConfirmation();
            if (action === "confirm-delete") this.confirmPermanentDelete();
            if (action === "undo-archive") this.undoArchive();
        });
        this.providerSelect.addEventListener("change", () => this.applyProviderPreset());
        this.subscriptionModelSelect.addEventListener("change", () => {
            this.modelInput.value = this.subscriptionModelSelect.value;
            this.updateModelSettingsState();
        });
        this.modelInput.addEventListener("input", () => this.updateModelSettingsState());
        this.approvalBypassInput.addEventListener(
            "change",
            () => this.setApprovalBypass(),
        );
        this.textarea.addEventListener("keydown", (event) => {
            if (
                event.key === "Enter"
                && !event.shiftKey
                && !event.isComposing
                && !this.running
            ) {
                event.preventDefault();
                this.send();
            }
        });
        this.textarea.addEventListener("input", () => this.resizeComposer());
        this.textarea.addEventListener("input", () => this.updateComposerState());
        this.textarea.addEventListener("focus", () => this.refreshCanvasContext());
        this.scrollElement.addEventListener("scroll", () => this.handleThreadScroll());
        this.scrollElement.addEventListener("scrollend", () => {
            if (this.jumpingToLatest) this.finishJumpToLatest();
        });
        this.scrollElement.addEventListener("wheel", () => {
            if (this.jumpingToLatest) this.finishJumpToLatest();
        }, { passive: true });
        this.scrollElement.addEventListener("pointerdown", () => {
            if (this.jumpingToLatest) this.finishJumpToLatest();
        });
        this.historySearch.addEventListener("input", () => this.renderHistory());
        this.container.addEventListener("keydown", (event) => this.handleKeydown(event));
        this.documentPointerHandler = (event) => {
            if (!this.overflowMenu.hidden && !event.target.closest(".fl-chat-header-right")) {
                this.closeMenu();
            }
        };
        document.addEventListener("pointerdown", this.documentPointerHandler);
    }

    toggleMenu() {
        const willOpen = this.overflowMenu.hidden;
        this.overflowMenu.hidden = !willOpen;
        this.overflowButton.setAttribute("aria-expanded", String(willOpen));
        if (willOpen) {
            this.overflowMenu.querySelector("button")?.focus();
        }
    }

    closeMenu() {
        this.overflowMenu.hidden = true;
        this.overflowButton.setAttribute("aria-expanded", "false");
    }

    openSheet(name, section = null) {
        const sheet = this.container.querySelector(
            `.fl-chat-sheet[data-sheet="${CSS.escape(name)}"]`,
        );
        if (!sheet) return;
        this.closeMenu();
        if (!this.activeSheet) this.sheetReturnFocus = document.activeElement;
        this.container.querySelectorAll(".fl-chat-sheet").forEach(item => {
            item.hidden = item !== sheet;
        });
        sheet.hidden = false;
        this.activeSheet = sheet;
        this.container.querySelector(".fl-chat-layout").classList.add("sheet-open");
        if (name === "history") this.renderHistory();
        requestAnimationFrame(() => {
            if (section) {
                const target = sheet.querySelector(
                    `[data-settings-section="${CSS.escape(section)}"]`,
                );
                if (target instanceof HTMLDetailsElement) target.open = true;
                target?.scrollIntoView({ block: "start" });
            }
            this.focusableElements(sheet)[0]?.focus();
        });
    }

    closeSheet() {
        if (!this.activeSheet) return;
        this.activeSheet.hidden = true;
        this.activeSheet = null;
        this.container.querySelector(".fl-chat-layout").classList.remove("sheet-open");
        const returnFocus = this.sheetReturnFocus;
        this.sheetReturnFocus = null;
        if (returnFocus?.isConnected) returnFocus.focus();
    }

    focusableElements(host) {
        return [...host.querySelectorAll(
            'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
        )].filter(element => !element.hidden && !element.closest("[hidden]"));
    }

    handleKeydown(event) {
        if (event.key === "Escape") {
            if (!this.confirmDialog.hidden) {
                event.preventDefault();
                this.closeDeleteConfirmation();
                return;
            }
            if (!this.overflowMenu.hidden) {
                event.preventDefault();
                this.closeMenu();
                this.overflowButton.focus();
                return;
            }
            if (this.activeSheet) {
                event.preventDefault();
                this.closeSheet();
            }
            return;
        }
        if (event.key !== "Tab") return;
        const focusHost = !this.confirmDialog.hidden
            ? this.confirmDialog
            : this.activeSheet;
        if (!focusHost) return;
        const focusable = this.focusableElements(focusHost);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    handleStatusAction() {
        if (this.statusState === "warning" || this.statusState === "error") {
            this.openSheet("settings", "diagnostics");
            return;
        }
        this.openSheet("settings");
    }

    refreshCanvasContext() {
        const next = this.getCanvasContext?.() || {};
        this.canvasContext = {
            connected: Boolean(next.connected),
            nodeCount: Math.max(0, Number(next.nodeCount) || 0),
            selectedCount: Math.max(0, Number(next.selectedCount) || 0),
        };
        const { connected, nodeCount, selectedCount } = this.canvasContext;
        this.canvasContextElement.classList.toggle("disconnected", !connected);
        if (!connected) {
            this.canvasContextElement.querySelector("span").textContent = "Canvas disconnected";
        } else if (selectedCount) {
            this.canvasContextElement.querySelector("span").textContent =
                `${nodeCount} ${nodeCount === 1 ? "node" : "nodes"} · ${selectedCount} selected`;
        } else {
            this.canvasContextElement.querySelector("span").textContent =
                `${nodeCount} ${nodeCount === 1 ? "node" : "nodes"} · Nothing selected`;
        }
        if (!this.welcomeElement.hidden) this.renderStarters();
    }

    announce(message) {
        this.liveRegion.textContent = "";
        requestAnimationFrame(() => {
            this.liveRegion.textContent = message;
        });
    }

    async initialize() {
        if (this.initializing) return;
        this.initializing = true;
        try {
            const sessionId = this.sessionManager.getSessionId();
            [this.settings, this.status] = await Promise.all([
                this.chat.settings(),
                this.chat.status(sessionId),
            ]);
            this.populateSettings();
            await this.refreshConversations();
            this.updateStatus();
            this.updateComposerState();
            if (!this.status.configured) this.openSheet("settings");
        } catch (error) {
            this.showError(`Assistant setup could not load: ${error.message}`);
            this.updateStatus("error");
        } finally {
            this.initializing = false;
        }
    }

    async refreshBackendStatus(running) {
        this.backendRunning = Boolean(running);
        this.updateDiagnosticsSettingsState();
        if (this.initializing) return;
        if (!running) {
            this.updateStatus("error");
            return;
        }
        if (!this.status || !this.settings) {
            await this.initialize();
            return;
        }
        try {
            this.status = await this.chat.status(this.sessionManager.getSessionId());
            this.updateStatus();
        } catch (_) {
            this.updateStatus("error");
        }
    }

    renderStarters() {
        const host = this.container.querySelector(".fl-starter-grid");
        host.replaceChildren();
        for (const prompt of starterPrompts(this.canvasContext)) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "fl-starter-option";
            button.textContent = prompt;
            button.addEventListener("click", () => {
                this.textarea.value = prompt;
                this.resizeComposer();
                this.send();
            });
            host.appendChild(button);
        }
    }

    populateSettings() {
        this.providerSelect.replaceChildren();
        for (const [id, preset] of Object.entries(this.settings.presets || {})) {
            const option = document.createElement("option");
            option.value = id;
            option.textContent = preset.label;
            this.providerSelect.appendChild(option);
        }
        this.providerSelect.value = this.settings.provider;
        this.baseUrlInput.value = this.settings.base_url || "";
        this.modelInput.value = this.settings.model || "";
        this.approvalBypassInput.checked = (
            this.settings.approval_mode === "bypass_all"
        );
        const preset = this.settings.presets?.[this.settings.provider];
        this.availableModels = preset?.models || (this.settings.model
            ? [{ id: this.settings.model, label: this.settings.model }]
            : []);
        this.renderProviderControls();
        this.updateCredentialField();
        this.updateProviderBadge();
        this.renderApprovalSettings();
    }

    setSettingsState(element, label, tone = "neutral") {
        if (!element) return;
        element.textContent = label;
        element.classList.remove("neutral", "ready", "warning", "error");
        element.classList.add(tone);
    }

    updateModelSettingsState() {
        if (!this.settings) return;
        const preset = this.settings.presets?.[this.providerSelect.value];
        const hasModel = Boolean(this.modelInput.value.trim());
        const connection = this.settings.credential || {};
        const configured = (
            this.providerSelect.value === this.settings.provider
            && connection.configured
        );
        if (!hasModel) {
            this.setSettingsState(this.modelSettingsState, "Choose model", "warning");
            return;
        }
        if (["claude_cli", "codex_cli"].includes(preset?.type)) {
            if (connection.installed === false) {
                this.setSettingsState(this.modelSettingsState, "CLI missing", "error");
            } else if (configured) {
                this.setSettingsState(this.modelSettingsState, "Connected", "ready");
            } else {
                this.setSettingsState(this.modelSettingsState, "Sign in needed", "warning");
            }
            return;
        }
        if (preset?.requires_key && !configured) {
            this.setSettingsState(this.modelSettingsState, "API key needed", "warning");
            return;
        }
        this.setSettingsState(this.modelSettingsState, "Ready", "ready");
    }

    renderApprovalSettings() {
        const bypassed = this.approvalBypassInput.checked;
        const tools = this.settings?.always_allowed_tools || [];
        this.approvalRulesCopy.textContent = tools.length
            ? tools.join(", ")
            : "None";
        this.clearApprovalRulesButton.hidden = tools.length === 0;
        this.approvalWarning.classList.toggle("active", bypassed);
        this.approvalWarningCopy.textContent = bypassed
            ? "All chat approval prompts are bypassed. Server-side workflow, file, Git, Manager, and process safety gates still apply."
            : "Server-side workflow, file, Git, Manager, and process safety gates still apply.";
        this.setSettingsState(
            this.approvalSettingsState,
            bypassed ? "Bypassed" : "Prompts on",
            bypassed ? "warning" : "ready",
        );
    }

    updateDiagnosticsSettingsState(force = null) {
        if (this.status?.bridgeConnected) {
            this.setSettingsState(this.diagnosticsSettingsState, "Connected", "ready");
            return;
        }
        if (
            force === "error"
            || this.backendRunning === false
            || this.status?.available === false
        ) {
            this.setSettingsState(this.diagnosticsSettingsState, "Unavailable", "error");
            return;
        }
        if (!this.status && this.backendRunning === null) {
            this.setSettingsState(this.diagnosticsSettingsState, "Checking", "neutral");
            return;
        }
        this.setSettingsState(this.diagnosticsSettingsState, "Canvas offline", "warning");
    }

    async setApprovalBypass() {
        const requestedMode = this.approvalBypassInput.checked
            ? "bypass_all"
            : "autonomous_edits";
        const previousMode = this.settings.approval_mode;
        this.approvalBypassInput.disabled = true;
        this.renderApprovalSettings();
        try {
            this.settings = await this.chat.updateSettings({
                approval_mode: requestedMode,
            });
            this.approvalBypassInput.checked = (
                this.settings.approval_mode === "bypass_all"
            );
            this.renderApprovalSettings();
            const resolved = Number(this.settings.resolvedApprovals || 0);
            this.announce(
                requestedMode === "bypass_all"
                    ? `Approval prompts bypassed.${resolved ? ` ${resolved} pending approval resolved.` : ""}`
                    : "Approval prompts restored.",
            );
        } catch (error) {
            this.approvalBypassInput.checked = previousMode === "bypass_all";
            this.renderApprovalSettings();
            this.showError(`Approval mode could not be changed: ${error.message}`);
        } finally {
            this.approvalBypassInput.disabled = false;
        }
    }

    async clearAlwaysAllowedTools() {
        this.clearApprovalRulesButton.disabled = true;
        try {
            this.settings = await this.chat.updateSettings({
                always_allowed_tools: [],
            });
            this.renderApprovalSettings();
            this.announce("Always allowed tools cleared.");
        } catch (error) {
            this.showError(`Approval rules could not be cleared: ${error.message}`);
        } finally {
            this.clearApprovalRulesButton.disabled = false;
        }
    }

    async applyProviderPreset() {
        const preset = this.settings.presets[this.providerSelect.value];
        if (!preset) return;
        this.baseUrlInput.value = preset.base_url || "";
        this.modelInput.value = preset.default_model || "";
        this.availableModels = preset.models || (this.modelInput.value
            ? [{ id: this.modelInput.value, label: this.modelInput.value }]
            : []);
        this.renderProviderControls();
        this.updateCredentialField();
        if (["claude_cli", "codex_cli"].includes(preset.type)) {
            try {
                this.settings = await this.chat.updateSettings({
                    provider: this.providerSelect.value,
                    base_url: "",
                    model: this.modelInput.value,
                });
                this.status = await this.chat.status(this.sessionManager.getSessionId());
                this.populateSettings();
                this.updateStatus();
            } catch (error) {
                this.showError(`Provider could not be selected: ${error.message}`);
            }
            return;
        }
        if (!preset.requires_key && preset.base_url) {
            await this.discoverModels();
            return;
        }
        if (!preset.base_url) return;
        try {
            this.settings = await this.chat.updateSettings({
                provider: this.providerSelect.value,
                base_url: this.baseUrlInput.value,
                model: this.modelInput.value,
            });
            this.status = await this.chat.status(this.sessionManager.getSessionId());
            this.updateCredentialField();
            this.updateStatus();
        } catch (error) {
            this.showError(`Provider could not be selected: ${error.message}`);
        }
    }

    renderProviderControls() {
        const currentModel = this.modelInput.value || this.settings?.model || "";
        const models = [...this.availableModels];
        const preset = this.settings?.presets?.[this.providerSelect.value];
        const isSubscription = ["claude_cli", "codex_cli"].includes(preset?.type);
        if (currentModel && !models.some((model) => model.id === currentModel)) {
            models.unshift({ id: currentModel, label: currentModel });
        }
        this.modelOptions.replaceChildren();
        this.subscriptionModelSelect.replaceChildren();
        for (const model of models) {
            const option = document.createElement("option");
            option.value = model.id;
            option.label = model.label || model.id;
            this.modelOptions.appendChild(option);

            const subscriptionOption = document.createElement("option");
            subscriptionOption.value = model.id;
            subscriptionOption.textContent = model.label || model.id;
            if (model.description) subscriptionOption.title = model.description;
            this.subscriptionModelSelect.appendChild(subscriptionOption);
        }
        this.modelInput.hidden = isSubscription;
        this.subscriptionModelSelect.hidden = !isSubscription;
        if (isSubscription) {
            this.subscriptionModelSelect.value = currentModel;
            this.modelInput.value = this.subscriptionModelSelect.value;
        }
    }

    updateProviderBadge() {
        const summary = modelProviderSummary({
            ...(this.settings || {}),
            model: this.status?.model || this.settings?.model || "",
        });
        const description = `${summary.providerLabel} · ${summary.modelLabel}`;
        this.providerBadge.dataset.provider = summary.id;
        this.providerMark.textContent = summary.mark;
        this.providerName.textContent = summary.providerLabel;
        this.providerModel.textContent = summary.modelLabel;
        this.providerBadge.title = `Using ${description}. Open settings.`;
        this.providerBadge.setAttribute(
            "aria-label",
            `Using ${description}. Open settings.`,
        );
    }

    updateCredentialField() {
        const preset = this.settings.presets?.[this.providerSelect.value];
        const isClaudeSubscription = preset?.type === "claude_cli";
        const isCodexSubscription = preset?.type === "codex_cli";
        const isSubscription = isClaudeSubscription || isCodexSubscription;
        const requiresKey = Boolean(preset?.requires_key);
        const supportsKey = requiresKey || this.providerSelect.value === "custom";
        const connection = this.settings.credential || {};
        const configured = (
            this.providerSelect.value === this.settings.provider
            && connection.configured
        );
        this.endpointField.hidden = isSubscription;
        this.credentialField.hidden = !supportsKey;
        this.claudeSubscription.hidden = !isClaudeSubscription;
        this.codexSubscription.hidden = !isCodexSubscription;
        this.credentialField.querySelector("span").textContent = requiresKey
            ? "API key"
            : "API key (optional)";
        if (isSubscription) {
            const subscription = isClaudeSubscription
                ? this.claudeSubscription
                : this.codexSubscription;
            const button = subscription.querySelector("button");
            button.textContent = configured
                ? "Refresh status"
                : (isClaudeSubscription ? "Sign in with Claude" : "Sign in with Codex");
            button.disabled = connection.installed === false;
            this.credentialStatus.textContent = connection.message
                || (isClaudeSubscription ? "Checking Claude Code…" : "Checking Codex…");
            this.credentialStatus.classList.toggle("error", !configured);
            this.updateModelSettingsState();
            return;
        }
        this.credentialStatus.classList.remove("error");
        this.credentialStatus.textContent = supportsKey
            ? (configured
                ? `Credential ready · ${this.settings.credential.source}`
                : (requiresKey
                    ? "No credential configured"
                    : "No credential configured · optional"))
            : "No API key required for this preset";
        this.updateModelSettingsState();
    }

    async connectClaudeSubscription() {
        return this.connectSubscription({
            host: this.claudeSubscription,
            startLogin: () => this.chat.startClaudeLogin(),
            refreshStatus: () => this.chat.refreshClaudeStatus(),
            pendingMessage: "Finish signing in through the Claude Code terminal window.",
            connectedMessage: "Claude subscription connected.",
            signInLabel: "Sign in with Claude",
        });
    }

    async connectCodexSubscription() {
        return this.connectSubscription({
            host: this.codexSubscription,
            startLogin: () => this.chat.startCodexLogin(),
            refreshStatus: () => this.chat.refreshCodexStatus(),
            pendingMessage: "Finish signing in through the Codex terminal window.",
            connectedMessage: "Codex subscription connected.",
            signInLabel: "Sign in with Codex",
        });
    }

    async connectSubscription({
        host,
        startLogin,
        refreshStatus,
        pendingMessage,
        connectedMessage,
        signInLabel,
    }) {
        const button = host.querySelector("button");
        const connection = this.settings?.credential || {};
        button.disabled = true;
        this.credentialStatus.classList.remove("error");
        try {
            if (!connection.configured) {
                await startLogin();
                button.textContent = "Waiting for sign-in…";
                this.credentialStatus.textContent = pendingMessage;
            }
            const attempts = connection.configured ? 1 : 30;
            for (let index = 0; index < attempts; index += 1) {
                if (index > 0) {
                    await new Promise(resolve => setTimeout(resolve, 2000));
                }
                const refreshed = await refreshStatus();
                this.settings.credential = refreshed;
                this.status = await this.chat.status(this.sessionManager.getSessionId());
                this.updateCredentialField();
                this.updateStatus();
                if (refreshed.configured) {
                    this.announce(connectedMessage);
                    return;
                }
            }
            throw new Error(
                "Sign-in is still pending. Finish it in the terminal, then choose Refresh status.",
            );
        } catch (error) {
            this.credentialStatus.textContent = error.message;
            this.credentialStatus.classList.add("error");
        } finally {
            button.disabled = this.settings?.credential?.installed === false;
            button.textContent = this.settings?.credential?.configured
                ? "Refresh status"
                : signInLabel;
        }
    }

    async discoverModels() {
        const button = this.container.querySelector('[data-action="discover-models"]');
        button.disabled = true;
        button.textContent = "Checking…";
        this.credentialStatus.classList.remove("error");
        try {
            this.settings = await this.chat.updateSettings({
                provider: this.providerSelect.value,
                base_url: this.baseUrlInput.value,
                model: this.modelInput.value,
            });
            this.updateCredentialField();
            const result = await this.chat.models();
            this.availableModels = result.models || [];
            if (!this.modelInput.value && result.models?.length) {
                this.modelInput.value = result.models[0].id;
            }
            this.renderProviderControls();
            this.credentialStatus.textContent = `${result.models?.length || 0} models found`;
            this.status = await this.chat.status(this.sessionManager.getSessionId());
            this.updateStatus();
        } catch (error) {
            this.credentialStatus.textContent = error.message;
            this.credentialStatus.classList.add("error");
            try {
                this.status = await this.chat.status(this.sessionManager.getSessionId());
                this.updateStatus();
            } catch (_) {
                // Keep the model discovery error visible.
            }
        } finally {
            button.disabled = false;
            button.textContent = "Refresh";
        }
    }

    async saveSettings() {
        const button = this.container.querySelector('[data-action="save-settings"]');
        button.disabled = true;
        button.textContent = "Testing…";
        this.clearError();
        try {
            const provider = this.providerSelect.value;
            if (this.credentialInput.value.trim()) {
                const stored = await this.chat.setCredential(
                    provider,
                    this.credentialInput.value,
                );
                this.credentialInput.value = "";
                if (stored.warning) this.credentialStatus.textContent = stored.warning;
            }
            this.settings = await this.chat.updateSettings({
                provider,
                base_url: this.baseUrlInput.value,
                model: this.modelInput.value,
                approval_mode: this.approvalBypassInput.checked
                    ? "bypass_all"
                    : "autonomous_edits",
            });
            const providerType = this.settings.presets[provider].type;
            if (
                providerType === "openai_compatible"
                || ["claude_cli", "codex_cli"].includes(providerType)
            ) {
                const result = await this.chat.models();
                this.availableModels = result.models || this.availableModels;
            }
            this.status = await this.chat.status(this.sessionManager.getSessionId());
            this.updateStatus();
            this.updateCredentialField();
            this.renderProviderControls();
            this.renderApprovalSettings();
            if (this.status.configured) this.closeSheet();
        } catch (error) {
            this.showError(`Connection test failed: ${error.message}`);
        } finally {
            button.disabled = false;
            button.textContent = "Save and test";
        }
    }

    async refreshConversations(preferredId = this.conversationId) {
        const [active, archived] = await Promise.all([
            this.chat.listConversations("active"),
            this.chat.listConversations("archived"),
        ]);
        this.conversations = active.conversations || [];
        this.archivedConversations = archived.conversations || [];
        this.renderHistory();
        if (!this.conversations.length) {
            this.conversationId = null;
            this.updateConversationTitle();
            this.renderMessages([]);
            return;
        }
        const nextId = this.conversations.some((item) => item.id === preferredId)
            ? preferredId
            : this.conversations[0].id;
        await this.loadConversation(nextId);
    }

    async loadConversation(conversationId) {
        if (!conversationId || this.running) return;
        try {
            const result = await this.chat.loadConversation(conversationId);
            this.conversationId = conversationId;
            this.updateConversationTitle();
            this.renderHistory();
            this.renderMessages(result.messages || []);
        } catch (error) {
            this.showError(`Conversation could not load: ${error.message}`);
        }
    }

    async newConversation() {
        if (this.running) return;
        try {
            const result = await this.chat.createConversation();
            await this.refreshConversations(result.conversation.id);
            this.closeSheet();
            this.textarea.focus();
        } catch (error) {
            this.showError(`Conversation could not be created: ${error.message}`);
        }
    }

    updateConversationTitle() {
        this.conversationTitle.textContent = "History";
    }

    setHistoryView(view) {
        if (!["active", "archived"].includes(view)) return;
        this.historyView = view;
        this.container.querySelectorAll("[data-action='history-view']").forEach(button => {
            const selected = button.dataset.view === view;
            button.classList.toggle("active", selected);
            button.setAttribute("aria-selected", String(selected));
        });
        this.renderHistory();
    }

    renderHistory() {
        const conversations = this.historyView === "archived"
            ? this.archivedConversations
            : this.conversations;
        const query = this.historySearch?.value.trim().toLowerCase() || "";
        const visible = conversations.filter(item => (
            !query || item.title.toLowerCase().includes(query)
        ));
        this.historyList.replaceChildren();
        if (!visible.length) {
            const empty = document.createElement("div");
            empty.className = "fl-history-empty";
            empty.textContent = query
                ? "No conversations match this search."
                : this.historyView === "archived"
                    ? "No archived conversations."
                    : "No conversations yet.";
            this.historyList.appendChild(empty);
            return;
        }
        for (const conversation of visible) {
            const row = document.createElement("article");
            row.className = "fl-history-row";
            row.dataset.conversationId = conversation.id;
            if (conversation.id === this.conversationId) row.classList.add("current");

            const select = document.createElement("button");
            select.type = "button";
            select.className = "fl-history-select";
            select.dataset.action = "select-conversation";
            select.dataset.conversationId = conversation.id;
            const title = document.createElement("strong");
            title.textContent = conversation.title;
            const updated = document.createElement("span");
            updated.textContent = this.formatRelativeDate(conversation.updatedAt);
            select.append(title, updated);

            const actions = document.createElement("div");
            actions.className = "fl-history-actions";
            const rename = this.iconAction(
                "rename-conversation",
                conversation.id,
                "pi pi-pencil",
                "Rename conversation",
            );
            const stateAction = this.historyView === "archived"
                ? this.iconAction(
                    "restore-conversation",
                    conversation.id,
                    "pi pi-replay",
                    "Restore conversation",
                )
                : this.iconAction(
                    "archive-conversation",
                    conversation.id,
                    "pi pi-inbox",
                    "Archive conversation",
                );
            actions.append(rename, stateAction);
            if (this.historyView === "archived") {
                actions.appendChild(this.iconAction(
                    "delete-conversation",
                    conversation.id,
                    "pi pi-trash",
                    "Delete conversation permanently",
                    "danger",
                ));
            }

            const renameForm = document.createElement("div");
            renameForm.className = "fl-history-rename";
            renameForm.hidden = true;
            const input = document.createElement("input");
            input.type = "text";
            input.value = conversation.title;
            input.maxLength = 120;
            input.setAttribute("aria-label", "Conversation title");
            const save = document.createElement("button");
            save.type = "button";
            save.dataset.action = "save-rename";
            save.dataset.conversationId = conversation.id;
            save.textContent = "Save";
            const cancel = document.createElement("button");
            cancel.type = "button";
            cancel.dataset.action = "cancel-rename";
            cancel.textContent = "Cancel";
            renameForm.append(input, save, cancel);

            row.append(select, actions, renameForm);
            this.historyList.appendChild(row);
        }
    }

    iconAction(action, conversationId, iconClass, label, className = "") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `fl-history-icon ${className}`.trim();
        button.dataset.action = action;
        button.dataset.conversationId = conversationId;
        button.title = label;
        button.setAttribute("aria-label", label);
        const icon = document.createElement("i");
        icon.className = iconClass;
        icon.setAttribute("aria-hidden", "true");
        button.appendChild(icon);
        return button;
    }

    formatRelativeDate(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return "";
        const elapsed = Date.now() - date.getTime();
        if (elapsed < 60_000) return "Just now";
        if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`;
        if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h ago`;
        if (elapsed < 604_800_000) return `${Math.floor(elapsed / 86_400_000)}d ago`;
        return date.toLocaleDateString([], { month: "short", day: "numeric" });
    }

    async selectConversation(conversationId) {
        if (!conversationId || this.running) return;
        await this.loadConversation(conversationId);
        this.closeSheet();
    }

    renameConversation(conversationId) {
        const row = this.historyList.querySelector(
            `[data-conversation-id="${CSS.escape(conversationId)}"]`,
        );
        if (!row) return;
        row.querySelector(".fl-history-select").hidden = true;
        row.querySelector(".fl-history-actions").hidden = true;
        const form = row.querySelector(".fl-history-rename");
        form.hidden = false;
        const input = form.querySelector("input");
        input.focus();
        input.select();
    }

    async saveConversationRename(conversationId) {
        const row = this.historyList.querySelector(
            `[data-conversation-id="${CSS.escape(conversationId)}"]`,
        );
        const title = row?.querySelector(".fl-history-rename input")?.value.trim();
        if (!title) return;
        try {
            await this.chat.updateConversation(conversationId, { title });
            await this.refreshConversations(this.conversationId);
        } catch (error) {
            this.showError(`Conversation could not be renamed: ${error.message}`);
        }
    }

    async archiveConversation(conversationId) {
        if (!conversationId || this.running) return;
        const conversation = this.conversations.find(item => item.id === conversationId);
        try {
            await this.chat.updateConversation(conversationId, { archived: true });
            this.lastArchivedConversation = conversation || { id: conversationId, title: "Conversation" };
            const preferredId = conversationId === this.conversationId ? null : this.conversationId;
            await this.refreshConversations(preferredId);
            this.showArchiveToast(this.lastArchivedConversation.title);
        } catch (error) {
            this.showError(`Conversation could not be archived: ${error.message}`);
        }
    }

    async undoArchive() {
        const conversation = this.lastArchivedConversation;
        if (!conversation) return;
        try {
            await this.chat.updateConversation(conversation.id, { archived: false });
            this.hideToast();
            await this.refreshConversations(conversation.id);
        } catch (error) {
            this.showError(`Conversation could not be restored: ${error.message}`);
        }
    }

    async restoreConversation(conversationId) {
        try {
            await this.chat.updateConversation(conversationId, { archived: false });
            this.setHistoryView("active");
            await this.refreshConversations(conversationId);
        } catch (error) {
            this.showError(`Conversation could not be restored: ${error.message}`);
        }
    }

    showArchiveToast(title) {
        clearTimeout(this.undoTimer);
        this.toast.querySelector("span").textContent = `Archived “${title}”.`;
        this.toast.hidden = false;
        this.announce(`Archived ${title}. Undo is available.`);
        this.undoTimer = setTimeout(() => this.hideToast(), 6_000);
    }

    hideToast() {
        clearTimeout(this.undoTimer);
        this.undoTimer = null;
        this.toast.hidden = true;
    }

    openDeleteConfirmation(conversationId) {
        this.pendingDeleteConversationId = conversationId;
        this.confirmDialog.hidden = false;
        this.confirmDialog.querySelector('[data-action="confirm-delete"]').focus();
    }

    closeDeleteConfirmation() {
        this.pendingDeleteConversationId = null;
        this.confirmDialog.hidden = true;
    }

    async confirmPermanentDelete() {
        const conversationId = this.pendingDeleteConversationId;
        if (!conversationId) return;
        try {
            await this.chat.deleteConversation(conversationId);
            this.closeDeleteConfirmation();
            await this.refreshConversations(this.conversationId);
            this.setHistoryView("archived");
        } catch (error) {
            this.showError(`Conversation could not be deleted: ${error.message}`);
        }
    }

    renderMessages(messages) {
        this.messagesElement.replaceChildren();
        this.currentAssistant = null;
        for (const message of messages) {
            this.appendMessage(message.role, message.content, {
                ...(message.metadata || {}),
                createdAt: message.createdAt,
            });
        }
        const empty = messages.length === 0;
        this.welcomeElement.hidden = !empty;
        this.messagesElement.hidden = empty;
        if (!empty) this.scrollToBottom();
    }

    appendMessage(role, content, metadata = {}) {
        this.welcomeElement.hidden = true;
        this.messagesElement.hidden = false;
        const article = document.createElement("article");
        article.className = `fl-message ${role}`;
        const header = document.createElement("div");
        header.className = "fl-message-header";
        const label = document.createElement("span");
        label.className = "fl-message-role";
        label.textContent = role === "user" ? "You" : role === "assistant" ? "Ren" : role;
        const timestamp = document.createElement("span");
        timestamp.className = "fl-message-time";
        timestamp.textContent = this.formatTime(metadata.createdAt);
        header.append(label, timestamp);
        let body = null;
        let timeline = null;
        if (role === "assistant") {
            timeline = document.createElement("div");
            timeline.className = "fl-message-timeline";
            article.append(header, timeline);
            body = this.renderPersistedAssistantTimeline(
                timeline,
                content,
                metadata?.toolSteps || [],
            );
        } else {
            body = this.createMessageContent(content);
            article.append(header, body);
        }
        this.messagesElement.appendChild(article);
        this.maybeFollowOutput();
        return { article, body, timeline };
    }

    createMessageContent(content = "") {
        const body = document.createElement("div");
        body.className = "fl-message-content";
        body.appendChild(renderMarkdown(content));
        return body;
    }

    renderPersistedAssistantTimeline(timeline, content, toolSteps) {
        const characters = Array.from(String(content || ""));
        const orderedSteps = toolSteps
            .map((step, index) => {
                const requestedOffset = Number(step.contentOffset);
                const offset = Number.isFinite(requestedOffset)
                    ? Math.max(0, Math.min(Math.floor(requestedOffset), characters.length))
                    : characters.length;
                return { step, offset, index };
            })
            .sort((left, right) => left.offset - right.offset || left.index - right.index);
        let cursor = 0;
        let lastBody = null;
        let stepIndex = 0;

        while (stepIndex < orderedSteps.length) {
            const offset = orderedSteps[stepIndex].offset;
            const precedingText = characters.slice(cursor, offset).join("");
            const hasVisibleText = Boolean(precedingText.trim());
            if (hasVisibleText) {
                lastBody = this.appendTimelineText(
                    timeline,
                    precedingText,
                );
            }
            const previousBlock = timeline.lastElementChild;
            const rail = (
                !hasVisibleText
                && previousBlock?.classList.contains("fl-toolchain-breadcrumb")
            )
                ? previousBlock
                : this.createToolRail();
            while (
                stepIndex < orderedSteps.length
                && orderedSteps[stepIndex].offset === offset
            ) {
                this.addToolStep(rail, orderedSteps[stepIndex].step);
                stepIndex += 1;
            }
            if (rail.parentElement !== timeline) timeline.appendChild(rail);
            cursor = offset;
        }

        const remainingText = characters.slice(cursor).join("");
        if (remainingText.trim()) {
            lastBody = this.appendTimelineText(
                timeline,
                remainingText,
            );
        }
        return lastBody;
    }

    appendTimelineText(timeline, content) {
        if (!String(content || "").trim()) return null;
        const body = this.createMessageContent(content);
        timeline.appendChild(body);
        return body;
    }

    formatTime(value) {
        if (!value) return "just now";
        const timestamp = new Date(value);
        if (Number.isNaN(timestamp.getTime())) return "";
        const elapsed = Date.now() - timestamp.getTime();
        if (elapsed < 60000) return "just now";
        if (elapsed < 3600000) return `${Math.floor(elapsed / 60000)}m ago`;
        return timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    ensureAssistantMessage() {
        if (this.currentAssistant) return this.currentAssistant;
        const message = this.appendMessage("assistant", "");
        message.article.classList.add("streaming");
        message.source = "";
        message.activeBody = message.body;
        message.activeSource = "";
        message.pendingText = "";
        if (message.activeBody) message.activeBody.classList.add("streaming-active");
        message.tools = new Map();
        this.currentAssistant = message;
        return message;
    }

    appendAssistantDelta(message, delta) {
        if (!message.activeBody) {
            message.pendingText += delta;
            if (!message.pendingText.trim()) return;
            message.activeBody = this.appendTimelineText(
                message.timeline,
                message.pendingText,
            );
            message.activeBody.classList.add("streaming-active");
            message.activeSource = message.pendingText;
            message.pendingText = "";
        } else {
            message.activeSource += delta;
        }
        message.activeBody.replaceChildren(renderMarkdown(message.activeSource));
        message.body = message.activeBody;
    }

    finishActiveTextSegment(message, discardEmpty = false) {
        if (discardEmpty && message.activeBody && !message.activeSource) {
            message.activeBody.remove();
        }
        message.activeBody?.classList.remove("streaming-active");
        message.activeBody = null;
        message.activeSource = "";
        message.pendingText = "";
    }

    toolRailAtCursor(message) {
        this.finishActiveTextSegment(message, true);
        const last = message.timeline.lastElementChild;
        if (last?.classList.contains("fl-toolchain-breadcrumb")) return last;
        const rail = this.createToolRail();
        message.timeline.appendChild(rail);
        return rail;
    }

    finishAssistantMessage(message) {
        if (!message) return;
        this.finishActiveTextSegment(message, true);
        message.article.classList.remove("streaming");
    }

    createToolRail() {
        const rail = document.createElement("div");
        rail.className = "fl-toolchain-breadcrumb";
        return rail;
    }

    addToolStep(rail, step) {
        const previousItem = rail.lastElementChild;
        const previousSteps = previousItem?.toolSteps || [];
        if (canStackToolSteps(previousSteps.at(-1), step)) {
            previousSteps.push(step);
            this.renderToolStep(previousItem);
            return previousItem;
        }

        const item = document.createElement("details");
        const summary = document.createElement("summary");
        const icon = document.createElement("i");
        icon.setAttribute("aria-hidden", "true");
        const name = document.createElement("span");
        name.className = "fl-crumb-copy";
        const label = document.createElement("strong");
        label.className = "fl-crumb-label";
        const description = document.createElement("span");
        description.className = "fl-crumb-description";
        name.append(label, description);
        const count = document.createElement("span");
        count.className = "fl-crumb-count";
        count.hidden = true;
        const status = document.createElement("em");
        status.className = "fl-crumb-status";
        summary.append(icon, name, count, status);
        const detail = document.createElement("div");
        detail.className = "fl-tool-technical";
        const detailLabel = document.createElement("span");
        detailLabel.textContent = "Technical details";
        const pre = document.createElement("pre");
        detail.append(detailLabel, pre);
        item.append(summary, detail);
        item.toolSteps = [step];
        rail.appendChild(item);
        this.renderToolStep(item);
        return item;
    }

    renderToolStep(item, step = null) {
        const steps = item.toolSteps?.length ? item.toolSteps : [step].filter(Boolean);
        const stack = toolStackState(steps);
        const representative = stack.step;
        const visualStatus = this.toolVisualStatus(stack.status);
        const tool = getToolConfig(representative.name);
        item.className = `fl-toolchain-crumb ${visualStatus}`;
        item.dataset.toolName = representative.name || "";
        item.dataset.toolCount = String(stack.count);
        const icon = item.querySelector("summary > i");
        icon.className = `${tool.iconClass} fl-crumb-icon`;
        const label = item.querySelector(".fl-crumb-label");
        label.textContent = visualStatus === "loading"
            ? tool.runningLabel
            : summarizeToolStep(representative, tool);
        const description = item.querySelector(".fl-crumb-description");
        description.textContent = visualStatus === "loading"
            ? (tool.description || tool.label || representative.name || "MCP tool")
            : (tool.label || representative.name || "MCP tool");
        const count = item.querySelector(".fl-crumb-count");
        count.hidden = stack.count < 2;
        count.textContent = `×${stack.count}`;
        count.setAttribute(
            "aria-label",
            `${stack.count} consecutive ${tool.label || representative.name || "tool"} calls`,
        );
        const status = item.querySelector(".fl-crumb-status");
        const statusLabels = {
            loading: "Working",
            completed: "Done",
            retried: "Retried",
            failed: "Failed",
            cancelled: "Stopped",
        };
        status.textContent = statusLabels[visualStatus];

        const technicalSections = [];
        for (const [index, currentStep] of steps.entries()) {
            const callSections = [];
            if (currentStep.arguments !== undefined && currentStep.arguments !== "") {
                callSections.push(`Arguments\n${technicalText(currentStep.arguments)}`);
            }
            if (currentStep.result !== undefined && currentStep.result !== "") {
                callSections.push(`Result\n${technicalText(currentStep.result)}`);
            }
            if (callSections.length) {
                technicalSections.push(
                    steps.length > 1
                        ? `Call ${index + 1}\n${callSections.join("\n\n")}`
                        : callSections.join("\n\n"),
                );
            }
        }
        const technical = item.querySelector(".fl-tool-technical");
        technical.hidden = !technicalSections.length;
        technical.querySelector("pre").textContent = technicalText(
            technicalSections.join("\n\n"),
        );
    }

    toolVisualStatus(status) {
        return {
            running: "loading",
            done: "completed",
            finished: "completed",
            retried: "retried",
            failed: "failed",
            cancelled: "cancelled",
        }[status] || "completed";
    }

    handleEvent(event) {
        if (event.type === "RUN_STARTED") {
            this.ensureAssistantMessage();
            this.announce("Ren started working.");
        } else if (event.type === "TEXT_MESSAGE_START") {
            this.ensureAssistantMessage();
        } else if (event.type === "TEXT_MESSAGE_CONTENT") {
            const message = this.ensureAssistantMessage();
            const delta = event.delta || "";
            message.source += delta;
            this.appendAssistantDelta(message, delta);
        } else if (event.type === "TOOL_CALL_START") {
            const message = this.ensureAssistantMessage();
            const id = event.toolCallId || crypto.randomUUID();
            for (const tool of message.tools.values()) {
                if (tool.name === event.toolCallName && tool.status === "running") {
                    this.setToolStatus(tool, "retried");
                    break;
                }
            }
            const item = this.addToolStep(this.toolRailAtCursor(message), {
                name: event.toolCallName,
                status: "running",
            });
            const step = {
                name: event.toolCallName,
                status: "running",
                arguments: "",
            };
            message.tools.set(id, {
                item,
                name: event.toolCallName,
                status: "running",
                arguments: "",
                step,
            });
            this.runStatusText.textContent = getToolConfig(event.toolCallName).runningLabel;
        } else if (event.type === "TOOL_CALL_ARGS") {
            const tool = this.currentAssistant?.tools.get(event.toolCallId);
            if (tool) {
                tool.arguments += event.delta || "";
                tool.step.arguments = tool.arguments;
                this.renderToolStep(tool.item, tool.step);
            }
        } else if (event.type === "TOOL_CALL_RESULT") {
            const tool = this.currentAssistant?.tools.get(event.toolCallId);
            if (tool) this.setToolStatus(tool, "done", event.content);
            this.runStatusText.textContent = "Ren is working…";
        } else if (event.type === "CUSTOM" && event.name === "approval_required") {
            this.renderApproval(event.value);
        } else if (event.type === "CUSTOM" && event.name === "approval_resolved") {
            this.resolveApprovalCard(event.value);
        } else if (event.type === "RUN_ERROR") {
            this.settleOpenTools(event.code === "cancelled" ? "cancelled" : "failed");
            this.finishAssistantMessage(this.currentAssistant);
            if (event.code === "cancelled") {
                this.clearError();
                this.announce("Response stopped.");
            } else {
                this.showRunError(event.message || "The assistant run failed.");
            }
            this.running = false;
            this.updateComposerState();
        } else if (event.type === "RUN_FINISHED") {
            this.settleOpenTools("finished");
            this.finishAssistantMessage(this.currentAssistant);
            this.running = false;
            this.updateComposerState();
            this.announce("Ren finished.");
        }
        this.maybeFollowOutput();
    }

    setToolStatus(tool, status, result = undefined) {
        tool.status = status;
        tool.step.status = status;
        if (result !== undefined) tool.step.result = result;
        this.renderToolStep(tool.item, tool.step);
    }

    settleOpenTools(status) {
        for (const tool of this.currentAssistant?.tools?.values() || []) {
            if (tool.status === "running") this.setToolStatus(tool, status);
        }
    }

    renderApproval(value) {
        const message = this.ensureAssistantMessage();
        const copy = this.approvalCopy(value.toolName, value.arguments);
        const card = document.createElement("section");
        card.className = "fl-approval-card";
        card.dataset.approvalId = value.approvalId;
        const eyebrow = document.createElement("span");
        eyebrow.className = "fl-approval-state";
        const shield = document.createElement("i");
        shield.className = "pi pi-shield";
        shield.setAttribute("aria-hidden", "true");
        eyebrow.append(shield, document.createTextNode("Approval required"));
        const title = document.createElement("strong");
        title.textContent = copy.title;
        const consequence = document.createElement("p");
        consequence.textContent = copy.consequence;
        const details = document.createElement("details");
        details.className = "fl-approval-technical";
        const summary = document.createElement("summary");
        summary.textContent = "Technical details";
        const args = document.createElement("pre");
        args.textContent = technicalText(value.arguments || {});
        details.append(summary, args);
        const actions = document.createElement("div");
        actions.className = "fl-approval-actions";
        const deny = document.createElement("button");
        deny.type = "button";
        deny.className = "fl-secondary-button";
        deny.textContent = "Deny";
        const approve = document.createElement("button");
        approve.type = "button";
        approve.className = "fl-primary-button";
        approve.textContent = "Allow once";
        const alwaysAllow = document.createElement("button");
        alwaysAllow.type = "button";
        alwaysAllow.className = "fl-secondary-button fl-always-allow-button";
        alwaysAllow.textContent = "Always allow";
        deny.addEventListener(
            "click",
            () => this.submitApproval(value.approvalId, "deny"),
        );
        approve.addEventListener(
            "click",
            () => this.submitApproval(value.approvalId, "allow_once"),
        );
        alwaysAllow.addEventListener(
            "click",
            () => this.submitApproval(value.approvalId, "always_allow"),
        );
        actions.append(deny, approve, alwaysAllow);
        card.append(eyebrow, title, consequence, details, actions);
        this.finishActiveTextSegment(message, true);
        message.timeline.appendChild(card);
        this.announce(`${copy.title} Approval required.`);
    }

    approvalCopy(toolName, argumentsValue) {
        const args = argumentsValue || {};
        const nodeIds = args.node_ids || args.nodeIds;
        const nodeCount = Array.isArray(nodeIds) ? nodeIds.length : null;
        const copies = {
            queue_workflow: {
                title: "Run this workflow?",
                consequence: "This will add the current workflow to ComfyUI’s execution queue.",
            },
            remove_nodes: {
                title: nodeCount === null
                    ? "Remove nodes from the canvas?"
                    : `Remove ${nodeCount} ${nodeCount === 1 ? "node" : "nodes"}?`,
                consequence: "The selected nodes and their connections will be removed from the open workflow.",
            },
            workflow_load_json: {
                title: "Replace the open workflow?",
                consequence: "This will load another workflow into the canvas and can replace unsaved changes.",
            },
            workflow_save_current: {
                title: "Save this workflow?",
                consequence: "This will write the current workflow to the requested file.",
            },
            manager_queue_action: {
                title: "Change installed custom nodes?",
                consequence: "ComfyUI Manager will perform the requested install, update, or removal action.",
            },
        };
        if (copies[toolName]) return copies[toolName];
        const tool = getToolConfig(toolName);
        return {
            title: `Allow ${tool.label || toolName || "this action"}?`,
            consequence: "Ren will perform this high-impact action once. Future actions will ask again.",
        };
    }

    async submitApproval(approvalId, decision) {
        const card = this.container.querySelector(
            `.fl-approval-card[data-approval-id="${CSS.escape(approvalId)}"]`,
        );
        card?.querySelectorAll("button").forEach((button) => {
            button.disabled = true;
        });
        try {
            const result = await this.chat.approve(approvalId, decision);
            if (result.resolution === "always_allowed") {
                this.settings = await this.chat.settings();
                this.approvalBypassInput.checked = (
                    this.settings.approval_mode === "bypass_all"
                );
                this.renderApprovalSettings();
            }
        } catch (error) {
            card?.querySelectorAll("button").forEach((button) => {
                button.disabled = false;
            });
            this.showError(`Approval could not be submitted: ${error.message}`);
        }
    }

    resolveApprovalCard(value) {
        const card = this.container.querySelector(
            `.fl-approval-card[data-approval-id="${CSS.escape(value.approvalId)}"]`,
        );
        if (!card) return;
        const resolution = value.resolution || (value.approved ? "approved" : "denied");
        card.classList.add(resolution);
        const labels = {
            approved: "Approved",
            always_allowed: "Always allowed",
            denied: "Denied",
            expired: "Approval expired",
        };
        const state = card.querySelector(".fl-approval-state");
        state.replaceChildren(document.createTextNode(labels[resolution] || "Resolved"));
        card.querySelector(".fl-approval-actions")?.remove();
        this.announce(labels[resolution] || "Approval resolved.");
    }

    async send() {
        const message = this.textarea.value.trim();
        if (!message || this.running) return;
        if (!this.status?.configured) {
            this.openSheet("settings");
            this.showError("Choose and test a model before sending a message.");
            return;
        }
        this.clearError();
        this.lastFailedMessage = message;
        this.running = true;
        this.currentAssistant = null;
        this.followOutput = true;
        this.appendMessage("user", message);
        this.textarea.value = "";
        this.resizeComposer();
        this.updateComposerState();
        try {
            await this.chat.startRun({
                sessionId: this.sessionManager.getSessionId(),
                conversationId: this.conversationId,
                message,
                onReady: ({ conversationId }) => {
                    this.conversationId = conversationId;
                },
                onEvent: (event) => this.handleEvent(event),
            });
        } catch (error) {
            if (error.name !== "AbortError") {
                this.showRunError(error.message);
            }
        } finally {
            this.running = false;
            this.updateComposerState();
            await this.refreshConversations(this.conversationId);
        }
    }

    async stop() {
        if (!this.running) return;
        try {
            await this.chat.cancel();
        } catch (error) {
            this.showError(`Response could not be stopped: ${error.message}`);
        }
    }

    updateComposerState() {
        this.sendButton.disabled = this.running || !this.textarea.value.trim();
        this.sendButton.title = this.running
            ? "Wait for the current response to finish"
            : "Send message (Enter)";
        this.runStatus.hidden = !this.running;
        if (!this.running) this.runStatusText.textContent = "Ren is working…";
        this.textarea.disabled = false;
        if (this.running) {
            this.textarea.setAttribute("aria-describedby", "fl-run-drafting-hint");
        } else {
            this.textarea.removeAttribute("aria-describedby");
        }
    }

    updateStatus(force = null) {
        const state = force || (
            !this.status?.available ? "error"
                : !this.status?.configured ? "setup"
                    : !this.status?.bridgeConnected ? "warning"
                        : "online"
        );
        this.statusState = state;
        const indicatorClass = {
            online: "connected",
            warning: "connecting",
            setup: "connecting",
            error: "disconnected",
        }[state];
        this.statusDot.className = `fl-status-indicator ${indicatorClass}`;
        const labels = {
            online: "Ready",
            warning: "Canvas offline",
            setup: "Setup needed",
            error: "Unavailable",
        };
        this.statusCopy.textContent = labels[state];
        const bannerCopy = {
            warning: "Ren is ready, but the canvas bridge is disconnected.",
            setup: "Choose a model connection before chatting with Ren.",
            error: "Ren is unavailable. Check the backend and bridge connection.",
        };
        const bannerAction = {
            warning: "Diagnostics",
            setup: "Set up",
            error: "Diagnostics",
        };
        this.statusBanner.hidden = state === "online";
        this.statusBannerCopy.textContent = bannerCopy[state] || "";
        this.statusBanner.querySelector("button").textContent = bannerAction[state] || "";
        this.updateDiagnosticsSettingsState(force === "error" ? "error" : null);
        if (this.status?.model) {
            this.modelInput.value = this.status.model;
            this.renderProviderControls();
        }
        this.updateProviderBadge();
        this.refreshCanvasContext();
    }

    addTool(toolName, state = "running") {
        this.diagnostics?.addTool(toolName, state);
    }

    completeTool(toolName, success = true) {
        this.diagnostics?.completeTool(toolName, success);
    }

    updateConnection() {
        this.diagnostics?.updateConnection();
        if (this.status) {
            this.status.bridgeConnected = Boolean(
                this.diagnostics?.wsClient?.connected
                && this.diagnostics?.wsClient?.handshakeComplete
            );
            this.updateStatus();
        }
    }

    showRunError(message) {
        this.showError(`Ren could not finish: ${message}`, {
            retry: Boolean(this.lastFailedMessage),
            settings: true,
        });
    }

    showError(message, options = {}) {
        this.errorCopy.textContent = message;
        this.errorActions.replaceChildren();
        const addAction = (label, handler, className = "") => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `fl-inline-action ${className}`.trim();
            button.textContent = label;
            button.addEventListener("click", handler);
            this.errorActions.appendChild(button);
        };
        if (options.retry) {
            addAction("Retry", () => this.retryLastMessage());
        }
        if (options.settings) {
            addAction("Settings", () => this.openSheet("settings"));
        }
        addAction("Copy", async () => {
            try {
                await navigator.clipboard.writeText(message);
                this.announce("Error details copied.");
            } catch (_) {
                this.announce("Could not copy error details.");
            }
        });
        this.errorElement.hidden = false;
    }

    clearError() {
        this.errorElement.hidden = true;
        this.errorCopy.textContent = "";
        this.errorActions.replaceChildren();
    }

    retryLastMessage() {
        if (!this.lastFailedMessage || this.running) return;
        this.textarea.value = this.lastFailedMessage;
        this.resizeComposer();
        this.updateComposerState();
        this.send();
    }

    resizeComposer() {
        this.textarea.style.height = "auto";
        this.textarea.style.height = `${Math.min(this.textarea.scrollHeight, 140)}px`;
    }

    handleThreadScroll() {
        const nearBottom = isNearBottom(this.scrollElement, 48);
        if (this.jumpingToLatest) {
            this.jumpLatestButton.hidden = true;
            if (nearBottom) this.finishJumpToLatest();
            return;
        }
        this.followOutput = nearBottom;
        this.jumpLatestButton.hidden = this.followOutput;
    }

    maybeFollowOutput(force = false) {
        if (this.jumpingToLatest && !force) {
            this.smoothScrollToLatest();
            return;
        }
        if (force) this.followOutput = true;
        if (!this.followOutput) {
            this.jumpLatestButton.hidden = false;
            return;
        }
        this.jumpLatestButton.hidden = true;
        requestAnimationFrame(() => {
            this.scrollElement.scrollTop = this.scrollElement.scrollHeight;
        });
    }

    jumpToLatest() {
        const reduceMotion = window.matchMedia?.(
            "(prefers-reduced-motion: reduce)",
        ).matches;
        this.followOutput = true;
        this.jumpLatestButton.hidden = true;
        if (reduceMotion) {
            this.scrollElement.scrollTo({
                top: this.scrollElement.scrollHeight,
                behavior: "auto",
            });
        } else {
            this.jumpingToLatest = true;
            this.smoothScrollToLatest();
        }
        this.textarea.focus({ preventScroll: true });
    }

    smoothScrollToLatest() {
        this.scrollElement.scrollTo({
            top: this.scrollElement.scrollHeight,
            behavior: "smooth",
        });
        clearTimeout(this.jumpScrollTimer);
        this.jumpScrollTimer = setTimeout(() => this.finishJumpToLatest(), 800);
    }

    finishJumpToLatest() {
        clearTimeout(this.jumpScrollTimer);
        this.jumpScrollTimer = null;
        this.jumpingToLatest = false;
        this.followOutput = isNearBottom(this.scrollElement, 48);
        this.jumpLatestButton.hidden = this.followOutput;
    }

    scrollToBottom() {
        this.maybeFollowOutput(true);
    }

    destroy() {
        this.chat.detach();
        this.diagnostics?.destroy();
        this.contextUnsubscribe?.();
        this.contextUnsubscribe = null;
        clearTimeout(this.undoTimer);
        clearTimeout(this.jumpScrollTimer);
        document.removeEventListener("pointerdown", this.documentPointerHandler);
        this.container.replaceChildren();
        this.container.classList.remove("fl-chat-panel-host");
    }
}
