const TECHNICAL_DETAIL_LIMIT = 20_000;

function parsePayload(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value !== "string") return value;
    try {
        return JSON.parse(value);
    } catch (_) {
        return value;
    }
}

function countSuccessful(value) {
    const parsed = parsePayload(value);
    const entries = Array.isArray(parsed)
        ? parsed
        : Array.isArray(parsed?.results)
            ? parsed.results
            : null;
    if (!entries) return null;
    return entries.filter(item => item?.success !== false).length;
}

function plural(count, singular, pluralForm = `${singular}s`) {
    return `${count} ${count === 1 ? singular : pluralForm}`;
}

const PROVIDER_MARKS = {
    lmstudio: "LM",
    ollama: "OL",
    openai: "OA",
    openrouter: "OR",
    anthropic: "AN",
    claude_subscription: "CL",
    codex_subscription: "CX",
    custom: "API",
};

export function modelProviderSummary(settings = {}) {
    const providerId = String(settings.provider || "").trim().toLowerCase();
    const preset = settings.presets?.[providerId] || {};
    const providerLabel = String(preset.label || providerId || "Model")
        .replace(/\s+subscription$/i, "")
        .replace(/\s+endpoint$/i, "");
    const modelId = String(settings.model || preset.default_model || "").trim();
    const model = (preset.models || []).find(candidate => candidate.id === modelId);
    const modelLabel = String(model?.label || modelId || "Not selected")
        .replace(/\s+\(recommended\)$/i, "");

    return {
        id: providerId || "unknown",
        mark: PROVIDER_MARKS[providerId] || providerLabel.slice(0, 2).toUpperCase(),
        providerLabel,
        modelLabel,
    };
}

export function isNearBottom(element, threshold = 48) {
    if (!element) return true;
    return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

export function technicalText(value, limit = TECHNICAL_DETAIL_LIMIT) {
    if (value === null || value === undefined || value === "") return "";
    const text = typeof value === "string"
        ? value
        : JSON.stringify(value, null, 2);
    if (text.length <= limit) return text;
    return `${text.slice(0, limit)}\n\n… Result truncated in the interface.`;
}

export function canStackToolSteps(previous, next) {
    const previousName = String(previous?.name || "");
    return Boolean(previousName && previousName === String(next?.name || ""));
}

export function toolStackState(steps = []) {
    const entries = steps.filter(Boolean);
    const categories = [
        { status: "running", values: new Set(["running"]) },
        { status: "failed", values: new Set(["failed", "error"]) },
        { status: "cancelled", values: new Set(["cancelled"]) },
        { status: "retried", values: new Set(["retried"]) },
        { status: "done", values: new Set(["done", "finished"]) },
    ];
    const category = categories.find(({ values }) => (
        entries.some(step => values.has(step.status))
    )) || categories.at(-1);
    const representative = entries.findLast(step => (
        category.values.has(step.status)
    )) || entries.at(-1) || {};
    return {
        count: entries.length,
        status: category.status,
        step: representative,
    };
}

export function summarizeToolStep(step, config = {}) {
    const name = step?.name || "";
    const args = parsePayload(step?.arguments);
    const result = parsePayload(step?.result);
    const failed = ["failed", "error"].includes(step?.status);
    if (failed) return config.failureLabel || `${config.label || name || "Action"} failed`;

    if (name === "create_nodes") {
        const count = countSuccessful(result)
            ?? (Array.isArray(args?.nodes) ? args.nodes.length : null);
        if (count !== null) return `Created ${plural(count, "node")}`;
    }
    if (name === "connect_nodes_batch") {
        const count = countSuccessful(result)
            ?? (Array.isArray(args?.connections) ? args.connections.length : null);
        if (count !== null) return `Connected ${plural(count, "link")}`;
    }
    if (name === "remove_nodes") {
        const count = Array.isArray(args?.node_ids) ? args.node_ids.length : null;
        if (count !== null) return `Removed ${plural(count, "node")}`;
    }
    if (name === "connect_nodes") return "Connected nodes";
    if (name === "set_node_values") {
        const count = Array.isArray(args?.updates)
            ? args.updates.length
            : args?.node_id !== undefined
                ? 1
                : null;
        if (count !== null) return `Updated ${plural(count, "node")}`;
    }
    if (name === "get_node_values") return "Read node values";
    if (name === "get_node_slots") return "Read node slots";
    if (name === "get_current_node_selection") return "Read canvas selection";
    if (name === "select_nodes") {
        const count = Array.isArray(args?.node_ids) ? args.node_ids.length : null;
        if (count !== null) return `Selected ${plural(count, "node")}`;
        return "Updated canvas selection";
    }
    if (name === "find_node") return "Found matching nodes";
    if (name === "focus_on_nodes") return "Focused canvas";
    if (name === "modify_layout") return "Updated workflow layout";
    if (name === "get_layout") return "Inspected workflow layout";
    if (name === "queue_workflow") return "Queued workflow";
    if (name === "take_screenshot") return "Captured canvas";
    if (name === "query_workflow") return "Searched workflow";
    if (name === "workflow_get_current_json") return "Read workflow JSON";
    if (name === "workflow_save_current") return "Saved workflow";
    if (name === "node_library_search") return "Searched node library";
    if (name === "manager_check_updates") return "Checked for custom-node updates";
    if (name === "workflow_overview") {
        const count = result?.node_count ?? result?.nodeCount ?? result?.total_nodes;
        if (Number.isFinite(count)) return `Inspected ${plural(count, "node")}`;
        return "Inspected workflow";
    }
    return config.completedLabel || `${config.label || name || "Action"} completed`;
}

export function starterPrompts(context = {}) {
    if (!context.connected) {
        return [
            "Help me reconnect Ren to the canvas",
            "Explain how the ComfyUI bridge works",
            "Help me configure my model connection",
        ];
    }
    if ((context.selectedCount || 0) > 0) {
        return [
            "Explain the selected nodes",
            "Debug this selected branch",
            "Organize the selected nodes",
        ];
    }
    if ((context.nodeCount || 0) > 0) {
        return [
            "Explain this workflow",
            "Check this workflow for problems",
            "Improve the workflow layout",
        ];
    }
    return [
        "Build a text-to-image workflow",
        "Show me which models are installed",
        "Teach me the ComfyUI basics",
    ];
}
