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

function toolResultPayload(value) {
    const parsed = parsePayload(value);
    if (Array.isArray(parsed)) {
        for (const item of parsed) {
            if (!item || typeof item !== "object") continue;
            if (item.type === "text") {
                const textPayload = parsePayload(item.text);
                if (textPayload && typeof textPayload === "object") {
                    return toolResultPayload(textPayload);
                }
            } else if (!item.type) {
                return toolResultPayload(item);
            }
        }
        return parsed;
    }
    if (!parsed || typeof parsed !== "object") return parsed;
    if (parsed.structuredContent !== undefined && parsed.structuredContent !== null) {
        return parsePayload(parsed.structuredContent);
    }
    if (parsed.structured_content !== undefined && parsed.structured_content !== null) {
        return parsePayload(parsed.structured_content);
    }
    if (Array.isArray(parsed.content)) return toolResultPayload(parsed.content);
    return parsed;
}

function publicHttpUrl(value) {
    try {
        const parsed = new URL(String(value || ""));
        return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
    } catch (_) {
        return "";
    }
}

export function toolDisplayImages(step) {
    const name = step?.name || "";
    const result = toolResultPayload(step?.result);
    // The user's message already owns the visible attachment thumbnail. The
    // inspection tool still receives the original image, but must not add a
    // second copy to long chat histories.
    if (name === "view_chat_image") return [];
    let candidates = Array.isArray(result?.displayImages)
        ? result.displayImages
        : [];
    if (name === "web_fetch_page" && Array.isArray(result?.images)) {
        candidates = result.images.map(image => ({ ...image, kind: "web" }));
    } else if (name === "view_output_image" && result?.image) {
        candidates = [{
            ...result.image,
            kind: "comfy",
            title: "Generated output",
            alt: "Generated ComfyUI output",
        }];
    }

    const images = [];
    const seen = new Set();
    for (const candidate of candidates) {
        if (!candidate || typeof candidate !== "object") continue;
        if (candidate.kind === "comfy") {
            const filename = String(candidate.filename || "").trim();
            const subfolder = String(candidate.subfolder || "").trim();
            const type = String(candidate.type || "output").toLowerCase();
            if (!filename || !["input", "output", "temp"].includes(type)) continue;
            const key = `comfy:${type}:${subfolder}:${filename}`;
            if (seen.has(key)) continue;
            seen.add(key);
            images.push({
                kind: "comfy",
                filename,
                subfolder,
                type,
                title: String(candidate.title || "Generated output").trim(),
                alt: String(candidate.alt || candidate.title || "Generated output").trim(),
            });
            continue;
        }
        const url = publicHttpUrl(candidate.url);
        if (!url || seen.has(url)) continue;
        seen.add(url);
        images.push({
            kind: "web",
            url,
            sourceUrl: publicHttpUrl(candidate.source_url || candidate.sourceUrl),
            title: String(candidate.title || "").trim(),
            alt: String(candidate.alt || candidate.title || "Web image").trim(),
            width: Number(candidate.width) || null,
            height: Number(candidate.height) || null,
        });
    }
    return images;
}

function dimensionsLabel(value) {
    const width = Number(value?.width);
    const height = Number(value?.height);
    return Number.isFinite(width) && Number.isFinite(height)
        ? `${width}×${height}`
        : "";
}

function coverageLabel(result) {
    const coverage = Number(result?.mask?.coveragePercent);
    if (!Number.isFinite(coverage)) return "";
    if (coverage === 0) return "empty mask";
    return `${Number(coverage.toFixed(2))}% covered`;
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

export function toolHistorySummary(steps = []) {
    const entries = steps.filter(Boolean);
    const counts = {
        total: entries.length,
        running: 0,
        done: 0,
        retried: 0,
        failed: 0,
        interrupted: 0,
    };
    for (const step of entries) {
        const status = String(step?.status || "").toLowerCase();
        if (status === "running") counts.running += 1;
        else if (["failed", "error"].includes(status)) counts.failed += 1;
        else if (status === "retried") counts.retried += 1;
        else if (["cancelled", "interrupted"].includes(status)) counts.interrupted += 1;
        else counts.done += 1;
    }
    counts.active = entries.findLast(step => step?.status === "running") || null;
    return counts;
}

export function summarizeToolStep(step, config = {}) {
    const name = step?.name || "";
    const args = parsePayload(step?.arguments);
    const request = args?.request && typeof args.request === "object"
        ? args.request
        : args;
    const result = toolResultPayload(step?.result);
    const failed = ["failed", "error"].includes(step?.status);
    if (failed) {
        const failureLabels = {
            view_output_image: "Couldn’t review output image",
            view_chat_image: "Couldn’t inspect attached image",
            place_chat_image_in_node: "Couldn’t place attached image",
            view_node_mask: "Couldn’t inspect image mask",
            edit_node_mask: "Couldn’t update image mask",
            confirm_mask_review: "Mask needs changes",
            web_search: "Couldn’t search the web",
            web_fetch_page: "Couldn’t read the web page",
        };
        return config.failureLabel
            || failureLabels[name]
            || `${config.label || name || "Action"} failed`;
    }

    if (name === "view_output_image") {
        const selected = Number(result?.selectedOutputIndex);
        const available = Number(result?.availableOutputCount);
        const size = dimensionsLabel(result?.originalSize);
        let summary = "Reviewed generated image";
        if (Number.isInteger(selected) && Number.isInteger(available) && available > 0) {
            summary = selected === available - 1
                ? "Reviewed final output"
                : `Reviewed output ${selected + 1} of ${available}`;
        }
        return size ? `${summary} · ${size}` : summary;
    }
    if (name === "view_chat_image") {
        const size = dimensionsLabel(result?.originalSize);
        return size ? `Inspected attached image · ${size}` : "Inspected attached image";
    }
    if (name === "place_chat_image_in_node") {
        const node = result?.title
            || (result?.node_id !== undefined ? `node ${result.node_id}` : "selected node");
        return `Placed attached image in ${node}`;
    }
    if (name === "view_node_mask") {
        const node = result?.title
            || (result?.node_id !== undefined ? `node ${result.node_id}` : "image");
        const coverage = coverageLabel(result);
        const summary = `Inspected mask on ${node}`;
        return coverage ? `${summary} · ${coverage}` : summary;
    }
    if (name === "edit_node_mask") {
        const regions = Array.isArray(request?.regions) ? request.regions : [];
        const operations = new Set(regions.map(region => region?.operation || "paint"));
        let action = "Edited";
        if (request?.clear_existing) action = "Replaced mask with";
        else if (operations.size === 1 && operations.has("paint")) action = "Painted";
        else if (operations.size === 1 && operations.has("erase")) action = "Erased";
        const regionSummary = regions.length
            ? `${action} ${plural(regions.length, "mask region")}`
            : "Updated image mask";
        const coverage = coverageLabel(result);
        return coverage ? `${regionSummary} · ${coverage}` : regionSummary;
    }
    if (name === "confirm_mask_review") return "Mask approved for workflow";

    if (name === "web_search") {
        const count = Array.isArray(result?.results) ? result.results.length : 0;
        const provider = result?.provider === "tavily" ? "Tavily" : "Free web";
        const credits = Number(result?.credits_used ?? result?.creditsUsed ?? 0);
        const summary = `Searched ${provider} · ${plural(count, "source")}`;
        return Number.isFinite(credits) && credits > 0
            ? `${summary} · ${plural(credits, "credit")}`
            : summary;
    }
    if (name === "web_fetch_page") {
        const title = String(result?.title || "web page").trim();
        const length = Number(result?.contentLength);
        const imageCount = Array.isArray(result?.images) ? result.images.length : 0;
        const cacheLabel = result?.fromCache ? " from cache" : "";
        const summary = `Read ${title}${cacheLabel}`;
        const sized = Number.isFinite(length) && length > 0
            ? `${summary} · ${length.toLocaleString("en-US")} chars`
            : summary;
        return imageCount > 0 ? `${sized} · ${plural(imageCount, "image")}` : sized;
    }

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
    if (name === "queue_workflow") {
        const outcomes = {
            completed: "Completed workflow",
            execution_error: "Workflow execution failed",
            cancelled: "Workflow execution cancelled",
            timeout: "Workflow wait timed out",
        };
        return outcomes[result?.status] || "Queued workflow";
    }
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
