import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { detectProjectProfile } from "../src/project-profile.ts";

test("project profile is deterministic, multi-label, and user-extensible", async () => {
	const root = await mkdtemp(join(tmpdir(), "meta-tool-profile-"));
	const cwd = join(root, "torchbench-project");
	try {
		await mkdir(join(cwd, ".pi", "meta-tool"), { recursive: true });
		await writeFile(
			join(cwd, "pyproject.toml"),
			"[project]\nname='torchbench-dvm'\ndependencies=['torch']\n",
		);
		await writeFile(
			join(cwd, ".pi", "meta-tool", "project.json"),
			JSON.stringify({ tags: ["custom-npu-lab"] }),
		);
		const profile = await detectProjectProfile({
			cwd,
			toolNames: ["goal_plus_search", "read"],
			providerNames: ["ascend-tools"],
		});
		for (const tag of [
			"custom-npu-lab",
			"python",
			"pytorch",
			"torchbench",
			"dvm",
			"goal-plus",
			"provider:ascend-tools",
		])
			assert.ok(profile.tags.includes(tag), `missing ${tag}`);
		assert.deepEqual(profile.tags, [...profile.tags].sort());
	} finally {
		await rm(root, { recursive: true, force: true });
	}
});
