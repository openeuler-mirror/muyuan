import { isDeepStrictEqual } from "node:util";
import type { MetaToolSpec } from "./types.ts";

export const READ_BATCH_KIND = "caller_bound_read_batch_v1";
export const MAX_READ_LINES = 300;

// Mirrors the portable bounded schema; cross-language fixtures test parity.
export function readBatchLayout(size: number) {
  if (![2, 3, 4].includes(size)) throw new Error("read batch size must be 2, 3 or 4");
  const properties: Record<string, unknown> = {}, steps: any[] = [], output: any[] = [];
  for (let i = 1; i <= size; i++) {
    properties[`path_${i}`] = { type: "string", minLength: 1, maxLength: 4096 };
    properties[`offset_${i}`] = { type: "integer", minimum: 1 };
    properties[`limit_${i}`] = { type: "integer", minimum: 1, maximum: MAX_READ_LINES };
    const args = Object.fromEntries(["path", "offset", "limit"].map(k => [k, { $input: `${k}_${i}` }]));
    steps.push({ id: `read_${i}`, tool: "read", arguments: args });
    output.push({ ...args, result: { $step: `read_${i}`, path: [] } });
  }
  return { input_schema: { type: "object", properties, required: Object.keys(properties), additionalProperties: false }, steps, output };
}

export function isCallerBoundReadBatch(spec: MetaToolSpec): boolean {
  if (spec.metadata.composition_kind !== READ_BATCH_KIND || ![2, 3, 4].includes(spec.steps.length)) return false;
  const layout = readBatchLayout(spec.steps.length);
  if (!isDeepStrictEqual(spec.input_schema, layout.input_schema)
      || !isDeepStrictEqual(spec.steps, layout.steps) || !isDeepStrictEqual(spec.output, layout.output)) return false;
  for (const [key, minimum] of [["support", 3], ["distinct_sessions", 2], ["llm_turn_coverage", 1],
    ["mean_llm_calls_saved_per_observed_occurrence", 1]] as const) {
    const v = spec.metadata[key];
    if (typeof v !== "number" || !Number.isFinite(v) || v < minimum) return false;
  }
  return spec.metadata.side_effect_free === true && spec.metadata.execution_guard === "none"
    && spec.metadata.requires_caller_plan === true;
}

export function flattenRanges(ranges: Array<{path: string; offset: number; limit: number}>) {
  if (![2, 3, 4].includes(ranges.length)) throw new Error("Supply 2–4 already planned ranges");
  const inputs: Record<string, unknown> = {};
  for (const [i, r] of ranges.entries()) {
    if (typeof r.path !== "string" || !r.path.length || r.path.length > 4096
        || !Number.isInteger(r.offset) || r.offset < 1
        || !Number.isInteger(r.limit) || r.limit < 1 || r.limit > MAX_READ_LINES)
      throw new Error(`Invalid read range ${i + 1}`);
    for (const k of ["path", "offset", "limit"] as const) inputs[`${k}_${i + 1}`] = r[k];
  }
  return inputs;
}

export function renderReadBatchResult(result: any): string {
  const output = Array.isArray(result) ? result : result?.output;
  if (!Array.isArray(output) || ![2, 3, 4].includes(output.length))
    throw new Error("Read batch did not return every primitive result");
  return output.map((r: any, i: number) => {
    if (!r || !Object.hasOwn(r, "result")) throw new Error("Missing read result");
    return `[${i + 1}] ${r.path}:${r.offset} (requested ${r.limit} lines)\n`
      + (typeof r.result === "string" ? r.result : JSON.stringify(r.result));
  }).join("\n\n");
}
