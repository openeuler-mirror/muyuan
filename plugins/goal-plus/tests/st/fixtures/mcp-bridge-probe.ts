import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default async function (pi: ExtensionAPI) {
    async function bridge(args: string[]) {
        const result = await pi.exec(process.env.GOAL_PLUS_PROBE_PYTHON!, [
            "-m", "tests.st.helpers.mcp_bridge_probe", ...args,
        ], { timeout: 30_000 });
        if (result.code !== 0) throw new Error(result.stderr);
        return JSON.parse(result.stdout);
    }
    const listed = await bridge(["list"]);
    const tool = listed.tools.find((item: any) => item.name === "goal_plus_monitor_snapshot");
    if (!tool) throw new Error("MCP did not advertise goal_plus_monitor_snapshot");
    const definition = {
        name: tool.name,
        label: tool.name,
        description: tool.description,
        parameters: tool.inputSchema,
        async execute(_id: string, params: Record<string, unknown>) {
            const result = await bridge(["call", tool.name, JSON.stringify(params)]);
            return { content: result.content, details: result };
        },
    };
    pi.registerTool(definition);
    pi.on("session_start", async () => {
        const result = await definition.execute("probe", {});
        pi.appendEntry("goal-plus-mcp-probe", {
            server: listed.server, tool, result: result.details,
        });
    });
}
