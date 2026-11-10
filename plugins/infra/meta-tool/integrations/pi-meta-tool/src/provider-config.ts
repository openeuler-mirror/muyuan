import { homedir } from "node:os";
import { dirname, join, parse, resolve } from "node:path";
import { readFile } from "node:fs/promises";

export interface McpProviderOptions {
	autoActivate: boolean;
	registerAliases: boolean;
	toolPrefix?: string;
	timeoutMs: number;
}

interface McpProviderBase extends McpProviderOptions {
	id: string;
	sourcePath: string;
	projectRoot: string;
}

export interface McpStdioProviderConfig extends McpProviderBase {
	transport: "stdio";
	command: string;
	args: string[];
	cwd: string;
	env: Record<string, string>;
}

export interface McpHttpProviderConfig extends McpProviderBase {
	transport: "http";
	url: string;
	headers: Record<string, string>;
}

export type McpProviderConfig = McpStdioProviderConfig | McpHttpProviderConfig;

export interface McpProviderDiscovery {
	providers: McpProviderConfig[];
	configFiles: string[];
	diagnostics: string[];
}

interface DiscoveryOptions {
	homeDirectory?: string;
	includeGlobal?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
	return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function environmentValue(value: string): string {
	return value
		.replace(
			/\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}/g,
			(_match, name: string) => process.env[name] ?? "",
		)
		.replace(
			/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
			(_match, name: string) => process.env[name] ?? "",
		);
}

function stringRecord(value: unknown): Record<string, string> {
	if (!isRecord(value)) return {};
	return Object.fromEntries(
		Object.entries(value).flatMap(([key, item]) =>
			typeof item === "string" ? [[key, environmentValue(item)]] : [],
		),
	);
}

function projectRootForConfig(path: string): string {
	const parent = dirname(path);
	const parentName = parent.split(/[\\/]/).at(-1);
	return parentName === ".pi" || parentName === ".vscode"
		? dirname(parent)
		: parent;
}

function providerOptions(value: Record<string, unknown>): McpProviderOptions {
	const rawOptions = isRecord(value.metaTool)
		? value.metaTool
		: isRecord(value["meta-tool"])
			? value["meta-tool"]
			: {};
	const rawTimeout =
		typeof rawOptions.timeoutMs === "number" &&
		Number.isFinite(rawOptions.timeoutMs)
			? rawOptions.timeoutMs
			: 30_000;
	return {
		autoActivate: rawOptions.autoActivate !== false,
		registerAliases: rawOptions.registerAliases !== false,
		toolPrefix: optionalString(rawOptions.toolPrefix),
		timeoutMs: Math.max(1_000, Math.trunc(rawTimeout)),
	};
}

function parseProvider(
	id: string,
	value: unknown,
	sourcePath: string,
	launchCwd: string,
	diagnostics: string[],
): McpProviderConfig | undefined {
	if (!isRecord(value)) {
		diagnostics.push(`${sourcePath}: MCP server ${id} must be an object`);
		return undefined;
	}
	if (value.disabled === true) return undefined;

	const projectRoot = projectRootForConfig(sourcePath);
	const options = providerOptions(value);
	const type = optionalString(value.type)?.toLowerCase();
	const url = optionalString(value.url);
	if (url || type === "http" || type === "streamable-http") {
		if (!url) {
			diagnostics.push(`${sourcePath}: HTTP MCP server ${id} is missing url`);
			return undefined;
		}
		try {
			const parsed = new URL(environmentValue(url));
			if (parsed.protocol !== "http:" && parsed.protocol !== "https:")
				throw new Error(`unsupported protocol ${parsed.protocol}`);
			return {
				...options,
				id,
				sourcePath,
				projectRoot,
				transport: "http",
				url: parsed.toString(),
				headers: stringRecord(value.headers),
			};
		} catch (error) {
			diagnostics.push(
				`${sourcePath}: invalid URL for MCP server ${id}: ${error instanceof Error ? error.message : String(error)}`,
			);
			return undefined;
		}
	}

	if (type && type !== "stdio") {
		diagnostics.push(
			`${sourcePath}: MCP server ${id} uses unsupported transport ${type}`,
		);
		return undefined;
	}
	const command = optionalString(value.command);
	if (!command) {
		diagnostics.push(
			`${sourcePath}: stdio MCP server ${id} is missing command`,
		);
		return undefined;
	}
	if (
		value.args !== undefined &&
		(!Array.isArray(value.args) ||
			value.args.some((argument) => typeof argument !== "string"))
	) {
		diagnostics.push(`${sourcePath}: MCP server ${id} args must be strings`);
		return undefined;
	}
	const configuredCwd = optionalString(value.cwd);
	return {
		...options,
		id,
		sourcePath,
		projectRoot,
		transport: "stdio",
		command: environmentValue(command),
		args: Array.isArray(value.args)
			? value.args.map((argument) => environmentValue(argument as string))
			: [],
		cwd: configuredCwd
			? resolve(projectRoot, environmentValue(configuredCwd))
			: resolve(launchCwd),
		env: stringRecord(value.env),
	};
}

function ancestorDirectories(cwd: string): string[] {
	const directories: string[] = [];
	let current = resolve(cwd);
	while (true) {
		directories.push(current);
		if (current === parse(current).root) break;
		current = dirname(current);
	}
	return directories.reverse();
}

function candidateConfigPaths(
	cwd: string,
	options: DiscoveryOptions,
): string[] {
	const paths: string[] = [];
	if (options.includeGlobal !== false) {
		const home = options.homeDirectory ?? homedir();
		paths.push(join(home, ".pi", "mcp.json"));
		paths.push(join(home, ".pi", "agent", "mcp.json"));
	}
	for (const directory of ancestorDirectories(cwd)) {
		paths.push(join(directory, ".vscode", "mcp.json"));
		paths.push(join(directory, ".pi", "mcp.json"));
		paths.push(join(directory, ".mcp.json"));
	}
	return [...new Set(paths)];
}

/** Discover common project/user MCP manifests. Nearest project entries win by server id. */
export async function discoverMcpProviders(
	cwd: string,
	options: DiscoveryOptions = {},
): Promise<McpProviderDiscovery> {
	const diagnostics: string[] = [];
	const configFiles: string[] = [];
	const providers = new Map<string, McpProviderConfig>();

	for (const path of candidateConfigPaths(cwd, options)) {
		let document: unknown;
		try {
			document = JSON.parse(await readFile(path, "utf8"));
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === "ENOENT") continue;
			diagnostics.push(
				`${path}: ${error instanceof Error ? error.message : String(error)}`,
			);
			continue;
		}
		configFiles.push(path);
		if (!isRecord(document)) {
			diagnostics.push(`${path}: MCP config must be an object`);
			continue;
		}
		const rawServers = isRecord(document.mcpServers)
			? document.mcpServers
			: isRecord(document.servers)
				? document.servers
				: undefined;
		if (!rawServers) {
			diagnostics.push(`${path}: expected mcpServers or servers object`);
			continue;
		}
		for (const [id, value] of Object.entries(rawServers)) {
			const provider = parseProvider(id, value, path, cwd, diagnostics);
			if (provider) providers.set(id, provider);
		}
	}

	return {
		providers: [...providers.values()].sort((left, right) =>
			left.id.localeCompare(right.id),
		),
		configFiles,
		diagnostics,
	};
}
