import { existsSync } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { basename, join } from "node:path";
import type { ProjectProfile } from "./types.ts";

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

const PROFILE_CACHE = new Map<
	string,
	{ signature: string; profile: ProjectProfile }
>();

async function profileSignature(cwd: string): Promise<string> {
	const paths = [
		".pi/meta-tool/project.json",
		"pyproject.toml",
		"setup.py",
		"requirements.txt",
		"package.json",
		"Cargo.toml",
		"go.mod",
		".gp",
		".goal-plus-verifiers",
		"optimization_config.json",
		"task.json",
	];
	return (
		await Promise.all(
			paths.map(async (path) => {
				try {
					const info = await stat(join(cwd, path));
					return `${path}:${info.ino}:${info.mtimeMs}:${info.size}`;
				} catch (error) {
					if ((error as NodeJS.ErrnoException).code === "ENOENT")
						return `${path}:missing`;
					throw error;
				}
			}),
		)
	).join("|");
}

async function readText(path: string, limit = 128_000): Promise<string> {
	try {
		return (await readFile(path, "utf8")).slice(0, limit);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return "";
		throw error;
	}
}

async function explicitTags(cwd: string): Promise<string[]> {
	try {
		const value: unknown = JSON.parse(
			await readFile(join(cwd, ".pi", "meta-tool", "project.json"), "utf8"),
		);
		if (!isRecord(value) || !Array.isArray(value.tags)) return [];
		return value.tags
			.filter((tag): tag is string => typeof tag === "string")
			.map((tag) => tag.trim().toLowerCase())
			.filter(Boolean);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
		throw error;
	}
}

/** Cheap deterministic project classification used only for retrieval ranking. */
export async function detectProjectProfile(options: {
	cwd: string;
	toolNames?: Iterable<string>;
	providerNames?: Iterable<string>;
}): Promise<ProjectProfile> {
	const toolKey = [...(options.toolNames ?? [])].sort().join(",");
	const providerKey = [...(options.providerNames ?? [])].sort().join(",");
	const cacheKey = `${options.cwd}|${toolKey}|${providerKey}`;
	const signature = await profileSignature(options.cwd);
	const cached = PROFILE_CACHE.get(cacheKey);
	if (cached?.signature === signature) return cached.profile;
	const tags = new Set(await explicitTags(options.cwd));
	const cwdText = options.cwd.toLowerCase();
	tags.add(`project:${basename(options.cwd).toLowerCase()}`);

	if (
		existsSync(join(options.cwd, "pyproject.toml")) ||
		existsSync(join(options.cwd, "setup.py")) ||
		existsSync(join(options.cwd, "requirements.txt"))
	)
		tags.add("python");
	if (existsSync(join(options.cwd, "package.json"))) tags.add("nodejs");
	if (existsSync(join(options.cwd, "Cargo.toml"))) tags.add("rust");
	if (existsSync(join(options.cwd, "go.mod"))) tags.add("golang");
	if (existsSync(join(options.cwd, ".gp"))) tags.add("goal-plus");
	if (existsSync(join(options.cwd, ".goal-plus-verifiers")))
		tags.add("goal-plus");
	if (existsSync(join(options.cwd, "optimization_config.json")))
		tags.add("performance-optimization");
	if (cwdText.includes("torchbench")) tags.add("torchbench");
	if (cwdText.includes("pytorch")) tags.add("pytorch");
	if (cwdText.includes("webarena")) tags.add("webarena");
	if (cwdText.includes("spreadsheet")) tags.add("spreadsheet");

	const pyproject = (
		await readText(join(options.cwd, "pyproject.toml"))
	).toLowerCase();
	const packageJson = (
		await readText(join(options.cwd, "package.json"))
	).toLowerCase();
	const taskJson = (
		await readText(join(options.cwd, "task.json"))
	).toLowerCase();
	const manifests = `${pyproject}\n${packageJson}\n${taskJson}`;
	for (const [needle, tag] of [
		["torch", "pytorch"],
		["torchbench", "torchbench"],
		["dvm", "dvm"],
		["inductor", "inductor"],
		["ascend", "ascend-npu"],
		["goal-plus", "goal-plus"],
		["playwright", "browser-automation"],
	] as const)
		if (manifests.includes(needle)) tags.add(tag);

	for (const name of options.toolNames ?? []) {
		if (
			name.startsWith("goal_plus_") ||
			name.startsWith("search_") ||
			name.startsWith("pi_search_")
		)
			tags.add("goal-plus");
		if (name.includes("browser") || name === "click")
			tags.add("browser-automation");
	}
	const providers = [...(options.providerNames ?? [])]
		.map((name) => name.trim().toLowerCase())
		.filter(Boolean)
		.sort();
	for (const provider of providers) tags.add(`provider:${provider}`);

	const profile = {
		cwd: options.cwd,
		tags: [...tags].sort(),
		providers,
		detectedAt: new Date().toISOString(),
	};
	PROFILE_CACHE.set(cacheKey, { signature, profile });
	return profile;
}
