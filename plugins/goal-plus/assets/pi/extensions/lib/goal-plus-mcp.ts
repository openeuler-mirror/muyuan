import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// Node's largest timer delay. Domain deadlines and host cancellation govern work;
// the SDK's default 60-second request timeout cannot bound a frozen verifier.
const MAX_TIMER_DELAY_MS = 2_147_483_647;
// Match Goal Plus's per-stream operational output bound.
const MAX_FRAME_BYTES = 8 * 1024 * 1024;

export class PiMcpConnection {
	readonly client = new Client({ name: "goal-plus-pi", version: "1" });
	readonly transport: StdioClientTransport;
	private closed = false;

	constructor(command: string, args: string[], cwd: string) {
		this.transport = new StdioClientTransport({
			command, args, cwd, stderr: "pipe", maxBufferSize: MAX_FRAME_BYTES,
			env: Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)),
		});
		this.transport.stderr?.on("data", () => {});
		this.client.onclose = () => { this.closed = true; };
	}

	async open() {
		try {
			await this.client.connect(this.transport);
			const tools = [];
			let cursor: string | undefined;
			do {
				const page = await this.client.listTools(cursor ? { cursor } : undefined);
				tools.push(...page.tools);
				cursor = page.nextCursor;
			} while (cursor);
			return { server: this.client.getServerVersion(), instructions: this.client.getInstructions(), tools };
		} catch (error) {
			await this.close();
			throw error;
		}
	}

	async call(name: string, args: Record<string, unknown>, signal?: AbortSignal, nativeSessionId?: string) {
		if (this.closed) throw new Error("Goal Plus MCP connection is closed");
		const result = await this.client.callTool({
			name, arguments: args,
			...(nativeSessionId ? { _meta: { threadId: nativeSessionId } } : {}),
		}, undefined, {
			signal, timeout: MAX_TIMER_DELAY_MS,
		});
		const text = result.content.filter(item => item.type === "text").map(item => item.text).join("\n");
		if (result.isError) {
			return { content: [{ type: "text" as const, text }], details: { ok: false, error: text, tool: name } };
		}
		if (result.content.length !== 1 || result.content[0].type !== "text") {
			throw new Error("Goal Plus MCP returned an unsupported result representation");
		}
		return { content: [{ type: "text" as const, text }], details: JSON.parse(text) };
	}

	async close() {
		this.closed = true;
		await this.client.close();
	}
}
