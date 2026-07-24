import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


const root = new URL("../../", import.meta.url);


test("sidebar keeps the stable Ren entry and reads live canvas context", async () => {
    const extension = await readFile(new URL("web/js/extension.js", root), "utf8");

    assert.match(extension, /id: "fl_mcp_bridge"/);
    assert.match(extension, /title: "Ren"/);
    assert.match(extension, /icon: "pi pi-comments"/);
    assert.match(extension, /getCanvasContext/);
    assert.match(extension, /selectedNodeCount/);
    assert.match(extension, /subscribeCanvasContext/);
    assert.match(extension, /installRenAwareFitView\(app\)/);
});


test("chat shell has compact two-row chrome and full-panel sheets", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");

    for (const landmark of [
        "fl-chat-topbar",
        "fl-chat-header",
        "fl-conversation-bar",
        "fl-chat-messages",
        "fl-chat-bottombar",
        "fl-chat-input-container",
        'data-sheet="history"',
        'data-sheet="settings"',
        "fl-live-region",
    ]) {
        assert.ok(panel.includes(landmark), `missing Ren landmark: ${landmark}`);
    }
    assert.match(panel, /openSheet/);
    assert.match(panel, /focusableElements/);
    assert.match(panel, /event\.key === "Escape"/);
    assert.match(panel, /<div class="fl-chat-title">MCP<\/div>/);
    assert.match(panel, /class="fl-conversation-title"[^>]*aria-label="Open chat history"[^>]*>\s*<span>History<\/span>/);
    assert.match(panel, /aria-label="Chat status"/);
    assert.doesNotMatch(panel, /fl-provider-toggle|fl-comfy-bar|openDrawer/);
});


test("settings use defined cards with live state and collapsed diagnostics", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /<h2 id="fl-settings-title">Settings<\/h2>/);
    assert.match(panel, /fl-settings-card-model/);
    assert.match(panel, /Model &amp; provider/);
    assert.match(panel, /fl-settings-card-approvals/);
    assert.match(panel, /Tool approvals/);
    assert.match(
        panel,
        /<details class="fl-settings-card fl-settings-disclosure fl-settings-card-diagnostics"/,
    );
    assert.doesNotMatch(
        panel,
        /<details class="fl-settings-card fl-settings-disclosure fl-settings-card-diagnostics"[^>]*\sopen/,
    );
    assert.match(panel, /data-settings-state="model"/);
    assert.match(panel, /data-settings-state="approvals"/);
    assert.match(panel, /data-settings-state="diagnostics"/);
    assert.match(panel, /target instanceof HTMLDetailsElement/);
    assert.match(panel, /target\.open = true/);
    assert.match(panel, /updateModelSettingsState/);
    assert.match(panel, /updateDiagnosticsSettingsState/);
    assert.match(styles, /\.fl-settings-content\s*\{[^}]*gap:\s*10px/s);
    assert.match(styles, /\.fl-settings-card\s*\{[^}]*border-radius:\s*10px/s);
    assert.match(styles, /\.fl-settings-disclosure > summary\s*\{[^}]*list-style:\s*none/s);
    assert.match(
        styles,
        /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.fl-settings-chevron/,
    );
});


test("top bar identifies the active provider and model", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /class="fl-provider-badge"/);
    assert.match(panel, /modelProviderSummary/);
    assert.match(panel, /updateProviderBadge/);
    assert.match(panel, /Using \$\{description\}\. Open settings\./);
    assert.match(styles, /\.fl-provider-badge\s*\{[^}]*max-width:\s*148px/s);
    assert.match(styles, /\.fl-provider-mark\s*\{[^}]*width:\s*23px/s);
    assert.match(styles, /\.fl-provider-badge\[data-provider="claude_subscription"\]/);
    assert.match(styles, /\.fl-provider-badge\[data-provider="codex_subscription"\]/);
});


test("only the message viewport and sheet content can scroll vertically", async () => {
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(styles, /\.fl-chat-panel-host\s*\{[^}]*overflow:\s*hidden\s*!important/s);
    assert.match(styles, /\.fl-chat-layout\s*\{[^}]*grid-template-rows:\s*auto minmax\(0, 1fr\) auto/s);
    assert.match(styles, /\.fl-chat-messages\s*\{[^}]*overflow-y:\s*auto/s);
    assert.match(styles, /\.fl-sheet-content\s*\{[^}]*overflow-y:\s*auto/s);
    assert.doesNotMatch(styles, /\.fl-chat-(?:topbar|bottombar)\s*\{[^}]*overflow-y:\s*auto/s);
});

test("fixed chat chrome casts inward depth shadows over the message viewport", async () => {
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(
        styles,
        /\.fl-chat-topbar\s*\{[^}]*box-shadow:\s*0 11px 18px -13px rgba\(0, 0, 0, 0\.82\)/s,
    );
    assert.match(
        styles,
        /\.fl-chat-bottombar\s*\{[^}]*box-shadow:\s*0 -11px 18px -13px rgba\(0, 0, 0, 0\.82\)/s,
    );
    assert.match(styles, /\.fl-jump-latest\s*\{[^}]*margin:\s*6px 0 7px/s);
});


test("tool calls keep chronological placement and one shared renderer", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const runtime = await readFile(new URL("backend/chat_runtime.py", root), "utf8");

    assert.match(panel, /className = "fl-message-timeline"/);
    assert.match(panel, /renderPersistedAssistantTimeline/);
    assert.match(panel, /appendAssistantDelta/);
    assert.match(panel, /toolRailAtCursor/);
    assert.match(panel, /renderToolStep/);
    assert.match(panel, /summarizeToolStep/);
    assert.match(panel, /canStackToolSteps/);
    assert.match(panel, /item\.toolSteps = \[step\]/);
    assert.match(panel, /`×\$\{stack\.count\}`/);
    assert.match(panel, /`Call \$\{index \+ 1\}/);
    assert.match(panel, /event\.content/);
    assert.match(runtime, /"contentOffset": len\(state\.assistant_text\)/);
    assert.match(runtime, /normalize_assistant_timeline/);
});


test("action trail stays compact, visible, and visually quiet when complete", async () => {
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");
    const tools = await readFile(new URL("web/js/tool_activity.js", root), "utf8");

    assert.match(styles, /\.fl-message-timeline\s*\{[^}]*gap:\s*5px/s);
    assert.match(styles, /\.fl-toolchain-breadcrumb\s*\{[^}]*gap:\s*3px/s);
    assert.match(styles, /\.fl-toolchain-crumb summary\s*\{[^}]*min-height:\s*30px/s);
    assert.match(styles, /\.fl-crumb-count\s*\{[^}]*border-radius:\s*999px/s);
    assert.match(styles, /\.fl-toolchain-crumb\.completed\s*\{[^}]*background:\s*rgba\(255, 255, 255, 0\.018\)/s);
    assert.match(tools, /TOOL_ICON_CLASSES/);
    assert.match(tools, /pi pi-plus-circle/);
});


test("composer supports drafting during runs without queueing another message", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /this\.sendButton\.disabled = this\.running/);
    assert.match(panel, /this\.textarea\.disabled = false/);
    assert.match(panel, /if \(!message \|\| this\.running\) return/);
    assert.match(panel, /fl-run-status/);
    assert.doesNotMatch(panel, /messageQueue|queuedMessage/);
    assert.match(
        styles,
        /\.fl-chat-layout \.fl-chat-input:focus-visible\s*\{[^}]*outline:\s*0;[^}]*box-shadow:\s*inset 0 0 0 1px #b888ee/s,
    );
});


test("history uses archive-first deletion and an undo affordance", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const routes = await readFile(new URL("backend/chat_routes.py", root), "utf8");

    assert.match(panel, /historyView === "archived"/);
    assert.match(panel, /archiveConversation/);
    assert.match(panel, /undoArchive/);
    assert.match(panel, /Delete conversation permanently/);
    assert.match(routes, /Archive the conversation before deleting it permanently/);
});


test("smart follow, accessible approvals, and structured recovery are explicit", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");

    assert.match(panel, /isNearBottom\(this\.scrollElement, 48\)/);
    assert.match(panel, /fl-jump-latest/);
    assert.match(panel, /behavior: "smooth"/);
    assert.match(panel, /prefers-reduced-motion: reduce/);
    assert.match(panel, /this\.jumpingToLatest/);
    assert.match(panel, /Allow once/);
    assert.match(panel, /Always allow/);
    assert.match(panel, /data-setting="approval_bypass"/);
    assert.match(panel, /Bypass all approval prompts/);
    assert.match(panel, /setApprovalBypass/);
    assert.match(panel, /clearAlwaysAllowedTools/);
    assert.match(panel, /always_allowed: "Always allowed"/);
    assert.match(panel, /value\.resolution/);
    assert.match(panel, /showRunError/);
    assert.match(panel, /retryLastMessage/);
    assert.match(panel, /navigator\.clipboard\.writeText/);
    assert.doesNotMatch(panel, /cdn\.jsdelivr|innerHTML\s*=\s*(?:content|message)/);
});


test("Claude subscription setup stays separate from API key providers", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");

    assert.match(panel, /Use your Claude subscription/);
    assert.match(panel, /preset\?\.type === "claude_cli"/);
    assert.match(panel, /connectClaudeSubscription/);
    assert.match(panel, /Finish signing in through the Claude Code terminal window/);
    assert.match(client, /\/api\/chat\/claude\/login/);
    assert.match(client, /\/api\/chat\/claude\/refresh/);
});


test("subscription providers use a real model dropdown while APIs stay editable", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");

    assert.match(
        panel,
        /data-setting="model" type="text" list="fl-mcp-model-options"/,
    );
    assert.match(panel, /data-setting="subscription_model"/);
    assert.match(panel, /this\.modelInput\.hidden = isSubscription/);
    assert.match(panel, /this\.subscriptionModelSelect\.hidden = !isSubscription/);
    assert.match(
        panel,
        /this\.modelInput\.value = this\.subscriptionModelSelect\.value/,
    );
});


test("Codex subscription setup stays separate from OpenAI API keys", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");

    assert.match(panel, /Use your Codex subscription/);
    assert.match(panel, /preset\?\.type === "codex_cli"/);
    assert.match(panel, /connectCodexSubscription/);
    assert.match(panel, /Finish signing in through the Codex terminal window/);
    assert.match(client, /\/api\/chat\/codex\/login/);
    assert.match(client, /\/api\/chat\/codex\/refresh/);
});
