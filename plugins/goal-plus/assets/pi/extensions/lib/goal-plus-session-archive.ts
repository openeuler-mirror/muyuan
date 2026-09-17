import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { fileURLToPath } from "node:url";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { THINKTHREAD_GOAL_PLUS_PROTOCOL } from "../../../thinkthread-session-rpc.ts";
import { sessionDiagnosticsEnabled } from "./goal-plus-session-diagnostics.ts";

const observationTypes = new Set([
	"goal-plus-instructions-observation", "goal-plus-tool-contracts", "goal-plus-provider-request",
	"goal-plus-injection-observation",
]);
const gpTool = (name: unknown) => typeof name === "string" && /^(goal_plus_|search_|thinkthread_)/.test(name);
const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
// Match the existing TT RPC chunk size; bound a diagnostic batch to 32 chunks.
const CHUNK_BYTES = 32 * 1024;
const MAX_BATCH_BYTES = 32 * CHUNK_BYTES;
const MAX_DECODED_BYTES = 16 * 1024 * 1024;
const ARCHIVE_SEND_TIMEOUT_MS = 5_000;
const CHECKPOINT_TYPE = "goal-plus-session-archive-checkpoint";

export async function sendDiagnosticMessages(parentId: string, messages: Record<string, unknown>[]): Promise<void> {
	// The SDK socket has no cancellation API. A bounded helper contains optional I/O.
	await new Promise<void>((resolve, reject) => {
		const child = execFile(process.execPath, [fileURLToPath(new URL("./goal-plus-diagnostic-sender.mjs", import.meta.url))],
			{ timeout: ARCHIVE_SEND_TIMEOUT_MS, killSignal: "SIGKILL", maxBuffer: CHUNK_BYTES },
			(error) => error ? reject(error) : resolve());
		child.stdin?.on("error", () => {});
		child.stdin?.end(JSON.stringify({ parentId, messages }));
	});
}

export function diagnosticEntry(entry: any): Record<string, unknown> {
	const base = { id: entry.id, parentId: entry.parentId, timestamp: entry.timestamp };
	if (entry.type === "custom" && observationTypes.has(entry.customType)) {
		return { ...base, type: "custom", customType: entry.customType, data: entry.data };
	}
	if (entry.type === "compaction") return { ...base, type: "compaction" };
	const message = entry.message;
	if (entry.type === "message" && message?.role === "assistant") {
		return { ...base, type: "message", message: {
			role: "assistant", stopReason: message.stopReason, provider: message.provider, model: message.model,
			errorMessage: message.errorMessage,
			content: (message.content ?? []).filter((part: any) => part.type === "toolCall" && gpTool(part.name))
				.map((part: any) => ({ type: "toolCall", id: part.id, name: part.name, arguments: part.arguments })),
		} };
	}
	if (entry.type === "message" && message?.role === "toolResult" && gpTool(message.toolName)) {
		return { ...base, type: "message", message: {
			role: "toolResult", toolCallId: message.toolCallId, toolName: message.toolName, isError: message.isError,
			content: (message.content ?? []).filter((part: any) => part.type === "text")
				.map((part: any) => ({ type: "text", text: part.text })),
		} };
	}
	// Keep tree identity without user text, native tool bodies or private reasoning.
	return { ...base, type: "diagnostic-placeholder" };
}

export class SessionArchive {
	private sessionId?: string;
	private streamId = randomUUID();
	private sequence = 0;
	private sent = new Set<string>();
	private pending?: { bytes: Buffer; entries: string[] };

	async flush(ctx: ExtensionContext, options: {
		root: string; role: string; sourceRoot: string;
		checkpoint: (data: Record<string, unknown>) => void;
		worker?: () => Promise<{ agentSessionId: string; send: (messages: Record<string, unknown>[]) => Promise<void> }>;
	}): Promise<void> {
		if (!sessionDiagnosticsEnabled()) return;
		try {
			const sessionId = ctx.sessionManager.getSessionId();
			if (this.sessionId !== sessionId) {
				this.sessionId = sessionId; this.streamId = randomUUID();
				this.sequence = 0; this.sent.clear(); this.pending = undefined;
				if (options.worker) {
					const entries = ctx.sessionManager.getEntries();
					const checkpoint = entries.findLast(entry => entry.type === "custom"
						&& entry.customType === CHECKPOINT_TYPE && (entry.data as any)?.session_id === sessionId);
					const data = (checkpoint as any)?.data;
					const last = entries.findIndex(entry => entry.id === data?.exported_through);
					if (data?.version === 1 && typeof data.stream_id === "string"
						&& Number.isSafeInteger(data.next_sequence) && data.next_sequence > 0 && last >= 0) {
						this.streamId = data.stream_id; this.sequence = data.next_sequence;
						for (const entry of entries.slice(0, last + 1)) this.sent.add(entry.id);
					}
				}
			}
			if (!options.worker) {
				const file = ctx.sessionManager.getSessionFile();
				if (!file) return;
				const dir = join(resolve(ctx.cwd, options.root), "host-logs", "session-diagnostics", `pi-${hash(sessionId)}`);
				mkdirSync(dir, { recursive: true, mode: 0o700 });
				const temporary = join(dir, `${randomUUID()}.tmp`);
				writeFileSync(temporary, JSON.stringify({ version: 1, source: "native", session_id: sessionId,
					agent_harness: "pi", role: options.role, history: file,
					overview: join(options.sourceRoot, "assets", "goal-plus-instructions.txt"),
				}), { mode: 0o600 });
				renameSync(temporary, join(dir, "index.json"));
				return;
			}
			const { send, agentSessionId } = await options.worker();
			const replaying = this.pending !== undefined;
			if (!this.pending) {
				const entries = ctx.sessionManager.getEntries().filter(entry => !this.sent.has(entry.id));
				if (entries.every(entry => entry.type === "custom" && entry.customType === CHECKPOINT_TYPE)) return;
				const raw = JSON.stringify({ version: 1, session_id: sessionId, stream_id: this.streamId,
					sequence: this.sequence, captured_at: new Date().toISOString(),
					leaf_id: ctx.sessionManager.getLeafId(), entries: entries.map(diagnosticEntry) });
				if (Buffer.byteLength(raw) > MAX_DECODED_BYTES) throw new Error("diagnostic_batch_too_large");
				const bytes = gzipSync(raw);
				if (bytes.length > MAX_BATCH_BYTES) throw new Error("diagnostic_batch_too_large");
				this.pending = { bytes, entries: entries.map(entry => entry.id) };
			}
			const { bytes, entries } = this.pending;
			const messages = [];
			for (let offset = 0; offset < bytes.length; offset += CHUNK_BYTES) {
				const chunk = bytes.subarray(offset, offset + CHUNK_BYTES);
				messages.push({ protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL, type: "session_diagnostics",
						agent_session_id: agentSessionId, batch_sha256: hash(bytes), chunk_index: offset / CHUNK_BYTES,
						chunk_count: Math.ceil(bytes.length / CHUNK_BYTES), chunk_sha256: hash(chunk),
						data_base64: chunk.toString("base64") });
			}
			await send(messages);
			for (const id of entries) this.sent.add(id);
			this.sequence++; this.pending = undefined;
			options.checkpoint({ version: 1, session_id: sessionId, stream_id: this.streamId,
				next_sequence: this.sequence, exported_through: entries.at(-1) });
			// A replayed pending batch may precede entries from the current invocation.
			if (replaying) await this.flush(ctx, options);
		} catch (error) {
			console.error(JSON.stringify({ event: "goal_plus_session_archive_unavailable",
				reason: error instanceof Error ? error.name : "Error" }));
		}
	}
}
