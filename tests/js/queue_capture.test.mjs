import assert from "node:assert/strict";
import test from "node:test";

import { captureAuthenticatedQueue } from "../../web/js/queue_capture.js";


class FakeApi extends EventTarget {
    async queuePrompt(_number, prompt) {
        return { prompt_id: prompt.id };
    }

    dispatch(name, detail) {
        const event = new Event(name);
        event.detail = detail;
        this.dispatchEvent(event);
    }
}


test("authenticated queue capture follows the matching ComfyUI request ID", async () => {
    const api = new FakeApi();

    const capture = await captureAuthenticatedQueue(api, async () => {
        api.dispatch("promptQueueing", { requestId: 10 });
        api.dispatch("promptQueueing", { requestId: 11 });
        await api.queuePrompt(0, { id: "other" });
        api.dispatch("promptQueued", { requestId: 11, batchCount: 1 });
        await api.queuePrompt(0, { id: "ren" });
        api.dispatch("promptQueued", { requestId: 10, batchCount: 1 });
        return true;
    });

    assert.equal(capture.accepted, true);
    assert.equal(capture.result.prompt_id, "ren");
});


test("authenticated queue capture preserves a hook installed during queueing", async () => {
    const api = new FakeApi();
    const replacement = async () => ({ prompt_id: "replacement" });

    await captureAuthenticatedQueue(api, async () => {
        await api.queuePrompt(0, { id: "ren" });
        api.queuePrompt = replacement;
        return true;
    });

    assert.equal(api.queuePrompt, replacement);
});


test("accepted Ren queue survives an adversarial interleaved queue failure", async () => {
    const api = new FakeApi();
    const seen = [];
    const original = api.queuePrompt.bind(api);
    api.queuePrompt = async (number, prompt) => {
        seen.push(structuredClone(prompt));
        return original(number, prompt);
    };
    let renRequestActive = false;
    let expectedProvenance = null;
    const graphToPrompt = async prompt => {
        const submission = structuredClone(prompt);
        if (!renRequestActive) return submission;
        expectedProvenance = {
            graph_hash: "a".repeat(64),
            request: "ren",
        };
        submission.provenance = structuredClone(expectedProvenance);
        return submission;
    };
    const replacementCalls = [];
    const unrelatedB = {
        id: "unrelated-b",
        output: { "2": { class_type: "Other", inputs: {} } },
        workflow: { nodes: [], links: [], extra: {} },
    };
    const replacement = async (number, prompt) => {
        replacementCalls.push([number, structuredClone(prompt)]);
        throw new Error("unrelated B failed");
    };

    const capture = await captureAuthenticatedQueue(
        api,
        async () => {
            api.dispatch("promptQueueing", { requestId: 21 });
            const renSubmission = await graphToPrompt({
                id: "ren",
                output: { "1": { class_type: "Test", inputs: {} } },
                workflow: { nodes: [], links: [], extra: {} },
            });
            await api.queuePrompt(0, renSubmission);
            api.dispatch("promptQueued", { requestId: 21, batchCount: 1 });

            const unrelatedSubmission = await graphToPrompt(unrelatedB);
            api.queuePrompt = replacement;
            await api.queuePrompt(0, unrelatedSubmission);
            return false;
        },
        {
            onRequestActiveChange: active => {
                renRequestActive = active;
            },
            shouldCapture: (args, { requestActive }) => (
                requestActive
                && args[1]?.provenance?.request === "ren"
            ),
            prepare: async args => ({
                args,
                metadata: structuredClone(args[1].provenance),
            }),
        },
    );

    assert.equal(seen.length, 1);
    assert.equal(seen[0].id, "ren");
    assert.deepEqual(seen[0].provenance, expectedProvenance);
    assert.equal(replacementCalls.length, 1);
    assert.deepEqual(replacementCalls[0], [0, unrelatedB]);
    assert.equal(capture.accepted, true);
    assert.equal(capture.result.prompt_id, "ren");
    assert.deepEqual(capture.metadata, expectedProvenance);
    assert.equal(api.queuePrompt, replacement);
    assert.equal(renRequestActive, false);
});


test("shouldCapture cannot capture outside the correlated request", async () => {
    const api = new FakeApi();
    let prepareCalls = 0;

    const capture = await captureAuthenticatedQueue(
        api,
        async () => {
            await api.queuePrompt(0, { id: "before" });
            api.dispatch("promptQueueing", { requestId: 31 });
            await api.queuePrompt(0, { id: "ren" });
            api.dispatch("promptQueued", { requestId: 31 });
            await api.queuePrompt(0, { id: "after" });
            return false;
        },
        {
            shouldCapture: () => true,
            prepare: async args => {
                prepareCalls += 1;
                return { args, metadata: { exact: true } };
            },
        },
    );

    assert.equal(prepareCalls, 1);
    assert.equal(capture.accepted, true);
    assert.equal(capture.result.prompt_id, "ren");
    assert.deepEqual(capture.metadata, { exact: true });
});


test("onSubmitting marks only the selected low-level POST boundary", async () => {
    const api = new FakeApi();
    const events = [];
    await captureAuthenticatedQueue(
        api,
        async () => {
            api.dispatch("promptQueueing", { requestId: 41 });
            await api.queuePrompt(0, { id: "ren" });
            api.dispatch("promptQueued", { requestId: 41 });
            return true;
        },
        {
            shouldCapture: args => args[1]?.id === "ren",
            prepare: async args => {
                events.push("prepared");
                return { args, metadata: { exact: true } };
            },
            onSubmitting: () => events.push("submitting"),
        },
    );
    assert.deepEqual(events, ["prepared", "submitting"]);
});
