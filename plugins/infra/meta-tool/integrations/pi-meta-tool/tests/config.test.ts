import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
	defaultConfigDocument,
	getPluginPaths,
	loadConfig,
	loadState,
	saveState,
} from "../src/config.ts";

test("stale offline config cannot restore exact-only extraction", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-config-"));
	try {
		const paths = getPluginPaths(cwd);
		await mkdir(paths.stateDir, { recursive: true });
		await writeFile(
			paths.configFile,
			JSON.stringify({
				normalization: {
					argument_matching: "exact",
				},
				online: { maxBytesPerExtraction: 1 },
				catalog: {
					versioned: false,
					userCatalogDir: "~/.pi/custom-meta-tools",
				},
			}),
		);
		const config = await loadConfig(cwd, "/package/root");
		assert.equal(config.normalization.argument_matching, "structural");
		assert.equal(config.online.enabled, true);
		assert.equal(config.online.maxBytesPerExtraction, 1024 * 1024);
		assert.equal(config.online.traceEligibility, "recovered_segment");
		assert.equal(config.online.maxRecoverableErrorsPerTask, 1);
		assert.equal(config.online.minRecoveredSegmentCalls, 2);
		assert.equal(config.routing.minActiveCatalogSize, 1);
		assert.equal(config.admission.policy.allow_caller_bound_read_batches, true);
		assert.equal(
			config.admission.allow_noninteractive_guarded_execution,
			false,
		);
		assert.equal(config.catalog.versioned, true);
		assert.doesNotMatch(config.catalog.userCatalogDir, /^~/);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("rejects an enabled legacy optimization loop before extraction", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-legacy-config-"));
	try {
		const paths = getPluginPaths(cwd);
		await mkdir(paths.stateDir, { recursive: true });
		await writeFile(paths.configFile, JSON.stringify({ optimization: { enabled: true } }));
		await assert.rejects(() => loadConfig(cwd, "/package/root"), /optimization loop/);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("read batches default on in Static and Online while explicit false wins", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-batch-config-"));
	try {
		const path = getPluginPaths(cwd).configFile;
		await mkdir(join(cwd, ".pi", "meta-tool"), { recursive: true });
		assert.equal((defaultConfigDocument().admission as any).policy.allow_caller_bound_read_batches, true);
		for (const online of [false, true]) {
			for (const allow of [undefined, false, true]) {
				await writeFile(path, JSON.stringify({ online: { enabled: online },
					admission: { policy: { allow_caller_bound_read_batches: allow } } }));
				const config = await loadConfig(cwd, "/package/root");
				assert.equal(config.online.enabled, online);
				assert.equal(config.admission.policy.allow_caller_bound_read_batches, allow !== false);
				assert.equal(config.admission.allow_noninteractive_guarded_execution, false);
			}
		}
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("online edition starts enabled but preserves an explicit disable", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-state-"));
	try {
		assert.equal((await loadState(cwd)).enabled, true);
		await saveState(cwd, false, "safe_auto");
		assert.equal((await loadState(cwd)).enabled, false);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("private runtime separates immutable config from Goal-Plus product files", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-private-layout-"));
	try {
		const configFile = join(cwd, ".pi", "meta-tool", "config.json");
		await mkdir(join(cwd, ".pi", "meta-tool"), { recursive: true });
		await writeFile(configFile, JSON.stringify({ runtimeLayout: "private" }));
		const paths = getPluginPaths(cwd);
		assert.equal(paths.configFile, configFile);
		assert.equal(paths.stateDir, join(cwd, ".tmp", "meta-tool"));
		await saveState(cwd, true, "safe_auto");
		assert.equal((await loadState(cwd)).enabled, true);
		assert.equal((await loadConfig(cwd, "/package/root")).online.segmentCalls, 32);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("Goal-Plus inner-Git marker selects private runtime without modifying config", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-inner-git-layout-"));
	try {
		await mkdir(join(cwd, ".git", "goal-plus"), { recursive: true });
		await writeFile(join(cwd, ".git", "goal-plus", "state.json"), "{}");
		assert.equal(getPluginPaths(cwd).stateDir, join(cwd, ".tmp", "meta-tool"));
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});
