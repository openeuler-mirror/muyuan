import type {
	AgentToolResult,
	ExtensionAPI,
} from "@earendil-works/pi-coding-agent";
import {
	Client,
	StreamableHTTPClientTransport,
	type Tool,
	type Transport,
} from "@modelcontextprotocol/client";
import {
	getDefaultEnvironment,
	StdioClientTransport,
} from "@modelcontextprotocol/client/stdio";
import { Type } from "typebox";
import {
	discoverMcpProviders,
	type McpProviderConfig,
	type McpProviderDiscovery,
} from "./provider-config.ts";
import {
	META_TOOL_RUN_NAME,
	META_TOOL_SEARCH_NAME,
	type PluginConfig,
} from "./types.ts";

interface ProviderConnection {
	config: McpProviderConfig;
	fingerprint: string;
	client: Client;
	tools: Tool[];
}

interface ToolBinding {
	registeredName: string;
	serverId: string;
	remoteName: string;
	readOnly: boolean;
	canonical: boolean;
	config: McpProviderConfig;
	client: Client;
	tool: Tool;
}

export interface McpProviderStatus {
	configFiles: string[];
	configuredProviders: number;
	connectedProviders: number;
	remoteTools: number;
	registeredTools: number;
	canonicalTools: number;
	providers: Array<{
		id: string;
		transport: "stdio" | "http";
		tools: number;
		sourcePath: string;
	}>;
	diagnostics: string[];
}

type ProviderDiscoveryLoader = (cwd: string) => Promise<McpProviderDiscovery>;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function providerFingerprint(config: McpProviderConfig): string {
	return JSON.stringify(config);
}

function escapedPattern(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function safeServerSegment(value: string): string {
	return value.replace(/[^A-Za-z0-9_.-]+/g, "_");
}

function unique(values: string[]): string[] {
	return [...new Set(values.filter(Boolean))];
}

function toolNames(config: McpProviderConfig, remoteName: string): string[] {
	const server = safeServerSegment(config.id);
	const prefixed = config.toolPrefix
		? `${config.toolPrefix}${remoteName}`
		: remoteName;
	return unique([
		prefixed,
		remoteName,
		`${server}.${remoteName}`,
		`${server}__${remoteName}`,
		`mcp__${server}__${remoteName}`,
	]);
}

function normalizeInputSchema(value: unknown): Record<string, unknown> {
	if (!isRecord(value))
		return { type: "object", properties: {}, additionalProperties: true };
	return value.type === "object"
		? value
		: { ...value, type: "object", properties: value.properties ?? {} };
}

function jsonText(value: unknown): string {
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value);
	}
}

function resultContent(value: unknown): AgentToolResult<unknown>["content"] {
	if (!isRecord(value)) return [{ type: "text", text: jsonText(value) }];
	const output: AgentToolResult<unknown>["content"] = [];
	if (Array.isArray(value.content)) {
		for (const block of value.content) {
			if (!isRecord(block) || typeof block.type !== "string") continue;
			if (block.type === "text" && typeof block.text === "string") {
				output.push({ type: "text", text: block.text });
				continue;
			}
			if (
				block.type === "image" &&
				typeof block.data === "string" &&
				typeof block.mimeType === "string"
			) {
				output.push({
					type: "image",
					data: block.data,
					mimeType: block.mimeType,
				});
				continue;
			}
			output.push({ type: "text", text: jsonText(block) });
		}
	}
	if (output.length === 0 && value.structuredContent !== undefined) {
		output.push({ type: "text", text: jsonText(value.structuredContent) });
	}
	if (output.length === 0)
		output.push({ type: "text", text: "MCP tool completed." });
	return output;
}

function resultErrorText(content: AgentToolResult<unknown>["content"]): string {
	const text = content
		.flatMap((block) => (block.type === "text" ? [block.text] : []))
		.join("\n")
		.trim();
	return text || "MCP tool reported an error";
}

async function callBinding(
	binding: ToolBinding,
	params: Record<string, unknown>,
	signal?: AbortSignal,
): Promise<AgentToolResult<unknown>> {
	const result = await binding.client.callTool(
		{ name: binding.remoteName, arguments: params },
		{
			timeout: binding.config.timeoutMs,
			...(signal ? { signal } : {}),
		},
	);
	const content = resultContent(result);
	if (result.isError) throw new Error(resultErrorText(content));
	return {
		content,
		details: {
			provider: "mcp",
			server: binding.serverId,
			tool: binding.remoteName,
			structuredContent: result.structuredContent,
		},
	};
}

function toolSemantics(tool: Tool): {
	readOnly: boolean;
	destructive: boolean;
} {
	const readOnly = tool.annotations?.readOnlyHint === true;
	const destructive = tool.annotations?.destructiveHint === true;
	return { readOnly: readOnly && !destructive, destructive };
}

async function connectProvider(
	config: McpProviderConfig,
): Promise<ProviderConnection> {
	let transport: Transport;
	let stderr = "";
	if (config.transport === "stdio") {
		const stdio = new StdioClientTransport({
			command: config.command,
			args: config.args,
			cwd: config.cwd,
			env: { ...getDefaultEnvironment(), ...config.env },
			stderr: "pipe",
			maxBufferSize: 64 * 1024 * 1024,
		});
		stdio.stderr?.on("data", (chunk: Buffer | string) => {
			stderr += chunk.toString();
			if (stderr.length > 16_000) stderr = stderr.slice(-16_000);
		});
		transport = stdio;
	} else {
		transport = new StreamableHTTPClientTransport(new URL(config.url), {
			requestInit: { headers: config.headers },
		});
	}

	const client = new Client(
		{ name: "pi-meta-tool-online", version: "2.0.0" },
		{
			versionNegotiation: {
				mode: "auto",
				probe: { timeoutMs: Math.min(config.timeoutMs, 3_000) },
			},
		},
	);
	try {
		await client.connect(transport);
		const { tools } = await client.listTools(undefined, {
			timeout: config.timeoutMs,
		});
		return {
			config,
			fingerprint: providerFingerprint(config),
			client,
			tools,
		};
	} catch (error) {
		await client.close().catch(() => undefined);
		const suffix = stderr.trim() ? `; stderr: ${stderr.trim()}` : "";
		throw new Error(
			`${error instanceof Error ? error.message : String(error)}${suffix}`,
		);
	}
}

function registrationDescription(binding: ToolBinding): string {
	const description = binding.tool.description?.trim() || binding.remoteName;
	return `${description}\n\nProvided automatically by MCP server ${binding.serverId}.`;
}

/**
 * Discovers MCP servers, mirrors their tool catalogs into Pi, and supplies the
 * tool semantics needed by Meta-tool admission. No workflow-specific adapter is
 * involved: every registered definition delegates tools/call to its provider.
 */
export class McpProviderManager {
	private connections = new Map<string, ProviderConnection>();
	private registeredNames = new Set<string>();
	private bindings = new Map<string, ToolBinding>();
	private lastDiscovery: McpProviderDiscovery = {
		providers: [],
		configFiles: [],
		diagnostics: [],
	};
	private diagnostics: string[] = [];
	private readonly discoverProviders: ProviderDiscoveryLoader;

	constructor(
		discoverProviders: ProviderDiscoveryLoader = discoverMcpProviders,
	) {
		this.discoverProviders = discoverProviders;
	}

	async refresh(
		pi: ExtensionAPI,
		cwd: string,
		trusted: boolean,
	): Promise<McpProviderStatus> {
		if (!trusted) {
			await this.close();
			this.diagnostics = [
				"Project is not trusted; MCP providers were not started.",
			];
			return this.status();
		}

		this.lastDiscovery = await this.discoverProviders(cwd);
		const diagnostics = [...this.lastDiscovery.diagnostics];
		const desiredIds = new Set(
			this.lastDiscovery.providers.map((item) => item.id),
		);
		for (const [id, connection] of this.connections) {
			const replacement = this.lastDiscovery.providers.find(
				(item) => item.id === id,
			);
			if (
				!desiredIds.has(id) ||
				(replacement &&
					providerFingerprint(replacement) !== connection.fingerprint)
			) {
				await connection.client.close().catch(() => undefined);
				this.connections.delete(id);
			}
		}

		for (const config of this.lastDiscovery.providers) {
			const current = this.connections.get(config.id);
			try {
				if (current) {
					const { tools } = await current.client.listTools(undefined, {
						timeout: config.timeoutMs,
						cacheMode: "refresh",
					});
					current.tools = tools;
				} else {
					this.connections.set(config.id, await connectProvider(config));
				}
			} catch (error) {
				diagnostics.push(
					`${config.id}: ${error instanceof Error ? error.message : String(error)}`,
				);
				if (current) {
					await current.client.close().catch(() => undefined);
					this.connections.delete(config.id);
				}
			}
		}

		const activeBeforeRegistration = pi.getActiveTools();
		const externalNames = new Set(
			pi
				.getAllTools()
				.map((tool) => tool.name)
				.filter((name) => !this.registeredNames.has(name)),
		);
		const claimedNames = new Set(externalNames);
		const nextBindings = new Map<string, ToolBinding>();
		const autoActiveNames = new Set<string>();

		for (const connection of [...this.connections.values()].sort(
			(left, right) => left.config.id.localeCompare(right.config.id),
		)) {
			for (const tool of [...connection.tools].sort((left, right) =>
				left.name.localeCompare(right.name),
			)) {
				const candidates = toolNames(connection.config, tool.name);
				const canonicalName = candidates.find(
					(name) =>
						name !== META_TOOL_RUN_NAME &&
						name !== META_TOOL_SEARCH_NAME &&
						!claimedNames.has(name),
				);
				if (!canonicalName) {
					diagnostics.push(
						`${connection.config.id}/${tool.name}: every generated Pi tool name collides`,
					);
					continue;
				}
				const names = connection.config.registerAliases
					? candidates
					: [canonicalName];
				for (const name of names) {
					if (
						name === META_TOOL_RUN_NAME ||
						name === META_TOOL_SEARCH_NAME ||
						(claimedNames.has(name) && !nextBindings.has(name))
					)
						continue;
					if (nextBindings.has(name)) continue;
					const semantics = toolSemantics(tool);
					const binding: ToolBinding = {
						registeredName: name,
						serverId: connection.config.id,
						remoteName: tool.name,
						readOnly: semantics.readOnly,
						canonical: name === canonicalName,
						config: connection.config,
						client: connection.client,
						tool,
					};
					claimedNames.add(name);
					nextBindings.set(name, binding);
					this.registeredNames.add(name);
					pi.registerTool({
						name,
						label:
							tool.title ??
							tool.annotations?.title ??
							`MCP: ${connection.config.id}/${tool.name}`,
						description: registrationDescription(binding),
						promptSnippet:
							name === canonicalName
								? `Call ${tool.name} through MCP server ${connection.config.id}`
								: undefined,
						parameters: Type.Unsafe<Record<string, unknown>>(
							normalizeInputSchema(tool.inputSchema),
						),
						executionMode: binding.readOnly ? "parallel" : "sequential",
						async execute(_toolCallId, params, signal, _onUpdate, ctx) {
							if (!ctx.isProjectTrusted())
								throw new Error(
									"MCP tool execution requires a trusted project.",
								);
							return callBinding(binding, params, signal);
						},
					});
					if (name === canonicalName && connection.config.autoActivate)
						autoActiveNames.add(name);
				}
			}
		}

		const visibleNames = new Set(pi.getAllTools().map((tool) => tool.name));
		this.bindings = new Map(
			[...nextBindings].filter(([name]) => visibleNames.has(name)),
		);
		const preservedActive = activeBeforeRegistration.filter(
			(name) =>
				!this.registeredNames.has(name) ||
				(this.bindings.has(name) && visibleNames.has(name)),
		);
		pi.setActiveTools(
			unique([
				...preservedActive,
				...[...autoActiveNames].filter((name) => visibleNames.has(name)),
			]),
		);
		const filtered = nextBindings.size - this.bindings.size;
		if (filtered > 0)
			diagnostics.push(
				`${filtered} MCP tool name(s) were blocked by Pi's --tools/exclude filter`,
			);
		this.diagnostics = diagnostics;
		return this.status();
	}

	/** Names backed by a live provider and therefore callable without Pi internals. */
	executableToolNames(): Set<string> {
		return new Set(this.bindings.keys());
	}

	hasTool(name: string): boolean {
		return this.bindings.has(name);
	}

	/**
	 * Dispatch directly through the provider binding. This is the stock-Pi
	 * fallback for learned workflows; normal model-issued calls still use the
	 * dynamically registered Pi tool definition above.
	 */
	async callTool(
		name: string,
		params: Record<string, unknown>,
		signal: AbortSignal | undefined,
		trusted: boolean,
	): Promise<AgentToolResult<unknown>> {
		if (!trusted)
			throw new Error("MCP tool execution requires a trusted project.");
		const binding = this.bindings.get(name);
		if (!binding)
			throw new Error(`No connected MCP provider supplies tool ${name}`);
		return callBinding(binding, params, signal);
	}

	augmentConfig(config: PluginConfig): PluginConfig {
		const semantics = [...this.bindings.values()];
		const configuredRules = Array.isArray(config.normalization.tool_rules)
			? config.normalization.tool_rules
			: [];
		const configuredReadOnly = Array.isArray(config.admission.read_only_tools)
			? config.admission.read_only_tools.filter(
					(value): value is string => typeof value === "string",
				)
			: [];
		return {
			...config,
			normalization: {
				...config.normalization,
				tool_rules: [
					...semantics.map((binding) => ({
						pattern: `^${escapedPattern(binding.registeredName)}$`,
						semantic_type: binding.readOnly ? "accessor" : "mutator",
						domain: `mcp:${binding.serverId}`,
					})),
					...configuredRules,
				],
			},
			admission: {
				...config.admission,
				read_only_tools: unique([
					...configuredReadOnly,
					...semantics
						.filter((binding) => binding.readOnly)
						.map((binding) => binding.registeredName),
				]),
			},
		};
	}

	status(): McpProviderStatus {
		const connections = [...this.connections.values()].sort((left, right) =>
			left.config.id.localeCompare(right.config.id),
		);
		return {
			configFiles: this.lastDiscovery.configFiles,
			configuredProviders: this.lastDiscovery.providers.length,
			connectedProviders: connections.length,
			remoteTools: connections.reduce(
				(total, connection) => total + connection.tools.length,
				0,
			),
			registeredTools: this.bindings.size,
			canonicalTools: [...this.bindings.values()].filter(
				(binding) => binding.canonical,
			).length,
			providers: connections.map((connection) => ({
				id: connection.config.id,
				transport: connection.config.transport,
				tools: connection.tools.length,
				sourcePath: connection.config.sourcePath,
			})),
			diagnostics: this.diagnostics,
		};
	}

	async close(): Promise<void> {
		await Promise.all(
			[...this.connections.values()].map((connection) =>
				connection.client.close().catch(() => undefined),
			),
		);
		this.connections.clear();
		this.bindings.clear();
	}
}

export function formatMcpProviderStatus(status: McpProviderStatus): string {
	return [
		`MCP configs: ${status.configFiles.length}`,
		`MCP providers: ${status.connectedProviders}/${status.configuredProviders} connected`,
		`MCP tools: ${status.remoteTools} remote, ${status.canonicalTools} canonical, ${status.registeredTools} names including aliases`,
		...status.providers.map(
			(provider) =>
				`- ${provider.id} [${provider.transport}]: ${provider.tools} tool(s) (${provider.sourcePath})`,
		),
		...status.diagnostics.map((diagnostic) => `warning: ${diagnostic}`),
	].join("\n");
}
