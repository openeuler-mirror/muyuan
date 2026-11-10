import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import {
	appendFile,
	mkdir,
	readdir,
	readFile,
	rename,
	stat,
	writeFile,
} from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { getPluginPaths } from "./config.ts";
import {
	META_TOOL_RUN_NAME,
	META_TOOL_SEARCH_NAME,
	type TraceCall,
	type TraceExport,
	type WorkflowTrace,
} from "./types.ts";

const JOURNAL_VERSION = 1;
const MAX_STRING_LENGTH = 65_536;
const MAX_ARRAY_ITEMS = 200;
const MAX_OBJECT_KEYS = 200;
const MAX_DEPTH = 8;
const MAX_SANITIZED_NODES = 10_000;
const MAX_SERIALIZED_VALUE_BYTES = 256 * 1024;
const SENSITIVE_KEY =
	/password|passwd|token|secret|api.?key|credential|cookie/i;

const JOURNAL_RECORD_CACHE = new Map<
	string,
	{ signature: string; records: JournalRecord[] }
>();

export interface OnlineCheckpoint {
	version: 1;
	watermarks: Record<string, number>;
	lastExtractionAt: string | null;
	lastCatalogVersion: number;
	pendingSince: string | null;
}

interface JournalBase {
	version: 1;
	kind: "task_start" | "tool_result" | "seal";
	eventId: string;
	sessionId: string;
	sequence: number;
	taskId: string;
	timestamp: string;
}

interface TaskStartRecord extends JournalBase {
	kind: "task_start";
	goal: string;
	context: Record<string, unknown>;
}

interface ToolResultRecord extends JournalBase {
	kind: "tool_result";
	goal: string;
	turnId: number;
	source: "primitive" | "meta_execution" | "meta_search";
	toolCallId: string;
	toolName: string;
	input: Record<string, unknown>;
	result: unknown;
	isError: boolean;
	toolSchemaFingerprint?: string;
	provider?: string;
}

interface SealRecord extends JournalBase {
	kind: "seal";
	goal: string;
	success: boolean;
	primitiveCalls: number;
	failedCalls: number;
	boundary?: "task" | "segment";
}

type JournalRecord = TaskStartRecord | ToolResultRecord | SealRecord;
type AppendRecord =
	| Pick<TaskStartRecord, "kind" | "taskId" | "goal" | "context">
	| Pick<
			ToolResultRecord,
			| "kind"
			| "taskId"
			| "goal"
			| "turnId"
			| "source"
			| "toolCallId"
			| "toolName"
			| "input"
			| "result"
			| "isError"
			| "toolSchemaFingerprint"
			| "provider"
	  >
	| Pick<
			SealRecord,
		"kind" | "taskId" | "goal" | "success" | "primitiveCalls" | "failedCalls" | "boundary"
	  >;

export interface OnlineSnapshot {
	trace: TraceExport;
	watermarks: Record<string, number>;
	newSuccessfulTraces: number;
	availableNewSuccessfulTraces: number;
	remainingNewSuccessfulTraces: number;
	totalSuccessfulTraces: number;
	totalCalls: number;
}

export interface SnapshotOptions {
	maxTraces: number;
	maxTraceAgeDays: number;
	maxCalls: number;
	maxBytes?: number;
	checkpoint?: OnlineCheckpoint;
	traceEligibility?: "strict" | "recovered_segment";
	maxRecoverableErrorsPerTask?: number;
	minRecoveredSegmentCalls?: number;
}

export interface OnlineSealStats {
	strictSuccess: boolean;
	primitiveCalls: number;
	failedCalls: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeSessionFile(sessionId: string): string {
	const readable = sessionId.replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 80);
	const hash = createHash("sha256")
		.update(sessionId)
		.digest("hex")
		.slice(0, 12);
	return `${readable || "session"}-${hash}.jsonl`;
}

function sanitizeValue(
	value: unknown,
	depth = 0,
	key = "",
	budget = { remaining: MAX_SANITIZED_NODES },
): unknown {
	if (SENSITIVE_KEY.test(key)) return "[REDACTED]";
	if (budget.remaining <= 0) return "[MAX_SIZE]";
	budget.remaining -= 1;
	if (depth >= MAX_DEPTH) return "[MAX_DEPTH]";
	if (typeof value === "string")
		return value.length <= MAX_STRING_LENGTH
			? value
			: `${value.slice(0, MAX_STRING_LENGTH)}…[TRUNCATED]`;
	if (value === null || typeof value === "boolean" || typeof value === "number")
		return value;
	if (Array.isArray(value))
		return value
			.slice(0, MAX_ARRAY_ITEMS)
			.map((item) => sanitizeValue(item, depth + 1, "", budget));
	if (isRecord(value)) {
		const result: Record<string, unknown> = {};
		for (const [childKey, child] of Object.entries(value).slice(
			0,
			MAX_OBJECT_KEYS,
		))
			result[childKey] = sanitizeValue(child, depth + 1, childKey, budget);
		return result;
	}
	return String(value);
}

function boundedSanitize(value: unknown, key = ""): unknown {
	const sanitized = sanitizeValue(value, 0, key);
	const encoded = JSON.stringify(sanitized);
	if (Buffer.byteLength(encoded, "utf8") <= MAX_SERIALIZED_VALUE_BYTES)
		return sanitized;
	return {
		_meta_tool_truncated: true,
		reason: `serialized value exceeded ${MAX_SERIALIZED_VALUE_BYTES} bytes`,
		preview: encoded.slice(0, 64 * 1024),
	};
}

async function atomicWrite(path: string, value: unknown): Promise<void> {
	await mkdir(dirname(path), { recursive: true });
	const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
	await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
	await rename(temporary, path);
}

async function ensureAppendBoundary(path: string): Promise<void> {
	if (!existsSync(path)) return;
	const content = await readFile(path, "utf8");
	if (content.length > 0 && !content.endsWith("\n"))
		await appendFile(path, "\n", "utf8");
}

async function lastSequence(path: string): Promise<number> {
	if (!existsSync(path)) return 0;
	const lines = (await readFile(path, "utf8")).trim().split("\n").reverse();
	for (const line of lines) {
		if (!line.trim()) continue;
		try {
			const value = JSON.parse(line) as Record<string, unknown>;
			if (typeof value.sequence === "number") return value.sequence;
		} catch {
			// A partial final line is ignored and recovered by the next append.
		}
	}
	return 0;
}

export class OnlineJournalWriter {
	readonly sessionId: string;
	readonly path: string;
	private sequence = 0;
	private taskOrdinal = 0;
	private readonly writerId = randomUUID();
	private taskId = "";
	private goal = "";
	private primitiveCalls = 0;
	private failedCalls = 0;
	private lastSealStats: OnlineSealStats | undefined;
	private readonly context: Record<string, unknown>;
	private readonly persistOnEachToolResult: boolean;
	private readonly journalFlushInterval: number;
	private bufferedLines: string[] = [];
	private bufferedRecords = 0;
	private writeChain: Promise<void> = Promise.resolve();

	private constructor(
		cwd: string,
		sessionId: string,
		context: Record<string, unknown>,
		persistOnEachToolResult: boolean,
		journalFlushInterval: number,
	) {
		this.sessionId = sessionId;
		this.path = join(
			getPluginPaths(cwd).journalDir,
			safeSessionFile(sessionId),
		);
		this.context = boundedSanitize(context, "context") as Record<
			string,
			unknown
		>;
		this.persistOnEachToolResult = persistOnEachToolResult;
		this.journalFlushInterval = Math.max(1, Math.trunc(journalFlushInterval));
	}

	static async open(
		cwd: string,
		sessionId: string,
		context: Record<string, unknown> = {},
		persistOnEachToolResult = true,
		journalFlushInterval = 32,
	): Promise<OnlineJournalWriter> {
		const writer = new OnlineJournalWriter(
			cwd,
			sessionId,
			context,
			persistOnEachToolResult,
			journalFlushInterval,
		);
		await mkdir(getPluginPaths(cwd).journalDir, { recursive: true });
		await ensureAppendBoundary(writer.path);
		writer.sequence = await lastSequence(writer.path);
		return writer;
	}

	private append(record: AppendRecord): Promise<void> {
		const sequence = ++this.sequence;
		const completed: JournalRecord = {
			...record,
			version: JOURNAL_VERSION,
			eventId: `${this.sessionId}:${sequence}`,
			sessionId: this.sessionId,
			sequence,
			timestamp: new Date().toISOString(),
		} as JournalRecord;
		const line = `${JSON.stringify(completed)}\n`;
		this.bufferedLines.push(line);
		this.bufferedRecords += 1;
		if (this.persistOnEachToolResult || this.bufferedRecords >= this.journalFlushInterval)
			return this.flush();
		return this.writeChain;
	}

	async beginTask(goal: string): Promise<string> {
		if (this.taskId) await this.seal();
		this.taskOrdinal += 1;
		this.taskId = `${this.sessionId}:${this.writerId}:task:${this.taskOrdinal}`;
		this.goal = String(sanitizeValue(goal.trim()));
		this.primitiveCalls = 0;
		this.failedCalls = 0;
		await this.append({
			kind: "task_start",
			taskId: this.taskId,
			goal: this.goal,
			context: this.context,
		});
		return this.taskId;
	}

	async recordToolResult(value: {
		toolCallId: string;
		toolName: string;
		input: Record<string, unknown>;
		result: unknown;
		isError: boolean;
		turnId: number;
		toolSchemaFingerprint?: string;
		provider?: string;
	}): Promise<void> {
		if (!this.taskId) await this.beginTask("Unlabelled Pi task");
		const source =
			value.toolName === META_TOOL_RUN_NAME
				? "meta_execution"
				: value.toolName === META_TOOL_SEARCH_NAME
					? "meta_search"
					: "primitive";
		if (source === "primitive") {
			this.primitiveCalls += 1;
			if (value.isError) this.failedCalls += 1;
		}
		await this.append({
			kind: "tool_result",
			taskId: this.taskId,
			goal: this.goal,
			turnId: value.turnId,
			source,
			toolCallId: value.toolCallId,
			toolName: value.toolName,
			input: boundedSanitize(value.input, "input") as Record<string, unknown>,
			result: boundedSanitize(value.result, "result"),
			isError: value.isError,
			toolSchemaFingerprint: value.toolSchemaFingerprint,
			provider: value.provider,
		});
	}

	async seal(boundary: "task" | "segment" = "task"): Promise<boolean> {
		if (!this.taskId) {
			this.lastSealStats = undefined;
			return false;
		}
		const success = this.primitiveCalls > 0 && this.failedCalls === 0;
		this.lastSealStats = {
			strictSuccess: success,
			primitiveCalls: this.primitiveCalls,
			failedCalls: this.failedCalls,
		};
		await this.append({
			kind: "seal",
			taskId: this.taskId,
			goal: this.goal,
			success,
			primitiveCalls: this.primitiveCalls,
			failedCalls: this.failedCalls,
			boundary,
		});
		this.taskId = "";
		this.goal = "";
		this.primitiveCalls = 0;
		this.failedCalls = 0;
		return success;
	}

	getLastSealStats(): OnlineSealStats | undefined {
		return this.lastSealStats ? { ...this.lastSealStats } : undefined;
	}

	async checkpoint(minCalls: number): Promise<boolean> {
		if (!this.taskId || this.primitiveCalls < minCalls) return false;
		const goal = this.goal;
		await this.seal("segment");
		await this.beginTask(goal);
		await this.flush();
		return true;
	}

	async flush(): Promise<void> {
		// A rejected append must not poison all future writes. Retain pending
		// lines until a successful append, and recreate only our owned directory.
		this.writeChain = this.writeChain.catch(() => undefined).then(async () => {
			if (this.bufferedLines.length === 0) return;
			const count = this.bufferedLines.length;
			const content = this.bufferedLines.slice(0, count).join("");
			await mkdir(dirname(this.path), { recursive: true });
			await appendFile(this.path, content, "utf8");
			this.bufferedLines.splice(0, count);
			this.bufferedRecords = this.bufferedLines.length;
		});
		await this.writeChain;
	}
}

function parseJournalRecord(line: string): JournalRecord | undefined {
	try {
		const value: unknown = JSON.parse(line);
		if (!isRecord(value) || value.version !== JOURNAL_VERSION) return undefined;
		if (!["task_start", "tool_result", "seal"].includes(String(value.kind)))
			return undefined;
		return value as unknown as JournalRecord;
	} catch {
		return undefined;
	}
}

async function journalRecords(path: string): Promise<JournalRecord[]> {
	const signature = await stat(path)
		.then((value) => `${value.ino}:${value.mtimeMs}:${value.size}`)
		.catch((error: unknown) => {
			if ((error as NodeJS.ErrnoException).code === "ENOENT") return "missing";
			throw error;
		});
	const cached = JOURNAL_RECORD_CACHE.get(path);
	if (cached?.signature === signature) return cached.records;
	const records = (await readFile(path, "utf8"))
		.split("\n")
		.filter(Boolean)
		.flatMap((line) => parseJournalRecord(line) ?? []);
	JOURNAL_RECORD_CACHE.set(path, { signature, records });
	return records;
}

export async function loadCheckpoint(cwd: string): Promise<OnlineCheckpoint> {
	try {
		const value: unknown = JSON.parse(
			await readFile(getPluginPaths(cwd).checkpointFile, "utf8"),
		);
		if (isRecord(value) && value.version === 1)
			return {
				version: 1,
				watermarks: isRecord(value.watermarks)
					? Object.fromEntries(
							Object.entries(value.watermarks).flatMap(([key, item]) =>
								typeof item === "number" ? [[key, item]] : [],
							),
						)
					: {},
				lastExtractionAt:
					typeof value.lastExtractionAt === "string"
						? value.lastExtractionAt
						: null,
				lastCatalogVersion:
					typeof value.lastCatalogVersion === "number"
						? value.lastCatalogVersion
						: 0,
				pendingSince:
					typeof value.pendingSince === "string" ? value.pendingSince : null,
			};
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	return {
		version: 1,
		watermarks: {},
		lastExtractionAt: null,
		lastCatalogVersion: 0,
		pendingSince: null,
	};
}

export async function saveCheckpoint(
	cwd: string,
	checkpoint: OnlineCheckpoint,
): Promise<void> {
	await atomicWrite(getPluginPaths(cwd).checkpointFile, checkpoint);
}

export async function readOnlineSnapshot(
	cwd: string,
	options: SnapshotOptions,
): Promise<OnlineSnapshot> {
	const paths = getPluginPaths(cwd);
	await mkdir(paths.journalDir, { recursive: true });
	const files = (await readdir(paths.journalDir))
		.filter((name) => name.endsWith(".jsonl"))
		.sort();
	const watermarks: Record<string, number> = {
		...(options.checkpoint?.watermarks ?? {}),
	};
	const traces: Array<
		WorkflowTrace & { sealedAt: string; sealSequence: number; journal: string }
	> = [];
	const cutoff = Date.now() - options.maxTraceAgeDays * 86_400_000;
	const traceEligibility = options.traceEligibility ?? "strict";
	const maxRecoverableErrorsPerTask = Math.max(
		0,
		Math.trunc(options.maxRecoverableErrorsPerTask ?? 1),
	);
	const minRecoveredSegmentCalls = Math.max(
		1,
		Math.trunc(options.minRecoveredSegmentCalls ?? 2),
	);

	for (const file of files) {
		const records = await journalRecords(join(paths.journalDir, file));
		const byTask = new Map<
			string,
			{
				goal: string;
				context: Record<string, unknown>;
				calls: TraceCall[];
				successfulCalls: number;
				failedCalls: number;
				lastFailureSequence?: number;
			}
		>();
		for (const record of records) {
			if (record.kind === "task_start") {
				byTask.set(record.taskId, {
					goal: record.goal,
					context: isRecord(record.context) ? record.context : {},
					calls: [],
					successfulCalls: 0,
					failedCalls: 0,
				});
				continue;
			}
			const task = byTask.get(record.taskId) ?? {
				goal: record.goal,
				context: {},
				calls: [],
				successfulCalls: 0,
				failedCalls: 0,
			};
			byTask.set(record.taskId, task);
			if (record.kind === "tool_result") {
				if (record.source !== "primitive") continue;
				if (record.isError) {
					task.failedCalls += 1;
					task.lastFailureSequence = record.sequence;
					// A failed primitive is a hard workflow boundary. Only the
					// contiguous recovery suffix may become training evidence.
					task.calls = [];
					continue;
				}
				task.successfulCalls += 1;
				task.calls.push({
					name: record.toolName,
					arguments: record.input,
					result: record.result,
					call_id: record.toolCallId,
					turn_id: record.turnId,
					metadata: {
						source: "online-journal",
						event_id: record.eventId,
						tool_schema_fingerprint: record.toolSchemaFingerprint,
						provider: record.provider,
					},
				});
				continue;
			}
			const observedFailedCalls = Math.max(
				task.failedCalls,
				Number.isFinite(record.failedCalls) ? record.failedCalls : 0,
			);
			const strict =
				record.success && observedFailedCalls === 0 && task.calls.length > 0;
			const recovered =
				traceEligibility === "recovered_segment" &&
				!record.success &&
				observedFailedCalls > 0 &&
				observedFailedCalls <= maxRecoverableErrorsPerTask &&
				task.calls.length >= minRecoveredSegmentCalls;
			if (!strict && !recovered) continue;
			const sealedAt = Date.parse(record.timestamp);
			if (!Number.isFinite(sealedAt) || sealedAt < cutoff) continue;
			traces.push({
				trace_id: `${record.sessionId}:${record.taskId}:${record.sequence}`,
				success: true,
				calls: [...task.calls],
				metadata: {
					...task.context,
					goal: task.goal,
					source: "online-journal",
					session_id: record.sessionId,
					task_id: record.taskId,
					sealed_at: record.timestamp,
					trace_eligibility: strict ? "strict" : "recovered_segment",
					source_task_success: record.success,
					workflow_boundary: record.boundary ?? "task",
					task_complete: record.boundary !== "segment",
					source_task_failed_calls: observedFailedCalls,
					discarded_successful_calls: Math.max(
						0,
						task.successfulCalls - task.calls.length,
					),
					recovery_boundary_sequence: task.lastFailureSequence,
				},
				sealedAt: record.timestamp,
				sealSequence: record.sequence,
				journal: file,
			});
		}
	}

	const isNew = (trace: (typeof traces)[number]): boolean =>
		trace.sealSequence > (options.checkpoint?.watermarks[trace.journal] ?? 0);
	const chronological = (
		left: (typeof traces)[number],
		right: (typeof traces)[number],
	) =>
		left.sealedAt.localeCompare(right.sealedAt) ||
		left.journal.localeCompare(right.journal) ||
		left.sealSequence - right.sealSequence;
	const selected: typeof traces = [];
	let totalCalls = 0;
	let totalBytes = 0;
	const maxBytes = options.maxBytes ?? 32 * 1024 * 1024;
	const traceBytes = (trace: (typeof traces)[number]): number =>
		Buffer.byteLength(JSON.stringify(trace), "utf8");
	const selectedIds = new Set<string>();
	const newTraces = traces.filter(isNew).sort(chronological);
	for (const trace of newTraces) {
		if (selected.length >= options.maxTraces) break;
		if (trace.calls.length > options.maxCalls)
			throw new Error(
				`Online trace ${trace.trace_id} has ${trace.calls.length} calls, exceeding maxCalls=${options.maxCalls}`,
			);
		const bytes = traceBytes(trace);
		if (bytes > maxBytes)
			throw new Error(
				`Online trace ${trace.trace_id} uses ${bytes} bytes, exceeding maxBytes=${maxBytes}`,
			);
		if (
			selected.length > 0 &&
			(totalCalls + trace.calls.length > options.maxCalls ||
				totalBytes + bytes > maxBytes)
		)
			break;
		selected.push(trace);
		selectedIds.add(trace.trace_id);
		totalCalls += trace.calls.length;
		totalBytes += bytes;
		watermarks[trace.journal] = Math.max(
			watermarks[trace.journal] ?? 0,
			trace.sealSequence,
		);
	}
	const selectedNewTraces = selected.length;

	// Previously checkpointed traces remain useful evidence for a workflow
	// that recurs across micro-batches. They only consume budget left after all
	// processable new traces, so old context can never starve a pending batch.
	const historical = traces
		.filter((trace) => !isNew(trace) && !selectedIds.has(trace.trace_id))
		.sort((left, right) => -chronological(left, right));
	for (const trace of historical) {
		if (selected.length >= options.maxTraces) break;
		const bytes = traceBytes(trace);
		if (
			totalCalls + trace.calls.length > options.maxCalls ||
			totalBytes + bytes > maxBytes
		)
			continue;
		selected.push(trace);
		totalCalls += trace.calls.length;
		totalBytes += bytes;
	}
	selected.sort((left, right) => left.sealedAt.localeCompare(right.sealedAt));

	return {
		trace: {
			version: 1,
			sessionId: `online-snapshot-${randomUUID()}`,
			cwd,
			createdAt: new Date().toISOString(),
			traces: selected.map(
				({
					sealedAt: _sealedAt,
					sealSequence: _sequence,
					journal: _journal,
					...trace
				}) => trace,
			),
		},
		watermarks,
		newSuccessfulTraces: selectedNewTraces,
		availableNewSuccessfulTraces: newTraces.length,
		remainingNewSuccessfulTraces: Math.max(
			0,
			newTraces.length - selectedNewTraces,
		),
		totalSuccessfulTraces: selected.length,
		totalCalls,
	};
}

export function journalName(path: string): string {
	return basename(path);
}
