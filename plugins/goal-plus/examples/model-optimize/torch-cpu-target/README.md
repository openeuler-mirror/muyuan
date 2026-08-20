# Torch CPU Optimization Target

This is a runnable `/goal-plus` example. The objective is to improve
`tokens_per_second` while preserving the deterministic output and using one CPU
core.

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

Start Codex with the same main model named by the prompt:

```bash
cd examples/model-optimize/torch-cpu-target
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

In the new Codex thread, paste the complete contents of
[`codex-goal-prompt.md`](codex-goal-prompt.md). The installed plugin supplies
the `/goal-plus` skill, lifecycle hooks, and MCP server even though this target
has no project-local `.codex/` configuration.

## Run with Pi

Start the installed Pi package from this target:

```bash
cd examples/model-optimize/torch-cpu-target
pi --model openai-codex/gpt-5.6-terra --thinking max
```

Then paste the complete contents of
[`pi-goal-prompt.md`](pi-goal-prompt.md).

Both prompts use the same role interface and optimization request. Only model
qualification differs because Pi selects models as `provider/model`, while
Codex uses its native model id. Goal Plus discovers the verifier, metric, edit
surface, Search strategy, and host-specific worker launch from the current
directory.

## Target contract

- `verify.py` is the correctness gate.
- `benchmark.py` reports `tokens_per_second`.
- `model.py` and `serving.py` are editable.
- `single_thread.py`, `workload.json`, the verifier, benchmark, and
  `cpp_reference/` are read-only task inputs.
- Increasing PyTorch, OpenMP, MKL, or build thread counts is invalid.

The optional C++ reference in `cpp_reference/fused_vector_tail.cpp` shows a
single-threaded fused operator pattern; it is evidence for agents, not a
required implementation.
