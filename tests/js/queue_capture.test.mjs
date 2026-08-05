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
