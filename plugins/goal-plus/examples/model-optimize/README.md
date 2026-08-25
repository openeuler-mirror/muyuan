# Model Optimize Example

The runnable target is [`torch-cpu-target/`](torch-cpu-target/). That directory
is intentionally limited to the deterministic single-core PyTorch workload,
correctness verifier, throughput benchmark, profiling helper, and read-only C++
reference used by the optimization task.

## Prerequisites

- Install the Goal Plus runtime and the Pi package or Codex plugin as described
  in the repository README.
- Use a Python environment with PyTorch available.

Check the untouched target first:

```bash
cd examples/model-optimize/torch-cpu-target
python verify.py
python benchmark.py
python profile.py
```

## Run with Codex

Select the main model when starting Codex:

```bash
cd examples/model-optimize/torch-cpu-target
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

In the new Codex thread, paste the complete contents of
[`codex-goal-prompt.md`](codex-goal-prompt.md). The installed plugin supplies
the `$goal-plus` skill, lifecycle hooks, and MCP server; the target itself has
no project-local `.codex/` configuration.

## Run with Pi

Start the installed Pi package from the target directory:

```bash
cd examples/model-optimize/torch-cpu-target
pi
```

Then paste the complete contents of
[`pi-goal-prompt.md`](pi-goal-prompt.md).

The Pi prompt does not pin `main`, `annotator`, or `workers`; it uses the model
already selected in Pi and the installed Goal Plus defaults. The Codex prompt
retains explicit Codex-native role ids as a host-specific model-routing example.
Goal Plus discovers the verifier, metric, edit surface, Search strategy, and
host-specific worker launch from the current target directory; no checked-in
SearchSpec is required.
Both ready-to-paste prompts require the entire Goal Plus task, including final
validation and promotion/reporting, to reach a terminal status within 10 minutes
of Goal Plus creation. Candidate work stops at 8 minutes, leaving 2 minutes for
closeout, and each worker lease is capped at 6 minutes.

## Target contract

- `verify.py` is the correctness gate.
- `benchmark.py` reports `tokens_per_second`.
- `model.py` and `serving.py` are editable.
- `single_thread.py`, `workload.json`, the verifier, benchmark, and
  `cpp_reference/` are read-only task inputs.
- Increasing PyTorch, OpenMP, MKL, or build thread counts is invalid.

The optional C++ reference in
`torch-cpu-target/cpp_reference/fused_vector_tail.cpp` shows a single-threaded
fused operator pattern; it is evidence for agents, not a required
implementation.
