import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import {
	META_TOOL_RUN_NAME,
	META_TOOL_SEARCH_NAME,
	type TraceCall,
	type TraceExport,
	type WorkflowTrace,
} from "./types.ts";

interface TraceMetadata {
	sessionId: string;
	cwd: string;
	createdAt?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function contentText(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((block) => {
			if (!isRecord(block)) return "";
			return block.type === "text" && typeof block.text === "string"
				? block.text
				: "";
		})
		.filter(Boolean)
		.join("\n");
}

function parseResult(content: unknown): unknown {
	const text = contentText(content);
	const trimmed = text.trim();
	if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
		try {
			return JSON.parse(trimmed);
		} catch {
			// Pi tool text is not required to be JSON.
		}
	}
	return text;
}

/** Convert the active Pi session branch into successful workflow traces. */
export function extractTraceFromEntries(
	entries: readonly unknown[],
	metadata: TraceMetadata,
): TraceExport {
	const traces: WorkflowTrace[] = [];
	const pending = new Map<string, TraceCall>();
	let currentGoal = "";
	let currentCalls: TraceCall[] = [];
	let batch = 0;
	let traceIndex = 0;
	let invalid = false;

	const flush = () => {
		if (
			currentGoal &&
			currentCalls.length > 0 &&
			pending.size === 0 &&
			!invalid
		) {
			traces.push({
				trace_id: `${metadata.sessionId}:${traceIndex}`,
				success: true,
				calls: currentCalls,
				metadata: { goal: currentGoal, source: "pi-session" },
			});
		}
		traceIndex++;
		currentCalls = [];
		pending.clear();
		batch = 0;
		invalid = false;
	};

	for (const rawEntry of entries) {
		if (!isRecord(rawEntry) || rawEntry.type !== "message") continue;
		const message = rawEntry.message;
		if (!isRecord(message)) continue;
		if (message.role === "user") {
			flush();
			currentGoal = contentText(message.content).trim();
			continue;
		}

		if (message.role === "assistant") {
			const toolCalls = Array.isArray(message.content)
				? message.content.filter(
						(block): block is Record<string, unknown> =>
							isRecord(block) && block.type === "toolCall",
					)
				: [];
			if (toolCalls.length === 0) continue;
			batch++;
			for (const toolCall of toolCalls) {
				const id = typeof toolCall.id === "string" ? toolCall.id : "";
				const name = typeof toolCall.name === "string" ? toolCall.name : "";
				if (!id || !name) {
					invalid = true;
					continue;
				}
				if (name === META_TOOL_SEARCH_NAME) continue;
				if (name === META_TOOL_RUN_NAME) {
					// Never teach discovery from a workflow that already executed a
					// meta-tool; this avoids self-reinforcing catalog entries.
					invalid = true;
					continue;
				}
				const call: TraceCall = {
					name,
					arguments: isRecord(toolCall.arguments) ? toolCall.arguments : {},
					result: "",
					call_id: id,
					turn_id: batch,
					metadata: {},
				};
				currentCalls.push(call);
				pending.set(id, call);
			}
			continue;
		}

		if (message.role === "toolResult") {
			const id =
				typeof message.toolCallId === "string" ? message.toolCallId : "";
			const call = pending.get(id);
			if (!call) continue;
			call.result = parseResult(message.content);
			if (message.isError === true) invalid = true;
			pending.delete(id);
		}
	}

	flush();
	return {
		version: 1,
		sessionId: metadata.sessionId,
		cwd: metadata.cwd,
		createdAt: metadata.createdAt ?? new Date().toISOString(),
		traces,
	};
}

export function extractTrace(ctx: ExtensionContext): TraceExport {
	return extractTraceFromEntries(ctx.sessionManager.getBranch(), {
		sessionId: ctx.sessionManager.getSessionId(),
		cwd: ctx.cwd,
	});
}
