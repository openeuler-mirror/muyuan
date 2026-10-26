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


export const RuntimeToolSchemas: Record<string, TSchema> = {
	goal_plus_thinkthread_verifier_input: Type.Object({}, { additionalProperties: false }),
	goal_plus_thinkthread_verifier_submit: Type.Object(
		{ result: Type.Record(Type.String(), Type.Unknown()) }, { additionalProperties: false },
	),
	goal_plus_thinkthread_annotation_next: Type.Object(
		{ run_id: Type.String() },
		{ additionalProperties: false },
	),
	goal_plus_thinkthread_artifact_view_list: Type.Object(
		{
			view_id: Type.String(),
			path: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_thinkthread_artifact_view_stat: Type.Object(
		{
			view_id: Type.String(),
			path: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_thinkthread_artifact_view_pread: Type.Object(
		{
			view_id: Type.String(),
			path: Type.String(),
			offset: Type.Optional(Type.Integer({ minimum: 0 })),
			length: Type.Optional(Type.Integer({ minimum: 1, maximum: 65536 })),
		},
		{ additionalProperties: false },
	),
	goal_plus_thinkthread_artifact_view_diff: Type.Object(
		{
			base_view_id: Type.String(),
			target_view_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	goal_plus_thinkthread_annotation_submit: Type.Object(
		{
			description: Type.String({ minLength: 1, maxLength: 1000 }),
			supplemental_evaluation: Type.Optional(
				Type.Union([ThinkThreadSupplementalEvaluation, Type.Null()]),
			),
			tool_views: Type.Optional(Type.Array(ThinkThreadToolView)),
		},
		{ additionalProperties: false },
	),
};

export const RuntimeToolDescriptions: Record<string, string> = {
	goal_plus_thinkthread_verifier_input: "Read this Verifier assessment's fixed instructions, evidence and output schema.",
	goal_plus_thinkthread_verifier_submit: "Submit this assessment's complete result matching the supplied output schema, then end the turn.",
	goal_plus_thinkthread_annotation_next: "仅供 retained ThinkThread Annotation Child 领取下一条由 Runtime settlement 已录取的 immutable Evidence task；candidate Child 调用会被 Root 拒绝。",
	goal_plus_thinkthread_artifact_view_list: "列出当前 annotation task 授权的 target/peer immutable snapshot 目录。",
	goal_plus_thinkthread_artifact_view_stat: "读取当前 annotation task 授权的 Artifact View 路径元数据。",
	goal_plus_thinkthread_artifact_view_pread: "分块读取当前 annotation task 授权的 Artifact View 文件；可遍历完整 workspace，不回退 Root live tree。",
	goal_plus_thinkthread_artifact_view_diff: "比较当前 annotation task 冻结的两个 target/peer snapshot Artifact View。",
	goal_plus_thinkthread_annotation_submit: "提交当前已领取 Evidence task 的结构化 annotation；Artifact View 读取记录不是 tool_views。supplemental evaluation 启用时必须提交该对象，并将 comparisons 的 candidate_id/iteration/commit/artifact_ref 按原顺序逐字段复制当前 task 的 comparison_basis；Root 会再次检查 queue generation/revision，拒绝 selection 后的 late publication。",
};
