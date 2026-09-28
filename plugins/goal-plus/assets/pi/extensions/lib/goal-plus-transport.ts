import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { PiMcpConnection } from "./goal-plus-mcp.ts";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, isAbsolute, join, resolve } from "node:path";

// Identity belongs to this installed package, never to the target workspace.
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
const receiptPath = join(packageRoot, ".goal-plus-runtime.json");
const receipt = existsSync(receiptPath) ? JSON.parse(readFileSync(receiptPath, "utf8")) : undefined;
const devRoot = !receipt && process.env.GOAL_PLUS_PI_DEV_ROOT === packageRoot ? packageRoot : undefined;
export const pythonExecutable = devRoot
	? process.env.GOAL_PLUS_PYTHON || ""
	: receipt?.python || "";

export const runtimeRoot = process.env.GOAL_PLUS_ROOT || ".gp";

export const INSTALL_HINT =
	'Run ./install.sh --pi from the Goal Plus source checkout, then restart Pi.';

export interface CommandInvocation {
	command: string;
	argsPrefix: string[];
	label: string;
}

export interface CommandRuntimeContext {
	cwd: string;
	nativeSessionId?: string;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

export function sourceRoot(ctx: CommandRuntimeContext): string {
	return devRoot || packageRoot;
}

export function projectModuleInvocation(ctx: CommandRuntimeContext, command: string, moduleName: string): CommandInvocation {
	if (devRoot) {
		if (!isAbsolute(pythonExecutable)) throw new Error("Pi development launcher must supply an absolute Python path");
		const src = join(devRoot, "src");
		const code = ["import sys", `sys.path.insert(0, ${JSON.stringify(src)})`,
			`from ${moduleName} import main`, "raise SystemExit(main())"].join("; ");
		return { command: pythonExecutable, argsPrefix: ["-I", "-c", code], label: moduleName };
	}
	if (receipt?.schema !== "goal-plus.managed-runtime.v1" || !isAbsolute(pythonExecutable) || !receipt?.build) {
		throw new Error(INSTALL_HINT);
	}
	return { command: pythonExecutable,
		argsPrefix: ["-I", "-m", "goal_plus.installation", "--expected-build", receipt.build, "--module", moduleName],
		label: `${moduleName} (${receipt.build})` };
}

export function parseJsonObject(text: string): Record<string, unknown> | undefined {
	const trimmed = text.trim();
	if (!trimmed) return undefined;
	try {
		const parsed = JSON.parse(trimmed);
		return isRecord(parsed) ? parsed : undefined;
	} catch {
		return undefined;
	}
}

export function isEnvironmentFailure(text: string): boolean {
	const normalized = text.toLowerCase();
	return (
		text.includes("ModuleNotFoundError") ||
		normalized.includes("no module named") ||
		normalized.includes("cannot find module") ||
		normalized.includes("command not found") ||
		normalized.includes("enoent") ||
		normalized.includes("not found:")
	);
}

export function commandFailure(
	tool: string,
	invocation: CommandInvocation,
	result: { stdout: string; stderr: string; code: number },
): { text: string; details: Record<string, unknown> } {
	const output = (result.stderr || result.stdout || `${invocation.label} failed with exit code ${result.code}`).trim();
	const parsed = parseJsonObject(output);
	const baseError = typeof parsed?.error === "string" ? parsed.error : output;
	const text = isEnvironmentFailure(baseError) ? `${baseError}\n\n${INSTALL_HINT}` : baseError;
	return {
		text,
		details: {
			...(parsed ?? {}),
			tool: typeof parsed?.tool === "string" ? parsed.tool : tool,
			ok: false,
			error: text,
		},
	};
}

const mcpConnections = new Map<string, Promise<{ connection: PiMcpConnection; manifest: Awaited<ReturnType<PiMcpConnection["open"]>> }>>();
const sharedTools = new Set<string>();

function mcpConnection(ctx: CommandRuntimeContext) {
	const root = resolve(ctx.cwd, runtimeRoot);
	let pending = mcpConnections.get(root);
	if (!pending) {
		const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
		pending = (async () => {
			const { PiMcpConnection } = await import("./goal-plus-mcp.ts");
			const connection = new PiMcpConnection(invocation.command, [
				...invocation.argsPrefix, "--root", root, "host_mcp_serve",
			], ctx.cwd);
			const manifest = await connection.open();
			for (const tool of manifest.tools) sharedTools.add(tool.name);
			return { connection, manifest };
		})();
		mcpConnections.set(root, pending);
	}
	return pending;
}

// Bound optional rendering so debug export cannot hold a host turn indefinitely.
const DEBUG_EXPORT_TIMEOUT_MS = 30_000;

export async function refreshDebugTranscript(pi: ExtensionAPI, ctx: CommandRuntimeContext) {
	if (process.env.GOAL_PLUS_DEBUG !== "1") return;
	const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
	const result = await pi.exec(invocation.command, [...invocation.argsPrefix,
		"--root", resolve(ctx.cwd, runtimeRoot), "host_debug_export"], { timeout: DEBUG_EXPORT_TIMEOUT_MS });
	if (result.code !== 0) throw new Error("debug transcript export failed");
}

export async function readRuntimeInfo(pi: ExtensionAPI, ctx: CommandRuntimeContext): Promise<string> {
	const invocation = projectModuleInvocation(ctx, "goal-plus", "goal_plus.cli");
	const result = await pi.exec(invocation.command, [...invocation.argsPrefix,
		"--root", resolve(ctx.cwd, runtimeRoot), "--runtime-info", "pi"], { timeout: 10_000, cwd: ctx.cwd });
	if (result.code !== 0) throw new Error(result.stderr.trim() || "Goal Plus runtime inspection failed");
	return result.stdout.trim();
}

export async function closeMcpConnections() {
	const pending = [...mcpConnections.values()];
	mcpConnections.clear();
	sharedTools.clear();
	const results = await Promise.allSettled(pending.map(async value => (await value).connection.close()));
	for (const result of results) {
		if (result.status === "rejected") throw result.reason;
	}
}

export async function runJsonCli(pi: ExtensionAPI, ctx: CommandRuntimeContext, tool: string, args: Record<string, unknown>, signal?: AbortSignal) {
	// Protected TT Children use Root Message RPC and only obtain schemas locally.
	const rootMessageOnly = Boolean(process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY) && (process.env.GOAL_PLUS_PI_ROLE || "main") !== "main";
	if (!rootMessageOnly && (tool === "host_mcp_manifest" || sharedTools.has(tool))) {
		const { connection, manifest } = await mcpConnection(ctx);
		return tool === "host_mcp_manifest"
			? { content: [{ type: "text" as const, text: JSON.stringify(manifest) }], details: manifest }
			: connection.call(tool, args, signal, ctx.nativeSessionId);
	}
	const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
	const result = await pi.exec(invocation.command, [
		...invocation.argsPrefix,
		"--root",
		runtimeRoot,
		"--args-json",
		JSON.stringify(args),
		tool,
	]);
	if (result.code !== 0) {
		const failure = commandFailure(tool, invocation, result);
		return {
			content: [{ type: "text" as const, text: failure.text }],
			details: failure.details,
		};
	}
	const parsed = JSON.parse(result.stdout || "null");
	return {
		content: [{ type: "text" as const, text: JSON.stringify(parsed) }],
		details: parsed,
	};
}

export async function runHostCommandCli(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	tool: "goal_plus_host_start" | "goal_plus_host_edit" | "goal_plus_host_control" | "goal_plus_host_session_event" | "goal_plus_host_kick_internal_agents",
	args: Record<string, unknown>,
) {
	const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
	const capabilityToken = randomUUID();
	const capabilityRoot = isAbsolute(runtimeRoot) ? runtimeRoot : join(ctx.cwd, runtimeRoot);
	const capabilityDir = join(capabilityRoot, "host-entrypoint", "pi");
	const capabilityPath = join(capabilityDir, `${capabilityToken}.json`);
	mkdirSync(capabilityDir, { recursive: true });
	writeFileSync(
		capabilityPath,
		JSON.stringify({ token: capabilityToken, issued_at_ms: Date.now() }),
		{ encoding: "utf8", flag: "wx", mode: 0o600 },
	);
	const result = await (async () => {
		try {
			return await pi.exec(invocation.command, [
				...invocation.argsPrefix,
				"--root",
				runtimeRoot,
				"--host-entrypoint",
				"--host-capability",
				capabilityToken,
				"--args-json",
				JSON.stringify(args),
				tool,
			]);
		} finally {
			rmSync(capabilityPath, { force: true });
		}
	})();
	if (result.code !== 0) {
		const failure = commandFailure(tool, invocation, result);
		return {
			content: [{ type: "text" as const, text: failure.text }],
			details: failure.details,
		};
	}
	const parsed = JSON.parse(result.stdout || "null");
	return {
		content: [{ type: "text" as const, text: JSON.stringify(parsed, null, 2) }],
		details: parsed,
	};
}
