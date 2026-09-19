import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { fileURLToPath } from "node:url";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { THINKTHREAD_GOAL_PLUS_PROTOCOL } from "../../../thinkthread-session-rpc.ts";
import { sessionDiagnosticsEnabled } from "./goal-plus-session-diagnostics.ts";

const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
// Match the existing TT RPC chunk size; bound each session archive batch to 32 chunks.
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

export function sessionEntry(entry: any): Record<string, unknown> {
	if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
		throw new TypeError("Pi session entry must be an object");
	}
	// Pi native entries are JSON records. Preserve the complete entry so the same
	// archive supports operational monitoring and post-run behavior review.
	return { ...entry };
}

export class SessionArchive {
	private sessionId?: string;
	private streamId = randomUUID();
	private sequence = 0;
	private sent = new Set<string>();
	private pending?: { bytes: Buffer; entries: string[] };

	private encodeBatch(sessionId: string, leafId: string | undefined,
		entries: Record<string, unknown>[]): Buffer {
		const raw = JSON.stringify({ version: 1, session_id: sessionId, stream_id: this.streamId,
			sequence: this.sequence, captured_at: new Date().toISOString(),
			leaf_id: leafId, entries: entries.map(sessionEntry) });
		if (Buffer.byteLength(raw) > MAX_DECODED_BYTES) throw new Error("session_batch_too_large");
		const bytes = gzipSync(raw);
		if (bytes.length > MAX_BATCH_BYTES) throw new Error("session_batch_too_large");
		return bytes;
	}

	private nextBatch(sessionId: string, leafId: string | undefined,
		entries: Record<string, unknown>[]): { bytes: Buffer; entries: string[] } {
		const selected: Record<string, unknown>[] = [];
		let bytes: Buffer | undefined;
		for (const entry of entries) {
			const candidate = [...selected, entry];
			try {
				bytes = this.encodeBatch(sessionId, leafId, candidate);
			} catch (error) {
				if (selected.length === 0) throw error;
				break;
			}
			selected.push(entry);
		}
		if (!bytes || selected.length === 0) throw new Error("session_batch_empty");
		return { bytes, entries: selected.map(entry => String(entry.id)) };
	}

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
				this.pending = this.nextBatch(sessionId, ctx.sessionManager.getLeafId(), entries);
			}
			const { bytes, entries } = this.pending;
			const messages = [];
			for (let offset = 0; offset < bytes.length; offset += CHUNK_BYTES) {
				const chunk = bytes.subarray(offset, offset + CHUNK_BYTES);
				messages.push({ protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL, type: "session_transcript",
						agent_session_id: agentSessionId, batch_sha256: hash(bytes), chunk_index: offset / CHUNK_BYTES,
						chunk_count: Math.ceil(bytes.length / CHUNK_BYTES), chunk_sha256: hash(chunk),
						data_base64: chunk.toString("base64") });
			}
			await send(messages);
			for (const id of entries) this.sent.add(id);
			this.sequence++; this.pending = undefined;
			options.checkpoint({ version: 1, session_id: sessionId, stream_id: this.streamId,
				next_sequence: this.sequence, exported_through: entries.at(-1) });
			// Drain the next bounded batch after a replay or a split full session.
			if (replaying || ctx.sessionManager.getEntries().some(entry => !this.sent.has(entry.id))) {
				await this.flush(ctx, options);
			}
		} catch (error) {
			console.error(JSON.stringify({ event: "goal_plus_session_archive_unavailable",
				reason: error instanceof Error ? error.name : "Error" }));
		}
	}
}
