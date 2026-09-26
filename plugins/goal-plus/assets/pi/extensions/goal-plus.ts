import { LooseObject, RuntimeToolSchemas, RuntimeToolDescriptions } from "./lib/goal-plus-schemas.ts";
import { pythonExecutable, runtimeRoot, type CommandRuntimeContext, isRecord, sourceRoot, parseJsonObject, runJsonCli, runHostCommandCli, closeMcpConnections } from "./lib/goal-plus-transport.ts";
import { THINKTHREAD_GOAL_PLUS_PROTOCOL, WorkerSteeringReceiver, dispatchMessageId, dispatchMetadata, requestWorkerTool } from "../../thinkthread-session-rpc.ts";
import { toolOwner, type ToolOwner } from "./lib/goal-plus-tool-identity.ts";
import {
	appendInjectionObservation,
	registerSessionDiagnostics,
	sessionDiagnosticsEnabled,
} from "./lib/goal-plus-session-diagnostics.ts";
import { SessionArchive, sendDiagnosticMessages } from "./lib/goal-plus-session-archive.ts";
import { parseArgs, type ExtensionAPI, type ExtensionContext, type ToolCallEvent, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Box, Text } from "@earendil-works/pi-tui";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { type TSchema, Type } from "typebox";
import {
	buildPiCompletionPresentation,
	buildPiProgressPresentation,
	type GoalPlusCandidateCompletion,
	type GoalPlusCandidateProgress,
	type GoalPlusCompletionView,
	type GoalPlusProgressState,
	type GoalPlusProgressView,
	type GoalPlusUsageSummary,
} from "./lib/goal-plus-progress.ts";

const role = process.env.GOAL_PLUS_PI_ROLE || "main";
const CONTROLLER_ONLY_CLOSEOUT_TOOLS = new Set([
	"goal_plus_search_select",
	"goal_plus_search_promote",
	"goal_plus_search_apply_promotion",
	"goal_plus_record_search_result",
]);
const controllerOwnsCloseout =
	role === "main" && process.env.GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT === "1";
const sharedMcpTools = new Map<string, { description: string; inputSchema: TSchema }>();
const diagnosticToolContracts = new Map<string, { name: string; description: string; parameters: unknown }>();
const sessionArchive = new SessionArchive();
const extensionPath = fileURLToPath(import.meta.url);
const isThinkThreadProfile = Boolean(process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY);
const isThinkThreadWorker = role === "worker" && isThinkThreadProfile;
let thinkThreadSupplementalEnabled = false;



const modeArgIndex = process.argv.indexOf("--mode");
const isPrintLikeInvocation =
	process.argv.includes("-p") ||
	process.argv.includes("--print") ||
	process.argv.includes("--mode=json") ||
	(modeArgIndex >= 0 && process.argv[modeArgIndex + 1] === "json");
const STATE_ENTRY_TYPE = "goal-plus-native-state";
const WORKER_CONTEXT_ENTRY_TYPE = "goal-plus-worker-context";
const GOAL_PLUS_STATS_ENTRY_TYPE = "goal-plus-stats";
const GOAL_PLUS_RUNTIME_ENTRY_TYPE = "goal-plus-runtime-start";
const GOAL_PLUS_COMMAND_CONTEXT_TYPE = "goal-plus-command-context";
const GOAL_PLUS_MUST_CONTINUE_TYPE = "goal-plus-must-continue";
const GOAL_PLUS_PROGRESS_WIDGET_KEY = "goal-plus-progress";
let workspaceRoot: string | undefined;
let sawContext = false;
let workerGeneration: { runId: string; candidateId: string; generation: number } | undefined;
let activeGoalPlusId = process.env.GOAL_PLUS_ID;
let cachedGoalStatus: GoalPlusStatusPayload | undefined;
let goalContextFullKey: string | undefined;
let mainGoalPlusToolNames: string[] = [];
let ensureMainGoalPlusTools: (() => Promise<void>) | undefined;

let continuationCount = 0;
// True once the full stop-audit policy block sits in this session's visible
// history. Compaction clears it so the next stop re-injects the full text
// instead of the compact pointer — same policy as goalContextFullKey.
let stopPolicyDelivered = false;
let workerRegistrationSent = false;
let workerSdkModulePromise: Promise<any> | undefined;
let workerSdkClientPromise: Promise<any> | undefined;
const workerAcknowledgedDispatches = new Set<string>();
let workerRpcTail: Promise<void> = Promise.resolve();
let workerActiveDispatchNonce: string | undefined;
let stopWorkerSteering: (() => void) | undefined;
let workerSteeringIdentity: { agentSessionId: string; dispatchNonce: string; messageId: string } | undefined;
let workerVerifierSubmitted = false;
let workerMissingAttemptContinuationSent = false;
let workerInnerGitBinding: ThinkThreadInnerGitBinding | undefined;
let workerInnerGitInitializedDispatch: string | undefined;
const stagedPublicationNotifications: Array<{ runId: string; notificationId: string }> = [];
const presentedPublicationNotifications = new Set<string>();
let activeGoalStartedAt: string | undefined;
let activeGoalStartEntryCount = 0;
let goalPlusSessionAuthorized = false;
let lastMainStopReason: string | undefined;
let mainTurnSignal: AbortSignal | undefined;
let suppressedContinuationAbort = false;
let mainStartPending = false;
let executionScopeId: string | undefined;


const AGENT_POSIX_CONTROL_PROTOCOL_VERSION = 2;
const AGENT_POSIX_CONTRACT_FINGERPRINT =
	"fcc80b665cd990f9d1e3681a9d384cb99994f2b739cd4fbddc97bdda01391131";
const THINKTHREAD_ANNOTATION_TOOL_NAMES = [
	"goal_plus_thinkthread_annotation_next",
	"goal_plus_thinkthread_artifact_view_list",
	"goal_plus_thinkthread_artifact_view_stat",
	"goal_plus_thinkthread_artifact_view_pread",
	"goal_plus_thinkthread_artifact_view_diff",
	"goal_plus_thinkthread_annotation_submit",
] as const;
const THINKTHREAD_ANNOTATION_TOOLS = new Set<string>(THINKTHREAD_ANNOTATION_TOOL_NAMES);

interface ThinkThreadInnerGitBinding {
	protocol: "goal-plus.inner-git.v1";
	adapter: "autoresearch-git";
	inner_agent_id: string;
	source_subdir: string;
	metric_name: string;
	initial_results_base64: string;
	initial_result_count: number;
}







































const MAIN_GATED_TOOLS = new Set([
	"bash",
	"edit",
	"write",
]);
const MAIN_READ_ONLY_RUNTIME_TOOLS = new Set([
	"goal_plus_get_tool_schema",
	"goal_plus_search_recovery_status",
	"goal_plus_status",
	"goal_plus_history",
	"goal_plus_monitor_snapshot",
	"goal_plus_list_models",
	"goal_plus_search_status",
	"goal_plus_search_list_history",
	"goal_plus_search_list_iterations",
	"goal_plus_search_get_agent_observability",
]);
const MAIN_SESSION_CLEANUP_TOOLS = new Set([
	"goal_plus_session_wait_any", "goal_plus_session_interrupt",
	"goal_plus_session_stop", "goal_plus_session_close",
]);

function requiresGoalPlusAuthorization(toolName: string, owner: ToolOwner): boolean {
	return owner === "goal-plus" && !MAIN_READ_ONLY_RUNTIME_TOOLS.has(toolName)
		&& !MAIN_SESSION_CLEANUP_TOOLS.has(toolName);
}

interface GoalPlusNativeState {
	activeGoalPlusId?: string;
	continuationCount?: number;
	stopPolicyDelivered?: boolean;
	startedAt?: string;
	startEntryCount?: number;
	stagedPublicationNotifications?: Array<{ runId: string; notificationId: string }>;
	status?: string;
	phase?: string;
	updatedAt?: string;
}

interface GoalPlusNextActionPayload {
	kind?: string;
	description?: string;
	required?: boolean;
	metadata?: Record<string, unknown>;
}

interface GoalPlusStatusPayload {
	goal_plus_id?: string;
	active_session?: { session_id?: string };
	raw_goal?: string;
	command_config?: Record<string, unknown>;
	goal_revision?: number;
	goal_revisions?: unknown[];
	policy?: Record<string, unknown>;
	final_check_policy?: Record<string, unknown>;
	final_checks?: unknown[];
	status?: string;
	control?: { state: "active" | "paused" | "closed"; reason?: string | null; version: number };
	reconciliation?: unknown;
	phase?: string;
	next_action?: GoalPlusNextActionPayload | null;
	triage?: unknown;
	spec_draft?: unknown;
	search_tasks?: unknown[];
	search_tasks_total?: number;
	current_search_run_id?: string | null;
	linked_search?: unknown;
	current_search?: unknown;
	source_path?: string | null;
	work_items?: unknown[];
}

interface GoalPlusGatePayload {
	decision?: string;
	reason?: string;
	continuation_prompt?: string;
	status?: string;
	phase?: string;
	goal_plus_id?: string;
	goal_revision?: number;
	control_version?: number;
	session_id?: string;
}

interface GoalPlusHostSessionDecisionPayload {
	goal_plus_id?: string;
	session_id?: string;
	record_status?: string;
	session_state?: string;
	decision?: "continue_now" | "continue_after_user" | "ask_user" | "wait_for_user" | "stop";
	reason?: string;
}

interface GoalPlusUsageTotals {
	assistantMessages: number;
	toolCalls: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	cost: number;
}

interface GoalPlusStatsEntry {
	goal_plus_id?: string;
	status?: string;
	startedAt?: string;
	endedAt: string;
	usage: GoalPlusUsageTotals;
	message: string;
	collapsedLineCount: number;
}

interface PiRuntimeStartEntry {
	schema_version: 1;
	agent: string;
	agent_harness: "pi";
	mode: string;
	project_trusted: boolean;
	active_tools: string[];
	active_builtin_tools: string[];
	active_non_builtin_tools: string[];
	pi_sandbox: "none" | "seatbelt" | "bubblewrap";
	pi_builtin_tool_approval: "none";
	filesystem_access: "launcher-process-user";
	network_access: "host-policy-unreported";
	cwd: string;
	captured_at: string;
}







function numberFrom(value: unknown): number {
	return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function optionalNumberFrom(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function commandContextFrom(ctx: ExtensionContext): CommandRuntimeContext {
	return { cwd: ctx.cwd, nativeSessionId: ctx.sessionManager.getSessionId() };
}

function piRuntimeAgent(): string {
	return role === "worker" ? "candidate" : role;
}

function buildPiRuntimeStartEntry(pi: ExtensionAPI, ctx: ExtensionContext): PiRuntimeStartEntry {
	const activeTools = [...new Set(pi.getActiveTools())].sort();
	const builtinToolNames = new Set(
		pi.getAllTools()
			.filter((tool) => tool.sourceInfo.source === "builtin")
			.map((tool) => tool.name),
	);
	const activeBuiltinTools = activeTools.filter((name) => builtinToolNames.has(name));
	const activeNonBuiltinTools = activeTools.filter((name) => !builtinToolNames.has(name));
	return {
		schema_version: 1,
		agent: piRuntimeAgent(),
		agent_harness: "pi",
		mode: ctx.mode,
		project_trusted: ctx.isProjectTrusted(),
		active_tools: activeTools,
		active_builtin_tools: activeBuiltinTools,
		active_non_builtin_tools: activeNonBuiltinTools,
		pi_sandbox: process.env.GOAL_PLUS_PI_SANDBOX === "seatbelt"
			? "seatbelt"
			: process.env.GOAL_PLUS_PI_SANDBOX === "bubblewrap" ? "bubblewrap" : "none",
		pi_builtin_tool_approval: "none",
		filesystem_access: "launcher-process-user",
		network_access: "host-policy-unreported",
		cwd: ctx.cwd,
		captured_at: new Date().toISOString(),
	};
}

function appendPiRuntimeStartEntry(pi: ExtensionAPI, ctx: ExtensionContext): PiRuntimeStartEntry {
	const entry = buildPiRuntimeStartEntry(pi, ctx);
	pi.appendEntry<PiRuntimeStartEntry>(GOAL_PLUS_RUNTIME_ENTRY_TYPE, entry);
	return entry;
}

function renderPiRuntimeStartEntry(entry: PiRuntimeStartEntry, expanded: boolean): string[] {
	const trust = entry.project_trusted ? "true" : "false";
	const lines = [
		`Pi start · agent=${entry.agent} · harness=${entry.agent_harness} · mode=${entry.mode} · project_trusted=${trust} · sandbox=${entry.pi_sandbox} · builtin_approval=${entry.pi_builtin_tool_approval}`,
		`tools=${entry.active_builtin_tools.length} builtin + ${entry.active_non_builtin_tools.length} non-builtin · filesystem=${entry.filesystem_access} · network=${entry.network_access}`,
	];
	if (expanded) {
		lines.push(
			`cwd=${entry.cwd}`,
			`builtin_tools=${entry.active_builtin_tools.join(",") || "none"}`,
			`non_builtin_tools=${entry.active_non_builtin_tools.join(",") || "none"}`,
			`captured_at=${entry.captured_at}`,
		);
	}
	return lines;
}











function toolParameters(name: string): TSchema {
    const shared = sharedMcpTools.get(name);
    if (shared) return shared.inputSchema;
    if (name.startsWith("pi_") || name.startsWith("goal_plus_thinkthread_")) {
        return RuntimeToolSchemas[name] ?? LooseObject;
    }
    throw new Error(`Goal Plus MCP did not advertise ${name}`);
}

function goalPlusIdFrom(value: unknown): string | undefined {
	if (!isRecord(value)) return undefined;
	const id = value.goal_plus_id;
	return typeof id === "string" && id.length > 0 ? id : undefined;
}

function statusFrom(value: unknown): GoalPlusStatusPayload | undefined {
	if (!isRecord(value)) return undefined;
	return value as GoalPlusStatusPayload;
}

function inactiveGoalStatusResult(): { content: Array<{ type: "text"; text: string }>; details: Record<string, unknown> } {
	const details = {
		status: "inactive",
		reason: "no_active_goal",
		message: "当前 Pi 会话不在 Goal Plus 任务中；Goal Plus 工具不可用。",
		details: { tool: "goal_plus_status", inactive: true },
	};
	return {
		content: [{ type: "text", text: JSON.stringify(details) }],
		details,
	};
}

function setMainGoalPlusToolAvailability(pi: ExtensionAPI, enabled: boolean): void {
	if (role !== "main" || mainGoalPlusToolNames.length === 0) return;
	const goalTools = new Set(mainGoalPlusToolNames);
	const active = pi.getActiveTools().filter((name) => !goalTools.has(name));
	if (enabled) active.push(...mainGoalPlusToolNames);
	pi.setActiveTools([...new Set(active)]);
}

function gateFrom(value: unknown): GoalPlusGatePayload | undefined {
	if (!isRecord(value)) return undefined;
	return value as GoalPlusGatePayload;
}

function hostSessionDecisionFrom(value: unknown): GoalPlusHostSessionDecisionPayload | undefined {
	if (!isRecord(value)) return undefined;
	return value as GoalPlusHostSessionDecisionPayload;
}



async function ensureExecutionScope(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	runId: string,
	nativeAgentConfig: PiNativeAgentConfig,
): Promise<string> {
	const nativeSessionId = ctx.sessionManager.getSessionId();
	if (!activeGoalPlusId || !nativeSessionId) {
		throw new Error("Execution scope requires the active Goal Plus native session");
	}
	const result = await runJsonCli(pi, commandContextFrom(ctx), "host_execution_scope_open", {
		agent_harness: "pi",
		goal_plus_id: activeGoalPlusId,
		native_session_id: nativeSessionId,
		owner_session_id: `${activeGoalPlusId}:${ctx.cwd}`,
		run_id: runId,
		native_agent_config: nativeAgentConfig,
	});
	if (!isRecord(result.details) || result.details.ok === false || typeof result.details.scope_id !== "string") {
		throw new Error(String((isRecord(result.details) && result.details.error) || "Pi execution scope could not be opened"));
	}
	executionScopeId = result.details.scope_id;
	return executionScopeId;
}

async function closeExecutionScope(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	reason: string,
): Promise<void> {
	if (!executionScopeId) return;
	const closingId = executionScopeId;
	const result = await runJsonCli(pi, ctx, "host_execution_scope_close", {
		scope_id: closingId,
		reason,
	});
	if (!isRecord(result.details) || result.details.ok === false || result.details.state !== "closed") {
		throw new Error(String((isRecord(result.details) && result.details.error) || `Execution scope ${closingId} cleanup did not complete`));
	}
	executionScopeId = undefined;
}

interface PublicationNotificationPoll {
	ok: boolean;
	content?: string;
	notificationIds: string[];
}

function publicationNotificationKey(runId: string, notificationId: string): string {
	return `${runId}:${notificationId}`;
}

async function publicationNotificationContext(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	runId: string,
	presentedInCurrentTurn = false,
): Promise<PublicationNotificationPoll> {
	const result = await runJsonCli(pi, ctx, "search_main_agent_notifications", { run_id: runId });
	if (!isRecord(result.details) || result.details.ok === false) {
		return { ok: false, notificationIds: [] };
	}
	const notifications = Array.isArray(result.details.notifications)
		? result.details.notifications.filter(isRecord)
		: [];
	const lines: string[] = [];
	const notificationIds: string[] = [];
	for (const notification of notifications) {
		const notificationId = stringFrom(notification.notification_id);
		const payload = isRecord(notification.payload) ? notification.payload : {};
		if (!notificationId || notification.state !== "pending") continue;
		notificationIds.push(notificationId);
		if (!stagedPublicationNotifications.some(
			(item) => item.runId === runId && item.notificationId === notificationId,
		)) {
			stagedPublicationNotifications.push({ runId, notificationId });
		}
		if (presentedInCurrentTurn) {
			presentedPublicationNotifications.add(publicationNotificationKey(runId, notificationId));
		}
		lines.push(
			stringFrom(payload.message) ?? "Goal Plus publication state changed.",
			`run_id: ${runId}`,
			`notification_id: ${notificationId}`,
			`publication_result: ${stringFrom(payload.publication_state) ?? "unknown"}`,
		);
		const pendingPath = stringFrom(payload.pending_artifact_path);
		if (pendingPath) lines.push(`pending_integration: ${pendingPath}`);
		const reportPath = stringFrom(payload.report_path);
		if (reportPath) lines.push(`report: ${reportPath}`);
	}
	const pendingIds = new Set(notificationIds);
	for (let index = stagedPublicationNotifications.length - 1; index >= 0; index -= 1) {
		const item = stagedPublicationNotifications[index];
		if (item.runId === runId && !pendingIds.has(item.notificationId)) {
			stagedPublicationNotifications.splice(index, 1);
		}
	}
	return {
		ok: true,
		content: lines.length > 0 ? lines.join("\n") : undefined,
		notificationIds,
	};
}

async function acknowledgeStagedPublicationNotifications(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	persist: boolean,
): Promise<boolean> {
	const presented = new Set(presentedPublicationNotifications);
	presentedPublicationNotifications.clear();
	let acknowledged = false;
	for (let index = stagedPublicationNotifications.length - 1; index >= 0; index -= 1) {
		const item = stagedPublicationNotifications[index];
		if (!presented.has(publicationNotificationKey(item.runId, item.notificationId))) continue;
		const result = await runJsonCli(pi, ctx, "search_ack_main_agent_notification", {
			run_id: item.runId,
			notification_id: item.notificationId,
		});
		if (isRecord(result.details) && result.details.ok !== false) {
			stagedPublicationNotifications.splice(index, 1);
			acknowledged = true;
		}
	}
	if (persist && (acknowledged || presented.size > 0)) persistGoalState(pi);
	return acknowledged;
}

async function workerAgentPosixClient(): Promise<any> {
	if (!workerSdkModulePromise) {
		const configured = process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY;
		if (!configured) {
			throw new Error("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY is required in the worker Profile");
		}
		workerSdkModulePromise = import(
			configured.startsWith("file:") ? configured : pathToFileURL(configured).href
		);
	}
	const sdk = await workerSdkModulePromise;
	if (!workerSdkClientPromise) {
		if (
			sdk.CONTROL_PROTOCOL_VERSION !== AGENT_POSIX_CONTROL_PROTOCOL_VERSION ||
			sdk.CONTRACT_FINGERPRINT !== AGENT_POSIX_CONTRACT_FINGERPRINT
		) {
			console.warn(
				`ThinkThread Agent POSIX SDK differs from the tested baseline: protocol=${String(sdk.CONTROL_PROTOCOL_VERSION)}, fingerprint=${String(sdk.CONTRACT_FINGERPRINT)}`,
			);
		}
		if (typeof sdk.AgentPosixClient?.fromEnv !== "function") {
			throw new Error("ThinkThread Agent POSIX SDK is missing AgentPosixClient.fromEnv");
		}
		workerSdkClientPromise = Promise.resolve(sdk.AgentPosixClient.fromEnv()).then((client) => {
			if (typeof client?.invoke !== "function") {
				throw new Error("ThinkThread Agent POSIX SDK client is missing invoke");
			}
			return client;
		});
	}
	return workerSdkClientPromise;
}

function latestDispatchMetadata(ctx: ExtensionContext, dispatchText?: string): {
	registrationNonce?: string;
	dispatchNonce?: string;
	agentSessionId?: string;
	thinkthreadRole?: string;
} {
	return dispatchMetadata(dispatchText ?? "", JSON.stringify(ctx.sessionManager.getEntries()));
}

function innerGitBindingFromContext(
	ctx: ExtensionContext,
	dispatchText: string | undefined,
): ThinkThreadInnerGitBinding | undefined {
	const source = `${JSON.stringify(ctx.sessionManager.getEntries())}\n${dispatchText ?? ""}`;
	const matches = [
		...source.matchAll(/goal_plus_inner_git_binding=([A-Za-z0-9_-]+)/g),
	];
	const encoded = matches.at(-1)?.[1];
	if (!encoded) return undefined;
	let decoded: unknown;
	try {
		const base64 = encoded.replace(/-/g, "+").replace(/_/g, "/");
		const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
		decoded = JSON.parse(Buffer.from(padded, "base64").toString("utf8"));
	} catch {
		throw new Error("ThinkThread AutoResearch-Git binding is not valid base64url JSON");
	}
	if (
		!isRecord(decoded)
		|| decoded.protocol !== "goal-plus.inner-git.v1"
		|| decoded.adapter !== "autoresearch-git"
		|| typeof decoded.inner_agent_id !== "string"
		|| decoded.inner_agent_id !== "autoresearch"
		|| typeof decoded.source_subdir !== "string"
		|| decoded.source_subdir.length === 0
		|| isAbsolute(decoded.source_subdir)
		|| decoded.source_subdir.includes("\\")
		|| decoded.source_subdir.split("/").includes("..")
		|| typeof decoded.metric_name !== "string"
		|| decoded.metric_name.length === 0
		|| typeof decoded.initial_results_base64 !== "string"
		|| !Number.isInteger(decoded.initial_result_count)
		|| Number(decoded.initial_result_count) < 0
	) {
		throw new Error("ThinkThread AutoResearch-Git binding is invalid");
	}
	return decoded as unknown as ThinkThreadInnerGitBinding;
}

async function runThinkThreadInnerGit(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	binding: ThinkThreadInnerGitBinding,
	action: "initialize" | "prepare-attempt",
	dispatchNonce?: string,
): Promise<Record<string, unknown>> {
	const root = sourceRoot(commandContextFrom(ctx));
	const src = join(root, "src");
	const packageDir = join(src, "goal_plus");
	const argsPrefix = existsSync(join(packageDir, "inner_git.py"))
		? ["-I", join(packageDir, "inner_git.py")]
		: ["-I", "-m", "goal_plus.inner_git"];
	const actionArgs = action === "initialize"
		? [
			"initialize",
			"--metric-name",
			binding.metric_name,
			"--initial-results-base64",
			binding.initial_results_base64,
		]
		: ["prepare-attempt", "--dispatch-nonce", dispatchNonce ?? ""];
	const result = await pi.exec(
		pythonExecutable,
		[...argsPrefix, "--workspace", binding.source_subdir, ...actionArgs],
		{ cwd: ctx.cwd, timeout: 60_000 },
	);
	const payload = parseJsonObject(result.stdout.trim().split("\n").at(-1) ?? "");
	if (result.code !== 0 || payload?.ok !== true || payload.adapter !== "autoresearch-git") {
		const detail = String(payload?.error || result.stderr || result.stdout || "inner Git helper failed").trim();
		throw new Error(`ThinkThread AutoResearch-Git ${action} failed: ${detail}`);
	}
	return payload;
}

async function initializeThinkThreadInnerGit(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
): Promise<void> {
	if (!workerInnerGitBinding || !workerActiveDispatchNonce) return;
	if (workerInnerGitInitializedDispatch === workerActiveDispatchNonce) return;
	const result = await runThinkThreadInnerGit(
		pi,
		ctx,
		workerInnerGitBinding,
		"initialize",
	);
	if (!Number.isInteger(result.result_count)) {
		throw new Error("ThinkThread AutoResearch-Git initialize omitted its result count");
	}
	workerInnerGitInitializedDispatch = workerActiveDispatchNonce;
}

async function waitForWorkerRpcTurn(prior: Promise<void>, signal: AbortSignal): Promise<void> {
	if (signal.aborted) throw new Error("ThinkThread worker RPC was aborted");
	let abort!: () => void;
	const aborted = new Promise<never>((_resolve, reject) => {
		abort = () => reject(new Error("ThinkThread worker RPC was aborted"));
		signal.addEventListener("abort", abort, { once: true });
	});
	try {
		await Promise.race([prior, aborted]);
	} finally {
		signal.removeEventListener("abort", abort);
	}
}

async function runThinkThreadWorkerTool(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	name: string,
	params: Record<string, unknown>,
	signal: AbortSignal,
) {
	const priorRpc = workerRpcTail;
	let releaseRpc!: () => void;
	workerRpcTail = new Promise<void>((resolve) => {
		releaseRpc = resolve;
	});
	let acquired = false;
	try {
		await waitForWorkerRpcTurn(priorRpc, signal);
		acquired = true;
		if (signal.aborted) throw new Error("ThinkThread worker RPC was aborted");
		return await runThinkThreadWorkerToolSerial(pi, ctx, name, params, signal);
	} finally {
		if (acquired) releaseRpc();
		else void priorRpc.then(releaseRpc);
	}
}

async function runThinkThreadWorkerToolSerial(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	name: string,
	params: Record<string, unknown>,
	signal: AbortSignal,
) {
	const allowed = new Set([
		"goal_plus_search_get_agent_context",
		"goal_plus_search_get_global_evidence",
		"goal_plus_search_get_evidence_detail",
		"goal_plus_search_run_verifier",
		"goal_plus_search_list_iterations",
		"goal_plus_thinkthread_annotation_next",
		"goal_plus_thinkthread_artifact_view_list",
		"goal_plus_thinkthread_artifact_view_stat",
		"goal_plus_thinkthread_artifact_view_pread",
		"goal_plus_thinkthread_artifact_view_diff",
		"goal_plus_thinkthread_annotation_submit",
	]);
	if (!allowed.has(name)) {
		throw new Error(`ThinkThread Candidate Child tool is not allowed: ${name}`);
	}
	try {
		await acknowledgeThinkThreadDispatch(pi, ctx);
	} catch (error) {
		try {
			await reportThinkThreadDispatchError(ctx, undefined, error);
		} catch {
			// Preserve the original startup error when diagnostic delivery also fails.
		}
		throw error;
	}
	const metadata = latestDispatchMetadata(ctx);
	if (metadata.thinkthreadRole === "annotator" && !THINKTHREAD_ANNOTATION_TOOLS.has(name)) {
		throw new Error(`ThinkThread Annotation Child tool is not allowed: ${name}`);
	}
	if (metadata.thinkthreadRole !== "annotator" && THINKTHREAD_ANNOTATION_TOOLS.has(name)) {
		throw new Error(`ThinkThread Candidate Child annotation tool is not allowed: ${name}`);
	}
	const client = await workerAgentPosixClient();
	const self = await client.invoke("self", {});
	const parentId = typeof self.parentThinkthreadId === "string" ? self.parentThinkthreadId : undefined;
	if (!parentId || parentId !== process.env.THINKTHREAD_PARENT_ID) {
		throw new Error("ThinkThread worker RPC parent binding mismatch");
	}
	if (!metadata.agentSessionId) {
		throw new Error("ThinkThread worker RPC agent_session_id is missing from the dispatch");
	}
	await ensureThinkThreadWorkerRegistration(client, parentId, metadata.registrationNonce);
	const requestParams = { ...params };
	if ("_inner_git_attempt" in requestParams) {
		throw new Error("ThinkThread private inner Git metadata is extension-owned");
	}
	if (name === "goal_plus_search_run_verifier" && !requestParams.attempt_id && workerInnerGitBinding) {
		if (!metadata.dispatchNonce) {
			throw new Error("ThinkThread AutoResearch-Git attempt has no dispatch nonce");
		}
		requestParams._inner_git_attempt = await runThinkThreadInnerGit(
			pi,
			ctx,
			workerInnerGitBinding,
			"prepare-attempt",
			metadata.dispatchNonce,
		);
	}
	const result = await requestWorkerTool(
		client, parentId, metadata.agentSessionId, name, requestParams, signal,
	);
	if (name === "goal_plus_search_run_verifier" && isRecord(result) && result.state === "accepted") workerVerifierSubmitted = true;
	return {
		content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
		details: result,
	};
}

async function ensureThinkThreadWorkerRegistration(
	client: any,
	parentId: string,
	registrationNonce: string | undefined,
) {
	if (workerRegistrationSent) return;
	if (!registrationNonce) {
		throw new Error("ThinkThread worker registration nonce is missing from the dispatch");
	}
	await client.invoke("message.send", {
		recipientThinkthreadId: parentId,
		text: JSON.stringify({
			protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL,
			type: "registration",
			registration_nonce: registrationNonce,
		}),
		wake: false,
	});
	workerRegistrationSent = true;
}

async function validateThinkThreadWorker(ctx: ExtensionContext): Promise<void> {
	if (!isThinkThreadWorker) return;
	const client = await workerAgentPosixClient();
	const self = await client.invoke("self", {});
	const parentId = typeof self.parentThinkthreadId === "string"
		? self.parentThinkthreadId
		: undefined;
	if (!parentId || parentId !== process.env.THINKTHREAD_PARENT_ID) {
		throw new Error("Goal Plus worker role does not match the authenticated ThinkThread Child");
	}
	const capabilities = Array.isArray(self.capabilities)
		? self.capabilities
			.filter((item: unknown) => isRecord(item) && typeof item.id === "string")
			.map((item: { id: string }) => item.id)
			.sort()
		: [];
	if (capabilities.length !== 1 || capabilities[0] !== "thinkthread.message") {
		throw new Error("Goal Plus Candidate Child must have a Message-only Capability grant");
	}
	const metadata = latestDispatchMetadata(ctx);
	await ensureThinkThreadWorkerRegistration(
		client,
		parentId,
		metadata.registrationNonce,
	);
}

function scopeThinkThreadWorkerTools(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	dispatchText?: string,
): void {
	if (!isThinkThreadWorker) return;
	const metadata = latestDispatchMetadata(ctx);
	const dispatchRole = /goal_plus_thinkthread_role=([A-Za-z0-9._-]+)/.exec(
		dispatchText ?? "",
	)?.[1] ?? metadata.thinkthreadRole;
	if (dispatchRole === "annotator") {
		pi.setActiveTools([...THINKTHREAD_ANNOTATION_TOOL_NAMES]);
		return;
	}
	pi.setActiveTools(
		pi.getActiveTools().filter((name) => !THINKTHREAD_ANNOTATION_TOOLS.has(name)
			&& (name !== "goal_plus_search_get_evidence_detail" || thinkThreadSupplementalEnabled)),
	);
}

async function acknowledgeThinkThreadDispatch(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	dispatchText?: string,
): Promise<void> {
	if (!isThinkThreadWorker) return;
	const metadata = latestDispatchMetadata(ctx, dispatchText);
	if (metadata.dispatchNonce && metadata.dispatchNonce !== workerActiveDispatchNonce) {
		workerActiveDispatchNonce = metadata.dispatchNonce;
		workerVerifierSubmitted = false;
		workerMissingAttemptContinuationSent = false;
		workerInnerGitBinding = innerGitBindingFromContext(ctx, dispatchText);
		workerInnerGitInitializedDispatch = undefined;
	}
	if (metadata.dispatchNonce && !workerAcknowledgedDispatches.has(metadata.dispatchNonce)) {
		const client = await workerAgentPosixClient();
		const self = await client.invoke("self", {});
		const parentId = self.parentThinkthreadId;
		if (typeof parentId !== "string") {
			throw new Error("Goal Plus dispatch acknowledgement requires a ThinkThread Child");
		}
		await ensureThinkThreadWorkerRegistration(
			client,
			parentId,
			metadata.registrationNonce,
		);
		const messageId = await dispatchMessageId(client, parentId, metadata.dispatchNonce);
		await client.invoke("message.send", {
			recipientThinkthreadId: parentId,
			text: JSON.stringify({
				protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL,
				type: "dispatch_ack",
				dispatch_nonce: metadata.dispatchNonce,
				message_id: messageId,
			}),
			wake: false,
		});
		workerAcknowledgedDispatches.add(metadata.dispatchNonce);
		workerSteeringIdentity = metadata.agentSessionId && metadata.thinkthreadRole !== "annotator"
			? { agentSessionId: metadata.agentSessionId, dispatchNonce: metadata.dispatchNonce, messageId }
			: undefined;
	}
	await initializeThinkThreadInnerGit(pi, ctx);
}

async function reportThinkThreadDispatchError(
	ctx: ExtensionContext,
	dispatchText: string | undefined,
	error: unknown,
): Promise<void> {
	if (!isThinkThreadWorker) return;
	const metadata = latestDispatchMetadata(ctx, dispatchText);
	if (!metadata.dispatchNonce) return;
	const client = await workerAgentPosixClient();
	const self = await client.invoke("self", {});
	const parentId = self.parentThinkthreadId;
	if (typeof parentId !== "string" || parentId !== process.env.THINKTHREAD_PARENT_ID) return;
	await ensureThinkThreadWorkerRegistration(client, parentId, metadata.registrationNonce);
	const message = error instanceof Error ? error.message : String(error);
	await client.invoke("message.send", {
		recipientThinkthreadId: parentId,
		text: JSON.stringify({
			protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL,
			type: "dispatch_error",
			dispatch_nonce: metadata.dispatchNonce,
			error_type: error instanceof Error ? error.name : "Error",
			message: message.slice(0, 2_000),
		}),
		wake: false,
	});
}



async function runPiHostSessionEvent(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	event: "session_start" | "user_turn" | "interrupt" | "execution_lost" | "continue_confirmed",
	delivery: "auto" | "next_user_turn",
	interaction: "interactive" | "headless",
): Promise<GoalPlusHostSessionDecisionPayload | undefined> {
	if (!activeGoalPlusId) return undefined;
	const result = await runHostCommandCli(
		pi,
		commandContextFrom(ctx),
		"goal_plus_host_session_event",
		{
			goal_plus_id: activeGoalPlusId,
			session_id: ctx.sessionManager.getSessionId(),
			event,
			delivery,
			interaction,
		},
	);
	return hostSessionDecisionFrom(result.details);
}

function piHostInvocation(
	ctx: ExtensionContext,
	command: "/goal-plus" | "/goal-plus-with-final-check",
	action: "start" | "edit" | "summary" | "pause" | "resume" | "clear",
	rawInput: string,
	nativeEntryId: string,
) {
	return {
		agent_harness: "pi",
		transport: "pi_extension_command",
		command,
		action,
		session_id: ctx.sessionManager.getSessionId(),
		native_entry_id: nativeEntryId,
		invocation_id: `pi:${randomUUID()}`,
		raw_input_sha256: createHash("sha256").update(rawInput).digest("hex"),
		invoked_at: new Date().toISOString(),
	};
}

function persistGoalState(pi: ExtensionAPI) {
	pi.appendEntry(STATE_ENTRY_TYPE, {
		activeGoalPlusId,
		continuationCount,
		stopPolicyDelivered,
		startedAt: activeGoalStartedAt,
		startEntryCount: activeGoalStartEntryCount,
		stagedPublicationNotifications: stagedPublicationNotifications.map((item) => ({ ...item })),
		status: cachedGoalStatus?.status,
		phase: cachedGoalStatus?.phase,
		updatedAt: new Date().toISOString(),
	} satisfies GoalPlusNativeState);
}

function canPersistGoalState(ctx: ExtensionContext): boolean {
	return ctx.sessionManager.isPersisted();
}

function piHostInteraction(ctx: ExtensionContext): "interactive" | "headless" {
	return ctx.mode === "tui" ? "interactive" : "headless";
}

function hasPiStartupInput(): boolean {
	// Pi binds extensions before submitting CLI messages/attachments/stdin.
	// Use the host's parser so flag values are never mistaken for user input.
	const args = parseArgs(process.argv.slice(2));
	return args.messages.length > 0 || args.fileArgs.length > 0 || !process.stdin.isTTY;
}

function restoreGoalState(ctx: ExtensionContext) {
	const entries = ctx.sessionManager.getEntries();
	const stateEntry = entries
		.filter((entry: { type: string; customType?: string }) => entry.type === "custom" && entry.customType === STATE_ENTRY_TYPE)
		.pop() as { data?: GoalPlusNativeState } | undefined;
	stagedPublicationNotifications.splice(0);
	presentedPublicationNotifications.clear();
	if (!stateEntry?.data) return;
	activeGoalPlusId = stateEntry.data.activeGoalPlusId;
	continuationCount = stateEntry.data.continuationCount ?? 0;
	stopPolicyDelivered = stateEntry.data.stopPolicyDelivered ?? false;
	activeGoalStartedAt = stateEntry.data.startedAt;
	activeGoalStartEntryCount = stateEntry.data.startEntryCount ?? 0;
	for (const item of stateEntry.data.stagedPublicationNotifications ?? []) {
		if (
			!isRecord(item)
			|| typeof item.runId !== "string"
			|| item.runId.length === 0
			|| typeof item.notificationId !== "string"
			|| item.notificationId.length === 0
		) continue;
		if (!stagedPublicationNotifications.some(
			(existing) => existing.runId === item.runId && existing.notificationId === item.notificationId,
		)) {
			stagedPublicationNotifications.push({
				runId: item.runId,
				notificationId: item.notificationId,
			});
		}
	}
}

function activateGoal(pi: ExtensionAPI, details: unknown, startEntryCount?: number, persist = true) {
	const id = goalPlusIdFrom(details);
	if (!id) return;
	if (id !== activeGoalPlusId || !activeGoalStartedAt) {
		activeGoalStartedAt = new Date().toISOString();
		activeGoalStartEntryCount = startEntryCount ?? activeGoalStartEntryCount;
		continuationCount = 0;
		stopPolicyDelivered = false;
	}
	activeGoalPlusId = id;
	cachedGoalStatus = statusFrom(details);
	setMainGoalPlusToolAvailability(pi, true);
	if (persist) persistGoalState(pi);
}

async function refreshActiveGoal(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	persist = true,
	detail = false,
): Promise<GoalPlusStatusPayload | undefined> {
	if (!activeGoalPlusId) return undefined;
	const result = await runJsonCli(pi, ctx, "goal_plus_status",
		{ goal_plus_id: activeGoalPlusId, ...(detail ? { detail: true } : {}) });
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) return undefined;
	cachedGoalStatus = status;
	if (persist) persistGoalState(pi);
	return status;
}

function isTerminalStatus(status: string | undefined): boolean {
	return status === "complete" || status === "abandoned";
}

function clearActiveGoalState(pi: ExtensionAPI, persist: boolean): void {
	activeGoalPlusId = undefined;
	activeGoalStartedAt = undefined;
	activeGoalStartEntryCount = 0;
	continuationCount = 0;
	stopPolicyDelivered = false;
	stagedPublicationNotifications.splice(0);
	presentedPublicationNotifications.clear();
	cachedGoalStatus = undefined;
	goalContextFullKey = undefined;
	goalPlusSessionAuthorized = false;
	setMainGoalPlusToolAvailability(pi, false);
	if (persist) persistGoalState(pi);
}

function queuePublicationNotificationTurn(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	runId: string,
	notificationIds: string[],
): void {
	const content = "处理本轮 native context 中的 Goal Plus publication 终态通知。不要恢复 Goal Plus，也不要调用 mutation tool。";
	appendInjectionObservation(pi, ctx, {
		kind: "publication_notification", role, channel: "follow_up", target_id: runId,
		notification_count: notificationIds.length, content,
	});
	pi.sendMessage(
		{
			customType: "goal-plus-publication-notification-delivery",
			content,
			display: false,
			details: {
				run_id: runId,
				notification_ids: notificationIds,
				delivery: "durable_turn_boundary",
			},
		},
		{ triggerTurn: true, deliverAs: "followUp" },
	);
}

function countToolCalls(content: unknown): number {
	if (!Array.isArray(content)) return 0;
	return content.filter((item) => isRecord(item) && item.type === "toolCall").length;
}

function collectGoalUsageFromEntries(entries: unknown[]): GoalPlusUsageTotals {
	const startIndex = Math.min(Math.max(0, activeGoalStartEntryCount), entries.length);
	const totals: GoalPlusUsageTotals = {
		assistantMessages: 0,
		toolCalls: 0,
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		cost: 0,
	};
	for (const entry of entries.slice(startIndex)) {
		if (!isRecord(entry) || entry.type !== "message" || !isRecord(entry.message)) continue;
		const message = entry.message;
		if (message.role !== "assistant") continue;
		const usage = isRecord(message.usage) ? message.usage : undefined;
		const cost = usage && isRecord(usage.cost) ? usage.cost : undefined;
		totals.assistantMessages += 1;
		totals.toolCalls += countToolCalls(message.content);
		totals.input += numberFrom(usage?.input);
		totals.output += numberFrom(usage?.output);
		totals.cacheRead += numberFrom(usage?.cacheRead);
		totals.cacheWrite += numberFrom(usage?.cacheWrite);
		totals.cost += numberFrom(cost?.total);
	}
	return totals;
}

function stringFrom(value: unknown): string | undefined {
	return typeof value === "string" && value.length > 0 ? value : undefined;
}

function wholeGoalPresetDurationSeconds(
	rawGoal: string | undefined,
	commandConfig?: Record<string, unknown>,
): number | undefined {
	const structured = optionalNumberFrom(commandConfig?.max_duration_seconds);
	if (structured !== undefined && structured > 0) return structured;
	if (!rawGoal) return undefined;
	const english = rawGoal.match(
		/(?:\bwithin\s+|\bpreset(?:\s+task)?\s+duration\s+is\s+)(\d+(?:\.\d+)?)\s*(seconds?|minutes?|hours?)\b/i,
	);
	if (english) {
		const value = Number(english[1]);
		const unit = english[2].toLowerCase();
		return value * (unit.startsWith("hour") ? 3600 : unit.startsWith("minute") ? 60 : 1);
	}
	const chinese = rawGoal.match(
		/预设(?:任务)?时间(?:为|是)\s*(\d+(?:\.\d+)?)\s*(秒|分钟|小时)/,
	) ?? rawGoal.match(/(\d+(?:\.\d+)?)\s*(秒|分钟|小时)(?:内|以内)/);
	if (!chinese) return undefined;
	const value = Number(chinese[1]);
	return value * (chinese[2] === "小时" ? 3600 : chinese[2] === "分钟" ? 60 : 1);
}

function monitorSearchTask(snapshot: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
	if (!snapshot || !Array.isArray(snapshot.search_tasks)) return undefined;
	const tasks = snapshot.search_tasks.filter(isRecord);
	const goal = isRecord(snapshot.goal_plus) ? snapshot.goal_plus : undefined;
	const currentRunId = stringFrom(goal?.current_search_run_id);
	return tasks.find((task) => stringFrom(task.run_id) === currentRunId)
		?? tasks.find((task) => task.is_current === true)
		?? tasks.at(-1);
}

function goalElapsedSeconds(snapshot: Record<string, unknown> | undefined): number {
	const goal = snapshot && isRecord(snapshot.goal_plus) ? snapshot.goal_plus : undefined;
	const createdAt = stringFrom(goal?.created_at);
	const updatedAt = stringFrom(goal?.updated_at);
	const createdAtMs = createdAt ? Date.parse(createdAt) : NaN;
	const updatedAtMs = updatedAt ? Date.parse(updatedAt) : NaN;
	if (Number.isFinite(createdAtMs) && Number.isFinite(updatedAtMs)) {
		return Math.max(0, (updatedAtMs - createdAtMs) / 1000);
	}
	const startedAtMs = activeGoalStartedAt ? Date.parse(activeGoalStartedAt) : NaN;
	return Number.isFinite(startedAtMs) ? Math.max(0, (Date.now() - startedAtMs) / 1000) : 0;
}

function usageSummaryFromMonitor(value: unknown): GoalPlusUsageSummary | undefined {
	if (!isRecord(value)) return undefined;
	const processedTokens = optionalNumberFrom(value.processed_tokens)
		?? numberFrom(value.input_tokens)
			+ numberFrom(value.cached_input_tokens)
			+ numberFrom(value.cache_write_tokens)
			+ numberFrom(value.output_tokens);
	return {
		assistantMessages: numberFrom(value.assistant_messages),
		toolCalls: numberFrom(value.tool_calls),
		processedTokens,
		costUsd: numberFrom(value.cost_usd),
	};
}

function candidateModelMapFromMonitor(snapshot: Record<string, unknown> | undefined): Map<string, string> {
	const modelByCandidate = new Map<string, string>();
	if (snapshot && Array.isArray(snapshot.subagents)) {
		for (const subagent of snapshot.subagents.filter(isRecord)) {
			const candidateId = stringFrom(subagent.candidate_id);
			const observability = isRecord(subagent.observability) ? subagent.observability : undefined;
			const execution = observability && isRecord(observability.execution) ? observability.execution : undefined;
			const model = stringFrom(execution?.model);
			if (candidateId && model) modelByCandidate.set(candidateId, model);
		}
	}
	return modelByCandidate;
}

function progressCandidatesFromMonitor(
	snapshot: Record<string, unknown> | undefined,
): GoalPlusCandidateProgress[] {
	if (!snapshot || !isRecord(snapshot.candidates)) return [];
	const modelByCandidate = candidateModelMapFromMonitor(snapshot);
	const nowMs = Date.now();
	return Object.values(snapshot.candidates)
		.filter(isRecord)
		.map((candidate) => {
			const candidateId = stringFrom(candidate.candidate_id) ?? "unknown";
			const avgRoundSeconds = optionalNumberFrom(candidate.avg_round_seconds);
			const lastVerifierMs = Date.parse(stringFrom(candidate.last_verifier_at) ?? "");
			const roundElapsedSeconds = Number.isFinite(lastVerifierMs)
				? Math.max(0, (nowMs - lastVerifierMs) / 1000)
				: undefined;
			const nextResultSeconds = avgRoundSeconds !== undefined && roundElapsedSeconds !== undefined
				? Math.max(0, avgRoundSeconds - roundElapsedSeconds)
				: undefined;
			return {
				candidateId,
				model: modelByCandidate.get(candidateId),
				verifierCount: numberFrom(candidate.verifier_count),
				bestScore: optionalNumberFrom(candidate.best_iteration_score)
					?? optionalNumberFrom(candidate.last_score),
				status: stringFrom(candidate.status) || undefined,
				lastScore: optionalNumberFrom(candidate.last_score),
				roundElapsedSeconds,
				nextResultSeconds,
			};
		});
}

function candidateCompletionsFromMonitor(
	snapshot: Record<string, unknown> | undefined,
	selectedCandidateId: string | undefined,
): GoalPlusCandidateCompletion[] {
	if (!snapshot || !isRecord(snapshot.candidates)) return [];
	const modelByCandidate = candidateModelMapFromMonitor(snapshot);
	return Object.values(snapshot.candidates)
		.filter(isRecord)
		.map((candidate) => {
			const candidateId = stringFrom(candidate.candidate_id) ?? "unknown";
			return {
				candidateId,
				model: modelByCandidate.get(candidateId),
				score: optionalNumberFrom(candidate.best_iteration_score) ?? optionalNumberFrom(candidate.last_score),
				verifierCount: numberFrom(candidate.verifier_count),
				disposition: stringFrom(candidate.last_disposition),
				selected: candidateId === selectedCandidateId,
			};
		})
		.sort((left, right) => left.candidateId.localeCompare(right.candidateId));
}

function buildGoalCompletionView(
	status: GoalPlusStatusPayload,
	usage: GoalPlusUsageTotals,
	snapshot: Record<string, unknown> | undefined,
): GoalPlusCompletionView {
	const goal = snapshot && isRecord(snapshot.goal_plus) ? snapshot.goal_plus : undefined;
	const task = monitorSearchTask(snapshot);
	const statistics = task && isRecord(task.statistics) ? task.statistics : undefined;
	const scores = statistics && isRecord(statistics.scores) ? statistics.scores : undefined;
	const selection = statistics && isRecord(statistics.selection) ? statistics.selection : undefined;
	const hostCleanup = task && isRecord(task.host_cleanup) ? task.host_cleanup : undefined;
	const workerUsage = usageSummaryFromMonitor(statistics?.usage);
	const statusLinked = status.current_search ?? status.linked_search;
	const linked = goal && isRecord(goal.linked_search)
		? goal.linked_search
		: isRecord(statusLinked)
			? statusLinked
			: undefined;
	const rawGoal = stringFrom(goal?.raw_goal) ?? status.raw_goal;
	const commandConfig = isRecord(goal?.command_config)
		? goal.command_config
		: status.command_config;
	const promotionVerified = typeof selection?.promotion_verified === "boolean"
		? selection.promotion_verified
		: stringFrom(task?.state) === "promoted"
			? true
			: undefined;
	const selectedCandidateId = stringFrom(task?.selected_candidate_id) ?? stringFrom(linked?.selected_candidate_id);
	return {
		status: stringFrom(goal?.status) ?? status.status ?? "unknown",
		elapsedSeconds: goalElapsedSeconds(snapshot),
		presetDurationSeconds: wholeGoalPresetDurationSeconds(rawGoal, commandConfig),
		goalPlusId: stringFrom(goal?.goal_plus_id) ?? status.goal_plus_id ?? activeGoalPlusId,
		searchTasksTotal: optionalNumberFrom(goal?.search_tasks_total)
			?? status.search_tasks_total
			?? status.search_tasks?.length
			?? 0,
		candidatesTotal: optionalNumberFrom(task?.candidates_total),
		verifierRunsTotal: optionalNumberFrom(task?.verifier_runs_total),
		selectedCandidateId,
		metricName: stringFrom(scores?.metric_name),
		metricDirection: scores?.direction === "minimize" ? "minimize" : scores?.direction === "maximize" ? "maximize" : undefined,
		baselineScore: optionalNumberFrom(scores?.baseline),
		selectedScore: optionalNumberFrom(scores?.selected) ?? optionalNumberFrom(scores?.best),
		promotionPassed: promotionVerified,
		hostCleanupState: stringFrom(hostCleanup?.state),
		reportPath: stringFrom(linked?.report_path) ?? stringFrom(task?.report_path),
		htmlReportPath: stringFrom(linked?.html_report_path) ?? stringFrom(task?.html_report_path),
		mainUsage: {
			assistantMessages: usage.assistantMessages,
			toolCalls: usage.toolCalls,
			processedTokens: usage.input + usage.output + usage.cacheRead + usage.cacheWrite,
			costUsd: usage.cost,
		},
		workerUsage,
		candidates: candidateCompletionsFromMonitor(snapshot, selectedCandidateId),
	};
}

function buildGoalProgressView(
	status: GoalPlusStatusPayload,
	snapshot: Record<string, unknown> | undefined,
): GoalPlusProgressView {
	const goal = snapshot && isRecord(snapshot.goal_plus) ? snapshot.goal_plus : undefined;
	const task = monitorSearchTask(snapshot);
	const statistics = task && isRecord(task.statistics) ? task.statistics : undefined;
	const scores = statistics && isRecord(statistics.scores) ? statistics.scores : undefined;
	const selection = statistics && isRecord(statistics.selection) ? statistics.selection : undefined;
	const goalStatus = stringFrom(goal?.status) ?? status.status;
	const taskState = stringFrom(task?.state);
	const state: GoalPlusProgressState = status.control?.state === "paused" || goalStatus === "blocked"
		? "blocked"
		: isTerminalStatus(goalStatus)
			? "complete"
			: taskState === "selecting" || taskState === "promoted"
				? "selecting"
				: task
					? "active"
					: "preparing";
	const selectedCandidateId = stringFrom(task?.selected_candidate_id);
	return {
		state,
		elapsedSeconds: goalElapsedSeconds(snapshot),
		metricName: stringFrom(scores?.metric_name),
		metricDirection: scores?.direction === "minimize" ? "minimize" : scores?.direction === "maximize" ? "maximize" : undefined,
		baselineScore: optionalNumberFrom(scores?.baseline),
		bestCandidateId: stringFrom(task?.best_candidate_id) || undefined,
		bestScore: optionalNumberFrom(scores?.best) ?? optionalNumberFrom(task?.best_score),
		selectedCandidateId: selectedCandidateId || undefined,
		promotionPassed: selection?.promotion_verified === true,
		candidatesTotal: optionalNumberFrom(task?.candidates_total) ?? 0,
		lanesEvaluated: optionalNumberFrom(task?.candidates_evaluated) ?? 0,
		verifierRunsTotal: optionalNumberFrom(task?.verifier_runs_total) ?? 0,
		candidates: progressCandidatesFromMonitor(snapshot),
	};
}

async function readMonitorSnapshot(
	pi: ExtensionAPI, ctx: CommandRuntimeContext, status: GoalPlusStatusPayload,
): Promise<Record<string, unknown> | undefined> {
	try {
		// The slim monitor summary omits search_tasks/candidates; the surfaces need
		// the detailed projection.
		const monitor = await runJsonCli(pi, ctx, "goal_plus_monitor_snapshot", {
			goal_plus_id: status.goal_plus_id ?? activeGoalPlusId,
			run_id: status.current_search_run_id ?? undefined,
			detail: true,
		});
		if (isRecord(monitor.details) && monitor.details.ok !== false) return monitor.details;
	} catch {
		// Live progress is advisory; the next turn boundary retries with fresh state.
	}
	return undefined;
}

// The live surfaces exist only while a Search run is being explored: they appear
// when the run starts (or when a session resumes into one), refresh on a fixed
// interval, and disappear shortly after the run stops being in progress.
const SEARCH_PROGRESS_REFRESH_MS = 5000;
const SEARCH_PROGRESS_HIDE_DELAY_MS = 5000;
// Sustained unknown reads back off instead of burning two RPC round-trips
// every 5s while idle; any authoritative read resets the cadence.
const SEARCH_PROGRESS_IDLE_REFRESH_MS = 30000;
const SEARCH_PROGRESS_UNKNOWN_STREAK = 3;
// Only these RunStates still move on their own. selection_blocked,
// ready_to_promote and the terminal states have settled - nothing changes
// until Main acts again - so the idle poll stops and a turn boundary restarts
// it if the run resumes. A future RunState lands here too, never in "running".
const RUN_STATES_IN_PROGRESS = new Set(["frozen_spec", "running", "waiting_for_workers", "evaluating", "selecting"]);

// A failed or incomplete read is "unknown": it must never hide the surfaces or
// stop the retry loop, because only an authoritative read may end a Search run.
type SearchProgressPhase = "running" | "blocked" | "finished" | "unknown";
let searchProgressPoll: ReturnType<typeof setTimeout> | undefined;
let searchProgressUnknownStreak = 0;
let searchProgressHide: ReturnType<typeof setTimeout> | undefined;
let searchProgressPhaseShown: SearchProgressPhase | undefined;
let searchProgressChecking = false;

function progressDebug(event: string, details: Record<string, unknown>): void {
	if (process.env.GOAL_PLUS_DEBUG !== "1") return;
	try {
		console.error(JSON.stringify({ event, ...details }));
	} catch {
		// Diagnostics cannot change progress handling.
	}
}

function stopSearchProgressPoll(): void {
	if (searchProgressPoll !== undefined) clearTimeout(searchProgressPoll);
	searchProgressPoll = undefined;
	searchProgressUnknownStreak = 0;
}

function cancelSearchProgressHide(): void {
	if (searchProgressHide !== undefined) clearTimeout(searchProgressHide);
	searchProgressHide = undefined;
}

function scheduleSearchProgressHide(ctx: ExtensionContext): void {
	if (searchProgressHide !== undefined) return;
	searchProgressHide = setTimeout(() => {
		searchProgressHide = undefined;
		clearPiProgress(ctx);
	}, SEARCH_PROGRESS_HIDE_DELAY_MS);
}

function ensureSearchProgressPoll(pi: ExtensionAPI, ctx: ExtensionContext): void {
	if (searchProgressPoll !== undefined) return;
	const idle = searchProgressUnknownStreak >= SEARCH_PROGRESS_UNKNOWN_STREAK;
	searchProgressPoll = setTimeout(() => {
		searchProgressPoll = undefined;
		void checkSearchProgress(pi, ctx);
	}, idle ? SEARCH_PROGRESS_IDLE_REFRESH_MS : SEARCH_PROGRESS_REFRESH_MS);
}

function searchProgressPhase(
	status: GoalPlusStatusPayload,
	snapshot: Record<string, unknown> | undefined,
): SearchProgressPhase {
	const runId = stringFrom(status.current_search_run_id);
	// Without a Search run there is no Search surface, whatever the Goal's
	// control state is: pausing a plain Goal must not render a blocked frame.
	if (!runId) return "finished";
	if (status.control?.state === "paused" || status.status === "blocked") return "blocked";
	const task = monitorSearchTask(snapshot);
	if (!task || task.is_current !== true || stringFrom(task.run_id) !== runId) return "unknown";
	return RUN_STATES_IN_PROGRESS.has(stringFrom(task.state)) ? "running" : "finished";
}

function applySearchProgress(
	ctx: ExtensionContext,
	status: GoalPlusStatusPayload,
	snapshot: Record<string, unknown> | undefined,
): void {
	const phase = searchProgressPhase(status, snapshot);
	progressDebug("goal_plus_progress_phase", {
		phase,
		run_id: stringFrom(status.current_search_run_id),
		visible: searchProgressPhaseShown !== undefined,
	});
	if (phase === "finished") {
		stopSearchProgressPoll();
		if (searchProgressPhaseShown !== undefined) scheduleSearchProgressHide(ctx);
		return;
	}
	if (phase === "unknown") {
		// Keep whatever is on screen; the interval retries with fresh state.
		cancelSearchProgressHide();
		return;
	}
	cancelSearchProgressHide();
	showPiProgress(ctx, buildGoalProgressView(status, snapshot));
	searchProgressPhaseShown = phase;
	if (phase === "blocked") {
		// A blocked Goal is not exploring; keep the blocked frame briefly, then hide.
		stopSearchProgressPoll();
		scheduleSearchProgressHide(ctx);
	}
}

function observeSearchProgress(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	status: GoalPlusStatusPayload,
	snapshot: Record<string, unknown> | undefined,
): void {
	progressDebug("goal_plus_progress_read", {
		source: stringFrom((status.details as Record<string, unknown> | undefined)?.tool),
		goal_plus_id: stringFrom(status.goal_plus_id),
		goal_status: stringFrom(status.status),
		phase: stringFrom(status.phase),
		control: stringFrom(status.control?.state),
		run_id: stringFrom(status.current_search_run_id),
		snapshot_ok: snapshot?.ok,
		snapshot_keys: snapshot ? Object.keys(snapshot).slice(0, 12).join(",") : undefined,
		snapshot_tasks: Array.isArray(snapshot?.search_tasks) ? snapshot.search_tasks.length : undefined,
		snapshot_error: stringFrom(snapshot?.error),
	});
	applySearchProgress(ctx, status, snapshot);
	const phase = searchProgressPhase(status, snapshot);
	searchProgressUnknownStreak = phase === "unknown" ? searchProgressUnknownStreak + 1 : 0;
	if (phase === "running" || phase === "unknown") ensureSearchProgressPoll(pi, ctx);
}

async function checkSearchProgress(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
	if (role !== "main" || !activeGoalPlusId) return;
	if (searchProgressChecking) return;
	searchProgressChecking = true;
	try {
		const commandCtx = commandContextFrom(ctx);
		// Only an authoritative read may end the surfaces. A failure payload is
		// not "the Goal is gone": it is unknown and keeps the retry loop alive.
		const result = await runJsonCli(pi, commandCtx, "goal_plus_status", { goal_plus_id: activeGoalPlusId });
		if (isRecord(result.details) && result.details.ok === false) {
			progressDebug("goal_plus_progress_read_failed", { tool: "goal_plus_status" });
			searchProgressUnknownStreak += 1;
			if (searchProgressPhaseShown !== undefined) ensureSearchProgressPoll(pi, ctx);
			return;
		}
		const status = statusFrom(result.details);
		if (status?.goal_plus_id) {
			cachedGoalStatus = status;
			observeSearchProgress(pi, ctx, status, await readMonitorSnapshot(pi, commandCtx, status));
			return;
		}
		stopSearchProgressPoll();
		if (searchProgressPhaseShown !== undefined) scheduleSearchProgressHide(ctx);
	} catch (error) {
		// A failed read must not clear the surfaces or stop the retry loop.
		progressDebug("goal_plus_progress_read_failed", {
			error: error instanceof Error ? error.message : String(error),
		});
		searchProgressUnknownStreak += 1;
		if (searchProgressPhaseShown !== undefined) ensureSearchProgressPoll(pi, ctx);
	} finally {
		searchProgressChecking = false;
	}
}

function appendGoalStats(
	pi: ExtensionAPI,
	status: GoalPlusStatusPayload,
	usage: GoalPlusUsageTotals,
	snapshot?: Record<string, unknown>,
): string {
	const endedAt = new Date().toISOString();
	const presentation = buildPiCompletionPresentation(buildGoalCompletionView(status, usage, snapshot));
	const message = presentation.lines.join("\n");
	pi.appendEntry<GoalPlusStatsEntry>(GOAL_PLUS_STATS_ENTRY_TYPE, {
		goal_plus_id: status.goal_plus_id ?? activeGoalPlusId,
		status: status.status,
		startedAt: activeGoalStartedAt,
		endedAt,
		usage,
		message,
		collapsedLineCount: presentation.collapsedLineCount,
	});
	return message;
}

function monitorSnapshotPresentation(value: unknown): { status: string; lines: string[] } | undefined {
	if (!isRecord(value) || value.ok === false) return undefined;
	const goal = isRecord(value.goal_plus) ? value.goal_plus : undefined;
	const task = monitorSearchTask(value);
	const statistics = task && isRecord(task.statistics) ? task.statistics : undefined;
	const scores = statistics && isRecord(statistics.scores) ? statistics.scores : undefined;
	const selection = statistics && isRecord(statistics.selection) ? statistics.selection : undefined;
	const status = stringFrom(goal?.status) ?? stringFrom(task?.state) ?? "snapshot";
	const candidateId = stringFrom(task?.selected_candidate_id)
		?? stringFrom(task?.best_candidate_id);
	const score = optionalNumberFrom(task?.selected_score)
		?? optionalNumberFrom(task?.best_score)
		?? optionalNumberFrom(scores?.selected)
		?? optionalNumberFrom(scores?.best);
	const verifierRuns = optionalNumberFrom(task?.verifier_runs_total);
	const promotionVerified = selection?.promotion_verified === true || stringFrom(task?.state) === "promoted";
	const headline = [
		status,
		candidateId ? `${candidateId}${score === undefined ? "" : ` ${score.toLocaleString("en-US", { maximumFractionDigits: 4 })}`}` : undefined,
		verifierRuns === undefined ? undefined : `${verifierRuns} verify`,
		promotionVerified ? "promoted" : undefined,
	].filter(Boolean).join(" · ");
	const runId = stringFrom(task?.run_id);
	const candidatesTotal = optionalNumberFrom(task?.candidates_total);
	const candidatesEvaluated = optionalNumberFrom(task?.candidates_evaluated);
	const detail = [
		runId,
		candidatesTotal === undefined
			? undefined
			: `${candidatesEvaluated ?? 0}/${candidatesTotal} candidates`,
		stringFrom(goal?.phase),
	].filter(Boolean).join(" · ");
	return { status, lines: [headline, ...(detail ? [detail] : [])] };
}

function showPiProgress(ctx: ExtensionContext, view: GoalPlusProgressView): void {
	const presentation = buildPiProgressPresentation(view);
	const accent: "success" | "error" | "accent" = view.state === "complete"
		? "success" : view.state === "blocked" ? "error" : "accent";
	const bar = ctx.ui.theme.fg(accent, "▌");
	const lines = presentation.lines.map((line) => {
		if (line === presentation.lines[0]) return `${bar} ${ctx.ui.theme.fg(accent, line)}`;
		if (line.includes("% better")) return `${bar} ${ctx.ui.theme.fg("success", line)}`;
		if (line.includes("% worse")) return `${bar} ${ctx.ui.theme.fg("error", line)}`;
		if (line.startsWith("Lanes ") || line.startsWith("Selected ")) return `${bar} ${line}`;
		if (view.bestCandidateId && line.startsWith(view.bestCandidateId)) {
			return `${bar} ${ctx.ui.theme.fg("accent", line)}`;
		}
		if (line.includes("running")) return `${bar} ${line}`;
		return `${bar} ${ctx.ui.theme.fg("dim", line)}`;
	});
	// One surface only: the widget above the editor. The footer status slot and the
	// streaming loader row would just duplicate the same summary.
	ctx.ui.setWidget(GOAL_PLUS_PROGRESS_WIDGET_KEY, lines);
}

function clearPiProgress(ctx: ExtensionContext): void {
	stopSearchProgressPoll();
	cancelSearchProgressHide();
	searchProgressPhaseShown = undefined;
	ctx.ui.setWidget(GOAL_PLUS_PROGRESS_WIDGET_KEY, undefined);
}

// The full context (goal text, command config, final-check policy) is only worth
// its tokens on the first turn of a session, after a compaction, or when the Goal
// revision/control changes. Other turns get the compact status line and read the
// rest through goal_plus_status when they need it.
function buildGoalPlusBriefContext(status: GoalPlusStatusPayload): string {
	const action = status.next_action;
	const lines = [
		"[GOAL PLUS ACTIVE]",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId ?? "unknown"}`,
		`status: ${status.status ?? "unknown"} · control: ${status.control?.state ?? "unknown"}`
			+ ` · phase: ${status.phase ?? "unknown"} · revision: ${status.goal_revision ?? 1}`,
		action
			? `next_action: ${action.kind ?? "unknown"}（required=${action.required === false ? "false" : "true"}）`
				+ ` — ${action.description ?? ""}`
			: "next_action: 未记录",
		"完整目标、命令配置和 final check policy 只在首轮、压缩后或 revision 变化时注入；"
			+ "需要时用 goal_plus_status(detail=true) 读取，并每轮读取 control、next_action 和 reconciliation。",
	];
	return lines.join("\n");
}

function buildGoalPlusContext(status: GoalPlusStatusPayload): string {
	const action = status.next_action;
	const lines = [
		"[GOAL PLUS ACTIVE]",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId ?? "unknown"}`,
		`status: ${status.status ?? "unknown"}`,
		`control: ${JSON.stringify(status.control ?? {})}`,
		`phase: ${status.phase ?? "unknown"}`,
		`goal_revision: ${status.goal_revision ?? 1}`,
		`command_config: ${JSON.stringify(status.command_config ?? {})}`,
		`final_check_policy: ${JSON.stringify(status.final_check_policy ?? status.policy?.final_check ?? { mode: "disabled" })}`,
		"",
		...(status.raw_goal ? ["原始目标：", status.raw_goal]
			: ["初始化、恢复或 revision 变化时，用 goal_plus_status(detail=true) 读取完整目标和配置。"]),
		"",
		"遵循 Goal Plus MCP 说明；每轮读取 goal_plus_status 的 control、next_action 和 reconciliation。",
	];
	if (action) {
		lines.push(
			"",
				"当前 next_action：",
			`- kind: ${action.kind ?? "unknown"}`,
			`- required: ${action.required === false ? "false" : "true"}`,
			`- description: ${action.description ?? ""}`,
		);
	}

	return lines.join("\n");
}

function buildNativeSessionContinuationPrompt(status: GoalPlusStatusPayload): string {
	return [
		"继续同一个原生 Pi session 中尚未终结的 Goal Plus 任务。",
		buildGoalPlusContext(status),
	].join("\n\n");
}

interface GoalPlusRoleModels {
	annotator?: string;
	workers?: string[];
	workerDirective?: "workers" | "models";
}

type PiModel = NonNullable<ExtensionContext["model"]>;

interface ResolvedGoalPlusRoleModels {
	roleModels: GoalPlusRoleModels;
	models: PiModel[];
}

function leadingCommandDirectives(rawGoal: string): Map<string, string> {
	let remaining = rawGoal.trim();
	const directives = new Map<string, string>();
	while (remaining.length > 0) {
		const match = remaining.match(/^([A-Za-z][A-Za-z0-9_-]*)=([^\s]+)(?:\s+|$)/);
		if (!match) break;
		const name = match[1]!.toLowerCase().replace(/-/g, "_");
		if (directives.has(name)) {
			throw new Error(`/goal-plus accepts at most one ${name}= directive`);
		}
		directives.set(name, match[2]!.trim());
		remaining = remaining.slice(match[0].length).trimStart();
	}
	return directives;
}

function parseGoalPlusRoleModels(rawGoal: string): GoalPlusRoleModels | undefined {
	const directives = leadingCommandDirectives(rawGoal);
	if (directives.has("main")) {
		throw new Error(
			"main= is not supported; select the main model when starting Pi or with /model before /goal-plus",
		);
	}
	if (directives.has("model")) {
		throw new Error("model= is not supported; use annotator= and workers=");
	}
	const annotator = directives.get("annotator");
	const workers = directives.get("workers");
	const models = directives.get("models");
	if (annotator?.includes(",")) {
		throw new Error("annotator= accepts exactly one model; use workers= for a list");
	}
	if (workers && models) {
		throw new Error("workers= and models= are aliases; specify only one");
	}
	const workerModels = workers ?? models;
	if (workerModels?.split(",").some((value) => value.trim().length === 0)) {
		throw new Error("workers=/models= contains an empty model reference");
	}
	if (!annotator && !workerModels) return undefined;
	return {
		annotator,
		workers: workerModels
			? workerModels.split(",").map((value) =>
				value.trim().replace(/\*\d+$/, ""),
			)
			: undefined,
		workerDirective: workers ? "workers" : models ? "models" : undefined,
	};
}

function piModelKey(model: PiModel): string {
	return `${model.provider}/${model.id}`.toLowerCase();
}

function piModelMatchesReference(model: PiModel, requested: string): boolean {
	const needle = requested.toLowerCase();
	return piModelKey(model) === needle
		|| model.id.toLowerCase() === needle
		|| model.name.toLowerCase() === needle;
}

function resolvePiModelReference(ctx: ExtensionContext, requested: string): PiModel {
	const needle = requested.toLowerCase();
	if (ctx.model && piModelMatchesReference(ctx.model, needle)) return ctx.model;
	const availableByCanonical = new Map<string, PiModel>();
	for (const model of ctx.modelRegistry.getAvailable()) {
		availableByCanonical.set(piModelKey(model), model);
	}
	const available = [...availableByCanonical.values()];
	const canonical = available.filter(
		(model) => piModelKey(model) === needle,
	);
	const exactIds = available.filter((model) => model.id.toLowerCase() === needle);
	const exactNames = available.filter((model) => model.name.toLowerCase() === needle);
	const matches = canonical.length > 0 ? canonical : exactIds.length > 0 ? exactIds : exactNames;
	if (matches.length === 0) {
		throw new Error(`requested Pi model is not available: ${requested}`);
	}
	if (matches.length > 1) {
		throw new Error(`requested Pi model is ambiguous; use provider/model: ${requested}`);
	}
	return matches[0]!;
}

function resolveGoalPlusRoleModels(
	ctx: ExtensionContext,
	rawGoal: string,
): ResolvedGoalPlusRoleModels | undefined {
	const requested = parseGoalPlusRoleModels(rawGoal);
	if (!requested) return undefined;
	const annotator = requested.annotator
		? resolvePiModelReference(ctx, requested.annotator)
		: undefined;
	const workers = requested.workers?.map((reference) =>
		resolvePiModelReference(ctx, reference)
	);
	return {
		roleModels: {
			annotator: annotator ? `${annotator.provider}/${annotator.id}` : undefined,
			workers: workers?.map((model) => `${model.provider}/${model.id}`),
			workerDirective: requested.workerDirective,
		},
		models: [annotator, ...(workers ?? [])].filter(
			(model): model is PiModel => Boolean(model),
		),
	};
}

function buildGoalStartPrompt(
	status: GoalPlusStatusPayload,
	roleModels?: GoalPlusRoleModels,
): string {
	const lines = [
		"继续此 Goal Plus 任务。",
		"",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId ?? "unknown"}`,
		`goal_revision: ${status.goal_revision ?? 1}`,
		"",
		"原始目标：",
		status.raw_goal ?? "",
		"",
		"显式命令配置（runtime 将在 SearchSpec draft 保存时校验）：",
		JSON.stringify(status.command_config ?? {}),
	];
	if (roleModels) {
		lines.push(
			"",
			"已解析的模型路由：",
		);
		if (roleModels.annotator) lines.push(`- annotator: ${roleModels.annotator}`);
		if (roleModels.workerDirective) {
			lines.push(`- workers: 使用 command_config.workers（入口别名 ${roleModels.workerDirective}=）。`);
		}
		lines.push(
			"- 冻结 SearchSpec 时，只写入显式角色：annotator 写入 strategy.evidence_annotator.model；workers=/models= 按原有分配规则写入 strategy.models。未指定的角色保持 Pi host native default。",
		);
	}
	lines.push(
		"",
		"遵循 Goal Plus MCP 说明，并从 goal_plus_status 的 next_action 开始执行。",
		"首次启动时，若目标和当前对话未给出最大运行时间，简短告知任务将持续至完成或用户停止，然后继续。",
	);
	if (controllerOwnsCloseout) {
		lines.push(
			"",
			"Host controller owns closeout: settle all workers and verifier evidence, keep the Goal active, then return normally. Do not select, promote, apply promotion, or record the final Search result.",
		);
	}
	return lines.join("\n");
}

interface GoalPlusSlashRequest {
	action: "start" | "edit" | "summary" | "pause" | "resume" | "clear" | "invalid";
	rawGoal: string;
	withFinalCheck: boolean;
	invalidReason?: string;
}

function goalPlusRequestFromSlashInput(text: string): GoalPlusSlashRequest | undefined {
	const match = text.match(/^\/(goal-plus(?:-with-final-check)?)(?:\s+([\s\S]*))?$/);
	if (!match) return undefined;
	const command = match[1];
	const body = (match[2] ?? "").trim();
	if (command === "goal-plus" && !body) {
		return { action: "summary", rawGoal: "", withFinalCheck: false };
	}
	const word = body.split(/\s+/, 1)[0];
	if (command === "goal-plus" && ["summary", "pause", "resume", "clear"].includes(word.toLowerCase())) {
		if (word === body && ["summary", "pause", "resume", "clear"].includes(word)) {
			return { action: word as "summary" | "pause" | "resume" | "clear", rawGoal: "", withFinalCheck: false };
		}
		return {
			action: "invalid",
			rawGoal: "",
			withFinalCheck: false,
			invalidReason:
				"Goal Plus controls require exact lowercase summary/pause/resume/clear without an objective.",
		};
	}
	if (command === "goal-plus" && (body === "edit" || body.startsWith("edit "))) {
		return { action: "edit", rawGoal: body.slice(5).trim(), withFinalCheck: false };
	}
	const normalizedBody = body.toLowerCase();
	if (command === "goal-plus" && (
		normalizedBody === "edit"
		|| normalizedBody.startsWith("edit ")
	)) {
		return {
			action: "invalid",
			rawGoal: "",
			withFinalCheck: false,
			invalidReason: "Pi Goal Plus subcommands must use exact lowercase edit.",
		};
	}
	return {
		action: "start",
		rawGoal: body,
		withFinalCheck: command === "goal-plus-with-final-check",
	};
}

function selectedGoalPlusThinking(pi: ExtensionAPI, ctx: ExtensionContext): string {
	try {
		return pi.getThinkingLevel();
	} catch (error) {
		ctx.ui.notify(`Goal Plus could not observe the current Pi thinking level: ${String(error)}`, "warning");
		return "unavailable";
	}
}

interface PiNativeAgentConfig {
	model_pattern: string;
	thinking_level?: string;
}

function currentPiNativeAgentConfig(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
): PiNativeAgentConfig {
	if (!ctx.model) {
		throw new Error("Goal Plus could not observe the current Pi model before child launch");
	}
	const thinking = selectedGoalPlusThinking(pi, ctx);
	return {
		model_pattern: `${ctx.model.provider}/${ctx.model.id}`,
		thinking_level: thinking === "unavailable" ? undefined : thinking,
	};
}


function piExecutionPolicy(nativeReasoningEffort: string) {
	return {
		execution: {
			mode: "orchestrated",
			main_reasoning_effort: "host_selected",
			delegation: "proactive",
			search_routing: "auto",
			completion: "until_terminal",
			host: {
				protocol: "goal-plus-ultra-v1",
				host_id: "pi",
				native_reasoning_effort: nativeReasoningEffort,
				enforcement: "host",
				operations: ["spawn", "wait", "observe"],
			},
		},
	};
}

async function createGoalPlusStart(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	rawGoal: string,
	withFinalCheck = false,
): Promise<string | undefined> {
	if (activeGoalPlusId) {
		const current = await refreshActiveGoal(
			pi,
			commandContextFrom(ctx),
			canPersistGoalState(ctx),
		);
		if (current && !isTerminalStatus(current.status)) {
			ctx.ui.notify(
				`Goal Plus ${activeGoalPlusId} is still active; resume its native Pi session or use /goal-plus edit <full goal>.`,
				"error",
			);
			return undefined;
		}
	}
	let roleModels: GoalPlusRoleModels | undefined;
	try {
		const resolved = resolveGoalPlusRoleModels(ctx, rawGoal);
		roleModels = resolved?.roleModels;
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		ctx.ui.notify(message, "error");
		pi.sendMessage({
			customType: "goal-plus-error",
			content: message,
			display: true,
			details: { stage: "model-routing" },
		});
		return undefined;
	}
	const observedThinking = selectedGoalPlusThinking(pi, ctx);
	appendPiRuntimeStartEntry(pi, ctx);
	const commandCtx = commandContextFrom(ctx);
	const startEntryCount = ctx.sessionManager.getEntries().length;
	const command = withFinalCheck ? "/goal-plus-with-final-check" : "/goal-plus";
	const result = await runHostCommandCli(pi, commandCtx, "goal_plus_host_start", {
		raw_goal: rawGoal,
		source_path: ctx.cwd,
		invocation: piHostInvocation(
			ctx,
			command,
			"start",
			`${command} ${rawGoal}`,
			`pi-entry-${startEntryCount}`,
		),
		policy: {
			...piExecutionPolicy(observedThinking),
			...(withFinalCheck ? { final_check: { mode: "required" } } : {}),
		},
	});
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) {
		const details =
			isRecord(result.details) && typeof result.details.error === "string"
				? result.details.error
				: "Pi /goal-plus host entrypoint did not return a goal_plus_id";
		pi.sendMessage({
			customType: "goal-plus-error",
			content: details,
			display: true,
			details: { tool: "goal_plus_host_start" },
		});
		return undefined;
	}
	await ensureMainGoalPlusTools?.();
	goalPlusSessionAuthorized = true;
	activateGoal(pi, status, startEntryCount, canPersistGoalState(ctx));
	pi.sendMessage({
		customType: "goal-plus-created",
		content: `Goal Plus ${status.goal_plus_id} created`,
		display: true,
		details: { goal_plus_id: status.goal_plus_id },
	});
	return buildGoalStartPrompt(status, roleModels);
}

async function updateGoalPlusStart(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	rawGoal: string,
): Promise<string | undefined> {
	if (!activeGoalPlusId) {
		ctx.ui.notify("No active Goal Plus record to edit", "error");
		return undefined;
	}
	try {
		resolveGoalPlusRoleModels(ctx, rawGoal);
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		ctx.ui.notify(message, "error");
		pi.sendMessage({
			customType: "goal-plus-error",
			content: message,
			display: true,
			details: { stage: "model-routing" },
		});
		return undefined;
	}
	const commandCtx = commandContextFrom(ctx);
	const current = await refreshActiveGoal(pi, commandCtx, canPersistGoalState(ctx));
	if (!current?.goal_plus_id || typeof current.goal_revision !== "number") return undefined;
	const nativeEntryId = `pi-entry-${ctx.sessionManager.getEntries().length}`;
	const result = await runHostCommandCli(pi, commandCtx, "goal_plus_host_edit", {
		goal_plus_id: current.goal_plus_id,
		raw_goal: rawGoal,
		expected_revision: current.goal_revision,
		reason: "user edited the Goal Plus objective through Pi",
		invocation: piHostInvocation(
			ctx,
			"/goal-plus",
			"edit",
			`/goal-plus edit ${rawGoal}`,
			nativeEntryId,
		),
	});
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) return undefined;
	await ensureMainGoalPlusTools?.();
	goalPlusSessionAuthorized = true;
	activateGoal(pi, status, undefined, canPersistGoalState(ctx));
	if (status.control?.state !== "active" || status.status === "blocked") {
		goalPlusSessionAuthorized = false;
		pi.sendMessage({ customType: "goal-plus-control", content: "Goal Plus edited; it remains stopped until /goal-plus resume.", display: true });
		return undefined;
	}
	return [
			"用户已编辑 Goal Plus 目标。",
		`goal_plus_id: ${status.goal_plus_id}`,
		`goal_revision: ${status.goal_revision ?? "unknown"}`,
			"新的原始目标取代之前的目标。直接执行修订后的目标；需要 Search 时准备当前修订版的评价合同。",
			"以前的 Search 任务仅作为历史证据保留；不要把旧修订版的结果或最终检查视为当前结果。",
	].join("\n");
}

async function controlGoalPlus(
	pi: ExtensionAPI, ctx: ExtensionContext,
	action: "summary" | "pause" | "resume" | "clear",
): Promise<string | undefined> {
	if (!activeGoalPlusId) {
		pi.sendMessage({ customType: "goal-plus-control", content: "No Goal Plus task is bound to this native session.", display: true });
		return undefined;
	}
	const current = await refreshActiveGoal(pi, commandContextFrom(ctx), false);
	if (!current) return undefined;
	const invocation = {
		...piHostInvocation(ctx, "/goal-plus", action, `/goal-plus ${action}`, `pi-entry-${ctx.sessionManager.getEntries().length}`),
		...(action === "resume" && process.env.GOAL_PLUS_RESUME_EXPECTATION
			? { resume_expectation: JSON.parse(process.env.GOAL_PLUS_RESUME_EXPECTATION) } : {}),
	};
	const result = await runHostCommandCli(pi, commandContextFrom(ctx), "goal_plus_host_control", {
		goal_plus_id: activeGoalPlusId, invocation,
	});
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) {
		pi.sendMessage({ customType: "goal-plus-error", content: JSON.stringify(result.details), display: true });
		return undefined;
	}
	if (action !== "summary") {
		await ensureMainGoalPlusTools?.();
		goalPlusSessionAuthorized = status.control?.state === "active";
		activateGoal(pi, status, undefined, canPersistGoalState(ctx));
	}
	if (role === "main") {
		if (action === "clear") {
			clearPiProgress(ctx);
		} else {
			observeSearchProgress(
				pi, ctx, status, await readMonitorSnapshot(pi, commandContextFrom(ctx), status),
			);
		}
	}
	pi.sendMessage({ customType: "goal-plus-control", content: `Goal Plus ${action}: ${status.goal_plus_id}, status=${status.status}, control=${status.control?.state}, reason=${status.control?.reason ?? "none"}`, display: true });
	if (action !== "resume") return undefined;
	// An already running native turn will reach agent_end with the new control
	// state. Do not create a second queued Main for repeated/busy resume.
	if (!ctx.isIdle() || mainStartPending) return undefined;
	return buildNativeSessionContinuationPrompt(status);
}

function startGoalPlusMainTurn(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	kind: "goal_start" | "goal_edit" | "native_continuation",
	context: string,
	deliverAsFollowUp: boolean,
) {
	mainStartPending = true;
	const message = {
		customType: GOAL_PLUS_COMMAND_CONTEXT_TYPE,
		content: context,
		display: false,
		details: {
			goal_plus_id: activeGoalPlusId,
			entrypoint_status: "host_verified",
			goal_revision: cachedGoalStatus?.goal_revision,
			control_version: cachedGoalStatus?.control?.version,
			session_id: cachedGoalStatus?.active_session?.session_id,
			request_id: randomUUID(),
		},
	};
	appendInjectionObservation(pi, ctx, {
		kind, role, channel: deliverAsFollowUp ? "follow_up" : "next_turn_user_message",
		target_id: activeGoalPlusId,
		goal_revision: cachedGoalStatus?.goal_revision,
		control_version: cachedGoalStatus?.control?.version,
		content: context,
	});
	if (deliverAsFollowUp) {
		pi.sendMessage(message, { triggerTurn: true, deliverAs: "followUp" });
	} else {
		// Custom-message triggers bypass before_agent_start in native Pi.
		pi.sendMessage(message, { deliverAs: "nextTurn" });
		pi.sendUserMessage("Continue the authorized Goal Plus task.");
	}
}

function registerGoalPlusTool<TParams extends TSchema>(pi: ExtensionAPI, definition: ToolDefinition<TParams>) {
	diagnosticToolContracts.set(definition.name, {
		name: definition.name, description: definition.description, parameters: definition.parameters,
	});
	// The registration closure is the ownership capability. This still gates our
	// own execute callback if host source metadata is absent; an overridden tool
	// from another extension never inherits this closure merely by sharing a name.
	pi.registerTool({
		...definition,
		async execute(toolCallId, params, signal, onUpdate, ctx) {
			const event: ToolCallEvent = { type: "tool_call", toolCallId, toolName: definition.name, input: params };
			const denied = workspaceGuard(event, ctx, "goal-plus") || await mainGate(event, ctx, "goal-plus");
			if (denied?.block) throw new Error(denied.reason);
			const result = await definition.execute(toolCallId, params, signal, onUpdate, ctx);
			// Creating the Search run links it to the Goal; surface it right away
			// instead of waiting for the next turn boundary. The interval keeps
			// retrying even if this first read fails.
			if (definition.name === "goal_plus_search_create") {
				ensureSearchProgressPoll(pi, ctx);
				void checkSearchProgress(pi, ctx);
			}
			if (process.env.GOAL_PLUS_DEBUG === "1") {
				try {
					console.error(JSON.stringify({
						event: "goal_plus_api_response_size",
						transport: "pi-tool",
						tool: definition.name,
						format: "json",
						bytes: Buffer.byteLength(JSON.stringify(result), "utf8"),
						characters: Array.from(JSON.stringify(result)).length,
						text_bytes: result.content.filter(item => item.type === "text")
							.reduce((sum, item) => sum + Buffer.byteLength(item.text, "utf8"), 0),
						text_characters: result.content.filter(item => item.type === "text")
							.reduce((sum, item) => sum + Array.from(item.text).length, 0),
						details_bytes: result.details === undefined ? 0
							: Buffer.byteLength(JSON.stringify(result.details), "utf8"),
						details_characters: result.details === undefined ? 0
							: Array.from(JSON.stringify(result.details)).length,
					}));
				} catch {
					// Diagnostics cannot change a completed tool's result.
				}
			}
			return result;
		},
	});
}

function registerRuntimeTool(pi: ExtensionAPI, name: string, registered: Set<string>) {
	if (registered.has(name)) return;
	registered.add(name);
	registerGoalPlusTool(pi, {
		name,
		label: name,
		description: sharedMcpTools.get(name)?.description ?? RuntimeToolDescriptions[name] ?? `调用 goal-plus facade 工具 ${name}。`,
		parameters: toolParameters(name),
		executionMode: name === "goal_plus_run_work_item" ? "concurrent" : "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			if (role === "main" && name === "goal_plus_status" && !activeGoalPlusId) {
				// A fresh ordinary session has no Goal Plus state to read. Keep an
				// accidental model call local so it cannot probe the workspace .gp tree.
				return inactiveGoalStatusResult();
			}
			const commandCtx = commandContextFrom(ctx);
			const startEntryCount = ctx.sessionManager.getEntries().length;
			const canPersistPiState = canPersistGoalState(ctx);
			let forwardedParams = params as Record<string, unknown>;
			if (role === "main") {
				if (["goal_plus_run_final_check", "goal_plus_run_work_item"].includes(name)) {
					await ensureExecutionScope(pi, ctx, String(forwardedParams.goal_plus_id), currentPiNativeAgentConfig(pi, ctx));
				}
				const runId = stringFrom(forwardedParams.run_id);
				if (runId && ["goal_plus_session_run", "goal_plus_search_select"].includes(name)) {
					const scopeId = await ensureExecutionScope(
						pi,
						ctx,
						runId,
						currentPiNativeAgentConfig(pi, ctx),
					);
					if (name === "goal_plus_search_select") {
						const quiet = await runJsonCli(pi, commandCtx, "host_execution_scope_close_run", {
							scope_id: scopeId,
							run_id: runId,
						});
						if (!isRecord(quiet.details) || quiet.details.ok === false || quiet.details.active_count !== 0) {
							throw new Error(String((isRecord(quiet.details) && quiet.details.error) || `Search run ${runId} still has active workers`));
						}
					}
				}
			}
			const result = isThinkThreadWorker
						? await runThinkThreadWorkerTool(pi, ctx, name, forwardedParams, signal)
					: await runJsonCli(pi, commandCtx, name, forwardedParams, signal);
			if (isThinkThreadWorker && name === "goal_plus_search_get_agent_context" && isRecord(result.details)) {
				thinkThreadSupplementalEnabled = result.details.supplemental_evaluation_enabled === true;
				const active = pi.getActiveTools().filter((tool) => tool !== "goal_plus_search_get_evidence_detail");
				if (thinkThreadSupplementalEnabled) active.push("goal_plus_search_get_evidence_detail");
				pi.setActiveTools(active);
			}
			if (!isThinkThreadWorker && ["goal_plus_search_run_verifier", "goal_plus_search_get_global_evidence", "goal_plus_search_get_agent_context", "goal_plus_search_select", "goal_plus_search_status", "goal_plus_search_invalidate_run", "goal_plus_search_report"].includes(name)) {
				const runId = stringFrom(forwardedParams.run_id)
					?? (isRecord(result.details) ? stringFrom(result.details.run_id) : undefined);
				if (runId) {
					const kicked = await runHostCommandCli(
						pi,
						commandCtx,
						"goal_plus_host_kick_internal_agents",
						{ run_id: runId },
					);
					if (isRecord(kicked.details) && kicked.details.ok === false) {
						throw new Error(String(kicked.details.error || "Pi internal-agent launch failed"));
					}
				}
			}
			if (name === "goal_plus_submit_final_check") {
				activateGoal(pi, result.details, startEntryCount, canPersistPiState);
			}
			if (name === "goal_plus_search_get_agent_context") {
				activateWorkerContext(pi, result.details, registered, true);
			}
			return result;
		},
		renderCall: name === "goal_plus_monitor_snapshot"
			? (args, theme) => {
				const input = isRecord(args) ? args : {};
				const identity = stringFrom(input.goal_plus_id) ?? stringFrom(input.run_id);
				const label = theme.fg("toolTitle", theme.bold("Goal Plus snapshot"));
				return new Text(`${label}${identity ? theme.fg("dim", ` · ${identity}`) : ""}`, 0, 0);
			}
			: undefined,
		renderResult: name === "goal_plus_monitor_snapshot"
			? (result, { expanded, isPartial }, theme) => {
				if (isPartial) return new Text(theme.fg("warning", "Reading snapshot..."), 0, 0);
				const presentation = monitorSnapshotPresentation(result.details);
				if (!presentation) {
					const content = result.content[0];
					const fallback = content?.type === "text" ? content.text.split("\n")[0] : "Snapshot unavailable";
					return new Text(theme.fg("error", fallback), 0, 0);
				}
				const visible = expanded ? presentation.lines : presentation.lines.slice(0, 1);
				const firstColor: "success" | "error" | "accent" = presentation.status === "complete"
					? "success"
					: presentation.status === "blocked" || presentation.status === "abandoned"
						? "error"
						: "accent";
				const rendered = visible.map((line, index) =>
					index === 0 ? theme.fg(firstColor, `✓ ${line}`) : theme.fg("dim", line)
				);
				return new Text(rendered.join("\n"), 0, 0);
			}
			: undefined,
	});
}

function activateWorkerContext(
	pi: ExtensionAPI,
	details: unknown,
	registered: Set<string>,
	persist: boolean,
) {
	if (!isRecord(details) || !isRecord(details.candidate_task)) return;
	const agentSessionId = details.agent_session_id;
	const candidateTask = details.candidate_task;
	const workspaceAccess = details.workspace_access;
	const workspace = typeof candidateTask.workspace === "string"
		? candidateTask.workspace
		: isThinkThreadWorker && isRecord(workspaceAccess)
			? workspaceAccess.logical_root
			: undefined;
	if (
		typeof agentSessionId !== "string" ||
		typeof workspace !== "string" ||
		(process.env.GOAL_PLUS_AGENT_SESSION_ID &&
			process.env.GOAL_PLUS_AGENT_SESSION_ID !== agentSessionId)
	) return;
	const firstActivation = !sawContext;
	workspaceRoot = workspace;
	sawContext = true;
	if (typeof details.run_id === "string" && typeof details.candidate_id === "string"
		&& typeof details.execution_generation === "number") {
		workerGeneration = {
			runId: details.run_id, candidateId: details.candidate_id,
			generation: details.execution_generation,
		};
	}
	for (const tool of [
		"goal_plus_search_get_global_evidence",
		"goal_plus_search_reference_open",
		"goal_plus_search_run_verifier",
		"goal_plus_search_list_iterations",
	]) {
		registerRuntimeTool(pi, tool, registered);
	}
	if (candidateTask.share_out_dir) {
		registerRuntimeTool(pi, "goal_plus_search_stage_shared_tool", registered);
		registerRuntimeTool(pi, "goal_plus_search_copy_shared_tool", registered);
	}
	if (details.supplemental_evaluation_enabled === true) {
		registerRuntimeTool(pi, "goal_plus_search_get_evidence_detail", registered);
	}
	if (persist && firstActivation) {
		pi.appendEntry(WORKER_CONTEXT_ENTRY_TYPE, {
			agent_session_id: agentSessionId,
			run_id: details.run_id,
			candidate_id: details.candidate_id,
			execution_generation: details.execution_generation,
			candidate_task: {
				workspace,
				...(candidateTask.share_out_dir
					? { share_out_dir: candidateTask.share_out_dir }
					: {}),
			},
			...(details.supplemental_evaluation_enabled === true
				? { supplemental_evaluation_enabled: true }
				: {}),
		});
	}
}


function extractCandidatePath(event: ToolCallEvent): string | undefined {
	const input = event.input as Record<string, unknown>;
	if (event.toolName === "bash") return String(input.command || "");
	for (const key of ["path", "file_path", "filePath"]) {
		if (typeof input[key] === "string") return input[key] as string;
	}
	return undefined;
}

function workspaceGuard(event: ToolCallEvent, ctx: ExtensionContext, owner: ToolOwner) {
	if (owner === "goal-plus" && role !== "main" && ["goal_plus_search_recovery_status", "goal_plus_search_prepare_recovery", "goal_plus_search_recover_candidate"].includes(event.toolName)) {
		return { block: true, reason: "Search recovery is Main-owned." };
	}
	if (role === "worker" && workerGeneration && !isThinkThreadWorker) {
		try {
			const record = JSON.parse(readFileSync(join(runtimeRoot, "runs", workerGeneration.runId,
				"candidates", workerGeneration.candidateId, "candidate.json"), "utf8"));
			if (record.execution_generation !== workerGeneration.generation) {
				return { block: true, reason: "StaleAgentSession: Search recovery fenced this worker." };
			}
		} catch {
			return { block: true, reason: "Search worker generation could not be verified." };
		}
	}
	if (owner === "native" && role === "final-checker" && ["edit", "write"].includes(event.toolName)) {
		return { block: true, reason: "最终检查审查员只能进行只读操作。" };
	}
	if (role !== "worker") return undefined;
	if (latestDispatchMetadata(ctx).thinkthreadRole === "annotator") {
		if (owner === "goal-plus" && THINKTHREAD_ANNOTATION_TOOLS.has(event.toolName)) return undefined;
		return {
			block: true,
			reason: `ThinkThread Annotation Child tool is not allowed: ${event.toolName}`,
		};
	}
	if (owner === "goal-plus" && event.toolName === "goal_plus_search_get_agent_context") return undefined;
	if (!sawContext) {
		const readOnly = new Set(["read", "grep", "find", "ls"]);
		if (owner === "native" && readOnly.has(event.toolName)) return undefined;
		return { block: true, reason: "使用变更类工具前调用 goal_plus_search_get_agent_context。" };
	}
	if (!workspaceRoot) return undefined;
	if (owner !== "native" || !["edit", "write", "bash"].includes(event.toolName)) return undefined;
	const target = extractCandidatePath(event);
	if (target && target.includes("..")) {
		return { block: true, reason: "workspaceGuard blocked parent-directory path." };
	}
	if (target && target.startsWith("/") && !target.startsWith(workspaceRoot)) {
		return { block: true, reason: "workspaceGuard blocked access outside candidate workspace." };
	}
	return undefined;
}

async function mainGate(event: ToolCallEvent, ctx: ExtensionContext, owner: ToolOwner) {
	if (role !== "main") return undefined;
	if (owner === "goal-plus" && !activeGoalPlusId && event.toolName !== "goal_plus_status") {
		return {
			block: true,
			reason: "GoalPlusUnavailable: this Pi session is not running a Goal Plus task; Goal Plus tools are unavailable.",
		};
	}
	const requiresAuthorization = requiresGoalPlusAuthorization(event.toolName, owner);
	if (requiresAuthorization && !goalPlusSessionAuthorized) {
		// An interrupt or explicit pause also clears the in-memory
		// authorization flag; distinguish that from a session that was never
		// authorized so the model can relay the real recovery step to the
		// user instead of a misleading entrypoint error.
		if (activeGoalPlusId) {
			const status = await refreshActiveGoal(
				piForGate, commandContextFrom(ctx), false,
			);
			const control = status?.control;
			if (control?.state === "paused") {
				return {
					block: true,
					reason: `GoalPlusResumeRequired: Goal Plus ${activeGoalPlusId} is paused (reason=${control.reason || "explicit_pause"}). This session's Goal Plus authorization was cleared by the interrupt or pause; only the user can resume it by running /goal-plus resume. Read-only status tools remain available.`,
				};
			}
			if (status && isTerminalStatus(status.status)) {
				return {
					block: true,
					reason: `GoalPlusTerminal: Goal Plus ${activeGoalPlusId} is finished (status=${status.status}). It cannot accept new work; start a new task with /goal-plus if needed.`,
				};
			}
		}
		return {
			block: true,
			reason: "GoalPlusEntrypointRequired: this Pi session was not authorized by exact /goal-plus or /goal-plus edit input and is not the resumed native session.",
		};
	}
	if (!requiresAuthorization && !(owner === "native" && MAIN_GATED_TOOLS.has(event.toolName))) return undefined;
	if (!goalPlusSessionAuthorized) return undefined;
	const goalPlusId = activeGoalPlusId;
	if (!goalPlusId) {
		return {
			block: true,
			reason: "GoalPlusEntrypointRequired: no host-authorized Goal Plus record is active for this Pi session.",
		};
	}
	const commandCtx = commandContextFrom(ctx);
	const gate = await runJsonCli(piForGate, commandCtx, "goal_plus_gate", {
		goal_plus_id: goalPlusId,
		event: "pre_tool_use",
		context: {
			tool_name: owner === "goal-plus" ? `mcp__goal_plus__${event.toolName}` : event.toolName,
			input: event.input,
		},
	});
	const details = gateFrom(gate.details);
	if (details?.decision === "block") {
		return { block: true, reason: details.reason || "goal_plus_gate blocked search tool use" };
	}
	return undefined;
}

async function queueMainGoalContinuation(pi: ExtensionAPI, ctx: ExtensionContext) {
	if (role !== "main" || !activeGoalPlusId || !goalPlusSessionAuthorized) return;
	if (controllerOwnsCloseout && lastMainStopReason === "stop") return;
	// agent_end is still inside Pi's awaited prompt lifecycle. The host drains
	// follow-ups queued here after native retry/compaction; agent_settled is too
	// late to start a fire-and-forget turn in print/JSON mode.
	if (lastMainStopReason === "error" || lastMainStopReason === "aborted") return;
	if (ctx.hasPendingMessages()) return;
	const commandCtx = commandContextFrom(ctx);
	const mode = ctx.mode;
	const gate = await runJsonCli(pi, commandCtx, "goal_plus_gate", {
		goal_plus_id: activeGoalPlusId,
		event: "stop",
		context: { mode, continuationCount, stop_prompt_compact: stopPolicyDelivered },
	});
	const details = gateFrom(gate.details);
	if (!details) return;
	if (details.decision === "block") {
		continuationCount += 1;
		// A non-compact request means the gate returned the full audit policy;
		// it is now visible in history until compaction clears it.
		if (!stopPolicyDelivered) stopPolicyDelivered = true;
		if (canPersistGoalState(ctx)) persistGoalState(pi);
		// The runtime owns the continuation text: it carries only the rules that
		// exist nowhere else, while the goal data comes from the per-turn context.
		// The followUp turn itself bypasses before_agent_start in native Pi, so
		// the next prompt-path turn keeps its per-turn reminder unconditionally.
		const content = details.continuation_prompt || details.reason
			|| "Goal Plus 仍处于 active。继续下一项必需 action。";
		appendInjectionObservation(pi, ctx, {
			kind: "must_continue", role, channel: "follow_up", target_id: activeGoalPlusId,
			goal_revision: details.goal_revision, control_version: details.control_version,
			continuation_count: continuationCount, content,
		});
		pi.sendMessage(
			{
				customType: GOAL_PLUS_MUST_CONTINUE_TYPE,
				content,
				display: false,
				details: { goal_plus_id: activeGoalPlusId, continuationCount,
					goal_revision: details.goal_revision, control_version: details.control_version,
					session_id: details.session_id, request_id: randomUUID() },
			},
			{ triggerTurn: true, deliverAs: "followUp" },
		);
	}
}

function notifyPausedGoal(pi: ExtensionAPI, cause: string) {
	if (!activeGoalPlusId) return;
	pi.sendMessage(
		{
			customType: "goal-plus-control",
			content: `Goal Plus ${activeGoalPlusId} 已暂停（${cause}）。继续执行需 /goal-plus resume；Main 仍可读取状态并转述恢复步骤。`,
			display: true,
		},
	);
}

async function settleMainGoal(pi: ExtensionAPI, ctx: ExtensionContext) {
	if (suppressedContinuationAbort) {
		suppressedContinuationAbort = false;
		lastMainStopReason = undefined;
		mainTurnSignal = undefined;
		return;
	}
	if (role !== "main" || !activeGoalPlusId || !goalPlusSessionAuthorized) return;
	const stopReason = lastMainStopReason;
	lastMainStopReason = undefined;
	mainTurnSignal = undefined;
	const persist = canPersistGoalState(ctx);
	if (stopReason === "error") {
		await closeExecutionScope(pi, commandContextFrom(ctx), "pi_execution_lost");
		await runPiHostSessionEvent(pi, ctx, "execution_lost", "next_user_turn", piHostInteraction(ctx));
		goalPlusSessionAuthorized = false;
		if (persist) persistGoalState(pi);
		notifyPausedGoal(pi, "执行丢失");
		await checkSearchProgress(pi, ctx);
		return;
	}
	if (stopReason === "aborted") {
		await closeExecutionScope(pi, commandContextFrom(ctx), "pi_agent_aborted");
		await runPiHostSessionEvent(
			pi, ctx, "interrupt", "next_user_turn", piHostInteraction(ctx),
		);
		goalPlusSessionAuthorized = false;
		if (persist) persistGoalState(pi);
		notifyPausedGoal(pi, "用户中断");
		await checkSearchProgress(pi, ctx);
		return;
	}
	if (ctx.hasPendingMessages()) return;
	const commandCtx = commandContextFrom(ctx);
	const usage = collectGoalUsageFromEntries(ctx.sessionManager.getEntries() as unknown[]);
	const status = await refreshActiveGoal(pi, commandCtx, persist);
	if (!isTerminalStatus(status?.status) && status?.goal_plus_id) {
		// Turn boundary: keep the Search-run surfaces current for the still-running Goal.
		observeSearchProgress(pi, ctx, status, await readMonitorSnapshot(pi, commandCtx, status));
		return;
	}
	if (isTerminalStatus(status?.status)) {
		await closeExecutionScope(pi, commandCtx, "goal_plus_terminal");
		let snapshot: Record<string, unknown> | undefined;
		try {
			const monitor = await runJsonCli(pi, commandCtx, "goal_plus_monitor_snapshot", {
				goal_plus_id: status.goal_plus_id ?? activeGoalPlusId,
				run_id: status.current_search_run_id ?? undefined,
				detail: true,
			});
			if (isRecord(monitor.details) && monitor.details.ok !== false) snapshot = monitor.details;
		} catch {
			// Terminal statistics remain available even when the read-only monitor is unavailable.
		}
		clearPiProgress(ctx);
		appendGoalStats(pi, status, usage, snapshot);
		const finalized = await runJsonCli(pi, commandCtx, "pi_goal_plus_finalize_reports", {
			goal_plus_id: status.goal_plus_id,
			usage,
			started_at: activeGoalStartedAt,
		});
		if (isRecord(finalized.details) && finalized.details.ok === false) {
			ctx.ui.notify(`Goal Plus report finalization failed: ${String(finalized.details.error)}`, "warning");
		}
		const runId = stringFrom(status.current_search_run_id);
		if (runId) {
			const publication = await publicationNotificationContext(pi, commandCtx, runId);
			if (!publication.ok) {
				goalPlusSessionAuthorized = false;
				if (persist) persistGoalState(pi);
				ctx.ui.notify(
					"Goal Plus publication notification delivery remains pending and will retry after restart.",
					"warning",
				);
				return;
			}
			if (publication.content) {
				goalPlusSessionAuthorized = false;
				if (persist) persistGoalState(pi);
				queuePublicationNotificationTurn(pi, ctx, runId, publication.notificationIds);
				return;
			}
		}
		// Retain history binding for summary and an explicit edit of complete.
		goalPlusSessionAuthorized = false;
		if (persist) persistGoalState(pi);
	}
}

let piForGate: ExtensionAPI;

export default async function (pi: ExtensionAPI) {
	piForGate = pi;
	let mcpInstructions: string | undefined;
	let mcpManifestLoaded = false;
	const loadMcpManifest = async (): Promise<void> => {
		if (mcpManifestLoaded) return;
		const manifest = await runJsonCli(pi, { cwd: process.cwd() }, "host_mcp_manifest", {});
		if (!isRecord(manifest.details) || !Array.isArray(manifest.details.tools)) {
			throw new Error("Goal Plus MCP tool discovery failed");
		}
		if (role === "main") {
			if (typeof manifest.details.instructions !== "string" || !manifest.details.instructions.trim()) {
				throw new Error("Goal Plus MCP instructions are missing");
			}
			mcpInstructions = manifest.details.instructions.trim();
		}
		for (const tool of manifest.details.tools) {
			if (!isRecord(tool) || typeof tool.name !== "string" || !isRecord(tool.inputSchema)) {
				throw new Error("Goal Plus MCP returned an invalid tool schema");
			}
			sharedMcpTools.set(tool.name, {
				description: typeof tool.description === "string" ? tool.description : tool.name,
				inputSchema: tool.inputSchema as TSchema,
			});
		}
		mcpManifestLoaded = true;
	};
	// Main tools are a capability of an activated Goal, not of every Pi
	// session. Worker and final-checker processes are already host-launched
	// for a Goal, so their role-scoped tools can be loaded during startup.
	if (role !== "ordinary" && role !== "main") await loadMcpManifest();
	if (typeof pi.registerEntryRenderer === "function") {
		pi.registerEntryRenderer<PiRuntimeStartEntry>(GOAL_PLUS_RUNTIME_ENTRY_TYPE, (entry, { expanded }, theme) => {
			const lines = renderPiRuntimeStartEntry(entry.data, expanded);
			const box = new Box(1, 0, (text) => theme.bg("customMessageBg", text));
			lines.forEach((line, index) => {
				const rendered = index === 0
					? `${theme.fg("accent", "[goal-plus]")} ${theme.fg("accent", line)}`
					: index === 1
						? line
						: theme.fg("dim", line);
				box.addChild(new Text(rendered, 0, 0));
			});
			return box;
		});
	}
	if (typeof pi.registerEntryRenderer === "function") {
		pi.registerEntryRenderer<GoalPlusStatsEntry>(GOAL_PLUS_STATS_ENTRY_TYPE, (entry, { expanded }, theme) => {
			const data = entry.data;
			const lines = (data?.message ?? "Goal Plus stats").split("\n");
			const collapsedLineCount = Math.max(1, data?.collapsedLineCount ?? 2);
			const visibleLines = expanded ? lines : lines.slice(0, collapsedLineCount);
			const box = new Box(1, 0, (text) => theme.bg("customMessageBg", text));
			visibleLines.forEach((line, index) => {
				const statusLine = data?.status === "complete"
					? theme.fg("success", line)
					: data?.status === "blocked"
						? theme.fg("error", line)
						: theme.fg("accent", line);
				const rendered = index === 0
					? `${theme.fg("accent", "[goal-plus]")} ${statusLine}`
					: index < collapsedLineCount
						? line
						: theme.fg("dim", line);
				box.addChild(new Text(rendered, 0, 0));
			});
			return box;
		});
	}
	// BEGIN GENERATED GOAL PLUS COMMAND CONFIG
	const GOAL_PLUS_COMMAND_CONFIG_USAGE = "[mode=autonomous|probe] [max_parallel=正整数] [max_duration_seconds=正整数（秒）] [workspace_provider=copy|git_worktree|thinkthread] [promotion_mode=apply|artifact_only] [strategy=NAME] [inner_agent=ADAPTER_ID] [annotator=MODEL] [workers|models=MODEL[,MODEL...] 或 MODEL*COUNT[,MODEL*COUNT...]]";
	// END GENERATED GOAL PLUS COMMAND CONFIG
	if (!isPrintLikeInvocation) {
		pi.registerCommand("goal-plus", {
			description: "运行或编辑原生 Pi Goal Plus（支持 typed command config）",
			handler: async (args, ctx) => {
				const request = goalPlusRequestFromSlashInput(`/goal-plus ${args}`);
				if (request?.action === "invalid") {
					ctx.ui.notify(request.invalidReason ?? "Invalid Goal Plus command", "error");
					return;
				}
				if (request && ["summary", "pause", "resume", "clear"].includes(request.action)) {
					const prompt = await controlGoalPlus(pi, ctx, request.action as "summary" | "pause" | "resume" | "clear");
					if (prompt) startGoalPlusMainTurn(pi, ctx, "native_continuation", prompt, false);
					return;
				}
				if (!request || !request.rawGoal) {
					ctx.ui.notify(`Usage: /goal-plus ${GOAL_PLUS_COMMAND_CONFIG_USAGE} <goal>, or /goal-plus edit [config] <full revised goal>`, "error");
					return;
				}
				const deliverAsFollowUp = !ctx.isIdle();
				const prompt = request.action === "edit"
					? await updateGoalPlusStart(pi, ctx, request.rawGoal)
					: await createGoalPlusStart(pi, ctx, request.rawGoal);
				if (prompt) startGoalPlusMainTurn(pi, ctx, request.action === "edit" ? "goal_edit" : "goal_start", prompt, deliverAsFollowUp);
			},
		});
		pi.registerCommand("goal-plus-with-final-check", {
			description: "运行原生 Pi Goal Plus，并要求独立最终检查",
			handler: async (args, ctx) => {
				const rawGoal = args.trim();
				if (!rawGoal) {
					ctx.ui.notify("Usage: /goal-plus-with-final-check [typed-config] <goal>", "error");
					return;
				}
				const prompt = await createGoalPlusStart(pi, ctx, rawGoal, true);
				if (prompt) startGoalPlusMainTurn(pi, ctx, "goal_start", prompt, !ctx.isIdle());
			},
		});
	}

	const mainTools = [
		"goal_plus_get_tool_schema",
		"goal_plus_session_run",
		"goal_plus_session_wait_any",
		"goal_plus_session_message",
		"goal_plus_session_interrupt",
		"goal_plus_session_stop",
		"goal_plus_session_close",
		"goal_plus_status",
		"goal_plus_history",
		"goal_plus_monitor_snapshot",
		"goal_plus_list_models",
		"goal_plus_record_triage",
		"goal_plus_record_work_event",
		"goal_plus_record_search_result",
		"goal_plus_run_final_check",
		"goal_plus_set_status",
		"goal_plus_run_work_item",
		"goal_plus_search_freeze_spec",
		"goal_plus_search_create",
		"goal_plus_search_status",
		"goal_plus_search_recovery_status",
		"goal_plus_search_prepare_recovery",
		"goal_plus_search_recover_candidate",
		"goal_plus_search_invalidate_run",
		"goal_plus_search_list_history",
		"goal_plus_search_plan_next",
		"goal_plus_search_start_batch",
		"goal_plus_search_get_agent_observability",
		"goal_plus_search_run_verifier",
		"goal_plus_search_list_allocation_decisions",
		"goal_plus_search_apply_allocation_decision",
		"goal_plus_search_select",
		"goal_plus_search_report",
		"goal_plus_search_promote",
		"goal_plus_search_apply_promotion",
	];
	const workerTools = ["goal_plus_search_get_agent_context"];
	const thinkThreadWorkerTools = [
		"goal_plus_search_get_agent_context",
		"goal_plus_search_get_global_evidence",
		"goal_plus_search_reference_open",
		"goal_plus_search_stage_shared_tool",
		"goal_plus_search_copy_shared_tool",
		"goal_plus_search_get_evidence_detail",
		"goal_plus_search_run_verifier",
		"goal_plus_search_list_iterations",
	];
	const thinkThreadAnnotationTools = [...THINKTHREAD_ANNOTATION_TOOL_NAMES];
	const finalCheckerTools = ["goal_plus_status", "goal_plus_submit_final_check"];
	const roleTools = role === "worker"
		? isThinkThreadWorker
			? [
				...thinkThreadWorkerTools.filter((tool) => ![
					"goal_plus_search_reference_open",
					"goal_plus_search_stage_shared_tool",
					"goal_plus_search_copy_shared_tool",
				].includes(tool)),
				...thinkThreadAnnotationTools,
			]
			: workerTools
		: role === "final-checker"
			? finalCheckerTools
			: role === "ordinary"
				? []
				: mainTools;
	const registeredRuntimeTools = new Set<string>();
	const visibleRoleTools = controllerOwnsCloseout
		? roleTools.filter((tool) => !CONTROLLER_ONLY_CLOSEOUT_TOOLS.has(tool))
		: roleTools;
	mainGoalPlusToolNames = role === "main" ? [...visibleRoleTools] : [];
	let mainToolsRegistered = false;
	const ensureMainToolsRegistered = async (): Promise<void> => {
		if (role !== "main" || mainToolsRegistered) return;
		await loadMcpManifest();
		for (const tool of visibleRoleTools) registerRuntimeTool(pi, tool, registeredRuntimeTools);
		mainToolsRegistered = true;
		setMainGoalPlusToolAvailability(pi, Boolean(activeGoalPlusId));
	};
	ensureMainGoalPlusTools = ensureMainToolsRegistered;
	if (role !== "main") {
		for (const tool of visibleRoleTools) {
			registerRuntimeTool(pi, tool, registeredRuntimeTools);
		}
	}
	pi.on("input", async (event, ctx) => {
		scopeThinkThreadWorkerTools(pi, ctx, event.text);
		if (role !== "main" || event.source === "extension") {
			return { action: "continue" };
		}
		const request = goalPlusRequestFromSlashInput(event.text);
		if (request === undefined) {
			if (activeGoalPlusId) {
				const interaction = piHostInteraction(ctx);
				const decision = await runPiHostSessionEvent(
					pi,
					ctx,
					"user_turn",
					"next_user_turn",
					interaction,
				);
				goalPlusSessionAuthorized = decision?.decision === "continue_after_user";
				if (goalPlusSessionAuthorized) {
					const status = await refreshActiveGoal(pi, commandContextFrom(ctx), canPersistGoalState(ctx));
					if (status) activateGoal(pi, status, undefined, canPersistGoalState(ctx));
				}
			}
			return { action: "continue" };
		}
		if (ctx.mode !== "print" && ctx.mode !== "json") return { action: "continue" };
		if (request.action === "invalid") {
			ctx.ui.notify(request.invalidReason ?? "Invalid Goal Plus command", "error");
			return { action: "handled" };
		}
		if (["summary", "pause", "resume", "clear"].includes(request.action)) {
			const prompt = await controlGoalPlus(pi, ctx, request.action as "summary" | "pause" | "resume" | "clear");
			if (prompt) appendInjectionObservation(pi, ctx, {
				kind: "native_continuation", role, channel: "input_transform",
				target_id: activeGoalPlusId, content: prompt,
			});
			return prompt ? { action: "transform", text: prompt, images: event.images } : { action: "handled" };
		}
		if (!request.rawGoal) {
			ctx.ui.notify("Goal Plus command requires a goal", "error");
			return { action: "handled" };
		}
		const prompt = request.action === "edit"
			? await updateGoalPlusStart(pi, ctx, request.rawGoal)
			: await createGoalPlusStart(pi, ctx, request.rawGoal, request.withFinalCheck);
		if (prompt) appendInjectionObservation(pi, ctx, {
			kind: request.action === "edit" ? "goal_edit" : "goal_start", role,
			channel: "input_transform", target_id: activeGoalPlusId, content: prompt,
		});
		return prompt
			? { action: "transform", text: prompt, images: event.images }
			: { action: "handled" };
	});
	pi.on("tool_call", async (event, ctx) => {
		const owner = toolOwner(pi.getAllTools(), event.toolName, extensionPath);
		if (owner === "goal-plus") return undefined; // Gated by its registration closure.
		return workspaceGuard(event, ctx, owner) || (await mainGate(event, ctx, owner));
	});
	pi.on("session_start", async (event, ctx) => {
		await validateThinkThreadWorker(ctx);
		goalPlusSessionAuthorized = false;
		lastMainStopReason = undefined;
		mainTurnSignal = undefined;
		mainStartPending = false;
		activeGoalPlusId = process.env.GOAL_PLUS_ID;
		cachedGoalStatus = undefined;
		if (role !== "main" && event.reason !== "reload") {
			appendPiRuntimeStartEntry(pi, ctx);
		}
		if (role === "worker") {
			const entry = ctx.sessionManager.getEntries()
				.filter((item: { type: string; customType?: string }) =>
					item.type === "custom" && item.customType === WORKER_CONTEXT_ENTRY_TYPE)
				.pop() as { data?: unknown } | undefined;
			activateWorkerContext(pi, entry?.data, registeredRuntimeTools, false);
			return;
		}
		restoreGoalState(ctx);
		if (activeGoalPlusId) await ensureMainToolsRegistered();
		setMainGoalPlusToolAvailability(pi, Boolean(activeGoalPlusId));
		if (role !== "main" || !activeGoalPlusId) return;
		const commandCtx = commandContextFrom(ctx);
		const persist = canPersistGoalState(ctx);
			try {
				const status = await refreshActiveGoal(pi, commandCtx, persist);
				if (isTerminalStatus(status?.status)) {
					const runId = stringFrom(status?.current_search_run_id);
				if (runId) {
					const publication = await publicationNotificationContext(pi, commandCtx, runId);
					if (!publication.ok) {
						ctx.ui.notify("Goal Plus publication notification recovery will retry at the next turn boundary.", "warning");
						return;
					}
					if (publication.content) {
						if (persist) persistGoalState(pi);
						queuePublicationNotificationTurn(pi, ctx, runId, publication.notificationIds);
						return;
						}
					}
					if (persist) persistGoalState(pi);
					return;
				}
			if (!status?.goal_plus_id) return;
			observeSearchProgress(pi, ctx, status, await readMonitorSnapshot(pi, commandCtx, status));
			const interaction = piHostInteraction(ctx);
			await runPiHostSessionEvent(
				pi,
				ctx,
				"session_start",
				"next_user_turn",
				interaction,
			);
		} catch {
			// Keep startup non-fatal; the next explicit tool call will surface runtime errors.
		}
	});
	pi.on("context", async (event, ctx) => {
		if (role !== "main" || !activeGoalPlusId) return;
		const messages = event.messages;
		const isRequest = (message: unknown) => isRecord(message) && message.role === "custom"
			&& [GOAL_PLUS_COMMAND_CONTEXT_TYPE, GOAL_PLUS_MUST_CONTINUE_TYPE].includes(String(message.customType));
		// Only cancel a stale, not-yet-run automatic request. Historical requests
		// must never abort an ordinary user turn or an already running Main turn.
		const lastInput = [...messages].reverse().find((message) => message.role === "user" || message.role === "assistant" || isRequest(message));
		// This hook fires before every LLM call; a status round-trip is worth it
		// only when a Goal Plus request is about to be consumed. Pruning
		// historical requests reuses the turn's cached status.
		const status = lastInput !== undefined && isRequest(lastInput)
			? await refreshActiveGoal(pi, commandContextFrom(ctx), false)
			: cachedGoalStatus;
		const stale = (message: unknown) => {
			if (!isRequest(message) || !isRecord(message) || !isRecord(message.details)) return false;
			const d = message.details;
			return !status || status.control?.state !== "active" || isTerminalStatus(status.status)
				|| d.goal_plus_id !== status.goal_plus_id || d.goal_revision !== status.goal_revision
				|| d.control_version !== status.control?.version
				|| (d.session_id !== undefined && d.session_id !== ctx.sessionManager.getSessionId());
		};
		if (lastInput && stale(lastInput)) {
			const pruned = messages.filter((message) => stale(message)).length;
			appendInjectionObservation(pi, ctx, {
				kind: "context_prune", role, channel: "context_filter",
				target_id: status?.goal_plus_id,
				pruned,
				reason: "stale_request_abort",
			});
			suppressedContinuationAbort = true;
			ctx.abort();
		}
		return { messages: messages.filter((message) => !stale(message)) };
	});
	pi.on("before_agent_start", async (event, ctx) => {
		try {
			scopeThinkThreadWorkerTools(pi, ctx, event.prompt);
			await acknowledgeThinkThreadDispatch(pi, ctx, event.prompt);
		} catch (error) {
			try {
				await reportThinkThreadDispatchError(ctx, event.prompt, error);
			} catch {
				// Preserve the original startup error when diagnostic delivery also fails.
			}
			throw error;
		}
		if (role === "main") mainStartPending = false;
		if (role !== "main") return;
		if (!activeGoalPlusId) return { systemPrompt: event.systemPrompt };
		await ensureMainToolsRegistered();
		const systemPrompt = mcpInstructions
			? `${event.systemPrompt}\n\n${mcpInstructions}`
			: event.systemPrompt;
		const commandCtx = commandContextFrom(ctx);
		const persist = canPersistGoalState(ctx);
		const status = await refreshActiveGoal(pi, commandCtx, persist);
		const runId = stringFrom(status?.current_search_run_id);
		const publication: PublicationNotificationPoll = runId
			? await publicationNotificationContext(pi, commandCtx, runId, true)
			: { ok: true, notificationIds: [] };
		if (publication.content && persist) persistGoalState(pi);
		const goalContextKey = status
			? `${status.goal_plus_id ?? ""}:${status.goal_revision ?? 0}:${status.control?.version ?? 0}`
			: undefined;
		const needsFullGoalContext = goalContextKey !== undefined && goalContextFullKey !== goalContextKey;
		// Full contexts are rare (first turn, post-compaction, revision/control
		// change); only they pay the detail fetch that carries the raw goal, so
		// ordinary turns keep the cheap status refresh and no goal tokens.
		const fullStatus = needsFullGoalContext
			? (await refreshActiveGoal(pi, commandCtx, persist, true)) ?? status
			: status;
		const goalContext = goalPlusSessionAuthorized && status && !isTerminalStatus(status.status)
			&& status.control?.state === "active"
			? needsFullGoalContext
				? buildGoalPlusContext(fullStatus)
				: buildGoalPlusBriefContext(status)
			: undefined;
		if (goalContext && needsFullGoalContext) goalContextFullKey = goalContextKey;
		// The injection ledger is the deterministic guardian of this cadence: the
		// audit turns a full context without raw_goal into a reported defect
		// instead of a silent prompt regression.
		appendInjectionObservation(pi, ctx, {
			kind: goalContext === undefined
				? "goal_context_skipped"
				: needsFullGoalContext ? "goal_context_full" : "goal_context_brief",
			role, channel: "system_message", target_id: status?.goal_plus_id,
			goal_revision: status?.goal_revision, control_version: status?.control?.version,
			raw_goal_present: Boolean(fullStatus?.raw_goal),
			needs_full: needsFullGoalContext,
			...(goalContext !== undefined
				? { content: goalContext }
				: { reason: !goalPlusSessionAuthorized ? "not_authorized"
					: !status ? "no_status"
					: isTerminalStatus(status.status) ? "terminal"
					: "control_inactive" }),
		});
		const content = [publication.content, goalContext].filter((item): item is string => Boolean(item));
		if (content.length === 0) return { systemPrompt };
		return {
			systemPrompt,
			message: {
				customType: "goal-plus-native-context",
				content: content.join("\n\n"),
				display: false,
				details: {
					goal_plus_id: status?.goal_plus_id,
					phase: status?.phase,
					status: status?.status,
					publication_notification: Boolean(publication.content),
				},
			},
		};
	});
	registerSessionDiagnostics(pi, diagnosticToolContracts);
	pi.on("turn_start", (event, ctx) => {
		if (role !== "main" || !sessionDiagnosticsEnabled() || !mcpInstructions) return;
		// Observe the host's final prompt after all before_agent_start handlers.
		// Custom entries stay in native history but do not enter model context.
		try {
		const expected = mcpInstructions ?? "";
		pi.appendEntry("goal-plus-instructions-observation", {
			version: 1,
			session_id: ctx.sessionManager.getSessionId(),
			turn_index: event.turnIndex,
			expected_sha256: createHash("sha256").update(expected).digest("hex"),
			overview_present: expected.length > 0 && ctx.getSystemPrompt().includes(expected),
		});
		} catch { /* Optional observations cannot prevent inference. */ }
	});
	pi.on("agent_start", async (_event, ctx) => {
		if (role === "main") mainTurnSignal = ctx.signal;
		stopWorkerSteering?.();
		if (!isThinkThreadWorker || !workerSteeringIdentity) return;
		const client = await workerAgentPosixClient();
		const parentId = process.env.THINKTHREAD_PARENT_ID!;
		const receiver = new WorkerSteeringReceiver(client, parentId, workerSteeringIdentity,
			() => !ctx.isIdle() && !ctx.signal?.aborted,
			(prompt) => {
				appendInjectionObservation(pi, ctx, {
					kind: "worker_steer", role, channel: "steer",
					target_id: workerSteeringIdentity.agentSessionId, content: prompt,
				});
				pi.sendUserMessage(prompt, { deliverAs: "steer" });
			}, () => ctx.abort());
		let stopped = false;
		let timer: ReturnType<typeof setTimeout> | undefined;
		const poll = async () => {
			try { await receiver.poll(); } catch {
				// A lost acknowledgement is retried from the retained Message page.
			} finally {
				if (!stopped) timer = setTimeout(poll, 100);
			}
		};
		stopWorkerSteering = () => { stopped = true; receiver.close(); clearTimeout(timer); };
		void poll();
	});
	pi.on("agent_end", async (event, ctx) => {
		stopWorkerSteering?.();
		await sessionArchive.flush(ctx, { root: runtimeRoot, role, sourceRoot: sourceRoot(commandContextFrom(ctx)),
			checkpoint: data => pi.appendEntry("goal-plus-session-archive-checkpoint", data),
			...(isThinkThreadWorker ? { worker: async () => {
				const metadata = latestDispatchMetadata(ctx);
				const parentId = process.env.THINKTHREAD_PARENT_ID;
				if (!parentId || !metadata.agentSessionId) {
					throw new Error("ThinkThread diagnostic parent binding mismatch");
				}
				return { agentSessionId: metadata.agentSessionId,
					send: (messages: Record<string, unknown>[]) => sendDiagnosticMessages(parentId, messages) };
			} } : {}),
		});
		const lastMessage = event.messages.at(-1);
		const persist = canPersistGoalState(ctx);
		if (role === "main" && presentedPublicationNotifications.size > 0) {
			const acknowledged = await acknowledgeStagedPublicationNotifications(
				pi,
				commandContextFrom(ctx),
				persist,
			);
			if (
				acknowledged
				&& stagedPublicationNotifications.length === 0
				&& isTerminalStatus(cachedGoalStatus?.status)
			) {
				goalPlusSessionAuthorized = false;
				if (persist) persistGoalState(pi);
				return;
			}
		}
		const lengthWithoutToolCall =
			lastMessage?.role === "assistant" &&
			lastMessage.stopReason === "length" &&
			countToolCalls(lastMessage.content) === 0;
		if (role === "main") {
			// Providers may serialize a user abort as an error. The native abort
			// signal, not provider wording, is the authoritative lifecycle fact.
			lastMainStopReason = mainTurnSignal?.aborted || ctx.signal?.aborted
				? "aborted"
				: lastMessage?.role === "assistant" ? lastMessage.stopReason : undefined;
			await queueMainGoalContinuation(pi, ctx);
			return;
		}
		if (lastMessage?.role === "assistant" && lastMessage.stopReason === "error") {
			// Pi may retry after a transient provider error without a new user message.
			// The exact host command authorizes this native session, not one model turn.
			return;
		}
		if (lastMessage?.role === "assistant" && lastMessage.stopReason === "aborted") {
			if (role === "main") {
				await closeExecutionScope(pi, commandContextFrom(ctx), "pi_agent_aborted");
			}
			return;
		}
		if (ctx.hasPendingMessages()) return;
		if (role === "worker") {
			if (lengthWithoutToolCall) return;
			// A worker turn that ends without any accepted attempt gets one short
			// "submit if you have a shaped plan" nudge - for every provider, not
			// only ThinkThread - before the wake is allowed to end. One-shot per
			// dispatch; submitting is still the model's judgment.
			if (
				latestDispatchMetadata(ctx).thinkthreadRole !== "annotator"
				&& !workerVerifierSubmitted
				&& !workerMissingAttemptContinuationSent
			) {
				workerMissingAttemptContinuationSent = true;
				const missingAttemptContent = "本次 dispatch 尚未提交 accepted verifier attempt。若有成型方案，建议立即提交验证：如尚未加载 candidate_task，先调用 goal_plus_search_get_agent_context；随后完成修改，通过 goal_plus_search_run_verifier 提交一个 attempt，并按回执行动。";
				appendInjectionObservation(pi, ctx, {
					kind: "missing_attempt_nudge", role, channel: "follow_up",
					target_id: workerSteeringIdentity?.agentSessionId,
					dispatch_nonce: workerActiveDispatchNonce, content: missingAttemptContent,
				});
				pi.sendMessage(
					{
						customType: "goal-plus-missing-attempt",
						content: missingAttemptContent,
						display: false,
						details: { dispatchNonce: workerActiveDispatchNonce },
					},
					{ triggerTurn: true, deliverAs: "followUp" },
				);
				return;
			}
		}
	});
	pi.on("agent_settled", async (_event, ctx) => {
		await settleMainGoal(pi, ctx);
	});
	// Compaction rebuilds the model context, so the next turn re-injects the full
	// Goal context instead of the compact status line, and the next stop
	// re-injects the full audit policy instead of the compact pointer.
	pi.on("session_before_compact", () => {
		goalContextFullKey = undefined;
		stopPolicyDelivered = false;
	});
	pi.on("session_compact", () => {
		goalContextFullKey = undefined;
		stopPolicyDelivered = false;
	});
	pi.on("session_shutdown", async (event, ctx) => {
		stopWorkerSteering?.();
		stopSearchProgressPoll();
		cancelSearchProgressHide();
		try {
			if (role === "main") await closeExecutionScope(pi, commandContextFrom(ctx), `pi_session_shutdown:${event.reason}`);
		} finally {
			await closeMcpConnections();
		}
	});
}
