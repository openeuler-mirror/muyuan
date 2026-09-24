/goal-plus max_parallel=2 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided Optimize the read-only Torch CPU template in the current directory and deliver the result as a report without modifying the template.

The entire Goal Plus task must reach a terminal status within 10 minutes of
creation. Do not start or continue candidate work after 8 minutes; reserve the
remaining time to preserve the best valid result and complete final validation,
promotion, reporting, and status finalization. No worker lease may exceed 6
minutes.

Maximize the median `tokens_per_second` emitted by
`workspace-template/.goal-plus-verifiers/torch_cpu_tokens.py --rank` while its
`--validate` mode remains valid. Keep the model on a single CPU core; do not
increase PyTorch, OpenMP, MKL, or build job thread counts.

Within isolated candidate workspaces, agents may edit `model.py`, `serving.py`,
and add implementation files under `ops/` or `cpp_ops/`. Treat the checked-in
`workspace-template/` as read-only. Candidates must not edit `verify.py`,
`benchmark.py`, `workload.json`, `single_thread.py`, `cpp_reference/`, or
`.goal-plus-verifiers/`. The vector tail may be fused into a C++ CPU operator
when useful; treat `cpp_reference/fused_vector_tail.cpp` as a read-only
reference and use `MAX_JOBS=1` for extension builds.

Use `workspace-template` as the Search source path, the maintained
`agent_guided` strategy, and a Git worktree-backed Search with two fixed lanes.
Freeze `workspace.promotion_mode="artifact_only"` because this tracked template
is a read-only demonstration exception to the normal apply default.
Run the lanes, select and promote the best verifier-backed valid result, settle
the Goal Plus record, and produce the compact result and canonical report. Keep
the promotion patch under `.gp/`; do not apply it back to
`workspace-template/`, and do not run `git apply` against the returned
`source_path`. Before terminal settlement, confirm that `workspace-template/`
remains unchanged.

Model selection: this prompt pins no model. Every subagent (candidate worker,
Evidence annotator, and any other helper) leaves its model unspecified and
inherits the main agent's current model.

Cadence: once a candidate has a plan, make the edit and submit it to the
verifier promptly; do not iterate internally for a long time without
submitting.
