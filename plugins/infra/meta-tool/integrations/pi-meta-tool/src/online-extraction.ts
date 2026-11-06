import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { runBridge } from "./bridge.ts";
import {
	acquireProjectLease,
	loadCatalogManifest,
	publishDiscoveryArtifacts,
} from "./catalog-store.ts";
import { getPluginPaths, loadCatalogFile } from "./config.ts";
import {
	loadCheckpoint,
	readOnlineSnapshot,
	saveCheckpoint,
} from "./online-journal.ts";
import type { PluginConfig } from "./types.ts";

export type OnlineExtractionState =
	| "idle"
	| "pending"
	| "busy"
	| "running"
	| "succeeded"
	| "failed";

export interface OnlineExtractionStatus {
	version: 1;
	state: OnlineExtractionState;
	updatedAt: string;
	reason: string;
	newSuccessfulTraces: number;
	totalSuccessfulTraces: number;
	toolCalls: number;
	activeCatalogVersion: number;
	published: boolean;
	remainingSuccessfulTraces?: number;
	retryAfterMs?: number;
	result?: Record<string, unknown>;
	error?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function atomicWrite(path: string, value: unknown): Promise<void> {
	await mkdir(dirname(path), { recursive: true });
	const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
	await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
	await rename(temporary, path);
}

async function writeStatus(
	cwd: string,
	status: Omit<OnlineExtractionStatus, "version" | "updatedAt">,
): Promise<OnlineExtractionStatus> {
	const complete: OnlineExtractionStatus = {
		version: 1,
		updatedAt: new Date().toISOString(),
		...status,
	};
	await atomicWrite(getPluginPaths(cwd).onlineStatusFile, complete);
	return complete;
}

export async function loadOnlineExtractionStatus(
	cwd: string,
): Promise<OnlineExtractionStatus | undefined> {
	try {
		const value: unknown = JSON.parse(
			await readFile(getPluginPaths(cwd).onlineStatusFile, "utf8"),
		);
		return isRecord(value) && value.version === 1
			? (value as unknown as OnlineExtractionStatus)
			: undefined;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
		throw error;
	}
}

async function archiveAttempt(
	cwd: string,
	stagingDirectory: string,
	suffix: string,
): Promise<string | undefined> {
	if (!existsSync(stagingDirectory)) return undefined;
	const paths = getPluginPaths(cwd);
	await mkdir(paths.onlineRunsDir, { recursive: true });
	const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
	const destination = join(
		paths.onlineRunsDir,
		`${timestamp}-${suffix}-${randomUUID().slice(0, 8)}`,
	);
	await rename(stagingDirectory, destination);
	return destination;
}

function elapsedSince(timestamp: string | null): number {
	if (!timestamp) return 0;
	const parsed = Date.parse(timestamp);
	return Number.isFinite(parsed) ? Math.max(0, Date.now() - parsed) : 0;
}

/**
 * Run one project-scoped online extraction attempt.
 *
 * This function is process-independent: a filesystem lease serializes all Pi
 * sessions, journals are read from every session, and the checkpoint advances
 * only after the Python discovery process finishes successfully.
 */
export async function runOnlineExtraction(options: {
	cwd: string;
	config: PluginConfig;
	supportedTools: ReadonlySet<string>;
	/**
	 * Tools visible in traces/current Pi, including tools the host cannot
	 * re-enter programmatically. These are retained for extraction provenance;
	 * only supportedTools may enter the executable catalog.
	 */
	observedTools?: ReadonlySet<string>;
	force?: boolean;
	/** Flush a closing session's new evidence without bypassing admission or
	 * reprocessing already checkpointed traces. */
	drain?: boolean;
	onProgress?: (message: string) => void;
}): Promise<OnlineExtractionStatus> {
	const manifestBefore = await loadCatalogManifest(options.cwd);
	const activeVersionBefore = manifestBefore?.activeVersion ?? 0;
	if (!options.config.online.enabled && !options.force)
		return writeStatus(options.cwd, {
			state: "idle",
			reason: "online extraction is disabled",
			newSuccessfulTraces: 0,
			totalSuccessfulTraces: 0,
			toolCalls: 0,
			activeCatalogVersion: activeVersionBefore,
			published: false,
		});

	const lease = await acquireProjectLease(
		options.cwd,
		options.config.online.leaseMs,
	);
	if (!lease)
		return writeStatus(options.cwd, {
			state: "busy",
			reason: "another Pi session owns the project extraction lease",
			newSuccessfulTraces: 0,
			totalSuccessfulTraces: 0,
			toolCalls: 0,
			activeCatalogVersion: activeVersionBefore,
			published: false,
		});

	let stagingDirectory: string | undefined;
	let heartbeat: NodeJS.Timeout | undefined;
	let leaseError: unknown;
	let newSuccessfulTraces = 0;
	let totalSuccessfulTraces = 0;
	let totalCalls = 0;
	try {
		const checkpoint = await loadCheckpoint(options.cwd);
		const snapshot = await readOnlineSnapshot(options.cwd, {
			maxTraces: options.config.online.maxTraces,
			maxTraceAgeDays: options.config.online.maxTraceAgeDays,
			maxCalls: options.config.online.maxCallsPerExtraction,
			maxBytes: options.config.online.maxBytesPerExtraction,
			traceEligibility: options.config.online.traceEligibility,
			maxRecoverableErrorsPerTask:
				options.config.online.maxRecoverableErrorsPerTask,
			minRecoveredSegmentCalls:
				options.config.online.minRecoveredSegmentCalls,
			checkpoint,
		});
		newSuccessfulTraces = snapshot.newSuccessfulTraces;
		totalSuccessfulTraces = snapshot.totalSuccessfulTraces;
		totalCalls = snapshot.totalCalls;
		if (
			snapshot.newSuccessfulTraces === 0 &&
			(!options.force || snapshot.totalSuccessfulTraces === 0)
		)
			return writeStatus(options.cwd, {
				state: "idle",
				reason: "no new eligible primitive workflow traces",
				newSuccessfulTraces: 0,
				totalSuccessfulTraces: snapshot.totalSuccessfulTraces,
				toolCalls: snapshot.totalCalls,
				activeCatalogVersion: activeVersionBefore,
				published: false,
			});

		const pendingSince = checkpoint.pendingSince ?? new Date().toISOString();
		const reachedThreshold =
			snapshot.newSuccessfulTraces >=
				options.config.online.minNewSuccessfulTraces ||
			snapshot.remainingNewSuccessfulTraces > 0;
		const reachedMaxWait =
			checkpoint.pendingSince !== null &&
			elapsedSince(checkpoint.pendingSince) >= options.config.online.maxWaitMs;
		if (!options.force && !options.drain && !reachedThreshold && !reachedMaxWait) {
			await saveCheckpoint(options.cwd, { ...checkpoint, pendingSince });
			const retryAfterMs = Math.max(
				0,
				options.config.online.maxWaitMs - elapsedSince(pendingSince),
			);
			return writeStatus(options.cwd, {
				state: "pending",
				reason: `waiting for ${options.config.online.minNewSuccessfulTraces} new eligible traces or maxWaitMs`,
				newSuccessfulTraces: snapshot.newSuccessfulTraces,
				totalSuccessfulTraces: snapshot.totalSuccessfulTraces,
				toolCalls: snapshot.totalCalls,
				activeCatalogVersion: activeVersionBefore,
				published: false,
				retryAfterMs,
			});
		}

		const paths = getPluginPaths(options.cwd);
		await mkdir(paths.stagingDir, { recursive: true });
		stagingDirectory = join(paths.stagingDir, randomUUID());
		await mkdir(stagingDirectory, { recursive: true });
		await writeStatus(options.cwd, {
			state: "running",
			reason: options.force
				? "forced online extraction"
				: options.drain
					? "session closeout: drain new evidence"
				: reachedThreshold
					? "new-trace threshold reached"
					: "maximum wait reached",
			newSuccessfulTraces: snapshot.newSuccessfulTraces,
			totalSuccessfulTraces: snapshot.totalSuccessfulTraces,
			toolCalls: snapshot.totalCalls,
			activeCatalogVersion: activeVersionBefore,
			published: false,
		});

		heartbeat = setInterval(
			() =>
				void lease.heartbeat().catch((error: unknown) => {
					leaseError = error;
				}),
			Math.max(5_000, Math.floor(options.config.online.leaseMs / 3)),
		);
		const resultValue = await runBridge({
			pythonCommand: options.config.pythonCommand,
			bridgePath: options.config.bridgePath,
			metaToolRoot: options.config.metaToolRoot,
			timeoutMs: options.config.timeoutMs,
			signal: undefined,
			payload: {
				operation: "discover",
				trace: snapshot.trace,
				outputDir: stagingDirectory,
				adapter: {
					supportedTools: [...options.supportedTools],
					observedTools: [
						...(options.observedTools ?? options.supportedTools),
					],
				},
				config: {
					normalization: options.config.normalization,
					optimization: options.config.optimization,
					extraction: options.config.extraction,
					compiler: options.config.compiler,
					admission: options.config.admission,
				},
			},
			onProgress: options.onProgress,
		});
		const result = isRecord(resultValue) ? resultValue : {};
		const activeCatalog = await loadCatalogFile(
			join(stagingDirectory, "active_meta_tools.json"),
		);
		if (leaseError) throw leaseError;
		await lease.assertOwned();
		let published = false;
		let activeVersion = activeVersionBefore;
		let reason = "discovery completed without an admitted workflow";
		if (
			options.config.online.mode === "shadow_then_active" &&
			activeCatalog.meta_tools.length > 0
		) {
			const manifest = await publishDiscoveryArtifacts({
				cwd: options.cwd,
				stagingDirectory,
				source: "online",
				traceCount: snapshot.totalSuccessfulTraces,
				toolCallCount: snapshot.totalCalls,
				watermarks: snapshot.watermarks,
				preservePreviousVersions:
					options.config.catalog.preservePreviousVersions,
			});
			stagingDirectory = undefined;
			published = true;
			activeVersion = manifest.activeVersion;
			reason = `published ${activeCatalog.meta_tools.length} admitted workflow(s)`;
		} else {
			const suffix =
				options.config.online.mode === "collect_only"
					? "collect-only"
					: "no-active";
			const archivedDirectory = await archiveAttempt(options.cwd, stagingDirectory, suffix);
			if (archivedDirectory) result.output = archivedDirectory;
			stagingDirectory = undefined;
			if (options.config.online.mode === "collect_only")
				reason = "discovery completed in collect-only mode";
		}

		await lease.assertOwned();
		await saveCheckpoint(options.cwd, {
			version: 1,
			watermarks: snapshot.watermarks,
			lastExtractionAt: new Date().toISOString(),
			lastCatalogVersion: activeVersion,
			pendingSince: null,
		});
		return writeStatus(options.cwd, {
			state: "succeeded",
			reason,
			newSuccessfulTraces: snapshot.newSuccessfulTraces,
			totalSuccessfulTraces: snapshot.totalSuccessfulTraces,
			toolCalls: snapshot.totalCalls,
			activeCatalogVersion: activeVersion,
			published,
			remainingSuccessfulTraces: snapshot.remainingNewSuccessfulTraces,
			result,
		});
	} catch (error) {
		if (stagingDirectory) {
			await archiveAttempt(options.cwd, stagingDirectory, "failed").catch(() =>
				rm(stagingDirectory as string, { recursive: true, force: true }),
			);
		}
		const message = error instanceof Error ? error.message : String(error);
		return writeStatus(options.cwd, {
			state: "failed",
			reason: "online extraction failed; checkpoint was not advanced",
			newSuccessfulTraces,
			totalSuccessfulTraces,
			toolCalls: totalCalls,
			activeCatalogVersion: activeVersionBefore,
			published: false,
			error: message,
		});
	} finally {
		if (heartbeat) clearInterval(heartbeat);
		await lease.release();
	}
}
