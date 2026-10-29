# Meta-tool / BATE Core

This is the first, independently usable layer of the branch-aware template
extraction (BATE) plugin. It provides:

- JSON-compatible trace and tool-call data contracts;
- portable workflow specifications with `$input` and `$step` bindings;
- deterministic synchronous and asynchronous execution of primitive tools;
- full JSON Schema validation before the first primitive is called.

This change does **not** implement automatic extraction, Static/Online learning,
Pi/MCP integration, worker propagation, or read batching. Those features belong
to separate changes. BATE and its older optimized predecessor share these
contracts, not their candidate-selection algorithm. A normalization-rule
optimization loop is not part of this package.

The existing distribution name `meta-tool-online`, Python namespace `awo`, and
version are retained to avoid mixing a package migration into this foundation.
The name does not mean that online learning is implemented here. No sibling
algorithm repository or absolute filesystem path is required.

## Install and verify

Python 3.11+ is required. Install the package and its test dependencies:

```bash
python3 -m pip install -e '.[dev]'
python3 -m awo
python3 -m pytest
```

`python3 -m awo` prints package identity/version only; it is not an extraction
CLI and does not launch an agent.

## Execute a reviewed specification

```python
from awo import MetaToolExecutor, MetaToolSpec, StepSpec, input_binding, step_binding

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
    tests/test_input_validation.py tests/test_core_boundary.py
```
