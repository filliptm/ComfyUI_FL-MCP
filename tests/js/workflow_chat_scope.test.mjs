import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const root = new URL("../../", import.meta.url);


async function loadToolExecutor() {
    const source = await readFile(new URL("web/js/tool_executor.js", root), "utf8");
    const classStart = source.indexOf("export class ToolExecutor");
    assert.notEqual(classStart, -1);
    const classSource = source
        .slice(classStart)
        .replace("export class ToolExecutor", "class ToolExecutor");
    const context = vm.createContext({
        console: { log() {}, error() {}, warn() {} },
        performance: { now: () => 1 },
    });
    vm.runInContext(
        `${classSource}\nglobalThis.ToolExecutor = ToolExecutor;`,
        context,
    );
    return context.ToolExecutor;
}


test("browser tools fail closed after the active workflow changes", async () => {
    const ToolExecutor = await loadToolExecutor();
    const executor = Object.create(ToolExecutor.prototype);
    const sent = [];
    let handled = false;
    Object.assign(executor, {
        flApi: {
            getActiveWorkflowContext: () => ({ id: "workflow-b", name: "B" }),
        },
        toolHandlers: {
            workflow_overview: async () => {
                handled = true;
                return {};
            },
        },
        executionLog: [],
        maxLogEntries: 100,
        wsClient: {
            send: async message => sent.push(message),
        },
    });

    await executor.executeToolRequest({
        request_id: "request-1",
        tool_name: "workflow_overview",
        parameters: {},
        workflow: { id: "workflow-a", name: "A" },
    });

    assert.equal(handled, false);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].success, false);
    assert.match(sent[0].error, /workflow_context_changed/);
});


test("workflow overview accepts non-string ComfyUI slot types", async () => {
    const source = await readFile(new URL("web/js/query_executor.js", root), "utf8");
    assert.match(
        source,
        /typeof input\.type === 'string' && input\.type\.endsWith\('\?'\)/,
    );
});
