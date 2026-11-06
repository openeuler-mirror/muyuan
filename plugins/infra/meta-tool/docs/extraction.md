# BATE extraction

Branch-Aware Template Extraction (BATE) learns a fixed primitive sequence with
explicit caller inputs and cross-step result bindings. Different complete
sequences remain separate candidates even when they share a prefix.

```bash
bate extract --traces tests/fixtures/branched_traces.json \
  --config tests/fixtures/branched_config.json --out /tmp/bate-example \
  --also-raw --max-len 4 --naming stable-short
```

This synthetic fixture produces two templates: customer lookup → order lookup
→ record read, and customer lookup → invoice lookup → record read. Both keep
their full sequence. Runtime IDs are obtained from fresh primitive results.

Stages: normalize calls and build execution paths; enumerate observed sequences;
refine graph candidates to connected data-flow spans; assess each complete
sequence; compile `$input`/`$step` bindings; reject compiler warnings by default;
assign optional `mt_<12 hex>` names; write the catalog and diagnostics.

The current builder uses primitive execution paths. It does not iteratively
compress already-selected workflows. The optional raw enumerator supplements
graph candidates; neither path runs the older greedy baseline. Same-name
sequence grouping does not itself prove identical argument shapes: the
data-flow and compiler checks still apply.

Defaults require three distinct supporting trace IDs, at least two steps,
internal data flow and at least one varying dependency. These are extraction
thresholds; host admission adds supported primitives, session evidence,
observed model-turn savings and execution policy. Prefix fan-out is diagnostic,
not a rejection condition. Fixed control flow does not imply task success.

`active_meta_tools.json`, `meta_tools.json` and `catalog.json` contain the
compiled specs. `extraction_summary.json` records assessments and sibling
branches; `rejected_meta_tools.json` gives rejection reasons. Static hosts freeze
this output; Online hosts trigger this same extractor as evidence accumulates.
Built-in read batches are separate from learned workflows.

The legacy `--baseline-config` spelling remains an alias for `--config`, and
`--no-baseline` is a no-op for existing host callers. No comparison baseline or
old selector is packaged. JSON workflow and trace formats remain compatible.
