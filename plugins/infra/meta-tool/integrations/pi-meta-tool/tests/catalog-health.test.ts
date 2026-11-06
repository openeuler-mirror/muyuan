import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
	degradedFingerprints,
	loadCatalogHealth,
	recordCandidateExecution,
	resetCandidateHealth,
} from "../src/catalog-health.ts";

test("three consecutive failures degrade a fingerprint and reset is explicit", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-health-"));
	try {
		for (let index = 0; index < 3; index++)
			await recordCandidateExecution({
				cwd,
				name: "workflow",
				fingerprint: "abc",
				catalogVersion: 2,
				durationMs: 10 + index,
				error: "provider schema mismatch",
				lastPrimitiveTool: "read",
				lastPrimitiveStep: 2,
				toolSchemaFingerprint: "schema-set-v1",
			});
		const health = await loadCatalogHealth(cwd);
		assert.equal(health.records.abc?.state, "degraded");
		assert.equal(health.records.abc?.consecutiveFailures, 3);
		assert.equal(health.records.abc?.lastPrimitiveTool, "read");
		assert.equal(health.records.abc?.lastPrimitiveStep, 2);
		assert.equal(health.records.abc?.toolSchemaFingerprint, "schema-set-v1");
		assert.ok(degradedFingerprints(health).has("abc"));
		assert.equal(await resetCandidateHealth({ cwd, name: "workflow" }), 1);
		assert.equal((await loadCatalogHealth(cwd)).records.abc, undefined);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});
