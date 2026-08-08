import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const root = new URL("../../", import.meta.url);


async function loadAssistantPanel() {
    const source = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const classStart = source.indexOf("export class AssistantPanel");
    assert.notEqual(classStart, -1, "AssistantPanel class was not found");

    const classSource = source
        .slice(classStart)
        .replace("export class AssistantPanel", "class AssistantPanel");
    const context = vm.createContext({});
    vm.runInContext(
        `${classSource}\nglobalThis.AssistantPanel = AssistantPanel;`,
        context,
    );
    return context.AssistantPanel;
}


test("runMessage renders the user request before starting the backend run", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    const calls = [];
    const article = {};
    const attachments = [{ filename: "reference.png", url: "/view/reference.png" }];

    Object.assign(panel, {
        status: { configured: true },
        running: false,
        workflowContext: { id: "workflow-1", name: "Workflow", path: "workflows/test.json" },
        workflowGeneration: 0,
        conversationId: "conversation-before",
        currentRunContext: null,
        currentAssistant: null,
        composerSearchSelect: { value: "free" },
        composerReasoningSelect: { value: "high" },
        sessionManager: { getSessionId: () => "session-1" },
        clearError: () => {},
        updateComposerState: () => {},
        handleEvent: () => {},
        showRunError: error => assert.fail(error),
        refreshConversations: async () => {},
        rememberWorkflowConversation: () => {},
        appendMessage(role, content, options) {
            calls.push({ type: "append", role, content, options });
            return { article };
        },
        applyUserMessageMetadata(target, message) {
            calls.push({ type: "metadata", target, message });
        },
        chat: {
            async startRun(options) {
                calls.push({ type: "start", options });
                options.onReady({
                    runId: "run-1",
                    conversationId: "conversation-after",
                    userMessage: { id: "message-1", revision: { index: 1, count: 1 } },
                });
            },
        },
    });

    await panel.runMessage("hello Ren", null, "off", attachments, null);

    assert.deepEqual(calls.map(call => call.type), ["append", "start", "metadata"]);
    assert.equal(calls[0].role, "user");
    assert.equal(calls[0].content, "hello Ren");
    assert.equal(calls[0].options.attachments, attachments);
    assert.equal(calls[1].options.searchMode, "off");
    assert.equal(calls[1].options.reasoningEffort, "high");
    assert.deepEqual(calls[1].options.attachments, attachments);
    assert.equal(calls[1].options.steerRunId, null);
    assert.equal(calls[1].options.workflow.id, "workflow-1");
    assert.equal(calls[2].target, article);
    assert.equal(calls[2].message.id, "message-1");
    assert.equal(panel.conversationId, "conversation-after");
    assert.equal(panel.currentRunContext.runId, "run-1");
    assert.equal(panel.running, false);
});


test("switching workflows interrupts the active run and restores the tab chat", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    const calls = [];
    let activeWorkflow = { id: "workflow-b", name: "B" };

    Object.assign(panel, {
        workflowContext: { id: "workflow-a", name: "A" },
        workflowGeneration: 3,
        workflowConversationIds: { "workflow-b": "conversation-b" },
        conversations: [{ id: "conversation-a", workflow: null }],
        conversationScopeMismatch: false,
        getWorkflowContext: () => activeWorkflow,
        running: true,
        stopping: false,
        steering: false,
        activeRunPromise: Promise.resolve(),
        currentRunContext: {},
        currentAssistant: {},
        conversationId: "conversation-a",
        saveWorkflowDraft: () => calls.push("save-draft"),
        discardMaskReviews: () => calls.push("discard-masks"),
        renderMessages: messages => calls.push(["render", messages]),
        restoreWorkflowDraft: () => calls.push("restore-draft"),
        rememberWorkflowConversation: (conversationId, workflowId) => {
            panel.workflowConversationIds[workflowId] = conversationId;
            calls.push(["remember", conversationId, workflowId]);
        },
        showError: error => assert.fail(error),
        refreshConversations: async (preferred, generation) => {
            calls.push(["refresh", preferred, generation]);
        },
        chat: {
            updateConversation: async (conversationId, changes) => {
                calls.push(["attach", conversationId, changes.workflow.id]);
                return {
                    conversation: {
                        id: conversationId,
                        workflow: changes.workflow,
                    },
                };
            },
            cancel: async reason => calls.push(["cancel", reason]),
            detach: () => calls.push("detach"),
        },
    });

    await panel.refreshWorkflowContext();

    assert.equal(panel.workflowContext.id, "workflow-b");
    assert.equal(panel.workflowGeneration, 4);
    assert.equal(panel.conversationId, null);
    assert.equal(panel.running, false);
    assert.deepEqual(calls.at(-1), ["refresh", "conversation-b", 4]);
    assert.ok(calls.some(call => Array.isArray(call) && call[0] === "attach" && call[2] === "workflow-a"));
    assert.ok(calls.some(call => Array.isArray(call) && call[0] === "remember" && call[2] === "workflow-a"));
    assert.ok(calls.some(call => Array.isArray(call) && call[0] === "cancel" && call[1] === "workflow_switched"));
    assert.ok(calls.includes("detach"));

    activeWorkflow = { id: "workflow-a", name: "A" };
    await panel.refreshWorkflowContext();

    assert.equal(panel.workflowContext.id, "workflow-a");
    assert.deepEqual(calls.at(-1), ["refresh", "conversation-a", 5]);
});


test("switching workflows does not silently attach unassigned history previews", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    let attached = false;

    Object.assign(panel, {
        workflowContext: { id: "workflow-a", name: "A" },
        workflowGeneration: 0,
        workflowConversationIds: {},
        conversations: [{ id: "legacy-conversation", workflow: null }],
        conversationId: "legacy-conversation",
        conversationScopeMismatch: true,
        getWorkflowContext: () => ({ id: "workflow-b", name: "B" }),
        running: false,
        saveWorkflowDraft: () => {},
        discardMaskReviews: () => {},
        renderMessages: () => {},
        restoreWorkflowDraft: () => {},
        refreshConversations: async () => {},
        chat: {
            updateConversation: async () => {
                attached = true;
            },
        },
    });

    await panel.refreshWorkflowContext();

    assert.equal(attached, false);
    assert.equal(panel.workflowContext.id, "workflow-b");
});


test("the first workflow adopts the latest legacy conversation once", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    const calls = [];

    Object.assign(panel, {
        workflowContext: { id: "workflow-a", name: "A" },
        workflowConversationIds: {},
        rememberWorkflowConversation: conversationId => calls.push(["remember", conversationId]),
        chat: {
            listConversations: async () => ({
                conversations: [
                    { id: "latest-legacy", workflow: null },
                    { id: "older-legacy", workflow: null },
                ],
            }),
            updateConversation: async (conversationId, changes) => {
                calls.push(["attach", conversationId, changes.workflow.id]);
            },
        },
    });

    await panel.adoptLatestLegacyConversation();

    assert.deepEqual(calls, [
        ["attach", "latest-legacy", "workflow-a"],
        ["remember", "latest-legacy"],
    ]);

    calls.length = 0;
    panel.chat.listConversations = async () => ({
        conversations: [
            { id: "scoped", workflow: { id: "workflow-b" } },
            { id: "latest-legacy", workflow: null },
        ],
    });

    await panel.adoptLatestLegacyConversation();

    assert.deepEqual(calls, []);
});


test("conversation refresh restores the last chat selected for the active workflow", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    const loaded = [];
    const active = [
        { id: "newer-conversation", workflow: { id: "workflow-a" } },
        { id: "last-open-conversation", workflow: { id: "workflow-a" } },
    ];

    Object.assign(panel, {
        workflowContext: { id: "workflow-a", name: "A" },
        workflowConversationIds: { "workflow-a": "last-open-conversation" },
        workflowGeneration: 0,
        conversationId: null,
        conversations: [],
        archivedConversations: [],
        renderHistory: () => {},
        loadConversation: async conversationId => loaded.push(conversationId),
        chat: {
            listConversations: async (state, workflowId) => ({
                conversations: state === "active" && (!workflowId || workflowId === "workflow-a")
                    ? active
                    : [],
            }),
        },
    });

    await panel.refreshConversations();

    assert.deepEqual(loaded, ["last-open-conversation"]);
});


test("legacy adoption preserves an explicitly selected workflow chat", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    const calls = [];

    Object.assign(panel, {
        workflowContext: { id: "workflow-a", name: "A" },
        workflowConversationIds: { "workflow-a": "selected-legacy" },
        rememberWorkflowConversation: conversationId => calls.push(["remember", conversationId]),
        chat: {
            listConversations: async () => ({
                conversations: [
                    { id: "newer-legacy", workflow: null },
                    { id: "selected-legacy", workflow: null },
                ],
            }),
            updateConversation: async (conversationId, changes) => {
                calls.push(["attach", conversationId, changes.workflow.id]);
            },
        },
    });

    await panel.adoptLatestLegacyConversation();

    assert.deepEqual(calls, [
        ["attach", "selected-legacy", "workflow-a"],
        ["remember", "selected-legacy"],
    ]);
});


test("restored chats stay pinned while their message layout settles", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    let followed = 0;
    panel.followOutput = true;
    panel.maybeFollowOutput = () => {
        followed += 1;
    };

    panel.handleThreadResize();
    assert.equal(followed, 1);

    panel.followOutput = false;
    panel.handleThreadResize();
    assert.equal(followed, 1);
});


test("plain-text assistant replies finish without tool history", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    let textFinished = false;
    let streamingRemoved = false;
    panel.finishActiveTextSegment = () => {
        textFinished = true;
    };
    const message = {
        toolHistory: null,
        article: {
            classList: {
                remove(name) {
                    assert.equal(name, "streaming");
                    streamingRemoved = true;
                },
            },
        },
    };

    assert.doesNotThrow(() => panel.finishAssistantMessage(message));
    assert.equal(textFinished, true);
    assert.equal(streamingRemoved, true);
});


test("subscription model changes persist before status refresh can roll them back", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    const calls = [];
    Object.assign(panel, {
        settings: { model: "sonnet" },
        status: { model: "sonnet" },
        modelInput: { value: "sonnet" },
        subscriptionModelSelect: { value: "opus", disabled: false },
        renderReasoningControls: () => {},
        updateModelSettingsState: () => {},
        updateStatus: () => calls.push(["status", panel.status.model]),
        updateProviderBadge: () => {},
        announce: message => calls.push(["announce", message]),
        showError: error => assert.fail(error),
        chat: {
            async updateSettings(update) {
                calls.push(["update", update]);
                assert.equal(panel.pendingSubscriptionModel, "opus");
                return { model: update.model };
            },
        },
    });

    await panel.selectSubscriptionModel();

    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
        ["update", { model: "opus" }],
        ["status", "opus"],
        ["announce", "Model changed to opus."],
    ]);
    assert.equal(panel.modelInput.value, "opus");
    assert.equal(panel.settings.model, "opus");
    assert.equal(panel.pendingSubscriptionModel, null);
    assert.equal(panel.subscriptionModelSelect.disabled, false);
});


test("status updates preserve a subscription model while it is being saved", async () => {
    const AssistantPanel = await loadAssistantPanel();
    const panel = Object.create(AssistantPanel.prototype);
    let rendered = false;
    Object.assign(panel, {
        pendingSubscriptionModel: "opus",
        status: { available: true, configured: true, bridgeConnected: true, model: "sonnet" },
        modelInput: { value: "sonnet" },
        statusDot: { className: "" },
        statusCopy: { textContent: "" },
        statusBanner: {
            hidden: false,
            querySelector: () => ({ textContent: "" }),
        },
        statusBannerCopy: { textContent: "" },
        updateDiagnosticsSettingsState: () => {},
        renderProviderControls: () => { rendered = true; },
        updateProviderBadge: () => {},
        refreshCanvasContext: () => {},
    });

    panel.updateStatus();

    assert.equal(panel.modelInput.value, "opus");
    assert.equal(rendered, true);
});
