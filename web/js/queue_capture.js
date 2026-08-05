export async function captureAuthenticatedQueue(api, invoke) {
    const originalQueuePrompt = api.queuePrompt;
    const capturedResults = [];
    let capturedError = null;
    let requestId = null;
    let correlatedResult = null;
    let captureQueueResult = true;

    const onPromptQueueing = (event) => {
        if (requestId === null) {
            requestId = event.detail?.requestId;
        }
    };
    const onPromptQueued = (event) => {
        if (event.detail?.requestId === requestId) {
            correlatedResult = capturedResults.at(-1) || null;
        }
    };
    const captureQueuePrompt = async (...args) => {
        try {
            const result = await originalQueuePrompt.apply(api, args);
            if (captureQueueResult) {
                capturedResults.push(result);
            }
            return result;
        } catch (error) {
            if (captureQueueResult) {
                capturedError = error;
            }
            throw error;
        }
    };

    api.addEventListener("promptQueueing", onPromptQueueing);
    api.addEventListener("promptQueued", onPromptQueued);
    api.queuePrompt = captureQueuePrompt;
    try {
        const accepted = await invoke();
        if (capturedError) throw capturedError;
        if (!correlatedResult && capturedResults.length === 1) {
            correlatedResult = capturedResults[0];
        }
        return { accepted, result: correlatedResult };
    } finally {
        captureQueueResult = false;
        api.removeEventListener("promptQueueing", onPromptQueueing);
        api.removeEventListener("promptQueued", onPromptQueued);
        if (api.queuePrompt === captureQueuePrompt) {
            api.queuePrompt = originalQueuePrompt;
        }
    }
}
