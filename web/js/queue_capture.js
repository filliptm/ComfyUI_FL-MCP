export async function captureAuthenticatedQueue(
    api,
    invoke,
    {
        shouldCapture = null,
        prepare = null,
        onRequestActiveChange = null,
        onSubmitting = null,
    } = {},
) {
    const originalQueuePrompt = api.queuePrompt;
    const capturedResults = [];
    let capturedError = null;
    let requestId = null;
    let correlatedRequestId = null;
    let correlatedResult = null;
    let captureQueueResult = true;
    let requestActive = false;

    const setRequestActive = (active) => {
        if (requestActive === active) return;
        requestActive = active;
        if (typeof onRequestActiveChange === "function") {
            onRequestActiveChange(active, { requestId });
        }
    };
    const onPromptQueueing = (event) => {
        if (requestId === null) {
            const nextRequestId = event.detail?.requestId;
            if (nextRequestId === undefined || nextRequestId === null) return;
            requestId = nextRequestId;
            setRequestActive(true);
        }
    };
    const onPromptQueued = (event) => {
        if (event.detail?.requestId === requestId) {
            correlatedRequestId = requestId;
            setRequestActive(false);
            for (let index = capturedResults.length - 1; index >= 0; index -= 1) {
                if (capturedResults[index].requestId === correlatedRequestId) {
                    correlatedResult = capturedResults[index];
                    break;
                }
            }
        }
    };
    const captureQueuePrompt = async (...originalArgs) => {
        let args = originalArgs;
        let metadata = null;
        let capturesThisCall = false;
        let capturedRequestId = null;
        try {
            if (
                captureQueueResult
                && requestActive
                && (
                    typeof shouldCapture !== "function"
                    || await shouldCapture(originalArgs, { requestActive, requestId })
                )
            ) {
                capturesThisCall = true;
                capturedRequestId = requestId;
                if (typeof prepare === "function") {
                    const prepared = await prepare(originalArgs);
                    if (!prepared || !Array.isArray(prepared.args)) {
                        throw new Error("Queue preparation must return an exact args array.");
                    }
                    args = prepared.args;
                    metadata = prepared.metadata ?? null;
                }
            }
            if (capturesThisCall && typeof onSubmitting === "function") {
                onSubmitting({ requestId: capturedRequestId });
            }
            const result = await originalQueuePrompt.apply(api, args);
            if (captureQueueResult && capturesThisCall) {
                const captured = { result, metadata, requestId: capturedRequestId };
                capturedResults.push(captured);
                if (capturedRequestId === correlatedRequestId) {
                    correlatedResult = captured;
                }
            }
            return result;
        } catch (error) {
            if (captureQueueResult && capturesThisCall) {
                capturedError = error;
            }
            throw error;
        }
    };

    api.addEventListener("promptQueueing", onPromptQueueing);
    api.addEventListener("promptQueued", onPromptQueued);
    api.queuePrompt = captureQueuePrompt;
    try {
        let outerAccepted = false;
        let invokeError = null;
        try {
            outerAccepted = await invoke();
        } catch (error) {
            invokeError = error;
        }
        if (capturedError) throw capturedError;
        if (!correlatedResult && capturedResults.length === 1) {
            correlatedResult = capturedResults[0];
        }
        const capturedAccepted = (
            typeof correlatedResult?.result?.prompt_id === "string"
            && correlatedResult.result.prompt_id.length > 0
            && correlatedResult.metadata !== null
        );
        if (invokeError && !capturedAccepted) throw invokeError;
        return {
            // app.queuePrompt() can continue draining another queued item after
            // this request emitted promptQueued. That later item must not turn
            // an already accepted, attested Ren prompt into a retryable error.
            accepted: capturedAccepted || Boolean(outerAccepted),
            result: correlatedResult?.result ?? null,
            metadata: correlatedResult?.metadata ?? null,
        };
    } finally {
        captureQueueResult = false;
        setRequestActive(false);
        api.removeEventListener("promptQueueing", onPromptQueueing);
        api.removeEventListener("promptQueued", onPromptQueued);
        if (api.queuePrompt === captureQueuePrompt) {
            api.queuePrompt = originalQueuePrompt;
        }
    }
}
