/goal-plus annotator=openai-codex/gpt-5.6-sol workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol max_parallel=2 Optimize the Torch CPU model in the current directory.

Improve `tokens_per_second` reported by `benchmark.py` while keeping
`verify.py` valid. Keep the model on a single CPU core; do not increase
PyTorch, OpenMP, MKL, or build job thread counts.

Agents may edit `model.py`, `serving.py`, and add implementation files under
`ops/` or `cpp_ops/`. They must not edit `verify.py`, `benchmark.py`,
`workload.json`, `single_thread.py`, or `cpp_reference/`. The vector tail may
be fused into a C++ CPU operator when useful; treat
`cpp_reference/fused_vector_tail.cpp` as a read-only reference and use
`MAX_JOBS=1` for extension builds.

Analyze the current workspace, discover and freeze the verifier-backed Search
contract if the task is ready, run the fixed candidate lanes, promote the best
valid result, and report the final `tokens_per_second`.
