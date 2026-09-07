import { createHash, randomUUID } from "node:crypto";

export const THINKTHREAD_GOAL_PLUS_PROTOCOL = "goal-plus.thinkthread.v3";
const THINKTHREAD_WORKER_RPC_TIMEOUT_MS = 120_000;

export interface ThinkThreadRpcClient {
    invoke(method: string, params: Record<string, unknown>): Promise<Record<string, any>>;
}

function isRecord(value: unknown): value is Record<string, any> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
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
