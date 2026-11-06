import { readFileSync } from "node:fs";
import { loadUserCatalog, mergeCatalogs } from "./catalog-store.ts";
import { isCallerBoundReadBatch } from "./read-batches.ts";
import type { MetaToolCatalog, PluginConfig } from "./types.ts";

// The same portable 2/3/4-read templates used in the worker read-batch runs.
// Keep their historical evidence; never count these as newly learned tools.
const bundled: MetaToolCatalog = JSON.parse(
	readFileSync(new URL("./bundled-read-batches.json", import.meta.url), "utf8"),
);
if (bundled.meta_tools.length !== 3 || bundled.meta_tools.some((spec, i) =>
	!isCallerBoundReadBatch(spec) || spec.steps.length !== i + 2 ||
	spec.name !== `meta_tool_read_batch_${i + 2}`)) {
	throw new Error("Invalid bundled read-batch catalog");
}

/** Include the built-in read capabilities without importing unrelated history. */
export async function loadExecutionUserCatalog(
	config: PluginConfig,
	includeUserCatalog = true,
): Promise<MetaToolCatalog> {
	const user = includeUserCatalog
		? await loadUserCatalog(config.catalog.userCatalogDir)
		: { meta_tools: [] };
	if (config.admission.policy?.allow_caller_bound_read_batches !== true) {
		return { meta_tools: user.meta_tools.filter((spec) => !isCallerBoundReadBatch(spec)) };
	}
	const defaults: MetaToolCatalog = structuredClone(bundled);
	for (const spec of defaults.meta_tools) {
		spec.metadata = { ...spec.metadata, catalog_scope: "user", catalog_version: 0,
			catalog_origin: "bundled_read_batch", preinstalled: true };
	}
	// Explicit user definitions retain precedence over bundled defaults.
	return mergeCatalogs(user, defaults);
}
