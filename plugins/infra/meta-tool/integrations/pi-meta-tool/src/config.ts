import { existsSync, readFileSync, statSync } from "node:fs";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type {
	DispatchMode,
	MetaToolCatalog,
	MetaToolSpec,
	PluginConfig,
	PluginPaths,
	PluginState,
} from "./types.ts";

const DEFAULT_NORMALIZATION: Record<string, unknown> = {
	default_semantic_type: "mutator",
	domain_commutativity: false,
	infer_domain_from_name: false,
	include_annotators: false,
	state_context_fields: [],
	// The online edition has one public extraction semantic: structural
	// generalization. Exact matching remains available only inside the Python
	// core for benchmarks and compatibility tests.
	argument_matching: "structural",
	structural_fallback: false,
	structural_fallback_max_calls: 5000,
	regex_substitutions: [],
	tool_rules: [
		{
			pattern: "^(read|grep|find|ls)$",
			semantic_type: "accessor",
			domain: "workspace_read",
		},
		{
			pattern: "^(edit|write)$",
			semantic_type: "mutator",
			domain: "workspace",
		},
		{ pattern: "^bash$", semantic_type: "mutator", domain: "shell" },
	],
};

const PROJECT_CONFIG_DIR = ".pi";

const DEFAULT_EXTRACTION: Record<string, unknown> = {
	threshold: 3,
	min_steps: 2,
	majority_fraction: 0.5,
	min_sequence_purity: 0.8,
	min_llm_turn_coverage: 0,
	min_llm_calls_saved_per_occurrence: 0,
	require_internal_data_flow: true,
	min_varying_data_flows: 1,
	min_runtime_local_data_flows: 0,
	max_data_flow_bridge_steps: 2,
	max_meta_tools: 20,
	enable_structural_sequence_fallback: true,
	max_structural_sequence_start_pairs: 128,
};

const DEFAULT_OPTIMIZATION: Record<string, unknown> = {
	enabled: false,
	max_iterations: 10,
	sample_size: 64,
	seed: 0,
	max_node_collapse_fraction: 0.75,
};

const DEFAULT_ADMISSION_POLICY: Record<string, unknown> = {
	mode: "safe_auto",
	require_reviewed: true,
	require_side_effect_free: false,
	require_no_compiler_warnings: true,
	require_internal_data_flow: true,
	min_varying_data_flows: 1,
	allow_stable_runtime_data_flows: false,
	min_sequence_purity: 1.0,
	min_branch_dominance: 1.0,
	min_llm_turn_coverage: 1.0,
	min_llm_calls_saved_per_occurrence: 1.0,
	fallback_on_execution_error: true,
	expose_to_model: false,
	allow_caller_bound_read_batches: true,
};

const DEFAULT_ADMISSION: Record<string, unknown> = {
	read_only_tools: ["read", "grep", "find", "ls"],
	min_distinct_sessions: 2,
	allow_noninteractive_guarded_execution: false,
	allow_runtime_derived_mutation_tools: [],
	allow_confirmed_runtime_derived_mutations: true,
	deny_runtime_derived_mutation_tools: ["bash", "edit", "write"],
	policy: DEFAULT_ADMISSION_POLICY,
};

const DEFAULT_ONLINE = {
	enabled: true,
	mode: "shadow_then_active" as const,
	traceEligibility: "recovered_segment" as const,
	maxRecoverableErrorsPerTask: 1,
	minRecoveredSegmentCalls: 2,
	debounceMs: 60_000,
	minNewSuccessfulTraces: 10,
	maxWaitMs: 300_000,
	maxTraces: 500,
	maxTraceAgeDays: 7,
	maxCallsPerExtraction: 5_000,
	maxBytesPerExtraction: 32 * 1024 * 1024,
	leaseMs: 180_000,
	// Journal writes are batched in the online hot path. A bounded interval
	// preserves crash recovery while avoiding one fs append per primitive call.
	persistOnEachToolResult: false,
	journalFlushInterval: 32,
	extractOnAgentSettled: true,
	// Checkpoint only at completed tool turns; a segment is not a completed audit.
	segmentCalls: 32,
};

const DEFAULT_ROUTING = {
	enabled: true,
	topK: 5,
	maxPromptCharacters: 6_000,
	minScore: 0.18,
	minActiveCatalogSize: 1,
	refreshOnEveryUserTurn: true,
	includeUserCatalog: false,
};

interface ConfigCacheEntry {
	signature: string;
	config: PluginConfig;
}

// Config is read by every routing/search/run operation. Keep the parsed value
// in-process while checking the file signature so edits still take effect
// immediately and no correctness-sensitive TTL is introduced.
const CONFIG_CACHE = new Map<string, ConfigCacheEntry>();

async function fileSignature(path: string): Promise<string> {
	try {
		const info = await stat(path);
		return `${info.ino}:${info.mtimeMs}:${info.size}`;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return "missing";
		throw error;
	}
}

export function clearConfigCache(): void {
	CONFIG_CACHE.clear();
}

interface CatalogCacheEntry {
	signature: string;
	catalog: MetaToolCatalog;
}

const CATALOG_CACHE = new Map<string, CatalogCacheEntry>();

async function catalogFileSignature(path: string): Promise<string> {
	try {
		const info = await stat(path);
		return `${info.ino}:${info.mtimeMs}:${info.size}`;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return "missing";
		throw error;
	}
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
	return typeof value === "string" && value.trim() ? value : undefined;
}

function expandHome(value: string): string {
	if (value === "~") return homedir();
	if (value.startsWith("~/")) return join(homedir(), value.slice(2));
	return value;
}

function finiteNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value)
		? value
		: undefined;
}

async function readObject(path: string): Promise<Record<string, unknown>> {
	try {
		const parsed: unknown = JSON.parse(await readFile(path, "utf8"));
		return isRecord(parsed) ? parsed : {};
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
		throw error;
	}
}

function emptyMetrics(): PluginState["metrics"] {
	return {
		attempts: 0,
		successes: 0,
		failures: 0,
		lastRunAt: null,
		lastMetaTool: null,
		lastError: null,
	};
}

function normalizeCounter(value: unknown): number {
	return Math.max(0, Math.trunc(finiteNumber(value) ?? 0));
}

function normalizeSpec(value: unknown): MetaToolSpec | undefined {
	if (!isRecord(value) || !optionalString(value.name)) return undefined;
	const steps = Array.isArray(value.steps)
		? value.steps.flatMap((step) => {
				if (
					!isRecord(step) ||
					!optionalString(step.id) ||
					!optionalString(step.tool)
				)
					return [];
				return [
					{
						id: step.id as string,
						tool: step.tool as string,
						arguments: isRecord(step.arguments) ? step.arguments : {},
					},
				];
			})
		: [];
	if (steps.length < 2) return undefined;
	return {
		name: value.name as string,
		description: optionalString(value.description) ?? (value.name as string),
		input_schema: isRecord(value.input_schema) ? value.input_schema : {},
		steps,
		output: value.output,
		metadata: isRecord(value.metadata) ? value.metadata : {},
	};
}

const LAYOUT_CACHE = new Map<string, { signature: string; privateRuntime: boolean }>();

export function getPluginPaths(cwd: string): PluginPaths {
	const configDir = join(cwd, PROJECT_CONFIG_DIR, "meta-tool");
	const configFile = join(configDir, "config.json");
	let signature = "missing";
	try {
		const info = statSync(configFile);
		signature = `${info.ino}:${info.mtimeMs}:${info.size}`;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	let layout = LAYOUT_CACHE.get(configFile);
	if (layout?.signature !== signature) {
		const raw = signature === "missing" ? {} : JSON.parse(readFileSync(configFile, "utf8"));
		if (raw.runtimeLayout !== undefined && !["legacy", "private"].includes(raw.runtimeLayout))
			throw new Error("runtimeLayout must be legacy or private");
		layout = { signature, privateRuntime: raw.runtimeLayout === "private" };
		LAYOUT_CACHE.set(configFile, layout);
	}
	// Goal-Plus explicitly excludes .tmp from product staging and rollback.
	// Keep configuration as immutable project input; never widen edit surfaces.
	const privateRuntime = layout.privateRuntime ||
		existsSync(join(cwd, ".git", "goal-plus", "state.json"));
	const stateDir = privateRuntime ? join(cwd, ".tmp", "meta-tool") : configDir;
	const artifactsDir = join(stateDir, "artifacts");
	const onlineDir = join(stateDir, "online");
	const catalogDir = join(stateDir, "catalog");
	return {
		stateDir,
		stateFile: join(stateDir, "state.json"),
		configFile,
		artifactsDir,
		discoveredCatalogFile: join(artifactsDir, "meta_tools.json"),
		catalogFile: join(artifactsDir, "active_meta_tools.json"),
		reportFile: join(artifactsDir, "report.json"),
		onlineDir,
		journalDir: join(onlineDir, "journal"),
		checkpointFile: join(onlineDir, "checkpoint.json"),
		onlineStatusFile: join(onlineDir, "status.json"),
		lockFile: join(onlineDir, "extract.lock"),
		stagingDir: join(onlineDir, "staging"),
		onlineRunsDir: join(onlineDir, "runs"),
		catalogVersionsDir: join(catalogDir, "versions"),
		catalogManifestFile: join(catalogDir, "manifest.json"),
		catalogHealthFile: join(catalogDir, "health.json"),
		catalogHealthLockFile: join(catalogDir, "health.lock"),
	};
}

export async function loadConfig(
	cwd: string,
	packageRoot: string,
): Promise<PluginConfig> {
	const configPath = getPluginPaths(cwd).configFile;
	const signature = await fileSignature(configPath);
	const cacheKey = `${resolve(packageRoot)}\n${configPath}\n${
		process.env.META_TOOL_ROOT ?? ""
	}\n${process.env.META_TOOL_PYTHON ?? ""}`;
	const cached = CONFIG_CACHE.get(cacheKey);
	if (cached?.signature === signature) return cached.config;
	const raw = await readObject(configPath);
	const inferredRoot = resolve(packageRoot, "../..");
	const metaToolRoot =
		optionalString(process.env.META_TOOL_ROOT) ??
		optionalString(raw.metaToolRoot) ??
		inferredRoot;
	const inferredPython = join(metaToolRoot, ".venv", "bin", "python");
	const onlineRaw = isRecord(raw.online) ? raw.online : {};
	const routingRaw = isRecord(raw.routing) ? raw.routing : {};
	const catalogRaw = isRecord(raw.catalog) ? raw.catalog : {};
	const config: PluginConfig = {
		metaToolRoot,
		bridgePath:
			optionalString(raw.bridgePath) ??
			join(packageRoot, "python", "bridge.py"),
		pythonCommand:
			optionalString(process.env.META_TOOL_PYTHON) ??
			optionalString(raw.pythonCommand) ??
			(existsSync(inferredPython) ? inferredPython : "python3"),
		timeoutMs: Math.max(
			10_000,
			Math.trunc(finiteNumber(raw.timeoutMs) ?? 120_000),
		),
		// Force the public Pi extraction path to generalized structural matching.
		// A stale project config from the offline plugin cannot silently restore
		// exact-only behavior.
		normalization: {
			...DEFAULT_NORMALIZATION,
			...(isRecord(raw.normalization) ? raw.normalization : {}),
			argument_matching: "structural",
			structural_fallback: false,
		},
		optimization: isRecord(raw.optimization)
			? { ...DEFAULT_OPTIMIZATION, ...raw.optimization }
			: DEFAULT_OPTIMIZATION,
		extraction: {
			...DEFAULT_EXTRACTION,
			...(isRecord(raw.extraction) ? raw.extraction : {}),
			enable_structural_sequence_fallback: true,
		},
		compiler: isRecord(raw.compiler) ? raw.compiler : {},
		admission: isRecord(raw.admission)
			? {
					...DEFAULT_ADMISSION,
					...raw.admission,
					policy: {
						...DEFAULT_ADMISSION_POLICY,
						...(isRecord(raw.admission.policy) ? raw.admission.policy : {}),
					},
				}
			: DEFAULT_ADMISSION,
		online: {
			...DEFAULT_ONLINE,
			...onlineRaw,
			enabled: onlineRaw.enabled !== false,
			mode:
				onlineRaw.mode === "collect_only"
					? "collect_only"
					: "shadow_then_active",
			traceEligibility:
				onlineRaw.traceEligibility === "strict"
					? "strict"
					: "recovered_segment",
			maxRecoverableErrorsPerTask: Math.max(
				0,
				Math.trunc(
					finiteNumber(onlineRaw.maxRecoverableErrorsPerTask) ??
						DEFAULT_ONLINE.maxRecoverableErrorsPerTask,
				),
			),
			minRecoveredSegmentCalls: Math.max(
				1,
				Math.trunc(
					finiteNumber(onlineRaw.minRecoveredSegmentCalls) ??
						DEFAULT_ONLINE.minRecoveredSegmentCalls,
				),
			),
			debounceMs: Math.max(
				0,
				Math.trunc(
					finiteNumber(onlineRaw.debounceMs) ?? DEFAULT_ONLINE.debounceMs,
				),
			),
			minNewSuccessfulTraces: Math.max(
				1,
				Math.trunc(
					finiteNumber(onlineRaw.minNewSuccessfulTraces) ??
						DEFAULT_ONLINE.minNewSuccessfulTraces,
				),
			),
			maxWaitMs: Math.max(
				1_000,
				Math.trunc(
					finiteNumber(onlineRaw.maxWaitMs) ?? DEFAULT_ONLINE.maxWaitMs,
				),
			),
			maxTraces: Math.max(
				2,
				Math.trunc(
					finiteNumber(onlineRaw.maxTraces) ?? DEFAULT_ONLINE.maxTraces,
				),
			),
			maxTraceAgeDays: Math.max(
				1,
				Math.trunc(
					finiteNumber(onlineRaw.maxTraceAgeDays) ??
						DEFAULT_ONLINE.maxTraceAgeDays,
				),
			),
			maxCallsPerExtraction: Math.max(
				2,
				Math.trunc(
					finiteNumber(onlineRaw.maxCallsPerExtraction) ??
						DEFAULT_ONLINE.maxCallsPerExtraction,
				),
			),
			maxBytesPerExtraction: Math.max(
				1024 * 1024,
				Math.trunc(
					finiteNumber(onlineRaw.maxBytesPerExtraction) ??
						DEFAULT_ONLINE.maxBytesPerExtraction,
				),
			),
			leaseMs: Math.max(
				10_000,
				Math.trunc(finiteNumber(onlineRaw.leaseMs) ?? DEFAULT_ONLINE.leaseMs),
			),
			persistOnEachToolResult:
				onlineRaw.persistOnEachToolResult === true ||
				(DEFAULT_ONLINE.persistOnEachToolResult &&
					onlineRaw.persistOnEachToolResult !== false),
			journalFlushInterval: Math.max(
				1,
				Math.trunc(
					finiteNumber(onlineRaw.journalFlushInterval) ??
						DEFAULT_ONLINE.journalFlushInterval,
				),
			),
			extractOnAgentSettled: onlineRaw.extractOnAgentSettled !== false,
			segmentCalls: Math.max(2, Math.trunc(
				finiteNumber(onlineRaw.segmentCalls) ?? DEFAULT_ONLINE.segmentCalls,
			)),
		},
		routing: {
			...DEFAULT_ROUTING,
			...routingRaw,
			enabled: routingRaw.enabled !== false,
			topK: Math.max(
				1,
				Math.min(
					20,
					Math.trunc(finiteNumber(routingRaw.topK) ?? DEFAULT_ROUTING.topK),
				),
			),
			maxPromptCharacters: Math.max(
				500,
				Math.trunc(
					finiteNumber(routingRaw.maxPromptCharacters) ??
						DEFAULT_ROUTING.maxPromptCharacters,
				),
			),
			minScore: Math.max(
				0,
				Math.min(
					1,
					finiteNumber(routingRaw.minScore) ?? DEFAULT_ROUTING.minScore,
				),
			),
			minActiveCatalogSize: Math.max(
				1,
				Math.trunc(
					finiteNumber(routingRaw.minActiveCatalogSize) ??
						DEFAULT_ROUTING.minActiveCatalogSize,
				),
			),
			// Task A and task B must never inherit one another's injected subset.
			refreshOnEveryUserTurn: true,
			includeUserCatalog: routingRaw.includeUserCatalog !== false,
		},
		catalog: {
			// Immutable versions are a crash/concurrency invariant of the online
			// edition, not an optional presentation preference.
			versioned: true,
			preservePreviousVersions: Math.max(
				1,
				Math.trunc(finiteNumber(catalogRaw.preservePreviousVersions) ?? 20),
			),
			userCatalogDir: expandHome(
				optionalString(catalogRaw.userCatalogDir) ??
					join(homedir(), ".pi", "meta-tool", "catalogs"),
			),
		},
	};
	CONFIG_CACHE.set(cacheKey, { signature, config });
	return config;
}

export async function loadState(cwd: string): Promise<PluginState> {
	const raw = await readObject(getPluginPaths(cwd).stateFile);
	const metrics = isRecord(raw.metrics) ? raw.metrics : {};
	return {
		version: 1,
		enabled: raw.enabled !== false,
		mode: raw.mode === "force" ? "force" : "safe_auto",
		updatedAt: optionalString(raw.updatedAt) ?? new Date(0).toISOString(),
		metrics: {
			attempts: normalizeCounter(metrics.attempts),
			successes: normalizeCounter(metrics.successes),
			failures: normalizeCounter(metrics.failures),
			lastRunAt: optionalString(metrics.lastRunAt) ?? null,
			lastMetaTool: optionalString(metrics.lastMetaTool) ?? null,
			lastError: optionalString(metrics.lastError) ?? null,
		},
	};
}

async function writeState(cwd: string, state: PluginState): Promise<void> {
	const paths = getPluginPaths(cwd);
	await mkdir(paths.stateDir, { recursive: true });
	await writeFile(
		paths.stateFile,
		`${JSON.stringify(state, null, 2)}\n`,
		"utf8",
	);
}

export async function saveState(
	cwd: string,
	enabled: boolean,
	mode: DispatchMode,
): Promise<PluginState> {
	const current = await loadState(cwd);
	const state: PluginState = {
		...current,
		enabled,
		mode,
		updatedAt: new Date().toISOString(),
	};
	await writeState(cwd, state);
	return state;
}

export async function recordRun(
	cwd: string,
	metaTool: string,
	error?: string,
): Promise<PluginState> {
	const current = await loadState(cwd);
	const succeeded = error === undefined;
	const state: PluginState = {
		...current,
		updatedAt: new Date().toISOString(),
		metrics: {
			attempts: current.metrics.attempts + 1,
			successes: current.metrics.successes + (succeeded ? 1 : 0),
			failures: current.metrics.failures + (succeeded ? 0 : 1),
			lastRunAt: new Date().toISOString(),
			lastMetaTool: metaTool,
			lastError: error ?? null,
		},
	};
	await writeState(cwd, state);
	return state;
}

export async function loadCatalog(cwd: string): Promise<MetaToolCatalog> {
	return loadCatalogFile(getPluginPaths(cwd).catalogFile);
}

export async function loadCatalogFile(path: string): Promise<MetaToolCatalog> {
	const signature = await catalogFileSignature(path);
	const cached = CATALOG_CACHE.get(path);
	if (cached?.signature === signature) return cached.catalog;
	const raw = await readObject(path);
	const values = Array.isArray(raw.meta_tools) ? raw.meta_tools : [];
	const catalog = {
		meta_tools: values.flatMap((value) => normalizeSpec(value) ?? []),
	};
	CATALOG_CACHE.set(path, { signature, catalog });
	return catalog;
}

/** Load every candidate emitted by core discovery, before Pi admission. */
export async function loadDiscoveredCatalog(
	cwd: string,
): Promise<MetaToolCatalog> {
	const raw = await readObject(getPluginPaths(cwd).discoveredCatalogFile);
	const values = Array.isArray(raw.meta_tools) ? raw.meta_tools : [];
	return { meta_tools: values.flatMap((value) => normalizeSpec(value) ?? []) };
}

export async function saveCatalog(
	cwd: string,
	catalog: MetaToolCatalog,
): Promise<void> {
	const paths = getPluginPaths(cwd);
	await mkdir(paths.artifactsDir, { recursive: true });
	await writeFile(
		paths.catalogFile,
		`${JSON.stringify(catalog, null, 2)}\n`,
		"utf8",
	);
}

export async function loadReport(
	cwd: string,
): Promise<Record<string, unknown>> {
	return readObject(getPluginPaths(cwd).reportFile);
}

export function defaultConfigDocument(): Record<string, unknown> {
	return {
		timeoutMs: 120_000,
		normalization: DEFAULT_NORMALIZATION,
		optimization: DEFAULT_OPTIMIZATION,
		extraction: DEFAULT_EXTRACTION,
		compiler: {
			parameterize_all_scalars: false,
			unstable_runtime_argument_pattern:
				"(?i)(^|\\.)(ref|element_id|node_id|backend_node_id|target_id)$",
		},
		admission: DEFAULT_ADMISSION,
		online: DEFAULT_ONLINE,
		routing: DEFAULT_ROUTING,
		catalog: {
			versioned: true,
			preservePreviousVersions: 20,
			userCatalogDir: "~/.pi/meta-tool/catalogs",
		},
	};
}

export async function ensureConfig(cwd: string): Promise<string> {
	const path = getPluginPaths(cwd).configFile;
	if (!existsSync(path)) {
		await mkdir(dirname(path), { recursive: true });
		await writeFile(
			path,
			`${JSON.stringify(defaultConfigDocument(), null, 2)}\n`,
			"utf8",
		);
	}
	return path;
}

export function freshState(): PluginState {
	return {
		version: 1,
		enabled: true,
		mode: "safe_auto",
		updatedAt: new Date(0).toISOString(),
		metrics: emptyMetrics(),
	};
}
