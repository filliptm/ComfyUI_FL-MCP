import { ChatClient } from "./chat_client.js";
import {
    isNearBottom,
    modelProviderSummary,
    starterPrompts,
    summarizeToolStep,
    technicalText,
    toolDisplayImages,
    toolHistorySummary,
} from "./chat_ui_helpers.js";
import { renderMarkdown } from "./safe_markdown.js";
import { getToolConfig } from "./tool_activity.js";

const REASONING_LABELS = {
    default: "Model default",
    low: "Low",
    medium: "Medium",
    high: "High",
    xhigh: "Extra high",
    max: "Max",
    ultra: "Ultra",
};

const SEARCH_MODE_OPTIONS = [
    { id: "off", label: "No web", detail: "Do not expose web tools for this message." },
    { id: "free", label: "Free web", detail: "No key or credits · best effort." },
    { id: "tavily_basic", label: "Tavily basic", detail: "Managed search · 1 credit." },
    { id: "tavily_advanced", label: "Tavily deep", detail: "Higher relevance · 2 credits." },
];

const MAX_TOOL_GALLERY_IMAGES = 12;
const TOOL_HISTORY_INITIAL_STEPS = 60;
const MAX_CHAT_ATTACHMENTS = 8;
const MAX_CHAT_ATTACHMENT_BYTES = 32 * 1024 * 1024;
const CHAT_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const WORKFLOW_CONVERSATIONS_KEY = "fl_mcp_workflow_conversations_v1";

export class AssistantPanel {
    constructor(container, sessionManager, options = {}) {
        this.container = container;
        this.sessionManager = sessionManager;
        this.chat = new ChatClient(options.baseUrl || "");
        this.createDiagnostics = options.createDiagnostics;
        this.discardMaskReview = options.discardMaskReview;
        this.getCanvasContext = options.getCanvasContext || (() => ({
            connected: Boolean(this.status?.bridgeConnected),
            nodeCount: 0,
            selectedCount: 0,
        }));
        this.subscribeCanvasContext = options.subscribeCanvasContext;
        this.getWorkflowContext = options.getWorkflowContext || (() => null);
        this.subscribeWorkflowContext = options.subscribeWorkflowContext;
        this.activateWorkflow = options.activateWorkflow;
        this.loadBridgeSettings = options.loadBridgeSettings;
        this.updateBridgeSettings = options.updateBridgeSettings;
        this.uploadChatImage = options.uploadChatImage;
        this.placeChatImageInSelectedNode = options.placeChatImageInSelectedNode;
        this.discardMaskReviews = options.discardMaskReviews;
        this.settings = null;
        this.bridgeSettings = null;
        this.bridgeSettingsError = null;
        this.status = null;
        this.conversations = [];
        this.archivedConversations = [];
        this.historyView = "active";
        this.conversationId = null;
        this.workflowContext = this.getWorkflowContext();
        this.workflowGeneration = 0;
        this.workflowConversationIds = this.loadWorkflowConversationIds();
        this.workflowDrafts = new Map();
        this.conversationScopeMismatch = false;
        this.running = false;
        this.stopping = false;
        this.steering = false;
        this.activeRunPromise = null;
        this.initializing = false;
        this.currentAssistant = null;
        this.currentRunContext = null;
        this.availableModels = [];
        this.pendingSubscriptionModel = null;
        this.diagnostics = null;
        this.backendRunning = null;
        this.backendError = "";
        this.canvasContext = { connected: false, nodeCount: 0, selectedCount: 0 };
        this.followOutput = true;
        this.jumpingToLatest = false;
        this.jumpScrollTimer = null;
        this.followFrame = null;
        this.threadResizeObserver = null;
        this.activeSheet = null;
        this.sheetReturnFocus = null;
        this.undoTimer = null;
        this.lastFailedMessage = "";
        this.lastFailedEditMessageId = null;
        this.lastFailedSearchMode = null;
        this.lastFailedAttachments = [];
        this.pendingAttachments = [];
        this.uploadingAttachments = false;
        this.composerDragDepth = 0;
        this.lastArchivedConversation = null;
        this.pendingDeleteConversationId = null;
        this.contextUnsubscribe = null;
        this.workflowContextUnsubscribe = null;
        this.olderMessagesCursor = null;
        this.hasOlderMessages = false;
        this.loadingOlderMessages = false;
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
                            <button class="fl-provider-badge" data-action="settings" data-section="model" data-provider="unknown" type="button" title="Open settings" aria-label="Open settings">
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
                        <span class="fl-run-status-copy"><i class="pi pi-spin pi-spinner fl-run-status-icon" aria-hidden="true"></i><span>Ren is working…</span></span>
                        <button class="fl-inline-action danger" data-action="stop" type="button">Stop</button>
                    </div>

                    <footer class="fl-chat-input-container">
                        <div class="fl-composer-toolbar">
                            <div class="fl-canvas-context">
                                <i class="pi pi-sitemap" aria-hidden="true"></i>
                                <span>Checking canvas…</span>
                            </div>
                            <div class="fl-composer-controls">
                                <div class="fl-composer-actions">
                                    <label class="fl-search-action-control" title="Web search for the next message">
                                        <i class="pi pi-globe" aria-hidden="true"></i>
                                        <span class="fl-sr-only">Web search</span>
                                        <select data-search="composer" aria-label="Web search for the next message"></select>
                                    </label>
                                </div>
                                <label class="fl-reasoning-control" title="Reasoning level for the next message">
                                    <i class="pi pi-sparkles" aria-hidden="true"></i>
                                    <span class="fl-sr-only">Reasoning level</span>
                                    <select data-reasoning="composer" aria-label="Reasoning level for the next message"></select>
                                </label>
                            </div>
                        </div>
                        <div class="fl-composer-attachments" hidden></div>
                        <div class="fl-composer-row">
                            <button class="fl-chat-attach" data-action="attach-images" type="button" title="Add images" aria-label="Add images">
                                <i class="pi pi-paperclip" aria-hidden="true"></i>
                            </button>
                            <input class="fl-chat-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden>
                            <textarea class="fl-chat-input" rows="3" placeholder="Ask Ren about this workflow…" aria-label="Message"></textarea>
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
                        <details class="fl-settings-card fl-settings-disclosure fl-settings-card-model" data-settings-section="model" open>
                            <summary class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-sliders-h"></i></span>
                                    <div>
                                        <h3 id="fl-settings-model-title">Connection</h3>
                                        <p>Choose how Ren thinks and connects.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-summary-state">
                                    <span class="fl-settings-state neutral" data-settings-state="model" role="status">Checking</span>
                                    <i class="pi pi-chevron-down fl-settings-chevron" aria-hidden="true"></i>
                                </span>
                            </summary>
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
                                    <label class="fl-field">
                                        <span>Default reasoning level</span>
                                        <select class="fl-provider-input" data-setting="reasoning_effort"></select>
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
                        </details>

                        <details class="fl-settings-card fl-settings-disclosure fl-settings-card-search" data-settings-section="search">
                            <summary class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-globe"></i></span>
                                    <div>
                                        <h3 id="fl-settings-search-title">Web search</h3>
                                        <p>Choose free search or Tavily for each request.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-summary-state">
                                    <span class="fl-settings-state neutral" data-settings-state="search" role="status">Checking</span>
                                    <i class="pi pi-chevron-down fl-settings-chevron" aria-hidden="true"></i>
                                </span>
                            </summary>
                            <div class="fl-settings-card-body">
                                <label class="fl-field">
                                    <span>Default search</span>
                                    <select class="fl-provider-input" data-setting="search_mode"></select>
                                    <small class="fl-search-mode-detail"></small>
                                </label>
                                <label class="fl-settings-toggle fl-search-actions-toggle">
                                    <input data-setting="show_action_buttons" type="checkbox">
                                    <span>
                                        <strong>Show composer action buttons</strong>
                                        <span>Turn off the web-search action selector for a cleaner composer. Ren will keep using the default search above.</span>
                                    </span>
                                </label>
                                <label class="fl-field fl-tavily-credential-field">
                                    <span>Tavily API key <em>optional</em></span>
                                    <input class="fl-provider-input" data-setting="tavily_credential" type="password" autocomplete="off" placeholder="Stored in your OS keychain">
                                </label>
                            </div>
                            <footer class="fl-settings-card-footer">
                                <div class="fl-search-credential-status" role="status" aria-live="polite"></div>
                                <button class="fl-primary-button" data-action="save-search-settings" type="button">Save search</button>
                            </footer>
                        </details>

                        <details class="fl-settings-card fl-settings-disclosure fl-settings-card-approvals" data-settings-section="approvals">
                            <summary class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-shield"></i></span>
                                    <div>
                                        <h3 id="fl-settings-approvals-title">Permissions</h3>
                                        <p>Control when Ren asks before acting.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-summary-state">
                                    <span class="fl-settings-state neutral" data-settings-state="approvals" role="status">Prompts on</span>
                                    <i class="pi pi-chevron-down fl-settings-chevron" aria-hidden="true"></i>
                                </span>
                            </summary>
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
                        </details>

                        <div class="fl-settings-group-label">Advanced</div>

                        <details class="fl-settings-card fl-settings-disclosure fl-settings-card-bridge" data-settings-section="bridge">
                            <summary class="fl-settings-card-header">
                                <div class="fl-settings-card-heading">
                                    <span class="fl-settings-card-icon" aria-hidden="true"><i class="pi pi-server"></i></span>
                                    <div>
                                        <h3>Bridge &amp; safety</h3>
                                        <p>Backend launch, local endpoints, paths, and server-side capabilities.</p>
                                    </div>
                                </div>
                                <span class="fl-settings-summary-state">
                                    <span class="fl-settings-state neutral" data-settings-state="bridge" role="status">Loading</span>
                                    <i class="pi pi-chevron-down fl-settings-chevron" aria-hidden="true"></i>
                                </span>
                            </summary>
                            <div class="fl-settings-card-body fl-bridge-settings-body">
                                <fieldset class="fl-bridge-settings-group">
                                    <legend>Backend</legend>
                                    <div class="fl-bridge-settings-grid">
                                        <label class="fl-field">
                                            <span>Launch mode</span>
                                            <select data-bridge-setting="backend_launch_mode">
                                                <option value="subprocess">Subprocess</option>
                                                <option value="terminal">Terminal</option>
                                                <option value="auto">Auto</option>
                                                <option value="manual">Manual</option>
                                            </select>
                                        </label>
                                        <label class="fl-field">
                                            <span>Log level</span>
                                            <select data-bridge-setting="log_level">
                                                <option value="DEBUG">Debug</option>
                                                <option value="INFO">Info</option>
                                                <option value="WARNING">Warning</option>
                                                <option value="ERROR">Error</option>
                                            </select>
                                        </label>
                                        <label class="fl-field">
                                            <span>Generation wait timeout (seconds)</span>
                                            <input data-bridge-setting="generation_completion_timeout" type="number" min="1" max="3600" step="1">
                                        </label>
                                    </div>
                                    <div class="fl-bridge-toggle-grid">
                                        <label class="fl-bridge-toggle">
                                            <input data-bridge-setting="auto_start_backend" type="checkbox">
                                            <span>Start backend with ComfyUI</span>
                                        </label>
                                        <label class="fl-bridge-toggle">
                                            <input data-bridge-setting="auto_restart_backend" type="checkbox">
                                            <span>Restart backend after failure</span>
                                        </label>
                                        <label class="fl-bridge-toggle">
                                            <input data-bridge-setting="log_backend_to_file" type="checkbox">
                                            <span>Write backend log file</span>
                                        </label>
                                        <label class="fl-bridge-toggle">
                                            <input data-bridge-setting="wait_for_generation_completion" type="checkbox">
                                            <span>Wait for generation completion by default</span>
                                        </label>
                                    </div>
                                </fieldset>

                                <fieldset class="fl-bridge-settings-group">
                                    <legend>Local endpoints</legend>
                                    <div class="fl-bridge-settings-grid">
                                        <label class="fl-field">
                                            <span>Bind host</span>
                                            <input data-bridge-setting="ws_host" type="text" spellcheck="false">
                                        </label>
                                        <label class="fl-field">
                                            <span>Bridge port</span>
                                            <input data-bridge-setting="ws_port" type="number" min="1" max="65535" step="1">
                                        </label>
                                        <label class="fl-field fl-bridge-wide-field">
                                            <span>Public bridge URL</span>
                                            <input data-bridge-setting="public_url" type="url" spellcheck="false" placeholder="http://127.0.0.1:8000">
                                        </label>
                                        <label class="fl-field fl-bridge-wide-field">
                                            <span>ComfyUI server URL</span>
                                            <input data-bridge-setting="comfyui_server_url" type="url" spellcheck="false">
                                        </label>
                                        <label class="fl-field">
                                            <span>API timeout (seconds)</span>
                                            <input data-bridge-setting="comfyui_api_timeout" type="number" min="1" max="300" step="1">
                                        </label>
                                    </div>
                                </fieldset>

                                <fieldset class="fl-bridge-settings-group">
                                    <legend>Paths</legend>
                                    <div class="fl-bridge-settings-grid">
                                        <label class="fl-field fl-bridge-wide-field">
                                            <span>ComfyUI path <small>Optional</small></span>
                                            <input data-bridge-setting="comfyui_path" type="text" spellcheck="false" placeholder="Auto-detect">
                                        </label>
                                        <label class="fl-field fl-bridge-wide-field">
                                            <span>Extra model paths file <small>Optional</small></span>
                                            <input data-bridge-setting="extra_model_paths_path" type="text" spellcheck="false" placeholder="Auto-detect extra_model_paths.yaml">
                                        </label>
                                    </div>
                                </fieldset>

                                <fieldset class="fl-bridge-settings-group">
                                    <legend>Server-side capabilities</legend>
                                    <p class="fl-bridge-settings-help">These gates remain authoritative even when chat approval prompts are bypassed.</p>
                                    <div class="fl-bridge-capability-list">
                                        <label class="fl-approval-toggle">
                                            <input data-bridge-setting="enable_workflow_writes" type="checkbox">
                                            <span><strong>Workflow writes</strong><span>Enabled by default. Edit the canvas and manage workflows, history, and ComfyUI settings.</span></span>
                                        </label>
                                        <label class="fl-approval-toggle">
                                            <input data-bridge-setting="enable_custom_node_writes" type="checkbox">
                                            <span><strong>Custom node writes</strong><span>Write files, apply patches, and create custom node packs.</span></span>
                                        </label>
                                        <label class="fl-approval-toggle">
                                            <input data-bridge-setting="enable_git_writes" type="checkbox">
                                            <span><strong>Git writes</strong><span>Commit and push repositories under custom_nodes.</span></span>
                                        </label>
                                        <label class="fl-approval-toggle">
                                            <input data-bridge-setting="enable_manager_mutations" type="checkbox">
                                            <span><strong>Manager mutations</strong><span>Install, update, or remove packs through ComfyUI Manager.</span></span>
                                        </label>
                                        <label class="fl-approval-toggle">
                                            <input data-bridge-setting="enable_comfy_process_control" type="checkbox">
                                            <span><strong>Process control</strong><span>Start, stop, or restart FL-MCP-managed ComfyUI processes.</span></span>
                                        </label>
                                    </div>
                                </fieldset>
                            </div>
                            <footer class="fl-settings-card-footer">
                                <span class="fl-bridge-settings-message" role="status" aria-live="polite">Changes take effect after restarting ComfyUI.</span>
                                <button class="fl-primary-button" data-action="save-bridge-settings" type="button">Save bridge settings</button>
                            </footer>
                        </details>

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
        this.threadResizeObserver = new ResizeObserver(() => this.handleThreadResize());
        this.threadResizeObserver.observe(this.scrollElement);
        this.threadResizeObserver.observe(this.messagesElement);
        this.welcomeElement = this.container.querySelector(".ren-welcome");
        this.errorElement = this.container.querySelector(".fl-chat-error");
        this.errorCopy = this.container.querySelector(".fl-chat-error-copy");
        this.errorActions = this.container.querySelector(".fl-chat-error-actions");
        this.textarea = this.container.querySelector(".fl-chat-input");
        this.composerContainer = this.container.querySelector(".fl-chat-input-container");
        this.attachmentInput = this.container.querySelector(".fl-chat-file-input");
        this.attachmentTray = this.container.querySelector(".fl-composer-attachments");
        this.sendButton = this.container.querySelector('[data-action="send"]');
        this.runStatus = this.container.querySelector(".fl-run-status");
        this.runStatusIcon = this.runStatus.querySelector(".fl-run-status-icon");
        this.runStatusText = this.runStatus.querySelector("span span");
        this.stopButton = this.runStatus.querySelector('[data-action="stop"]');
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
        this.settingsReasoningSelect = this.container.querySelector(
            '[data-setting="reasoning_effort"]',
        );
        this.composerReasoningSelect = this.container.querySelector(
            '[data-reasoning="composer"]',
        );
        this.composerActions = this.container.querySelector(".fl-composer-actions");
        this.composerSearchSelect = this.container.querySelector(
            '[data-search="composer"]',
        );
        this.settingsSearchSelect = this.container.querySelector(
            '[data-setting="search_mode"]',
        );
        this.searchModeDetail = this.container.querySelector(".fl-search-mode-detail");
        this.showActionButtonsInput = this.container.querySelector(
            '[data-setting="show_action_buttons"]',
        );
        this.tavilyCredentialInput = this.container.querySelector(
            '[data-setting="tavily_credential"]',
        );
        this.searchCredentialStatus = this.container.querySelector(
            ".fl-search-credential-status",
        );
        this.searchSettingsState = this.container.querySelector(
            '[data-settings-state="search"]',
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
        this.bridgeSettingsState = this.container.querySelector(
            '[data-settings-state="bridge"]',
        );
        this.bridgeSettingsInputs = [
            ...this.container.querySelectorAll("[data-bridge-setting]"),
        ];
        this.bridgeSettingsMessage = this.container.querySelector(
            ".fl-bridge-settings-message",
        );
        this.bridgeSettingsSaveButton = this.container.querySelector(
            '[data-action="save-bridge-settings"]',
        );
        this.settingsDisclosures = [
            ...this.container.querySelectorAll(".fl-settings-disclosure"),
        ];
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
                    onBackendStatus: (status) => this.refreshBackendStatus(status),
                },
            );
        }
        this.contextUnsubscribe = this.subscribeCanvasContext?.(
            () => this.refreshCanvasContext(),
        ) || null;
        this.workflowContextUnsubscribe = this.subscribeWorkflowContext?.(
            () => this.refreshWorkflowContext(),
        ) || null;
    }

    bind() {
        this.container.addEventListener("click", (event) => {
            const actionElement = event.target.closest("[data-action]");
            if (!actionElement) return;
            const action = actionElement.dataset.action;
            if (action === "toggle-menu") this.toggleMenu(actionElement);
            if (action === "settings") {
                this.openSheet("settings", actionElement.dataset.section || null);
            }
            if (action === "diagnostics") this.openSheet("settings", "diagnostics");
            if (action === "history") this.openSheet("history");
            if (action === "close-sheet") this.closeSheet();
            if (action === "new-chat") this.newConversation();
            if (action === "send") this.send();
            if (action === "attach-images") this.attachmentInput.click();
            if (action === "remove-pending-attachment") {
                this.removePendingAttachment(Number(actionElement.dataset.attachmentIndex));
            }
            if (action === "use-pending-attachment") {
                this.usePendingAttachment(Number(actionElement.dataset.attachmentIndex));
            }
            if (action === "use-message-attachment") {
                const article = actionElement.closest(".fl-message.user");
                this.useAttachment(
                    article?.messageAttachments?.[
                        Number(actionElement.dataset.attachmentIndex)
                    ],
                );
            }
            if (action === "attach-tool-image") {
                this.attachToolImage(actionElement.toolImage);
            }
            if (action === "use-tool-image") {
                this.useToolImage(actionElement.toolImage);
            }
            if (action === "toggle-tool-history") {
                this.toggleToolHistory(actionElement.closest(".fl-toolchain-breadcrumb"));
            }
            if (action === "load-tool-history") {
                this.loadMoreToolHistory(actionElement.closest(".fl-toolchain-breadcrumb"));
            }
            if (action === "load-older-messages") this.loadOlderMessages();
            if (action === "stop") this.stop();
            if (action === "jump-latest") this.jumpToLatest();
            if (action === "status-action") this.handleStatusAction();
            if (action === "discover-models") this.discoverModels();
            if (action === "save-settings") this.saveSettings();
            if (action === "save-search-settings") this.saveSearchSettings();
            if (action === "save-bridge-settings") this.saveBridgeSettings();
            if (action === "claude-login") this.connectClaudeSubscription();
            if (action === "codex-login") this.connectCodexSubscription();
            if (action === "clear-always-allowed") this.clearAlwaysAllowedTools();
            if (action === "history-view") this.setHistoryView(actionElement.dataset.view);
            if (action === "select-conversation") this.selectConversation(actionElement.dataset.conversationId);
            if (action === "attach-conversation") this.attachConversation(actionElement.dataset.conversationId);
            if (action === "activate-conversation-workflow") this.activateConversationWorkflow(actionElement.dataset.conversationId);
            if (action === "rename-conversation") this.renameConversation(actionElement.dataset.conversationId);
            if (action === "save-rename") this.saveConversationRename(actionElement.dataset.conversationId);
            if (action === "cancel-rename") this.renderHistory();
            if (action === "archive-conversation") this.archiveConversation(actionElement.dataset.conversationId);
            if (action === "restore-conversation") this.restoreConversation(actionElement.dataset.conversationId);
            if (action === "delete-conversation") this.openDeleteConfirmation(actionElement.dataset.conversationId);
            if (action === "cancel-confirm") this.closeDeleteConfirmation();
            if (action === "confirm-delete") this.confirmPermanentDelete();
            if (action === "undo-archive") this.undoArchive();
            if (action === "edit-message") this.startMessageEdit(actionElement.dataset.messageId);
            if (action === "cancel-message-edit") this.cancelMessageEdit(actionElement.dataset.messageId);
            if (action === "submit-message-edit") this.submitMessageEdit(actionElement.dataset.messageId);
            if (action === "resend-message") this.resendMessage(actionElement.dataset.messageId);
            if (action === "previous-message-version") this.changeMessageVersion(actionElement.dataset.messageId, -1);
            if (action === "next-message-version") this.changeMessageVersion(actionElement.dataset.messageId, 1);
        });
        this.providerSelect.addEventListener("change", () => this.applyProviderPreset());
        this.subscriptionModelSelect.addEventListener(
            "change",
            () => this.selectSubscriptionModel(),
        );
        this.modelInput.addEventListener("input", () => {
            this.renderReasoningControls();
            this.updateModelSettingsState();
        });
        this.approvalBypassInput.addEventListener(
            "change",
            () => this.setApprovalBypass(),
        );
        this.bridgeSettingsInputs.forEach((input) => {
            input.addEventListener("change", () => {
                this.setSettingsState(this.bridgeSettingsState, "Unsaved", "warning");
                this.bridgeSettingsMessage.textContent =
                    "Save these changes, then restart ComfyUI to apply them.";
            });
        });
        this.settingsDisclosures.forEach((disclosure) => {
            disclosure.addEventListener(
                "toggle",
                () => this.handleSettingsDisclosureToggle(disclosure),
            );
        });
        this.settingsSearchSelect.addEventListener(
            "change",
            () => this.renderSearchSettings(),
        );
        this.showActionButtonsInput.addEventListener(
            "change",
            () => this.setComposerActionsVisibility(),
        );
        this.textarea.addEventListener("keydown", (event) => {
            if (
                event.key === "Enter"
                && !event.shiftKey
                && !event.isComposing
            ) {
                event.preventDefault();
                this.send();
            }
        });
        this.textarea.addEventListener("input", () => this.resizeComposer());
        this.textarea.addEventListener("input", () => this.updateComposerState());
        this.textarea.addEventListener("focus", () => this.refreshCanvasContext());
        this.textarea.addEventListener("paste", (event) => this.handleImagePaste(event));
        this.attachmentInput.addEventListener("change", () => {
            this.addImageFiles(this.attachmentInput.files);
            this.attachmentInput.value = "";
        });
        this.composerContainer.addEventListener("dragenter", (event) => {
            if (!this.dragHasFiles(event)) return;
            event.preventDefault();
            this.composerDragDepth += 1;
            this.composerContainer.classList.add("drag-active");
        });
        this.composerContainer.addEventListener("dragover", (event) => {
            if (!this.dragHasFiles(event)) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
        });
        this.composerContainer.addEventListener("dragleave", () => {
            this.composerDragDepth = Math.max(0, this.composerDragDepth - 1);
            if (this.composerDragDepth === 0) {
                this.composerContainer.classList.remove("drag-active");
            }
        });
        this.composerContainer.addEventListener("drop", (event) => {
            if (!this.dragHasFiles(event)) return;
            event.preventDefault();
            this.composerDragDepth = 0;
            this.composerContainer.classList.remove("drag-active");
            this.addImageFiles(event.dataTransfer.files);
        });
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
        if (name === "settings" && section) this.openSettingsSection(section);
        requestAnimationFrame(() => {
            if (section) {
                const target = sheet.querySelector(
                    `[data-settings-section="${CSS.escape(section)}"]`,
                );
                target?.scrollIntoView({ block: "start" });
                target?.querySelector("summary")?.focus({ preventScroll: true });
                return;
            }
            this.focusableElements(sheet)[0]?.focus();
        });
    }

    openSettingsSection(section) {
        const target = this.settingsDisclosures.find(
            item => item.dataset.settingsSection === section,
        );
        if (!target) return null;
        this.settingsDisclosures.forEach((item) => {
            item.open = item === target;
        });
        return target;
    }

    handleSettingsDisclosureToggle(disclosure) {
        if (!disclosure.open) return;
        this.settingsDisclosures.forEach((item) => {
            if (item !== disclosure) item.open = false;
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
        )].filter(element => (
            !element.hidden
            && !element.closest("[hidden]")
            && (!element.closest("details:not([open])") || element.matches("summary"))
        ));
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
        if (this.statusState === "setup") {
            this.openSheet("settings", "model");
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
            const bridgeSettingsRequest = this.loadBridgeSettings
                ? this.loadBridgeSettings().catch((error) => {
                    this.bridgeSettingsError = error;
                    return null;
                })
                : Promise.resolve(null);
            [this.settings, this.status, this.bridgeSettings] = await Promise.all([
                this.chat.settings(),
                this.chat.status(sessionId),
                bridgeSettingsRequest,
            ]);
            this.populateSettings();
            this.populateBridgeSettings();
            await this.adoptLatestLegacyConversation();
            await this.refreshConversations();
            this.updateStatus();
            this.updateComposerState();
            if (!this.status.configured) this.openSheet("settings", "model");
        } catch (error) {
            this.showError(`Assistant setup could not load: ${error.message}`);
            this.updateStatus("error");
        } finally {
            this.initializing = false;
        }
    }

    async refreshBackendStatus(status) {
        const launcherStatus = status && typeof status === "object"
            ? status
            : { backendReachable: Boolean(status) };
        this.backendRunning = Boolean(launcherStatus.backendReachable);
        this.backendError = String(launcherStatus.error || "").trim();
        this.updateDiagnosticsSettingsState();
        if (this.initializing) return;
        if (!this.backendRunning) {
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
        this.renderReasoningControls({ resetComposer: true });
        this.populateSearchControls({ resetComposer: true });
        this.updateCredentialField();
        this.updateProviderBadge();
        this.renderSearchSettings();
        this.renderApprovalSettings();
    }

    populateBridgeSettings() {
        if (!this.bridgeSettings) {
            this.bridgeSettingsInputs.forEach((input) => {
                input.disabled = true;
            });
            this.bridgeSettingsSaveButton.disabled = true;
            this.setSettingsState(this.bridgeSettingsState, "Unavailable", "error");
            this.bridgeSettingsMessage.textContent = this.bridgeSettingsError?.message
                || "Bridge settings are unavailable.";
            return;
        }

        const values = this.bridgeSettings.stored || this.bridgeSettings.defaults || {};
        for (const input of this.bridgeSettingsInputs) {
            const value = values[input.dataset.bridgeSetting];
            input.disabled = false;
            if (input.type === "checkbox") {
                input.checked = Boolean(value);
            } else {
                input.value = value ?? "";
            }
        }
        this.bridgeSettingsSaveButton.disabled = false;
        this.updateBridgeSettingsState();
    }

    updateBridgeSettingsState() {
        const pending = this.bridgeSettings?.pendingRestartFields || [];
        if (pending.length) {
            this.setSettingsState(this.bridgeSettingsState, "Restart needed", "warning");
            this.bridgeSettingsMessage.textContent =
                `${pending.length} saved ${pending.length === 1 ? "change is" : "changes are"} waiting for a ComfyUI restart.`;
        } else {
            this.setSettingsState(this.bridgeSettingsState, "Current", "ready");
            this.bridgeSettingsMessage.textContent = this.bridgeSettings?.migratedFromEnv
                ? "Legacy .env values were imported. Future changes are managed here."
                : "Changes take effect after restarting ComfyUI.";
        }
    }

    bridgeSettingsChanges() {
        return Object.fromEntries(this.bridgeSettingsInputs.map((input) => {
            const name = input.dataset.bridgeSetting;
            if (input.type === "checkbox") return [name, input.checked];
            if (input.type === "number") return [name, Number(input.value)];
            if (["comfyui_path", "extra_model_paths_path"].includes(name)) {
                return [name, input.value.trim() || null];
            }
            return [name, input.value.trim()];
        }));
    }

    async saveBridgeSettings() {
        if (!this.updateBridgeSettings || !this.bridgeSettings) return;
        const button = this.bridgeSettingsSaveButton;
        button.disabled = true;
        button.textContent = "Saving…";
        try {
            this.bridgeSettings = await this.updateBridgeSettings(
                this.bridgeSettingsChanges(),
            );
            this.populateBridgeSettings();
        } catch (error) {
            this.setSettingsState(this.bridgeSettingsState, "Save failed", "error");
            this.bridgeSettingsMessage.textContent = error.message;
        } finally {
            button.disabled = false;
            button.textContent = "Save bridge settings";
        }
    }

    populateSearchControls({ resetComposer = false } = {}) {
        const selectedComposerMode = resetComposer
            ? (this.settings?.search_mode || "free")
            : (this.composerSearchSelect.value || this.settings?.search_mode || "free");
        for (const select of [this.settingsSearchSelect, this.composerSearchSelect]) {
            select.replaceChildren();
            for (const mode of SEARCH_MODE_OPTIONS) {
                const option = document.createElement("option");
                option.value = mode.id;
                option.textContent = mode.label;
                option.title = mode.detail;
                select.appendChild(option);
            }
        }
        this.settingsSearchSelect.value = this.settings?.search_mode || "free";
        this.composerSearchSelect.value = selectedComposerMode;
        this.showActionButtonsInput.checked = this.settings?.show_action_buttons !== false;
        this.composerActions.hidden = !this.showActionButtonsInput.checked;
    }

    renderSearchSettings() {
        const mode = SEARCH_MODE_OPTIONS.find(
            item => item.id === this.settingsSearchSelect.value,
        ) || SEARCH_MODE_OPTIONS[1];
        const tavilySelected = mode.id.startsWith("tavily_");
        const configured = Boolean(this.settings?.searchCredential?.configured);
        this.searchModeDetail.textContent = mode.detail;
        this.searchCredentialStatus.classList.toggle(
            "error",
            tavilySelected && !configured,
        );
        this.searchCredentialStatus.textContent = configured
            ? `Tavily credential ready · ${this.settings.searchCredential.source}`
            : "Tavily is optional · Free web needs no API key";
        if (mode.id === "off") {
            this.setSettingsState(this.searchSettingsState, "Off", "neutral");
        } else if (mode.id === "free") {
            this.setSettingsState(this.searchSettingsState, "Free · no cost", "ready");
        } else if (configured) {
            this.setSettingsState(this.searchSettingsState, "Tavily ready", "ready");
        } else {
            this.setSettingsState(this.searchSettingsState, "API key needed", "warning");
        }
    }

    async setComposerActionsVisibility() {
        const previous = this.settings?.show_action_buttons !== false;
        const visible = this.showActionButtonsInput.checked;
        this.showActionButtonsInput.disabled = true;
        this.composerActions.hidden = !visible;
        try {
            this.settings = await this.chat.updateSettings({
                show_action_buttons: visible,
            });
            this.announce(visible
                ? "Composer action buttons shown."
                : "Composer action buttons hidden. Default search remains active.");
        } catch (error) {
            this.showActionButtonsInput.checked = previous;
            this.composerActions.hidden = !previous;
            this.showError(`Action buttons could not be changed: ${error.message}`);
        } finally {
            this.showActionButtonsInput.disabled = false;
        }
    }

    async saveSearchSettings() {
        const button = this.container.querySelector('[data-action="save-search-settings"]');
        button.disabled = true;
        button.textContent = "Saving…";
        this.searchCredentialStatus.classList.remove("error");
        try {
            if (this.tavilyCredentialInput.value.trim()) {
                await this.chat.setCredential("tavily", this.tavilyCredentialInput.value);
                this.tavilyCredentialInput.value = "";
            }
            this.settings = await this.chat.updateSettings({
                search_mode: this.settingsSearchSelect.value,
                show_action_buttons: this.showActionButtonsInput.checked,
            });
            this.populateSearchControls({ resetComposer: true });
            this.renderSearchSettings();
            this.announce("Web search settings saved.");
        } catch (error) {
            this.searchCredentialStatus.textContent = error.message;
            this.searchCredentialStatus.classList.add("error");
        } finally {
            button.disabled = false;
            button.textContent = "Save search";
        }
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
                    reasoning_effort: this.settingsReasoningSelect.value,
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
                reasoning_effort: this.settingsReasoningSelect.value,
            });
            this.status = await this.chat.status(this.sessionManager.getSessionId());
            this.updateCredentialField();
            this.updateStatus();
        } catch (error) {
            this.showError(`Provider could not be selected: ${error.message}`);
        }
    }

    async selectSubscriptionModel() {
        const selectedModel = this.subscriptionModelSelect.value;
        if (!selectedModel) return;
        const previousModel = this.settings?.model || this.modelInput.value || "";
        this.pendingSubscriptionModel = selectedModel;
        this.modelInput.value = selectedModel;
        if (this.settings) this.settings = { ...this.settings, model: selectedModel };
        if (this.status) this.status = { ...this.status, model: selectedModel };
        this.renderReasoningControls();
        this.updateModelSettingsState();
        this.subscriptionModelSelect.disabled = true;
        try {
            this.settings = await this.chat.updateSettings({ model: selectedModel });
            if (this.status) this.status = { ...this.status, model: selectedModel };
            this.pendingSubscriptionModel = null;
            this.updateStatus();
            this.announce(`Model changed to ${selectedModel}.`);
        } catch (error) {
            this.pendingSubscriptionModel = null;
            this.modelInput.value = previousModel;
            if (this.settings) this.settings = { ...this.settings, model: previousModel };
            if (this.status) this.status = { ...this.status, model: previousModel };
            this.renderProviderControls();
            this.showError(`Model could not be changed: ${error.message}`);
        } finally {
            this.subscriptionModelSelect.disabled = false;
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
        this.renderReasoningControls();
    }

    supportedReasoningEfforts() {
        const preset = this.settings?.presets?.[this.providerSelect.value];
        const modelId = this.modelInput.value || this.settings?.model || "";
        const model = this.availableModels.find((item) => item.id === modelId);
        if (Array.isArray(model?.reasoningEfforts) && model.reasoningEfforts.length) {
            return model.reasoningEfforts;
        }
        if (preset?.type === "codex_cli") {
            return ["low", "medium", "high", "xhigh", "max", "ultra"];
        }
        if (preset?.type === "claude_cli" || preset?.type === "anthropic") {
            return ["low", "medium", "high", "xhigh", "max"];
        }
        return ["low", "medium", "high"];
    }

    populateReasoningSelect(select, value, efforts) {
        select.replaceChildren();
        for (const effort of ["default", ...efforts]) {
            const option = document.createElement("option");
            option.value = effort;
            option.textContent = REASONING_LABELS[effort] || effort;
            select.appendChild(option);
        }
        select.value = efforts.includes(value) ? value : "default";
    }

    renderReasoningControls({ resetComposer = false } = {}) {
        if (!this.settingsReasoningSelect || !this.composerReasoningSelect) return;
        const efforts = this.supportedReasoningEfforts();
        const saved = this.settings?.reasoning_effort || "default";
        const composer = resetComposer
            ? saved
            : (this.composerReasoningSelect.value || saved);
        this.populateReasoningSelect(this.settingsReasoningSelect, saved, efforts);
        this.populateReasoningSelect(this.composerReasoningSelect, composer, efforts);
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
            const connectionMessage = connection.message
                || (isClaudeSubscription ? "Checking Claude Code…" : "Checking Codex…");
            this.credentialStatus.textContent = connection.executablePath
                ? `${connectionMessage} · CLI: ${connection.executablePath}`
                : connectionMessage;
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
                reasoning_effort: this.settingsReasoningSelect.value,
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

    loadWorkflowConversationIds() {
        try {
            const value = JSON.parse(localStorage.getItem(WORKFLOW_CONVERSATIONS_KEY) || "{}");
            return value && typeof value === "object" ? value : {};
        } catch (_) {
            return {};
        }
    }

    saveWorkflowConversationIds() {
        try {
            localStorage.setItem(
                WORKFLOW_CONVERSATIONS_KEY,
                JSON.stringify(this.workflowConversationIds),
            );
        } catch (_) {
            // The latest workflow conversation still falls back to backend ordering.
        }
    }

    rememberWorkflowConversation(conversationId, workflowId = this.workflowContext?.id) {
        if (!workflowId) return;
        if (conversationId) {
            this.workflowConversationIds[workflowId] = conversationId;
        } else {
            delete this.workflowConversationIds[workflowId];
        }
        this.saveWorkflowConversationIds();
    }

    async adoptLatestLegacyConversation() {
        if (!this.workflowContext?.id) return;
        const result = await this.chat.listConversations("active");
        const conversations = result.conversations || [];
        const preferredId = this.workflowConversationIds[this.workflowContext.id];
        const preferred = conversations.find(
            item => item.id === preferredId && !item.workflow,
        );
        if (preferred) {
            await this.chat.updateConversation(preferred.id, {
                workflow: this.workflowContext,
            });
            this.rememberWorkflowConversation(preferred.id);
            return;
        }
        if (conversations.some(item => item.workflow)) return;
        const conversation = conversations.find(item => !item.workflow);
        if (!conversation) return;
        await this.chat.updateConversation(conversation.id, {
            workflow: this.workflowContext,
        });
        this.rememberWorkflowConversation(conversation.id);
    }

    saveWorkflowDraft() {
        if (!this.workflowContext?.id || !this.textarea) return;
        this.workflowDrafts.set(this.workflowContext.id, {
            text: this.textarea.value,
            attachments: this.pendingAttachments.map(item => ({ ...item })),
        });
    }

    restoreWorkflowDraft() {
        const draft = this.workflowDrafts.get(this.workflowContext?.id) || {};
        this.textarea.value = draft.text || "";
        this.pendingAttachments = (draft.attachments || []).map(item => ({ ...item }));
        this.renderPendingAttachments();
        this.resizeComposer();
        this.updateComposerState();
    }

    async refreshWorkflowContext() {
        let next = this.getWorkflowContext?.() || null;
        if ((next?.id || null) === (this.workflowContext?.id || null)) {
            this.workflowContext = next;
            this.updateConversationTitle();
            return;
        }

        this.saveWorkflowDraft();
        const previousWorkflow = this.workflowContext;
        const previousConversation = this.conversations.find(
            item => item.id === this.conversationId,
        );
        if (
            previousWorkflow?.id
            && previousConversation
            && !previousConversation.workflow
            && !this.conversationScopeMismatch
        ) {
            try {
                const result = await this.chat.updateConversation(previousConversation.id, {
                    workflow: previousWorkflow,
                });
                previousConversation.workflow = result.conversation.workflow;
                this.rememberWorkflowConversation(previousConversation.id, previousWorkflow.id);
            } catch (error) {
                this.showError(`Conversation could not be attached: ${error.message}`);
            }
            next = this.getWorkflowContext?.() || null;
            if ((next?.id || null) === (this.workflowContext?.id || null)) {
                this.workflowContext = next;
                this.updateConversationTitle();
                return;
            }
        }

        const generation = ++this.workflowGeneration;
        if (this.running) {
            void this.chat.cancel("workflow_switched").catch(() => {});
            this.chat.detach();
            this.running = false;
            this.stopping = false;
            this.steering = false;
            this.activeRunPromise = null;
            this.currentRunContext = null;
            this.currentAssistant = null;
        }
        this.discardMaskReviews?.();
        this.workflowContext = next;
        this.conversationId = null;
        this.conversationScopeMismatch = false;
        this.olderMessagesCursor = null;
        this.hasOlderMessages = false;
        this.renderMessages([]);
        this.restoreWorkflowDraft();
        const preferredId = next?.id ? this.workflowConversationIds[next.id] : null;
        await this.refreshConversations(preferredId, generation);
    }

    async refreshConversations(
        preferredId = this.conversationId
            || this.workflowConversationIds[this.workflowContext?.id]
            || null,
        generation = this.workflowGeneration,
    ) {
        const workflowId = this.workflowContext?.id || null;
        const requests = [
            this.chat.listConversations("active"),
            this.chat.listConversations("archived"),
        ];
        if (workflowId) requests.push(this.chat.listConversations("active", workflowId));
        const [active, archived, scoped] = await Promise.all(requests);
        if (generation !== this.workflowGeneration) return;
        this.conversations = active.conversations || [];
        this.archivedConversations = archived.conversations || [];
        this.renderHistory();
        const available = workflowId
            ? (scoped?.conversations || [])
            : this.conversations.filter(item => !item.workflow);
        if (!available.length) {
            this.conversationId = null;
            this.conversationScopeMismatch = false;
            this.rememberWorkflowConversation(null);
            this.updateConversationTitle();
            this.renderMessages([]);
            this.updateComposerState();
            return;
        }
        const nextId = available.some((item) => item.id === preferredId)
            ? preferredId
            : available[0].id;
        await this.loadConversation(nextId, generation);
    }

    async loadConversation(conversationId, generation = this.workflowGeneration) {
        if (!conversationId || this.running) return;
        try {
            const result = await this.chat.loadConversation(conversationId);
            if (generation !== this.workflowGeneration) return;
            this.conversationId = conversationId;
            const ownerId = result.conversation?.workflow?.id || null;
            this.conversationScopeMismatch = Boolean(
                this.workflowContext?.id && ownerId !== this.workflowContext.id
            );
            if (!this.conversationScopeMismatch && ownerId) {
                this.rememberWorkflowConversation(conversationId);
            } else if (ownerId) {
                this.rememberWorkflowConversation(conversationId, ownerId);
            }
            this.olderMessagesCursor = result.nextBefore || null;
            this.hasOlderMessages = Boolean(result.hasMore);
            this.updateConversationTitle();
            this.renderHistory();
            this.renderMessages(result.messages || []);
            this.updateComposerState();
        } catch (error) {
            this.showError(`Conversation could not load: ${error.message}`);
        }
    }

    async newConversation() {
        if (this.running) return;
        if (!this.workflowContext) {
            this.showError("Open a workflow before starting a Ren chat.");
            return;
        }
        try {
            const result = await this.chat.createConversation(this.workflowContext);
            this.rememberWorkflowConversation(result.conversation.id);
            await this.refreshConversations(result.conversation.id);
            this.closeSheet();
            this.textarea.focus();
        } catch (error) {
            this.showError(`Conversation could not be created: ${error.message}`);
        }
    }

    updateConversationTitle() {
        this.conversationTitle.textContent = this.workflowContext?.name || "History";
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
            !query
            || item.title.toLowerCase().includes(query)
            || (item.workflow?.name || "Unassigned").toLowerCase().includes(query)
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
            updated.textContent = `${conversation.workflow?.name || "Unassigned"} · ${this.formatRelativeDate(conversation.updatedAt)}`;
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
            if (
                conversation.workflow
                && conversation.workflow.id !== this.workflowContext?.id
                && this.activateWorkflow
            ) {
                actions.prepend(this.iconAction(
                    "activate-conversation-workflow",
                    conversation.id,
                    "pi pi-external-link",
                    `Switch to ${conversation.workflow.name}`,
                ));
            }
            if (!conversation.workflow && this.workflowContext) {
                actions.prepend(this.iconAction(
                    "attach-conversation",
                    conversation.id,
                    "pi pi-link",
                    `Use with ${this.workflowContext.name}`,
                ));
            }
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

    async attachConversation(conversationId) {
        if (!conversationId || !this.workflowContext || this.running) return;
        try {
            await this.chat.updateConversation(conversationId, {
                workflow: this.workflowContext,
            });
            this.rememberWorkflowConversation(conversationId);
            await this.refreshConversations(conversationId);
            this.closeSheet();
        } catch (error) {
            this.showError(`Conversation could not be attached: ${error.message}`);
        }
    }

    async activateConversationWorkflow(conversationId) {
        const conversation = [...this.conversations, ...this.archivedConversations]
            .find(item => item.id === conversationId);
        if (!conversation?.workflow || !this.activateWorkflow || this.running) return;
        try {
            this.rememberWorkflowConversation(conversationId, conversation.workflow.id);
            await this.activateWorkflow(conversation.workflow.id);
            this.closeSheet();
        } catch (error) {
            this.showError(`Workflow could not be opened: ${error.message}`);
        }
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
        this.currentAssistant = null;
        const fragment = document.createDocumentFragment();
        if (this.hasOlderMessages) fragment.appendChild(this.createOlderMessagesButton());
        for (const message of messages) {
            this.appendMessage(message.role, message.content, {
                ...(message.metadata || {}),
                messageId: message.id,
                revision: message.revision,
                createdAt: message.createdAt,
            }, fragment, false);
        }
        this.messagesElement.replaceChildren(fragment);
        const empty = messages.length === 0;
        this.welcomeElement.hidden = !empty;
        this.messagesElement.hidden = empty;
        if (!empty) this.scrollToBottom();
    }

    createOlderMessagesButton() {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "fl-load-older-messages";
        button.dataset.action = "load-older-messages";
        button.textContent = this.loadingOlderMessages
            ? "Loading earlier messages…"
            : "Load earlier messages";
        button.disabled = this.loadingOlderMessages;
        return button;
    }

    async loadOlderMessages() {
        if (
            this.loadingOlderMessages
            || !this.hasOlderMessages
            || !this.olderMessagesCursor
            || !this.conversationId
        ) return;
        const conversationId = this.conversationId;
        this.loadingOlderMessages = true;
        const existingButton = this.messagesElement.querySelector(
            ".fl-load-older-messages",
        );
        if (existingButton) {
            existingButton.disabled = true;
            existingButton.textContent = "Loading earlier messages…";
        }
        const previousHeight = this.scrollElement.scrollHeight;
        const previousTop = this.scrollElement.scrollTop;
        try {
            const result = await this.chat.loadConversation(conversationId, {
                before: this.olderMessagesCursor,
            });
            if (this.conversationId !== conversationId) return;
            this.olderMessagesCursor = result.nextBefore || null;
            this.hasOlderMessages = Boolean(result.hasMore);
            const fragment = document.createDocumentFragment();
            if (this.hasOlderMessages) {
                fragment.appendChild(this.createOlderMessagesButton());
            }
            for (const message of result.messages || []) {
                this.appendMessage(message.role, message.content, {
                    ...(message.metadata || {}),
                    messageId: message.id,
                    revision: message.revision,
                    createdAt: message.createdAt,
                }, fragment, false);
            }
            existingButton?.remove();
            this.messagesElement.prepend(fragment);
            requestAnimationFrame(() => {
                const addedHeight = this.scrollElement.scrollHeight - previousHeight;
                this.scrollElement.scrollTop = previousTop + addedHeight;
            });
        } catch (error) {
            this.showError(`Earlier messages could not load: ${error.message}`);
        } finally {
            this.loadingOlderMessages = false;
            const button = this.messagesElement.querySelector(".fl-load-older-messages");
            if (button) {
                button.disabled = false;
                button.textContent = "Load earlier messages";
            }
        }
    }

    appendMessage(
        role,
        content,
        metadata = {},
        target = this.messagesElement,
        follow = true,
    ) {
        this.welcomeElement.hidden = true;
        this.messagesElement.hidden = false;
        const article = document.createElement("article");
        article.className = `fl-message ${role}`;
        if (metadata.messageId) article.dataset.messageId = metadata.messageId;
        article.messageContent = String(content || "");
        article.messageAttachments = Array.isArray(metadata.attachments)
            ? metadata.attachments.map(item => ({ ...item }))
            : [];
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
        let toolHistory = null;
        if (role === "assistant") {
            timeline = document.createElement("div");
            timeline.className = "fl-message-timeline";
            article.append(header, timeline);
            ({ body, toolHistory } = this.renderPersistedAssistantTimeline(
                timeline,
                content,
                metadata?.toolSteps || [],
            ));
            if (metadata.interrupted) article.classList.add("interrupted");
        } else {
            body = this.createMessageContent(content, article.messageAttachments, {
                messageId: metadata.messageId,
            });
            article.append(header, body);
            if (role === "user" && metadata.messageId) {
                article.appendChild(this.createUserMessageActions(metadata));
            }
        }
        target.appendChild(article);
        if (follow) this.maybeFollowOutput();
        return { article, body, timeline, toolHistory };
    }

    createUserMessageActions(metadata = {}) {
        const actions = document.createElement("div");
        actions.className = "fl-message-actions";
        const actionButton = (action, iconClass, label) => {
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.action = action;
            button.dataset.messageId = metadata.messageId;
            button.title = label;
            button.setAttribute("aria-label", label);
            const icon = document.createElement("i");
            icon.className = iconClass;
            icon.setAttribute("aria-hidden", "true");
            button.appendChild(icon);
            return button;
        };
        actions.append(
            actionButton("edit-message", "pi pi-pencil", "Edit and resend request"),
            actionButton("resend-message", "pi pi-refresh", "Resend request"),
        );

        const revision = metadata.revision || {};
        const index = Number(revision.index) || 1;
        const count = Number(revision.count) || 1;
        if (count > 1) {
            const versions = document.createElement("span");
            versions.className = "fl-message-versions";
            const previous = actionButton(
                "previous-message-version",
                "pi pi-chevron-left",
                "Previous request version",
            );
            previous.disabled = index <= 1;
            const position = document.createElement("span");
            position.textContent = `${index} / ${count}`;
            position.setAttribute("aria-label", `Request version ${index} of ${count}`);
            const next = actionButton(
                "next-message-version",
                "pi pi-chevron-right",
                "Next request version",
            );
            next.disabled = index >= count;
            versions.append(previous, position, next);
            actions.appendChild(versions);
        }
        return actions;
    }

    createMessageContent(content = "", attachments = [], options = {}) {
        const body = document.createElement("div");
        body.className = "fl-message-content";
        if (String(content || "").trim()) {
            body.appendChild(this.renderChatMarkdown(content));
        }
        if (attachments.length) {
            body.appendChild(this.createAttachmentGrid(attachments, options));
        }
        return body;
    }

    createAttachmentGrid(attachments, { pending = false, messageId = null } = {}) {
        const grid = document.createElement("section");
        grid.className = "fl-chat-attachment-grid fl-image-grid";
        grid.dataset.count = String(attachments.length);
        grid.dataset.layout = attachments.length === 1
            ? "single"
            : attachments.length % 2 === 1 ? "hero" : "grid";
        grid.setAttribute("aria-label", `${attachments.length} attached ${attachments.length === 1 ? "image" : "images"}`);
        for (const [index, attachment] of attachments.entries()) {
            const figure = document.createElement("figure");
            figure.className = "fl-chat-attachment fl-image-card";
            const preview = document.createElement("img");
            const image = { ...attachment, kind: "comfy" };
            preview.src = this.toolImagePreviewSource(image);
            preview.alt = attachment.originalName || `Attached image ${index + 1}`;
            preview.loading = "lazy";
            preview.decoding = "async";
            const previewLink = document.createElement("a");
            previewLink.href = this.toolImageOriginalSource(image);
            previewLink.target = "_blank";
            previewLink.rel = "noopener noreferrer";
            previewLink.title = "Open attached image";
            previewLink.appendChild(preview);
            figure.appendChild(previewLink);

            const caption = document.createElement("figcaption");
            const name = document.createElement("span");
            name.textContent = attachment.originalName || attachment.filename;
            name.title = name.textContent;
            caption.appendChild(name);
            const actions = document.createElement("span");
            actions.className = "fl-attachment-actions";
            if (this.placeChatImageInSelectedNode) {
                const useButton = document.createElement("button");
                useButton.type = "button";
                useButton.dataset.action = pending
                    ? "use-pending-attachment"
                    : "use-message-attachment";
                useButton.dataset.attachmentIndex = String(index);
                if (messageId) useButton.dataset.messageId = messageId;
                useButton.title = "Use in selected Load Image node";
                useButton.setAttribute("aria-label", "Use in selected Load Image node");
                const useIcon = document.createElement("i");
                useIcon.className = "pi pi-sign-in";
                useIcon.setAttribute("aria-hidden", "true");
                useButton.appendChild(useIcon);
                actions.appendChild(useButton);
            }
            if (pending) {
                const removeButton = document.createElement("button");
                removeButton.type = "button";
                removeButton.dataset.action = "remove-pending-attachment";
                removeButton.dataset.attachmentIndex = String(index);
                removeButton.title = "Remove attachment";
                removeButton.setAttribute("aria-label", "Remove attachment");
                const removeIcon = document.createElement("i");
                removeIcon.className = "pi pi-times";
                removeIcon.setAttribute("aria-hidden", "true");
                removeButton.appendChild(removeIcon);
                actions.appendChild(removeButton);
            }
            caption.appendChild(actions);
            figure.appendChild(caption);
            grid.appendChild(figure);
        }
        return grid;
    }

    renderChatMarkdown(content) {
        return renderMarkdown(content, {
            resolveImageUrl: url => this.chat.webImagePreviewUrl(url),
        });
    }

    renderPersistedAssistantTimeline(timeline, content, toolSteps) {
        const steps = Array.isArray(toolSteps) ? toolSteps : [];
        let toolHistory = null;
        if (steps.length) {
            const rail = this.createToolRail(steps);
            timeline.appendChild(rail);
            toolHistory = rail.toolHistory;
            this.renderToolHistory(toolHistory, { images: true });
        }
        const body = this.appendTimelineText(timeline, content);
        return { body, toolHistory };
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

    ensureAssistantMessage(context = null) {
        if (context?.assistant) return context.assistant;
        if (!context && this.currentAssistant) return this.currentAssistant;
        const message = this.appendMessage("assistant", "");
        message.article.classList.add("streaming");
        message.source = "";
        message.activeBody = message.body;
        message.activeSource = "";
        message.pendingText = "";
        message.textRenderFrame = null;
        if (message.activeBody) message.activeBody.classList.add("streaming-active");
        message.tools = new Map();
        if (context) context.assistant = message;
        if (!context || context === this.currentRunContext) this.currentAssistant = message;
        return message;
    }

    appendAssistantDelta(message, delta) {
        message.pendingText += delta;
        if (message.textRenderFrame !== null) return;
        message.textRenderFrame = requestAnimationFrame(() => {
            message.textRenderFrame = null;
            this.flushAssistantText(message);
        });
    }

    flushAssistantText(message) {
        if (!message?.pendingText && message?.activeBody) return;
        if (!message.activeBody) {
            if (!message.pendingText.trim()) return;
            message.activeBody = this.createMessageContent();
            message.timeline.appendChild(message.activeBody);
            message.activeBody.classList.add("streaming-active");
        }
        message.activeSource += message.pendingText;
        message.pendingText = "";
        message.activeBody.replaceChildren(this.renderChatMarkdown(message.activeSource));
        message.body = message.activeBody;
    }

    finishActiveTextSegment(message, discardEmpty = false) {
        if (message?.textRenderFrame !== null && message?.textRenderFrame !== undefined) {
            cancelAnimationFrame(message.textRenderFrame);
            message.textRenderFrame = null;
        }
        this.flushAssistantText(message);
        if (discardEmpty && message.activeBody && !message.activeSource) {
            message.activeBody.remove();
        }
        message.activeBody?.classList.remove("streaming-active");
        message.activeBody = null;
        message.activeSource = "";
        message.pendingText = "";
    }

    toolRailAtCursor(message) {
        if (message.toolHistory?.rail?.isConnected) return message.toolHistory.rail;
        const rail = this.createToolRail();
        message.timeline.prepend(rail);
        message.toolHistory = rail.toolHistory;
        return rail;
    }

    finishAssistantMessage(message) {
        if (!message) return;
        this.finishActiveTextSegment(message, true);
        const history = message.toolHistory;
        if (history && history.renderFrame !== null) {
            cancelAnimationFrame(history.renderFrame);
            history.renderFrame = null;
            const renderImages = history.renderImages;
            history.renderImages = false;
            this.renderToolHistory(history, { images: renderImages });
        }
        message.article.classList.remove("streaming");
    }

    createToolRail(steps = []) {
        const rail = document.createElement("section");
        rail.className = "fl-toolchain-breadcrumb";
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "fl-toolchain-summary";
        toggle.dataset.action = "toggle-tool-history";
        toggle.setAttribute("aria-expanded", "false");
        const icon = document.createElement("i");
        icon.className = "pi pi-cog fl-crumb-icon fl-toolchain-active-icon";
        icon.setAttribute("aria-hidden", "true");
        const current = document.createElement("strong");
        current.className = "fl-toolchain-current";
        const meta = document.createElement("span");
        meta.className = "fl-toolchain-meta";
        const disclosure = document.createElement("span");
        disclosure.className = "fl-toolchain-disclosure";
        disclosure.textContent = "Show history";
        const chevron = document.createElement("i");
        chevron.className = "pi pi-chevron-down";
        chevron.setAttribute("aria-hidden", "true");
        toggle.append(icon, current, meta, disclosure, chevron);

        const panel = document.createElement("div");
        panel.className = "fl-tool-history-panel";
        panel.hidden = true;
        const list = document.createElement("div");
        list.className = "fl-tool-history-list";
        list.setAttribute("role", "list");
        list.setAttribute("aria-label", "Tool use history");
        panel.appendChild(list);
        rail.append(toggle, panel);
        rail.toolHistory = {
            rail,
            steps: [...steps],
            expanded: false,
            visibleStepCount: TOOL_HISTORY_INITIAL_STEPS,
            cards: new Map(),
            renderFrame: null,
            renderImages: false,
        };
        return rail;
    }

    addToolStep(rail, step) {
        const history = rail.toolHistory;
        history.steps.push(step);
        this.scheduleToolHistoryRender(history);
        return history;
    }

    scheduleToolHistoryRender(history, { images = false } = {}) {
        if (!history) return;
        history.renderImages ||= images;
        if (history.renderFrame !== null) return;
        history.renderFrame = requestAnimationFrame(() => {
            history.renderFrame = null;
            const nextImages = history.renderImages;
            history.renderImages = false;
            this.renderToolHistory(history, { images: nextImages });
        });
    }

    renderToolHistory(history, { images = false } = {}) {
        if (!history) return;
        const { rail } = history;
        const summary = toolHistorySummary(history.steps);
        const active = summary.active;
        const representative = active || history.steps.at(-1) || {};
        const tool = getToolConfig(representative.name);
        const visualStatus = active
            ? "loading"
            : summary.failed
                ? "failed"
                : summary.interrupted
                    ? "cancelled"
                    : "completed";
        rail.className = `fl-toolchain-breadcrumb ${visualStatus}`;
        rail.dataset.toolCount = String(summary.total);
        rail.hidden = summary.total === 0;
        const toggle = rail.querySelector(".fl-toolchain-summary");
        toggle.setAttribute("aria-expanded", String(history.expanded));
        toggle.querySelector(".fl-toolchain-active-icon").className = `${
            tool.iconClass || "pi pi-cog"
        } fl-crumb-icon fl-toolchain-active-icon`;
        const current = toggle.querySelector(".fl-toolchain-current");
        current.textContent = active
            ? tool.runningLabel
            : summarizeToolStep(representative, tool);
        const metaParts = [];
        metaParts.push(`${summary.total} ${summary.total === 1 ? "action" : "actions"}`);
        if (!active && summary.done) metaParts.push(`${summary.done} done`);
        if (summary.retried) metaParts.push(`${summary.retried} retried`);
        if (summary.needsChoice) metaParts.push(`${summary.needsChoice} needs choice`);
        if (summary.failed) metaParts.push(`${summary.failed} failed`);
        if (summary.interrupted) metaParts.push(`${summary.interrupted} interrupted`);
        if (active) metaParts.push(`${summary.total} ${summary.total === 1 ? "call" : "calls"}`);
        toggle.querySelector(".fl-toolchain-meta").textContent = metaParts.join(" · ");
        toggle.querySelector(".fl-toolchain-disclosure").textContent = history.expanded
            ? "Hide history"
            : "Show history";
        toggle.querySelector(".pi-chevron-down, .pi-chevron-up").className = history.expanded
            ? "pi pi-chevron-up"
            : "pi pi-chevron-down";
        const panel = rail.querySelector(".fl-tool-history-panel");
        panel.hidden = !history.expanded;
        if (history.expanded) this.renderToolHistoryCards(history);
        if (images) this.renderToolImages(rail, history.steps);
    }

    renderToolHistoryCards(history) {
        const list = history.rail.querySelector(".fl-tool-history-list");
        const fragment = document.createDocumentFragment();
        const firstVisible = Math.max(0, history.steps.length - history.visibleStepCount);
        if (firstVisible > 0) {
            const more = document.createElement("button");
            more.type = "button";
            more.className = "fl-tool-history-more";
            more.dataset.action = "load-tool-history";
            more.textContent = `Show ${firstVisible} earlier ${
                firstVisible === 1 ? "action" : "actions"
            }`;
            fragment.appendChild(more);
        }
        const visibleSteps = history.steps.slice(firstVisible);
        for (const step of visibleSteps) {
            let card = history.cards.get(step);
            if (!card) {
                card = this.createToolHistoryCard();
                history.cards.set(step, card);
            }
            this.renderToolHistoryCard(card, step);
            fragment.appendChild(card);
        }
        list.replaceChildren(fragment);
    }

    createToolHistoryCard() {
        const card = document.createElement("details");
        card.setAttribute("role", "listitem");
        const summary = document.createElement("summary");
        const icon = document.createElement("i");
        icon.className = "pi pi-bolt fl-crumb-icon fl-tool-history-icon";
        icon.setAttribute("aria-hidden", "true");
        const copy = document.createElement("span");
        copy.className = "fl-crumb-copy";
        const label = document.createElement("strong");
        label.className = "fl-crumb-label";
        const description = document.createElement("span");
        description.className = "fl-crumb-description";
        copy.append(label, description);
        const status = document.createElement("em");
        status.className = "fl-crumb-status";
        summary.append(icon, copy, status);
        const technical = document.createElement("div");
        technical.className = "fl-tool-technical";
        const detailLabel = document.createElement("span");
        detailLabel.textContent = "Technical details";
        const pre = document.createElement("pre");
        technical.append(detailLabel, pre);
        card.append(summary, technical);
        return card;
    }

    renderToolHistoryCard(card, step) {
        const visualStatus = this.toolVisualStatus(step.status);
        const tool = getToolConfig(step.name);
        card.className = `fl-toolchain-crumb ${visualStatus}`;
        card.dataset.toolName = step.name || "";
        const icon = card.querySelector(".fl-tool-history-icon");
        icon.className = `${
            tool.iconClass || "pi pi-cog"
        } fl-crumb-icon fl-tool-history-icon`;
        card.querySelector(".fl-crumb-label").textContent = visualStatus === "loading"
            ? tool.runningLabel
            : summarizeToolStep(step, tool);
        card.querySelector(".fl-crumb-description").textContent = visualStatus === "loading"
            ? (tool.description || tool.label || step.name || "MCP tool")
            : (tool.label || step.name || "MCP tool");
        card.querySelector(".fl-crumb-status").textContent = {
            loading: "Working",
            completed: "Done",
            retried: "Retried",
            failed: "Failed",
            cancelled: step.status === "interrupted" ? "Interrupted" : "Stopped",
        }[visualStatus];
        const technicalSections = [];
        if (step.arguments !== undefined && step.arguments !== "") {
            technicalSections.push(`Arguments\n${technicalText(step.arguments)}`);
        }
        if (step.result !== undefined && step.result !== "") {
            technicalSections.push(`Result\n${technicalText(step.result)}`);
        }
        const technical = card.querySelector(".fl-tool-technical");
        technical.hidden = technicalSections.length === 0;
        technical.querySelector("pre").textContent = technicalText(
            technicalSections.join("\n\n"),
        );
    }

    toggleToolHistory(rail) {
        const history = rail?.toolHistory;
        if (!history) return;
        history.expanded = !history.expanded;
        this.renderToolHistory(history);
    }

    loadMoreToolHistory(rail) {
        const history = rail?.toolHistory;
        if (!history) return;
        history.visibleStepCount += TOOL_HISTORY_INITIAL_STEPS;
        this.renderToolHistory(history);
    }

    renderToolImages(item, steps) {
        const discovered = [];
        const seen = new Set();
        for (const step of steps) {
            for (const image of toolDisplayImages(step)) {
                const key = image.kind === "comfy"
                    ? `${image.type}:${image.subfolder}:${image.filename}`
                    : image.url;
                if (seen.has(key)) continue;
                seen.add(key);
                discovered.push(image);
            }
        }
        if (!discovered.length) {
            item.imageGrid?.remove();
            item.imageGrid = null;
            item.imageSignature = "";
            return;
        }

        const images = discovered.slice(0, MAX_TOOL_GALLERY_IMAGES);
        const signature = JSON.stringify(images.map(image => (
            image.kind === "comfy"
                ? [image.kind, image.type, image.subfolder, image.filename]
                : [image.kind, image.url]
        )));
        if (item.imageSignature === signature && item.imageGrid?.isConnected) return;
        item.imageSignature = signature;
        let grid = item.imageGrid;
        if (!grid?.isConnected) {
            grid = document.createElement("section");
            grid.className = "fl-tool-image-grid fl-image-grid";
            grid.setAttribute("role", "list");
            item.after(grid);
            item.imageGrid = grid;
        }
        grid.replaceChildren();
        grid.dataset.count = String(images.length);
        grid.dataset.layout = images.length === 1
            ? "single"
            : images.length % 2 === 1
                ? "hero"
                : "grid";
        grid.setAttribute(
            "aria-label",
            `${images.length} ${images.length === 1 ? "image" : "images"}`,
        );

        for (const [index, image] of images.entries()) {
            const figure = document.createElement("figure");
            figure.className = "fl-tool-image-card fl-image-card";
            figure.setAttribute("role", "listitem");
            const source = this.toolImageOriginalSource(image);
            const link = document.createElement("a");
            link.href = image.kind === "web"
                ? (image.sourceUrl || image.url)
                : source;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.title = image.kind === "web"
                ? "Open original source"
                : "Open full generated image";

            const preview = document.createElement("img");
            preview.src = this.toolImagePreviewSource(image);
            preview.alt = image.alt || image.title || `Image ${index + 1}`;
            preview.loading = "lazy";
            preview.decoding = "async";
            preview.referrerPolicy = "no-referrer";
            preview.fetchPriority = "low";

            const fallback = document.createElement("span");
            fallback.className = "fl-tool-image-fallback fl-image-fallback";
            fallback.hidden = true;
            const fallbackIcon = document.createElement("i");
            fallbackIcon.className = "pi pi-image";
            fallbackIcon.setAttribute("aria-hidden", "true");
            const fallbackCopy = document.createElement("span");
            fallbackCopy.textContent = "Preview unavailable";
            fallback.append(fallbackIcon, fallbackCopy);
            preview.addEventListener("error", () => {
                figure.classList.add("failed");
                preview.hidden = true;
                fallback.hidden = false;
            }, { once: true });
            link.append(preview, fallback);
            figure.appendChild(link);

            const caption = this.toolImageCaption(image, index);
            const figcaption = document.createElement("figcaption");
            const captionCopy = document.createElement("span");
            captionCopy.textContent = caption || `Image ${index + 1}`;
            captionCopy.title = captionCopy.textContent;
            figcaption.appendChild(captionCopy);
            if (this.uploadChatImage) {
                const actions = document.createElement("span");
                actions.className = "fl-attachment-actions";
                const addImageAction = (action, iconClass, label) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.dataset.action = action;
                    button.toolImage = image;
                    button.title = label;
                    button.setAttribute("aria-label", label);
                    const actionIcon = document.createElement("i");
                    actionIcon.className = iconClass;
                    actionIcon.setAttribute("aria-hidden", "true");
                    button.appendChild(actionIcon);
                    return button;
                };
                actions.append(
                    addImageAction(
                        "attach-tool-image",
                        "pi pi-paperclip",
                        "Attach this image to a new request",
                    ),
                    addImageAction(
                        "use-tool-image",
                        "pi pi-sign-in",
                        "Use in selected Load Image node",
                    ),
                );
                figcaption.appendChild(actions);
            }
            figure.appendChild(figcaption);
            grid.appendChild(figure);
        }

        if (discovered.length > images.length) {
            const overflow = document.createElement("span");
            overflow.className = "fl-tool-image-overflow fl-image-overflow";
            overflow.textContent = `+${discovered.length - images.length} more images`;
            grid.appendChild(overflow);
        }
    }

    toolImageOriginalSource(image) {
        if (image.kind === "web") return image.url;
        const params = new URLSearchParams({
            filename: image.filename,
            type: image.type,
        });
        if (image.subfolder) params.set("subfolder", image.subfolder);
        return `/api/view?${params.toString()}`;
    }

    toolImagePreviewSource(image) {
        if (image.kind === "web") {
            return this.chat.webImagePreviewUrl(image.url);
        }
        const params = new URLSearchParams({
            filename: image.filename,
            type: image.type,
        });
        if (image.subfolder) params.set("subfolder", image.subfolder);
        return `/fl_mcp/image/thumbnail?${params.toString()}`;
    }

    toolImageImportSource(image) {
        return image.kind === "web"
            ? this.chat.webImagePreviewUrl(image.url)
            : this.toolImageOriginalSource(image);
    }

    toolImageCaption(image, index) {
        const explicit = String(image.title || image.alt || "").trim();
        if (explicit && explicit !== "Web image") return explicit.slice(0, 120);
        if (image.kind === "web") {
            try {
                return new URL(image.sourceUrl || image.url).hostname;
            } catch (_) {
                return `Web image ${index + 1}`;
            }
        }
        return image.title || `Generated output ${index + 1}`;
    }

    toolVisualStatus(status) {
        return {
            running: "loading",
            done: "completed",
            finished: "completed",
            retried: "retried",
            failed: "failed",
            cancelled: "cancelled",
            interrupted: "cancelled",
        }[status] || "completed";
    }

    handleEvent(event, context = this.currentRunContext) {
        if (event.type === "RUN_STARTED") {
            if (context) context.runId = event.runId || context.runId;
            const message = this.ensureAssistantMessage(context);
            message.runId = event.runId || message.runId || null;
            if (message.runId) message.article.dataset.runId = message.runId;
            if (context && context === this.currentRunContext && this.steering) {
                this.steering = false;
                this.setRunStatus("Ren is working…");
                this.updateComposerState();
            }
            this.announce("Ren started working.");
        } else if (event.type === "TEXT_MESSAGE_START") {
            this.ensureAssistantMessage(context);
        } else if (event.type === "TEXT_MESSAGE_CONTENT") {
            const message = this.ensureAssistantMessage(context);
            const delta = event.delta || "";
            message.source += delta;
            this.appendAssistantDelta(message, delta);
        } else if (event.type === "TOOL_CALL_START") {
            const message = this.ensureAssistantMessage(context);
            const id = event.toolCallId || crypto.randomUUID();
            for (const tool of message.tools.values()) {
                if (tool.name === event.toolCallName && tool.status === "running") {
                    this.setToolStatus(tool, "retried");
                    break;
                }
            }
            const step = {
                name: event.toolCallName,
                status: "running",
                arguments: "",
            };
            const history = this.addToolStep(this.toolRailAtCursor(message), step);
            message.tools.set(id, {
                history,
                name: event.toolCallName,
                status: "running",
                arguments: "",
                step,
            });
            const toolConfig = getToolConfig(event.toolCallName);
            this.setRunStatus(toolConfig.runningLabel, toolConfig.iconClass);
        } else if (event.type === "TOOL_CALL_ARGS") {
            const tool = (context?.assistant || this.currentAssistant)?.tools.get(
                event.toolCallId,
            );
            if (tool) {
                tool.arguments += event.delta || "";
                tool.step.arguments = tool.arguments;
                if (tool.history.expanded) {
                    this.scheduleToolHistoryRender(tool.history, { details: true });
                }
            }
        } else if (event.type === "TOOL_CALL_RESULT") {
            const tool = (context?.assistant || this.currentAssistant)?.tools.get(
                event.toolCallId,
            );
            if (tool) this.setToolStatus(tool, "done", event.content);
            this.setRunStatusForActiveTool(context?.assistant || this.currentAssistant);
        } else if (event.type === "CUSTOM" && event.name === "approval_required") {
            this.renderApproval(event.value, context);
        } else if (event.type === "CUSTOM" && event.name === "approval_resolved") {
            this.resolveApprovalCard(event.value);
        } else if (event.type === "RUN_ERROR") {
            const interrupted = event.code === "steered";
            this.settleOpenTools(
                interrupted ? "interrupted" : event.code === "cancelled" ? "cancelled" : "failed",
                context?.assistant || this.currentAssistant,
            );
            const assistant = context?.assistant || this.currentAssistant;
            this.finishAssistantMessage(assistant);
            if (interrupted) assistant?.article?.classList.add("interrupted");
            if (event.code === "cancelled" || interrupted) {
                this.clearError();
                this.announce(interrupted ? "Previous response interrupted." : "Response stopped.");
            } else {
                this.showRunError(event.message || "The assistant run failed.");
            }
            if (!context || context === this.currentRunContext) {
                this.running = false;
                this.updateComposerState();
            }
        } else if (event.type === "RUN_FINISHED") {
            this.settleOpenTools("finished", context?.assistant || this.currentAssistant);
            this.finishAssistantMessage(context?.assistant || this.currentAssistant);
            if (!context || context === this.currentRunContext) {
                this.running = false;
                this.updateComposerState();
            }
            this.announce("Ren finished.");
        }
        this.maybeFollowOutput();
    }

    setToolStatus(tool, status, result = undefined) {
        tool.status = status;
        tool.step.status = status;
        if (result !== undefined) tool.step.result = result;
        this.scheduleToolHistoryRender(tool.history, {
            details: tool.history.expanded,
            images: result !== undefined,
        });
    }

    settleOpenTools(status, message = this.currentAssistant) {
        for (const tool of message?.tools?.values() || []) {
            if (tool.status === "running") this.setToolStatus(tool, status);
        }
    }

    renderApproval(value, context = this.currentRunContext) {
        const message = this.ensureAssistantMessage(context);
        const copy = this.approvalCopy(value.toolName, value.arguments);
        const isMaskReview = value.toolName === "confirm_mask_review";
        const card = document.createElement("section");
        card.className = "fl-approval-card";
        card.dataset.approvalId = value.approvalId;
        card.dataset.toolName = value.toolName || "";
        const eyebrow = document.createElement("span");
        eyebrow.className = "fl-approval-state";
        const shield = document.createElement("i");
        shield.className = "pi pi-shield";
        shield.setAttribute("aria-hidden", "true");
        eyebrow.append(
            shield,
            document.createTextNode(isMaskReview ? "Mask review" : "Approval required"),
        );
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
        deny.textContent = isMaskReview ? "Needs changes" : "Deny";
        const approve = document.createElement("button");
        approve.type = "button";
        approve.className = "fl-primary-button";
        approve.textContent = isMaskReview ? "Use this mask" : "Allow once";
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
        actions.append(deny, approve);
        if (!isMaskReview) actions.appendChild(alwaysAllow);
        card.append(eyebrow, title, consequence, details, actions);
        this.finishActiveTextSegment(message, true);
        message.timeline.appendChild(card);
        this.announce(`${copy.title} Approval required.`);
    }

    approvalCopy(toolName, argumentsValue) {
        const args = argumentsValue || {};
        const request = args.request && typeof args.request === "object"
            ? args.request
            : args;
        const nodeIds = request.node_ids || request.nodeIds;
        const nodeCount = Array.isArray(nodeIds) ? nodeIds.length : null;
        const maskRegions = Array.isArray(request.regions) ? request.regions : [];
        const maskOperations = new Set(
            maskRegions.map(region => region?.operation || "paint"),
        );
        const maskVerb = maskOperations.size === 1 && maskOperations.has("paint")
            ? "paint"
            : maskOperations.size === 1 && maskOperations.has("erase")
                ? "erase"
                : "apply";
        const maskRegionCount = `${maskRegions.length} ${
            maskRegions.length === 1 ? "region" : "regions"
        }`;
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
            edit_node_mask: {
                title: request.clear_existing
                    ? "Replace this image mask?"
                    : "Edit this image mask?",
                consequence: maskRegions.length
                    ? `Ren will ${maskVerb} ${maskRegionCount}, save a new mask image, and update the selected image node.`
                    : "Ren will save a new mask image and update the selected image node.",
            },
            confirm_mask_review: {
                title: "Use this mask?",
                consequence: "Inspect the magenta mask on the canvas. Approve it to continue, or choose Needs changes and tell Ren what to revise.",
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
        const isMaskReview = card.dataset.toolName === "confirm_mask_review";
        const labels = {
            approved: isMaskReview ? "Mask approved" : "Approved",
            always_allowed: "Always allowed",
            denied: isMaskReview ? "Needs changes" : "Denied",
            expired: isMaskReview ? "Mask review expired" : "Approval expired",
        };
        const state = card.querySelector(".fl-approval-state");
        state.replaceChildren(document.createTextNode(labels[resolution] || "Resolved"));
        card.querySelector(".fl-approval-actions")?.remove();
        this.announce(labels[resolution] || "Approval resolved.");
    }

    async send() {
        const message = this.textarea.value.trim();
        const attachments = this.pendingAttachments.map(item => ({ ...item }));
        if (
            (!message && !attachments.length)
            || this.uploadingAttachments
            || this.stopping
            || this.steering
        ) return;
        if (!this.status?.configured) {
            this.openSheet("settings", "model");
            this.showError("Choose and test a model before sending a message.");
            return;
        }
        if (this.running) {
            await this.steer(message, attachments, this.composerSearchSelect.value);
            return;
        }
        this.clearComposerDraft();
        await this.startRunMessage(
            message,
            null,
            this.composerSearchSelect.value,
            attachments,
        );
    }

    clearComposerDraft() {
        this.textarea.value = "";
        this.pendingAttachments = [];
        this.renderPendingAttachments();
        this.resizeComposer();
        this.updateComposerState();
    }

    async startRunMessage(...args) {
        const runPromise = this.runMessage(...args);
        this.activeRunPromise = runPromise;
        try {
            await runPromise;
        } finally {
            if (this.activeRunPromise === runPromise) this.activeRunPromise = null;
        }
    }

    async steer(message, attachments, searchMode) {
        this.steering = true;
        this.setRunStatus("Steering Ren…", "pi pi-send");
        this.updateComposerState();
        try {
            const activeRunId = this.chat.runId || await this.chat.runReady;
            if (!activeRunId) {
                throw new Error("The current response has not started yet.");
            }
            this.clearComposerDraft();
            this.announce("Steering Ren with your new message.");
            await this.startRunMessage(
                message,
                null,
                searchMode,
                attachments,
                activeRunId,
            );
        } catch (error) {
            this.showError(`Message could not steer the response: ${error.message}`);
        } finally {
            this.steering = false;
            if (this.running) this.setRunStatus("Ren is working…");
            this.updateComposerState();
        }
    }

    async runMessage(
        message,
        editMessageId = null,
        searchMode = this.composerSearchSelect.value,
        attachments = [],
        steerRunId = null,
    ) {
        if ((!message && !attachments.length) || (this.running && !steerRunId)) return;
        if (!this.workflowContext) {
            this.showError("Open a workflow before messaging Ren.");
            return;
        }
        if (this.conversationScopeMismatch) {
            this.showError("This conversation belongs to another workflow. Open that workflow to continue.");
            return;
        }
        if (!this.status?.configured) {
            this.openSheet("settings", "model");
            this.showError("Choose and test a model before sending a message.");
            return;
        }
        this.clearError();
        this.lastFailedMessage = message;
        this.lastFailedEditMessageId = editMessageId;
        this.lastFailedSearchMode = searchMode;
        this.lastFailedAttachments = attachments.map(item => ({ ...item }));
        this.running = true;
        const generation = this.workflowGeneration;
        const workflow = { ...this.workflowContext };
        const runContext = { runId: null, assistant: null, generation, workflowId: workflow.id };
        this.currentRunContext = runContext;
        this.currentAssistant = null;
        this.followOutput = true;
        const optimisticUser = editMessageId
            ? this.renderOptimisticRevision(editMessageId, message, attachments)
            : this.appendMessage("user", message, { attachments });
        this.updateComposerState();
        try {
            await this.chat.startRun({
                sessionId: this.sessionManager.getSessionId(),
                conversationId: this.conversationId,
                message,
                reasoningEffort: this.composerReasoningSelect.value,
                searchMode,
                editMessageId,
                attachments,
                workflow,
                steerRunId,
                onReady: ({ runId, conversationId, userMessage }) => {
                    if (generation !== this.workflowGeneration) return;
                    runContext.runId = runId;
                    this.conversationId = conversationId;
                    this.rememberWorkflowConversation(conversationId);
                    this.applyUserMessageMetadata(
                        optimisticUser?.article,
                        userMessage,
                    );
                },
                onEvent: (event) => {
                    if (generation === this.workflowGeneration) {
                        this.handleEvent(event, runContext);
                    }
                },
            });
        } catch (error) {
            if (error.name !== "AbortError") {
                this.showRunError(error.message);
            }
        } finally {
            if (this.currentRunContext === runContext) {
                this.running = false;
                this.updateComposerState();
            }
            if (generation === this.workflowGeneration) {
                await this.refreshConversations(this.conversationId, generation);
            }
        }
    }

    renderOptimisticRevision(messageId, content, attachments = []) {
        const article = this.messagesElement.querySelector(
            `.fl-message.user[data-message-id="${CSS.escape(messageId)}"]`,
        );
        if (!article) {
            return this.appendMessage("user", content, { attachments });
        }
        let following = article.nextElementSibling;
        while (following) {
            const next = following.nextElementSibling;
            following.remove();
            following = next;
        }
        article.remove();
        return this.appendMessage("user", content, { attachments });
    }

    applyUserMessageMetadata(article, message) {
        if (!article || !message?.id) return;
        article.dataset.messageId = message.id;
        article.querySelector(".fl-message-actions")?.remove();
        article.appendChild(this.createUserMessageActions({
            messageId: message.id,
            revision: message.revision,
        }));
    }

    startMessageEdit(messageId) {
        if (!messageId || this.running) return;
        this.container.querySelectorAll(".fl-message-edit-form").forEach(form => {
            const existingId = form.closest(".fl-message")?.dataset.messageId;
            this.cancelMessageEdit(existingId);
        });
        const article = this.messagesElement.querySelector(
            `.fl-message.user[data-message-id="${CSS.escape(messageId)}"]`,
        );
        if (!article || article.querySelector(".fl-message-edit-form")) return;
        article.querySelector(".fl-message-content").hidden = true;
        article.querySelector(".fl-message-actions").hidden = true;
        const form = document.createElement("div");
        form.className = "fl-message-edit-form";
        const input = document.createElement("textarea");
        input.value = article.messageContent || "";
        input.rows = 3;
        input.setAttribute("aria-label", "Edit request");
        const actions = document.createElement("div");
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "fl-secondary-button";
        cancel.dataset.action = "cancel-message-edit";
        cancel.dataset.messageId = messageId;
        cancel.textContent = "Cancel";
        const submit = document.createElement("button");
        submit.type = "button";
        submit.className = "fl-primary-button";
        submit.dataset.action = "submit-message-edit";
        submit.dataset.messageId = messageId;
        submit.textContent = "Send edited request";
        actions.append(cancel, submit);
        form.append(input, actions);
        article.appendChild(form);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Escape") this.cancelMessageEdit(messageId);
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                this.submitMessageEdit(messageId);
            }
        });
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
    }

    cancelMessageEdit(messageId) {
        if (!messageId) return;
        const article = this.messagesElement.querySelector(
            `.fl-message.user[data-message-id="${CSS.escape(messageId)}"]`,
        );
        article?.querySelector(".fl-message-edit-form")?.remove();
        const content = article?.querySelector(".fl-message-content");
        const actions = article?.querySelector(".fl-message-actions");
        if (content) content.hidden = false;
        if (actions) actions.hidden = false;
    }

    async submitMessageEdit(messageId) {
        if (this.running) return;
        const article = this.messagesElement.querySelector(
            `.fl-message.user[data-message-id="${CSS.escape(messageId)}"]`,
        );
        const content = article?.querySelector(".fl-message-edit-form textarea")
            ?.value.trim();
        const attachments = article?.messageAttachments || [];
        if (!content && !attachments.length) return;
        await this.startRunMessage(
            content,
            messageId,
            this.composerSearchSelect.value,
            attachments,
        );
    }

    async resendMessage(messageId) {
        if (this.running) return;
        const article = this.messagesElement.querySelector(
            `.fl-message.user[data-message-id="${CSS.escape(messageId)}"]`,
        );
        const content = article?.messageContent?.trim();
        const attachments = article?.messageAttachments || [];
        if (!content && !attachments.length) return;
        await this.startRunMessage(
            content,
            messageId,
            this.composerSearchSelect.value,
            attachments,
        );
    }

    async changeMessageVersion(messageId, direction) {
        if (!this.conversationId || !messageId || this.running) return;
        try {
            const result = await this.chat.selectMessageVersion(
                this.conversationId,
                messageId,
                direction,
            );
            this.renderMessages(result.messages || []);
        } catch (error) {
            this.showError(`Message version could not load: ${error.message}`);
        }
    }

    async stop() {
        if (!this.running || this.stopping || this.steering) return;
        const activeRun = this.activeRunPromise;
        this.stopping = true;
        this.setRunStatus("Stopping Ren…", "pi pi-stop-circle");
        this.updateComposerState();
        try {
            const cancelled = await this.chat.cancel();
            if (!cancelled) throw new Error("The current response has not started yet.");
            this.discardMaskReviews?.();
            if (activeRun) await activeRun;
            this.discardMaskReviews?.();
        } catch (error) {
            this.showError(`Response could not be stopped: ${error.message}`);
        } finally {
            this.stopping = false;
            if (this.running) this.setRunStatus("Ren is working…");
            this.updateComposerState();
        }
    }

    setRunStatus(text, iconClass = "pi pi-spin pi-spinner") {
        this.runStatusText.textContent = text;
        this.runStatusIcon.className = `${iconClass} fl-run-status-icon`;
    }

    setRunStatusForActiveTool(message) {
        const tools = [...(message?.tools?.values() || [])];
        for (let index = tools.length - 1; index >= 0; index--) {
            if (tools[index].status !== "running") continue;
            const config = getToolConfig(tools[index].name);
            this.setRunStatus(config.runningLabel, config.iconClass);
            return;
        }
        this.setRunStatus("Ren is working…");
    }

    updateComposerState() {
        const hasDraft = Boolean(
            this.textarea.value.trim() || this.pendingAttachments.length
        );
        this.sendButton.disabled = this.uploadingAttachments
            || this.stopping
            || this.steering
            || !this.workflowContext
            || this.conversationScopeMismatch
            || !hasDraft;
        this.sendButton.title = this.running
            ? "Steer Ren with this message (Enter)"
            : "Send message (Enter)";
        this.runStatus.hidden = !this.running;
        this.stopButton.disabled = this.stopping || this.steering;
        this.stopButton.textContent = this.stopping ? "Stopping…" : "Stop";
        this.textarea.disabled = false;
        if (!this.running) this.setRunStatus("Ren is working…");
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
            setup: this.status?.model
                ? (this.status?.credential?.message
                    || "The selected model connection is not ready.")
                : "Choose a model connection before chatting with Ren.",
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
        const activeModel = this.pendingSubscriptionModel || this.status?.model;
        if (activeModel) {
            this.modelInput.value = activeModel;
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
        if ((!this.lastFailedMessage && !this.lastFailedAttachments.length) || this.running) return;
        if (this.lastFailedEditMessageId) {
            this.startRunMessage(
                this.lastFailedMessage,
                this.lastFailedEditMessageId,
                this.lastFailedSearchMode,
                this.lastFailedAttachments,
            );
            return;
        }
        if (this.lastFailedSearchMode) {
            this.composerSearchSelect.value = this.lastFailedSearchMode;
        }
        this.textarea.value = this.lastFailedMessage;
        this.pendingAttachments = this.lastFailedAttachments.map(item => ({ ...item }));
        this.renderPendingAttachments();
        this.resizeComposer();
        this.updateComposerState();
        this.send();
    }

    dragHasFiles(event) {
        return Array.from(event.dataTransfer?.types || []).includes("Files");
    }

    handleImagePaste(event) {
        const files = Array.from(event.clipboardData?.files || []).filter(file => (
            String(file.type || "").startsWith("image/")
        ));
        if (!files.length) return;
        event.preventDefault();
        this.addImageFiles(files);
    }

    async imageDimensions(file) {
        try {
            const bitmap = await createImageBitmap(file);
            const dimensions = { width: bitmap.width, height: bitmap.height };
            bitmap.close?.();
            return dimensions;
        } catch (_) {
            return { width: 0, height: 0 };
        }
    }

    async addImageFiles(fileList) {
        if (this.uploadingAttachments) return;
        if (!this.uploadChatImage) {
            this.showError("Image upload is unavailable until the ComfyUI bridge loads.");
            return;
        }
        const available = MAX_CHAT_ATTACHMENTS - this.pendingAttachments.length;
        const files = Array.from(fileList || []).slice(0, Math.max(0, available));
        if (!files.length) {
            this.showError(`Attach at most ${MAX_CHAT_ATTACHMENTS} images per message.`);
            return;
        }
        const invalid = files.find(file => (
            !CHAT_IMAGE_TYPES.has(String(file.type || "").toLowerCase())
            || file.size > MAX_CHAT_ATTACHMENT_BYTES
        ));
        if (invalid) {
            this.showError(`${invalid.name}: use PNG, JPEG, WebP, or GIF up to 32 MB.`);
            return;
        }
        this.clearError();
        this.uploadingAttachments = true;
        this.composerContainer.classList.add("uploading");
        this.updateComposerState();
        const session = String(this.sessionManager.getSessionId() || "session")
            .replace(/[^a-zA-Z0-9_-]+/g, "-")
            .slice(0, 80);
        const subfolder = `ren-chat/${session}`;
        try {
            for (const file of files) {
                const [image, dimensions] = await Promise.all([
                    this.uploadChatImage(file, subfolder),
                    this.imageDimensions(file),
                ]);
                this.pendingAttachments.push({
                    ...image,
                    originalName: file.name || image.filename,
                    mimeType: file.type,
                    sizeBytes: file.size,
                    ...dimensions,
                });
                this.renderPendingAttachments();
            }
            this.announce(`${files.length} ${files.length === 1 ? "image" : "images"} attached.`);
        } catch (error) {
            this.showError(`Image could not be attached: ${error.message}`);
        } finally {
            this.uploadingAttachments = false;
            this.composerContainer.classList.remove("uploading");
            this.updateComposerState();
        }
    }

    renderPendingAttachments() {
        this.attachmentTray.replaceChildren();
        this.attachmentTray.hidden = this.pendingAttachments.length === 0;
        if (this.pendingAttachments.length) {
            this.attachmentTray.appendChild(this.createAttachmentGrid(
                this.pendingAttachments,
                { pending: true },
            ));
        }
        this.updateComposerState();
    }

    removePendingAttachment(index) {
        if (!Number.isInteger(index) || index < 0 || index >= this.pendingAttachments.length) return;
        this.pendingAttachments.splice(index, 1);
        this.renderPendingAttachments();
    }

    async usePendingAttachment(index) {
        await this.useAttachment(this.pendingAttachments[index]);
    }

    async useAttachment(attachment) {
        if (!attachment || !this.placeChatImageInSelectedNode) return;
        try {
            const result = await this.placeChatImageInSelectedNode(attachment);
            this.clearError();
            this.announce(`Image placed in ${result.title || `node ${result.node_id}`}.`);
        } catch (error) {
            this.showError(`Image could not be placed: ${error.message}`);
        }
    }

    importedImageName(image, mimeType) {
        const extensions = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        };
        let name = String(image?.filename || "");
        if (!name && image?.url) {
            try {
                name = decodeURIComponent(new URL(image.url).pathname.split("/").pop() || "");
            } catch (_) {
                name = "";
            }
        }
        const base = (name || "chat-image").replace(/\.[^.]+$/, "");
        return `${base}${extensions[mimeType] || ".png"}`;
    }

    async importToolImage(image) {
        if (!image) throw new Error("This image is no longer available.");
        if (
            image.kind === "comfy"
            && image.type === "input"
            && (image.subfolder === "ren-chat" || image.subfolder?.startsWith("ren-chat/"))
        ) {
            return {
                ...image,
                originalName: image.title || image.filename,
                mimeType: "",
                sizeBytes: 0,
                width: 0,
                height: 0,
            };
        }
        const response = await fetch(this.toolImageImportSource(image));
        if (!response.ok) {
            throw new Error(`Image download failed (${response.status}).`);
        }
        const blob = await response.blob();
        const mimeType = String(blob.type || "").split(";")[0].toLowerCase();
        if (!CHAT_IMAGE_TYPES.has(mimeType) || blob.size > MAX_CHAT_ATTACHMENT_BYTES) {
            throw new Error("The image format or size cannot be imported.");
        }
        const originalName = this.importedImageName(image, mimeType);
        const file = new File([blob], originalName, { type: mimeType });
        const session = String(this.sessionManager.getSessionId() || "session")
            .replace(/[^a-zA-Z0-9_-]+/g, "-")
            .slice(0, 80);
        const [uploaded, dimensions] = await Promise.all([
            this.uploadChatImage(file, `ren-chat/${session}`),
            this.imageDimensions(file),
        ]);
        return {
            ...uploaded,
            originalName,
            mimeType,
            sizeBytes: file.size,
            ...dimensions,
        };
    }

    async attachToolImage(image) {
        if (this.uploadingAttachments || this.pendingAttachments.length >= MAX_CHAT_ATTACHMENTS) {
            this.showError(`Attach at most ${MAX_CHAT_ATTACHMENTS} images per message.`);
            return;
        }
        this.uploadingAttachments = true;
        this.composerContainer.classList.add("uploading");
        this.updateComposerState();
        try {
            const attachment = await this.importToolImage(image);
            this.pendingAttachments.push(attachment);
            this.renderPendingAttachments();
            this.clearError();
            this.announce("Image attached to the next request.");
        } catch (error) {
            this.showError(`Image could not be attached: ${error.message}`);
        } finally {
            this.uploadingAttachments = false;
            this.composerContainer.classList.remove("uploading");
            this.updateComposerState();
        }
    }

    async useToolImage(image) {
        if (this.uploadingAttachments) return;
        this.uploadingAttachments = true;
        this.composerContainer.classList.add("uploading");
        this.updateComposerState();
        try {
            const attachment = await this.importToolImage(image);
            await this.useAttachment(attachment);
        } catch (error) {
            this.showError(`Image could not be placed: ${error.message}`);
        } finally {
            this.uploadingAttachments = false;
            this.composerContainer.classList.remove("uploading");
            this.updateComposerState();
        }
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

    handleThreadResize() {
        if (this.followOutput) this.maybeFollowOutput();
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
        if (this.followFrame !== null) return;
        this.followFrame = requestAnimationFrame(() => {
            this.followFrame = null;
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
        this.workflowContextUnsubscribe?.();
        this.workflowContextUnsubscribe = null;
        this.threadResizeObserver?.disconnect();
        this.threadResizeObserver = null;
        clearTimeout(this.undoTimer);
        clearTimeout(this.jumpScrollTimer);
        if (this.followFrame !== null) cancelAnimationFrame(this.followFrame);
        this.followFrame = null;
        for (const history of this.container.querySelectorAll(".fl-toolchain-breadcrumb")) {
            if (history.toolHistory?.renderFrame !== null) {
                cancelAnimationFrame(history.toolHistory.renderFrame);
            }
        }
        document.removeEventListener("pointerdown", this.documentPointerHandler);
        this.container.replaceChildren();
        this.container.classList.remove("fl-chat-panel-host");
    }
}
