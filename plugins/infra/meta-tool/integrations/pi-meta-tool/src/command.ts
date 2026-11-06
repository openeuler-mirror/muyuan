export interface ExtractCommandArguments {
	tracePath: string | undefined;
	deprecatedGeneralizeAlias: boolean;
}

/**
 * Parse the arguments following `/meta-tool extract`.
 *
 * Keep the path as a single string so existing paths containing spaces remain
 * supported. The one-shot flag may appear before or after that path.
 */
export function parseExtractCommandArguments(
	text: string,
): ExtractCommandArguments {
	const trimmed = text.trim();
	if (!trimmed)
		return { tracePath: undefined, deprecatedGeneralizeAlias: false };
	if (trimmed === "--generalize")
		return { tracePath: undefined, deprecatedGeneralizeAlias: true };

	const prefix = "--generalize ";
	if (trimmed.startsWith(prefix)) {
		const tracePath = trimmed.slice(prefix.length).trim();
		if (!tracePath)
			return { tracePath: undefined, deprecatedGeneralizeAlias: true };
		if (tracePath.includes("--generalize"))
			throw new Error("--generalize may only be supplied once");
		return { tracePath, deprecatedGeneralizeAlias: true };
	}

	const suffix = " --generalize";
	if (trimmed.endsWith(suffix)) {
		const tracePath = trimmed.slice(0, -suffix.length).trim();
		if (tracePath.includes("--generalize"))
			throw new Error("--generalize may only be supplied once");
		return {
			tracePath: tracePath || undefined,
			deprecatedGeneralizeAlias: true,
		};
	}

	if (trimmed.includes("--generalize"))
		throw new Error(
			"--generalize must appear before or after the optional trace path",
		);
	if (trimmed.startsWith("--"))
		throw new Error(`Unknown extract option: ${trimmed.split(/\s+/, 1)[0]}`);
	return { tracePath: trimmed, deprecatedGeneralizeAlias: false };
}

/** Enforce the online edition's single public extraction mode. */
export function withGeneralizedExtraction(
	normalization: Record<string, unknown>,
): Record<string, unknown> {
	return {
		...normalization,
		argument_matching: "structural",
		structural_fallback: false,
	};
}
