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


test("run identity remains available across CORS and provider startup", async () => {
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");
    const runtime = await readFile(new URL("backend/chat_runtime.py", root), "utf8");
    const server = await readFile(new URL("backend/server.py", root), "utf8");

    assert.match(client, /event\.type === "RUN_STARTED"/);
    assert.match(client, /markRunReady\(event\.runId, event\.threadId\)/);
    assert.match(runtime, /Publish before provider setup/);
    assert.match(runtime, /Codex timed out while connecting to the Ren MCP tools/);
    assert.match(server, /expose_headers=/);
});


test("atomic workflow application is registered and suppresses auto-queue", async () => {
    const executor = await readFile(new URL("web/js/tool_executor.js", root), "utf8");

    assert.match(executor, /"apply_workflow_plan": this\._handleApplyWorkflowPlan/);
    assert.match(executor, /const autoQueueState = this\.flApi\.pauseAutoQueue\(\)/);
    assert.match(executor, /this\.flApi\.restoreAutoQueue\(autoQueueState\)/);
    assert.match(executor, /await applyWorkflowPlanAtomic\(params, adapter\)/);
    assert.match(executor, /preferred_size:\s*\{ width: 420, height: 340 \}/);
    assert.match(executor, /node:\s*500/);
    assert.match(executor, /connection:\s*500/);
    assert.match(executor, /WORKFLOW_REVEAL_DELAYS_MS\[step\?\.phase\]/);
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


test("settings use a compact single-open accordion with live state", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /<h2 id="fl-settings-title">Settings<\/h2>/);
    assert.match(panel, /fl-settings-card-model/);
    assert.match(panel, /<h3 id="fl-settings-model-title">Connection<\/h3>/);
    assert.match(panel, /fl-settings-card-search/);
    assert.match(panel, /Free · no cost/);
    assert.match(panel, /data-settings-state="search"/);
    assert.match(panel, /data-setting="search_mode"/);
    assert.match(panel, /data-setting="show_action_buttons"/);
    assert.match(panel, /data-setting="tavily_credential"/);
    assert.match(panel, /fl-settings-card-approvals/);
    assert.match(panel, /<h3 id="fl-settings-approvals-title">Permissions<\/h3>/);
    assert.match(panel, /<div class="fl-settings-group-label">Advanced<\/div>/);
    assert.match(panel, /fl-settings-card-bridge/);
    assert.match(panel, /Bridge &amp; safety/);
    assert.match(panel, /data-bridge-setting="ws_port"/);
    assert.match(panel, /data-bridge-setting="enable_workflow_writes"/);
    assert.match(panel, /Workflow writes<\/strong><span>Enabled by default\./);
    assert.match(panel, /data-bridge-setting="wait_for_generation_completion"/);
    assert.match(panel, /data-bridge-setting="generation_completion_timeout"/);
    assert.match(panel, /saveBridgeSettings/);
    assert.match(panel, /pendingRestartFields/);
    assert.match(
        panel,
        /<details class="fl-settings-card fl-settings-disclosure fl-settings-card-model"[^>]*\sopen>/,
    );
    assert.equal(
        panel.match(/<details class="fl-settings-card fl-settings-disclosure/g)?.length,
        5,
    );
    assert.equal(panel.match(/data-settings-section="[^"]+" open/g)?.length, 1);
    assert.match(panel, /data-settings-state="model"/);
    assert.match(panel, /data-settings-state="approvals"/);
    assert.match(panel, /data-settings-state="diagnostics"/);
    assert.match(panel, /this\.settingsDisclosures/);
    assert.match(panel, /openSettingsSection\(section\)/);
    assert.match(panel, /handleSettingsDisclosureToggle\(disclosure\)/);
    assert.match(panel, /data-section="model"/);
    assert.match(panel, /updateModelSettingsState/);
    assert.match(panel, /updateDiagnosticsSettingsState/);
    assert.match(styles, /\.fl-settings-content\s*\{[^}]*gap:\s*6px/s);
    assert.match(styles, /\.fl-settings-card\s*\{[^}]*border-radius:\s*9px[^}]*box-shadow:\s*none/s);
    assert.match(styles, /\.fl-settings-card-header\s*\{[^}]*min-height:\s*48px/s);
    assert.match(styles, /\.fl-settings-state::before\s*\{[^}]*border-radius:\s*50%/s);
    assert.match(styles, /\.fl-bridge-settings-grid\s*\{[^}]*display:\s*grid/s);
    assert.match(styles, /\.fl-bridge-settings-group\s*\{[^}]*border-top:\s*1px solid var\(--ren-border\)/s);
    assert.match(styles, /\.fl-mcp-metric\s*\{[^}]*grid-template-columns:\s*72px minmax\(0, 1fr\)/s);
    assert.match(styles, /\.fl-settings-disclosure > summary\s*\{[^}]*list-style:\s*none/s);
    assert.match(
        styles,
        /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.fl-settings-chevron/,
    );
});


test("bridge settings use ComfyUI-side JSON endpoints", async () => {
    const extension = await readFile(new URL("web/js/extension.js", root), "utf8");
    const backend = await readFile(new URL("__init__.py", root), "utf8");
    const config = await readFile(new URL("backend/config.py", root), "utf8");

    assert.match(extension, /fetchJson\("\/fl_mcp\/settings"/);
    assert.match(extension, /method: "PATCH"/);
    assert.match(backend, /routes\.get\("\/fl_mcp\/settings"\)/);
    assert.match(backend, /routes\.patch\("\/fl_mcp\/settings"\)/);
    assert.match(config, /bridge_settings\.json/);
    assert.match(config, /dotenv_values/);
    assert.doesNotMatch(config, /pydantic_settings|BaseSettings/);
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


test("tool calls use a compact summary with lazy vertical per-call cards", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const runtime = await readFile(new URL("backend/chat_runtime.py", root), "utf8");

    assert.match(panel, /className = "fl-message-timeline"/);
    assert.match(panel, /renderPersistedAssistantTimeline/);
    assert.match(panel, /appendAssistantDelta/);
    assert.match(panel, /toolRailAtCursor/);
    assert.match(panel, /renderToolHistory/);
    assert.match(panel, /renderToolHistoryCards/);
    assert.match(panel, /createToolHistoryCard/);
    assert.match(panel, /renderToolHistoryCard/);
    assert.match(panel, /summarizeToolStep/);
    assert.doesNotMatch(panel, /groupToolSteps/);
    assert.match(panel, /TOOL_HISTORY_INITIAL_STEPS = 60/);
    assert.match(panel, /history\.steps\.push\(step\)/);
    assert.match(panel, /history\.cards\.get\(step\)/);
    assert.match(panel, /history\.steps\.slice\(firstVisible\)/);
    assert.match(panel, /card\.setAttribute\("role", "listitem"\)/);
    assert.match(panel, /fl-tool-history-icon/);
    assert.match(panel, /tool\.iconClass \|\| "pi pi-cog"/);
    assert.match(panel, /fl-toolchain-active-icon/);
    assert.match(panel, /event\.content/);
    assert.match(runtime, /"contentOffset": len\(state\.assistant_text\)/);
    assert.match(runtime, /normalize_assistant_timeline/);
});


test("web and generated images render in ordered chat galleries", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const helpers = await readFile(new URL("web/js/chat_ui_helpers.js", root), "utf8");
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");
    const markdown = await readFile(new URL("web/js/safe_markdown.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");
    const routes = await readFile(new URL("backend/chat_routes.py", root), "utf8");

    assert.match(panel, /renderToolImages/);
    assert.match(panel, /MAX_TOOL_GALLERY_IMAGES = 12/);
    assert.match(panel, /grid\.dataset\.layout = images\.length === 1/);
    assert.match(panel, /image\.sourceUrl \|\| image\.url/);
    assert.match(panel, /preview\.referrerPolicy = "no-referrer"/);
    assert.match(helpers, /export function toolDisplayImages/);
    assert.match(client, /api\/chat\/web-images\/preview/);
    assert.match(panel, /resolveImageUrl: url => this\.chat\.webImagePreviewUrl\(url\)/);
    assert.match(markdown, /parseMarkdownImageLine/);
    assert.match(markdown, /fl-chat-image-grid fl-image-grid/);
    assert.match(routes, /WebImagePreviewService/);
    assert.match(routes, /WebImagePreviewService\(max_dimension=192\)/);
    assert.match(styles, /\.fl-image-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,/s);
    assert.match(styles, /data-layout="hero"[^}]*first-child/s);
    assert.match(styles, /object-fit:\s*contain/);
    assert.match(
        styles,
        /\.fl-tool-image-grid\[data-layout\]\s*\{[^}]*repeat\(auto-fill, minmax\(96px, 128px\)\)/s,
    );
    assert.match(
        styles,
        /\.fl-tool-image-grid \.fl-tool-image-card\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent/s,
    );
    assert.match(
        styles,
        /\.fl-tool-image-grid\[data-layout="single"\] \.fl-tool-image-card a\s*\{[^}]*width:\s*fit-content;[^}]*aspect-ratio:\s*auto;[^}]*background:\s*transparent/s,
    );
    assert.match(
        styles,
        /\.fl-tool-image-grid \.fl-tool-image-card img\s*\{[^}]*width:\s*auto;[^}]*height:\s*auto;[^}]*max-height:\s*96px;[^}]*object-fit:\s*contain/s,
    );
});


test("action trail stays compact, visible, and visually quiet when complete", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");
    const tools = await readFile(new URL("web/js/tool_activity.js", root), "utf8");

    assert.match(styles, /\.fl-message-timeline\s*\{[^}]*gap:\s*5px/s);
    assert.match(styles, /\.fl-toolchain-summary\s*\{[^}]*grid-template-columns:/s);
    assert.match(styles, /\.fl-tool-history-list\s*\{[^}]*flex-direction:\s*column/s);
    assert.match(styles, /\.fl-toolchain-crumb summary\s*\{[^}]*grid-template-columns:/s);
    assert.doesNotMatch(styles, /\.fl-tool-history-chip/);
    assert.match(styles, /\.fl-toolchain-breadcrumb\.completed\s*\{[^}]*background:\s*rgba\(255, 255, 255, 0\.018\)/s);
    assert.match(styles, /content-visibility:\s*auto/);
    assert.match(tools, /TOOL_ICON_CLASSES/);
    assert.match(tools, /pi pi-plus-circle/);
    assert.match(tools, /view_node_mask/);
    assert.match(tools, /edit_node_mask/);
    assert.match(tools, /confirm_mask_review/);
    assert.match(panel, /Replace this image mask\?/);
    assert.match(panel, /save a new mask image, and update the selected image node/);
    assert.match(panel, /Use this mask\?/);
    assert.match(panel, /Needs changes/);
    assert.match(panel, /if \(!isMaskReview\) actions\.appendChild\(alwaysAllow\)/);
    assert.match(panel, /Mask approved/);
});


test("mask edits show a live preview and block queueing until review", async () => {
    const api = await readFile(new URL("web/js/fl_api.js", root), "utf8");
    const executor = await readFile(new URL("web/js/tool_executor.js", root), "utf8");
    const extension = await readFile(new URL("web/js/extension.js", root), "utf8");
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const prompt = await readFile(new URL("backend/chat_prompt.md", root), "utf8");
    const editImplementation = api.slice(
        api.indexOf("async editNodeMask"),
        api.indexOf("confirmMaskReview"),
    );
    const previewStart = api.indexOf("async _canvasToImage");
    const previewImplementation = api.slice(
        previewStart,
        api.indexOf("_pauseAutoQueueForMaskReview", previewStart),
    );

    assert.match(api, /node\.imgs = \[reviewPreview\.image\]/);
    assert.match(api, /previewUrl: reviewPreview\.url/);
    assert.match(api, /this\._assignImageToNode\(node, pending\.image\)/);
    assert.match(api, /preview\.src = api\.apiURL\(`\/fl_mcp\/image\/thumbnail\?/);
    assert.match(api, /_releaseMaskReviewPreview/);
    assert.match(api, /discardMaskReviews\(\)/);
    assert.doesNotMatch(editImplementation, /_assignImageToNode/);
    assert.doesNotMatch(editImplementation, /_setWidgetValue/);
    assert.doesNotMatch(previewImplementation, /finally/);
    assert.match(previewImplementation, /catch \(error\)[\s\S]*URL\.revokeObjectURL\(url\)/);
    assert.match(api, /app\.canvas\?\.centerOnNode\?\.\(node\)/);
    assert.match(api, /this\.pendingMaskReviews/);
    assert.match(api, /Mask review required for node/);
    assert.match(api, /_pauseAutoQueueForMaskReview/);
    assert.match(executor, /confirm_mask_review/);
    assert.match(extension, /toolExecutor\.flApi\.discardMaskReviews\(\)/);
    assert.match(panel, /this\.discardMaskReviews\?\.\(\)/);
    assert.match(prompt, /Never queue until the latest mask is approved/);
});


test("composer can steer an active response and exposes real stop progress", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /if \(this\.running\) \{\s*await this\.steer\(/);
    assert.match(panel, /const cancelled = await this\.chat\.cancel\(\);/);
    assert.match(panel, /const activeRunId = this\.chat\.runId \|\| await this\.chat\.runReady;/);
    assert.match(panel, /attachments,\s*activeRunId,/);
    assert.match(panel, /context === this\.currentRunContext && this\.steering/);
    assert.match(panel, /Steer Ren with this message \(Enter\)/);
    assert.match(panel, /Stopping Ren…/);
    assert.match(panel, /fl-run-status-icon/);
    assert.match(panel, /this\.setRunStatus\(toolConfig\.runningLabel, toolConfig\.iconClass\)/);
    assert.match(panel, /setRunStatusForActiveTool/);
    assert.match(panel, /this\.stopButton\.disabled = this\.stopping \|\| this\.steering/);
    assert.match(panel, /this\.textarea\.disabled = false/);
    assert.match(panel, /fl-run-status/);
    assert.match(styles, /\.fl-inline-action:disabled/);
    assert.match(
        styles,
        /\.fl-chat-input-container:focus-within\s*\{[^}]*border-color:\s*rgba\(184, 136, 238, 0\.82\);[^}]*box-shadow:[^}]*0 0 16px rgba\(153, 102, 217, 0\.14\)/s,
    );
});


test("composer defaults to three lines with a rounded focused shell", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /class="fl-chat-input" rows="3"/);
    assert.match(
        styles,
        /\.fl-chat-input-container\s*\{[^}]*margin:\s*0 10px 14px;[^}]*border-radius:\s*12px;/s,
    );
    assert.match(styles, /\.fl-chat-input\s*\{[^}]*min-height:\s*76px;[^}]*padding:\s*7px 4px 12px 10px;/s);
    assert.match(
        styles,
        /\.fl-chat-layout \.fl-chat-input:focus-visible\s*\{[^}]*outline:\s*0;[^}]*box-shadow:\s*none;/s,
    );
});


test("message sending has one attachment-aware run implementation", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const runMethods = panel.match(/^    async runMessage\(/gm) || [];

    assert.equal(runMethods.length, 1);
    assert.match(panel, /const optimisticUser = editMessageId/);
    assert.match(panel, /this\.appendMessage\("user", message, \{ attachments \}\)/);
    assert.match(panel, /searchMode,[\s\S]*attachments,[\s\S]*steerRunId,/);
    assert.match(panel, /onReady: \(\{ runId, conversationId, userMessage \}\)/);
    assert.match(panel, /applyUserMessageMetadata\(article, message\)/);
});


test("chat accepts local images and can place them into a selected image node", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const helpers = await readFile(new URL("web/js/chat_ui_helpers.js", root), "utf8");
    const api = await readFile(new URL("web/js/fl_api.js", root), "utf8");
    const extension = await readFile(new URL("web/js/extension.js", root), "utf8");
    const runtime = await readFile(new URL("backend/chat_runtime.py", root), "utf8");
    const mcp = await readFile(new URL("backend/mcp_server.py", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    assert.match(panel, /data-action="attach-images"/);
    assert.match(panel, /multiple hidden/);
    assert.match(panel, /addEventListener\("paste"/);
    assert.match(panel, /addEventListener\("drop"/);
    assert.match(panel, /pendingAttachments/);
    assert.match(panel, /use-message-attachment/);
    assert.match(panel, /attach-tool-image/);
    assert.match(panel, /use-tool-image/);
    assert.match(panel, /importToolImage/);
    assert.match(panel, /toolImagePreviewSource/);
    assert.match(panel, /\/fl_mcp\/image\/thumbnail\?/);
    assert.match(panel, /previewLink\.href = this\.toolImageOriginalSource\(image\)/);
    assert.match(panel, /fetch\(this\.toolImageImportSource\(image\)\)/);
    assert.match(helpers, /if \(name === "view_chat_image"\) return \[\];/);
    assert.match(api, /api\.fetchApi\("\/upload\/image"/);
    assert.match(api, /formData\.append\("image", file,/);
    assert.match(api, /placeChatImageInNode/);
    assert.match(api, /restoreNestedImageReferences/);
    assert.match(api, /optionValues\.push\(widgetValue\)/);
    assert.match(api, /\/fl_mcp\/image\/thumbnail\?/);
    assert.match(api, /preview\.onload = \(\) => \{[\s\S]*node\.imgs = \[preview\]/);
    assert.match(api, /preview\.onerror = \(\) =>/);
    assert.match(api, /preview\.src = api\.apiURL\(`\/view\?/);
    assert.match(extension, /afterConfigureGraph/);
    assert.match(runtime, /message_content_for_model/);
    assert.match(runtime, /"attachments": normalized_attachments/);
    assert.match(mcp, /async def view_chat_image/);
    assert.match(mcp, /async def place_chat_image_in_node/);
    assert.match(styles, /\.fl-composer-attachments/);
    assert.match(styles, /--fl-attachment-thumb-height:\s*48px/);
    assert.match(styles, /--fl-attachment-thumb-width:\s*68px/);
    assert.match(styles, /\.fl-chat-attachment\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;/s);
    assert.match(styles, /\.fl-chat-attachment img\s*\{[^}]*width:\s*auto;[^}]*height:\s*auto;[^}]*object-fit:\s*contain/s);
    assert.match(styles, /\.fl-chat-input-container\.drag-active/);
});


test("reasoning can be set as a default and overridden in the composer", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");

    assert.match(panel, /data-setting="reasoning_effort"/);
    assert.match(panel, /data-reasoning="composer"/);
    assert.match(panel, /reasoningEffort: this\.composerReasoningSelect\.value/);
    assert.match(panel, /ultra: "Ultra"/);
    assert.match(client, /reasoningEffort: reasoningEffort \|\| "default"/);
});


test("web search can be selected per message and its composer action can be hidden", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");
    const tools = await readFile(new URL("web/js/tool_activity.js", root), "utf8");

    assert.match(panel, /data-search="composer"/);
    assert.match(panel, /Free web/);
    assert.match(panel, /Tavily basic/);
    assert.match(panel, /Tavily deep/);
    assert.match(panel, /searchMode = this\.composerSearchSelect\.value/);
    assert.match(panel, /reasoningEffort: this\.composerReasoningSelect\.value,\s*searchMode,/);
    assert.match(panel, /this\.composerActions\.hidden = !visible/);
    assert.match(panel, /Default search remains active/);
    assert.match(client, /searchMode: searchMode \|\| "free"/);
    assert.match(tools, /web_search: "Searching the web"/);
    assert.match(tools, /web_fetch_page: "Reading web page"/);
});


test("official Registry discovery has clear human tool activity", async () => {
    const tools = await readFile(new URL("web/js/tool_activity.js", root), "utf8");

    assert.match(tools, /"registry_search_packages"\s*:\s*\{/);
    assert.match(tools, /label:\s*"Registry Search"/);
    assert.match(tools, /registry_search_packages:\s*"Searching official Comfy Registry"/);
    assert.match(tools, /"registry_get_package"\s*:\s*\{/);
    assert.match(tools, /label:\s*"Registry Package"/);
    assert.match(tools, /registry_get_package:\s*"Inspecting Registry package"/);
});


test("deterministic workflow planning has clear human tool activity", async () => {
    const tools = await readFile(new URL("web/js/tool_activity.js", root), "utf8");

    assert.match(tools, /"resolve_workflow_spec"\s*:\s*\{/);
    assert.match(tools, /label:\s*"Resolve Capabilities"/);
    assert.match(tools, /resolve_workflow_spec:\s*"Resolving workflow capabilities"/);
    assert.match(tools, /"compile_workflow_spec"\s*:\s*\{/);
    assert.match(tools, /label:\s*"Compile Workflow"/);
    assert.match(tools, /compile_workflow_spec:\s*"Compiling complete workflow"/);
    assert.match(tools, /"plan_workflow"\s*:\s*\{/);
    assert.match(tools, /label:\s*"Validate Plan"/);
    assert.match(tools, /plan_workflow:\s*"Validating workflow plan"/);
    assert.match(tools, /"node_knowledge_search"\s*:\s*\{/);
    assert.match(tools, /label:\s*"Node Knowledge"/);
    assert.match(tools, /node_knowledge_search:\s*"Searching local node knowledge"/);
});


test("workflow queueing preserves ComfyUI frontend authentication", async () => {
    const api = await readFile(new URL("web/js/fl_api.js", root), "utf8");

    assert.match(api, /await app\.queuePrompt\(0, effectiveBatchCount\)/);
    assert.match(api, /const originalQueuePrompt = api\.queuePrompt/);
    assert.match(api, /api\.queuePrompt = originalQueuePrompt/);
    assert.match(api, /partner\/API nodes report "Please login first"/);
});


test("canvas screenshots wait for Fit View and thumbnails to settle", async () => {
    const api = await readFile(new URL("web/js/fl_api.js", root), "utf8");
    const executor = await readFile(new URL("web/js/tool_executor.js", root), "utf8");

    assert.match(api, /await this\.waitForCanvasStable\(\)/);
    assert.match(api, /stableFrames < 3/);
    assert.match(api, /previewsReady\(\)/);
    assert.match(api, /canvas\?\.draw\?\.\(true, true\)/);
    assert.doesNotMatch(
        executor,
        /requestAnimationFrame\(\(\) => requestAnimationFrame\(finish\)\)/,
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


test("sent requests can be edited, resent, and browsed by version", async () => {
    const panel = await readFile(new URL("web/js/chat_panel.js", root), "utf8");
    const client = await readFile(new URL("web/js/chat_client.js", root), "utf8");
    const styles = await readFile(new URL("web/js/style.css", root), "utf8");

    for (const action of [
        "edit-message",
        "resend-message",
        "previous-message-version",
        "next-message-version",
    ]) {
        assert.match(panel, new RegExp(`data\\.action = action|\"${action}\"`));
    }
    assert.match(panel, /Send edited request/);
    assert.match(panel, /editMessageId/);
    assert.match(panel, /selectMessageVersion/);
    assert.match(client, /messages\/\$\{encodeURIComponent\(messageId\)\}\/version/);
    assert.match(styles, /\.fl-message-actions/);
    assert.match(styles, /\.fl-message-edit-form/);
    assert.match(styles, /\.fl-message-versions/);
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
    assert.match(panel, /history && history\.renderFrame !== null/);
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
