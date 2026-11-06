import assert from "node:assert/strict";
import test from "node:test";
import {
	parseExtractCommandArguments,
	withGeneralizedExtraction,
} from "../src/command.ts";

test("parses generalized extraction with a deprecated compatibility alias", () => {
	assert.deepEqual(parseExtractCommandArguments(""), {
		tracePath: undefined,
		deprecatedGeneralizeAlias: false,
	});
	assert.deepEqual(parseExtractCommandArguments("--generalize"), {
		tracePath: undefined,
		deprecatedGeneralizeAlias: true,
	});
	assert.deepEqual(
		parseExtractCommandArguments("--generalize traces/goal plus.json"),
		{
			tracePath: "traces/goal plus.json",
			deprecatedGeneralizeAlias: true,
		},
	);
	assert.deepEqual(
		parseExtractCommandArguments("traces/goal plus.json --generalize"),
		{
			tracePath: "traces/goal plus.json",
			deprecatedGeneralizeAlias: true,
		},
	);
	assert.deepEqual(parseExtractCommandArguments("traces/current.jsonl"), {
		tracePath: "traces/current.jsonl",
		deprecatedGeneralizeAlias: false,
	});
});

test("public extraction always enforces structural generalization", () => {
	const persistent = {
		argument_matching: "exact",
		structural_fallback: true,
		tool_rules: [{ pattern: "^read$" }],
	};
	const request = withGeneralizedExtraction(persistent);

	assert.notEqual(request, persistent);
	assert.equal(request.structural_fallback, false);
	assert.equal(request.argument_matching, "structural");
	assert.equal(persistent.structural_fallback, true);
	assert.equal(persistent.argument_matching, "exact");
});

test("rejects misplaced, duplicate, and unknown extract options", () => {
	assert.throws(
		() => parseExtractCommandArguments("trace.json --generalize extra"),
		/appear before or after/,
	);
	assert.throws(
		() => parseExtractCommandArguments("--generalize trace.json --generalize"),
		/only be supplied once/,
	);
	assert.throws(
		() => parseExtractCommandArguments("--unknown trace.json"),
		/Unknown extract option/,
	);
});
