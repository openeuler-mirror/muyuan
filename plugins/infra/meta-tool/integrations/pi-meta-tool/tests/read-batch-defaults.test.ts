import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { registerReadBatch } from "../extensions/read-batch.ts";
import { loadConfig, saveState } from "../src/config.ts";
import { loadExecutionUserCatalog } from "../src/read-batch-catalog.ts";
import { isCallerBoundReadBatch } from "../src/read-batches.ts";
import { metaToolFingerprint } from "../src/router.ts";

test("fresh Static and Online workspaces have all three portable read batches", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-batch-defaults-"));
	try {
		for (const online of [false, true]) {
			await mkdir(join(cwd, ".pi/meta-tool"), { recursive: true });
			await writeFile(join(cwd, ".pi/meta-tool/config.json"), JSON.stringify({
				online: { enabled: online }, routing: { includeUserCatalog: false },
				catalog: { userCatalogDir: join(cwd, "missing-user-catalog") },
			}));
			const config = await loadConfig(cwd, "/package/root");
			const catalog = await loadExecutionUserCatalog(config, false);
			assert.deepEqual(catalog.meta_tools.map((spec) => spec.steps.length), [2, 3, 4]);
			for (const spec of catalog.meta_tools) {
				assert(isCallerBoundReadBatch(spec));
				assert.equal(spec.metadata.preinstalled, true);
				assert.equal(spec.metadata.catalog_origin, "bundled_read_batch");
				assert(spec.steps.every((step) => step.tool === "read"));
			}
			assert(!JSON.stringify(catalog).includes("/data/disk/"));
			const disabled = { ...config, admission: { ...config.admission,
				policy: { ...(config.admission.policy as object), allow_caller_bound_read_batches: false } } };
			assert.deepEqual((await loadExecutionUserCatalog(disabled)).meta_tools, []);
		}
	} finally { await rm(cwd, { recursive: true, force: true }); }
});

test("user catalog overrides defaults and unrelated user history stays opt-in", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-batch-overrides-"));
	try {
		const config = await loadConfig(cwd, "/package/root");
		config.catalog.userCatalogDir = join(cwd, "catalog");
		const defaults = await loadExecutionUserCatalog(config, false);
		const override = structuredClone(defaults.meta_tools[0]);
		override.description = "custom description";
		const unrelated = { ...structuredClone(defaults.meta_tools[0]), name: "unrelated",
			steps: [{ id: "a", tool: "read", arguments: { path: "a" } },
				{ id: "b", tool: "read", arguments: { path: "b" } }], metadata: {} };
		await mkdir(config.catalog.userCatalogDir);
		await writeFile(join(config.catalog.userCatalogDir, "catalog.json"),
			JSON.stringify({ meta_tools: [override, unrelated] }));
		assert.equal((await loadExecutionUserCatalog(config)).meta_tools[0].description, "custom description");
		assert.equal((await loadExecutionUserCatalog(config, false)).meta_tools.length, 3);
		const disabled = { ...config, admission: { ...config.admission,
			policy: { ...(config.admission.policy as object), allow_caller_bound_read_batches: false } } };
		assert.deepEqual((await loadExecutionUserCatalog(disabled)).meta_tools.map((spec) => spec.name), ["unrelated"]);
	} finally { await rm(cwd, { recursive: true, force: true }); }
});

test("batch entry executes pinned compositions and respects disable and trust", async () => {
	const cwd = await mkdtemp(join(tmpdir(), "bate-batch-entry-"));
	try {
		const tools = new Map<string, any>(), hooks = new Map<string, any[]>();
		let active = ["read", "meta_tool_run"], trusted = true, calls = 0;
		const ctx = { cwd, isProjectTrusted: () => trusted };
		const pi = {
			registerTool(tool: any) { assert(!tools.has(tool.name)); tools.set(tool.name, tool); },
			on(event: string, handler: any) { hooks.set(event, [...(hooks.get(event) ?? []), handler]); },
			getActiveTools: () => active,
			setActiveTools: (next: string[]) => { active = next; },
			async callTool(name: string, params: any) {
				calls++;
				assert.equal(name, "meta_tool_run");
				const config = await loadConfig(cwd, "/package/root");
				const spec = (await loadExecutionUserCatalog(config)).meta_tools.find((s) => s.name === params.metaTool)!;
				assert.equal(params.fingerprint, metaToolFingerprint(spec));
				assert.equal(params.catalogScope, "user");
				return { isError: false, content: [{ type: "text", text: JSON.stringify(spec.steps.map((_, i) => ({
					path: params.inputs[`path_${i + 1}`], offset: params.inputs[`offset_${i + 1}`],
					limit: params.inputs[`limit_${i + 1}`], result: `read ${i + 1}\n[output truncated]`,
				}))) }] };
			},
		};
		registerReadBatch(pi);
		const refresh = async (event: string) => { for (const hook of hooks.get(event) ?? []) await hook({}, ctx); };
		await refresh("session_start");
		assert(active.includes("meta_tool_read_batch"));
		const batch = tools.get("meta_tool_read_batch");
		for (const size of [2, 3, 4]) {
			const result = await batch.execute("call", { ranges: Array.from({ length: size }, (_, i) => ({
				path: `file-${i}.c`, offset: i + 1, limit: 3,
			})) }, undefined, undefined, ctx);
			assert.equal(result.details.primitiveReads, size);
			assert(result.content[0].text.includes(`read ${size}\n[output truncated]`));
		}
		assert.equal(calls, 3);
		await assert.rejects(() => batch.execute("bad", { ranges: [
			{ path: "a.c", offset: 1, limit: 301 }, { path: "b.c", offset: 1, limit: 3 },
		] }, undefined, undefined, ctx));
		assert.equal(calls, 3);
		await saveState(cwd, false, "safe_auto");
		await refresh("before_agent_start");
		assert(!active.includes("meta_tool_read_batch"));
		await assert.rejects(() => batch.execute("off", {}, undefined, undefined, ctx), /not enabled/);
		await saveState(cwd, true, "safe_auto");
		await refresh("before_agent_start");
		assert(active.includes("meta_tool_read_batch"));
		trusted = false;
		await refresh("before_agent_start");
		assert(!active.includes("meta_tool_read_batch"));
		await assert.rejects(() => batch.execute("untrusted", {}, undefined, undefined, ctx), /not enabled/);
		assert.equal(calls, 3);
	} finally { await rm(cwd, { recursive: true, force: true }); }
});
