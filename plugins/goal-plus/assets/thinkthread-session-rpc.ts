import { createHash, randomUUID } from "node:crypto";

export function dispatchMetadata(current: string, history = ""): {
    registrationNonce?: string; dispatchNonce?: string; agentSessionId?: string; thinkthreadRole?: string;
} {
    return {
        registrationNonce: /registration_nonce=([0-9a-f-]{36})/i.exec(current)?.[1]
            ?? /registration_nonce=([0-9a-f-]{36})/i.exec(history)?.[1],
        dispatchNonce: [...current.matchAll(/dispatch_nonce=([0-9a-f-]{36})/gi)].at(-1)?.[1]
            ?? [...history.matchAll(/dispatch_nonce=([0-9a-f-]{36})/gi)].at(-1)?.[1],
        agentSessionId: /agent_session_id=([A-Za-z0-9._-]+)/.exec(current)?.[1]
            ?? /agent_session_id=([A-Za-z0-9._-]+)/.exec(history)?.[1],
        thinkthreadRole: /goal_plus_thinkthread_role=([A-Za-z0-9._-]+)/.exec(current)?.[1]
            ?? /goal_plus_thinkthread_role=([A-Za-z0-9._-]+)/.exec(history)?.[1],
    };
}

export const THINKTHREAD_GOAL_PLUS_PROTOCOL = "goal-plus.thinkthread.v3";
export const THINKTHREAD_WORKER_RPC_TIMEOUT_MS = 120_000;

export interface ThinkThreadRpcClient {
    invoke(method: string, params: Record<string, unknown>): Promise<Record<string, any>>;
}

function isRecord(value: unknown): value is Record<string, any> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

export async function dispatchMessageId(
    client: ThinkThreadRpcClient, parentId: string, nonce: string,
): Promise<string> {
    let cursor: string | undefined;
    const deadline = Date.now() + THINKTHREAD_WORKER_RPC_TIMEOUT_MS;
    while (Date.now() < deadline) {
        const page = await client.invoke("message.receive", {
            senderThinkthreadId: parentId, limit: 32, ...(cursor ? { after: cursor } : {}),
        });
        if (!Array.isArray(page.messages) || typeof page.nextCursor !== "string") {
            throw new Error("ThinkThread dispatch Message page is invalid");
        }
        const matches = page.messages.filter((message: any) =>
            message.senderThinkthreadId === parentId && message.truncated === false
            && typeof message.text === "string"
            && [...message.text.matchAll(/\bdispatch_nonce=([0-9a-f-]{36})(?=[.;\s]|$)/gi)]
                .some((match) => match[1] === nonce),
        );
        if (matches.length > 1) throw new Error("ThinkThread dispatch Message is ambiguous");
        if (matches.length === 1) {
            if (typeof matches[0].messageId !== "string") throw new Error("ThinkThread dispatch omitted messageId");
            return matches[0].messageId;
        }
        if (!page.hasMore || page.nextCursor === cursor) break;
        cursor = page.nextCursor;
    }
    throw new Error("ThinkThread dispatch has no exact retained Message");
}

function strictBase64(value: unknown): Buffer {
	if (typeof value !== "string") throw new Error("ThinkThread response chunk omitted data_base64");
	const decoded = Buffer.from(value, "base64");
	if (decoded.toString("base64").replace(/=+$/, "") !== value.replace(/=+$/, "")) {
		throw new Error("ThinkThread response chunk contains invalid base64");
	}
	return decoded;
}

function sha256Bytes(value: Buffer | string): string {
	return createHash("sha256").update(value).digest("hex");
}

export class WorkerSteeringReceiver {
    private cursor: string | undefined;
    private closed = false;
    private replies = new Map<string, Record<string, unknown>>();
    private client: ThinkThreadRpcClient;
    private parentId: string;
    private identity: { agentSessionId: string; dispatchNonce: string; messageId: string };
    private active: () => boolean;
    private steer: (prompt: string) => void;
    private interrupt?: () => void;

    constructor(
        client: ThinkThreadRpcClient, parentId: string,
        identity: { agentSessionId: string; dispatchNonce: string; messageId: string },
        active: () => boolean, steer: (prompt: string) => void, interrupt?: () => void,
    ) {
        this.client = client; this.parentId = parentId; this.identity = identity;
        this.active = active; this.steer = steer; this.interrupt = interrupt;
    }

    close(): void { this.closed = true; }

    async poll(): Promise<void> {
        if (this.closed) return;
        const page = await this.client.invoke("message.receive", {
            senderThinkthreadId: this.parentId, limit: 32,
            ...(this.cursor ? { after: this.cursor } : {}),
        });
        if (!Array.isArray(page.messages) || typeof page.nextCursor !== "string") {
            throw new Error("ThinkThread steering received an invalid Message page");
        }
        for (const message of page.messages) {
            if (this.closed) return;
            if (message.senderThinkthreadId !== this.parentId || message.truncated !== false) continue;
            let request;
            try { request = JSON.parse(message.text); } catch { continue; }
            if (!isRecord(request) || request.protocol !== THINKTHREAD_GOAL_PLUS_PROTOCOL
                || !["session_message", "session_interrupt"].includes(request.type)
                || typeof request.request_id !== "string") continue;
            if (typeof request.content_json !== "string"
                || sha256Bytes(request.content_json) !== request.content_sha256) continue;
            const prior = this.replies.get(request.request_id);
            const replyType = `${request.type}_ack`;
            if (prior && (prior.content_sha256 !== request.content_sha256 || prior.type !== replyType)) continue;
            let reply = prior;
            if (!reply) {
                let content;
                try { content = JSON.parse(request.content_json); } catch { continue; }
                if (!isRecord(content)) continue;
                const bound = content.agent_session_id === this.identity.agentSessionId
                    && content.dispatch_nonce === this.identity.dispatchNonce
                    && content.message_id === this.identity.messageId;
                const valid = (request.type === "session_interrupt" ? this.interrupt !== undefined
                    : typeof content.prompt === "string" && content.prompt.trim().length > 0)
                    && Number.isFinite(content.deadline_unix_ms) && content.deadline_unix_ms > Date.now();
                const accepted = bound && valid && this.active();
                reply = {
                    protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL, type: replyType,
                    request_id: request.request_id, content_sha256: request.content_sha256,
                    accepted, reason: accepted ? null : "inactive_or_mismatched_invocation",
                };
                // No await between the native active check and queueing the steering message.
                this.replies.set(request.request_id, reply);
                if (accepted) {
                    try {
                        if (request.type === "session_interrupt") this.interrupt!();
                        else this.steer(content.prompt);
                    }
                    catch { reply.accepted = false; reply.reason = "native_control_failed"; }
                }
            }
            await this.client.invoke("message.send", {
                recipientThinkthreadId: this.parentId, text: JSON.stringify(reply), wake: false,
            });
        }
        this.cursor = page.nextCursor;
    }
}

async function pauseForWorkerResponse(signal: AbortSignal, milliseconds: number): Promise<void> {
	if (signal.aborted) throw new Error("ThinkThread worker RPC was aborted");
	await new Promise<void>((resolve, reject) => {
		const timer = setTimeout(() => {
			signal.removeEventListener("abort", abort);
			resolve();
		}, milliseconds);
		const abort = () => {
			clearTimeout(timer);
			reject(new Error("ThinkThread worker RPC was aborted"));
		};
		signal.addEventListener("abort", abort, { once: true });
	});
}

export async function requestWorkerTool(
    client: ThinkThreadRpcClient,
    parentId: string,
    agentSessionId: string,
    name: string,
    params: Record<string, unknown>,
    signal: AbortSignal,
): Promise<unknown> {
    const requestId = `req-${randomUUID()}`;
	const deadlineUnixMs = Date.now() + THINKTHREAD_WORKER_RPC_TIMEOUT_MS;
	const contentJson = JSON.stringify({
		agent_session_id: agentSessionId,
		params: params,
		tool: name,
		deadline_unix_ms: deadlineUnixMs,
	});
	const request = {
		protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL,
		type: "request",
		request_id: requestId,
		agent_session_id: agentSessionId,
		tool: name,
		params: params,
		deadline_unix_ms: deadlineUnixMs,
		content_json: contentJson,
		content_sha256: sha256Bytes(contentJson),
	};
	const chunks = new Map<number, Buffer>();
	let expectedCount: number | undefined;
	let expectedHash: string | undefined;
	let cursor: string | undefined;
	const deadline = deadlineUnixMs;
	let nextSendAt = 0;
	while (Date.now() < deadline) {
		if (signal.aborted) throw new Error("ThinkThread worker RPC was aborted");
		if (Date.now() >= nextSendAt) {
			await client.invoke("message.send", {
				recipientThinkthreadId: parentId,
				text: JSON.stringify(request),
				wake: false,
			});
			nextSendAt = Date.now() + 2_000;
		}
		const receiveParams: Record<string, unknown> = {
			senderThinkthreadId: parentId,
			limit: 32,
		};
		if (cursor) receiveParams.after = cursor;
		const page = await client.invoke("message.receive", receiveParams);
		if (!Array.isArray(page.messages) || typeof page.nextCursor !== "string") {
			throw new Error("ThinkThread worker RPC received an invalid Message page");
		}
		cursor = page.nextCursor;
		for (const message of page.messages) {
			if (!isRecord(message) || message.senderThinkthreadId !== parentId || message.truncated === true) continue;
			if (typeof message.text !== "string") continue;
			let envelope: unknown;
			try {
				envelope = JSON.parse(message.text);
			} catch {
				continue;
			}
			if (
				!isRecord(envelope) ||
				envelope.protocol !== THINKTHREAD_GOAL_PLUS_PROTOCOL ||
				envelope.type !== "response_chunk" ||
				envelope.request_id !== requestId
			) continue;
			const index = envelope.chunk_index;
			const count = envelope.chunk_count;
			if (!Number.isInteger(index) || !Number.isInteger(count) || Number(index) < 0 || Number(count) < 1 || Number(index) >= Number(count)) {
				throw new Error("ThinkThread worker RPC response chunk identity is invalid");
			}
			const chunk = strictBase64(envelope.data_base64);
			if (envelope.chunk_sha256 !== sha256Bytes(chunk)) {
				throw new Error("ThinkThread worker RPC response chunk hash mismatch");
			}
			if (expectedCount !== undefined && expectedCount !== count) {
				throw new Error("ThinkThread worker RPC response chunk count changed");
			}
			if (expectedHash !== undefined && expectedHash !== envelope.response_sha256) {
				throw new Error("ThinkThread worker RPC response hash changed");
			}
			expectedCount = Number(count);
			expectedHash = String(envelope.response_sha256);
			const prior = chunks.get(Number(index));
			if (prior && !prior.equals(chunk)) {
				throw new Error("ThinkThread worker RPC replayed a changed response chunk");
			}
			chunks.set(Number(index), chunk);
		}
		if (expectedCount !== undefined && chunks.size === expectedCount) {
			const responseBytes = Buffer.concat(
				Array.from({ length: expectedCount }, (_, index) => {
					const chunk = chunks.get(index);
					if (!chunk) throw new Error("ThinkThread worker RPC response omitted a chunk");
					return chunk;
				}),
			);
			if (sha256Bytes(responseBytes) !== expectedHash) {
				throw new Error("ThinkThread worker RPC response hash mismatch");
			}
			const response = JSON.parse(responseBytes.toString("utf8"));
			await client.invoke("message.send", {
				recipientThinkthreadId: parentId,
				text: JSON.stringify({
					protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL,
					type: "response_ack",
					request_id: requestId,
					response_sha256: expectedHash,
				}),
				wake: false,
			});
			if (!isRecord(response) || typeof response.ok !== "boolean") {
				throw new Error("ThinkThread worker RPC response envelope is invalid");
			}
			if (!response.ok) {
				const error = isRecord(response.error) ? response.error : {};
				throw new Error(`${String(error.type || "RuntimeError")}: ${String(error.message || "worker RPC failed")}`);
			}
			return response.result;
		}
		if (page.hasMore !== true) await pauseForWorkerResponse(signal, 100);
	}
	throw new Error(`ThinkThread worker RPC timed out: ${requestId}`);
}
