import assert from "node:assert/strict";
import test from "node:test";
import { planPreview } from "../src/preview.ts";
import type { MetaToolSpec } from "../src/types.ts";

test("plan preview substitutes current inputs and preserves step bindings", () => {
	const spec: MetaToolSpec = {
		name: "inspect_then_run",
		description: "test",
		input_schema: {
			type: "object",
			properties: { command: { type: "string" } },
		},
		steps: [
			{
				id: "step_1",
				tool: "read",
				arguments: { path: "README.md" },
			},
			{
				id: "step_2",
				tool: "bash",
				arguments: {
					command: { $input: "command" },
					context: { $step: "step_1", path: [] },
				},
			},
		],
		output: { $step: "step_2", path: [] },
		metadata: {},
	};

	const preview = planPreview(spec, { command: "echo verified" });

	assert.match(preview, /"command":"echo verified"/);
	assert.match(preview, /"\$step":"step_1"/);
});
