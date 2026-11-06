export const META_TOOL_RUN_NAME = "meta_tool_run";
export const META_TOOL_SEARCH_NAME = "meta_tool_search";
export const META_TOOL_COMMAND_NAME = "meta-tool";

export const LEGACY_SUPPORTED_TOOL_NAMES = [
	"read",
	"bash",
	"edit",
	"write",
	"grep",
	"find",
	"ls",
] as const;

export type LegacySupportedToolName =
	(typeof LEGACY_SUPPORTED_TOOL_NAMES)[number];
export type DispatchMode = "safe_auto" | "force";

export interface TraceCall {
	name: string;
	arguments: Record<string, unknown>;
	result: unknown;
	call_id: string;
	turn_id: number;
	metadata: Record<string, unknown>;
}

export interface WorkflowTrace {
	trace_id: string;
	success: boolean;
	calls: TraceCall[];
	metadata: Record<string, unknown>;
}

export interface TraceExport {
	version: 1;
	sessionId: string;
	cwd: string;
	createdAt: string;
	traces: WorkflowTrace[];
}

export interface StepSpec {
	id: string;
	tool: string;
	arguments: Record<string, unknown>;
}

export interface MetaToolSpec {
	name: string;
	description: string;
	input_schema: Record<string, unknown>;
	steps: StepSpec[];
	output: unknown;
	metadata: Record<string, unknown>;
}

export interface MetaToolCatalog {
	meta_tools: MetaToolSpec[];
}

export interface PluginPaths {
	stateDir: string;
	stateFile: string;
	configFile: string;
	artifactsDir: string;
	discoveredCatalogFile: string;
	catalogFile: string;
	reportFile: string;
	onlineDir: string;
	journalDir: string;
	checkpointFile: string;
	onlineStatusFile: string;
	lockFile: string;
	stagingDir: string;
	onlineRunsDir: string;
	catalogVersionsDir: string;
	catalogManifestFile: string;
	catalogHealthFile: string;
	catalogHealthLockFile: string;
}

export interface OnlineConfig {
	enabled: boolean;
	mode: "shadow_then_active" | "collect_only";
	traceEligibility: "strict" | "recovered_segment";
	maxRecoverableErrorsPerTask: number;
	minRecoveredSegmentCalls: number;
	debounceMs: number;
	minNewSuccessfulTraces: number;
	maxWaitMs: number;
	maxTraces: number;
	maxTraceAgeDays: number;
	maxCallsPerExtraction: number;
	maxBytesPerExtraction: number;
	leaseMs: number;
	persistOnEachToolResult: boolean;
	/** Flush buffered journal records after this many results when batching. */
	journalFlushInterval: number;
	extractOnAgentSettled: boolean;
	/** Minimum primitive calls before sealing at a completed tool-turn boundary. */
	segmentCalls: number;
}

export interface RoutingConfig {
	enabled: boolean;
	topK: number;
	maxPromptCharacters: number;
	minScore: number;
	minActiveCatalogSize: number;
	refreshOnEveryUserTurn: boolean;
	includeUserCatalog: boolean;
}

export interface CatalogConfig {
	versioned: boolean;
	preservePreviousVersions: number;
	userCatalogDir: string;
}

export interface PluginState {
	version: 1;
	enabled: boolean;
	mode: DispatchMode;
	updatedAt: string;
	metrics: {
		attempts: number;
		successes: number;
		failures: number;
		lastRunAt: string | null;
		lastMetaTool: string | null;
		lastError: string | null;
	};
}

export interface PluginConfig {
	metaToolRoot: string;
	bridgePath: string;
	pythonCommand: string;
	timeoutMs: number;
	normalization: Record<string, unknown>;
	optimization: Record<string, unknown>;
	extraction: Record<string, unknown>;
	compiler: Record<string, unknown>;
	admission: Record<string, unknown>;
	online: OnlineConfig;
	routing: RoutingConfig;
	catalog: CatalogConfig;
}

export interface ProjectProfile {
	cwd: string;
	tags: string[];
	providers: string[];
	detectedAt: string;
}

export interface MetaToolSearchMatch {
	name: string;
	catalogVersion: number;
	catalogScope: "project" | "user";
	fingerprint: string;
	score: number;
	matchReason: string[];
	description: string;
	requiredTools: string[];
	inputSchema: Record<string, unknown>;
	executionGuard: string;
	projectTags: string[];
	providers: string[];
}

export interface BridgeRunOptions {
	pythonCommand: string;
	bridgePath: string;
	metaToolRoot: string;
	timeoutMs: number;
	signal: AbortSignal | undefined;
	payload: Record<string, unknown>;
	onProgress?: (message: string) => void;
	onToolCall?: (
		id: string,
		name: string,
		input: Record<string, unknown>,
	) => Promise<string>;
}
