# Codex Goal Plus prompt: C++ kernel optimization

Paste this complete prompt into Codex from a workspace with the following
layout:

```text
<source>/
  matmul_ref.py          # torch Model + get_inputs
  matmul_kernel.cpp      # candidate-editable kernel source
  matmul_wrapper.py      # ModelNew wrapper, loads the .cpp via load_inline
```

Replace `<source>` with the workspace-relative source directory before use.

```text
$goal-plus Optimize the matmul kernel under <source> using the kernel-optimize example contract.

The entire Goal Plus task must reach a terminal status within 30 minutes of
creation. Do not start or continue candidate work after 25 minutes; reserve the
remaining time to preserve the best valid result and complete final validation,
selection, promotion, reporting, and status finalization. Use two fixed
candidate lanes and a maximum worker lease of 20 minutes.

Read `examples/kernel-optimize/README.md` first. Copy
`examples/kernel-optimize/verifier/{verify.py,benchmark.py,_common_utils.py}`
into `<source>/_verifier/`. Copy `<source>/matmul_ref.py` to
`<source>/_verifier/matmul_torch.py` and `<source>/matmul_wrapper.py` to
`<source>/_verifier/matmul_impl.py`.

Use `avg_latency_ms`, minimized, as the ranking metric. Freeze correctness
before latency. Only `matmul_kernel.cpp` is editable; deny `_verifier/`,
`matmul_ref.py`, and `matmul_wrapper.py`. Freeze all verifier files and copied
reference/wrapper artifacts. Use the Git worktree backend and the maintained
Codex `parallel_loops` worker contract with `max_parallel=2`,
`max_runtime_seconds=1200`, `max_turns=20`, and `on_exceed=interrupt`.

Before Search, verify that the untouched source passes correctness and produces
a finite baseline latency. Create the fixed lanes exactly once. Let each worker
choose its own evidence-supported optimization, confirm every returned result
with the frozen verifier, and continue the same native worker only while the
closeout boundary permits. Do not replace a lane because of score or rank.

At closeout, drain workers, select and promote the best verifier-backed Git
revision, complete the Goal Plus audit, set terminal status, and call
`search_report` exactly once. Report the baseline and final latency, every
candidate's score and iteration count, selected candidate and commit, promotion
result, and report path.
```

The wrapper that loads `matmul_kernel.cpp` is denied because changing the
binding contract is outside this example. If it does not already define
`ModelNew`, prepare a frozen adapter before Search. For NPU-side DSLs, require a
working target runtime before freezing; do not silently fall back to CPU timing
for an accelerator result.
