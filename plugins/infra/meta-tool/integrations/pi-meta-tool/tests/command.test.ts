import assert from "node:assert/strict";
import test from "node:test";
import {
	parseExtractCommandArguments,
	withGeneralizedExtraction,
} from "../src/command.ts";

test("parses an optional trace path, preserving spaces", () => {
	assert.deepEqual(parseExtractCommandArguments(""), {
		tracePath: undefined,
	});
	assert.deepEqual(
		parseExtractCommandArguments("  traces/goal plus.json  "),
		{
			tracePath: "traces/goal plus.json",
		},
	);
	assert.deepEqual(parseExtractCommandArguments("traces/current.jsonl"), {
		tracePath: "traces/current.jsonl",
	});
});

test("public extraction always enforces structural generalization", () => {
	const persistent = {
		argument_matching: "exact",
		tool_rules: [{ pattern: "^read$" }],
	};
	const request = withGeneralizedExtraction(persistent);

	assert.notEqual(request, persistent);
	assert.equal(request.argument_matching, "structural");
	assert.equal(persistent.argument_matching, "exact");
});

test("rejects extract options instead of treating them as trace paths", () => {
	for (const value of ["--unknown trace.json", "trace.json --unknown"])
		assert.throws(() => parseExtractCommandArguments(value), /Unknown extract option/);
});
