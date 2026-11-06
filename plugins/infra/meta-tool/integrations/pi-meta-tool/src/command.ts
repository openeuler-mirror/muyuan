export interface ExtractCommandArguments {
	tracePath: string | undefined;
}

/**
 * Parse the arguments following `/meta-tool extract`.
 *
 * Keep the path as a single string so existing paths containing spaces remain
 * supported. Extraction always uses structural generalization.
 */
export function parseExtractCommandArguments(
	text: string,
): ExtractCommandArguments {
	const trimmed = text.trim();
	const option = trimmed.match(/(?:^|\s)(--\S+)/);
	if (option) throw new Error(`Unknown extract option: ${option[1]}`);
	return { tracePath: trimmed || undefined };
}

/** Enforce the online edition's single public extraction mode. */
export function withGeneralizedExtraction(
	normalization: Record<string, unknown>,
): Record<string, unknown> {
	return {
		...normalization,
		argument_matching: "structural",
	};
}
