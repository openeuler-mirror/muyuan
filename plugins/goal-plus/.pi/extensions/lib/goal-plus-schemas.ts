import { type TSchema, Type } from "typebox";

export const LooseObject = Type.Object({}, { additionalProperties: true });

export const ThinkThreadArtifactRef = Type.Object(
	{
		kind: Type.Union([Type.Literal("git_commit"), Type.Literal("fs_snapshot")]),
		id: Type.String({ minLength: 1 }),
		provider: Type.String({ minLength: 1 }),
	},
	{ additionalProperties: false },
);

export const ThinkThreadSupplementalDimension = Type.Object(
	{
		name: Type.String({ minLength: 1, maxLength: 120 }),
		finding: Type.String({ minLength: 1, maxLength: 1000 }),
		confidence: Type.Union([Type.Literal("high"), Type.Literal("medium"), Type.Literal("low")]),
		evidence: Type.Array(Type.String(), { maxItems: 8 }),
	},
	{ additionalProperties: false },
);

export const ThinkThreadPeerComparison = Type.Object(
	{
		candidate_id: Type.String({ minLength: 1 }),
		iteration: Type.Integer({ minimum: 1 }),
		commit: Type.Union([Type.String({ minLength: 1 }), Type.Null()]),
		artifact_ref: Type.Union([ThinkThreadArtifactRef, Type.Null()]),
		relation: Type.Union([
			Type.Literal("similar"),
			Type.Literal("different"),
			Type.Literal("tradeoff"),
			Type.Literal("complementary"),
			Type.Literal("unknown"),
		]),
		rationale: Type.String({ minLength: 1, maxLength: 1000 }),
		evidence: Type.Array(Type.String(), { maxItems: 8 }),
	},
	{ additionalProperties: false },
);

export const ThinkThreadSupplementalEvaluation = Type.Object(
	{
		summary: Type.String({ minLength: 1, maxLength: 1000 }),
		dimensions: Type.Array(ThinkThreadSupplementalDimension, { minItems: 1, maxItems: 8 }),
		comparisons: Type.Array(ThinkThreadPeerComparison, {
			maxItems: 8,
			description: "Copy candidate_id, iteration, commit, and artifact_ref exactly from the current task comparison_basis, in the same order.",
		}),
		limitations: Type.Array(Type.String(), { maxItems: 8 }),
	},
	{ additionalProperties: false },
);

export const ThinkThreadToolView = Type.Object(
	{
		tool_id: Type.String({ minLength: 1 }),
		summary: Type.String({ minLength: 1, maxLength: 1000 }),
		capabilities: Type.Array(Type.String(), { maxItems: 16 }),
		when_to_use: Type.String({ minLength: 1, maxLength: 1000 }),
		entrypoint: Type.Union([Type.String({ maxLength: 500 }), Type.Null()]),
		inputs: Type.Array(Type.String(), { maxItems: 16 }),
		outputs: Type.Array(Type.String(), { maxItems: 16 }),
		dependencies: Type.Array(Type.String(), { maxItems: 16 }),
		adoption_steps: Type.Array(Type.String(), { maxItems: 16 }),
		limitations: Type.Array(Type.String(), { maxItems: 16 }),
	},
	{ additionalProperties: false },
);

export const GoalPlusConfidence = Type.Union([Type.Literal("high"), Type.Literal("medium"), Type.Literal("low")]);

export const GoalPlusRecommendedPhase = Type.Union([
	Type.Literal("goal"),
	Type.Literal("spec_discovery"),
	Type.Literal("search"),
]);

export const GoalPlusDiscoveryOrigin = Type.Union([Type.Literal("initial"), Type.Literal("in_progress")]);

export const GoalPlusFinalCheckerHost = Type.Union([Type.Literal("codex"), Type.Literal("pi")]);

export const GoalPlusTriage = Type.Object(
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

export const GoalPlusNextAction = Type.Object(
	{
		kind: Type.String(),
		description: Type.String(),
		required: Type.Optional(Type.Boolean()),
		metadata: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);

export const GoalPlusWorkEvent = Type.Union([
	Type.Literal("dispatch"),
	Type.Literal("bind"),
	Type.Literal("message"),
	Type.Literal("result"),
	Type.Literal("rework"),
	Type.Literal("accepted"),
	Type.Literal("failed"),
	Type.Literal("cancelled"),
]);

export const PositiveInteger = Type.Integer({ exclusiveMinimum: 0 });

export const NullableString = Type.Union([Type.String(), Type.Null()]);

export const NullablePositiveInteger = Type.Union([PositiveInteger, Type.Null()]);

export const VerifierRole = Type.Union([
	Type.Literal("validity_gate"),
	Type.Literal("process_gate"),
	Type.Literal("ranking_signal"),
	Type.Literal("diagnostic_signal"),
	Type.Literal("promotion_gate"),
	Type.Literal("anti_cheat_gate"),
]);

export const FeedbackPolicy = Type.Union([
	Type.Literal("visible_to_workers"),
	Type.Literal("summary_only"),
	Type.Literal("final_only"),
]);

export const VerifierCommand = Type.Object(
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

export const EditSurface = Type.Object(
	{
		allow: Type.Array(Type.String(), { minItems: 1 }),
		deny: Type.Optional(Type.Array(Type.String())),
		max_file_changes: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);

export const DevicePoolSpec = Type.Object(
	{
		pool_id: Type.String({
			minLength: 1,
			pattern: "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
		}),
	},
	{ additionalProperties: false },
);

export const SearchBudget = Type.Object(
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

export const WorkerBudget = Type.Object(
	{
		max_runtime_seconds: Type.Optional(NullablePositiveInteger),
		max_turns: Type.Optional(NullablePositiveInteger),
		on_exceed: Type.Optional(Type.Literal("interrupt")),
		min_runtime_seconds: Type.Optional(NullablePositiveInteger),
		min_verifier_runs: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);

export const SearchSchedulerSpec = Type.Object(
	{
		model: Type.String({ minLength: 1 }),
		reasoning_effort: Type.String({ minLength: 1 }),
		timeout_seconds: PositiveInteger,
		reward: Type.String({ minLength: 1 }),
		allocation: Type.String({ minLength: 1 }),
	},
	{ additionalProperties: false },
);

export const CandidateProposal = Type.Object(
	{
		intent: Type.String({ minLength: 1 }),
		hypothesis: Type.Optional(NullableString),
		expected_tradeoff: Type.Optional(Type.String()),
		instructions: Type.Optional(Type.Array(Type.String())),
		metadata: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);

export const WorkerLaunch = Type.Object(
	{
		model: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		service_tier: Type.Optional(NullableString),
	},
	{ additionalProperties: false },
);

export const EvidenceAnnotator = Type.Object(
	{
		model: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		timeout_seconds: Type.Optional(PositiveInteger),
	},
	{ additionalProperties: false },
);

export const ModelSpec = Type.Object(
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

export const StrategySpec = Type.Object(
	{
		name: Type.Optional(Type.String({ minLength: 1 })),
		orchestration_mode: Type.Optional(
			Type.Union([
				Type.Literal("parallel_loops"),
				Type.Literal("adaptive_search"),
			]),
		),
		inner_agent: Type.Optional(Type.String({ minLength: 1 })),
		worker_budget: Type.Optional(Type.Union([WorkerBudget, Type.Null()])),
		worker_launch: Type.Optional(Type.Union([WorkerLaunch, Type.Null()])),
		evidence_annotator: Type.Optional(EvidenceAnnotator),
		models: Type.Optional(Type.Array(ModelSpec)),
		search_scheduler: Type.Optional(Type.Union([SearchSchedulerSpec, Type.Null()])),
		config: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);

export const WorkspaceSpec = Type.Object(
	{
		backend: Type.Optional(Type.Union([
			Type.Literal("copy"),
			Type.Literal("git_worktree"),
			Type.Literal("thinkthread"),
		])),
		promotion_mode: Type.Optional(Type.Union([Type.Literal("apply"), Type.Literal("artifact_only")])),
		dependencies: Type.Optional(Type.Array(Type.Object(
			{
				path: Type.String({ minLength: 1 }),
				scope: Type.Union([
					Type.Literal("content"),
					Type.Literal("entries"),
					Type.Literal("tree_entries"),
					Type.Literal("tree_content"),
				]),
			},
			{ additionalProperties: false },
		), { maxItems: 256 })),
	},
	{ additionalProperties: false },
);

export const SharedDirSpec = Type.Object(
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

export const ToolizationSignal = Type.Union([
	Type.Literal("repeated_workflow"),
	Type.Literal("domain_construction_or_probe"),
	Type.Literal("behavior_or_invariant_checker"),
	Type.Literal("reproducer_fixture_or_case_generator"),
	Type.Literal("parser_trace_or_comparator"),
	Type.Literal("peer_setup_or_feedback_reduction"),
]);

export const ToolizationExclusion = Type.Union([
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

export const ToolizationDecision = Type.Object(
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

export const SearchSpecSchema = Type.Object(
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

export const SearchSpecDraftSchema = Type.Partial(SearchSpecSchema);

export const GoalPlusSpecDraft = Type.Object(
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

export const RuntimeToolSchemas: Record<string, TSchema> = {
	goal_plus_create: Type.Object(
		{
			raw_goal: Type.String(),
			source_path: Type.Optional(NullableString),
			policy: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	goal_plus_status: Type.Object({
		goal_plus_id: Type.String(),
		detail: Type.Optional(Type.Boolean({ default: false })),
	}, { additionalProperties: false }),
	goal_plus_history: Type.Object({
		goal_plus_id: Type.String(),
		collection: Type.Union([Type.Literal("goal_revisions"), Type.Literal("work_items"),
			Type.Literal("search_tasks"), Type.Literal("final_checks"), Type.Literal("events")]),
		offset: Type.Optional(Type.Integer({ minimum: 0 })),
		limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
	}, { additionalProperties: false }),
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
			detail: Type.Optional(Type.Boolean({ default: false })),
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
			goal_plus_id: Type.String(),
			spec: SearchSpecSchema,
			verifier_artifact_paths: Type.Array(Type.String()),
		},
		{ additionalProperties: false },
	),
	search_create: Type.Object(
		{
			goal_plus_id: Type.String(),
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
	search_recovery_status: Type.Object({ run_id: Type.String() }, { additionalProperties: false }),
	search_prepare_recovery: Type.Object({
		run_id: Type.String(), candidate_id: Type.String(),
	}, { additionalProperties: false }),
	search_recover_candidate: Type.Object({
		run_id: Type.String(), candidate_id: Type.String(), recovery_id: Type.String(),
	}, { additionalProperties: false }),
		search_redispatch_candidate: Type.Object(
			{
				run_id: Type.String(),
				candidate_id: Type.String(),
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
			detail: Type.Optional(Type.Boolean({ default: false })),
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
	thinkthread_search_pool_open: Type.Object(
		{
			run_id: Type.String(),
			candidate_ids: Type.Optional(Type.Array(Type.String())),
			worker_budgets: Type.Optional(Type.Record(Type.String(), WorkerBudget)),
			max_parallel: Type.Optional(PositiveInteger),
		},
		{ additionalProperties: false },
	),
	thinkthread_search_pool_wait_any: Type.Object(
		{
			pool_id: Type.String(),
			timeout_seconds: Type.Optional(Type.Number({ minimum: 0 })),
		},
		{ additionalProperties: false },
	),
	thinkthread_search_pool_snapshot: Type.Object(
		{
			pool_id: Type.Optional(Type.String()),
			run_id: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	thinkthread_search_pool_continue: Type.Object(
		{
			pool_id: Type.String(),
			candidate_id: Type.String(),
			worker_budget: Type.Optional(WorkerBudget),
		},
		{ additionalProperties: false },
	),
	thinkthread_search_pool_close: Type.Object(
		{
			pool_id: Type.String(),
			mode: Type.Optional(Type.Union([Type.Literal("drain"), Type.Literal("interrupt")])),
			timeout_seconds: Type.Optional(Type.Number({ minimum: 0 })),
		},
		{ additionalProperties: false },
	),
	thinkthread_annotation_next: Type.Object(
		{ run_id: Type.String() },
		{ additionalProperties: false },
	),
	thinkthread_artifact_view_list: Type.Object(
		{
			view_id: Type.String(),
			path: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	thinkthread_artifact_view_stat: Type.Object(
		{
			view_id: Type.String(),
			path: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	thinkthread_artifact_view_pread: Type.Object(
		{
			view_id: Type.String(),
			path: Type.String(),
			offset: Type.Optional(Type.Integer({ minimum: 0 })),
			length: Type.Optional(Type.Integer({ minimum: 1, maximum: 65536 })),
		},
		{ additionalProperties: false },
	),
	thinkthread_artifact_view_diff: Type.Object(
		{
			base_view_id: Type.String(),
			target_view_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	thinkthread_annotation_submit: Type.Object(
		{
			description: Type.String({ minLength: 1, maxLength: 1000 }),
			supplemental_evaluation: Type.Optional(
				Type.Union([ThinkThreadSupplementalEvaluation, Type.Null()]),
			),
			tool_views: Type.Optional(Type.Array(ThinkThreadToolView)),
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

export const RuntimeToolDescriptions: Record<string, string> = {
	goal_plus_status: "默认读取当前 revision、control、待办和 next_action。初始化、恢复或 revision 变化时用 detail=true 读取完整 raw_goal、command_config、policy、triage 和 spec_draft；历史详情用 goal_plus_history。",
	goal_plus_history: "按 collection 分页读取 Goal revision、dispatch 完整证据、Search 结果、final check 或审计事件。默认每页 20 条，最多 100 条；按 next_offset 继续，null 表示读完。",
	goal_plus_monitor_snapshot: "默认返回当前进度、错误和恢复待办；配置、逐 candidate/session 状态和详细诊断用 detail=true。只读，不执行 worker lifecycle 操作。",
	search_recovery_status: "读取 Search 各 candidate 的结算检查点和旧 worker 绑定，不修改运行状态。",
	search_prepare_recovery: "固定中断 candidate 的恢复点并 fence 旧 worker。随后关闭旧 pool，再调用 search_recover_candidate。",
	search_recover_candidate: "核对真实 host 静默后，通过 workspace provider 恢复结算点；幂等保留 Evidence。随后用新 pool/fresh worker 继续原 candidate。",
	goal_plus_record_work_event:
		"按实际发生记录带 attempt/generation fencing 的 subagent 派发、绑定、消息、结果和主 Agent 决定；首次 dispatch 自动创建记录。",
	goal_plus_save_spec_draft:
		"保存发现的 SearchSpec draft。spec_draft 顶层只接受 baseline、metric、correctness_gate、edit_surface、verifier_artifacts、search_spec、promotion_rule、confidence、origin 和 open_questions；candidate_acceptance、constraints、root_hypotheses、strategy、workspace 等 SearchSpec 字段必须放在 spec_draft.search_spec。固定候选使用 orchestration_mode=parallel_loops；明确需要 runtime reward/allocation 时改用 orchestration_mode=adaptive_search 并设置非 null 的 search_scheduler。max_parallel 是实时并发上限；可选 max_candidates 是累计唯一 candidate 上限，null 表示不限制。",
	goal_plus_set_status:
		"根据真实证据设置 Goal Plus 状态。当前优化/Search revision 必须先通过 goal_plus_record_search_result 记录已提升且已按需 apply 的结果，才能设置 complete；没有 verified candidate 且固定 deadline 已阻止继续工作时应记录 blocked，不得声称完成。",
	search_freeze_spec:
		"使用当前 session 的 goal_plus_id 冻结不可变的 SearchSpec、verifier bundle 和 source artifact。本地 process verifier suite 必须实际输出有限数值分数；分数从 stdout 的单行 JSON 对象读取，例如 {\"score\":123.0}，字段与 spec.metric_name 对应。文本分数或写入文件的 JSON 不会被解析。Git backend 先在 clean origin 运行 process verifier；第一次 search_start_batch 再在 c001 worktree 验证 relocation，通过后才创建其余 lane。bootstrap 结果不是 candidate iteration。并发 Search 下 verifier 临时文件必须放入唯一的 GOAL_PLUS_VERIFIER_TMPDIR/TMPDIR，绝不能使用固定 /tmp 路径。",
	search_create:
		"使用与 freeze 相同的当前 goal_plus_id 从 frozen_spec_id 创建 Search run。初始 run 必须省略 source_run_id，或在 strict schema 下传 null；仅在已有真实前驱时传入准确的 run_* ID，绝不能传 initial 或 in_progress。",
	search_get_agent_context:
		"读取当前 worker 的紧凑权威 candidate 上下文，包括 candidate_task.instructions、冻结验收合同、进程环境、最近三轮与最佳轮次、canonical .gp ledger 来源、可选 results_tsv projection 和条件能力。",
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
		"为一个 candidate attempt 评分。worker process verifier 必须提供一句话 hypothesis 并遵守 candidate_task.instructions；shared_dir 启用时还必须提交 toolization_decision：staged 至少包含一个当前 capability-oriented signal 和实际 tool_names；not_applicable 必须给出具体 exclusion，不能只写不复用。搜索期行为 checker、功能验证函数、fixture/case、复现、差分和不变量检查是正向候选，不因 test_ 命名而排除。runtime 以 staging inventory 和 publication settlement 为权威；工具化 review、staging 或 decision 的缺失和不一致只记录为 monitor/report advisory，不改变结算、硬 score、选择或 promotion。工具化目标仅是降低同一 run 内 peer 重建诊断流程的成本，不要求跨项目通用。canonical .gp ledger 为每份报告追加且只追加一条已验证记录。内置 autoresearch binding 使用 AutoResearch-Git：普通 Git/Pi RPC 按 binding 同步并提交 workspace results_tsv projection；ThinkThread 由 trusted extension 维护 Child-private Git 和 results.tsv，但 public git_head 为空且 outer Evidence 只认 exact fs_snapshot。common binding 不创建 results.tsv。Git/Pi RPC 同步返回 ScoreReport；ThinkThread Child 即时只返回带稳定 attempt_id 的 accepted receipt，Child 随即结束 turn，完整 ScoreReport 会在同一 retained Child 的下一次 wake 中注入。无 accepted attempt 的 ThinkThread turn 不会被自动验证。每份已知 verifier 通过、失败、超时或执行错误都形成记录；completion-unknown 进入 provider recovery，不伪造结果。process verifier 产生 keep/retain/discard/failure disposition；严格改善为 keep，同分为 retain 并成为 candidate-local 最新基线，只有退化或验证失败时恢复此前硬分最佳。开放式补充评价和动态 peer 比较不改变结算、硬 score 或最终 PASS/FAIL。带 candidate_action=stop_and_report 的 VerifierWorkspaceSideEffect 属于基础设施失败：worker 必须停止，不能清理或重试，使父级能够修复并重新冻结。",
	search_list_iterations:
		"返回当前 agent session 所属 candidate 可能很大的全量 iteration 历史，并且只在紧凑 context、可选 ledger projection 和 Git 无法回答旧轮次的准确 metrics、failure 或 provenance 时调用；不用于常规刷新。",
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
	thinkthread_search_pool_open:
		"从 runtime 已捕获的同一 exact S0 启动完整 ThinkThread Pi candidate 集合；每个 candidate 使用独立 Child、Session 和 private branch。",
	thinkthread_search_pool_wait_any:
		"等待 ThinkThread candidate turn 结束；candidate_ready 仅在已接受的 attempt 捕获 exact snapshot、记录完整 ScoreReport，并在 discard/failure 时完成 branch reset 后返回，同时携带 typed attempt_result。",
	thinkthread_search_pool_snapshot:
		"按 pool_id 或 run_id 读取持久化 ThinkThread host pool 状态，不读取 Child live workspace。",
	thinkthread_search_pool_continue:
		"唤醒同一 retained ThinkThread Child 和 Session，继续同一 candidate private branch。",
	thinkthread_search_pool_close:
		"关闭或中断 ThinkThread candidate pool，并仅清理该 pool 拥有的 Child 与 branch。select 前必须关闭。",
	thinkthread_annotation_next:
		"仅供 retained ThinkThread Annotation Child 领取下一条由 Runtime settlement 已录取的 immutable Evidence task；candidate Child 调用会被 Root 拒绝。",
	thinkthread_artifact_view_list:
		"列出当前 annotation task 授权的 target/peer immutable snapshot 目录。",
	thinkthread_artifact_view_stat:
		"读取当前 annotation task 授权的 Artifact View 路径元数据。",
	thinkthread_artifact_view_pread:
		"分块读取当前 annotation task 授权的 Artifact View 文件；可遍历完整 workspace，不回退 Root live tree。",
	thinkthread_artifact_view_diff:
		"比较当前 annotation task 冻结的两个 target/peer snapshot Artifact View。",
	thinkthread_annotation_submit:
		"提交当前已领取 Evidence task 的结构化 annotation；Artifact View 读取记录不是 tool_views。supplemental evaluation 启用时必须提交该对象，并将 comparisons 的 candidate_id/iteration/commit/artifact_ref 按原顺序逐字段复制当前 task 的 comparison_basis；Root 会再次检查 queue generation/revision，拒绝 selection 后的 late publication。",
};
