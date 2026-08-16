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

function workflowDeltaParts(delta) {
    const normalized = delta && typeof delta === "object" ? delta : {};
    return [
        [normalized.created_node_count, "new node"],
        [normalized.updated_node_count, "updated node"],
        [normalized.removed_node_count, "removed node"],
        [normalized.added_edge_count, "added connection"],
        [normalized.removed_edge_count, "removed connection"],
    ]
        .map(([count, label]) => [Number(count || 0), label])
        .filter(([count]) => Number.isFinite(count) && count > 0)
        .map(([count, label]) => plural(count, label));
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
        needsChoice: 0,
        interrupted: 0,
    };
    for (const step of entries) {
        const status = String(step?.status || "").toLowerCase();
        if (status === "running") counts.running += 1;
        else if (["failed", "error"].includes(status)) counts.failed += 1;
        else if (status === "needs_choice") counts.needsChoice += 1;
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
    const needsChoice = step?.status === "needs_choice"
        || (failed && result?.needs_choice === true);
    if (needsChoice) {
        const choiceLabels = {
            view_node_mask: "Choose the exact image node to inspect",
            view_prompt_reference_image: "Choose the exact reference image route",
            update_connected_prompt: "Choose the exact prompt target",
        };
        return choiceLabels[name] || `${config.label || name || "Action"} needs your choice`;
    }
    if (failed) {
        const failureLabels = {
            view_output_image: "Couldn’t review output image",
            view_chat_image: "Couldn’t inspect attached image",
            view_canvas_images: "Couldn’t inspect canvas images",
            place_chat_image_in_node: "Couldn’t place attached image",
            view_node_mask: "Couldn’t inspect image for masking",
            view_prompt_reference_image: "Couldn’t inspect reference image",
            update_connected_prompt: "Couldn’t update connected prompt",
            edit_node_mask: "Couldn’t update image mask",
            confirm_mask_review: "Mask needs changes",
            web_search: "Couldn’t search the web",
            web_fetch_page: "Couldn’t read the web page",
            compile_workflow_spec: "Couldn’t compile workflow",
            compile_workflow_refinement_spec: "Couldn’t plan workflow",
            apply_workflow_graph_patch: "Couldn’t build workflow",
            workflow_branches_discover: "Couldn’t discover workflow branches",
            workflow_branch_compare: "Couldn’t compare workflow branches",
            workflow_branch_navigate: "Couldn’t focus workflow branch",
            compile_workflow_branch_operation: "Couldn’t plan branch change",
            resolve_workflow_branch_successor: "Couldn’t resolve branch lineage",
            plan_workflow: "Couldn’t validate workflow plan",
            registry_search_packages: "Couldn’t search official Comfy Registry",
            registry_get_package: "Couldn’t inspect Registry package",
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
    if (name === "view_canvas_images") {
        const returned = Number(result?.returned_count);
        const total = Number(result?.total_count);
        if (Number.isInteger(returned) && Number.isInteger(total)) {
            return result?.has_more
                ? `Inspected ${returned} of ${total} canvas images`
                : `Inspected all ${total} canvas images`;
        }
        return "Inspected canvas images";
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
        const summary = `Inspected ${node} for masking`;
        return coverage ? `${summary} · ${coverage}` : summary;
    }
    if (name === "view_prompt_reference_image") {
        const input = result?.consumer_input || "reference input";
        return `Inspected image connected to ${input}`;
    }
    if (name === "update_connected_prompt") {
        const node = result?.producer_node_id !== undefined
            ? `node ${result.producer_node_id}`
            : "connected prompt";
        return `Updated exact prompt on ${node}`;
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
    if (name === "apply_workflow_plan") {
        const count = Number(result?.node_count ?? result?.plan?.nodes?.length ?? 0);
        if (result?.success && result?.already_applied) {
            return `Workflow already applied · ${plural(count, "node")}`;
        }
        if (result?.success) return `Applied workflow plan · ${plural(count, "node")}`;
        if (result?.rollback?.attempted && result?.rollback?.complete) {
            return `Workflow apply failed · rolled back ${plural(result.rollback.attempted_node_ids?.length || 0, "node")}`;
        }
        if (result?.rollback?.attempted) return "Workflow apply failed · rollback incomplete";
        return "Workflow plan not applied";
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
    if (name === "node_knowledge_search") {
        const count = Number.isFinite(result?.count) ? result.count : 0;
        return `Searched node knowledge · ${plural(count, "match", "matches")}`;
    }
    if (name === "node_library_status") {
        const count = result?.catalog?.node_count;
        if (Number.isFinite(count)) {
            const label = count === 1 ? "loaded node" : "loaded nodes";
            return `Cataloged ${count.toLocaleString("en-US")} ${label}`;
        }
        return "Checked loaded-node catalog";
    }
    if (name === "plan_workflow") {
        const nodeCount = Array.isArray(result?.plan?.nodes)
            ? result.plan.nodes.length
            : 0;
        const errorCount = Number(result?.error_count || 0);
        if (result?.valid === false) {
            return `Plan needs fixes · ${plural(errorCount, "error")}`;
        }
        return `Validated workflow plan · ${plural(nodeCount, "node")}`;
    }
    if (name === "plan_workflow_refinement") {
        if (result?.valid === false) {
            return `Refinement needs fixes · ${plural(Number(result?.error_count || 0), "error")}`;
        }
        const operation = result?.plan?.operation || "change";
        return `Planned workflow ${operation}`;
    }
    if (name === "apply_workflow_refinement") {
        if (result?.success === false) return "Workflow refinement failed safely";
        if (result?.already_applied) return "Workflow refinement already applied";
        return `Refined workflow · ${result?.operation || "updated graph"}`;
    }
    if (name === "compile_workflow_refinement_spec") {
        if (result?.valid === false) {
            if (result?.needs_choice) return "Workflow plan needs your choice";
            return `Workflow plan needs fixes · ${plural(Number(result?.error_count || 0), "error")}`;
        }
        const delta = result?.plan?.expected_delta || {};
        const parts = workflowDeltaParts(delta);
        return parts.length > 0
            ? `Planned workflow · ${parts.join(" · ")}`
            : "Planned workflow";
    }
    if (name === "apply_workflow_graph_patch") {
        if (result?.success === false) return "Workflow build failed safely";
        if (result?.already_applied) return "Workflow change already applied";
        const declared = request?.plan?.expected_delta || result?.expected_delta;
        const fallback = {
            created_node_count: Array.isArray(result?.created_node_ids)
                ? result.created_node_ids.length
                : 0,
            removed_node_count: Array.isArray(result?.removed_node_ids)
                ? result.removed_node_ids.length
                : 0,
        };
        const parts = workflowDeltaParts(declared || fallback);
        return parts.length > 0
            ? `Built workflow · ${parts.join(" · ")}`
            : "Built workflow";
    }
    if (name === "workflow_branches_discover") {
        const resolution = result?.resolution || {};
        if (resolution.status === "needs_choice") return "Branch match needs your choice";
        if (["stale", "invalid_catalog"].includes(resolution.status)) {
            return "Branch discovery stopped safely";
        }
        if (resolution.status === "not_found") return "No matching workflow branch";
        if (resolution.status === "resolved") {
            const count = Number(result?.selected_branch?.selectable_node_ids?.length ?? 0);
            return `Found workflow branch · ${plural(count, "node")}`;
        }
        const count = Number(resolution.candidate_count ?? result?.summary?.branch_count ?? 0);
        return `Listed ${plural(count, "workflow branch", "workflow branches")}`;
    }
    if (name === "workflow_branch_compare") {
        if (result?.status !== "compared") return "Branch comparison stopped safely";
        const structure = result?.structurally_equal === true
            ? "same structure"
            : result?.structurally_equal === false
                ? "different structure"
                : "structure unavailable";
        const values = result?.value_equal === true
            ? "same values"
            : result?.value_equal === false
                ? "different values"
                : "values protected";
        return `Compared workflow branches · ${structure} · ${values}`;
    }
    if (name === "workflow_branch_navigate") {
        if (result?.success === false) return "Branch navigation stopped safely";
        const count = Number(result?.selected_count ?? result?.selected_node_ids?.length ?? 0);
        return `Focused workflow branch · ${plural(count, "node")}`;
    }
    if (name === "compile_workflow_branch_operation") {
        if (result?.valid === false) {
            if (result?.needs_choice) return "Branch change needs your choice";
            return `Branch change needs fixes · ${plural(Number(result?.error_count || 0), "error")}`;
        }
        const operation = String(result?.operation || "change");
        const parts = workflowDeltaParts(result?.plan?.expected_delta || {});
        return parts.length > 0
            ? `Planned branch ${operation} · ${parts.join(" · ")}`
            : `Planned branch ${operation}`;
    }
    if (name === "resolve_workflow_branch_successor") {
        if (result?.valid !== true) return "Branch lineage stopped safely";
        const count = Array.isArray(result?.successor_branch_ids)
            ? result.successor_branch_ids.length
            : 0;
        if (count === 0) return "Confirmed branch removal · no successor branches";
        return `Resolved ${plural(count, "successor branch", "successor branches")}`;
    }
    if (name === "compile_workflow_spec") {
        const nodeCount = Array.isArray(result?.plan?.nodes)
            ? result.plan.nodes.length
            : 0;
        const errorCount = Number(result?.error_count || 0);
        if (result?.valid === false) {
            return `Workflow needs fixes · ${plural(errorCount, "error")}`;
        }
        return `Compiled workflow · ${plural(nodeCount, "node")}`;
    }
    if (name === "registry_search_packages") {
        if (result?.ok === false) return "Registry search unavailable";
        const count = Number.isFinite(result?.count)
            ? result.count
            : Array.isArray(result?.results)
                ? result.results.length
                : null;
        return count === null
            ? "Searched official Comfy Registry"
            : `Found ${plural(count, "Registry package")}`;
    }
    if (name === "registry_get_package") {
        if (result?.ok === false) return "Registry package unavailable";
        const packageResult = result?.package || result;
        const packageName = String(
            packageResult?.name
            || packageResult?.display_name
            || packageResult?.id
            || request?.package_id
            || "Registry package"
        ).trim();
        return `Inspected ${packageName} on Registry`;
    }
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
