import { createHash, randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Contract = { name: string; description: string; parameters: unknown };
export const sessionDiagnosticsEnabled = () => process.env.GOAL_PLUS_SESSION_DIAGNOSTICS !== "0";
const record = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value);

// One ledger entry per injection of Goal Plus content into a model context.
// The audit script treats these as the authoritative injection registry; any
// new injection site must register through appendInjectionObservation.
export const INJECTION_OBSERVATION_TYPE = "goal-plus-injection-observation";

// Heuristic estimate only: CJK text averages ~1 token per character and ASCII
// ~4 characters per token (cl100k-style ratio). Reports label these estimates
// and never mix them with provider usage numbers.
export function estimateTokens(text: string): number {
	let cjk = 0;
	for (const character of text) {
		const code = character.codePointAt(0)!;
		if ((code >= 0x4e00 && code <= 0x9fff) || (code >= 0x3400 && code <= 0x4dbf)
			|| (code >= 0x3000 && code <= 0x303f) || (code >= 0xff00 && code <= 0xffef)) cjk += 1;
	}
	return Math.ceil(cjk + (text.length - cjk) / 4);
}

export interface InjectionObservation {
	kind: string;
	role: string;
	/** Injected text; measured for chars/tokens/digest when present. */
	content?: string;
	[key: string]: unknown;
}

export function appendInjectionObservation(
	pi: ExtensionAPI,
	ctx: { sessionManager: { getSessionId(): string } },
	observation: InjectionObservation,
): void {
	if (!sessionDiagnosticsEnabled()) return;
	try {
		const { kind, role, content, ...rest } = observation;
		pi.appendEntry(INJECTION_OBSERVATION_TYPE, {
			version: 1,
			session_id: ctx.sessionManager.getSessionId(),
			kind,
			role,
			...(content !== undefined ? {
				chars: content.length,
				est_tokens: estimateTokens(content),
				sha256: createHash("sha256").update(content).digest("hex"),
			} : {}),
			...rest,
		});
	} catch {
		// Optional diagnostics never replace the injection or prevent inference.
	}
}

export function registerSessionDiagnostics(pi: ExtensionAPI, contracts: Map<string, Contract>) {
	let turnIndex: number | undefined;
	let sessionId: string | undefined;
	const branchContracts = new Map<string, Set<string>>();
	pi.on("turn_start", event => { turnIndex = event.turnIndex; });
	pi.on("before_provider_request", (event, ctx) => {
		if (!sessionDiagnosticsEnabled()) return;
		try {
			const payload = record(event.payload) ? event.payload : {};
			const tools: Contract[] = [];
			let supported = Array.isArray(payload.tools);
			for (const item of Array.isArray(payload.tools) ? payload.tools : []) {
				if (!record(item)) { supported = false; continue; }
				const tool = record(item.function) ? item.function : item;
				if (typeof tool.name !== "string") { supported = false; continue; }
				if (!contracts.has(tool.name)) continue;
				const parameters = tool.parameters ?? tool.input_schema;
				if (!record(parameters) || typeof tool.description !== "string") supported = false;
				tools.push({ name: tool.name, description: tool.description as string, parameters });
			}
			const serialized = JSON.stringify({
				registered: [...contracts.values()], request: tools,
			});
			const digest = createHash("sha256").update(serialized).digest("hex");
			if (sessionId !== ctx.sessionManager.getSessionId()) {
				sessionId = ctx.sessionManager.getSessionId();
				branchContracts.clear();
			}
			// Walk only new ancestors; cached sets belong to exact branch entries.
			let cursor = ctx.sessionManager.getLeafId();
			const added: string[] = [];
			while (cursor && !branchContracts.has(cursor)) {
				const entry = ctx.sessionManager.getEntry(cursor);
				if (!entry) break;
				if (entry.type === "custom" && entry.customType === "goal-plus-tool-contracts"
					&& record(entry.data) && typeof entry.data.sha256 === "string") added.push(entry.data.sha256);
				cursor = entry.parentId;
			}
			const known = new Set(cursor ? branchContracts.get(cursor) : []);
			for (const hash of added) known.add(hash);
			if (!known.has(digest)) pi.appendEntry("goal-plus-tool-contracts", {
				version: 1, sha256: digest, serialized,
			});
			pi.appendEntry("goal-plus-provider-request", {
				version: 1, session_id: ctx.sessionManager.getSessionId(),
				request_id: randomUUID(), turn_index: turnIndex,
				model: ctx.model ? { provider: ctx.model.provider, id: ctx.model.id } : null,
				contracts_sha256: digest, status: supported ? "observed" : "unavailable",
				evidence_scope: "provider_request_hook",
				...(process.env.GOAL_PLUS_DEBUG === "1"
					? { request_bytes: Buffer.byteLength(JSON.stringify(payload), "utf8") } : {}),
			});
			known.add(digest);
			const leaf = ctx.sessionManager.getLeafId();
			if (leaf) branchContracts.set(leaf, known);
		} catch {
			// Optional diagnostics never replace the request or prevent inference.
		}
	});
}
