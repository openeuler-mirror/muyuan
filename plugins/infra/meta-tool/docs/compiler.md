# Data-flow compilation

`bate.candidates.MetaToolCandidate` carries the original, aligned call
occurrences; `bate.compiler.MetaToolCompiler` compiles a fixed sequence into
the runtime contract. Candidate discovery is a separate stage.

For each argument, the compiler checks all occurrences. Varying values copied
from a preceding result become `$step` references; varying caller values become
`$input` parameters. Stable literals remain literals, except sensitive fields
which are parameterized. Equal constants alone do not prove data flow.
Inconsistent argument shapes and unbound runtime-local references produce
warnings. Hosts must apply admission before executing a compiled template.

The implementation preserves the shared contracts and compiler from the local
BATE implementation. `candidates.py` contains only the required candidate types,
occurrence slicing and workflow identity helpers; the older greedy selector
and normalization-rule optimization loop are not included.

Run `python -m pytest tests/test_dataflow.py tests/test_compiler.py` to check
cross-occurrence bindings, optional outputs, runtime-local references and both
synchronous and asynchronous replay against fresh primitive results.
