import { realpathSync } from "node:fs";

interface RegisteredToolSource {
	name: string;
	sourceInfo: { source: string; path: string };
}

export type ToolOwner = "goal-plus" | "native" | "external";

export function toolOwner(
	tools: readonly RegisteredToolSource[], name: string, extensionPath: string,
): ToolOwner {
	// Pi supplies the effective registration's sourceInfo, including overrides.
	// Names, descriptions, UI labels and model-supplied input are not ownership.
	const source = tools.find((tool) => tool.name === name)?.sourceInfo;
	if (!source) return "external";
	if (source.source === "builtin") return "native";
	try {
		if (realpathSync(source.path) === realpathSync(extensionPath)) return "goal-plus";
	} catch {
		// Missing/unknown registration metadata does not claim an external tool.
	}
	return "external";
}
