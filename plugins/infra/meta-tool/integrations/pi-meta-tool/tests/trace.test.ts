import assert from "node:assert/strict";
import test from "node:test";
import { extractTraceFromEntries } from "../src/trace.ts";

function message(role: string, content: unknown) {
	return { type: "message", message: { role, content } };
}

test("extracts completed built-in Pi calls as workflow traces", () => {
	const trace = extractTraceFromEntries(
		[
			message("user", [{ type: "text", text: "inspect the project" }]),
			message("assistant", [
				{
					type: "toolCall",
					id: "call-1",
					name: "read",
					arguments: { path: "README.md" },
				},
				{
					type: "toolCall",
					id: "call-2",
					name: "ls",
					arguments: { path: "." },
				},
			]),
			message("toolResult", [{ type: "text", text: '{"name":"awo"}' }]),
			message("toolResult", [{ type: "text", text: "README.md" }]),
		],
		{ sessionId: "session-1", cwd: "/workspace" },
	);

	// Attach tool call IDs as Pi does on toolResult messages.
	const entries = [
		message("user", [{ type: "text", text: "inspect the project" }]),
		message("assistant", [
			{
				type: "toolCall",
				id: "call-1",
				name: "read",
				arguments: { path: "README.md" },
			},
			{
				type: "toolCall",
				id: "call-2",
				name: "ls",
				arguments: { path: "." },
			},
		]),
		{
			type: "message",
			message: {
				role: "toolResult",
				toolCallId: "call-1",
				content: [{ type: "text", text: '{"name":"awo"}' }],
			},
		},
		{
			type: "message",
			message: {
				role: "toolResult",
				toolCallId: "call-2",
				content: [{ type: "text", text: "README.md" }],
			},
		},
	];
	const completed = extractTraceFromEntries(entries, {
		sessionId: "session-1",
		cwd: "/workspace",
	});

	assert.equal(trace.traces.length, 0);
	assert.equal(completed.traces.length, 1);
	assert.equal(completed.traces[0]?.calls.length, 2);
	assert.equal(completed.traces[0]?.calls[0]?.turn_id, 1);
	assert.deepEqual(completed.traces[0]?.calls[0]?.result, { name: "awo" });
});

test("preserves successful extension tool calls and drops failed turns", () => {
	const entries = [
		message("user", "custom tool turn"),
		message("assistant", [
			{ type: "toolCall", id: "x", name: "custom_tool", arguments: {} },
		]),
		{
			type: "message",
			message: {
				role: "toolResult",
				toolCallId: "x",
				content: [{ type: "text", text: "custom result" }],
			},
		},
		message("user", "failed turn"),
		message("assistant", [
			{
				type: "toolCall",
				id: "y",
				name: "bash",
				arguments: { command: "false" },
			},
		]),
		{
			type: "message",
			message: {
				role: "toolResult",
				toolCallId: "y",
				content: [{ type: "text", text: "exit 1" }],
				isError: true,
			},
		},
	];
	const trace = extractTraceFromEntries(entries, {
		sessionId: "session-2",
		cwd: "/workspace",
	});

	assert.equal(trace.traces.length, 1);
	assert.equal(trace.traces[0]?.calls[0]?.name, "custom_tool");
	assert.equal(trace.traces[0]?.calls[0]?.result, "custom result");
});

test("does not recursively learn a meta_tool_run invocation", () => {
	const trace = extractTraceFromEntries(
		[
			message("user", "run a learned workflow"),
			message("assistant", [
				{ type: "toolCall", id: "x", name: "meta_tool_run", arguments: {} },
			]),
		],
		{ sessionId: "session-3", cwd: "/workspace" },
	);

	assert.deepEqual(trace.traces, []);
});
