/**
 * Decide whether the model should see the Meta-tool schemas for this turn.
 *
 * Journal collection and online discovery remain enabled independently. An
 * empty catalog has no executable value, so exposing search/run only adds
 * prompt and tool-selection overhead.
 */
export function shouldExposeMetaTools(options: {
	enabled: boolean;
	trusted: boolean;
	activeCatalogSize: number;
	minActiveCatalogSize: number;
}): boolean {
	return (
		options.enabled &&
		options.trusted &&
		Number.isFinite(options.activeCatalogSize) &&
		options.activeCatalogSize >= Math.max(1, options.minActiveCatalogSize)
	);
}
