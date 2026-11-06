import assert from "node:assert/strict";
import test from "node:test";
import { flattenRanges, renderReadBatchResult, readBatchLayout, isCallerBoundReadBatch } from "../src/read-batches.ts";

test("all read outputs survive rendering, including truncation notices", () => {
  const values = [{path:"a.c",offset:1,limit:2,result:"one\ntwo\n"},
    {path:"b.c",offset:3,limit:2,result:"THREE\n[output truncated; next offset 4]"}];
  for (const payload of [values, {output:values}]) {
    const text = renderReadBatchResult(payload);
    assert(text.includes("one\ntwo\n"));
    assert(text.includes("[output truncated; next offset 4]"));
    assert(text.includes("b.c:3"));
  }
  assert.throws(() => renderReadBatchResult([values[0]]));
});

test("caller ranges are bounded and the full executable spec is validated", () => {
  const ranges = [{path:"a.c",offset:1,limit:20},{path:"b.c",offset:30,limit:40}];
  assert.deepEqual(flattenRanges(ranges), {path_1:"a.c",offset_1:1,limit_1:20,path_2:"b.c",offset_2:30,limit_2:40});
  assert.throws(() => flattenRanges([{...ranges[0],limit:301},ranges[1]]));
  const spec:any = {name:"batch", description:"", ...readBatchLayout(2), metadata:{
    composition_kind:"caller_bound_read_batch_v1",support:3,distinct_sessions:3,
    llm_turn_coverage:1,mean_llm_calls_saved_per_observed_occurrence:1,
    side_effect_free:true,execution_guard:"none",requires_caller_plan:true}};
  assert(isCallerBoundReadBatch(spec));
  spec.steps[0].tool="bash";
  assert(!isCallerBoundReadBatch(spec));
});
