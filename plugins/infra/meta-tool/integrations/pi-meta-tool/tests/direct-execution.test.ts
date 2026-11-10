import assert from "node:assert/strict";
import test from "node:test";
import { directExecutionAllowed, executeDirectMetaTool } from "../src/direct-execution.ts";
import { readBatchLayout } from "../src/read-batches.ts";
import type { MetaToolSpec } from "../src/types.ts";

function workflow(): MetaToolSpec {
	return {
		name: "fixture", description: "read a resolved record",
		input_schema: { type: "object", properties: {
			limit: { type: "integer", minimum: 1, maximum: 300 },
			options: { type: "object", properties: {
				kind: { enum: ["brief", "full"] },
				tags: { type: "array", items: { type: "string" }, minItems: 1 },
			}, required: ["kind", "tags"], additionalProperties: false },
		}, required: ["limit", "options"], additionalProperties: false },
		steps: [
			{ id: "lookup", tool: "lookup", arguments: { limit: { $input: "limit" } } },
			{ id: "read", tool: "read", arguments: { id: { $step: "lookup", path: ["id"] } } },
		], output: { $step: "read" },
		metadata: { execution_guard: "none", side_effect_free: true, reviewed: true,
			internal_data_flows: 1, varying_data_flows: 1, sequence_purity: 1,
			min_branch_dominance: 1, llm_turn_coverage: 1,
			mean_llm_calls_saved_per_observed_occurrence: 1 },
	};
}

test("invalid nested inputs never reach a primitive", async () => {
	for (const inputs of [
		{ limit: 9999, options: { kind: "brief", tags: ["a"] } },
		{ limit: "not-an-integer", options: { kind: "brief", tags: ["a"] } },
		{ limit: 1, options: { kind: "other", tags: ["a"] } },
		{ limit: 1, options: { kind: "brief", tags: [3] } },
		{ limit: 1, options: { kind: "brief", tags: [] } },
		{ limit: 1, options: { kind: "brief" } },
		{ limit: 1, options: { kind: "brief", tags: ["a"], extra: true } },
	]) {
		let calls = 0;
		await assert.rejects(() => executeDirectMetaTool(workflow(), inputs, async () => { calls++; }));
		assert.equal(calls, 0);
	}
});

test("valid inputs are unchanged and cross-step outputs still bind", async () => {
	const inputs = { limit: 2, options: { kind: "brief", tags: ["a"] } };
	const before = structuredClone(inputs), calls: unknown[] = [];
	const spec = workflow();
	assert(directExecutionAllowed(spec, "safe_auto", {}));
	assert.deepEqual(await executeDirectMetaTool(spec, inputs, async (_, tool, args) => {
		calls.push([tool, args]);
		return tool === "lookup" ? { id: "live" } : { found: true };
	}), { found: true });
	assert.deepEqual(calls, [["lookup", { limit: 2 }], ["read", { id: "live" }]]);
	assert.deepEqual(inputs, before);
});

test("read batch schema bounds also apply through meta_tool_run", async () => {
	const spec = { ...workflow(), ...readBatchLayout(2) };
	let calls = 0;
	await assert.rejects(() => executeDirectMetaTool(spec, {
		path_1: "a", offset_1: 1, limit_1: 9999,
		path_2: "b", offset_2: 1, limit_2: 1,
	}, async () => { calls++; }));
	assert.equal(calls, 0);
});

test("unvalidated schemas fall back even in force mode", () => {
	for (const input_schema of [
		{ $schema: "http://json-schema.org/draft-07/schema#", type: "object" },
		{ type: "object", properties: { n: { type: "integer", minimum: "bad" } } },
		{ $ref: "https://unavailable.example/schema" },
		{ type: "object", properties: { n: { type: "integer", nullable: true } } },
	]) {
		for (const mode of ["safe_auto", "force"] as const)
			assert.equal(directExecutionAllowed({ ...workflow(), input_schema }, mode, {}), false);
	}
});

test("tightened varying-flow policy cannot be bypassed by the fast path", () => {
	assert.equal(directExecutionAllowed(workflow(), "safe_auto", { min_varying_data_flows: 2 }), false);
	assert.equal(directExecutionAllowed(workflow(), "safe_auto", { min_varying_data_flows: "2" }), false);
	const forged = workflow();
	forged.steps[1].arguments = { id: "constant" };
	assert.equal(directExecutionAllowed(forged, "safe_auto", {}), false);
	const nonnumeric = workflow();
	nonnumeric.metadata.varying_data_flows = "1";
	assert.equal(directExecutionAllowed(nonnumeric, "safe_auto", {}), false);
});
