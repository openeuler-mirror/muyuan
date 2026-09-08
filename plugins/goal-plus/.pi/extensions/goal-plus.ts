import { LooseObject, RuntimeToolSchemas, RuntimeToolDescriptions } from "./lib/goal-plus-schemas.ts";
import { pythonExecutable, runtimeRoot, type CommandRuntimeContext, isRecord, sourceRoot, projectModuleInvocation, parseJsonObject, commandFailure, runJsonCli, runHostCommandCli } from "./lib/goal-plus-transport.ts";
import { THINKTHREAD_GOAL_PLUS_PROTOCOL, requestWorkerTool } from "./lib/goal-plus-thinkthread-rpc.ts";
import { toolOwner, type ToolOwner } from "./lib/goal-plus-tool-identity.ts";
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
	PI_COMPLETION_PREVIEW,
	PI_PROGRESS_PREVIEWS,
	type GoalPlusCandidateCompletion,
	type GoalPlusCompletionView,
	type GoalPlusProgressView,
	type GoalPlusUsageSummary,
} from "./lib/goal-plus-progress.ts";

const role = process.env.GOAL_PLUS_PI_ROLE || "main";
const extensionPath = fileURLToPath(import.meta.url);
const isThinkThreadProfile = Boolean(process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY);
const isThinkThreadWorker = role === "worker" && isThinkThreadProfile;



const workerContinueUntilMs = Number(process.env.GOAL_PLUS_PI_WORKER_CONTINUE_UNTIL_MS || "0");
const progressPreviewMode = (() => {
	const value = process.env.GOAL_PLUS_UI_PREVIEW?.trim().toLowerCase();
	if (!value) return undefined;
	if (value === "1" || value === "running") return "active";
	if (value === "active" || value === "complete") return value;
	return undefined;
})();
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
const GOAL_PLUS_PROGRESS_STATUS_KEY = "goal-plus-progress";
const GOAL_PLUS_PROGRESS_WIDGET_KEY = "goal-plus-progress";
let workspaceRoot: string | undefined;
let sawContext = false;
let workerGeneration: { runId: string; candidateId: string; generation: number } | undefined;
let activeGoalPlusId = process.env.GOAL_PLUS_ID;
let cachedGoalStatus: GoalPlusStatusPayload | undefined;
let continuationCount = 0;
let workerContinuationCount = 0;
let workerAllocationDecisionId: string | undefined;
let workerRegistrationSent = false;
let workerSdkModulePromise: Promise<any> | undefined;
let workerSdkClientPromise: Promise<any> | undefined;
const workerAcknowledgedDispatches = new Set<string>();
let workerRpcTail: Promise<void> = Promise.resolve();
let workerActiveDispatchNonce: string | undefined;
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
	"thinkthread_annotation_next",
	"thinkthread_artifact_view_list",
	"thinkthread_artifact_view_stat",
	"thinkthread_artifact_view_pread",
	"thinkthread_artifact_view_diff",
	"thinkthread_annotation_submit",
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
	"search_recovery_status",
	"goal_plus_status",
	"goal_plus_monitor_snapshot",
	"goal_plus_list_models",
	"search_status",
	"search_list_history",
	"search_list_iterations",
	"search_get_agent_observability",
]);

function requiresGoalPlusAuthorization(toolName: string, owner: ToolOwner): boolean {
	return owner === "goal-plus" && !MAIN_READ_ONLY_RUNTIME_TOOLS.has(toolName);
}

interface GoalPlusNativeState {
	activeGoalPlusId?: string;
	continuationCount?: number;
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
	host: "pi" | "pi-rpc";
	mode: string;
	project_trusted: boolean;
	active_tools: string[];
	active_builtin_tools: string[];
	active_non_builtin_tools: string[];
	pi_sandbox: "none";
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
	return { cwd: ctx.cwd };
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
		host: role === "main" ? "pi" : "pi-rpc",
		mode: ctx.mode,
		project_trusted: ctx.isProjectTrusted(),
		active_tools: activeTools,
		active_builtin_tools: activeBuiltinTools,
		active_non_builtin_tools: activeNonBuiltinTools,
		pi_sandbox: "none",
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
		`Pi start · agent=${entry.agent} · host=${entry.host} · mode=${entry.mode} · project_trusted=${trust} · sandbox=${entry.pi_sandbox} · builtin_approval=${entry.pi_builtin_tool_approval}`,
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
	return RuntimeToolSchemas[name] ?? LooseObject;
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
	ctx: CommandRuntimeContext,
	runId: string,
	nativeAgentConfig: PiNativeAgentConfig,
): Promise<string> {
	if (!activeGoalPlusId || !cachedGoalStatus?.active_session?.session_id) {
		throw new Error("Execution scope requires the active Goal Plus native session");
	}
	const result = await runJsonCli(pi, ctx, "host_execution_scope_open", {
		host: "pi",
		goal_plus_id: activeGoalPlusId,
		native_session_id: cachedGoalStatus.active_session.session_id,
		owner_session_id: `${activeGoalPlusId}:${ctx.cwd}`,
		owner_pid: process.pid,
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
	if (
		sdk.CONTROL_PROTOCOL_VERSION !== AGENT_POSIX_CONTROL_PROTOCOL_VERSION ||
		sdk.CONTRACT_FINGERPRINT !== AGENT_POSIX_CONTRACT_FINGERPRINT
	) {
		throw new Error(
			`unsupported ThinkThread Agent POSIX SDK contract: protocol=${String(sdk.CONTROL_PROTOCOL_VERSION)}, fingerprint=${String(sdk.CONTRACT_FINGERPRINT)}`,
		);
	}
	if (!workerSdkClientPromise) {
		workerSdkClientPromise = Promise.resolve(sdk.AgentPosixClient.fromEnv());
	}
	return workerSdkClientPromise;
}

function latestDispatchMetadata(ctx: ExtensionContext, dispatchText?: string): {
	registrationNonce?: string;
	dispatchNonce?: string;
	agentSessionId?: string;
	thinkthreadRole?: string;
} {
	const serialized = JSON.stringify(ctx.sessionManager.getEntries());
	const registration = /registration_nonce=([0-9a-f-]{36})/i.exec(serialized)?.[1];
	const currentDispatches = [
		...(dispatchText ?? "").matchAll(/dispatch_nonce=([0-9a-f-]{36})/gi),
	];
	const sessionDispatches = [...serialized.matchAll(/dispatch_nonce=([0-9a-f-]{36})/gi)];
	return {
		registrationNonce: registration,
		dispatchNonce: currentDispatches.at(-1)?.[1] ?? sessionDispatches.at(-1)?.[1],
		agentSessionId: /agent_session_id=([A-Za-z0-9._-]+)/.exec(serialized)?.[1],
		thinkthreadRole: /goal_plus_thinkthread_role=([A-Za-z0-9._-]+)/.exec(serialized)?.[1],
	};
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
		"search_get_agent_context",
		"search_get_global_evidence",
		"search_get_evidence_detail",
		"search_run_verifier",
		"search_list_iterations",
		"thinkthread_annotation_next",
		"thinkthread_artifact_view_list",
		"thinkthread_artifact_view_stat",
		"thinkthread_artifact_view_pread",
		"thinkthread_artifact_view_diff",
		"thinkthread_annotation_submit",
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
	if (name === "search_run_verifier" && workerInnerGitBinding) {
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
	if (name === "search_run_verifier") workerVerifierSubmitted = true;
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
		pi.getActiveTools().filter((name) => !THINKTHREAD_ANNOTATION_TOOLS.has(name)),
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
		await client.invoke("message.send", {
			recipientThinkthreadId: parentId,
			text: JSON.stringify({
				protocol: THINKTHREAD_GOAL_PLUS_PROTOCOL,
				type: "dispatch_ack",
				dispatch_nonce: metadata.dispatchNonce,
			}),
			wake: false,
		});
		workerAcknowledgedDispatches.add(metadata.dispatchNonce);
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
		host: "pi",
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
	}
	activeGoalPlusId = id;
	cachedGoalStatus = statusFrom(details);
	if (persist) persistGoalState(pi);
}

async function refreshActiveGoal(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	persist = true,
): Promise<GoalPlusStatusPayload | undefined> {
	if (!activeGoalPlusId) return undefined;
	const result = await runJsonCli(pi, ctx, "goal_plus_status", { goal_plus_id: activeGoalPlusId });
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
	stagedPublicationNotifications.splice(0);
	presentedPublicationNotifications.clear();
	cachedGoalStatus = undefined;
	goalPlusSessionAuthorized = false;
	if (persist) persistGoalState(pi);
}

function queuePublicationNotificationTurn(
	pi: ExtensionAPI,
	runId: string,
	notificationIds: string[],
): void {
	pi.sendMessage(
		{
			customType: "goal-plus-publication-notification-delivery",
			content: "处理本轮 native context 中的 Goal Plus publication 终态通知。不要恢复 Goal Plus，也不要调用 mutation tool。",
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

function candidateCompletionsFromMonitor(
	snapshot: Record<string, unknown> | undefined,
	selectedCandidateId: string | undefined,
): GoalPlusCandidateCompletion[] {
	if (!snapshot || !isRecord(snapshot.candidates)) return [];
	const modelByCandidate = new Map<string, string>();
	if (Array.isArray(snapshot.subagents)) {
		for (const subagent of snapshot.subagents.filter(isRecord)) {
			const candidateId = stringFrom(subagent.candidate_id);
			const observability = isRecord(subagent.observability) ? subagent.observability : undefined;
			const execution = observability && isRecord(observability.execution) ? observability.execution : undefined;
			const model = stringFrom(execution?.model);
			if (candidateId && model) modelByCandidate.set(candidateId, model);
		}
	}
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
	const linked = goal && isRecord(goal.linked_search)
		? goal.linked_search
		: isRecord(status.linked_search)
			? status.linked_search
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

function appendGoalCompletionPreview(pi: ExtensionAPI): void {
	const presentation = buildPiCompletionPresentation(PI_COMPLETION_PREVIEW);
	pi.appendEntry<GoalPlusStatsEntry>(GOAL_PLUS_STATS_ENTRY_TYPE, {
		goal_plus_id: PI_COMPLETION_PREVIEW.goalPlusId,
		status: PI_COMPLETION_PREVIEW.status,
		startedAt: undefined,
		endedAt: new Date().toISOString(),
		usage: {
			assistantMessages: PI_COMPLETION_PREVIEW.mainUsage.assistantMessages,
			toolCalls: PI_COMPLETION_PREVIEW.mainUsage.toolCalls,
			input: 0,
			output: PI_COMPLETION_PREVIEW.mainUsage.processedTokens,
			cacheRead: 0,
			cacheWrite: 0,
			cost: PI_COMPLETION_PREVIEW.mainUsage.costUsd,
		},
		message: presentation.lines.join("\n"),
		collapsedLineCount: presentation.collapsedLineCount,
	});
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
	const lines = presentation.lines.map((line, index) => {
		if (index === 0) return ctx.ui.theme.fg(view.state === "complete" ? "success" : "accent", line);
		if (line.startsWith("Next  ")) return ctx.ui.theme.fg("muted", line);
		return line;
	});
	const statusColor = view.state === "complete" ? "success" : view.state === "blocked" ? "error" : "accent";
	ctx.ui.setStatus(GOAL_PLUS_PROGRESS_STATUS_KEY, ctx.ui.theme.fg(statusColor, presentation.status));
	ctx.ui.setWidget(GOAL_PLUS_PROGRESS_WIDGET_KEY, lines);
}

function clearPiProgress(ctx: ExtensionContext): void {
	ctx.ui.setStatus(GOAL_PLUS_PROGRESS_STATUS_KEY, undefined);
	ctx.ui.setWidget(GOAL_PLUS_PROGRESS_WIDGET_KEY, undefined);
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
		`final_check_policy: ${JSON.stringify(status.policy?.final_check ?? { mode: "disabled" })}`,
		"",
			"原始目标：",
		status.raw_goal ?? "",
		"",
		"加载 goal-plus skill；每轮读取 goal_plus_status 的 control、next_action 和 reconciliation。",
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

async function probePiModels(
	ctx: ExtensionContext,
	models: PiModel[],
): Promise<void> {
	const unique = new Map<string, PiModel>();
	for (const model of models) {
		unique.set(piModelKey(model), model);
	}
	if (unique.size === 0) return;
	const failures = await Promise.all([...unique.values()].map(async (model) => {
		const reference = `${model.provider}/${model.id}`;
		const controller = new AbortController();
		const timeout = setTimeout(() => controller.abort(), 25_000);
		try {
			const response = await ctx.modelRegistry.complete(
				model,
				{
					systemPrompt: "Reply briefly. This is a connectivity preflight.",
					messages: [{ role: "user", content: "hi", timestamp: Date.now() }],
				},
				{
					signal: controller.signal,
					maxTokens: 16,
					maxRetries: 0,
					timeoutMs: 25_000,
				},
			);
			if (response.stopReason === "error" || response.stopReason === "aborted") {
				return `${reference}: ${response.stopReason}`;
			}
			return undefined;
		} catch (error) {
			return `${reference}: ${error instanceof Error ? error.name : "request failed"}`;
		} finally {
			clearTimeout(timeout);
		}
	}));
	const failed = failures.filter((failure): failure is string => Boolean(failure));
	if (failed.length > 0) {
		throw new Error(
			`Pi model preflight failed before agent launch: ${failed.join("; ")}`,
		);
	}
}

async function probePiModelReferences(
	ctx: ExtensionContext,
	references: Array<string | undefined>,
): Promise<void> {
	await probePiModels(
		ctx,
		references
			.filter((reference): reference is string => Boolean(reference))
			.map((reference) => resolvePiModelReference(ctx, reference)),
	);
}

async function probePiSearchSpecModels(
	ctx: ExtensionContext,
	params: Record<string, unknown>,
): Promise<void> {
	const spec = isRecord(params.spec) ? params.spec : undefined;
	const strategy = spec && isRecord(spec.strategy) ? spec.strategy : undefined;
	const references: Array<string | undefined> = [];
	if (strategy) {
		if (Array.isArray(strategy.models)) {
			for (const item of strategy.models) {
				if (isRecord(item)) references.push(stringFrom(item.model));
			}
		}
		if (isRecord(strategy.worker_launch)) {
			references.push(stringFrom(strategy.worker_launch.model));
		}
		if (isRecord(strategy.evidence_annotator)) {
			references.push(stringFrom(strategy.evidence_annotator.model));
		}
		if (isRecord(strategy.search_scheduler)) {
			references.push(stringFrom(strategy.search_scheduler.model));
		}
	}
	await probePiModelReferences(ctx, references);
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
		"加载 goal-plus skill，并从 goal_plus_status 的 next_action 开始执行。",
		"首次启动时，若目标和当前对话未给出最大运行时间，简短告知任务将持续至完成或用户停止，然后继续。",
	);
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

function inheritPiNativeAgentConfig(
	launch: Record<string, unknown>,
	nativeAgentConfig: PiNativeAgentConfig,
): Record<string, unknown> {
	return {
		...launch,
		model_pattern: stringFrom(launch.model_pattern) ?? nativeAgentConfig.model_pattern,
		thinking_level: stringFrom(launch.thinking_level) ?? nativeAgentConfig.thinking_level,
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
		await probePiModels(ctx, resolved?.models ?? []);
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
		const resolved = resolveGoalPlusRoleModels(ctx, rawGoal);
		await probePiModels(ctx, resolved?.models ?? []);
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
			"新的原始目标取代之前的目标。继续前重新运行 goal_plus_record_triage。",
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
		goalPlusSessionAuthorized = status.control?.state === "active";
		activateGoal(pi, status, undefined, canPersistGoalState(ctx));
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
	context: string,
	deliverAsFollowUp: boolean,
) {
	mainStartPending = true;
	pi.sendMessage(
		{
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
		},
		deliverAsFollowUp
			? { triggerTurn: true, deliverAs: "followUp" }
			: { triggerTurn: true },
	);
}

function registerGoalPlusTool<TParams extends TSchema>(pi: ExtensionAPI, definition: ToolDefinition<TParams>) {
	// The registration closure is the ownership capability. This still gates our
	// own execute callback if host source metadata is absent; an overridden tool
	// from another extension never inherits this closure merely by sharing a name.
	pi.registerTool({
		...definition,
		async execute(toolCallId, params, signal, onUpdate, ctx) {
			const event: ToolCallEvent = { type: "tool_call", toolCallId, toolName: definition.name, input: params };
			const denied = workspaceGuard(event, ctx, "goal-plus") || await mainGate(event, ctx, "goal-plus");
			if (denied?.block) throw new Error(denied.reason);
			return definition.execute(toolCallId, params, signal, onUpdate, ctx);
		},
	});
}

function registerRuntimeTool(pi: ExtensionAPI, name: string, registered: Set<string>) {
	if (registered.has(name)) return;
	registered.add(name);
	registerGoalPlusTool(pi, {
		name,
		label: name,
		description: RuntimeToolDescriptions[name] ?? `调用 goal-plus facade 工具 ${name}。`,
		parameters: toolParameters(name),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const commandCtx = commandContextFrom(ctx);
			const startEntryCount = ctx.sessionManager.getEntries().length;
			const canPersistPiState = canPersistGoalState(ctx);
			let forwardedParams = params as Record<string, unknown>;
			if (role === "main" && name === "search_freeze_spec") {
				await probePiSearchSpecModels(ctx, forwardedParams);
			}
			if (role === "main") {
				const runId = stringFrom(forwardedParams.run_id);
				if (runId && ["search_start_agent_session", "pi_search_pool_open", "thinkthread_search_pool_open", "search_select"].includes(name)) {
					const scopeId = await ensureExecutionScope(
						pi,
						commandCtx,
						runId,
						currentPiNativeAgentConfig(pi, ctx),
					);
					if (name === "pi_search_pool_open" || name === "thinkthread_search_pool_open") {
						forwardedParams = { ...forwardedParams, execution_scope_id: scopeId };
					}
					if (name === "search_select") {
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
					: await runJsonCli(pi, commandCtx, name, forwardedParams);
			if (!isThinkThreadWorker && ["search_run_verifier", "search_get_global_evidence", "search_get_agent_context", "search_select", "search_status", "search_invalidate_run", "search_report"].includes(name)) {
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
			if (name === "search_get_agent_context") {
				activateWorkerContext(pi, result.details, registered, true);
			}
			if (role === "worker" && name === "search_run_verifier" && isRecord(result.details)) {
				const decision = result.details.allocation_decision;
				if (isRecord(decision) && typeof decision.decision_id === "string") {
					workerAllocationDecisionId = decision.decision_id;
				}
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
		"search_get_global_evidence",
		"search_reference_open",
		"search_run_verifier",
		"search_list_iterations",
	]) {
		registerRuntimeTool(pi, tool, registered);
	}
	if (candidateTask.share_out_dir) {
		registerRuntimeTool(pi, "search_stage_shared_tool", registered);
		registerRuntimeTool(pi, "search_copy_shared_tool", registered);
	}
	if (details.supplemental_evaluation_enabled === true) {
		registerRuntimeTool(pi, "search_get_evidence_detail", registered);
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

function registerPiFinalCheckTool(pi: ExtensionAPI) {
	registerGoalPlusTool(pi, {
		name: "pi_goal_plus_run_final_check",
		label: "Pi Goal Plus Final Check",
		description: "根据 goal_plus_prepare_final_check.launch 启动前台 Pi RPC 最终检查审查员。",
		parameters: toolParameters("pi_goal_plus_run_final_check"),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const commandCtx = commandContextFrom(ctx);
			const invocation = projectModuleInvocation(commandCtx, "goal-plus-pi-worker", "goal_plus.pi_worker");
			let launch = (params as { launch: Record<string, unknown> }).launch;
			const nativeAgentConfig = currentPiNativeAgentConfig(pi, ctx);
			const goalIdentity = stringFrom(launch.goal_plus_id) ?? activeGoalPlusId;
			if (goalIdentity) {
				const scopeId = await ensureExecutionScope(
					pi,
					commandCtx,
					goalIdentity,
					nativeAgentConfig,
				);
				launch = { ...launch, execution_scope_id: scopeId };
			}
			launch = inheritPiNativeAgentConfig(launch, nativeAgentConfig);
			const result = await pi.exec(invocation.command, [
				...invocation.argsPrefix,
				"run",
				"--launch-json",
				JSON.stringify(launch),
			]);
			const goalPlusId = typeof launch.goal_plus_id === "string" ? launch.goal_plus_id : activeGoalPlusId;
			const handle = result.code === 0 ? JSON.parse(result.stdout || "{}") : undefined;
			const statusResult = goalPlusId
				? await runJsonCli(pi, commandCtx, "goal_plus_status", { goal_plus_id: goalPlusId })
				: undefined;
			if (result.code !== 0) {
				const failure = commandFailure("pi_goal_plus_run_final_check", invocation, result);
				const details = { ...failure.details, status: statusResult?.details };
				return { content: [{ type: "text" as const, text: failure.text }], details };
			}
			const details = { handle, status: statusResult?.details };
			cachedGoalStatus = statusFrom(statusResult?.details);
			return {
				content: [{ type: "text" as const, text: JSON.stringify(details, null, 2) }],
				details,
			};
		},
	});
}

function registerPiWorkItemTool(pi: ExtensionAPI) {
	registerGoalPlusTool(pi, {
		name: "pi_goal_plus_run_work_item",
		label: "Pi Goal Plus Work Item",
		description: "在隔离的 Pi RPC 子进程中直接执行一个普通 subagent 任务。",
		parameters: toolParameters("pi_goal_plus_run_work_item"),
		executionMode: "concurrent",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const input = params as {
				goal_plus_id: string;
				work_item_id: string;
				task: string;
				max_runtime_seconds?: number;
			};
			const commandCtx = commandContextFrom(ctx);
			const nativeAgentConfig = currentPiNativeAgentConfig(pi, ctx);
			const executionScopeId = await ensureExecutionScope(
				pi,
				commandCtx,
				input.goal_plus_id,
				nativeAgentConfig,
			);
			const statusResult = await runJsonCli(pi, commandCtx, "goal_plus_status", {
				goal_plus_id: input.goal_plus_id,
			});
			const status = statusFrom(statusResult.details);
			const workItem = status?.work_items?.find(
				(item) => isRecord(item) && item.work_item_id === input.work_item_id,
			);
			const resumable = isRecord(workItem) && workItem.status === "result_ready" &&
				typeof workItem.agent_id === "string" &&
				typeof workItem.attempt_id === "string" &&
				typeof workItem.generation === "number";
			let sessionId: string;
			let attemptId: unknown;
			let generation: unknown;
			if (resumable && isRecord(workItem)) {
				sessionId = String(workItem.agent_id);
				const resumed = await runJsonCli(pi, commandCtx, "goal_plus_record_work_event", {
					goal_plus_id: input.goal_plus_id,
					work_item_id: input.work_item_id,
					event: "rework",
					summary: input.task,
					host: "pi",
					task_name: input.work_item_id,
					agent_id: sessionId,
					metadata: {
						orchestration_monitor: {
							native_operation: "pi_goal_plus_run_work_item",
							direction: "main_to_subagent",
						},
					},
				});
				if (isRecord(resumed.details) && resumed.details.ok === false) {
					throw new Error(String(resumed.details.error || "Pi work item resume was rejected"));
				}
				const resumedStatus = statusFrom(resumed.details);
				const resumedItem = resumedStatus?.work_items?.find(
					(item) => isRecord(item) && item.work_item_id === input.work_item_id,
				);
				attemptId = isRecord(resumedItem) ? resumedItem.attempt_id : undefined;
				generation = isRecord(resumedItem) ? resumedItem.generation : undefined;
			} else {
				sessionId = `${input.goal_plus_id}_${input.work_item_id}_${Date.now()}`.replace(
					/[^A-Za-z0-9._-]/g,
					"_",
				);
				const dispatchResult = await runJsonCli(pi, commandCtx, "goal_plus_record_work_event", {
					goal_plus_id: input.goal_plus_id,
					work_item_id: input.work_item_id,
					event: "dispatch",
					summary: input.task,
					host: "pi",
					task_name: input.work_item_id,
					metadata: {
						orchestration_monitor: {
							native_operation: "pi_goal_plus_run_work_item",
							semantic_event: "assignment",
							direction: "main_to_subagent",
						},
					},
				});
				if (isRecord(dispatchResult.details) && dispatchResult.details.ok === false) {
					throw new Error(String(dispatchResult.details.error || "Pi work item dispatch was rejected"));
				}
				const dispatched = statusFrom(dispatchResult.details);
				const dispatchedItem = dispatched?.work_items?.find(
					(item) => isRecord(item) && item.work_item_id === input.work_item_id,
				);
				attemptId = isRecord(dispatchedItem) ? dispatchedItem.attempt_id : undefined;
				generation = isRecord(dispatchedItem) ? dispatchedItem.generation : undefined;
			}
			if (typeof attemptId !== "string" || typeof generation !== "number") {
				throw new Error(`Pi work item ${input.work_item_id} did not return an attempt identity`);
			}
			const launch = inheritPiNativeAgentConfig({
				role: "ordinary",
				root: runtimeRoot,
				cwd: status?.source_path || ctx.cwd,
				agent_session_id: sessionId,
				session_id: sessionId,
				prompt: [
					"You are an implementation subagent. Work only on the assigned item.",
					`Task: ${input.task}`,
					"Do not coordinate other agents or use Goal Plus/Search tools. Return a concise result and verification evidence.",
				].join("\n\n"),
				budget_control: {
					max_runtime_seconds: input.max_runtime_seconds ?? 600,
					soft_closeout_seconds: 30,
				},
				execution_scope_id: executionScopeId,
				goal_plus_work_item: resumable
					? undefined
					: {
						goal_plus_id: input.goal_plus_id,
						work_item_id: input.work_item_id,
						attempt_id: attemptId,
						generation,
						host: "pi",
						task_name: input.work_item_id,
					},
			}, nativeAgentConfig);
			const invocation = projectModuleInvocation(commandCtx, "goal-plus-pi-worker", "goal_plus.pi_worker");
			const result = await pi.exec(invocation.command, [
				...invocation.argsPrefix,
				"run",
				"--launch-json",
				JSON.stringify(launch),
			]);
			if (result.code !== 0) {
				await runJsonCli(pi, commandCtx, "goal_plus_record_work_event", {
					goal_plus_id: input.goal_plus_id,
					work_item_id: input.work_item_id,
					event: "failed",
					summary: "Pi subagent process failed before returning a result.",
					host: "pi",
					agent_id: sessionId,
					attempt_id: attemptId,
					generation,
					metadata: {
						orchestration_monitor: {
							native_operation: "goal-plus-pi-worker",
							direction: "subagent_to_main",
						},
					},
				});
				return commandFailure("pi_goal_plus_run_work_item", invocation, result);
			}
			const handle = JSON.parse(result.stdout || "{}");
			const metadata = isRecord(handle) && isRecord(handle.metadata) ? handle.metadata : {};
			const metrics = isRecord(metadata.pi_metrics) ? metadata.pi_metrics : {};
			const observedThinking = typeof metrics.thinking_level === "string"
				? metrics.thinking_level
				: undefined;
			const failed = metadata.timed_out === true || !observedThinking;
			const assistantText = typeof metadata.assistant_text === "string"
				? metadata.assistant_text.trim()
				: "";
			const summary = assistantText
				? assistantText.slice(0, 4000)
				: "Pi subagent completed without a textual result.";
			const updated = await runJsonCli(pi, commandCtx, "goal_plus_record_work_event", {
				goal_plus_id: input.goal_plus_id,
				work_item_id: input.work_item_id,
				event: failed ? "failed" : "result",
				summary: failed ? "Pi subagent did not complete with a reported thinking level." : summary,
				host: "pi",
				task_name: input.work_item_id,
				agent_id: sessionId,
				attempt_id: attemptId,
				generation,
				transcript_path: typeof metadata.session_file === "string" ? metadata.session_file : undefined,
				metadata: {
					requested_reasoning_effort: nativeAgentConfig.thinking_level ?? "unavailable",
					native_reasoning_effort: observedThinking,
					orchestration_monitor: {
						native_operation: "goal-plus-pi-worker",
						direction: "subagent_to_main",
					},
				},
			});
			if (isRecord(updated.details) && updated.details.ok === false) {
				throw new Error(String(updated.details.error || "Pi work item result was rejected"));
			}
			return {
				content: [{ type: "text" as const, text: summary }],
				details: { handle, status: updated.details },
			};
		},
	});
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
	if (owner === "goal-plus" && role !== "main" && ["search_recovery_status", "search_prepare_recovery", "search_recover_candidate"].includes(event.toolName)) {
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
	if (owner === "goal-plus" && event.toolName === "search_get_agent_context") return undefined;
	if (!sawContext) {
		const readOnly = new Set(["read", "grep", "find", "ls"]);
		if (owner === "native" && readOnly.has(event.toolName)) return undefined;
		return { block: true, reason: "使用变更类工具前调用 search_get_agent_context。" };
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
	const requiresAuthorization = requiresGoalPlusAuthorization(event.toolName, owner);
	if (requiresAuthorization && !goalPlusSessionAuthorized) {
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
		context: { mode, continuationCount },
	});
	const details = gateFrom(gate.details);
	if (!details) return;
	if (details.decision === "block") {
		continuationCount += 1;
		if (canPersistGoalState(ctx)) persistGoalState(pi);
		pi.sendMessage(
			{
				customType: "goal-plus-stop-continuation",
				content: details.continuation_prompt || details.reason || "Goal Plus 仍处于 active。继续下一项必需 action。",
				display: true,
				details: { goal_plus_id: activeGoalPlusId, continuationCount,
					goal_revision: details.goal_revision, control_version: details.control_version,
					session_id: details.session_id, request_id: randomUUID() },
			},
			{ triggerTurn: true, deliverAs: "followUp" },
		);
	}
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
		return;
	}
	if (stopReason === "aborted") {
		await closeExecutionScope(pi, commandContextFrom(ctx), "pi_agent_aborted");
		await runPiHostSessionEvent(
			pi, ctx, "interrupt", "next_user_turn", piHostInteraction(ctx),
		);
		goalPlusSessionAuthorized = false;
		if (persist) persistGoalState(pi);
		return;
	}
	if (ctx.hasPendingMessages()) return;
	const commandCtx = commandContextFrom(ctx);
	const usage = collectGoalUsageFromEntries(ctx.sessionManager.getEntries() as unknown[]);
	const status = await refreshActiveGoal(pi, commandCtx, persist);
	if (isTerminalStatus(status?.status)) {
		await closeExecutionScope(pi, commandCtx, "goal_plus_terminal");
		let snapshot: Record<string, unknown> | undefined;
		try {
			const monitor = await runJsonCli(pi, commandCtx, "goal_plus_monitor_snapshot", {
				goal_plus_id: status.goal_plus_id ?? activeGoalPlusId,
				run_id: status.current_search_run_id ?? undefined,
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
				queuePublicationNotificationTurn(pi, runId, publication.notificationIds);
				return;
			}
		}
		// Retain history binding for summary and an explicit edit of complete.
		goalPlusSessionAuthorized = false;
		if (persist) persistGoalState(pi);
	}
}

let piForGate: ExtensionAPI;

export default function (pi: ExtensionAPI) {
	piForGate = pi;
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
	const GOAL_PLUS_COMMAND_CONFIG_USAGE = "[mode=autonomous|probe] [max_parallel=正整数] [max_duration_seconds=正整数（秒）] [workspace_backend=copy|git_worktree|thinkthread] [promotion_mode=apply|artifact_only] [strategy=NAME] [inner_agent=ADAPTER_ID] [annotator=MODEL] [workers|models=MODEL[,MODEL...] 或 MODEL*COUNT[,MODEL*COUNT...]]";
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
					if (prompt) startGoalPlusMainTurn(pi, prompt, false);
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
				if (prompt) startGoalPlusMainTurn(pi, prompt, deliverAsFollowUp);
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
				if (prompt) startGoalPlusMainTurn(pi, prompt, !ctx.isIdle());
			},
		});
		if (role === "main" && progressPreviewMode) {
			pi.registerCommand("goal-plus-ui-preview", {
				description: "预览 Goal Plus 进度或完成 UI，不创建 Goal Plus/Search run",
				handler: async (args, ctx) => {
					const mode = args.trim().toLowerCase() || "active";
					if (mode === "clear") {
						clearPiProgress(ctx);
						ctx.ui.notify("Goal Plus progress preview cleared.", "info");
						return;
					}
					if (mode === "result") {
						clearPiProgress(ctx);
						appendGoalCompletionPreview(pi);
						return;
					}
					if (mode !== "active" && mode !== "complete") {
						ctx.ui.notify("Usage: /goal-plus-ui-preview [active|complete|result|clear]", "error");
						return;
					}
					showPiProgress(ctx, PI_PROGRESS_PREVIEWS[mode]);
				},
			});
		}
	}

	const mainTools = [
		"goal_plus_status",
		"goal_plus_monitor_snapshot",
		"goal_plus_list_models",
		"goal_plus_record_triage",
		"goal_plus_record_work_event",
		"goal_plus_save_spec_draft",
		"goal_plus_link_search_run",
		"goal_plus_record_search_result",
		"goal_plus_prepare_final_check",
		"goal_plus_set_status",
		"goal_plus_gate",
		"pi_goal_plus_run_work_item",
		"search_freeze_spec",
		"search_create",
		"search_status",
		"search_recovery_status",
		"search_prepare_recovery",
		"search_recover_candidate",
		"search_invalidate_run",
		"search_list_history",
		"search_plan_next",
		"search_start_batch",
		"search_get_agent_observability",
		"search_run_verifier",
		"search_list_allocation_decisions",
		"search_apply_allocation_decision",
		"search_select",
		"search_report",
		"search_promote",
		"search_apply_promotion",
		"pi_search_pool_open",
		"pi_search_pool_wait_any",
		"pi_search_pool_snapshot",
		"pi_search_pool_continue",
		"pi_search_pool_close",
		"thinkthread_search_pool_open",
		"thinkthread_search_pool_wait_any",
		"thinkthread_search_pool_snapshot",
		"thinkthread_search_pool_continue",
		"thinkthread_search_pool_close",
	];
	const workerTools = ["search_get_agent_context"];
	const thinkThreadWorkerTools = [
		"search_get_agent_context",
		"search_get_global_evidence",
		"search_reference_open",
		"search_stage_shared_tool",
		"search_copy_shared_tool",
		"search_get_evidence_detail",
		"search_run_verifier",
		"search_list_iterations",
	];
	const thinkThreadAnnotationTools = [...THINKTHREAD_ANNOTATION_TOOL_NAMES];
	const finalCheckerTools = ["goal_plus_status", "goal_plus_submit_final_check"];
	const roleTools = role === "worker"
		? isThinkThreadWorker
			? [
				...thinkThreadWorkerTools.filter((tool) => ![
					"search_reference_open",
					"search_stage_shared_tool",
					"search_copy_shared_tool",
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
	for (const tool of roleTools) {
		registerRuntimeTool(pi, tool, registeredRuntimeTools);
	}
	if (role === "main") {
		registerPiFinalCheckTool(pi);
		registerPiWorkItemTool(pi);
	}
	pi.on("input", async (event, ctx) => {
		scopeThinkThreadWorkerTools(pi, ctx, event.text);
		if (role !== "main") {
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
			return prompt ? { action: "transform", text: prompt, images: event.images } : { action: "handled" };
		}
		if (!request.rawGoal) {
			ctx.ui.notify("Goal Plus command requires a goal", "error");
			return { action: "handled" };
		}
		const prompt = request.action === "edit"
				? await updateGoalPlusStart(pi, ctx, request.rawGoal)
				: await createGoalPlusStart(pi, ctx, request.rawGoal, request.withFinalCheck);
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
		if (role === "main" && progressPreviewMode) {
			showPiProgress(ctx, PI_PROGRESS_PREVIEWS[progressPreviewMode]);
			return;
		}
		restoreGoalState(ctx);
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
						queuePublicationNotificationTurn(pi, runId, publication.notificationIds);
						return;
						}
					}
					if (persist) persistGoalState(pi);
					return;
				}
			if (!status?.goal_plus_id) return;
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
		const status = await refreshActiveGoal(pi, commandContextFrom(ctx), false);
		const messages = event.messages;
		const isRequest = (message: unknown) => isRecord(message) && message.role === "custom"
			&& [GOAL_PLUS_COMMAND_CONTEXT_TYPE, "goal-plus-stop-continuation"].includes(String(message.customType));
		const stale = (message: unknown) => {
			if (!isRequest(message) || !isRecord(message) || !isRecord(message.details)) return false;
			const d = message.details;
			return !status || status.control?.state !== "active" || isTerminalStatus(status.status)
				|| d.goal_plus_id !== status.goal_plus_id || d.goal_revision !== status.goal_revision
				|| d.control_version !== status.control?.version
				|| (d.session_id !== undefined && d.session_id !== ctx.sessionManager.getSessionId());
		};
		// Only cancel a stale, not-yet-run automatic request. Historical requests
		// must never abort an ordinary user turn or an already running Main turn.
		const lastInput = [...messages].reverse().find((message) => message.role === "user" || message.role === "assistant" || isRequest(message));
		if (lastInput && stale(lastInput)) {
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
		if (role !== "main" || !activeGoalPlusId) return;
		const commandCtx = commandContextFrom(ctx);
		const persist = canPersistGoalState(ctx);
		const status = await refreshActiveGoal(pi, commandCtx, persist);
		const runId = stringFrom(status?.current_search_run_id);
		const publication: PublicationNotificationPoll = runId
			? await publicationNotificationContext(pi, commandCtx, runId, true)
			: { ok: true, notificationIds: [] };
		if (publication.content && persist) persistGoalState(pi);
		const goalContext = goalPlusSessionAuthorized && status && !isTerminalStatus(status.status)
			&& status.control?.state === "active"
			? buildGoalPlusContext(status)
			: undefined;
		const content = [publication.content, goalContext].filter((item): item is string => Boolean(item));
		if (content.length === 0) return;
		return {
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
	pi.on("agent_start", async (_event, ctx) => {
		if (role === "main") mainTurnSignal = ctx.signal;
	});
	pi.on("agent_end", async (event, ctx) => {
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
			if (workerAllocationDecisionId) return;
			if (
				isThinkThreadWorker
				&& latestDispatchMetadata(ctx).thinkthreadRole !== "annotator"
				&& !workerVerifierSubmitted
				&& !workerMissingAttemptContinuationSent
			) {
				workerMissingAttemptContinuationSent = true;
				pi.sendMessage(
					{
						customType: "goal-plus-thinkthread-missing-attempt",
						content: "本次 ThinkThread dispatch 尚未提交 accepted verifier attempt。继续当前 wake：如果尚未加载 candidate_task，先调用 search_get_agent_context；随后完成本轮修改，并使用当前 agent_session_id 恰好调用一次 search_run_verifier。不得只读取或总结后再次结束。",
						display: false,
						details: { dispatchNonce: workerActiveDispatchNonce },
					},
					{ triggerTurn: true, deliverAs: "followUp" },
				);
				return;
			}
			if (
				!Number.isFinite(workerContinueUntilMs) ||
				workerContinueUntilMs <= 0 ||
				Date.now() >= workerContinueUntilMs
			) {
				return;
			}
			workerContinuationCount += 1;
			pi.sendMessage(
				{
					customType: "goal-plus-worker-continuation",
					content: "继续当前 Candidate 会话。沿用已加载的运行时上下文和 Evidence；探索新方向。只有产物发生实质变化后才运行 verifier，不要重复验证未修改的产物。",
					display: false,
					details: { workerContinuationCount, workerContinueUntilMs },
				},
				{ triggerTurn: true, deliverAs: "followUp" },
			);
			return;
		}
	});
	pi.on("agent_settled", async (_event, ctx) => {
		await settleMainGoal(pi, ctx);
	});
	pi.on("session_shutdown", async (event, ctx) => {
		if (role !== "main") return;
		await closeExecutionScope(pi, commandContextFrom(ctx), `pi_session_shutdown:${event.reason}`);
	});
}
