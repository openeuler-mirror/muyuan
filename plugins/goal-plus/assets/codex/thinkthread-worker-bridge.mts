import { pathToFileURL } from "node:url";
import { dispatchMetadata, requestWorkerTool, THINKTHREAD_GOAL_PLUS_PROTOCOL as protocol, THINKTHREAD_WORKER_RPC_TIMEOUT_MS } from "../thinkthread-session-rpc.ts";

// Only trusted Parent Messages provide the candidate binding. MCP arguments cannot select it.
export async function workerBinding(client: any, parentId: string, timeoutMs = 0) {
    const self = await client.invoke("self", {});
    if (!parentId || self.parentThinkthreadId !== parentId
        || self.capabilities?.length !== 1 || self.capabilities[0].id !== "thinkthread.message") {
        throw new Error("Goal Plus tools require an authenticated Message-only Child");
    }
    const deadline = Date.now() + timeoutMs;
    let cursor: string | undefined;
    let latest: any;
    do {
        const page = await client.invoke("message.receive", {
            senderThinkthreadId: parentId, limit: 32, ...(cursor ? {after: cursor} : {}),
        });
        if (!Array.isArray(page.messages) || typeof page.nextCursor !== "string") {
            throw new Error("Invalid Parent dispatch Message page");
        }
        for (const message of page.messages) {
            if (message.senderThinkthreadId !== parentId || message.recipientThinkthreadId !== self.thinkthreadId
                || message.truncated !== false || typeof message.text !== "string") continue;
            // Tool replies and steering envelopes can quote dispatch text but cannot create a binding.
            try { JSON.parse(message.text); continue; } catch { /* Dispatch is a plain-text native input. */ }
            const binding = dispatchMetadata(message.text);
            if (!binding.dispatchNonce || !binding.registrationNonce || !binding.agentSessionId) continue;
            if (typeof message.messageId !== "string") throw new Error("Dispatch has no Message identity");
            const encoded = [...message.text.matchAll(
                /(?:^|\n)goal_plus_candidate_capabilities=([A-Za-z0-9_-]+);(?=\s|$)/g,
            )].at(-1)?.[1];
            let candidateCapabilities: unknown;
            if (encoded !== undefined) {
                const decoded = Buffer.from(encoded, "base64url");
                if (decoded.toString("base64url") !== encoded) {
                    throw new Error("Parent dispatch has invalid candidate capabilities encoding");
                }
                // The Python bridge validates the shared typed projection. This
                // endpoint only decodes transport bytes from the bound Parent.
                candidateCapabilities = JSON.parse(decoded.toString("utf8"));
            }
            latest = {...binding, candidateCapabilities, parentId, childId: self.thinkthreadId,
                messageId: message.messageId};
        }
        if (!page.hasMore) {
            if (latest || Date.now() >= deadline) break;
            cursor = page.nextCursor;
            await new Promise(resolve => setTimeout(resolve, 100));
            continue;
        }
        if (page.nextCursor === cursor) throw new Error("Parent Message cursor did not advance");
        cursor = page.nextCursor;
    } while (true);
    if (!latest) throw new Error("Child has no retained Goal Plus dispatch");
    return latest;
}

const ANNOTATION_TOOLS = new Set([
    "goal_plus_thinkthread_annotation_next",
    "goal_plus_thinkthread_artifact_view_list",
    "goal_plus_thinkthread_artifact_view_stat",
    "goal_plus_thinkthread_artifact_view_pread",
    "goal_plus_thinkthread_artifact_view_diff",
    "goal_plus_thinkthread_annotation_submit",
]);
const VERIFIER_TOOLS = new Set([
    "goal_plus_thinkthread_verifier_input",
    "goal_plus_thinkthread_verifier_submit",
]);

export async function workerOperation(client: any, parentId: string, request: any) {
    const binding = await workerBinding(client, parentId,
        request.operation === "binding" ? THINKTHREAD_WORKER_RPC_TIMEOUT_MS : 0);
    if (request.operation === "binding") return binding;
    if (request.operation !== "call" || request.params?.agent_session_id !== binding.agentSessionId) {
        throw new Error("MCP candidate binding does not match the current Parent dispatch");
    }
    const {name, arguments: args} = request.params;
    const annotator = binding.thinkthreadRole === "annotator";
    const verifier = binding.thinkthreadRole === "verifier";
    if (verifier !== VERIFIER_TOOLS.has(name)) {
        throw new Error(`ThinkThread Verifier Child tool is not allowed: ${name}`);
    }
    if (annotator !== ANNOTATION_TOOLS.has(name)) {
        throw new Error(annotator
            ? `ThinkThread Annotation Child tool is not allowed: ${name}`
            : `ThinkThread Candidate Child annotation tool is not allowed: ${name}`);
    }
    if (typeof name !== "string" || !args || typeof args !== "object" || Array.isArray(args)
        || "agent_session_id" in args) {
        throw new Error(annotator
            ? "MCP annotation arguments must not select a session"
            : "MCP tool arguments do not match the bound candidate");
    }
    for (const payload of [
        {type: "registration", registration_nonce: binding.registrationNonce},
        {type: "dispatch_ack", dispatch_nonce: binding.dispatchNonce, message_id: binding.messageId},
    ]) {
        await client.invoke("message.send", {
            recipientThinkthreadId: parentId, text: JSON.stringify({protocol, ...payload}), wake: false,
        });
    }
    return requestWorkerTool(client, parentId, binding.agentSessionId, name, args,
        AbortSignal.timeout(THINKTHREAD_WORKER_RPC_TIMEOUT_MS));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    try {
        let input = "";
        for await (const chunk of process.stdin) input += chunk;
        const entry = process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY;
        if (!entry) throw new Error("The installed Agent POSIX SDK is required");
        const {AgentPosixClient} = await import(entry.startsWith("file:") ? entry : pathToFileURL(entry).href);
        const request = JSON.parse(input);
        const operationResult = await workerOperation(AgentPosixClient.fromEnv(), process.env.THINKTHREAD_PARENT_ID ?? "", request);
        // SDK operations return objects; Parent tool calls may return entry arrays.
        const result = request.operation === "call" ? {toolResult: operationResult} : operationResult;
        process.stdout.write(JSON.stringify({ok: true, result}) + "\n");
    } catch (error) {
        process.stdout.write(JSON.stringify({ok: false, error: {
            name: error instanceof Error ? error.name : "Error",
            message: error instanceof Error ? error.message : String(error),
        }}) + "\n");
        process.exitCode = 1;
    }
}
