import assert from "node:assert/strict";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runBridge } from "../src/bridge.ts";
import {
	loadCatalogManifest,
	loadProjectCatalog,
} from "../src/catalog-store.ts";
import { loadCatalogFile, loadConfig } from "../src/config.ts";
import { runOnlineExtraction } from "../src/online-extraction.ts";
import {
	OnlineJournalWriter,
	readOnlineSnapshot,
} from "../src/online-journal.ts";
import { metaToolFingerprint } from "../src/router.ts";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

async function addEmployeeTrace(
	cwd: string,
	index: number,
	name: string,
	recoverAfterFailure = false,
): Promise<void> {
	const writer = await OnlineJournalWriter.open(cwd, `session-${index}`, {
		plugin_version: "1.0.0-test",
		project_tags: ["employee-demo"],
		tool_schema_fingerprints: {
			"directory.search_employee": "schema-directory-v1",
			"documents.get_document": "schema-documents-v1",
			"mail.send_email": "schema-mail-v1",
		},
	});
	await writer.beginTask(
		`Find ${name}'s passport and send it to the department`,
	);
	if (recoverAfterFailure)
		await writer.recordToolResult({
			toolCallId: `${index}-failed-audit`,
			toolName: "bash",
			input: { command: "false" },
			result: { exitCode: 1 },
			isError: true,
			turnId: 1,
		});
	await writer.recordToolResult({
		toolCallId: `${index}-search`,
		toolName: "directory.search_employee",
		input: { name },
		result: { employee_id: `emp-${index}`, name },
		isError: false,
		turnId: 1,
	});
	await writer.recordToolResult({
		toolCallId: `${index}-document`,
		toolName: "documents.get_document",
		input: { employee_id: `emp-${index}`, kind: "passport" },
		result: { document_id: `doc-${index}` },
		isError: false,
		turnId: 2,
	});
	await writer.recordToolResult({
		toolCallId: `${index}-mail`,
		toolName: "mail.send_email",
		input: {
			to: "travel@example.test",
			subject: `${name} passport`,
			attachment_id: `doc-${index}`,
		},
		result: { sent: true, message_id: `message-${index}` },
		isError: false,
		turnId: 3,
	});
	await writer.seal();
	await writer.flush();
}

test("online extraction merges sessions and atomically publishes a generalized catalog", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-online-extract-"));
	try {
		await addEmployeeTrace(cwd, 1, "Alice");
		await addEmployeeTrace(cwd, 2, "Bob");
		await addEmployeeTrace(cwd, 3, "Carol", true);
		const base = await loadConfig(cwd, PACKAGE_ROOT);
		const config = {
			...base,
			normalization: {
				...base.normalization,
				infer_domain_from_name: true,
				tool_rules: [
					{
						pattern: "^directory\\.search_employee$",
						semantic_type: "accessor",
						domain: "directory",
					},
					{
						pattern: "^documents\\.get_document$",
						semantic_type: "accessor",
						domain: "documents",
					},
					{
						pattern: "^mail\\.send_email$",
						semantic_type: "mutator",
						domain: "mail",
					},
				],
			},
			optimization: { ...base.optimization, enabled: false },
			extraction: { ...base.extraction, threshold: 2 },
			admission: {
				...base.admission,
				read_only_tools: [
					"directory.search_employee",
					"documents.get_document",
				],
			},
			online: {
				...base.online,
				minNewSuccessfulTraces: 3,
				debounceMs: 0,
			},
		};
		const supportedTools = new Set([
			"directory.search_employee",
			"documents.get_document",
			"mail.send_email",
		]);
		const snapshot = await readOnlineSnapshot(cwd, {
			maxTraces: config.online.maxTraces,
			maxTraceAgeDays: config.online.maxTraceAgeDays,
			maxCalls: config.online.maxCallsPerExtraction,
			traceEligibility: config.online.traceEligibility,
			maxRecoverableErrorsPerTask:
				config.online.maxRecoverableErrorsPerTask,
			minRecoveredSegmentCalls: config.online.minRecoveredSegmentCalls,
		});
		assert.equal(
			snapshot.trace.traces.filter(
				(trace) => trace.metadata.trace_eligibility === "recovered_segment",
			).length,
			1,
		);
		const offlineOutput = join(cwd, "offline-artifacts");
		await runBridge({
			pythonCommand: config.pythonCommand,
			bridgePath: config.bridgePath,
			metaToolRoot: config.metaToolRoot,
			timeoutMs: config.timeoutMs,
			signal: undefined,
			payload: {
				operation: "discover",
				trace: snapshot.trace,
				outputDir: offlineOutput,
				adapter: { supportedTools: [...supportedTools] },
				config: {
					normalization: config.normalization,
					optimization: config.optimization,
					extraction: config.extraction,
					compiler: config.compiler,
					admission: config.admission,
				},
			},
		});
		const status = await runOnlineExtraction({ cwd, config, supportedTools });
		assert.equal(status.state, "succeeded");
		assert.equal(status.published, true, status.error ?? status.reason);
		assert.equal(status.newSuccessfulTraces, 3);
		assert.equal((await loadCatalogManifest(cwd))?.activeVersion, 1);
		const catalog = await loadProjectCatalog({ cwd });
		const offlineCatalog = await loadCatalogFile(
			join(offlineOutput, "active_meta_tools.json"),
		);
		assert.deepEqual(
			catalog.meta_tools.map(metaToolFingerprint).sort(),
			offlineCatalog.meta_tools.map(metaToolFingerprint).sort(),
		);
		assert.ok(catalog.meta_tools.length > 0);
		assert.ok(
			catalog.meta_tools.every(
				(spec) => spec.metadata.generalization_mode === "structural",
			),
		);
		assert.ok(
			catalog.meta_tools.every((spec) => {
				const schemas = spec.metadata
					.required_tool_schema_fingerprints as Record<string, unknown>;
				return spec.steps.every(
					(step) => typeof schemas[step.tool] === "string",
				);
			}),
		);
		assert.ok(
			catalog.meta_tools.some(
				(spec) =>
					Array.isArray(spec.metadata.task_goals) &&
					spec.metadata.task_goals.length === 3,
			),
		);
		assert.ok(
			catalog.meta_tools.every(
				(spec) =>
					Array.isArray(spec.metadata.project_tags) &&
					spec.metadata.project_tags.includes("employee-demo"),
			),
		);

		const noNewData = await runOnlineExtraction({
			cwd,
			config,
			supportedTools,
		});
		assert.equal(noNewData.state, "idle");
		assert.equal(noNewData.published, false);

		// An explicit online run must be able to re-evaluate checkpointed
		// evidence. This is required when the host tool registry changes after
		// an earlier attempt rejected otherwise valid candidates.
		const forcedReplay = await runOnlineExtraction({
			cwd,
			config,
			supportedTools,
			force: true,
		});
		assert.equal(forcedReplay.state, "succeeded");
		assert.equal(forcedReplay.published, true);
		assert.equal(forcedReplay.newSuccessfulTraces, 0);
		assert.equal((await loadCatalogManifest(cwd))?.activeVersion, 2);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("online extraction exposes a bounded pending retry instead of rebuilding per call", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-online-pending-"));
	try {
		await addEmployeeTrace(cwd, 1, "Alice");
		const base = await loadConfig(cwd, PACKAGE_ROOT);
		const config = {
			...base,
			online: {
				...base.online,
				minNewSuccessfulTraces: 10,
				maxWaitMs: 5_000,
			},
		};
		const status = await runOnlineExtraction({
			cwd,
			config,
			supportedTools: new Set([
				"directory.search_employee",
				"documents.get_document",
				"mail.send_email",
			]),
		});
		assert.equal(status.state, "pending");
		assert.ok((status.retryAfterMs ?? 0) > 0);
		assert.ok((status.retryAfterMs ?? Infinity) <= 5_000);
		const drained = await runOnlineExtraction({
			cwd, config, drain: true,
			supportedTools: new Set([
				"directory.search_employee", "documents.get_document", "mail.send_email",
			]),
		});
		assert.equal(drained.state, "succeeded");
		assert.equal(drained.published, false);
		assert.equal(drained.totalSuccessfulTraces, 1);
		const archivedOutput = (drained.result as { output?: string })?.output;
		assert.ok(archivedOutput, "diagnostic output must point to its archived directory");
		await access(archivedOutput);
		const repeated = await runOnlineExtraction({
			cwd, config, drain: true,
			supportedTools: new Set(["directory.search_employee"]),
		});
		assert.equal(repeated.state, "idle");
		assert.equal(repeated.newSuccessfulTraces, 0);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});
