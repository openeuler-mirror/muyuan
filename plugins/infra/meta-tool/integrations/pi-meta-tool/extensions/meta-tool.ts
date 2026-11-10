import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, rm } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type {
	AgentToolResult,
	AgentToolUpdateCallback,
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import {
	createBashToolDefinition,
	createEditToolDefinition,
	createFindToolDefinition,
	createGrepToolDefinition,
	createLsToolDefinition,
	createReadToolDefinition,
	createWriteToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { runBridge } from "../src/bridge.ts";
import { directExecutionAllowed, executeDirectMetaTool } from "../src/direct-execution.ts";
import { isCallerBoundReadBatch } from "../src/read-batches.ts";
import { loadExecutionUserCatalog } from "../src/read-batch-catalog.ts";
import { registerReadBatch } from "./read-batch.ts";
import {
	degradedFingerprints,
	loadCatalogHealth,
	recordCandidateExecution,
	resetCandidateHealth,
} from "../src/catalog-health.ts";
import {
	acquireProjectLease,
	activateCatalogVersion,
	listCatalogVersions,
	loadCatalogManifest,
	loadProjectCatalog,
	materializeRuntimeCatalog,
	mergeCatalogs,
	promoteToUserCatalog,
	publishDiscoveryArtifacts,
} from "../src/catalog-store.ts";
import {
	type ExtractCommandArguments,
	parseExtractCommandArguments,
	withGeneralizedExtraction,
} from "../src/command.ts";
import {
	ensureConfig,
	freshState,
	getPluginPaths,
	loadConfig,
	loadDiscoveredCatalog,
	loadReport,
	loadState,
	recordRun,
	saveState,
} from "../src/config.ts";
import {
	formatMcpProviderStatus,
	McpProviderManager,
} from "../src/mcp-provider.ts";
import {
	loadOnlineExtractionStatus,
	runOnlineExtraction,
} from "../src/online-extraction.ts";
import { OnlineJournalWriter } from "../src/online-journal.ts";
import { planPreview } from "../src/preview.ts";
import { detectProjectProfile } from "../src/project-profile.ts";
import { shouldExposeMetaTools } from "../src/exposure.ts";
import {
	canonicalJson,
	metaToolFingerprint,
	routedCatalogPrompt,
	searchMetaTools as uncachedSearchMetaTools,
} from "../src/router.ts";
import { extractTrace } from "../src/trace.ts";
import {
	type DispatchMode,
	LEGACY_SUPPORTED_TOOL_NAMES,
	type LegacySupportedToolName,
	META_TOOL_COMMAND_NAME,
	META_TOOL_RUN_NAME,
	META_TOOL_SEARCH_NAME,
	type MetaToolCatalog,
	type MetaToolSpec,
	type PluginConfig,
	type PluginState,
} from "../src/types.ts";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const RUN_PARAMETERS = Type.Object({
	metaTool: Type.String({
		description: "Exact meta-tool name from the active catalog",
	}),
	inputs: Type.Record(Type.String(), Type.Unknown(), {
		description: "Inputs matching the selected meta-tool JSON Schema",
	}),
	catalogScope: Type.Optional(
		Type.Union([Type.Literal("project"), Type.Literal("user")], {
			description: "Catalog scope returned by meta_tool_search",
		}),
	),
	catalogVersion: Type.Optional(
		Type.Number({
			description:
				"Immutable project catalog version returned by meta_tool_search",
		}),
	),
	fingerprint: Type.Optional(
		Type.String({
			description: "Workflow fingerprint returned by meta_tool_search",
		}),
	),
	dryRun: Type.Optional(
		Type.Boolean({
			description: "Show the deterministic primitive plan without executing it",
		}),
	),
});

const SEARCH_PARAMETERS = Type.Object({
	query: Type.String({
		description: "Current task or workflow intent to retrieve meta-tools for",
	}),
	requiredTools: Type.Optional(
		Type.Array(Type.String(), {
			description:
				"Only return workflows containing every named primitive tool",
		}),
	),
	topK: Type.Optional(
		Type.Number({ minimum: 1, maximum: 20, description: "Maximum matches" }),
	),
});

interface ExecutableTool {
	name: LegacySupportedToolName;
	description: string;
	parameters: unknown;
	execute(
		toolCallId: string,
		params: Record<string, unknown>,
		signal: AbortSignal | undefined,
		onUpdate: AgentToolUpdateCallback<unknown> | undefined,
		ctx: ExtensionContext,
	): Promise<AgentToolResult<unknown>>;
}

type ProgrammaticCallTool = (
	toolName: string,
	args: Record<string, unknown>,
	signal?: AbortSignal,
) => Promise<AgentToolResult<unknown> & { isError: boolean }>;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function createToolMap(
	cwd: string,
): Map<LegacySupportedToolName, ExecutableTool> {
	const definitions = [
		createReadToolDefinition(cwd),
		createBashToolDefinition(cwd),
		createEditToolDefinition(cwd),
		createWriteToolDefinition(cwd),
		createGrepToolDefinition(cwd),
		createFindToolDefinition(cwd),
		createLsToolDefinition(cwd),
	];
	return new Map(
		definitions.map((definition) => [
			definition.name as LegacySupportedToolName,
			definition as unknown as ExecutableTool,
		]),
	);
}

function programmaticCallTool(
	pi: ExtensionAPI,
): ProgrammaticCallTool | undefined {
	const candidate = (pi as unknown as { callTool?: ProgrammaticCallTool })
		.callTool;
	return typeof candidate === "function" ? candidate.bind(pi) : undefined;
}

/**
 * Return every primitive visible in the current Pi session. This is an
 * observation set, not an execution guarantee: an upstream Pi without
 * ExtensionAPI.callTool can report extension tools here while still being
 * unable to invoke them from meta_tool_run.
 */
function observedToolNames(
	pi: ExtensionAPI,
	providers: McpProviderManager,
): Set<string> {
	const observed = new Set<string>(
		pi
			.getAllTools()
			.map((tool) => tool.name)
			.filter(
				(name) => name !== META_TOOL_RUN_NAME && name !== META_TOOL_SEARCH_NAME,
			),
	);
	for (const name of providers.executableToolNames()) observed.add(name);
	return observed;
}

function executableToolNames(
	pi: ExtensionAPI,
	providers: McpProviderManager,
	observed = observedToolNames(pi, providers),
): Set<string> {
	if (programmaticCallTool(pi)) return observed;
	return new Set([
		...LEGACY_SUPPORTED_TOOL_NAMES.filter((name) => observed.has(name)),
		...providers.executableToolNames(),
	]);
}

function textFromToolResult(result: AgentToolResult<unknown>): string {
	return result.content
		.map((block) =>
			block.type === "text"
				? block.text
				: `[${block.type} output omitted from meta-tool data flow]`,
		)
		.join("\n");
}

function decodeRuntimeToolResult(value: string): unknown {
	const trimmed = value.trim();
	if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
		try {
			return JSON.parse(trimmed);
		} catch {
			// Primitive Pi tools are allowed to return arbitrary text.
		}
	}
	return value;
}

function encodeRuntimeToolResult(value: unknown): string {
	if (typeof value === "string") return value;
	try {
		return JSON.stringify(value);
	} catch {
		return String(value);
	}
}

function journalResult(
	content: AgentToolResult<unknown>["content"],
	details: unknown,
): unknown {
	if (isRecord(details) && details.structuredContent !== undefined)
		return details.structuredContent;
	const text = content
		.flatMap((block) => (block.type === "text" ? [block.text] : []))
		.join("\n")
		.trim();
	if (text.startsWith("{") || text.startsWith("[")) {
		try {
			return JSON.parse(text);
		} catch {
			// Tool text is not required to be JSON.
		}
	}
	return text;
}

function activateMetaTools(pi: ExtensionAPI, enabled: boolean): void {
	const previous = pi.getActiveTools();
	const active = previous
		.filter(
			(name) => name !== META_TOOL_RUN_NAME && name !== META_TOOL_SEARCH_NAME,
		)
		.filter((name) => enabled || name !== "meta_tool_read_batch");
	const next = enabled
			? [...new Set([...active, META_TOOL_SEARCH_NAME, META_TOOL_RUN_NAME])]
			: active;
	if (next.length !== previous.length || next.some((name, i) => name !== previous[i]))
		pi.setActiveTools(next);
}

async function effectiveCatalog(
	cwd: string,
	config: PluginConfig,
	projectVersion?: number,
): Promise<MetaToolCatalog> {
	const project = await loadProjectCatalog({ cwd, version: projectVersion });
	return mergeCatalogs(
		project,
		await loadExecutionUserCatalog(config, config.routing.includeUserCatalog),
	);
}

type SearchOptions = Parameters<typeof uncachedSearchMetaTools>[0];
const ROUTING_CACHE = new Map<string, ReturnType<typeof uncachedSearchMetaTools>>();

function searchMetaTools(options: SearchOptions): ReturnType<typeof uncachedSearchMetaTools> {
	const key = canonicalJson({
		catalogVersion: options.catalogVersion,
		catalog: options.catalog.meta_tools.map((spec) => [
			spec.name,
			metaToolFingerprint(spec),
			spec.metadata,
		]),
		query: options.query,
		profile: {
			tags: options.profile.tags,
			providers: options.profile.providers,
		},
		supportedTools: [...options.supportedTools].sort(),
		topK: options.topK,
		minScore: options.minScore,
		requiredTools: options.requiredTools
			? [...options.requiredTools].sort()
			: undefined,
		blockedFingerprints: options.blockedFingerprints
			? [...options.blockedFingerprints].sort()
			: undefined,
		toolSchemaFingerprints: options.toolSchemaFingerprints
			? [...options.toolSchemaFingerprints.entries()].sort()
			: undefined,
	});
	const cached = ROUTING_CACHE.get(key);
	if (cached) return cached;
	const matches = uncachedSearchMetaTools(options);
	ROUTING_CACHE.set(key, matches);
	if (ROUTING_CACHE.size > 256)
		ROUTING_CACHE.delete(ROUTING_CACHE.keys().next().value as string);
	return matches;
}

type SearchResult = {
	catalog: MetaToolCatalog;
	profile: Awaited<ReturnType<typeof detectProjectProfile>>;
	matches: ReturnType<typeof uncachedSearchMetaTools>;
};

async function findSearchMatches(
	pi: ExtensionAPI,
	providers: McpProviderManager,
	cwd: string,
	query: string,
	options: {
		requiredTools?: Set<string>;
		topK?: number;
	} = {},
): Promise<SearchResult> {
	const config = await loadConfig(cwd, PACKAGE_ROOT);
	const manifest = await loadCatalogManifest(cwd);
	const supportedTools = executableToolNames(pi, providers);
	const profile = await detectProjectProfile({
		cwd,
		toolNames: supportedTools,
		providerNames: providerNames(providers),
	});
	const catalog = await effectiveCatalog(cwd, config);
	const matches = searchMetaTools({
		catalog,
		catalogVersion: manifest?.activeVersion ?? 0,
		query,
		profile,
		supportedTools,
		topK: options.topK ?? config.routing.topK,
		minScore: config.routing.minScore,
		requiredTools: options.requiredTools,
		blockedFingerprints: degradedFingerprints(await loadCatalogHealth(cwd)),
		toolSchemaFingerprints: toolSchemaFingerprints(pi, supportedTools),
	});
	return { catalog, profile, matches };
}

function providerNames(providers: McpProviderManager): string[] {
	return providers.status().providers.map((provider) => provider.id);
}

function toolSchemaFingerprint(
	pi: ExtensionAPI,
	name: string,
): string | undefined {
	return toolSchemaSnapshot(pi).get(name);
}

const TOOL_SCHEMA_CACHE = new WeakMap<object, {
	definitions: readonly unknown[];
	fingerprints: Map<string, string>;
}>();

function toolSchemaSnapshot(pi: ExtensionAPI): Map<string, string> {
	const definitions = pi.getAllTools() as readonly unknown[];
	const cached = TOOL_SCHEMA_CACHE.get(pi as object);
	if (
		cached &&
		cached.definitions.length === definitions.length &&
		cached.definitions.every((value, index) => value === definitions[index])
	)
		return cached.fingerprints;
	const fingerprints = new Map<string, string>();
	for (const value of definitions) {
		if (!isRecord(value) || typeof value.name !== "string") continue;
		try {
			fingerprints.set(
				value.name,
				createHash("sha256")
					.update(canonicalJson(value.parameters))
					.digest("hex"),
			);
		} catch {
			// A non-serializable schema is treated as unavailable provenance.
		}
	}
	TOOL_SCHEMA_CACHE.set(pi as object, { definitions, fingerprints });
	return fingerprints;
}

function toolSchemaFingerprints(
	pi: ExtensionAPI,
	names: Iterable<string>,
): Map<string, string> {
	const snapshot = toolSchemaSnapshot(pi);
	return new Map(
		[...names].flatMap((name) => {
			const fingerprint = snapshot.get(name);
			return fingerprint ? [[name, fingerprint] as const] : [];
		}),
	);
}

function resultProvider(details: unknown): string | undefined {
	if (!isRecord(details)) return undefined;
	const provider =
		typeof details.provider === "string" ? details.provider : undefined;
	const server =
		typeof details.server === "string" ? details.server : undefined;
	return provider && server ? `${provider}:${server}` : provider;
}

function findSpec(
	catalog: MetaToolCatalog,
	name: string,
): MetaToolSpec | undefined {
	return catalog.meta_tools.find((spec) => spec.name === name);
}

function unsupportedTools(
	spec: MetaToolSpec,
	supportedTools: ReadonlySet<string>,
): string[] {
	return [
		...new Set(
			spec.steps
				.map((step) => step.tool)
				.filter((name) => !supportedTools.has(name)),
		),
	];
}

function formatSpec(
	spec: MetaToolSpec,
	piExecutable?: boolean,
	rejectionReasons: string[] = [],
): string[] {
	return [
		`${spec.name}: ${spec.description}`,
		piExecutable === undefined
			? undefined
			: `pi-executable: ${piExecutable ? "yes" : "no"}`,
		...rejectionReasons.map((reason) => `pi-rejection: ${reason}`),
		`inputs: ${JSON.stringify(spec.input_schema)}`,
		`reviewed: ${spec.metadata.reviewed === true ? "yes" : "no"}`,
		`admission: ${typeof spec.metadata.admission_policy === "string" ? spec.metadata.admission_policy : "manual/pending"}`,
		`execution-guard: ${typeof spec.metadata.execution_guard === "string" ? spec.metadata.execution_guard : "legacy"}`,
		`side-effect-free: ${spec.metadata.side_effect_free === true ? "yes" : "no"}`,
		...spec.steps.map(
			(step, index) =>
				`${index + 1}. ${step.tool} ${JSON.stringify(step.arguments)}`,
		),
	].filter((line): line is string => typeof line === "string");
}

function admissionRejections(
	report: Record<string, unknown>,
): Map<string, string[]> {
	const admission = isRecord(report.pi_meta_tool_admission)
		? report.pi_meta_tool_admission
		: {};
	const rejected = Array.isArray(admission.rejected) ? admission.rejected : [];
	return new Map(
		rejected.flatMap((value): [string, string[]][] => {
			if (!isRecord(value) || typeof value.name !== "string") return [];
			const reasons = Array.isArray(value.reasons)
				? value.reasons.filter(
						(reason): reason is string => typeof reason === "string",
					)
				: [];
			return [[value.name, reasons]];
		}),
	);
}

function formatStatus(
	cwd: string,
	state: PluginState,
	activeCatalog: MetaToolCatalog,
	discoveredCatalog: MetaToolCatalog,
	report: Record<string, unknown>,
): string {
	const input =
		typeof report.input === "object" && report.input !== null
			? (report.input as Record<string, unknown>)
			: {};
	return [
		`Meta-tool: ${state.enabled ? "enabled" : "disabled"}`,
		`mode: ${state.mode}`,
		`discovered candidates: ${discoveredCatalog.meta_tools.length}`,
		`Pi-executable meta-tools: ${activeCatalog.meta_tools.length}`,
		`source traces: ${typeof input.traces === "number" ? input.traces : "n/a"}`,
		`source tool calls: ${typeof input.tool_calls === "number" ? input.tool_calls : "n/a"}`,
		`runs: ${state.metrics.successes}/${state.metrics.attempts} succeeded`,
		`last run: ${state.metrics.lastRunAt ?? "n/a"}`,
		`last meta-tool: ${state.metrics.lastMetaTool ?? "n/a"}`,
		state.metrics.lastError
			? `last error: ${state.metrics.lastError}`
			: undefined,
		`state: ${getPluginPaths(cwd).stateDir}`,
	]
		.filter((line): line is string => typeof line === "string")
		.join("\n");
}

function summarize(value: unknown): string {
	return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function requireMetaToolAccess(
	state: PluginState,
	ctx: ExtensionContext,
	operation: "retrieval" | "execution",
): void {
	if (!state.enabled)
		throw new Error(`Meta-tool ${operation} is disabled. Run /meta-tool enable first.`);
	if (!ctx.isProjectTrusted())
		throw new Error(`Meta-tool ${operation} requires a trusted project.`);
}

function resolveUserPath(value: string, cwd: string): string {
	let unquoted = value.trim();
	if (
		(unquoted.startsWith('"') && unquoted.endsWith('"')) ||
		(unquoted.startsWith("'") && unquoted.endsWith("'"))
	)
		unquoted = unquoted.slice(1, -1);
	if (unquoted === "~") return homedir();
	if (unquoted.startsWith("~/"))
		unquoted = resolve(homedir(), unquoted.slice(2));
	return resolve(cwd, unquoted);
}

function configureGoalPlusWorkerPropagation(): void {
	if (process.env.META_TOOL_PI_DISABLE_WORKER_PROPAGATION === "1") return;
	const wrapperDir = join(PACKAGE_ROOT, "bin");
	const wrapperPath = join(wrapperDir, "pi");
	if (!existsSync(wrapperPath)) return;

	const pathEntries = (process.env.PATH ?? "")
		.split(process.platform === "win32" ? ";" : ":")
		.filter(Boolean);
	if (!pathEntries.includes(wrapperDir))
		process.env.PATH = [wrapperDir, ...pathEntries].join(
			process.platform === "win32" ? ";" : ":",
		);

	const extensionPath = fileURLToPath(import.meta.url);
	const existing = process.env.META_TOOL_PI_EXTRA_EXTENSIONS
		?.split(process.platform === "win32" ? ";" : ":")
		.filter(Boolean) ?? [];
	if (!existing.includes(extensionPath))
		process.env.META_TOOL_PI_EXTRA_EXTENSIONS = [
			extensionPath,
			...existing,
		].join(process.platform === "win32" ? ";" : ":");
}

export default function metaToolExtension(pi: ExtensionAPI) {
	configureGoalPlusWorkerPropagation();
	let state = freshState();
	const providers = new McpProviderManager();
	let journal: OnlineJournalWriter | undefined;
	const pendingGoals: string[] = [];
	let turnIndex = 0;
	let metaExecutionDepth = 0;
	let onlineTimer: ReturnType<typeof setTimeout> | undefined;
	let onlineTimerDueAt: number | undefined;
	let onlineRun: Promise<void> | undefined;
	let closing = false;

	const runOnline = async (
		ctx: ExtensionContext,
		force = false,
		drain = false,
	): Promise<void> => {
		if (onlineRun) {
			await onlineRun;
			if (!drain && !force) return;
		}
		onlineRun = (async () => {
			const config = providers.augmentConfig(
				await loadConfig(ctx.cwd, PACKAGE_ROOT),
			);
			const observedTools = observedToolNames(pi, providers);
			const status = await runOnlineExtraction({
				cwd: ctx.cwd,
				config,
				observedTools,
				supportedTools: executableToolNames(pi, providers, observedTools),
				force,
				drain,
				onProgress: (message) => ctx.ui.setStatus("meta-tool-online", message),
			});
			ctx.ui.setStatus("meta-tool-online", undefined);
			if (status.published) {
				const catalog = await effectiveCatalog(ctx.cwd, config);
				// Pi applies setActiveTools to the next model turn, including
				// turns in the same long-running user task.
				activateMetaTools(pi, shouldExposeMetaTools({
					enabled: state.enabled,
					trusted: ctx.isProjectTrusted(),
					activeCatalogSize: catalog.meta_tools.length,
					minActiveCatalogSize: config.routing.minActiveCatalogSize,
				}));
				ctx.ui.notify(
					`Online meta-tool catalog v${status.activeCatalogVersion} published: ${status.reason}.`,
					"info",
				);
			}
			else if (status.state === "failed")
				ctx.ui.notify(
					`Online meta-tool extraction failed: ${status.error}`,
					"warning",
				);
			else if (status.state === "busy") scheduleOnline(ctx, false);
			else if (status.state === "pending")
				scheduleOnline(
					ctx,
					false,
					config.online.debounceMs > 0
						? Math.min(
								config.online.debounceMs,
								status.retryAfterMs ?? config.online.maxWaitMs,
							)
						: (status.retryAfterMs ?? config.online.maxWaitMs),
				);
			if (
				status.state === "succeeded" &&
				(status.remainingSuccessfulTraces ?? 0) > 0
			)
				scheduleOnline(ctx, force, 0);
		})().finally(() => {
			onlineRun = undefined;
		});
		return onlineRun;
	};

	const scheduleOnline = (
		ctx: ExtensionContext,
		force = false,
		delay?: number,
	): void => {
		if (closing) return;
		void loadConfig(ctx.cwd, PACKAGE_ROOT).then((config) => {
			if (closing) return;
			if (!config.online.enabled && !force) return;
			const effectiveDelay = delay ?? (force ? 0 : config.online.debounceMs);
			const dueAt = Date.now() + effectiveDelay;
			// A burst of successful tasks must not keep pushing a debounce timer
			// into the future forever. Preserve the earliest scheduled check.
			if (
				onlineTimer &&
				onlineTimerDueAt !== undefined &&
				onlineTimerDueAt <= dueAt
			)
				return;
			if (onlineTimer) clearTimeout(onlineTimer);
			onlineTimerDueAt = dueAt;
			onlineTimer = setTimeout(() => {
				onlineTimer = undefined;
				onlineTimerDueAt = undefined;
				void runOnline(ctx, force).catch((error: unknown) =>
					ctx.ui.notify(`Online extraction error: ${String(error)}`, "warning"),
				);
			}, effectiveDelay);
			onlineTimer.unref?.();
		});
	};

	pi.registerTool({
		name: META_TOOL_SEARCH_NAME,
		label: "Meta-tool Search",
		description:
			"Search the persistent project/user meta-tool catalogs for workflows relevant to the current task. Results are hard-filtered against tools registered in this Pi session and include immutable execution pins.",
		promptSnippet: "Search reusable meta-tool workflows for the current task",
		promptGuidelines: [
			"Use meta_tool_search when the task changes or the injected relevant meta-tool subset has no clear match.",
		],
		parameters: SEARCH_PARAMETERS,
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			requireMetaToolAccess(state, ctx, "retrieval");
			const requestedTopK =
				typeof params.topK === "number"
					? Math.max(1, Math.min(20, Math.trunc(params.topK)))
					: undefined;
			const requiredTools = Array.isArray(params.requiredTools)
				? new Set<string>(
						params.requiredTools.filter(
							(value): value is string => typeof value === "string",
						),
					)
				: undefined;
			const { catalog, profile, matches } = await findSearchMatches(
				pi,
				providers,
				ctx.cwd,
				params.query,
				{ requiredTools, topK: requestedTopK },
			);
			return {
				content: [
					{
						type: "text",
						text: JSON.stringify(
							{
								query: params.query,
								projectTags: profile.tags,
								catalogCandidates: catalog.meta_tools.length,
								matches,
							},
							null,
							2,
						),
					},
				],
				details: { matches, profile },
			};
		},
	});

	pi.registerTool({
		name: META_TOOL_RUN_NAME,
		label: "Meta-tool Runner",
		description:
			"Execute one admitted generalized meta-tool as a deterministic sequence of registered Pi or provider tools. Prefer the scope, version, and fingerprint returned by meta_tool_search.",
		promptSnippet: "Run an admitted deterministic meta-tool workflow",
		promptGuidelines: [
			"Prefer meta_tool_run for an exact match to an enabled catalog entry; use ordinary tools for novel or ambiguous work.",
			"If meta_tool_run fails, continue with ordinary tools instead of repeatedly retrying the same workflow.",
		],
		parameters: RUN_PARAMETERS,
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			requireMetaToolAccess(state, ctx, "execution");

			const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
			const catalog =
				params.catalogScope === "user"
					? await loadExecutionUserCatalog(config)
					: params.catalogVersion !== undefined
						? await loadProjectCatalog({
								cwd: ctx.cwd,
								version: Math.trunc(params.catalogVersion),
							})
						: await effectiveCatalog(ctx.cwd, config);
			const spec = findSpec(catalog, params.metaTool);
			if (!spec) {
				const names = catalog.meta_tools.map((item) => item.name).join(", ");
				throw new Error(
					`Unknown meta-tool ${JSON.stringify(params.metaTool)}; available: ${names || "none"}`,
				);
			}
			const fingerprint = metaToolFingerprint(spec);
			if (isCallerBoundReadBatch(spec) &&
				config.admission.policy?.allow_caller_bound_read_batches !== true)
				throw new Error("Caller-planned read composition is not enabled");
			if (params.fingerprint && params.fingerprint !== fingerprint)
				throw new Error(
					`Meta-tool fingerprint mismatch for ${spec.name}; search again before execution.`,
				);
			const supportedTools = executableToolNames(pi, providers);
			const unsupported = unsupportedTools(spec, supportedTools);
			if (unsupported.length > 0)
				throw new Error(
					`Meta-tool references unavailable Pi/provider tools: ${unsupported.join(", ")}`,
				);
			const expectedSchemas = isRecord(
				spec.metadata.required_tool_schema_fingerprints,
			)
				? spec.metadata.required_tool_schema_fingerprints
				: {};
			if (
				isRecord(spec.metadata.tool_schema_conflicts) &&
				Object.keys(spec.metadata.tool_schema_conflicts).length > 0
			)
				throw new Error(
					`Meta-tool has conflicting primitive schema evidence: ${Object.keys(spec.metadata.tool_schema_conflicts).join(", ")}`,
				);
			const currentSchemas = toolSchemaFingerprints(pi, supportedTools);
			const runtimeSchemaFingerprint = createHash("sha256")
				.update(
					JSON.stringify(
						[...new Set(spec.steps.map((step) => step.tool))]
							.sort()
							.map((name) => [name, currentSchemas.get(name)]),
					),
				)
				.digest("hex");
			const schemaMismatches = Object.entries(expectedSchemas).flatMap(
				([name, expected]) =>
					typeof expected === "string" && currentSchemas.get(name) !== expected
						? [name]
						: [],
			);
			if (schemaMismatches.length > 0)
				throw new Error(
					`Meta-tool primitive schema changed; search/extract again: ${schemaMismatches.join(", ")}`,
				);

			if (params.dryRun === true) {
				return {
					content: [{ type: "text", text: planPreview(spec, params.inputs) }],
					details: { metaTool: spec.name, dryRun: true, mode: state.mode },
				};
			}
			const health = await loadCatalogHealth(ctx.cwd);
			if (health.records[fingerprint]?.state === "degraded")
				throw new Error(
					`${spec.name} is degraded after repeated execution failures. Use ordinary tools or run /meta-tool health reset ${spec.name} after fixing its provider/schema.`,
				);

			const guardedMutation =
				spec.metadata.execution_guard === "confirm_each_run" ||
				spec.metadata.side_effect_free !== true;
			const nonInteractiveGuardPreapproved =
				config.admission.allow_noninteractive_guarded_execution === true;
			const effectiveMode: DispatchMode =
				state.mode === "force" || guardedMutation ? "force" : "safe_auto";
			if (effectiveMode === "force") {
				if (!ctx.hasUI && !nonInteractiveGuardPreapproved)
					throw new Error(
						"This meta-tool requires interactive plan confirmation.",
					);
				if (ctx.hasUI) {
					const approved = await ctx.ui.confirm(
						guardedMutation
							? "Run mutating meta-tool?"
							: spec.metadata.side_effect_free === true
								? "Run unguarded meta-tool?"
								: "Run potentially mutating meta-tool?",
						planPreview(spec, params.inputs),
					);
					if (!approved)
						throw new Error("Meta-tool plan was rejected by the user.");
				}
			}

			const runtimePolicy = isRecord(config.admission.policy)
				? config.admission.policy
				: {};
			const useDirectExecution = directExecutionAllowed(
				spec,
				effectiveMode,
				runtimePolicy,
			);
			const runtimeCatalogFile = useDirectExecution
				? undefined
				: await materializeRuntimeCatalog({
						cwd: ctx.cwd,
						spec,
						fingerprint,
					});
			const callTool = programmaticCallTool(pi);
			const legacyTools = callTool ? undefined : createToolMap(ctx.cwd);
			onUpdate?.({
				content: [{ type: "text", text: `Meta-tool executing ${spec.name}` }],
				details: { metaTool: spec.name, dryRun: false, mode: effectiveMode },
			});
			const startedAt = Date.now();
			let lastPrimitiveTool: string | undefined;
			let lastPrimitiveStep = 0;
			let executionPath: "direct" | "bridge" = "bridge";
			const catalogVersion =
				typeof spec.metadata.catalog_version === "number"
					? spec.metadata.catalog_version
					: typeof params.catalogVersion === "number"
						? Math.trunc(params.catalogVersion)
						: 0;
			try {
				metaExecutionDepth += 1;
				let result: unknown;
				try {
					const stepConfirmedTools = new Set(
						Array.isArray(
							spec.metadata.runtime_derived_mutation_tools_confirmed_at_step,
						)
							? spec.metadata.runtime_derived_mutation_tools_confirmed_at_step.filter(
									(value): value is string => typeof value === "string",
								)
							: [],
					);
					const callPrimitive = async (
						id: string,
						name: string,
						input: Record<string, unknown>,
					): Promise<unknown> => {
						lastPrimitiveTool = name;
						lastPrimitiveStep += 1;
						if (!supportedTools.has(name))
							throw new Error(
								`Unsupported primitive requested by meta-tool: ${name}`,
							);
						if (stepConfirmedTools.has(name)) {
							if (!ctx.hasUI && !nonInteractiveGuardPreapproved)
								throw new Error(
									`Resolved mutation step ${name} requires interactive confirmation.`,
								);
							if (ctx.hasUI) {
								const approved = await ctx.ui.confirm(
									`Run resolved mutation step ${name}?`,
									JSON.stringify(input, null, 2),
								);
								if (!approved)
									throw new Error(
										`Resolved mutation step ${name} was rejected.`,
									);
							}
						}
						if (callTool) {
							const toolResult = await callTool(name, input, signal);
							const text = textFromToolResult(toolResult);
							if (toolResult.isError)
								throw new Error(text || `Pi tool failed: ${name}`);
							return decodeRuntimeToolResult(text);
						}
						if (providers.hasTool(name)) {
							const toolResult = await providers.callTool(
								name,
								input,
								signal,
								ctx.isProjectTrusted(),
							);
							return decodeRuntimeToolResult(textFromToolResult(toolResult));
						}
						const tool = legacyTools?.get(name as LegacySupportedToolName);
						if (!tool) throw new Error(`Pi tool is unavailable: ${name}`);
						const toolResult = await tool.execute(
							id,
							input,
							signal,
							undefined,
							ctx,
						);
						return decodeRuntimeToolResult(textFromToolResult(toolResult));
					};

					if (useDirectExecution) {
						executionPath = "direct";
						result = await executeDirectMetaTool(
							spec,
							params.inputs,
							(id, name, input) => callPrimitive(id, name, input),
						);
					} else {
						result = await runBridge({
							pythonCommand: config.pythonCommand,
							bridgePath: config.bridgePath,
							metaToolRoot: config.metaToolRoot,
							timeoutMs: config.timeoutMs,
							signal,
							payload: {
								operation: "run",
								catalogFile: runtimeCatalogFile as string,
								metaTool: spec.name,
								inputs: params.inputs,
								mode: effectiveMode,
								policy: runtimePolicy,
								supportedTools: [...supportedTools],
							},
							onProgress(message) {
								onUpdate?.({
									content: [{ type: "text", text: message }],
									details: {
										metaTool: spec.name,
										dryRun: false,
										mode: effectiveMode,
									},
								});
							},
							onToolCall: async (id, name, input) =>
								encodeRuntimeToolResult(await callPrimitive(id, name, input)),
						});
					}
				} finally {
					metaExecutionDepth -= 1;
				}
				await recordCandidateExecution({
					cwd: ctx.cwd,
					name: spec.name,
					fingerprint,
					catalogVersion,
					durationMs: Date.now() - startedAt,
					lastPrimitiveTool,
					lastPrimitiveStep:
						lastPrimitiveStep > 0 ? lastPrimitiveStep : undefined,
					toolSchemaFingerprint: runtimeSchemaFingerprint,
				}).catch((error: unknown) =>
					ctx.ui.setStatus(
						"meta-tool-online",
						`health telemetry error: ${error instanceof Error ? error.message : String(error)}`,
					),
				);
				state = await recordRun(ctx.cwd, spec.name);
				ctx.ui.setStatus(
					"meta-tool",
					`meta-tool: ${state.mode} | runs ${state.metrics.successes}/${state.metrics.attempts}`,
				);
					return {
						content: [{ type: "text", text: summarize(result) }],
						details: {
							metaTool: spec.name,
							dryRun: false,
							mode: effectiveMode,
							executionPath,
						},
					};
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				const health = await recordCandidateExecution({
					cwd: ctx.cwd,
					name: spec.name,
					fingerprint,
					catalogVersion,
					durationMs: Date.now() - startedAt,
					error: message,
					lastPrimitiveTool,
					lastPrimitiveStep:
						lastPrimitiveStep > 0 ? lastPrimitiveStep : undefined,
					toolSchemaFingerprint: runtimeSchemaFingerprint,
				}).catch(() => undefined);
				state = await recordRun(ctx.cwd, spec.name, message);
				if (health?.state === "degraded")
					ctx.ui.notify(
						`${spec.name} was degraded after ${health.consecutiveFailures} consecutive failures and will no longer be retrieved.`,
						"warning",
					);
				throw error;
			}
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		closing = false;
		if (onlineTimer) clearTimeout(onlineTimer);
		onlineTimer = undefined;
		onlineTimerDueAt = undefined;
		await journal?.seal();
		await journal?.flush();
		journal = undefined;
		pendingGoals.length = 0;
		await providers.refresh(pi, ctx.cwd, ctx.isProjectTrusted());
		const sessionConfig = await loadConfig(ctx.cwd, PACKAGE_ROOT);
		state = await loadState(ctx.cwd);
		if (!ctx.isProjectTrusted()) state = { ...state, enabled: false };
		const sessionCatalog = await effectiveCatalog(ctx.cwd, sessionConfig);
		activateMetaTools(
			pi,
			shouldExposeMetaTools({
				enabled: state.enabled,
				trusted: ctx.isProjectTrusted(),
				activeCatalogSize: sessionCatalog.meta_tools.length,
				minActiveCatalogSize: sessionConfig.routing.minActiveCatalogSize,
			}),
		);
		turnIndex = 0;
		if (ctx.isProjectTrusted() && sessionConfig.online.enabled) {
			const observedTools = observedToolNames(pi, providers);
			const supportedTools = executableToolNames(pi, providers, observedTools);
			const profile = await detectProjectProfile({
				cwd: ctx.cwd,
				toolNames: supportedTools,
				providerNames: providerNames(providers),
			});
			journal = await OnlineJournalWriter.open(
				ctx.cwd,
				ctx.sessionManager.getSessionId(),
				{
					plugin_version: "2.0.0",
					project_tags: profile.tags,
					providers: profile.providers,
					tool_schema_fingerprints: Object.fromEntries(
						[...observedTools].map((name) => [
							name,
							toolSchemaFingerprint(pi, name) ?? "unknown",
						]),
					),
				},
				sessionConfig.online.persistOnEachToolResult,
				sessionConfig.online.journalFlushInterval,
			);
		} else journal = undefined;
		ctx.ui.setStatus(
			"meta-tool",
			state.enabled
				? `meta-tool: ${state.mode} | runs ${state.metrics.successes}/${state.metrics.attempts}`
				: undefined,
		);
		if (ctx.isProjectTrusted() && sessionConfig.online.enabled)
			scheduleOnline(ctx, false, 1_000);
	});

	pi.on("input", async (event) => {
		if (event.source === "extension" || !journal) return;
		if (event.streamingBehavior === "steer") return;
		if (event.streamingBehavior === "followUp") {
			pendingGoals.push(event.text);
			return;
		}
		await journal.beginTask(event.text);
	});

	pi.on("turn_start", async (event) => {
		turnIndex = event.turnIndex;
	});

	pi.on("tool_result", async (event, ctx) => {
		if (!journal || metaExecutionDepth > 0) return;
		await journal
			.recordToolResult({
				toolCallId: event.toolCallId,
				toolName: event.toolName,
				input: event.input,
				result: journalResult(event.content, event.details),
				isError: event.isError,
				turnId: turnIndex,
				toolSchemaFingerprint: toolSchemaFingerprint(pi, event.toolName),
				provider: resultProvider(event.details),
			})
			.catch((error: unknown) =>
				ctx.ui.setStatus(
					"meta-tool-online",
					`journal error: ${error instanceof Error ? error.message : String(error)}`,
				),
			);
	});

	pi.on("turn_end", async (_event, ctx) => {
		if (!journal || closing) return;
		try {
			const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
			if (await journal.checkpoint(config.online.segmentCalls))
				scheduleOnline(ctx);
		} catch (error) {
			ctx.ui.setStatus("meta-tool-online", `journal checkpoint error: ${String(error)}`);
		}
	});

	pi.on("agent_settled", async (_event, ctx) => {
		await journal?.seal();
		const sealStats = journal?.getLastSealStats();
		await journal?.flush();
		if (sealStats && sealStats.primitiveCalls > 0) {
			const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
			if (config.online.enabled && config.online.extractOnAgentSettled)
				scheduleOnline(ctx);
		}
		const nextGoal = pendingGoals.shift();
		if (nextGoal && journal) await journal.beginTask(nextGoal);
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		closing = true;
		if (onlineTimer) clearTimeout(onlineTimer);
		onlineTimer = undefined;
		onlineTimerDueAt = undefined;
		await journal?.seal();
		await journal?.flush();
		// Single-shot Pi exits immediately after agent_settled. Await the last
		// bounded discovery instead of cancelling the only extraction opportunity.
		await onlineRun;
		if (journal && ctx.isProjectTrusted()) await runOnline(ctx, false, true);
		journal = undefined;
		pendingGoals.length = 0;
		await providers.close();
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!state.enabled || !ctx.isProjectTrusted()) return;
		const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
		const supportedTools = executableToolNames(pi, providers);
		const manifest = await loadCatalogManifest(ctx.cwd);
		const catalog = await effectiveCatalog(ctx.cwd, config);
		const exposeMetaTools = shouldExposeMetaTools({
			enabled: state.enabled,
			trusted: ctx.isProjectTrusted(),
			activeCatalogSize: catalog.meta_tools.length,
			minActiveCatalogSize: config.routing.minActiveCatalogSize,
		});
		activateMetaTools(pi, exposeMetaTools);
		if (!exposeMetaTools) return { systemPrompt: event.systemPrompt };
		const profile = await detectProjectProfile({
			cwd: ctx.cwd,
			toolNames: supportedTools,
			providerNames: providerNames(providers),
		});
		const matches = config.routing.enabled
			? searchMetaTools({
					catalog,
					catalogVersion: manifest?.activeVersion ?? 0,
					query: event.prompt,
					profile,
					supportedTools,
					topK: config.routing.topK,
					minScore: config.routing.minScore,
					blockedFingerprints: degradedFingerprints(
						await loadCatalogHealth(ctx.cwd),
					),
					toolSchemaFingerprints: toolSchemaFingerprints(pi, supportedTools),
				})
			: [];
		const routed = routedCatalogPrompt(
			matches,
			config.routing.maxPromptCharacters,
		);
		const prompt = [
			"<meta-tool-routing>",
			`Meta-tools are enabled in ${state.mode} mode. Catalog retrieval is task-scoped: do not assume an unrelated workflow is available.`,
			"Use a relevant injected result when it is an exact workflow match. Otherwise call meta_tool_search with the current task; use ordinary tools for novel or ambiguous work.",
			"Pass catalogScope, catalogVersion, and fingerprint from a search result to meta_tool_run so execution is pinned.",
			`Project tags: ${profile.tags.join(", ") || "none"}`,
			routed,
			"</meta-tool-routing>",
		]
			.filter(Boolean)
			.join("\n");
		return { systemPrompt: `${event.systemPrompt}\n\n${prompt}` };
	});

	pi.registerCommand(META_TOOL_COMMAND_NAME, {
		description:
			"Discover, inspect, review, enable, or execute deterministic meta-tools",
		getArgumentCompletions: (prefix) => {
			const commands = [
				"extract",
				"online status",
				"online run",
				"search",
				"promote",
				"health status",
				"health reset",
				"catalog list",
				"catalog rollback",
				"providers",
				"enable",
				"disable",
				"status",
				"inspect",
				"config",
			];
			const matches = commands.filter((command) => command.startsWith(prefix));
			return matches.length
				? matches.map((command) => ({ value: command, label: command }))
				: null;
		},
		handler: async (args, ctx) => {
			const commandText = args.trim();
			const subcommand = commandText.split(/\s+/, 1)[0] || "status";
			const subcommandArgs = commandText.slice(subcommand.length).trim();

			if (subcommand === "extract") {
				let extractArguments: ExtractCommandArguments;
				try {
					extractArguments = parseExtractCommandArguments(subcommandArgs);
				} catch (error) {
					ctx.ui.notify(
						`Invalid extract arguments: ${error instanceof Error ? error.message : String(error)}`,
						"warning",
					);
					return;
				}
				await ctx.waitForIdle();
				await providers.refresh(pi, ctx.cwd, ctx.isProjectTrusted());
				const config = providers.augmentConfig(
					await loadConfig(ctx.cwd, PACKAGE_ROOT),
				);
				const paths = getPluginPaths(ctx.cwd);
				const observedTools = observedToolNames(pi, providers);
				const supportedTools = executableToolNames(pi, providers, observedTools);
				const tracePath = extractArguments.tracePath
					? resolveUserPath(extractArguments.tracePath, ctx.cwd)
					: undefined;
				const trace = tracePath ? undefined : extractTrace(ctx);
				if (trace) {
					const schemas = Object.fromEntries(
						[...observedTools].map((name) => [
							name,
							toolSchemaFingerprint(pi, name) ?? "unknown",
						]),
					);
					for (const item of trace.traces)
						item.metadata = {
							...item.metadata,
							session_id: trace.sessionId,
							tool_schema_fingerprints: schemas,
						};
				}
				if (trace && trace.traces.length === 0) {
					ctx.ui.notify(
						"No completed successful Pi tool traces found on the current branch.",
						"warning",
					);
					return;
				}
				ctx.ui.setStatus(
					"meta-tool",
					"meta-tool: discovering generalized workflows",
				);
				const stagingDirectory = join(
					paths.stagingDir,
					`manual-${randomUUID()}`,
				);
				const extractionLease = await acquireProjectLease(
					ctx.cwd,
					config.online.leaseMs,
				);
				if (!extractionLease) {
					ctx.ui.setStatus("meta-tool", undefined);
					ctx.ui.notify(
						"Another Pi session is publishing this project's catalog; retry extract shortly.",
						"warning",
					);
					return;
				}
				try {
					await mkdir(stagingDirectory, { recursive: true });
					const resultValue = await runBridge({
						pythonCommand: config.pythonCommand,
						bridgePath: config.bridgePath,
						metaToolRoot: config.metaToolRoot,
						timeoutMs: config.timeoutMs,
						signal: undefined,
						payload: {
							operation: "discover",
							...(tracePath ? { tracePath } : { trace }),
							outputDir: stagingDirectory,
							adapter: {
								supportedTools: [...supportedTools],
								observedTools: [...observedTools],
							},
							config: {
								normalization: withGeneralizedExtraction(config.normalization),
								extraction: config.extraction,
								admission: {
									...config.admission,
									// Manual extraction is an explicit user action and may
									// intentionally use repeated tasks from one Pi session.
									min_distinct_sessions: 1,
								},
							},
						},
						onProgress: (message) => ctx.ui.setStatus("meta-tool", message),
					});
					const result = isRecord(resultValue) ? resultValue : {};
					const traceCount =
						typeof result.traces === "number"
							? result.traces
							: (trace?.traces.length ?? 0);
					const toolCallCount =
						typeof result.toolCalls === "number"
							? result.toolCalls
							: (trace?.traces.reduce(
									(sum, item) => sum + item.calls.length,
									0,
								) ?? 0);
					await extractionLease.assertOwned();
					await publishDiscoveryArtifacts({
						cwd: ctx.cwd,
						stagingDirectory,
						source: "manual",
						traceCount,
						toolCallCount,
						preservePreviousVersions: config.catalog.preservePreviousVersions,
					});
					const catalog = await loadProjectCatalog({ cwd: ctx.cwd });
					const exposedCatalog = await effectiveCatalog(ctx.cwd, config);
					const discoveredCatalog = await loadDiscoveredCatalog(ctx.cwd);
					const direct = catalog.meta_tools.filter(
						(spec) => spec.metadata.execution_guard !== "confirm_each_run",
					).length;
					const guarded = catalog.meta_tools.length - direct;
					const autoEnabled = ctx.isProjectTrusted();
					state = await saveState(ctx.cwd, autoEnabled, "safe_auto");
					activateMetaTools(
						pi,
						shouldExposeMetaTools({
							enabled: autoEnabled,
							trusted: ctx.isProjectTrusted(),
							activeCatalogSize: exposedCatalog.meta_tools.length,
							minActiveCatalogSize: config.routing.minActiveCatalogSize,
						}),
					);
					ctx.ui.notify(
						catalog.meta_tools.length > 0
							? autoEnabled
								? `Meta-tool ready: ${catalog.meta_tools.length} workflow(s) admitted (${direct} direct, ${guarded} confirm-before-change); automatic mode is enabled for the next matching task. ${summarize(result)}`
								: `Meta-tool admitted ${catalog.meta_tools.length} workflow(s), but the project is not trusted, so execution remains disabled. ${summarize(result)}`
							: discoveredCatalog.meta_tools.length > 0
								? `Meta-tool discovery found ${discoveredCatalog.meta_tools.length} core candidate(s), but none passed the current Pi/provider and safety admission checks. Discovery results remain in meta_tools.json; see rejected_meta_tools.json for exact reasons. ${summarize(result)}`
								: `Meta-tool discovery found no recurring workflow candidate. See report.json for diagnostics. ${summarize(result)}`,
						autoEnabled ? "info" : "warning",
					);
				} catch (error) {
					await rm(stagingDirectory, { recursive: true, force: true });
					ctx.ui.notify(
						`Meta-tool discovery failed: ${error instanceof Error ? error.message : String(error)}`,
						"error",
					);
				} finally {
					await extractionLease.release();
					ctx.ui.setStatus(
						"meta-tool",
						state.enabled
							? `meta-tool: ${state.mode} | runs ${state.metrics.successes}/${state.metrics.attempts}`
							: undefined,
					);
				}
				return;
			}

			if (subcommand === "online") {
				const action = subcommandArgs || "status";
				if (action === "run") {
					if (!ctx.isProjectTrusted()) {
						ctx.ui.notify(
							"Trust this project before running online extraction.",
							"error",
						);
						return;
					}
					await ctx.waitForIdle();
					await providers.refresh(pi, ctx.cwd, true);
					await runOnline(ctx, true);
				} else if (action !== "status") {
					ctx.ui.notify("Usage: /meta-tool online [status|run]", "warning");
					return;
				}
				const status = await loadOnlineExtractionStatus(ctx.cwd);
				const manifest = await loadCatalogManifest(ctx.cwd);
				ctx.ui.notify(
					status
						? [
								`online state: ${status.state}`,
								`reason: ${status.reason}`,
								`new eligible traces: ${status.newSuccessfulTraces}`,
								`snapshot traces/calls: ${status.totalSuccessfulTraces}/${status.toolCalls}`,
								`active catalog: v${manifest?.activeVersion ?? 0}`,
								status.error ? `error: ${status.error}` : undefined,
							]
								.filter(Boolean)
								.join("\n")
						: "Online extraction has not run yet.",
					"info",
				);
				return;
			}

			if (subcommand === "search") {
				if (!subcommandArgs) {
					ctx.ui.notify(
						"Usage: /meta-tool search <task description>",
						"warning",
					);
					return;
				}
				const { matches } = await findSearchMatches(
					pi,
					providers,
					ctx.cwd,
					subcommandArgs,
				);
				ctx.ui.notify(
					matches.length
						? JSON.stringify(matches, null, 2)
						: "No executable meta-tool matched this task.",
					matches.length ? "info" : "warning",
				);
				return;
			}

			if (subcommand === "promote") {
				if (!subcommandArgs) {
					ctx.ui.notify(
						"Usage: /meta-tool promote <meta-tool-name>",
						"warning",
					);
					return;
				}
				const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
				const spec = findSpec(
					await loadProjectCatalog({ cwd: ctx.cwd }),
					subcommandArgs,
				);
				if (!spec) {
					ctx.ui.notify(
						`Unknown active project meta-tool: ${subcommandArgs}`,
						"error",
					);
					return;
				}
				if (!ctx.hasUI) {
					ctx.ui.notify(
						"Promotion requires an interactive Pi session.",
						"error",
					);
					return;
				}
				const approved = await ctx.ui.confirm(
					"Promote this workflow to the user catalog?",
					`${planPreview(spec)}\n\nIt may be retrieved by other projects when their tools and task semantics match.`,
				);
				if (!approved) return;
				const path = await promoteToUserCatalog({
					userCatalogDir: config.catalog.userCatalogDir,
					spec,
				});
				ctx.ui.notify(`Promoted ${spec.name} to ${path}`, "info");
				return;
			}

			if (subcommand === "health") {
				const [action = "status", ...nameParts] = subcommandArgs.split(/\s+/);
				if (action === "reset") {
					const name = nameParts.join(" ").trim();
					if (!name) {
						ctx.ui.notify(
							"Usage: /meta-tool health reset <meta-tool-name>",
							"warning",
						);
						return;
					}
					const changed = await resetCandidateHealth({ cwd: ctx.cwd, name });
					ctx.ui.notify(
						changed > 0
							? `Reset ${changed} health record(s) for ${name}.`
							: `No health record found for ${name}.`,
						changed > 0 ? "info" : "warning",
					);
					return;
				}
				if (action !== "status") {
					ctx.ui.notify(
						"Usage: /meta-tool health [status|reset <name>]",
						"warning",
					);
					return;
				}
				const health = await loadCatalogHealth(ctx.cwd);
				const records = Object.values(health.records).sort((left, right) =>
					left.name.localeCompare(right.name),
				);
				ctx.ui.notify(
					records.length
						? records
								.map(
									(record) =>
										`${record.name}: ${record.state}, ${record.successes}/${record.attempts} successful, consecutive failures=${record.consecutiveFailures}, last=${record.lastDurationMs}ms${record.lastPrimitiveTool ? `, primitive=${record.lastPrimitiveStep ?? "?"}:${record.lastPrimitiveTool}` : ""}${record.lastError ? `, error=${record.lastError}` : ""}`,
								)
								.join("\n")
						: "No meta-tool execution health records yet.",
					"info",
				);
				return;
			}

			if (subcommand === "catalog") {
				const [action = "list", versionText] = subcommandArgs.split(/\s+/);
				if (action === "list") {
					const versions = await listCatalogVersions(ctx.cwd);
					const active = (await loadCatalogManifest(ctx.cwd))?.activeVersion;
					ctx.ui.notify(
						versions.length
							? versions
									.map(
										(version) =>
											`v${version}${version === active ? " (active)" : ""}`,
									)
									.join("\n")
							: "No versioned project catalog yet.",
						"info",
					);
					return;
				}
				if (action !== "rollback" || !/^v?\d+$/.test(versionText ?? "")) {
					ctx.ui.notify(
						"Usage: /meta-tool catalog [list|rollback <version>]",
						"warning",
					);
					return;
				}
				const version = Number((versionText as string).replace(/^v/, ""));
				if (!ctx.hasUI) {
					ctx.ui.notify(
						"Catalog rollback requires an interactive Pi session.",
						"error",
					);
					return;
				}
				if (
					!(await ctx.ui.confirm(
						`Activate catalog v${version}?`,
						"The immutable version is preserved; the manifest and compatibility mirrors will point to it atomically.",
					))
				)
					return;
				const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
				const lease = await acquireProjectLease(ctx.cwd, config.online.leaseMs);
				if (!lease) {
					ctx.ui.notify("Another session is updating the catalog.", "warning");
					return;
				}
				try {
					await lease.assertOwned();
					await activateCatalogVersion(ctx.cwd, version);
					ctx.ui.notify(`Catalog v${version} is now active.`, "info");
				} catch (error) {
					ctx.ui.notify(
						`Catalog rollback failed: ${error instanceof Error ? error.message : String(error)}`,
						"error",
					);
				} finally {
					await lease.release();
				}
				return;
			}

			if (subcommand === "providers") {
				await ctx.waitForIdle();
				const providerStatus = await providers.refresh(
					pi,
					ctx.cwd,
					ctx.isProjectTrusted(),
				);
				ctx.ui.notify(formatMcpProviderStatus(providerStatus), "info");
				return;
			}

			if (subcommand === "enable") {
				if (!ctx.isProjectTrusted()) {
					ctx.ui.notify(
						"Trust this project before enabling meta-tools.",
						"error",
					);
					return;
				}
				const requestedMode = subcommandArgs || "safe";
				if (!["safe", "safe_auto", "force"].includes(requestedMode)) {
					ctx.ui.notify("Usage: /meta-tool enable [safe|force]", "warning");
					return;
				}
				const mode: DispatchMode =
					requestedMode === "force" ? "force" : "safe_auto";
				if (mode === "force") {
					if (!ctx.hasUI) {
						ctx.ui.notify(
							"Force mode requires an interactive Pi session.",
							"error",
						);
						return;
					}
					const approved = await ctx.ui.confirm(
						"Enable meta-tool force mode?",
						"Force mode uses the FORCE runtime policy only for already-admitted workflows. Every run still requires interactive plan approval.",
					);
					if (!approved) return;
				}
				state = await saveState(ctx.cwd, true, mode);
				const config = await loadConfig(ctx.cwd, PACKAGE_ROOT);
				const catalog = await effectiveCatalog(ctx.cwd, config);
				activateMetaTools(
					pi,
					shouldExposeMetaTools({
						enabled: true,
						trusted: ctx.isProjectTrusted(),
						activeCatalogSize: catalog.meta_tools.length,
						minActiveCatalogSize: config.routing.minActiveCatalogSize,
					}),
				);
				ctx.ui.setStatus(
					"meta-tool",
					`meta-tool: ${state.mode} | runs ${state.metrics.successes}/${state.metrics.attempts}`,
				);
				ctx.ui.notify(`Meta-tool enabled in ${mode} mode.`, "info");
				return;
			}

			if (subcommand === "disable") {
				state = await saveState(ctx.cwd, false, state.mode);
				activateMetaTools(pi, false);
				ctx.ui.setStatus("meta-tool", undefined);
				ctx.ui.notify("Meta-tool disabled.", "info");
				return;
			}

			if (subcommand === "inspect") {
				const catalog = await loadDiscoveredCatalog(ctx.cwd);
				const activeCatalog = await loadProjectCatalog({ cwd: ctx.cwd });
				const report = await loadReport(ctx.cwd);
				const activeNames = new Set(
					activeCatalog.meta_tools.map((spec) => spec.name),
				);
				const rejections = admissionRejections(report);
				const specs = subcommandArgs
					? catalog.meta_tools.filter((spec) => spec.name === subcommandArgs)
					: catalog.meta_tools;
				const lines = specs.length
					? specs.flatMap((spec, index) => [
							...(index > 0 ? [""] : []),
							...formatSpec(
								spec,
								activeNames.has(spec.name),
								rejections.get(spec.name),
							),
						])
					: [
							catalog.meta_tools.length === 0
								? "No meta-tools. Run /meta-tool extract."
								: `Unknown meta-tool: ${subcommandArgs}`,
						];
				ctx.ui.setWidget("meta-tool-inspect", lines, {
					placement: "aboveEditor",
				});
				ctx.ui.notify(
					`Meta-tool discovered catalog: ${getPluginPaths(ctx.cwd).discoveredCatalogFile}; Pi executable catalog: ${getPluginPaths(ctx.cwd).catalogFile}`,
					"info",
				);
				return;
			}

			if (subcommand === "config") {
				const path = await ensureConfig(ctx.cwd);
				ctx.ui.notify(`Meta-tool config: ${path}`, "info");
				return;
			}

			if (subcommand !== "status") {
				ctx.ui.notify(
					"Usage: /meta-tool extract [trace.json|jsonl]|online [status|run]|search <task>|promote <name>|health [status|reset <name>]|catalog [list|rollback <version>]|providers|enable [safe|force]|disable|status|inspect [name]|config",
					"warning",
				);
				return;
			}

			state = await loadState(ctx.cwd);
			const onlineStatus = await loadOnlineExtractionStatus(ctx.cwd);
			const manifest = await loadCatalogManifest(ctx.cwd);
			const health = await loadCatalogHealth(ctx.cwd);
			const degraded = Object.values(health.records).filter(
				(record) => record.state === "degraded",
			).length;
			ctx.ui.notify(
				`${formatStatus(
					ctx.cwd,
					state,
					await loadProjectCatalog({ cwd: ctx.cwd }),
					await loadDiscoveredCatalog(ctx.cwd),
					await loadReport(ctx.cwd),
				)}\ncatalog version: v${manifest?.activeVersion ?? 0}\nonline extraction: ${onlineStatus?.state ?? "not-run"}${onlineStatus ? ` (${onlineStatus.reason})` : ""}\ndegraded workflows: ${degraded}\nregistry execution: ${programmaticCallTool(pi) ? "ExtensionAPI.callTool (all registered tools)" : "compatibility mode (seven built-ins + connected MCP providers)"}\n${formatMcpProviderStatus(providers.status())}`,
				"info",
			);
		},
	});

	// Shared by Static and Online, including native Pi workers that load only
	// this extension through the existing worker propagation wrapper.
	registerReadBatch(pi);
}
