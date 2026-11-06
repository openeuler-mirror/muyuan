import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { McpProviderManager } from "../src/mcp-provider.ts";
import { discoverMcpProviders } from "../src/provider-config.ts";
import type { PluginConfig } from "../src/types.ts";

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SERVER_PATH = resolve(TEST_DIRECTORY, "fixtures", "fake_mcp_server.py");

interface RegisteredTool {
	name: string;
	description: string;
	execute: (
		id: string,
		params: Record<string, unknown>,
		signal: AbortSignal | undefined,
		onUpdate: undefined,
		ctx: { isProjectTrusted: () => boolean },
	) => Promise<{
		content: Array<{ type: string; text?: string }>;
		details: unknown;
	}>;
}

function fakePi() {
	const tools = new Map<string, RegisteredTool>();
	const active = new Set<string>(["read"]);
	const api = {
		registerTool(tool: RegisteredTool) {
			tools.set(tool.name, tool);
			active.add(tool.name);
		},
		getAllTools() {
			return [
				{ name: "read", description: "Read a file" },
				...[...tools.values()].map((tool) => ({
					name: tool.name,
					description: tool.description,
				})),
			];
		},
		getActiveTools() {
			return [...active];
		},
		setActiveTools(names: string[]) {
			active.clear();
			for (const name of names) active.add(name);
		},
	};
	return { api: api as unknown as ExtensionAPI, tools, active };
}

function baseConfig(): PluginConfig {
	return {
		metaToolRoot: "/tmp/meta-tool",
		bridgePath: "/tmp/bridge.py",
		pythonCommand: "python3",
		timeoutMs: 30_000,
		normalization: { tool_rules: [] },
		extraction: {},
		admission: { read_only_tools: ["read"] },
	};
}

test("discovers MCP config, registers tools and aliases, and delegates calls", async () => {
	const temporary = await mkdtemp(join(tmpdir(), "pi-meta-tool-mcp-"));
	const project = join(temporary, "project");
	await mkdir(join(project, ".pi"), { recursive: true });
	await writeFile(
		join(project, ".pi", "mcp.json"),
		`${JSON.stringify({
			mcpServers: {
				demo: {
					command: process.env.META_TOOL_TEST_PYTHON ?? "python3",
					args: [SERVER_PATH],
					metaTool: { autoActivate: true, registerAliases: true },
				},
			},
		})}\n`,
		"utf8",
	);

	const manager = new McpProviderManager((cwd) =>
		discoverMcpProviders(cwd, { includeGlobal: false }),
	);
	try {
		const pi = fakePi();
		const status = await manager.refresh(pi.api, project, true);

		assert.equal(status.connectedProviders, 1, JSON.stringify(status));
		assert.equal(status.remoteTools, 2);
		assert.equal(status.canonicalTools, 2);
		assert.ok(pi.tools.has("lookup"));
		assert.ok(pi.tools.has("demo.lookup"));
		assert.ok(pi.tools.has("mcp__demo__lookup"));
		assert.ok(pi.active.has("read"));
		assert.ok(pi.active.has("lookup"));
		assert.ok(!pi.active.has("demo.lookup"));

		const lookup = pi.tools.get("demo.lookup");
		assert.ok(lookup);
		const result = await lookup.execute(
			"call-1",
			{ query: "needle" },
			undefined,
			undefined,
			{ isProjectTrusted: () => true },
		);
		assert.match(result.content[0]?.text ?? "", /needle/);

		assert.ok(manager.hasTool("demo.lookup"));
		assert.ok(manager.executableToolNames().has("lookup"));
		const directResult = await manager.callTool(
			"demo.lookup",
			{ query: "direct-without-pi-callTool" },
			undefined,
			true,
		);
		assert.match(
			directResult.content[0]?.type === "text"
				? directResult.content[0].text
				: "",
			/direct-without-pi-callTool/,
		);
		await assert.rejects(
			manager.callTool("demo.lookup", {}, undefined, false),
			/trusted project/,
		);

		const augmented = manager.augmentConfig(baseConfig());
		assert.ok(
			(augmented.admission.read_only_tools as string[]).includes("demo.lookup"),
		);
		assert.ok(
			!(augmented.admission.read_only_tools as string[]).includes(
				"demo.update",
			),
		);
		assert.ok(
			(
				augmented.normalization.tool_rules as Array<Record<string, unknown>>
			).some(
				(rule) =>
					rule.pattern === "^demo\\.lookup$" &&
					rule.semantic_type === "accessor",
			),
		);
	} finally {
		await manager.close();
		await rm(temporary, { recursive: true, force: true });
	}
});

test("nearest MCP config wins and untrusted projects do not start providers", async () => {
	const temporary = await mkdtemp(join(tmpdir(), "pi-meta-tool-config-"));
	const project = join(temporary, "project");
	await mkdir(project, { recursive: true });
	await writeFile(
		join(temporary, ".mcp.json"),
		JSON.stringify({
			mcpServers: { demo: { command: "parent-server" } },
		}),
		"utf8",
	);
	await writeFile(
		join(project, ".mcp.json"),
		JSON.stringify({
			mcpServers: { demo: { command: "project-server" } },
		}),
		"utf8",
	);

	try {
		const discovery = await discoverMcpProviders(project, {
			includeGlobal: false,
		});
		assert.equal(discovery.providers.length, 1);
		assert.equal(discovery.providers[0]?.transport, "stdio");
		if (discovery.providers[0]?.transport === "stdio")
			assert.equal(discovery.providers[0].command, "project-server");

		const manager = new McpProviderManager((cwd) =>
			discoverMcpProviders(cwd, { includeGlobal: false }),
		);
		const status = await manager.refresh(fakePi().api, project, false);
		assert.equal(status.connectedProviders, 0);
		assert.match(status.diagnostics.join("\n"), /not trusted/);
	} finally {
		await rm(temporary, { recursive: true, force: true });
	}
});

test("inherited MCP configs launch in the active project unless cwd is explicit", async () => {
	const temporary = await mkdtemp(join(tmpdir(), "pi-meta-tool-inherited-cwd-"));
	const project = join(temporary, "examples", "nested-project");
	await mkdir(project, { recursive: true });
	await writeFile(
		join(temporary, ".mcp.json"),
		JSON.stringify({
			mcpServers: {
				inherited: { command: "inherited-server" },
				explicit: { command: "explicit-server", cwd: "runtime" },
			},
		}),
		"utf8",
	);

	try {
		const discovery = await discoverMcpProviders(project, {
			includeGlobal: false,
		});
		const inherited = discovery.providers.find(
			(provider) => provider.id === "inherited",
		);
		const explicit = discovery.providers.find(
			(provider) => provider.id === "explicit",
		);
		assert.equal(inherited?.transport, "stdio");
		assert.equal(explicit?.transport, "stdio");
		if (inherited?.transport === "stdio") assert.equal(inherited.cwd, project);
		if (explicit?.transport === "stdio")
			assert.equal(explicit.cwd, join(temporary, "runtime"));
	} finally {
		await rm(temporary, { recursive: true, force: true });
	}
});
