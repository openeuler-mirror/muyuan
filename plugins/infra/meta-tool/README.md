# Meta-tool / BATE Core

This is the independently usable foundation of the branch-aware template
extraction (BATE) plugin. It provides:

- JSON-compatible trace and tool-call data contracts;
- configurable call normalization and deterministic state-graph construction;
- portable workflow specifications with `$input` and `$step` bindings;
- deterministic synchronous and asynchronous execution of primitive tools;
- full JSON Schema validation before the first primitive is called.

This change does **not** implement automatic extraction, Static/Online learning,
Pi/MCP integration, worker propagation, or read batching. Those features belong
to separate changes. BATE and its older optimized predecessor share these
contracts, not their candidate-selection algorithm. A normalization-rule
optimization loop is not part of this package.

The distribution name `meta-tool-online` is retained. The Python package has
moved from `awo` to `bate`: update imports and `python -m awo` invocations to
`bate`, then reinstall this distribution. There is no `awo` compatibility shim
or second implementation. Existing JSON trace and workflow formats are unchanged.
No sibling algorithm repository or absolute filesystem path is required.

## Install and verify

Python 3.11+ is required. Install the package and its test dependencies:

```bash
python3 -m pip install -e '.[dev]'
python3 -m bate
python3 -m pytest
```

`python3 -m bate` prints package identity/version only; it is not an extraction
CLI and does not launch an agent.

## Normalize traces and build a state graph

The conservative default matches exact argument values and treats unknown tools
as ordered mutators. Classify known read-only tools explicitly:

```python
from bate import (
    NormalizationConfig, SemanticType, StateGraphBuilder,
    ToolCall, ToolRule, Trace, TraceNormalizer,
)

config = NormalizationConfig(tool_rules=(
    ToolRule(pattern=r"^get[.]", semantic_type=SemanticType.ACCESSOR),
))
traces = [
    Trace("one", (ToolCall("get.a", {"id": 1}), ToolCall("get.b", {"id": 2}))),
    Trace("two", (ToolCall("get.b", {"id": 2}), ToolCall("get.a", {"id": 1}))),
]
result = StateGraphBuilder(TraceNormalizer(config)).build(traces)
assert result.paths[0].nodes[-1] == result.paths[1].nodes[-1]
assert result.graph.nodes[result.paths[0].nodes[-1]].weight == 2
graph_json = result.graph.to_dict()   # nodes, edges, weights and statistics
graph_dot = result.graph.to_dot()    # Graphviz source; Graphviz is not required
assert graph_json["stats"]["nodes"] == 4
assert graph_dot.startswith("digraph meta_tool")
```

`bate.io.load_config(path)` loads a JSON configuration;
`load_config(None)` returns the conservative defaults. Rules support conditional
tool renaming, ignored/placeholder argument paths and regex substitutions.
`NormalizationConfig.to_dict()` produces a JSON-compatible configuration.

Matching and state semantics:

- Exact matching is the default. Opt-in `argument_matching="structural"`
  abstracts ordinary leaf values by JSON type and arrays by distinct item shapes
  (not length or order). It preserves boolean flags and operation selectors such
  as `operation`, `mode` and `command`. Shell/edit/write/patch tools remain exact
  under the default exact-tool patterns. Original calls/arguments remain in each
  path's occurrences; structural similarity alone does not make calls safe to
  substitute or execute.
- Accessors accumulate as a set; ordering and repeated identical reads do not
  change the final state. Mutators retain ordered history. Cross-domain
  commutativity is disabled unless explicitly enabled; unknown domains remain
  ordering barriers. Annotators are skipped by default. Repeated accessor or
  explicitly recurrent calls can create self-loops, so this is not always a DAG.
- A node identifies normalized call history, **not** a live environment snapshot.
  `state_context_fields` can retain selected metadata (for example a browser URL)
  in call identity, including in structural mode. The builder does not infer
  environmental equivalence or verify that accessor/domain annotations are true.
- Node/edge weights count distinct trace IDs; visits/traversals count all
  occurrences. Use unique trace IDs (checked by `load_traces`) when counting
  independent executions. `result.paths` preserves per-trace nodes, action
  occurrences and their original call indices. The graph export is a summary,
  not a serialized replay catalog.

The builder neither compresses paths nor extracts or admits workflows. Candidate
selection, data-flow checks, compilation and host execution policies are not
implemented by graph construction.

## Execute a reviewed specification

```python
from bate import MetaToolExecutor, MetaToolSpec, StepSpec, input_binding, step_binding

spec = MetaToolSpec(
    name="lookup_then_label",
    description="Look up an identifier and label it.",
    input_schema={
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
    steps=(
        StepSpec("lookup", "lookup", {"key": input_binding("key")}),
        StepSpec("label", "label", {"identifier": step_binding("lookup", ["id"])}),
    ),
    output=step_binding("label"),
)
executor = MetaToolExecutor({
    "lookup": lambda key: {"id": "id:" + key},
    "label": lambda identifier: {"label": identifier.upper()},
})
assert executor.execute(spec, {"key": "example"}).output == {"label": "ID:EXAMPLE"}
```

The caller supplies and reviews the specification and registers its primitive
functions. This low-level executor does not infer tools, grant filesystem or
network access, certify read-only behavior, or implement mutation confirmation.
It must not be used to execute untrusted specifications without a host policy.

Input validation covers constraints declared by the specification, including
nested objects/arrays, types, required/extra fields, bounds, enum/const, schema
combinations, and local references. It does not coerce values or insert defaults.
Unspecified dialects use Draft 2020-12; `format` is an annotation. References do
not trigger network downloads. Invalid inputs/schemas fail before any primitive
runs, and validation errors identify the field/constraint without echoing values.
Undeclared business constraints and primitive argument schemas are not inferred.

## Tests

Run all tests in this foundation (no model/API credentials are required):

```bash
python3 -m pytest -q tests/test_models_io.py tests/test_compiler_runtime.py \
    tests/test_input_validation.py tests/test_core_boundary.py \
    tests/test_normalization_graph.py
```
