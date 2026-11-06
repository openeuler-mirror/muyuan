import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import {
	copyFile,
	mkdir,
	open,
	readdir,
	readFile,
	rename,
	rm,
	stat,
	unlink,
	writeFile,
} from "node:fs/promises";
import { hostname } from "node:os";
import { dirname, join, relative } from "node:path";
import { getPluginPaths, loadCatalogFile } from "./config.ts";
import { canonicalJson } from "./router.ts";
import type { MetaToolCatalog, MetaToolSpec } from "./types.ts";

const ARTIFACT_NAMES = [
	"active_meta_tools.json",
	"meta_tools.json",
	"rejected_meta_tools.json",
	"report.json",
	"normalization_config.json",
	"optimization_report.json",
	"tool_schemas.json",
	"candidates.json",
] as const;

export interface CatalogManifest {
	version: 1;
	activeVersion: number;
	activeDirectory: string;
	catalogFingerprint: string;
	publishedAt: string;
	source: "online" | "manual" | "import";
	traceCount: number;
	toolCallCount: number;
	watermarks: Record<string, number>;
}

export interface ProjectLease {
	owner: string;
	assertOwned(): Promise<void>;
	heartbeat(): Promise<void>;
	release(): Promise<void>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(path: string): Promise<Record<string, unknown>> {
	try {
		const value: unknown = JSON.parse(await readFile(path, "utf8"));
		return isRecord(value) ? value : {};
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
		throw error;
	}
}

async function atomicWrite(path: string, value: unknown): Promise<void> {
	await mkdir(dirname(path), { recursive: true });
	const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
	await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
	await rename(temporary, path);
}

export async function loadCatalogManifest(
	cwd: string,
): Promise<CatalogManifest | undefined> {
	const raw = await readJson(getPluginPaths(cwd).catalogManifestFile);
	if (
		raw.version !== 1 ||
		typeof raw.activeVersion !== "number" ||
		typeof raw.activeDirectory !== "string"
	)
		return undefined;
	return raw as unknown as CatalogManifest;
}

export async function activeArtifactPath(
	cwd: string,
	name: (typeof ARTIFACT_NAMES)[number],
): Promise<string> {
	const paths = getPluginPaths(cwd);
	const manifest = await loadCatalogManifest(cwd);
	if (manifest) {
		const versioned = join(paths.stateDir, manifest.activeDirectory, name);
		if (existsSync(versioned)) return versioned;
	}
	return join(paths.artifactsDir, name);
}

export async function loadCatalogVersionFile(
	cwd: string,
	version: number,
): Promise<string | undefined> {
	const path = join(
		getPluginPaths(cwd).catalogVersionsDir,
		`v${String(version).padStart(6, "0")}`,
		"active_meta_tools.json",
	);
	return existsSync(path) ? path : undefined;
}

function scopedSpec(
	spec: MetaToolSpec,
	scope: "project" | "user",
	version: number,
): MetaToolSpec {
	return {
		...spec,
		metadata: {
			...spec.metadata,
			catalog_scope: scope,
			catalog_version: version,
		},
	};
}

export async function loadProjectCatalog(options: {
	cwd: string;
	version?: number;
}): Promise<MetaToolCatalog> {
	const manifest = await loadCatalogManifest(options.cwd);
	const version = options.version ?? manifest?.activeVersion ?? 0;
	const path =
		options.version === undefined
			? await activeArtifactPath(options.cwd, "active_meta_tools.json")
			: await loadCatalogVersionFile(options.cwd, options.version);
	if (!path) return { meta_tools: [] };
	const catalog = await loadCatalogFile(path);
	return {
		meta_tools: catalog.meta_tools.map((spec) =>
			scopedSpec(spec, "project", version),
		),
	};
}

/** Load user-promoted catalogs. Project-local candidates override same names. */
export async function loadUserCatalog(
	userCatalogDir: string,
): Promise<MetaToolCatalog> {
	if (!existsSync(userCatalogDir)) return { meta_tools: [] };
	const files = (await readdir(userCatalogDir))
		.filter((name) => name.endsWith(".json"))
		.sort();
	const byName = new Map<string, MetaToolSpec>();
	for (const file of files) {
		const catalog = await loadCatalogFile(join(userCatalogDir, file));
		for (const spec of catalog.meta_tools)
			byName.set(spec.name, scopedSpec(spec, "user", 0));
	}
	return { meta_tools: [...byName.values()] };
}

export function mergeCatalogs(
	project: MetaToolCatalog,
	user: MetaToolCatalog,
): MetaToolCatalog {
	const merged = new Map(user.meta_tools.map((spec) => [spec.name, spec]));
	for (const spec of project.meta_tools) merged.set(spec.name, spec);
	return { meta_tools: [...merged.values()] };
}

export async function promoteToUserCatalog(options: {
	userCatalogDir: string;
	spec: MetaToolSpec;
}): Promise<string> {
	await mkdir(options.userCatalogDir, { recursive: true });
	const path = join(options.userCatalogDir, "promoted_meta_tools.json");
	const existing = await loadCatalogFile(path);
	const byName = new Map(
		existing.meta_tools.map((spec) => [spec.name, spec] as const),
	);
	const metadata = { ...options.spec.metadata };
	delete metadata.catalog_scope;
	delete metadata.catalog_version;
	byName.set(options.spec.name, { ...options.spec, metadata });
	await atomicWrite(path, { meta_tools: [...byName.values()] });
	return path;
}

export async function materializeRuntimeCatalog(options: {
	cwd: string;
	spec: MetaToolSpec;
	fingerprint: string;
}): Promise<string> {
	const directory = join(
		getPluginPaths(options.cwd).stateDir,
		"catalog",
		"runtime",
	);
	const specFingerprint = createHash("sha256")
		.update(canonicalJson(options.spec))
		.digest("hex")
		.slice(0, 20);
	const path = join(
		directory,
		`${options.fingerprint}-${specFingerprint}.json`,
	);
	if (!existsSync(path))
		await atomicWrite(path, { meta_tools: [options.spec] });
	return path;
}

export async function acquireProjectLease(
	cwd: string,
	leaseMs: number,
): Promise<ProjectLease | undefined> {
	const path = getPluginPaths(cwd).lockFile;
	await mkdir(dirname(path), { recursive: true });
	const owner = `${process.pid}:${randomUUID()}`;
	const host = hostname();
	const attempt = async () => {
		const handle = await open(path, "wx");
		await handle.writeFile(
			`${JSON.stringify({ owner, pid: process.pid, hostname: host, acquiredAt: new Date().toISOString() })}\n`,
			"utf8",
		);
		await handle.close();
	};
	try {
		await attempt();
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
		let stale = false;
		let current: Record<string, unknown> = {};
		try {
			const info = await stat(path);
			stale = Date.now() - info.mtimeMs > leaseMs;
			current = await readJson(path);
		} catch (statError) {
			if ((statError as NodeJS.ErrnoException).code !== "ENOENT")
				throw statError;
		}
		if (!stale) return undefined;
		// A live local process must never be fenced out only because its event
		// loop was delayed. Otherwise it could wake and publish concurrently
		// with the replacement owner. Signal 0 performs no mutation.
		if (
			current.hostname === host &&
			typeof current.pid === "number" &&
			Number.isInteger(current.pid) &&
			current.pid > 0
		) {
			try {
				process.kill(current.pid, 0);
				return undefined;
			} catch (processError) {
				if ((processError as NodeJS.ErrnoException).code === "EPERM")
					return undefined;
			}
		}
		const reclaimed = `${path}.stale.${process.pid}.${randomUUID()}`;
		try {
			// rename is the atomic stale-lock claim. Two contenders can no longer
			// both unlink one another's freshly-created lock.
			await rename(path, reclaimed);
		} catch (renameError) {
			if ((renameError as NodeJS.ErrnoException).code !== "ENOENT")
				return undefined;
			return undefined;
		}
		try {
			await attempt();
		} catch (retryError) {
			if ((retryError as NodeJS.ErrnoException).code === "EEXIST")
				return undefined;
			throw retryError;
		} finally {
			await rm(reclaimed, { force: true }).catch(() => undefined);
		}
	}

	let released = false;
	const assertOwned = async (): Promise<void> => {
		if (released) throw new Error("Meta-tool project lease was released");
		const handle = await open(path, "r").catch((error: unknown) => {
			if ((error as NodeJS.ErrnoException).code === "ENOENT")
				throw new Error("Meta-tool project lease was lost");
			throw error;
		});
		try {
			const raw = JSON.parse(await handle.readFile("utf8")) as Record<
				string,
				unknown
			>;
			if (raw.owner !== owner)
				throw new Error("Meta-tool project lease ownership changed");
		} finally {
			await handle.close();
		}
	};
	return {
		owner,
		assertOwned,
		async heartbeat() {
			if (released) return;
			// Touch the inode we verified, rather than a path that could have been
			// replaced between an ownership read and utimes(path).
			const handle = await open(path, "r+");
			try {
				const raw = JSON.parse(await handle.readFile("utf8")) as Record<
					string,
					unknown
				>;
				if (raw.owner !== owner)
					throw new Error("Meta-tool project lease ownership changed");
				const now = new Date();
				await handle.utimes(now, now);
			} finally {
				await handle.close();
			}
		},
		async release() {
			if (released) return;
			released = true;
			try {
				const raw = await readJson(path);
				if (raw.owner === owner) await unlink(path);
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
			}
		},
	};
}

async function catalogFingerprint(path: string): Promise<string> {
	return createHash("sha256")
		.update(await readFile(path))
		.digest("hex");
}

async function mirrorArtifacts(
	paths: ReturnType<typeof getPluginPaths>,
	versionDirectory: string,
): Promise<void> {
	await mkdir(paths.artifactsDir, { recursive: true });
	for (const name of ARTIFACT_NAMES) {
		const source = join(versionDirectory, name);
		if (!existsSync(source)) {
			await unlink(join(paths.artifactsDir, name)).catch(() => undefined);
			continue;
		}
		const temporary = join(
			paths.artifactsDir,
			`.${name}.${process.pid}.${randomUUID()}.tmp`,
		);
		await copyFile(source, temporary);
		await rename(temporary, join(paths.artifactsDir, name));
	}
}

export async function listCatalogVersions(cwd: string): Promise<number[]> {
	const directory = getPluginPaths(cwd).catalogVersionsDir;
	if (!existsSync(directory)) return [];
	return (await readdir(directory))
		.flatMap((name) =>
			/^v(\d{6})$/.exec(name)?.[1] ? [Number(name.slice(1))] : [],
		)
		.sort((left, right) => left - right);
}

export async function activateCatalogVersion(
	cwd: string,
	version: number,
): Promise<CatalogManifest> {
	const paths = getPluginPaths(cwd);
	const versionName = `v${String(version).padStart(6, "0")}`;
	const versionDirectory = join(paths.catalogVersionsDir, versionName);
	const activeCatalog = join(versionDirectory, "active_meta_tools.json");
	if (!existsSync(activeCatalog))
		throw new Error(`Catalog version does not exist: v${version}`);
	const previous = await loadCatalogManifest(cwd);
	const manifest: CatalogManifest = {
		version: 1,
		activeVersion: version,
		activeDirectory: relative(paths.stateDir, versionDirectory),
		catalogFingerprint: await catalogFingerprint(activeCatalog),
		publishedAt: new Date().toISOString(),
		source: "import",
		traceCount: previous?.traceCount ?? 0,
		toolCallCount: previous?.toolCallCount ?? 0,
		watermarks: previous?.watermarks ?? {},
	};
	await atomicWrite(paths.catalogManifestFile, manifest);
	await mirrorArtifacts(paths, versionDirectory);
	return manifest;
}

export async function publishDiscoveryArtifacts(options: {
	cwd: string;
	stagingDirectory: string;
	source: CatalogManifest["source"];
	traceCount: number;
	toolCallCount: number;
	watermarks?: Record<string, number>;
	preservePreviousVersions: number;
}): Promise<CatalogManifest> {
	const paths = getPluginPaths(options.cwd);
	const activeCatalog = join(
		options.stagingDirectory,
		"active_meta_tools.json",
	);
	if (!existsSync(activeCatalog))
		throw new Error(
			"Discovery staging directory has no active_meta_tools.json",
		);
	await mkdir(paths.catalogVersionsDir, { recursive: true });
	const existingVersions = await listCatalogVersions(options.cwd);
	const version = (existingVersions.at(-1) ?? 0) + 1;
	const versionName = `v${String(version).padStart(6, "0")}`;
	const versionDirectory = join(paths.catalogVersionsDir, versionName);
	if (existsSync(versionDirectory))
		throw new Error(`Catalog version already exists: ${versionName}`);
	await rename(options.stagingDirectory, versionDirectory);
	const manifest: CatalogManifest = {
		version: 1,
		activeVersion: version,
		activeDirectory: relative(paths.stateDir, versionDirectory),
		catalogFingerprint: await catalogFingerprint(
			join(versionDirectory, "active_meta_tools.json"),
		),
		publishedAt: new Date().toISOString(),
		source: options.source,
		traceCount: options.traceCount,
		toolCallCount: options.toolCallCount,
		watermarks: options.watermarks ?? {},
	};
	await atomicWrite(paths.catalogManifestFile, manifest);

	// Compatibility mirrors are written after the immutable version and
	// manifest exist. Runtime reads can always recover from the manifest.
	await mirrorArtifacts(paths, versionDirectory);

	const versions = (await readdir(paths.catalogVersionsDir))
		.filter((name) => /^v\d{6}$/.test(name))
		.sort();
	const excess = versions.slice(
		0,
		Math.max(0, versions.length - options.preservePreviousVersions),
	);
	for (const name of excess)
		await rm(join(paths.catalogVersionsDir, name), {
			recursive: true,
			force: true,
		});
	return manifest;
}
