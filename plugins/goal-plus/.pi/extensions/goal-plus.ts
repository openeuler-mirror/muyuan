import { parseArgs, type ExtensionAPI, type ExtensionContext, type ToolCallEvent } from "@earendil-works/pi-coding-agent";
import { Box, Text } from "@earendil-works/pi-tui";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
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
} from "./goal-plus-progress.ts";

const role = process.env.GOAL_PLUS_PI_ROLE || "main";
const runtimeRoot = process.env.GOAL_PLUS_ROOT || ".gp";
const sourcePath = process.env.GOAL_PLUS_SOURCE_PATH;
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
let activeGoalPlusId = process.env.GOAL_PLUS_ID;
let cachedGoalStatus: GoalPlusStatusPayload | undefined;
let continuationCount = 0;
let workerContinuationCount = 0;
let workerAllocationDecisionId: string | undefined;
let activeGoalStartedAt: string | undefined;
let activeGoalStartEntryCount = 0;
let goalPlusSessionAuthorized = false;
let lastMainStopReason: string | undefined;
let mainTurnSignal: AbortSignal | undefined;

const INSTALL_HINT =
	'Install this project into the Python environment that launches Pi: python -m pip install -e ".[dev]".';
const LooseObject = Type.Object({}, { additionalProperties: true });
const GoalPlusConfidence = Type.Union([Type.Literal("high"), Type.Literal("medium"), Type.Literal("low")]);
const GoalPlusRecommendedPhase = Type.Union([
	Type.Literal("goal"),
	Type.Literal("spec_discovery"),
	Type.Literal("search"),
]);
const GoalPlusDiscoveryOrigin = Type.Union([Type.Literal("initial"), Type.Literal("in_progress")]);
const GoalPlusFinalCheckerHost = Type.Union([Type.Literal("codex"), Type.Literal("pi")]);
const GoalPlusTriage = Type.Object(
	{
		is_optimization: Type.Boolean(),
		confidence: GoalPlusConfidence,
		recommended_phase: GoalPlusRecommendedPhase,
		identified_at: Type.Optional(GoalPlusDiscoveryOrigin),
		scenario: Type.Optional(Type.String()),
		reasons: Type.Optional(Type.Array(Type.String())),
		missing: Type.Optional(Type.Array(Type.String())),
	},
	{ additionalProperties: false },
);
const GoalPlusNextAction = Type.Object(
	{
		kind: Type.String(),
		description: Type.String(),
		required: Type.Optional(Type.Boolean()),
		metadata: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const GoalPlusWorkEvent = Type.Union([
	Type.Literal("dispatch"),
	Type.Literal("bind"),
	Type.Literal("message"),
	Type.Literal("result"),
	Type.Literal("rework"),
	Type.Literal("accepted"),
	Type.Literal("failed"),
	Type.Literal("cancelled"),
]);
const PositiveInteger = Type.Integer({ exclusiveMinimum: 0 });
const NullableString = Type.Union([Type.String(), Type.Null()]);
const NullablePositiveInteger = Type.Union([PositiveInteger, Type.Null()]);
const VerifierRole = Type.Union([
	Type.Literal("validity_gate"),
	Type.Literal("process_gate"),
	Type.Literal("ranking_signal"),
	Type.Literal("diagnostic_signal"),
	Type.Literal("promotion_gate"),
	Type.Literal("anti_cheat_gate"),
]);
const FeedbackPolicy = Type.Union([
	Type.Literal("visible_to_workers"),
	Type.Literal("summary_only"),
	Type.Literal("final_only"),
]);
const VerifierCommand = Type.Object(
	{
		name: Type.String({ minLength: 1 }),
		role: VerifierRole,
		command: Type.Array(Type.String(), { minItems: 1 }),
		cwd: Type.Optional(Type.String()),
		timeout_seconds: Type.Optional(PositiveInteger),
		feedback_policy: Type.Optional(FeedbackPolicy),
		expected_outputs: Type.Optional(Type.Array(Type.String())),
		resource_lock: Type.Optional(Type.String({ minLength: 1 })),
	},
	{ additionalProperties: false },
);
const EditSurface = Type.Object(
	{
		allow: Type.Array(Type.String(), { minItems: 1 }),
		deny: Type.Optional(Type.Array(Type.String())),
		max_file_changes: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);
const DevicePoolSpec = Type.Object(
	{
		pool_id: Type.String({
			minLength: 1,
			pattern: "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
		}),
	},
	{ additionalProperties: false },
);
const SearchBudget = Type.Object(
	{
		max_parallel: Type.Integer({
			exclusiveMinimum: 0,
			description:
				"一个 Search run 的初始 candidate 数和实时活跃 candidate 上限 K。",
		}),
		max_candidates: Type.Optional(
			Type.Union([PositiveInteger, Type.Null()], {
				description:
					"累计唯一 candidate 上限；null 表示不限制，有界值必须大于等于 max_parallel。",
			}),
		),
		max_tokens: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);
const WorkerBudget = Type.Object(
	{
		max_runtime_seconds: Type.Optional(NullablePositiveInteger),
		max_turns: Type.Optional(NullablePositiveInteger),
		on_exceed: Type.Optional(Type.Literal("interrupt")),
		min_runtime_seconds: Type.Optional(NullablePositiveInteger),
		min_verifier_runs: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);
const SearchSchedulerSpec = Type.Object(
	{
		host: Type.Union([
			Type.Literal("codex"),
			Type.Literal("pi-rpc"),
		]),
		model: Type.String({ minLength: 1 }),
		reasoning_effort: Type.String({ minLength: 1 }),
		timeout_seconds: PositiveInteger,
		reward: Type.String({ minLength: 1 }),
		allocation: Type.String({ minLength: 1 }),
	},
	{ additionalProperties: false },
);
const CandidateProposal = Type.Object(
	{
		intent: Type.String({ minLength: 1 }),
		hypothesis: Type.Optional(NullableString),
		expected_tradeoff: Type.Optional(Type.String()),
		instructions: Type.Optional(Type.Array(Type.String())),
		metadata: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const WorkerLaunch = Type.Object(
	{
		model: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		service_tier: Type.Optional(NullableString),
	},
	{ additionalProperties: false },
);
const EvidenceAnnotatorProvider = Type.Object(
	{
		provider_id: Type.Optional(Type.String({ pattern: "^[A-Za-z0-9_-]+$" })),
		name: Type.Optional(Type.String({ minLength: 1 })),
		base_url: Type.String({ minLength: 1 }),
		api_key_env: Type.Optional(Type.String({ minLength: 1 })),
		wire_api: Type.Optional(Type.String({ minLength: 1 })),
	},
	{ additionalProperties: false },
);
const EvidenceAnnotator = Type.Object(
	{
		model: Type.Optional(NullableString),
		pi_provider: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		timeout_seconds: Type.Optional(PositiveInteger),
		provider: Type.Optional(Type.Union([EvidenceAnnotatorProvider, Type.Null()])),
	},
	{ additionalProperties: false },
);
const ModelSpec = Type.Object(
	{
		model: Type.String({ minLength: 1 }),
		count: Type.Optional(PositiveInteger),
		provider: Type.Optional(NullableString),
		adapter_version: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		service_tier: Type.Optional(NullableString),
		context_policy: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const StrategySpec = Type.Object(
	{
		name: Type.Optional(Type.String({ minLength: 1 })),
		orchestration_mode: Type.Optional(
			Type.Union([
				Type.Literal("parallel_loops"),
				Type.Literal("adaptive_search"),
			]),
		),
		worker_host: Type.Optional(Type.Literal("pi-rpc")),
		worker_agent_type: Type.Optional(NullableString),
		worker_budget: Type.Optional(Type.Union([WorkerBudget, Type.Null()])),
		worker_launch: Type.Optional(Type.Union([WorkerLaunch, Type.Null()])),
		evidence_annotator: Type.Optional(EvidenceAnnotator),
		models: Type.Optional(Type.Array(ModelSpec)),
		search_scheduler: Type.Optional(Type.Union([SearchSchedulerSpec, Type.Null()])),
		config: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const WorkspaceSpec = Type.Object(
	{
		backend: Type.Optional(Type.Union([Type.Literal("copy"), Type.Literal("git_worktree")])),
		promotion_mode: Type.Optional(Type.Union([Type.Literal("apply"), Type.Literal("artifact_only")])),
	},
	{ additionalProperties: false },
);
const SharedDirSpec = Type.Object(
	{
		enabled: Type.Boolean(),
		max_tools_per_iteration: Type.Optional(PositiveInteger),
		max_pending_revisions_per_family: Type.Optional(Type.Literal(1)),
		max_published_versions_per_family: Type.Optional(Type.Integer({ minimum: 1, maximum: 32 })),
		max_files_per_iteration: Type.Optional(PositiveInteger),
		max_path_entries_per_iteration: Type.Optional(PositiveInteger),
		max_depth: Type.Optional(PositiveInteger),
		max_bytes_per_iteration: Type.Optional(PositiveInteger),
	},
	{ additionalProperties: false },
);
const ToolizationSignal = Type.Union([
	Type.Literal("repeated_workflow"),
	Type.Literal("domain_construction_or_probe"),
	Type.Literal("behavior_or_invariant_checker"),
	Type.Literal("reproducer_fixture_or_case_generator"),
	Type.Literal("parser_trace_or_comparator"),
	Type.Literal("peer_setup_or_feedback_reduction"),
]);
const ToolizationExclusion = Type.Union([
	Type.Literal("single_common_command"),
	Type.Literal("logic_free_wrapper"),
	Type.Literal("restricted_artifact"),
	Type.Literal("candidate_private_state"),
	Type.Literal("duplicate_snapshot"),
	Type.Literal("no_toolizable_material"),
	Type.Literal("existing_family_sufficient"),
	Type.Literal("no_material_tool_delta"),
	Type.Literal("draft_not_ready"),
	Type.Literal("max_published_versions_reached"),
]);
const ToolizationDecision = Type.Object(
	{
		outcome: Type.Union([Type.Literal("staged"), Type.Literal("not_applicable")]),
		signals: Type.Array(ToolizationSignal, {
			maxItems: 6,
			description: "使用六个 capability-oriented toolization signal。",
		}),
		exclusion: Type.Optional(Type.Union([ToolizationExclusion, Type.Null()])),
		rationale: Type.String({ minLength: 1, maxLength: 1000 }),
		tool_names: Type.Array(Type.String({ minLength: 1, maxLength: 120 }), { maxItems: 16 }),
	},
	{ additionalProperties: false },
);
const SearchSpecSchema = Type.Object(
	{
		objective: Type.String({ minLength: 1 }),
		metric_name: Type.String({ minLength: 1 }),
		metric_direction: Type.Union([Type.Literal("minimize"), Type.Literal("maximize")]),
		source_path: Type.String({ minLength: 1 }),
		edit_surface: EditSurface,
		budget: SearchBudget,
		process_verifiers: Type.Array(VerifierCommand, { minItems: 1 }),
		promotion_verifiers: Type.Optional(Type.Array(VerifierCommand)),
		device_pools: Type.Optional(Type.Array(DevicePoolSpec)),
		candidate_acceptance: Type.Optional(
			Type.Array(Type.String({ minLength: 1, maxLength: 1000 }), { maxItems: 32 }),
		),
		constraints: Type.Optional(LooseObject),
		root_hypotheses: Type.Optional(Type.Array(Type.String())),
		strategy: Type.Optional(StrategySpec),
		workspace: Type.Optional(WorkspaceSpec),
		shared_dir: Type.Optional(SharedDirSpec),
	},
	{ additionalProperties: false },
);
const SearchSpecDraftSchema = Type.Partial(SearchSpecSchema);
const GoalPlusSpecDraft = Type.Object(
	{
		baseline: LooseObject,
		metric: LooseObject,
		correctness_gate: LooseObject,
		edit_surface: LooseObject,
		verifier_artifacts: Type.Optional(Type.Array(Type.String())),
		search_spec: SearchSpecDraftSchema,
		promotion_rule: Type.String(),
		confidence: GoalPlusConfidence,
		origin: Type.Optional(GoalPlusDiscoveryOrigin),
		open_questions: Type.Optional(Type.Array(Type.String())),
	},
	{ additionalProperties: false },
);
const RuntimeToolSchemas: Record<string, TSchema> = {
	goal_plus_create: Type.Object(
		{
			raw_goal: Type.String(),
			source_path: Type.Optional(NullableString),
			policy: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	goal_plus_status: Type.Object({ goal_plus_id: Type.String() }, { additionalProperties: false }),
	goal_plus_update_goal: Type.Object(
		{
			goal_plus_id: Type.String(),
			raw_goal: Type.String(),
			expected_revision: Type.Integer(),
			reason: Type.Optional(NullableString),
		},
		{ additionalProperties: false },
	),
	goal_plus_monitor_snapshot: Type.Object(
		{
			goal_plus_id: Type.Optional(Type.String()),
			run_id: Type.Optional(Type.String()),
			stale_after_seconds: Type.Optional(Type.Number()),
			feature_plugins: Type.Optional(Type.Array(Type.String())),
		},
		{ additionalProperties: false },
	),
	goal_plus_list_models: Type.Object(
		{
			host: Type.Union([Type.Literal("codex"), Type.Literal("pi-rpc")]),
			query: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_record_triage: Type.Object(
		{
			goal_plus_id: Type.String(),
			triage: GoalPlusTriage,
		},
		{ additionalProperties: false },
	),
	goal_plus_record_work_event: Type.Object(
		{
			goal_plus_id: Type.String(),
			work_item_id: Type.String(),
			event: GoalPlusWorkEvent,
			summary: Type.String({ minLength: 1, maxLength: 4000 }),
			host: Type.Optional(Type.String({ minLength: 1, maxLength: 120 })),
			task_name: Type.Optional(Type.String()),
			agent_id: Type.Optional(Type.String()),
			attempt_id: Type.Optional(Type.String()),
			generation: Type.Optional(PositiveInteger),
			launch_ttl_seconds: Type.Optional(Type.Integer({ minimum: 1, maximum: 3600 })),
			transcript_path: Type.Optional(Type.String()),
			evidence: Type.Optional(Type.Array(LooseObject)),
			metadata: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	goal_plus_save_spec_draft: Type.Object(
		{
			goal_plus_id: Type.String(),
			spec_draft: GoalPlusSpecDraft,
		},
		{ additionalProperties: false },
	),
	goal_plus_link_search_run: Type.Object(
		{
			goal_plus_id: Type.String(),
			frozen_spec_id: Type.String(),
			run_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	goal_plus_record_search_result: Type.Object(
		{
			goal_plus_id: Type.String(),
			run_id: Type.String(),
			selected_candidate_id: Type.Optional(Type.String()),
			report_path: Type.Optional(Type.String()),
			promotion_artifact_path: Type.Optional(Type.String()),
			summary: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_prepare_final_check: Type.Object(
		{
			goal_plus_id: Type.String(),
			checker_host: GoalPlusFinalCheckerHost,
		},
		{ additionalProperties: false },
	),
	goal_plus_submit_final_check: Type.Object(
		{
			goal_plus_id: Type.String(),
			check_id: Type.String(),
			goal_revision: Type.Number(),
			verdict: Type.Union([
				Type.Literal("pass"),
				Type.Literal("fail"),
				Type.Literal("interrupted"),
			]),
			summary: Type.String(),
			findings: Type.Optional(Type.Array(LooseObject)),
			evidence: Type.Optional(Type.Array(LooseObject)),
			checker_metadata: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	goal_plus_set_status: Type.Object(
		{
			goal_plus_id: Type.String(),
			status: Type.Union([
				Type.Literal("active"),
				Type.Literal("needs_user"),
				Type.Literal("blocked"),
				Type.Literal("complete"),
				Type.Literal("abandoned"),
			]),
			reason: Type.Optional(Type.String()),
			evidence: Type.Optional(Type.Array(LooseObject)),
			next_action: Type.Optional(GoalPlusNextAction),
		},
		{ additionalProperties: false },
	),
	goal_plus_gate: Type.Object(
		{
			goal_plus_id: Type.String(),
			event: Type.Union([
				Type.Literal("stop"),
				Type.Literal("subagent_stop"),
				Type.Literal("pre_tool_use"),
				Type.Literal("user_prompt_submit"),
			]),
			context: LooseObject,
		},
		{ additionalProperties: false },
	),
	search_freeze_spec: Type.Object(
		{
			spec: SearchSpecSchema,
			verifier_artifact_paths: Type.Array(Type.String()),
		},
		{ additionalProperties: false },
	),
	search_create: Type.Object(
		{
			frozen_spec_id: Type.String(),
			source_run_id: Type.Optional(
				Type.Union(
					[Type.String({ pattern: "^run_" }), Type.Null()],
					{
						description:
							"初始 run 必须省略 source_run_id，或在 strict schema 下传 null；仅后继 run 传入真实已存在的 run_* ID。",
					},
				),
			),
		},
		{ additionalProperties: false },
	),
	search_status: Type.Object({ run_id: Type.String() }, { additionalProperties: false }),
	search_invalidate_run: Type.Object(
		{
			run_id: Type.String(),
			reason: Type.Union([
				Type.Literal("verifier_contract_invalid"),
				Type.Literal("verifier_coverage_inadequate"),
				Type.Literal("verifier_nondeterministic"),
				Type.Literal("verifier_target_mismatch"),
				Type.Literal("verifier_infrastructure_failure"),
			]),
			summary: Type.String({ minLength: 1 }),
			evidence: Type.Array(LooseObject, { minItems: 1 }),
		},
		{ additionalProperties: false },
	),
	search_list_history: Type.Object(
		{
			run_id: Type.String(),
			top_n: Type.Optional(Type.Number()),
			sort_by: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	search_plan_next: Type.Object(
		{
			run_id: Type.String(),
			requested_k: Type.Optional(
				Type.Integer({
					exclusiveMinimum: 0,
					description:
						"仅为本规划轮次请求的候选数。运行时按 min(requested_k, 剩余候选总预算, budget.max_parallel) 进行规划。默认值 4 是 batch size 请求，不是整个 run 的预算。",
				}),
			),
		},
		{ additionalProperties: false },
	),
	search_start_batch: Type.Object(
		{
			run_id: Type.String(),
			plan_id: Type.String(),
			proposals: Type.Optional(Type.Array(CandidateProposal)),
		},
		{ additionalProperties: false },
	),
	search_start_agent_session: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
			directive: Type.Optional(Type.Union([Type.String(), LooseObject])),
			worker_budget: Type.Optional(WorkerBudget),
		},
		{ additionalProperties: false },
	),
		search_redispatch_candidate: Type.Object(
			{
				run_id: Type.String(),
				candidate_id: Type.String(),
				worker_agent_type: Type.Optional(Type.String()),
			worker_budget: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	search_bind_agent_handle: Type.Object(
		{
			agent_session_id: Type.String(),
			handle: LooseObject,
		},
		{ additionalProperties: false },
	),
		search_continue_agent_session: Type.Object(
			{
				agent_session_id: Type.String(),
				worker_budget: Type.Optional(WorkerBudget),
		},
		{ additionalProperties: false },
	),
	search_get_agent_context: Type.Object({ agent_session_id: Type.String() }, { additionalProperties: false }),
	search_get_global_evidence: Type.Object(
		{ agent_session_id: Type.String() },
		{ additionalProperties: false },
	),
	search_reference_open: Type.Object(
		{
			agent_session_id: Type.String(),
			source_candidate_id: Type.String(),
			source_iteration: PositiveInteger,
		},
		{ additionalProperties: false },
	),
	search_copy_shared_tool: Type.Object(
		{
			agent_session_id: Type.String(),
			tool_id: Type.String(),
			snapshot_hash: Type.String(),
		},
		{ additionalProperties: false },
	),
	search_get_evidence_detail: Type.Object(
		{
			agent_session_id: Type.String(),
			candidate_id: Type.String(),
			iteration: PositiveInteger,
		},
		{ additionalProperties: false },
	),
	search_stage_shared_tool: Type.Object(
		{
			agent_session_id: Type.String(),
			name: Type.String({ minLength: 1, maxLength: 120 }),
			summary: Type.String({ minLength: 1, maxLength: 500 }),
			entrypoint: Type.String({ minLength: 1, maxLength: 300 }),
			candidate_relative_source_paths: Type.Array(Type.String({ minLength: 1 }), {
				minItems: 1,
			}),
			publication_intent: Type.Optional(
				Type.Union([
					Type.Literal("new"),
					Type.Literal("capability_extension"),
					Type.Literal("adoption_fix"),
					Type.Literal("contract_change"),
				]),
			),
			supersedes_tool_id: Type.Optional(Type.String({ minLength: 1 })),
			capability_ids: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 160 }), {
				maxItems: 32,
				uniqueItems: true,
			})),
			coverage_keys: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 160 }), {
				maxItems: 64,
				uniqueItems: true,
			})),
		},
		{ additionalProperties: false },
	),
	search_get_agent_observability: Type.Object(
		{ agent_session_id: Type.String() },
		{ additionalProperties: false },
	),
	search_run_verifier: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
			scope: Type.Optional(Type.Union([Type.Literal("process"), Type.Literal("promotion")])),
			agent_session_id: Type.Optional(Type.String()),
			hypothesis: Type.Optional(Type.String()),
			toolization_decision: Type.Optional(ToolizationDecision),
		},
		{ additionalProperties: false },
	),
	search_list_iterations: Type.Object(
		{
			agent_session_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	search_list_allocation_decisions: Type.Object(
		{
			run_id: Type.String(),
			status: Type.Optional(Type.Union([
				Type.Literal("pending"),
				Type.Literal("applied"),
			])),
		},
		{ additionalProperties: false },
	),
	search_apply_allocation_decision: Type.Object(
		{
			run_id: Type.String(),
			decision_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	search_select: Type.Object(
		{ run_id: Type.String() },
		{ additionalProperties: false },
	),
	search_report: Type.Object({ run_id: Type.String() }, { additionalProperties: false }),
	search_promote: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	search_apply_promotion: Type.Object(
		{ run_id: Type.String() },
		{ additionalProperties: false },
	),
		pi_search_pool_open: Type.Object(
			{
				run_id: Type.String(),
				candidate_ids: Type.Optional(Type.Array(Type.String())),
				worker_budgets: Type.Optional(Type.Record(Type.String(), WorkerBudget)),
			final_verify: Type.Optional(Type.Boolean()),
			max_parallel: Type.Optional(PositiveInteger),
		},
		{ additionalProperties: false },
	),
	pi_search_pool_wait_any: Type.Object(
		{
			pool_id: Type.String(),
			timeout_seconds: Type.Optional(Type.Number({ minimum: 0 })),
		},
		{ additionalProperties: false },
	),
	pi_search_pool_snapshot: Type.Object(
		{
			pool_id: Type.Optional(Type.String()),
			run_id: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
		pi_search_pool_continue: Type.Object(
			{
				pool_id: Type.String(),
				candidate_id: Type.String(),
				worker_budget: Type.Optional(WorkerBudget),
				final_verify: Type.Optional(Type.Boolean()),
		},
		{ additionalProperties: false },
	),
	pi_search_pool_close: Type.Object(
		{
			pool_id: Type.String(),
			mode: Type.Optional(Type.Union([Type.Literal("drain"), Type.Literal("interrupt")])),
			timeout_seconds: Type.Optional(Type.Number({ minimum: 0 })),
		},
		{ additionalProperties: false },
	),
	pi_goal_plus_run_final_check: Type.Object(
		{ launch: LooseObject },
		{ additionalProperties: false },
	),
	pi_goal_plus_run_work_item: Type.Object(
		{
			goal_plus_id: Type.String(),
			work_item_id: Type.String(),
			task: Type.String({ minLength: 1, maxLength: 4000 }),
			max_runtime_seconds: Type.Optional(Type.Integer({ minimum: 30, maximum: 3600 })),
		},
		{ additionalProperties: false },
	),
};
const RuntimeToolDescriptions: Record<string, string> = {
	goal_plus_record_work_event:
		"按实际发生记录带 attempt/generation fencing 的 subagent 派发、绑定、消息、结果和主 Agent 决定；首次 dispatch 自动创建记录。",
	goal_plus_save_spec_draft:
		"保存发现的 SearchSpec draft。固定候选使用 orchestration_mode=parallel_loops；明确需要 runtime reward/allocation 时改用 orchestration_mode=adaptive_search 并设置非 null 的 search_scheduler。max_parallel 是实时并发上限；可选 max_candidates 是累计唯一 candidate 上限，null 表示不限制。",
	search_freeze_spec:
		"冻结不可变的 SearchSpec、verifier bundle 和 source artifact。Git backend 先在 clean origin 运行 process verifier；第一次 search_start_batch 再在 c001 worktree 验证 relocation，通过后才创建其余 lane。bootstrap 结果不是 candidate iteration。并发 Search 下 verifier 临时文件必须放入唯一的 GOAL_PLUS_VERIFIER_TMPDIR/TMPDIR，绝不能使用固定 /tmp 路径。",
	search_create:
		"从 frozen_spec_id 创建 Search run。初始 run 必须省略 source_run_id，或在 strict schema 下传 null；仅在已有真实前驱时传入准确的 run_* ID，绝不能传 initial 或 in_progress。",
	search_get_agent_context:
		"读取当前 worker 的紧凑权威 candidate 上下文，包括 candidate_task.instructions、冻结验收合同、进程环境、近期与最佳 iteration、results.tsv 路径和条件能力。",
	search_get_global_evidence:
		"读取当前 run 的窄 Global Evidence 视图；任一 View 为 null 时都无需等待。",
	search_reference_open:
		"把一条已结算 Evidence checkout 到当前 worker 唯一的 ref-workspace。再次调用会把同一目录切到新的精确 commit；直接用普通文件和命令工具查看返回的 root，所有实现修改仍留在 candidate workspace。",
	search_copy_shared_tool:
		"按 Global Evidence 中的 tool_id 和 snapshot_hash 复制精确 shared-dir 快照到 candidate 临时 inbox；receipt 由下一次 worker verifier 消费。",
	search_stage_shared_tool:
		"把当前 candidate 的 .tmp/tool-drafts/ 中显式选择的文件安全复制到 .tmp/share-out；该工具只负责 staging，发布仍由 passing process verifier 结算。",
	search_get_evidence_detail:
		"按需展开一条已结算 Evidence 的 supplemental evaluation。仅当 agent context 声明该能力开启且目标行 supplemental_available=true 时调用；independent 模式只允许读取自己的 candidate。",
	search_run_verifier:
		"为候选运行 process verifier。必须提供一句话 hypothesis 并遵守 candidate_task.instructions。报告在 workspace/results.tsv 中追加且只追加一条已验证记录，并返回 keep/retain/discard/failure 结算；VerifierWorkspaceSideEffect 要求停止并上报。",
	search_list_iterations:
		"返回当前 agent session 所属 candidate 可能很大的全量 iteration 历史；只在紧凑 context、results.tsv 和 Git 无法回答旧轮次的准确 metrics、failure 或 provenance 时调用。",
	search_list_allocation_decisions:
		"仅当当前 FrozenSpec 使用 orchestration_mode=adaptive_search 时，只读列出其持久化 allocation decisions；不启动、停止或继续 host worker。",
	search_apply_allocation_decision:
		"仅当当前 FrozenSpec 使用 orchestration_mode=adaptive_search 且 runtime 已返回准确 decision 时，整批幂等应用其剪枝与派生：预检全部 action，保留 Evidence，物化所有派生 workspace，并创建 native session provenance。Pi 主 agent 只在足够执行配额内原样启动全部返回 candidate。",
	search_invalidate_run:
		"主 agent 确认 verifier 契约、覆盖范围、确定性、目标对齐或基础设施失败后，原子地隔离该 run。随后中断每个 host worker，等待 active worker 数归零，修复并重新冻结，再使用 source_run_id 创建后继项。",
	search_report:
		"生成最终 report.md 和 report.html。对已链接的 Goal Plus run，只能在 Goal Plus 记录达到终态后调用且只调用一次；独立 Search 在提升后调用。active 的已链接 Goal Plus 记录会被拒绝。",
	search_plan_next:
		"规划初始候选。parallel_loops 模式下只能调用一次；后续工作恢复现有候选。标准流程令 requested_k 等于 max_parallel。",
	pi_search_pool_open:
		"打开持久化 Pi 候选 pool，并启动完整初始候选集合。启动后立即返回，并强制执行冻结的 max_parallel 限制。",
	pi_search_pool_wait_any:
		"等待任一 Pi pool worker 的终态事件。candidate_ready 要求 handle 已绑定、最低累计 lease 已满足且存在持久化验证证据；最低 lease 到硬上限仍未满足时返回 timed_out。已有当前产物的 durable Evidence 时不会重复运行父级 process verifier；只对 candidate_ready 应用后续继续策略。",
	pi_search_pool_snapshot:
		"无需等待即可检查持久化 Pi pool 状态、active worker、终态结果和空闲 slot。主 session 中断后传入 run_id 重新发现 pool，或传入 pool_id 指定准确 pool。",
	pi_search_pool_continue:
		"通过显式状态重新派发，在现有工作区恢复同一条自主 Pi 候选循环；可选传入另一份单次派发预算。",
	pi_search_pool_close:
		"通过 drain 或中断 active worker 来关闭 Pi pool。select/promote 前必须关闭 pool。",
};
const MAIN_GATED_TOOLS = new Set([
		"bash",
		"edit",
		"write",
		"pi_search_pool_open",
	"pi_search_pool_wait_any",
	"pi_search_pool_snapshot",
	"pi_search_pool_continue",
	"pi_search_pool_close",
	"pi_goal_plus_run_final_check",
	"pi_goal_plus_run_work_item",
]);
const MAIN_READ_ONLY_RUNTIME_TOOLS = new Set([
	"goal_plus_status",
	"goal_plus_monitor_snapshot",
	"goal_plus_list_models",
	"search_status",
	"search_list_history",
	"search_list_iterations",
	"search_get_agent_observability",
]);

function requiresGoalPlusAuthorization(toolName: string): boolean {
	if (MAIN_READ_ONLY_RUNTIME_TOOLS.has(toolName)) return false;
	return (
		toolName.startsWith("goal_plus_") ||
		toolName.startsWith("search_") ||
		toolName.startsWith("pi_search_pool_") ||
		toolName === "pi_goal_plus_run_final_check" ||
		toolName === "pi_goal_plus_run_work_item"
	);
}

interface GoalPlusNativeState {
	activeGoalPlusId?: string;
	continuationCount?: number;
	startedAt?: string;
	startEntryCount?: number;
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
	raw_goal?: string;
	command_config?: Record<string, unknown>;
	goal_revision?: number;
	goal_revisions?: unknown[];
	policy?: Record<string, unknown>;
	final_checks?: unknown[];
	status?: string;
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

interface CommandInvocation {
	command: string;
	argsPrefix: string[];
	label: string;
}

interface CommandRuntimeContext {
	cwd: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
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

function sourceRoot(ctx: CommandRuntimeContext): string {
	return sourcePath || ctx.cwd;
}

function projectModuleInvocation(ctx: CommandRuntimeContext, command: string, moduleName: string): CommandInvocation {
	const root = sourceRoot(ctx);
	const src = join(root, "src");
	const packageDir = join(src, "goal_plus");
	if (existsSync(packageDir)) {
		const code = [
			"import sys",
			`sys.path.insert(0, ${JSON.stringify(src)})`,
			`from ${moduleName} import main`,
			"raise SystemExit(main())",
		].join("; ");
		return { command: "python", argsPrefix: ["-c", code], label: `python -c ${moduleName}` };
	}
	return { command, argsPrefix: [], label: command };
}

function parseJsonObject(text: string): Record<string, unknown> | undefined {
	const trimmed = text.trim();
	if (!trimmed) return undefined;
	try {
		const parsed = JSON.parse(trimmed);
		return isRecord(parsed) ? parsed : undefined;
	} catch {
		return undefined;
	}
}

function isEnvironmentFailure(text: string): boolean {
	const normalized = text.toLowerCase();
	return (
		text.includes("ModuleNotFoundError") ||
		normalized.includes("no module named") ||
		normalized.includes("cannot find module") ||
		normalized.includes("command not found") ||
		normalized.includes("enoent") ||
		normalized.includes("not found:")
	);
}

function commandFailure(
	tool: string,
	invocation: CommandInvocation,
	result: { stdout: string; stderr: string; code: number },
): { text: string; details: Record<string, unknown> } {
	const output = (result.stderr || result.stdout || `${invocation.label} failed with exit code ${result.code}`).trim();
	const parsed = parseJsonObject(output);
	const baseError = typeof parsed?.error === "string" ? parsed.error : output;
	const text = isEnvironmentFailure(baseError) ? `${baseError}\n\n${INSTALL_HINT}` : baseError;
	return {
		text,
		details: {
			...(parsed ?? {}),
			tool: typeof parsed?.tool === "string" ? parsed.tool : tool,
			ok: false,
			error: text,
		},
	};
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

async function runJsonCli(pi: ExtensionAPI, ctx: CommandRuntimeContext, tool: string, args: Record<string, unknown>) {
	const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
	const result = await pi.exec(invocation.command, [
		...invocation.argsPrefix,
		"--root",
		runtimeRoot,
		"--args-json",
		JSON.stringify(args),
		tool,
	]);
	if (result.code !== 0) {
		const failure = commandFailure(tool, invocation, result);
		return {
			content: [{ type: "text" as const, text: failure.text }],
			details: failure.details,
		};
	}
	const parsed = JSON.parse(result.stdout || "null");
	return {
		content: [{ type: "text" as const, text: JSON.stringify(parsed) }],
		details: parsed,
	};
}

async function runHostCommandCli(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	tool: "goal_plus_host_start" | "goal_plus_host_edit" | "goal_plus_host_session_event",
	args: Record<string, unknown>,
) {
	const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
	const capabilityToken = randomUUID();
	const capabilityRoot = isAbsolute(runtimeRoot) ? runtimeRoot : join(ctx.cwd, runtimeRoot);
	const capabilityDir = join(capabilityRoot, "host-entrypoint", "pi");
	const capabilityPath = join(capabilityDir, `${capabilityToken}.json`);
	mkdirSync(capabilityDir, { recursive: true });
	writeFileSync(
		capabilityPath,
		JSON.stringify({ token: capabilityToken, issued_at_ms: Date.now() }),
		{ encoding: "utf8", flag: "wx", mode: 0o600 },
	);
	const result = await (async () => {
		try {
			return await pi.exec(invocation.command, [
				...invocation.argsPrefix,
				"--root",
				runtimeRoot,
				"--host-entrypoint",
				"--host-capability",
				capabilityToken,
				"--args-json",
				JSON.stringify(args),
				tool,
			]);
		} finally {
			rmSync(capabilityPath, { force: true });
		}
	})();
	if (result.code !== 0) {
		const failure = commandFailure(tool, invocation, result);
		return {
			content: [{ type: "text" as const, text: failure.text }],
			details: failure.details,
		};
	}
	const parsed = JSON.parse(result.stdout || "null");
	return {
		content: [{ type: "text" as const, text: JSON.stringify(parsed, null, 2) }],
		details: parsed,
	};
}

async function runPiHostSessionEvent(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	event: "session_start" | "user_turn" | "interrupt" | "continue_confirmed",
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
	action: "start" | "edit",
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
	if (!stateEntry?.data) return;
	activeGoalPlusId = stateEntry.data.activeGoalPlusId ?? activeGoalPlusId;
	continuationCount = stateEntry.data.continuationCount ?? continuationCount;
	activeGoalStartedAt = stateEntry.data.startedAt ?? activeGoalStartedAt;
	activeGoalStartEntryCount = stateEntry.data.startEntryCount ?? activeGoalStartEntryCount;
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
	return status === "blocked" || status === "complete" || status === "abandoned";
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
		`phase: ${status.phase ?? "unknown"}`,
		`goal_revision: ${status.goal_revision ?? 1}`,
		`command_config: ${JSON.stringify(status.command_config ?? {})}`,
		`final_check_policy: ${JSON.stringify(status.policy?.final_check ?? { mode: "disabled" })}`,
		"",
			"原始目标：",
		status.raw_goal ?? "",
		"",
			"规则：",
			"- command_config 中的字段是 runtime 强约束；只有未配置字段才由你判断。",
			"- 将原始目标与实现猜测分开。",
			"- 每次 phase 变化后更新 goal-plus 状态。",
			"- Search 是自主升级：spec draft 达到高置信度且无 open question 后，直接通过 Search Mode gate，不要请求用户批准。",
			"- 声称完成前，对照当前证据审计原始目标，并调用 goal_plus_set_status。",
			"- 如果 final_check.mode 为 required，必须由该目标修订版通过独立最终检查来完成。",
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
		"不要创建新 Goal Plus 记录，也不要重做已经结算的 phase。",
		buildGoalPlusContext(status),
	].join("\n\n");
}

interface GoalPlusRoleModels {
	annotator?: string;
	workerDirective?: "workers" | "models";
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
		workerDirective: workers ? "workers" : models ? "models" : undefined,
	};
}

function resolvePiModelReference(ctx: ExtensionContext, requested: string) {
	const needle = requested.toLowerCase();
	const available = ctx.modelRegistry.getAvailable();
	const canonical = available.filter(
		(model) => `${model.provider}/${model.id}`.toLowerCase() === needle,
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
): GoalPlusRoleModels | undefined {
	const requested = parseGoalPlusRoleModels(rawGoal);
	if (!requested) return undefined;
	const annotator = requested.annotator
		? resolvePiModelReference(ctx, requested.annotator)
		: undefined;
	return {
		annotator: annotator ? `${annotator.provider}/${annotator.id}` : undefined,
		workerDirective: requested.workerDirective,
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
			"- 冻结 SearchSpec 时，只写入显式角色：annotator 写入 strategy.evidence_annotator.model；workers=/models= 按原有分配规则写入 strategy.models。未指定的角色保持现有 host 默认或继承语义。",
		);
	}
	lines.push(
		"",
		"重要事项：",
		"- command_config 中的字段必须原样落实；只有未配置字段才允许自主判断。",
		"- 原生 Pi `/goal-plus` host command 已创建并授权此记录；skill、MCP 和模型工具不能创建或恢复 Goal Plus。",
		"- 这是新创建记录的首轮。由你语义检查原始目标或当前对话上下文是否明确给出了整个 Goal Plus 任务的最大运行时间；如果没有，先用一句简短 commentary 告知用户：‘当前未设置最大运行时间，Goal Plus 将持续运行直至目标完成或用户手动停止。’这只是告知，不要询问或等待确认；告知后立即继续执行。",
		"- 加载并遵循 goal-plus skill。",
		"- 以最新用户消息作为判断继续、修订或讨论无关内容的权威依据；不要仅因 Goal Plus 处于 active 就恢复工作。",
		"- 如果用户要改变实际范围、交付物或成功标准，停止本轮变更并要求使用精确的 `/goal-plus edit <完整修订目标>`；不要调用 goal_plus_update_goal。否则保持修订版不变。",
		"- 除了加载 goal-plus skill 之外，在 goal_plus_record_triage 前不要读取或审计目标文件。",
		"- 首先使用 goal_plus_record_triage 记录 triage。",
		"- 如果原始目标明确要求 verifier 引导的 Search Mode，并提供可度量的 verifier 或 metric，不要将其降级为普通 Goal Mode。",
		"- 对适合独立执行的普通工作，直接调用 pi_goal_plus_run_work_item 并传入完整 task；这是 Pi RPC subagent，不要调用 Codex spawn_agent，也不要预建工作 DAG。",
		"- 如果任务已准备好进入 Search，通过 frozen-spec 和 Search Mode gate 自主进入 Search Mode；不要要求用户批准该转换。",
		"- 绝不能编造 frozen_spec_id、run_id、plan_id、candidate_id 或 agent_session_id。只使用紧邻的前序运行时工具返回的准确 id；在 goal_plus_link_search_run 前调用 search_create。",
		"- 如果尚未准备好进入 Search，在 Goal Mode 中继续，并在停止前更新 goal-plus 状态。",
		"- 如果该记录要求最终检查，调用 goal_plus_prepare_final_check(checker_host=\"pi\")，然后把其 launch payload 传给 pi_goal_plus_run_final_check。",
	);
	return lines.join("\n");
}

interface GoalPlusSlashRequest {
	action: "start" | "edit" | "invalid";
	rawGoal: string;
	withFinalCheck: boolean;
	invalidReason?: string;
}

function goalPlusRequestFromSlashInput(text: string): GoalPlusSlashRequest | undefined {
	const match = text.match(/^\/(goal-plus(?:-with-final-check)?)(?:\s+([\s\S]*))?$/);
	if (!match) return undefined;
	const command = match[1];
	const body = (match[2] ?? "").trim();
	if (command === "goal-plus" && body.toLowerCase() === "resume") {
		return {
			action: "invalid",
			rawGoal: "",
			withFinalCheck: false,
			invalidReason:
				"Goal Plus has no /goal-plus resume subcommand. Resume the native Pi session with --session or --continue.",
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
		roleModels = resolveGoalPlusRoleModels(ctx, rawGoal);
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
		parseGoalPlusRoleModels(rawGoal);
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
	return [
			"用户已编辑 Goal Plus 目标。",
		`goal_plus_id: ${status.goal_plus_id}`,
		`goal_revision: ${status.goal_revision ?? "unknown"}`,
			"新的原始目标取代之前的目标。继续前重新运行 goal_plus_record_triage。",
			"以前的 Search 任务仅作为历史证据保留；不要把旧修订版的结果或最终检查视为当前结果。",
	].join("\n");
}

function startGoalPlusMainTurn(
	pi: ExtensionAPI,
	context: string,
	deliverAsFollowUp: boolean,
) {
	pi.sendMessage(
		{
			customType: GOAL_PLUS_COMMAND_CONTEXT_TYPE,
			content: context,
			display: false,
			details: {
				goal_plus_id: activeGoalPlusId,
				entrypoint_status: "host_verified",
			},
		},
		deliverAsFollowUp
			? { triggerTurn: true, deliverAs: "followUp" }
			: { triggerTurn: true },
	);
}

function registerRuntimeTool(pi: ExtensionAPI, name: string, registered: Set<string>) {
	if (registered.has(name)) return;
	registered.add(name);
	pi.registerTool({
		name,
		label: name,
		description: RuntimeToolDescriptions[name] ?? `调用 goal-plus facade 工具 ${name}。`,
		parameters: toolParameters(name),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const commandCtx = commandContextFrom(ctx);
			const startEntryCount = ctx.sessionManager.getEntries().length;
			const canPersistPiState = canPersistGoalState(ctx);
			const result = await runJsonCli(pi, commandCtx, name, params as Record<string, unknown>);
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
	const workspace = candidateTask.workspace;
	if (
		typeof agentSessionId !== "string" ||
		typeof workspace !== "string" ||
		(process.env.GOAL_PLUS_AGENT_SESSION_ID &&
			process.env.GOAL_PLUS_AGENT_SESSION_ID !== agentSessionId)
	) return;
	const firstActivation = !sawContext;
	workspaceRoot = workspace;
	sawContext = true;
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
	pi.registerTool({
		name: "pi_goal_plus_run_final_check",
		label: "Pi Goal Plus Final Check",
		description: "根据 goal_plus_prepare_final_check.launch 启动前台 Pi RPC 最终检查审查员。",
		parameters: toolParameters("pi_goal_plus_run_final_check"),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const commandCtx = commandContextFrom(ctx);
			const invocation = projectModuleInvocation(commandCtx, "goal-plus-pi-worker", "goal_plus.pi_worker");
			const launch = (params as { launch: Record<string, unknown> }).launch;
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
	pi.registerTool({
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
			const selectedThinking = selectedGoalPlusThinking(pi, ctx);
			const launch = {
				role: "ordinary",
				root: runtimeRoot,
				cwd: status?.source_path || ctx.cwd,
				agent_session_id: sessionId,
				session_id: sessionId,
				thinking_level: selectedThinking === "unavailable" ? undefined : selectedThinking,
				prompt: [
					"You are an implementation subagent. Work only on the assigned item.",
					`Task: ${input.task}`,
					"Do not coordinate other agents or use Goal Plus/Search tools. Return a concise result and verification evidence.",
				].join("\n\n"),
				budget_control: {
					max_runtime_seconds: input.max_runtime_seconds ?? 600,
					soft_closeout_seconds: 30,
				},
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
			};
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
					requested_reasoning_effort: selectedThinking,
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

function workspaceGuard(event: ToolCallEvent) {
	if (role === "final-checker" && ["edit", "write"].includes(event.toolName)) {
		return { block: true, reason: "最终检查审查员只能进行只读操作。" };
	}
	if (role !== "worker") return undefined;
	if (event.toolName === "search_get_agent_context") return undefined;
	if (!sawContext) {
		const readOnly = new Set(["read", "grep", "find", "ls"]);
		if (readOnly.has(event.toolName)) return undefined;
		return { block: true, reason: "使用变更类工具前调用 search_get_agent_context。" };
	}
	if (!workspaceRoot) return undefined;
	if (!["edit", "write", "bash"].includes(event.toolName)) return undefined;
	const target = extractCandidatePath(event);
	if (target && target.includes("..")) {
		return { block: true, reason: "workspaceGuard blocked parent-directory path." };
	}
	if (target && target.startsWith("/") && !target.startsWith(workspaceRoot)) {
		return { block: true, reason: "workspaceGuard blocked access outside candidate workspace." };
	}
	return undefined;
}

async function mainGate(event: ToolCallEvent, ctx: ExtensionContext) {
	if (role !== "main") return undefined;
	const requiresAuthorization = requiresGoalPlusAuthorization(event.toolName);
	if (requiresAuthorization && !goalPlusSessionAuthorized) {
		return {
			block: true,
			reason: "GoalPlusEntrypointRequired: this Pi session was not authorized by exact /goal-plus or /goal-plus edit input and is not the resumed native session.",
		};
	}
	if (!requiresAuthorization && !MAIN_GATED_TOOLS.has(event.toolName)) return undefined;
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
		context: { tool_name: event.toolName, input: event.input },
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
				details: { goal_plus_id: activeGoalPlusId, continuationCount },
			},
			{ triggerTurn: true, deliverAs: "followUp" },
		);
	}
}

async function settleMainGoal(pi: ExtensionAPI, ctx: ExtensionContext) {
	if (role !== "main" || !activeGoalPlusId || !goalPlusSessionAuthorized) return;
	const stopReason = lastMainStopReason;
	lastMainStopReason = undefined;
	mainTurnSignal = undefined;
	const persist = canPersistGoalState(ctx);
	if (stopReason === "error") return;
	if (stopReason === "aborted") {
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
		activeGoalPlusId = undefined;
		activeGoalStartedAt = undefined;
		activeGoalStartEntryCount = 0;
		continuationCount = 0;
		cachedGoalStatus = undefined;
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
	const GOAL_PLUS_COMMAND_CONFIG_USAGE = "[mode=autonomous|probe] [max_parallel=正整数] [max_duration_seconds=正整数（秒）] [workspace_backend=copy|git_worktree|thinkthread] [promotion_mode=apply|artifact_only] [strategy=NAME] [annotator=MODEL] [workers|models=MODEL[,MODEL...] 或 MODEL*COUNT[,MODEL*COUNT...]]";
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
	];
	const workerTools = ["search_get_agent_context"];
	const finalCheckerTools = ["goal_plus_status", "goal_plus_submit_final_check"];
	const roleTools = role === "worker"
		? workerTools
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
		return workspaceGuard(event) || (await mainGate(event, ctx));
	});
	pi.on("session_start", async (event, ctx) => {
		goalPlusSessionAuthorized = false;
		lastMainStopReason = undefined;
		mainTurnSignal = undefined;
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
				activeGoalPlusId = undefined;
				activeGoalStartedAt = undefined;
				activeGoalStartEntryCount = 0;
				continuationCount = 0;
				if (persist) persistGoalState(pi);
				return;
			}
			if (!status?.goal_plus_id) return;
			const interaction = piHostInteraction(ctx);
			const pendingUserInput = ctx.hasPendingMessages()
				|| (event.reason === "startup" && hasPiStartupInput());
			const delivery =
				event.reason !== "reload" && interaction === "interactive" && !pendingUserInput
					? "auto"
					: "next_user_turn";
			let decision = await runPiHostSessionEvent(
				pi,
				ctx,
				"session_start",
				delivery,
				interaction,
			);
			if (decision?.decision === "ask_user" && interaction === "interactive") {
				const confirmed = await ctx.ui.confirm(
					"Continue Goal Plus?",
					`Goal Plus ${status.goal_plus_id} is not terminal. Continue it in this Pi session?`,
				);
				if (confirmed) {
					decision = await runPiHostSessionEvent(
						pi,
						ctx,
						"continue_confirmed",
						"auto",
						"interactive",
					);
				}
			}
			if (decision?.decision === "continue_now") {
				goalPlusSessionAuthorized = true;
				activateGoal(pi, status, undefined, persist);
				startGoalPlusMainTurn(
					pi,
					buildNativeSessionContinuationPrompt(status),
					false,
				);
			}
		} catch {
			// Keep startup non-fatal; the next explicit tool call will surface runtime errors.
		}
	});
	pi.on("before_agent_start", async (_event, ctx) => {
		if (role !== "main" || !activeGoalPlusId || !goalPlusSessionAuthorized) return;
		const commandCtx = commandContextFrom(ctx);
		const status = await refreshActiveGoal(pi, commandCtx, canPersistGoalState(ctx));
		if (!status || isTerminalStatus(status.status)) return;
		return {
			message: {
				customType: "goal-plus-native-context",
				content: buildGoalPlusContext(status),
				display: false,
				details: { goal_plus_id: status.goal_plus_id, phase: status.phase, status: status.status },
			},
		};
	});
	pi.on("agent_start", async (_event, ctx) => {
		if (role === "main") mainTurnSignal = ctx.signal;
	});
	pi.on("agent_end", async (event, ctx) => {
		const lastMessage = event.messages.at(-1);
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
			return;
		}
		if (ctx.hasPendingMessages()) return;
		if (role === "worker") {
			if (lengthWithoutToolCall) return;
			if (workerAllocationDecisionId) return;
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
}
