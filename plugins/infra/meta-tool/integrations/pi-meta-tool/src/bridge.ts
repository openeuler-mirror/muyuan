import { spawn } from "node:child_process";
import { delimiter, join } from "node:path";
import { createInterface } from "node:readline";
import type { BridgeRunOptions } from "./types.ts";

interface BridgeMessage {
	type: string;
	id?: string;
	message?: string;
	name?: string;
	input?: Record<string, unknown>;
	result?: unknown;
	error?: string;
}

function parseMessage(line: string): BridgeMessage {
	const parsed: unknown = JSON.parse(line);
	if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
		throw new Error("Meta-tool bridge emitted a non-object JSON message");
	const message = parsed as Record<string, unknown>;
	if (typeof message.type !== "string")
		throw new Error("Meta-tool bridge message is missing type");
	return message as unknown as BridgeMessage;
}

export async function runBridge(options: BridgeRunOptions): Promise<unknown> {
	return new Promise((resolve, reject) => {
		const sourceRoot = join(options.metaToolRoot, "src");
		const existingPythonPath = process.env.PYTHONPATH;
		const child = spawn(options.pythonCommand, [options.bridgePath], {
			cwd: options.metaToolRoot,
			env: {
				...process.env,
				META_TOOL_ROOT: options.metaToolRoot,
				PYTHONPATH: existingPythonPath
					? `${sourceRoot}${delimiter}${existingPythonPath}`
					: sourceRoot,
			},
			stdio: ["pipe", "pipe", "pipe"],
		});
		const lines = createInterface({ input: child.stdout });
		let stderr = "";
		let settled = false;
		let writeChain = Promise.resolve();

		const finish = (error: Error | undefined, result?: unknown) => {
			if (settled) return;
			settled = true;
			clearTimeout(timeout);
			options.signal?.removeEventListener("abort", abort);
			lines.close();
			if (!child.killed) child.kill("SIGTERM");
			if (error) reject(error);
			else resolve(result);
		};

		const send = (message: Record<string, unknown>) => {
			writeChain = writeChain.then(
				() =>
					new Promise<void>((writeResolve, writeReject) => {
						child.stdin.write(`${JSON.stringify(message)}\n`, (error) => {
							if (error) writeReject(error);
							else writeResolve();
						});
					}),
			);
			writeChain.catch((error: unknown) =>
				finish(error instanceof Error ? error : new Error(String(error))),
			);
		};

		const abort = () => finish(new Error("Meta-tool operation cancelled"));
		const timeout = setTimeout(
			() =>
				finish(new Error(`Meta-tool bridge timed out after ${options.timeoutMs}ms`)),
			options.timeoutMs,
		);

		child.stderr.on("data", (chunk: Buffer) => {
			stderr += chunk.toString("utf8");
			if (stderr.length > 64_000) stderr = stderr.slice(-64_000);
		});
		child.on("error", (error) => finish(error));
		child.on("exit", (code, signal) => {
			if (settled) return;
			const suffix = stderr.trim() ? `\n${stderr.trim()}` : "";
			finish(
				new Error(
					`Meta-tool bridge exited before completion (code=${code}, signal=${signal})${suffix}`,
				),
			);
		});

		lines.on("line", (line) => {
			if (!line.trim() || settled) return;
			let message: BridgeMessage;
			try {
				message = parseMessage(line);
			} catch (error) {
				finish(
					new Error(`Invalid meta-tool bridge output: ${line}\n${String(error)}`),
				);
				return;
			}
			if (message.type === "progress" && message.message) {
				options.onProgress?.(message.message);
				return;
			}
			if (
				message.type === "tool_call" &&
				message.id &&
				message.name &&
				message.input &&
				options.onToolCall
			) {
				options.onToolCall(message.id, message.name, message.input).then(
					(result) => send({ type: "tool_result", id: message.id, result }),
					(error: unknown) =>
						send({ type: "tool_result", id: message.id, error: String(error) }),
				);
				return;
			}
			if (message.type === "complete") {
				finish(undefined, message.result);
				return;
			}
			if (message.type === "error") {
				const suffix = stderr.trim() ? `\n${stderr.trim()}` : "";
				finish(
					new Error(`${message.error ?? "Unknown meta-tool bridge error"}${suffix}`),
				);
			}
		});

		if (options.signal?.aborted) {
			abort();
			return;
		}
		options.signal?.addEventListener("abort", abort, { once: true });
		send(options.payload);
	});
}
