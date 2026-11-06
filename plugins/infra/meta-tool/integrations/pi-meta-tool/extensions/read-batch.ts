/** Default low-overhead entry point for bounded caller-planned read batches. */
import { Type } from "typebox";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig, loadState } from "../src/config.ts";
import { loadExecutionUserCatalog } from "../src/read-batch-catalog.ts";
import { metaToolFingerprint } from "../src/router.ts";
import { isCallerBoundReadBatch, flattenRanges, renderReadBatchResult, MAX_READ_LINES } from "../src/read-batches.ts";
import type { MetaToolSpec } from "../src/types.ts";
const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export function registerReadBatch(pi: any) {
  let specs = new Map<number, MetaToolSpec>();
  pi.registerTool({
    name: "meta_tool_read_batch",
    label: "Read planned code ranges",
    description: "Read 2–4 code ranges in one call using admitted BATE compositions. Supply all paths and ranges up front, only when already known. Returns every read unchanged. Do not predict a later range that requires interpreting an earlier result; use ordinary read then. No catalog search is needed.",
    promptSnippet: "Read several already planned code ranges together",
    parameters: Type.Object({ ranges: Type.Array(Type.Object({
      path: Type.String({ minLength: 1, maxLength: 4096 }),
      offset: Type.Integer({ minimum: 1, description: "1-based starting line" }),
      limit: Type.Integer({ minimum: 1, maximum: MAX_READ_LINES }),
    }), { minItems: 2, maxItems: 4 }) }),
    executionMode: "sequential",
    async execute(_id: string, params: any, signal: AbortSignal, _update: any, ctx: any) {
      const config = await loadConfig(ctx.cwd, PACKAGE_ROOT), state = await loadState(ctx.cwd);
      if (!ctx.isProjectTrusted() || !state.enabled || config.admission.policy?.allow_caller_bound_read_batches !== true)
        throw new Error("Caller-planned read composition is not enabled");
      const inputs = flattenRanges(params.ranges);
      const catalog = await loadExecutionUserCatalog(config);
      const spec = catalog.meta_tools.find((item) =>
        isCallerBoundReadBatch(item) && item.steps.length === params.ranges.length);
      if (!spec || !isCallerBoundReadBatch(spec)) throw new Error("No admitted read batch of this size");
      const response = await pi.callTool("meta_tool_run", {
        metaTool: spec.name, catalogScope: "user", fingerprint: metaToolFingerprint(spec), inputs,
      }, signal);
      if (response.isError) return response;
      const text = response.content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
      const result = JSON.parse(text);
      return { content: [{ type: "text", text: renderReadBatchResult(result) }],
        details: { composition: spec.name, primitiveReads: spec.steps.length, requiresCallerPlan: true } };
    },
  });
  const refresh = async (_event: any, ctx: any) => {
    const config = await loadConfig(ctx.cwd, PACKAGE_ROOT), state = await loadState(ctx.cwd);
    const enabled = state.enabled && ctx.isProjectTrusted() && config.admission.policy?.allow_caller_bound_read_batches === true;
    specs = new Map();
    if (enabled) for (const spec of (await loadExecutionUserCatalog(config)).meta_tools)
      if (isCallerBoundReadBatch(spec)) specs.set(spec.steps.length, spec);
    const active = pi.getActiveTools().filter((n: string) => n !== "meta_tool_read_batch");
    pi.setActiveTools(specs.size ? [...active, "meta_tool_read_batch"] : active);
  };
  pi.on("session_start", refresh);
  pi.on("before_agent_start", refresh);
}

// Kept for old launchers that explicitly load both extensions. Registration is
// owned by meta-tool.ts, so loading this compatibility entry cannot duplicate it.
export default function readBatchCompatibility() {}
