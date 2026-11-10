import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, rename, writeFile, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import {
	loadCheckpoint,
	OnlineJournalWriter,
	readOnlineSnapshot,
	saveCheckpoint,
} from "../src/online-journal.ts";

test("completed-turn checkpoints publish bounded segments, not completed tasks", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-segments-"));
	try {
		const writer = await OnlineJournalWriter.open(cwd, "segmented");
		await writer.beginTask("long audit");
		await primitive(writer, { id: "one", name: "find" });
		assert.equal(await writer.checkpoint(2), false);
		await primitive(writer, { id: "two", name: "read" });
		assert.equal(await writer.checkpoint(2), true);
		const snapshot = await readOnlineSnapshot(cwd, {
			maxTraces: 20, maxTraceAgeDays: 7, maxCalls: 100,
		});
		assert.equal(snapshot.totalCalls, 2);
		assert.equal(snapshot.trace.traces[0].metadata.task_complete, false);
		assert.equal(snapshot.trace.traces[0].metadata.workflow_boundary, "segment");
		await primitive(writer, { id: "three", name: "read" });
		await writer.seal();
		await writer.flush();
		const final = await readOnlineSnapshot(cwd, {
			maxTraces: 20, maxTraceAgeDays: 7, maxCalls: 100,
		});
		assert.equal(final.totalCalls, 3);
		assert.equal(final.totalSuccessfulTraces, 2);
		assert.equal(new Set(final.trace.traces.map(t => t.metadata.session_id)).size, 1);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("transient append errors retain pending records and do not poison future flushes", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-write-recovery-"));
	try {
		const writer = await OnlineJournalWriter.open(cwd, "recover");
		await writer.beginTask("recover journal");
		const directory = dirname(writer.path);
		await rename(directory, `${directory}.retained`);
		await writeFile(directory, "temporary blocker");
		await assert.rejects(primitive(writer, { id: "pending", name: "read" }));
		await unlink(directory);
		await rename(`${directory}.retained`, directory);
		await writer.flush();
		await primitive(writer, { id: "later", name: "read" });
		await writer.seal();
		await writer.flush();
		const snapshot = await readOnlineSnapshot(cwd, {
			maxTraces: 20, maxTraceAgeDays: 7, maxCalls: 100,
		});
		assert.deepEqual(snapshot.trace.traces[0].calls.map(c => c.call_id), ["pending", "later"]);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

async function primitive(
	writer: OnlineJournalWriter,
	value: { id: string; name: string; error?: boolean; secret?: string },
): Promise<void> {
	await writer.recordToolResult({
		toolCallId: value.id,
		toolName: value.name,
		input: { path: `${value.id}.txt`, apiToken: value.secret ?? "hidden" },
		result: { ok: !value.error },
		isError: value.error === true,
		turnId: 1,
	});
}

test("journals merge sealed successful primitive traces across sessions", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-journal-"));
	try {
		const first = await OnlineJournalWriter.open(cwd, "session-a");
		await first.beginTask("read project metadata");
		await primitive(first, { id: "a1", name: "read" });
		assert.equal(await first.seal(), true);

		await first.beginTask("failed task is not training data");
		await primitive(first, { id: "a2", name: "read", error: true });
		assert.equal(await first.seal(), false);

		await first.beginTask("meta execution is not primitive training data");
		await primitive(first, { id: "a3", name: "meta_tool_run" });
		assert.equal(await first.seal(), false);
		await first.flush();

		const second = await OnlineJournalWriter.open(cwd, "session-b");
		await second.beginTask("find and read configuration");
		await primitive(second, { id: "b1", name: "find" });
		await primitive(second, { id: "b2", name: "read" });
		assert.equal(await second.seal(), true);
		await second.flush();

		const checkpoint = await loadCheckpoint(cwd);
		const snapshot = await readOnlineSnapshot(cwd, {
			maxTraces: 20,
			maxTraceAgeDays: 7,
			maxCalls: 100,
			checkpoint,
		});
		assert.equal(snapshot.newSuccessfulTraces, 2);
		assert.equal(snapshot.totalSuccessfulTraces, 2);
		assert.equal(snapshot.totalCalls, 3);
		assert.deepEqual(
			snapshot.trace.traces.map((trace) => trace.metadata.goal).sort(),
			["find and read configuration", "read project metadata"],
		);
		assert.ok(
			snapshot.trace.traces.every((trace) =>
				trace.calls.every(
					(call) =>
						call.name !== "meta_tool_run" && call.name !== "meta_tool_search",
				),
			),
		);

		await saveCheckpoint(cwd, {
			version: 1,
			watermarks: snapshot.watermarks,
			lastExtractionAt: new Date().toISOString(),
			lastCatalogVersion: 1,
			pendingSince: null,
		});
		const afterCheckpoint = await readOnlineSnapshot(cwd, {
			maxTraces: 20,
			maxTraceAgeDays: 7,
			maxCalls: 100,
			checkpoint: await loadCheckpoint(cwd),
		});
		assert.equal(afterCheckpoint.newSuccessfulTraces, 0);

		const raw = await readFile(first.path, "utf8");
		assert.doesNotMatch(raw, /hidden/);
		assert.match(raw, /\[REDACTED\]/);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("reopening one Pi session creates distinct task ids", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-reopen-"));
	try {
		for (const goal of ["first", "second"]) {
			const writer = await OnlineJournalWriter.open(cwd, "same-session");
			await writer.beginTask(goal);
			await primitive(writer, { id: goal, name: "read" });
			await writer.seal();
			await writer.flush();
		}
		const snapshot = await readOnlineSnapshot(cwd, {
			maxTraces: 10,
			maxTraceAgeDays: 7,
			maxCalls: 100,
		});
		assert.equal(snapshot.totalSuccessfulTraces, 2);
		assert.equal(
			new Set(snapshot.trace.traces.map((trace) => trace.trace_id)).size,
			2,
		);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("recovered tasks contribute only the successful suffix after the last failure", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-recovered-segment-"));
	try {
		const writer = await OnlineJournalWriter.open(cwd, "recovery-session");
		await writer.beginTask("recover after one failed command");
		await primitive(writer, { id: "before", name: "read" });
		await primitive(writer, { id: "failure", name: "bash", error: true });
		await primitive(writer, { id: "after-find", name: "find" });
		await primitive(writer, { id: "after-read", name: "read" });
		assert.equal(await writer.seal(), false);
		assert.deepEqual(writer.getLastSealStats(), {
			strictSuccess: false,
			primitiveCalls: 4,
			failedCalls: 1,
		});

		await writer.beginTask("terminal failure is not recovery");
		await primitive(writer, { id: "terminal-before", name: "read" });
		await primitive(writer, {
			id: "terminal-failure",
			name: "bash",
			error: true,
		});
		await writer.seal();

		await writer.beginTask("too many failures are rejected");
		await primitive(writer, { id: "failure-1", name: "bash", error: true });
		await primitive(writer, { id: "middle", name: "read" });
		await primitive(writer, { id: "failure-2", name: "bash", error: true });
		await primitive(writer, { id: "suffix-find", name: "find" });
		await primitive(writer, { id: "suffix-read", name: "read" });
		await writer.seal();
		await writer.flush();

		const strict = await readOnlineSnapshot(cwd, {
			maxTraces: 10,
			maxTraceAgeDays: 7,
			maxCalls: 100,
			traceEligibility: "strict",
		});
		assert.equal(strict.totalSuccessfulTraces, 0);

		const recovered = await readOnlineSnapshot(cwd, {
			maxTraces: 10,
			maxTraceAgeDays: 7,
			maxCalls: 100,
			traceEligibility: "recovered_segment",
			maxRecoverableErrorsPerTask: 1,
			minRecoveredSegmentCalls: 2,
		});
		assert.equal(recovered.totalSuccessfulTraces, 1);
		assert.deepEqual(
			recovered.trace.traces[0]?.calls.map((call) => call.call_id),
			["after-find", "after-read"],
		);
		assert.equal(
			recovered.trace.traces[0]?.metadata.trace_eligibility,
			"recovered_segment",
		);
		assert.equal(
			recovered.trace.traces[0]?.metadata.discarded_successful_calls,
			1,
		);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("snapshot budgets advance only the successful traces included in each batch", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-journal-batches-"));
	try {
		for (let index = 1; index <= 4; index++) {
			const writer = await OnlineJournalWriter.open(cwd, `session-${index}`);
			await writer.beginTask(`task ${index}`);
			await primitive(writer, { id: String(index), name: "read" });
			await writer.seal();
			await writer.flush();
		}
		const first = await readOnlineSnapshot(cwd, {
			maxTraces: 2,
			maxTraceAgeDays: 7,
			maxCalls: 10,
		});
		assert.equal(first.availableNewSuccessfulTraces, 4);
		assert.equal(first.newSuccessfulTraces, 2);
		assert.equal(first.remainingNewSuccessfulTraces, 2);
		await saveCheckpoint(cwd, {
			version: 1,
			watermarks: first.watermarks,
			lastExtractionAt: new Date().toISOString(),
			lastCatalogVersion: 1,
			pendingSince: null,
		});
		const second = await readOnlineSnapshot(cwd, {
			maxTraces: 2,
			maxTraceAgeDays: 7,
			maxCalls: 10,
			checkpoint: await loadCheckpoint(cwd),
		});
		assert.equal(second.availableNewSuccessfulTraces, 2);
		assert.equal(second.newSuccessfulTraces, 2);
		assert.equal(second.remainingNewSuccessfulTraces, 0);
		assert.equal(
			new Set(first.trace.traces.map((trace) => trace.trace_id)).size,
			2,
		);
		assert.equal(
			first.trace.traces.some((left) =>
				second.trace.traces.some((right) => right.trace_id === left.trace_id),
			),
			false,
		);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("journal bounds oversized serialized tool values before bridge batching", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-journal-size-"));
	try {
		const writer = await OnlineJournalWriter.open(cwd, "large-session");
		await writer.beginTask("bounded output");
		await writer.recordToolResult({
			toolCallId: "large",
			toolName: "read",
			input: { path: "large.json" },
			result: Object.fromEntries(
				Array.from({ length: 10 }, (_, index) => [
					`field_${index}`,
					"x".repeat(65_536),
				]),
			),
			isError: false,
			turnId: 1,
		});
		await writer.seal();
		await writer.flush();
		const raw = await readFile(writer.path, "utf8");
		assert.match(raw, /_meta_tool_truncated/);
		assert.ok(Buffer.byteLength(raw, "utf8") < 100 * 1024);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});
