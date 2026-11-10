# Policy dispatch and bounded read compositions

This layer extends the reviewed-spec executor with a registry, evidence-based
admission, sync/async dispatch and opt-in caller-planned read batches. It depends
only on the previously published contracts/runtime; normalization, state-graph
construction and automatic extraction are not required. No Pi/Codex extension,
RPC transport, worker launcher or model call is included.

Install and test from the plugin directory:

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
```

## Dispatch an explicitly reviewed read-only workflow

```python
from bate import MetaToolSpec, StepSpec, input_binding, step_binding
from bate.dispatch import AccelerationPolicy, DispatchMode, MetaToolDispatcher

spec = MetaToolSpec(
    name="lookup_then_read", description="Resolve a key and read its record.",
    input_schema={"type": "object", "properties": {"key": {"type": "string"}},
                  "required": ["key"], "additionalProperties": False},
    steps=(StepSpec("lookup", "lookup", {"key": input_binding("key")}),
           StepSpec("read", "read", {"id": step_binding("lookup", ["id"])})),
    output=step_binding("read"),
    # This example supplies review/evidence explicitly; the dispatcher does not
    # derive these declarations from the primitive functions.
    metadata={"reviewed": True, "side_effect_free": True,
              "internal_data_flows": 1, "varying_data_flows": 1,
              "sequence_purity": 1, "min_branch_dominance": 1,
              "llm_turn_coverage": 1,
              "mean_llm_calls_saved_per_observed_occurrence": 1},
)
events = []
dispatcher = MetaToolDispatcher(
    {"lookup": lambda key: {"id": "id:" + key},
     "read": lambda id: {"record": id}},
    [spec], policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
    telemetry=events.append,
)
assert dispatcher.dispatch(spec.name, {"key": "example"}) == {"record": "id:example"}
assert events[0].path == "meta"
assert dispatcher.model_tool_schemas() == ()  # Exposure is separately opt-in.
```

`MetaToolRegistry` rejects duplicate names unless replacement is explicit.
Primitive/meta-tool name collisions are rejected. Registering an unrecognized
primitive is the host's responsibility; registering a specification does not
create its primitive implementations.

Policy modes:

- `disabled` (default): use a registered user-program fallback, or reject.
- `safe_auto`: require review/evidence declarations, a side-effect-free
  declaration, no compiler warnings and the configured data-flow/turn thresholds.
  Compiled internal step bindings are counted and compared with declared counts.
- `force`: bypass admission checks for explicit experiments. Input JSON Schema
  validation still applies. This mode is not a permission or safety check.

`adispatch()` supports async primitives and fallbacks. `dispatch()` rejects
awaitables and directs the caller to the async interface. Cancellation propagates
without invoking a fallback. Telemetry sink failures do not repeat execution.

## Admission and trust boundaries

`bate.admission.auto_admit_specs(specs, admission, adapter_supported_tools)` narrows
the actual host tool set, checks evidence/schema-conflict metadata and returns
`(admitted_specs, rejected_records)`. It annotates machine approval separately
from human review. It does not contact an agent or execute a primitive.

The default tool names and policy identifier retain compatibility with existing
catalogs; they do not load a Pi adapter. Supply the actual registered tools and
read-only classifications rather than assuming those defaults describe a host.

Important boundaries:

- These are rules over trusted/reviewed catalog data, not a sandbox or a proof of
  semantic equivalence, causal data flow or absence of side effects. Metadata
  supplied by an untrusted catalog must not be treated as authorization.
- Non-read-only specifications may be marked `execution_guard=confirm_each_run`.
  This is a requirement for a future host, **not a confirmation UI implemented
  by this Python dispatcher**. The host must enforce confirmation and primitive
  permissions before execution. `force` does not enforce that metadata guard.
- Invalid inputs, invalid/unresolvable schemas and missing validation
  dependencies cannot execute primitives or escape validation through fallback.
- Execution-error fallback is allowed only for specifications declared
  side-effect-free and with fallback enabled. Side-effecting failures are not
  replayed. The declaration must be trustworthy; the dispatcher cannot verify it.
- Events omit structured arguments/results, but exception/rejection messages
  can contain application data. Redact these at the host telemetry boundary.

## Caller-planned read compositions

`bate.read_batches.read_batch_layout(size)` defines portable 2/3/4-read specs with
each path/offset/limit supplied before execution. Each limit is at most 300 lines;
every primitive result is returned unchanged with its requested range. Reads are
executed sequentially by the existing executor, not concurrently.

`extract_read_batches(traces)` recognizes successful, cross-turn read patterns
with at least three distinct trace IDs and two sessions. Same-turn reads do not
provide savings evidence. Outputs contain parameter bindings and evidence, not
historical file paths or contents. Input file loading remains the caller's job.

`is_caller_bound_read_batch(spec)` verifies the bounded schema, steps, output and
evidence, rather than trusting a metadata tag. Dispatch requires explicit
`allow_caller_bound_read_batches=True`; other workflow admission thresholds are
unchanged. The standalone core remains opt-in. Pi's default-on integration and
worker propagation are separate changes and are not included here.

All ranges must be known beforehand. This layer cannot choose the next file by
understanding code, and historical read patterns do not prove future LLM savings.
