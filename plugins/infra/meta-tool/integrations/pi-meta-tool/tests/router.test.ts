import assert from "node:assert/strict";
import test from "node:test";
import {
	metaToolFingerprint,
	routedCatalogPrompt,
	routingTokens,
	searchMetaTools,
} from "../src/router.ts";
import type { MetaToolCatalog, ProjectProfile } from "../src/types.ts";

const catalog: MetaToolCatalog = {
	meta_tools: [
		{
			name: "reddit_navigation",
			description: "Open a Reddit post and inspect comments",
			input_schema: {
				type: "object",
				properties: { post: { type: "string" } },
			},
			steps: [
				{ id: "step_1", tool: "click", arguments: { target: "posts" } },
				{
					id: "step_2",
					tool: "click",
					arguments: { target: { $input: "post" } },
				},
			],
			output: { $step: "step_2", path: [] },
			metadata: {
				task_goals: ["find a Reddit post and read its comments"],
				project_tags: ["webarena", "browser-automation"],
				support: 8,
				sequence_purity: 1,
				execution_guard: "none",
				catalog_scope: "project",
				catalog_version: 4,
			},
		},
		{
			name: "torchbench_profile",
			description:
				"Profile a TorchBench training model and compare DVM metrics",
			input_schema: { type: "object" },
			steps: [
				{ id: "step_1", tool: "bash", arguments: { command: "baseline" } },
				{ id: "step_2", tool: "read", arguments: { path: "metrics.json" } },
			],
			output: { $step: "step_2", path: [] },
			metadata: {
				task_goals: ["optimize PyTorch TorchBench DVM training performance"],
				project_tags: ["pytorch", "torchbench", "dvm"],
				support: 10,
				sequence_purity: 1,
				execution_guard: "confirm_each_run",
			},
		},
	],
};

const profile: ProjectProfile = {
	cwd: "/work/reddit",
	tags: ["webarena", "browser-automation"],
	providers: ["webarena-reddit"],
	detectedAt: new Date(0).toISOString(),
};

test("router selects task-relevant workflows and emits execution pins", () => {
	const matches = searchMetaTools({
		catalog,
		catalogVersion: 4,
		query: "Open the Reddit post and inspect the latest comments",
		profile,
		supportedTools: new Set(["click", "read", "bash"]),
		topK: 5,
		minScore: 0.1,
	});
	assert.deepEqual(
		matches.map((match) => match.name),
		["reddit_navigation"],
	);
	assert.equal(matches[0]?.catalogScope, "project");
	assert.equal(matches[0]?.catalogVersion, 4);
	assert.equal(matches[0]?.fingerprint.length, 64);
	const prompt = routedCatalogPrompt(matches, 5_000);
	assert.match(prompt, /reddit_navigation@project:v4/);
	assert.match(prompt, /run-pin=.*fingerprint/);
	assert.doesNotMatch(prompt, /torchbench_profile/);
});

test("router hard-filters unavailable and explicitly required primitive tools", () => {
	assert.equal(
		searchMetaTools({
			catalog,
			catalogVersion: 4,
			query: "TorchBench DVM profile",
			profile,
			supportedTools: new Set(["read"]),
			topK: 5,
			minScore: 0,
		}).some((match) => match.name === "torchbench_profile"),
		false,
	);
	assert.equal(
		searchMetaTools({
			catalog,
			catalogVersion: 4,
			query: "Reddit comments",
			profile,
			supportedTools: new Set(["click", "read"]),
			topK: 5,
			minScore: 0,
			requiredTools: new Set(["read"]),
		}).length,
		0,
	);
});

test("router omits degraded fingerprints", () => {
	const first = searchMetaTools({
		catalog,
		catalogVersion: 4,
		query: "Reddit comments",
		profile,
		supportedTools: new Set(["click", "read"]),
		topK: 5,
		minScore: 0,
	})[0];
	assert.ok(first);
	assert.equal(
		searchMetaTools({
			catalog,
			catalogVersion: 4,
			query: "Reddit comments",
			profile,
			supportedTools: new Set(["click", "read"]),
			topK: 5,
			minScore: 0,
			blockedFingerprints: new Set([first.fingerprint]),
		}).some((match) => match.name === first.name),
		false,
	);
});

test("router hard-filters primitive schema drift when provenance is available", () => {
	const reddit = catalog.meta_tools[0];
	assert.ok(reddit);
	const schemaCatalog: MetaToolCatalog = {
		meta_tools: [
			{
				...reddit,
				metadata: {
					...reddit.metadata,
					required_tool_schema_fingerprints: { click: "schema-v1" },
				},
			},
		],
	};
	assert.equal(
		searchMetaTools({
			catalog: schemaCatalog,
			catalogVersion: 4,
			query: "Reddit comments",
			profile,
			supportedTools: new Set(["click"]),
			toolSchemaFingerprints: new Map([["click", "schema-v2"]]),
			topK: 5,
			minScore: 0,
		}).length,
		0,
	);
	assert.equal(
		searchMetaTools({
			catalog: schemaCatalog,
			catalogVersion: 4,
			query: "Reddit comments",
			profile,
			supportedTools: new Set(["click"]),
			toolSchemaFingerprints: new Map([["click", "schema-v1"]]),
			topK: 5,
			minScore: 0,
		}).length,
		1,
	);
	const conflicting: MetaToolCatalog = {
		meta_tools: [
			{
				...reddit,
				metadata: {
					...reddit.metadata,
					tool_schema_conflicts: { click: ["schema-v1", "schema-v2"] },
				},
			},
		],
	};
	assert.equal(
		searchMetaTools({
			catalog: conflicting,
			catalogVersion: 4,
			query: "Reddit comments",
			profile,
			supportedTools: new Set(["click"]),
			topK: 5,
			minScore: 0,
		}).length,
		0,
	);
});

test("routing tokenizer preserves Chinese bigrams", () => {
	const tokens = routingTokens("优化训练模型性能");
	assert.ok(tokens.has("优化"));
	assert.ok(tokens.has("训练"));
	assert.ok(tokens.has("性能"));
});

test("quality evidence cannot route a semantically unrelated workflow", () => {
	const highSupport: MetaToolCatalog = {
		meta_tools: catalog.meta_tools.map((candidate) => ({
			...candidate,
			metadata: {
				...candidate.metadata,
				support: 10_000,
				sequence_purity: 1,
				execution_success_rate: 1,
			},
		})),
	};
	assert.equal(
		searchMetaTools({
			catalog: highSupport,
			catalogVersion: 4,
			query: "Rotate production database credentials",
			profile,
			supportedTools: new Set(["click", "read", "bash"]),
			topK: 5,
			minScore: 0,
		}).length,
		0,
	);
});

test("workflow fingerprints are independent of JSON object insertion order", () => {
	const candidate = catalog.meta_tools[0];
	assert.ok(candidate);
	const reordered = {
		...candidate,
		input_schema: {
			properties: { post: { type: "string" } },
			type: "object",
		},
	};
	assert.equal(metaToolFingerprint(candidate), metaToolFingerprint(reordered));
});
