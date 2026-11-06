import type { MetaToolSpec } from "./types.ts";

function previewValue(
	value: unknown,
	inputs: Record<string, unknown> | undefined,
): unknown {
	if (Array.isArray(value))
		return value.map((item) => previewValue(item, inputs));
	if (typeof value !== "object" || value === null) return value;
	const record = value as Record<string, unknown>;
	if (typeof record.$input === "string" && inputs) {
		return Object.prototype.hasOwnProperty.call(inputs, record.$input)
			? inputs[record.$input]
			: `[missing input: ${record.$input}]`;
	}
	return Object.fromEntries(
		Object.entries(record).map(([key, item]) => [
			key,
			previewValue(item, inputs),
		]),
	);
}

export function planPreview(
	spec: MetaToolSpec,
	inputs?: Record<string, unknown>,
): string {
	const lines = [
		`${spec.name}: ${spec.description}`,
		`inputs: ${JSON.stringify(spec.input_schema)}`,
		...spec.steps.map(
			(step, index) =>
				`${index + 1}. ${step.tool} ${JSON.stringify(previewValue(step.arguments, inputs))}`,
		),
	];
	const text = lines.join("\n");
	return text.length > 6_000 ? `${text.slice(0, 6_000)}\n...` : text;
}
