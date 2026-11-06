import { randomUUID } from "node:crypto";
import {
	mkdir,
	open,
	readFile,
	rename,
	stat,
	unlink,
	writeFile,
} from "node:fs/promises";
import { dirname } from "node:path";
import { getPluginPaths } from "./config.ts";

export interface CandidateHealth {
	name: string;
	fingerprint: string;
	catalogVersion: number;
	attempts: number;
	successes: number;
	failures: number;
	consecutiveFailures: number;
	totalDurationMs: number;
	lastDurationMs: number;
	lastRunAt: string;
	lastError: string | null;
	lastPrimitiveTool: string | null;
	lastPrimitiveStep: number | null;
	toolSchemaFingerprint: string | null;
	state: "active" | "degraded";
}

export interface CatalogHealth {
	version: 1;
	updatedAt: string;
	records: Record<string, CandidateHealth>;
}

const HEALTH_CACHE = new Map<
	string,
	{ signature: string; value: CatalogHealth }
>();

function emptyHealth(): CatalogHealth {
	return { version: 1, updatedAt: new Date(0).toISOString(), records: {} };
}

async function atomicWrite(path: string, value: unknown): Promise<void> {
	await mkdir(dirname(path), { recursive: true });
	const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
	await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
	await rename(temporary, path);
}

async function withHealthLock<T>(
	cwd: string,
	action: () => Promise<T>,
): Promise<T> {
	const path = getPluginPaths(cwd).catalogHealthLockFile;
	await mkdir(dirname(path), { recursive: true });
	const owner = `${process.pid}:${randomUUID()}`;
	for (let attempt = 0; ; attempt++) {
		try {
			const handle = await open(path, "wx");
			await handle.writeFile(owner, "utf8");
			await handle.close();
			break;
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
			const age = await stat(path)
				.then((value) => Date.now() - value.mtimeMs)
				.catch(() => 0);
			if (age > 10_000) {
				await unlink(path).catch(() => undefined);
				continue;
			}
			if (attempt >= 100)
				throw new Error("Timed out acquiring meta-tool health lock");
			await new Promise((resolve) => setTimeout(resolve, 10));
		}
	}
	try {
		return await action();
	} finally {
		const current = await readFile(path, "utf8").catch(() => "");
		if (current === owner) await unlink(path).catch(() => undefined);
	}
}

export async function loadCatalogHealth(cwd: string): Promise<CatalogHealth> {
	const path = getPluginPaths(cwd).catalogHealthFile;
	const signature = await stat(path)
		.then((value) => `${value.ino}:${value.mtimeMs}:${value.size}`)
		.catch((error: unknown) => {
			if ((error as NodeJS.ErrnoException).code === "ENOENT") return "missing";
			throw error;
		});
	const cached = HEALTH_CACHE.get(path);
	if (cached?.signature === signature) return cached.value;
	try {
		const value = JSON.parse(
			await readFile(path, "utf8"),
		) as CatalogHealth;
		const result = value.version === 1 &&
			value.records &&
			typeof value.records === "object"
			? value
			: emptyHealth();
		HEALTH_CACHE.set(path, { signature, value: result });
		return result;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") {
			const result = emptyHealth();
			HEALTH_CACHE.set(path, { signature: "missing", value: result });
			return result;
		}
		throw error;
	}
}

export function degradedFingerprints(health: CatalogHealth): Set<string> {
	return new Set(
		Object.values(health.records)
			.filter((record) => record.state === "degraded")
			.map((record) => record.fingerprint),
	);
}

export async function recordCandidateExecution(options: {
	cwd: string;
	name: string;
	fingerprint: string;
	catalogVersion: number;
	durationMs: number;
	error?: string;
	lastPrimitiveTool?: string;
	lastPrimitiveStep?: number;
	toolSchemaFingerprint?: string;
	degradeAfterFailures?: number;
}): Promise<CandidateHealth> {
	return withHealthLock(options.cwd, async () => {
		const health = await loadCatalogHealth(options.cwd);
		const current = health.records[options.fingerprint];
		const succeeded = options.error === undefined;
		const consecutiveFailures = succeeded
			? 0
			: (current?.consecutiveFailures ?? 0) + 1;
		const record: CandidateHealth = {
			name: options.name,
			fingerprint: options.fingerprint,
			catalogVersion: options.catalogVersion,
			attempts: (current?.attempts ?? 0) + 1,
			successes: (current?.successes ?? 0) + (succeeded ? 1 : 0),
			failures: (current?.failures ?? 0) + (succeeded ? 0 : 1),
			consecutiveFailures,
			totalDurationMs:
				(current?.totalDurationMs ?? 0) + Math.max(0, options.durationMs),
			lastDurationMs: Math.max(0, options.durationMs),
			lastRunAt: new Date().toISOString(),
			lastError: options.error ?? null,
			lastPrimitiveTool: options.lastPrimitiveTool ?? null,
			lastPrimitiveStep: options.lastPrimitiveStep ?? null,
			toolSchemaFingerprint: options.toolSchemaFingerprint ?? null,
			state:
				consecutiveFailures >= (options.degradeAfterFailures ?? 3)
					? "degraded"
					: "active",
		};
		health.records[options.fingerprint] = record;
		health.updatedAt = new Date().toISOString();
		await atomicWrite(getPluginPaths(options.cwd).catalogHealthFile, health);
		return record;
	});
}

export async function resetCandidateHealth(options: {
	cwd: string;
	name: string;
}): Promise<number> {
	return withHealthLock(options.cwd, async () => {
		const health = await loadCatalogHealth(options.cwd);
		let changed = 0;
		for (const [fingerprint, record] of Object.entries(health.records)) {
			if (record.name !== options.name) continue;
			delete health.records[fingerprint];
			changed += 1;
		}
		if (changed > 0) {
			health.updatedAt = new Date().toISOString();
			await atomicWrite(getPluginPaths(options.cwd).catalogHealthFile, health);
		}
		return changed;
	});
}
