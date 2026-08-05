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
    assert.equal(calls[2].target, article);
    assert.equal(calls[2].message.id, "message-1");
    assert.equal(panel.conversationId, "conversation-after");
    assert.equal(panel.currentRunContext.runId, "run-1");
    assert.equal(panel.running, false);
});
