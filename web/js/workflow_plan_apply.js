/** Deterministic, rollback-safe application of one validated workflow plan. */

import { parseImageWidgetRef } from "./mask_utils.js";

export const WORKFLOW_APPLICATION_PROPERTY = "fl_mcp_workflow_application";
export const WORKFLOW_APPLICATION_SCHEMA = "fl-mcp.workflow-application.v1";


function canonicalValue(value) {
    if (Array.isArray(value)) return value.map(canonicalValue);
    if (value && typeof value === "object") {
        return Object.fromEntries(
            Object.keys(value).sort().map(key => [key, canonicalValue(value[key])]),
        );
    }
    return value;
}


function valuesEqual(left, right) {
    return JSON.stringify(canonicalValue(left)) === JSON.stringify(canonicalValue(right));
}


function imageValuesEqual(left, right) {
    const leftRef = parseImageWidgetRef(left);
    const rightRef = parseImageWidgetRef(right);
    return Boolean(leftRef && rightRef && valuesEqual(leftRef, rightRef));
}


function issue(code, path, message) {
    return { code, path, message };
}


function nodeId(value) {
    return value?.node_id ?? value?.id;
}


function connectionKey(connection) {
    return [
        String(connection.source_node_id),
        String(connection.source_output_index),
        String(connection.target_node_id),
        String(connection.target_input),
    ].join("|");
}


function dynamicInputSequence(targetInput) {
    const match = String(targetInput || "").match(/^(.*?)(?:_|\.)(\d+)$/);
    if (!match) return null;
    return { stem: match[1], index: Number(match[2]) };
}


function applicationNodeOrder(nodes, connections) {
    const byAlias = new Map(nodes.map(node => [node.alias, node]));
    const incoming = new Map(nodes.map(node => [node.alias, 0]));
    const outgoing = new Map(nodes.map(node => [node.alias, new Set()]));
    for (const connection of connections) {
        if (
            connection.source_alias === connection.target_alias
            || !byAlias.has(connection.source_alias)
            || !byAlias.has(connection.target_alias)
            || outgoing.get(connection.source_alias).has(connection.target_alias)
        ) {
            continue;
        }
        outgoing.get(connection.source_alias).add(connection.target_alias);
        incoming.set(connection.target_alias, incoming.get(connection.target_alias) + 1);
    }
    const ready = [...incoming.entries()]
        .filter(([, count]) => count === 0)
        .map(([alias]) => alias)
        .sort();
    const aliases = [];
    while (ready.length > 0) {
        const alias = ready.shift();
        aliases.push(alias);
        for (const target of [...outgoing.get(alias)].sort()) {
            const remaining = incoming.get(target) - 1;
            incoming.set(target, remaining);
            if (remaining === 0) {
                ready.push(target);
                ready.sort();
            }
        }
    }
    if (aliases.length !== nodes.length) return nodes;
    return aliases.map(alias => byAlias.get(alias));
}


/**
 * Keep the canonical plan order except for numbered dynamic inputs on the same
 * target. ComfyUI nodes such as Nano Banana expose image_2 only after image_1
 * is connected, so those sockets must be populated in ascending sequence.
 */
function applicationConnectionOrder(connections) {
    const ordered = [...connections];
    const groups = new Map();
    connections.forEach((connection, position) => {
        const sequence = dynamicInputSequence(connection.target_input);
        if (!sequence) return;
        const key = `${connection.target_alias}\u0000${sequence.stem}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push({ connection, position, index: sequence.index });
    });
    for (const group of groups.values()) {
        if (group.length < 2) continue;
        const positions = group.map(item => item.position);
        const sequenced = [...group].sort((left, right) => left.index - right.index);
        positions.forEach((position, index) => {
            ordered[position] = sequenced[index].connection;
        });
    }
    return ordered;
}


function verifyApplication(plan, planHash, applicationId, adapter, applicationNodes) {
    const issues = [];
    const attachmentInputs = new Set(
        (plan.attachments || []).map(binding => `${binding.node_alias}\u0000${binding.input_name}`),
    );
    const byAlias = new Map();
    for (const node of applicationNodes) {
        const metadata = node.metadata || {};
        const alias = metadata.alias;
        if (!alias) {
            issues.push(issue(
                "application_alias_missing",
                `nodes.${nodeId(node)}`,
                "An application node is missing its semantic alias.",
            ));
            continue;
        }
        if (byAlias.has(alias)) {
            issues.push(issue(
                "application_alias_duplicate",
                `nodes.${alias}`,
                `Application alias ${alias} is present more than once on the canvas.`,
            ));
            continue;
        }
        byAlias.set(alias, node);
    }

    if (applicationNodes.length !== plan.nodes.length) {
        issues.push(issue(
            "application_node_count_mismatch",
            "nodes",
            `Expected ${plan.nodes.length} application nodes but found ${applicationNodes.length}.`,
        ));
    }

    const aliases = {};
    for (const expected of plan.nodes) {
        const observed = byAlias.get(expected.alias);
        if (!observed) {
            issues.push(issue(
                "application_node_missing",
                `nodes.${expected.alias}`,
                `Application node ${expected.alias} is missing from the canvas.`,
            ));
            continue;
        }
        const id = nodeId(observed);
        aliases[expected.alias] = id;
        const metadata = observed.metadata || {};
        if (
            metadata.schema !== WORKFLOW_APPLICATION_SCHEMA
            || metadata.application_id !== applicationId
            || metadata.plan_hash !== planHash
        ) {
            issues.push(issue(
                "application_identity_mismatch",
                `nodes.${expected.alias}.metadata`,
                `Application metadata for ${expected.alias} does not match this request.`,
            ));
        }
        if (observed.node_type !== expected.node_type) {
            issues.push(issue(
                "application_node_type_mismatch",
                `nodes.${expected.alias}.node_type`,
                `Expected ${expected.node_type} but found ${observed.node_type}.`,
            ));
        }
        if (metadata.schema_hash !== expected.schema_hash) {
            issues.push(issue(
                "application_schema_hash_mismatch",
                `nodes.${expected.alias}.schema_hash`,
                `The applied schema identity for ${expected.alias} changed.`,
            ));
        }
        const observedValues = adapter.getNodeValues(id);
        for (const [name, value] of Object.entries(expected.values || {})) {
            const attachmentBound = attachmentInputs.has(`${expected.alias}\u0000${name}`);
            if (
                !Object.prototype.hasOwnProperty.call(observedValues, name)
                || !(attachmentBound
                    ? imageValuesEqual(observedValues[name], value)
                    : valuesEqual(observedValues[name], value))
            ) {
                issues.push(issue(
                    "application_widget_mismatch",
                    `nodes.${expected.alias}.values.${name}`,
                    `Widget ${expected.alias}.${name} does not match the validated value.`,
                ));
            }
        }
    }

    for (const connection of plan.connections) {
        const sourceId = aliases[connection.source_alias];
        const targetId = aliases[connection.target_alias];
        if (sourceId === undefined || targetId === undefined) continue;
        if (!adapter.connectionExists(sourceId, targetId, connection)) {
            issues.push(issue(
                "application_connection_missing",
                `connections.${connection.source_alias}.${connection.target_alias}.${connection.target_input}`,
                `Connection ${connection.source_alias}.${connection.source_output} → `
                    + `${connection.target_alias}.${connection.target_input} is missing or different.`,
            ));
        }
    }


    const observedConnections = adapter.listConnections(Object.values(aliases));
    const plannedKeys = new Set(
        plan.connections
            .filter(connection => (
                aliases[connection.source_alias] !== undefined
                && aliases[connection.target_alias] !== undefined
            ))
            .map(connection => connectionKey({
                source_node_id: aliases[connection.source_alias],
                source_output_index: connection.source_output_index,
                target_node_id: aliases[connection.target_alias],
                target_input: connection.target_input,
            })),
    );
    for (const observed of observedConnections) {
        const key = connectionKey(observed);
        if (!plannedKeys.has(key)) {
            issues.push(issue(
                "application_connection_unexpected",
                "connections",
                `Unexpected connection ${observed.source_node_id}.${observed.source_output_index} → `
                    + `${observed.target_node_id}.${observed.target_input || observed.target_input_index} `
                    + "touches the applied subgraph.",
            ));
        }
    }

    return { valid: issues.length === 0, aliases, issues };
}


function rollbackCreatedNodes(createdIds, adapter) {
    const errors = [];
    try {
        adapter.removeNodes([...createdIds].reverse());
    } catch (error) {
        errors.push(String(error?.message || error));
    }
    const remainingIds = createdIds.filter(id => {
        try {
            return adapter.nodeExists(id);
        } catch (error) {
            errors.push(String(error?.message || error));
            return true;
        }
    });
    return {
        attempted: createdIds.length > 0,
        complete: remainingIds.length === 0,
        attempted_node_ids: [...createdIds],
        remaining_node_ids: remainingIds,
        errors,
    };
}


/**
 * Apply one canonical planner result through a canvas adapter.
 * The function never throws after mutation begins; failures include rollback state.
 */
export async function applyWorkflowPlanAtomic(request, adapter) {
    const { plan, plan_hash: planHash, application_id: applicationId } = request || {};
    if (
        !plan
        || !Array.isArray(plan.nodes)
        || !Array.isArray(plan.connections)
        || !/^[a-f0-9]{64}$/.test(String(planHash || ""))
        || !/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(String(applicationId || ""))
    ) {
        return {
            success: false,
            applied: false,
            already_applied: false,
            application_schema: WORKFLOW_APPLICATION_SCHEMA,
            error: { code: "invalid_application_payload", message: "A canonical workflow plan is required." },
            rollback: { attempted: false, complete: true, attempted_node_ids: [], remaining_node_ids: [], errors: [] },
            queued: false,
        };
    }

    const existing = adapter.findApplicationNodes(applicationId);
    if (existing.length > 0) {
        const verification = verifyApplication(
            plan,
            planHash,
            applicationId,
            adapter,
            existing,
        );
        if (verification.valid) {
            return {
                success: true,
                applied: false,
                already_applied: true,
                application_schema: WORKFLOW_APPLICATION_SCHEMA,
                application_id: applicationId,
                plan_hash: planHash,
                aliases: verification.aliases,
                node_count: plan.nodes.length,
                connection_count: plan.connections.length,
                attachment_count: (plan.attachments || []).length,
                verification,
                rollback: { attempted: false, complete: true, attempted_node_ids: [], remaining_node_ids: [], errors: [] },
                queued: false,
            };
        }
        return {
            success: false,
            applied: false,
            already_applied: false,
            application_schema: WORKFLOW_APPLICATION_SCHEMA,
            application_id: applicationId,
            plan_hash: planHash,
            error: {
                code: "idempotency_conflict",
                message: "This application ID already exists but does not match the validated plan.",
            },
            verification,
            rollback: { attempted: false, complete: true, attempted_node_ids: [], remaining_node_ids: [], errors: [] },
            queued: false,
        };
    }

    const createdIds = [];
    const createdNodes = [];
    const aliases = {};
    const connectionResults = [];
    const attachmentResults = [];
    try {
        for (const plannedNode of applicationNodeOrder(plan.nodes, plan.connections)) {
            const created = adapter.createNode(plannedNode);
            const id = nodeId(created);
            if (id === undefined || id === null) {
                throw new Error(`Creating ${plannedNode.alias} did not return a node ID.`);
            }
            createdIds.push(id);
            aliases[plannedNode.alias] = id;
            adapter.setNodeMetadata(id, {
                schema: WORKFLOW_APPLICATION_SCHEMA,
                application_id: applicationId,
                plan_hash: planHash,
                alias: plannedNode.alias,
                schema_hash: plannedNode.schema_hash,
            });
            createdNodes.push({
                alias: plannedNode.alias,
                node_id: id,
                node_type: plannedNode.node_type,
                position: created.position || null,
                size: created.size || null,
            });
            if (typeof adapter.afterMutationStep === "function") {
                await adapter.afterMutationStep({
                    phase: "node",
                    alias: plannedNode.alias,
                    node_id: id,
                });
            }
        }

        for (const binding of plan.attachments || []) {
            const targetId = aliases[binding.node_alias];
            if (targetId === undefined) {
                throw new Error(`Attachment target ${binding.node_alias} was not created.`);
            }
            if (typeof adapter.assignAttachment !== "function") {
                throw new Error("The canvas adapter cannot assign validated chat attachments.");
            }
            const assigned = adapter.assignAttachment(targetId, binding);
            attachmentResults.push({
                node_alias: binding.node_alias,
                node_id: targetId,
                input_name: binding.input_name,
                image: binding.image,
                assigned,
            });
        }

        for (const connection of applicationConnectionOrder(plan.connections)) {
            const sourceId = aliases[connection.source_alias];
            const targetId = aliases[connection.target_alias];
            const connected = adapter.connectNodes(sourceId, targetId, connection);
            connectionResults.push({
                source_alias: connection.source_alias,
                source_node_id: sourceId,
                source_output: connection.source_output,
                source_output_index: connection.source_output_index,
                target_alias: connection.target_alias,
                target_node_id: targetId,
                target_input: connection.target_input,
                connection: connected,
            });
            if (typeof adapter.afterMutationStep === "function") {
                await adapter.afterMutationStep({
                    phase: "connection",
                    source_alias: connection.source_alias,
                    target_alias: connection.target_alias,
                    target_input: connection.target_input,
                });
            }
        }

        const applicationNodes = adapter.findApplicationNodes(applicationId);
        const verification = verifyApplication(
            plan,
            planHash,
            applicationId,
            adapter,
            applicationNodes,
        );
        if (!verification.valid) {
            const error = new Error("Post-apply verification did not match the validated plan.");
            error.code = "post_apply_verification_failed";
            error.verification = verification;
            throw error;
        }

        return {
            success: true,
            applied: true,
            already_applied: false,
            application_schema: WORKFLOW_APPLICATION_SCHEMA,
            application_id: applicationId,
            plan_hash: planHash,
            aliases,
            nodes: createdNodes,
            connections: connectionResults,
            attachments: attachmentResults,
            node_count: createdNodes.length,
            connection_count: connectionResults.length,
            attachment_count: attachmentResults.length,
            verification,
            rollback: { attempted: false, complete: true, attempted_node_ids: [], remaining_node_ids: [], errors: [] },
            queued: false,
        };
    } catch (error) {
        const rollback = rollbackCreatedNodes(createdIds, adapter);
        return {
            success: false,
            applied: false,
            already_applied: false,
            application_schema: WORKFLOW_APPLICATION_SCHEMA,
            application_id: applicationId,
            plan_hash: planHash,
            aliases,
            error: {
                code: error?.code || "workflow_application_failed",
                message: String(error?.message || error),
            },
            verification: error?.verification || { valid: false, aliases, issues: [] },
            rollback,
            queued: false,
        };
    }
}
