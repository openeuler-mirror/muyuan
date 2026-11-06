import { createHash } from "node:crypto";
import type {
	MetaToolCatalog,
	MetaToolSearchMatch,
	MetaToolSpec,
	ProjectProfile,
} from "./types.ts";

const ROUTING_STOP_WORDS = new Set([
	"a",
	"an",
	"and",
	"for",
	"from",
	"in",
	"into",
	"of",
	"on",
	"or",
	"the",
	"then",
	"this",
	"to",
	"use",
	"using",
	"with",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textValues(value: unknown): string[] {
	if (typeof value === "string") return [value];
	if (Array.isArray(value)) return value.flatMap(textValues);
	if (isRecord(value)) return Object.values(value).flatMap(textValues);
	return [];
}

/** Tokenize English identifiers and Chinese text without an external model. */
export function routingTokens(text: string): Set<string> {
	const normalized = text
		.toLowerCase()
		.replace(/[_./:-]+/g, " ")
		.replace(/[^\p{L}\p{N}\s]+/gu, " ");
	const tokens = new Set(
		normalized
			.split(/\s+/)
			.map((token) => token.trim())
			.filter((token) => token.length >= 2 && !ROUTING_STOP_WORDS.has(token)),
	);
	const chineseRuns = normalized.match(/[\p{Script=Han}]+/gu) ?? [];
	for (const run of chineseRuns)
		for (let index = 0; index < run.length - 1; index++)
			tokens.add(run.slice(index, index + 2));
	return tokens;
}

function overlap(
	left: ReadonlySet<string>,
	right: ReadonlySet<string>,
): number {
	if (left.size === 0 || right.size === 0) return 0;
	let shared = 0;
	for (const item of left) if (right.has(item)) shared += 1;
	return shared / Math.sqrt(left.size * right.size);
}

function finite(value: unknown, fallback: number): number {
	return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function canonicalize(value: unknown): unknown {
	if (Array.isArray(value)) return value.map(canonicalize);
	if (!isRecord(value)) return value;
	return Object.fromEntries(
		Object.keys(value)
			.sort()
			.map((key) => [key, canonicalize(value[key])]),
	);
}

/** Stable JSON used by catalog pins regardless of object insertion order. */
export function canonicalJson(value: unknown): string {
	const encoded = JSON.stringify(canonicalize(value));
	if (encoded === undefined)
		throw new TypeError("Meta-tool fingerprint input is not JSON-serializable");
	return encoded;
}

export function metaToolFingerprint(spec: MetaToolSpec): string {
	return createHash("sha256")
		.update(
			canonicalJson({
				name: spec.name,
				input_schema: spec.input_schema,
				steps: spec.steps,
				output: spec.output,
			}),
		)
		.digest("hex");
}

function candidateText(spec: MetaToolSpec): string {
	const metadata = spec.metadata;
	return [
		spec.name,
		spec.description,
		...spec.steps.map((step) => step.tool),
		...textValues(metadata.task_goals),
		...textValues(metadata.intent),
		...textValues(metadata.domains),
		...textValues(metadata.project_tags),
		...textValues(metadata.providers),
	].join(" ");
}

function projectTags(spec: MetaToolSpec): string[] {
	const explicit = Array.isArray(spec.metadata.project_tags)
		? spec.metadata.project_tags.filter(
				(value): value is string => typeof value === "string",
			)
		: [];
	const inferred = new Set(explicit.map((item) => item.toLowerCase()));
	const text = candidateText(spec).toLowerCase();
	for (const tag of [
		"goal-plus",
		"pytorch",
		"torchbench",
		"dvm",
		"inductor",
		"browser-automation",
		"webarena",
		"spreadsheet",
	])
		if (text.includes(tag.replace("-", "_")) || text.includes(tag))
			inferred.add(tag);
	return [...inferred].sort();
}

export function searchMetaTools(options: {
	catalog: MetaToolCatalog;
	catalogVersion: number;
	query: string;
	profile: ProjectProfile;
	supportedTools: ReadonlySet<string>;
	topK: number;
	minScore: number;
	requiredTools?: ReadonlySet<string>;
	blockedFingerprints?: ReadonlySet<string>;
	toolSchemaFingerprints?: ReadonlyMap<string, string>;
}): MetaToolSearchMatch[] {
	const queryTokens = routingTokens(options.query);
	const profileTokens = routingTokens(options.profile.tags.join(" "));
	const matches: MetaToolSearchMatch[] = [];

	for (const spec of options.catalog.meta_tools) {
		if (options.blockedFingerprints?.has(metaToolFingerprint(spec))) continue;
		const requiredTools = [...new Set(spec.steps.map((step) => step.tool))];
		if (requiredTools.some((name) => !options.supportedTools.has(name)))
			continue;
		const expectedSchemas = isRecord(
			spec.metadata.required_tool_schema_fingerprints,
		)
			? spec.metadata.required_tool_schema_fingerprints
			: {};
		if (
			isRecord(spec.metadata.tool_schema_conflicts) &&
			Object.keys(spec.metadata.tool_schema_conflicts).length > 0
		)
			continue;
		if (
			Object.entries(expectedSchemas).some(
				([name, expected]) =>
					typeof expected === "string" &&
					options.toolSchemaFingerprints?.get(name) !== expected,
			)
		)
			continue;
		if (
			options.requiredTools &&
			[...options.requiredTools].some((name) => !requiredTools.includes(name))
		)
			continue;
		const candidateTokens = routingTokens(candidateText(spec));
		const semantic = overlap(queryTokens, candidateTokens);
		const tags = projectTags(spec);
		const project = overlap(profileTokens, routingTokens(tags.join(" ")));
		// Support and purity rank semantically eligible candidates; they must
		// never make an unrelated workflow visible by themselves. An explicit
		// requiredTools query is the only non-semantic retrieval mode.
		if (semantic === 0 && !options.requiredTools) continue;
		const support = Math.min(
			1,
			Math.log2(1 + finite(spec.metadata.support, 0)) / 4,
		);
		const purity = finite(spec.metadata.sequence_purity, 0);
		const historicalSuccess = finite(spec.metadata.execution_success_rate, 1);
		const score = Math.min(
			1,
			0.68 * semantic +
				0.12 * project +
				0.1 * support +
				0.05 * purity +
				0.05 * historicalSuccess,
		);
		if (score < options.minScore) continue;
		const reasons: string[] = [];
		if (semantic > 0.3) reasons.push("task semantics match");
		if (project > 0) reasons.push("project/profile tags match");
		if (support >= 0.5) reasons.push("repeated workflow support");
		if (purity >= 1) reasons.push("pure observed sequence");
		matches.push({
			name: spec.name,
			catalogVersion:
				typeof spec.metadata.catalog_version === "number"
					? spec.metadata.catalog_version
					: options.catalogVersion,
			catalogScope: spec.metadata.catalog_scope === "user" ? "user" : "project",
			fingerprint: metaToolFingerprint(spec),
			score: Number(score.toFixed(4)),
			matchReason: reasons.length ? reasons : ["catalog fallback match"],
			description: spec.description,
			requiredTools,
			inputSchema: spec.input_schema,
			executionGuard:
				typeof spec.metadata.execution_guard === "string"
					? spec.metadata.execution_guard
					: "confirm_each_run",
			projectTags: tags,
			providers: Array.isArray(spec.metadata.providers)
				? spec.metadata.providers.filter(
						(value): value is string => typeof value === "string",
					)
				: [],
		});
	}

	return matches
		.sort(
			(left, right) =>
				right.score - left.score || left.name.localeCompare(right.name),
		)
		.slice(0, options.topK);
}

export function routedCatalogPrompt(
	matches: readonly MetaToolSearchMatch[],
	maxCharacters: number,
): string {
	if (matches.length === 0) return "";
	const lines = [
		"<relevant-meta-tools>",
		"Only the task-relevant admitted catalog subset is shown. Call meta_tool_run only for an exact workflow match; use meta_tool_search when the task changes or more candidates are needed.",
	];
	for (const match of matches) {
		const entry = [
			`- ${match.name}@${match.catalogScope}:v${match.catalogVersion} [${match.executionGuard}, score=${match.score}]: ${match.description}`,
			`  required-tools=${match.requiredTools.join(",")}`,
			...(match.providers.length
				? [`  observed-providers=${match.providers.join(",")}`]
				: []),
			`  inputs=${JSON.stringify(match.inputSchema)}`,
			`  run-pin={"catalogScope":"${match.catalogScope}","catalogVersion":${match.catalogVersion},"fingerprint":"${match.fingerprint}"}`,
		].join("\n");
		if (
			`${lines.join("\n")}\n${entry}\n</relevant-meta-tools>`.length >
			maxCharacters
		)
			break;
		lines.push(entry);
	}
	lines.push("</relevant-meta-tools>");
	return lines.join("\n");
}
