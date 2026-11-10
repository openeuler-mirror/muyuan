import assert from "node:assert/strict";
import {
	mkdir,
	mkdtemp,
	readFile,
	rm,
	utimes,
	writeFile,
} from "node:fs/promises";
import { hostname, tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
	acquireProjectLease,
	activateCatalogVersion,
	loadCatalogManifest,
	loadProjectCatalog,
	loadUserCatalog,
	materializeRuntimeCatalog,
	mergeCatalogs,
	promoteToUserCatalog,
	publishDiscoveryArtifacts,
} from "../src/catalog-store.ts";

function spec(name: string) {
	return {
		name,
		description: `${name} workflow`,
		input_schema: { type: "object" },
		steps: [
			{ id: "step_1", tool: "read", arguments: { path: "a" } },
			{ id: "step_2", tool: "read", arguments: { path: "b" } },
		],
		output: { $step: "step_2", path: [] },
		metadata: { reviewed: true, side_effect_free: true },
	};
}

async function staging(cwd: string, name: string): Promise<string> {
	const path = join(cwd, `.staging-${name}`);
	await mkdir(path, { recursive: true });
	const catalog = { meta_tools: [spec(name)] };
	await writeFile(
		join(path, "active_meta_tools.json"),
		JSON.stringify(catalog),
	);
	await writeFile(join(path, "meta_tools.json"), JSON.stringify(catalog));
	await writeFile(join(path, "report.json"), JSON.stringify({ name }));
	return path;
}

test("catalog publication is immutable, versioned, and recoverable", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-catalog-"));
	try {
		const first = await publishDiscoveryArtifacts({
			cwd,
			stagingDirectory: await staging(cwd, "one"),
			source: "manual",
			traceCount: 3,
			toolCallCount: 6,
			preservePreviousVersions: 20,
		});
		const second = await publishDiscoveryArtifacts({
			cwd,
			stagingDirectory: await staging(cwd, "two"),
			source: "online",
			traceCount: 10,
			toolCallCount: 20,
			watermarks: { "session.jsonl": 9 },
			preservePreviousVersions: 20,
		});
		assert.equal(first.activeVersion, 1);
		assert.equal(second.activeVersion, 2);
		assert.equal((await loadCatalogManifest(cwd))?.activeVersion, 2);
		assert.equal(
			(await loadProjectCatalog({ cwd })).meta_tools[0]?.name,
			"two",
		);
		assert.equal(
			(await loadProjectCatalog({ cwd, version: 1 })).meta_tools[0]?.name,
			"one",
		);
		assert.match(
			await readFile(
				join(cwd, ".pi", "meta-tool", "artifacts", "report.json"),
				"utf8",
			),
			/"two"/,
		);
		await activateCatalogVersion(cwd, 1);
		assert.equal(
			(await loadProjectCatalog({ cwd })).meta_tools[0]?.name,
			"one",
		);
		const third = await publishDiscoveryArtifacts({
			cwd,
			stagingDirectory: await staging(cwd, "three"),
			source: "online",
			traceCount: 12,
			toolCallCount: 24,
			preservePreviousVersions: 20,
		});
		assert.equal(third.activeVersion, 3);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("project lease serializes sessions and user catalog is explicit", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-lease-"));
	const userCatalogDir = join(cwd, "user-catalog");
	try {
		const first = await acquireProjectLease(cwd, 60_000);
		assert.ok(first);
		await first?.assertOwned();
		assert.equal(await acquireProjectLease(cwd, 60_000), undefined);
		await first?.release();
		await assert.rejects(() => first?.assertOwned());
		const second = await acquireProjectLease(cwd, 60_000);
		assert.ok(second);
		await second?.release();

		await promoteToUserCatalog({ userCatalogDir, spec: spec("shared") });
		const user = await loadUserCatalog(userCatalogDir);
		assert.equal(user.meta_tools[0]?.metadata.catalog_scope, "user");
		const merged = mergeCatalogs(
			{ meta_tools: [spec("shared"), spec("local")] },
			user,
		);
		assert.equal(merged.meta_tools.length, 2);
		assert.notEqual(
			merged.meta_tools.find((item) => item.name === "shared")?.metadata
				.catalog_scope,
			"user",
		);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("stale project lease recovery elects exactly one replacement owner", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-stale-lease-"));
	try {
		const lock = join(cwd, ".pi", "meta-tool", "online", "extract.lock");
		await mkdir(join(cwd, ".pi", "meta-tool", "online"), {
			recursive: true,
		});
		await writeFile(
			lock,
			JSON.stringify({
				owner: "dead-owner",
				pid: 2_147_483_647,
				hostname: hostname(),
			}),
		);
		const old = new Date(Date.now() - 60_000);
		await utimes(lock, old, old);
		const contenders = await Promise.all([
			acquireProjectLease(cwd, 10),
			acquireProjectLease(cwd, 10),
		]);
		const owners = contenders.filter((lease) => lease !== undefined);
		assert.equal(owners.length, 1);
		await owners[0]?.assertOwned();
		await owners[0]?.release();
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});

test("runtime materialization pins full spec metadata as well as workflow", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "meta-tool-runtime-spec-"));
	try {
		const base = spec("pinned");
		const first = await materializeRuntimeCatalog({
			cwd,
			spec: base,
			fingerprint: "workflow-fingerprint",
		});
		const second = await materializeRuntimeCatalog({
			cwd,
			spec: {
				...base,
				metadata: { ...base.metadata, execution_guard: "confirm_each_run" },
			},
			fingerprint: "workflow-fingerprint",
		});
		assert.notEqual(first, second);
		assert.match(await readFile(second, "utf8"), /confirm_each_run/);
	} finally {
		await rm(cwd, { recursive: true, force: true });
	}
});
