/goal-plus max_parallel=2 Optimize the read-only EdgeBench-format Ad Placement template in the current directory and deliver the result as a report without modifying the template.

The entire Goal Plus task must reach a terminal status within 8 minutes of
creation. Do not start or continue candidate work after 6 minutes; reserve the
remaining two minutes to preserve the best valid result and complete final
validation, promotion, reporting, and status finalization. No worker lease may
exceed 4 minutes.

Maximize the valid `local_score_sum` emitted by
`workspace-template/.goal-plus-verifiers/ad_local_score.py`. Preserve valid
C++17 compilation and legal output for all ten public deterministic cases.
Within isolated candidate workspaces, only `solution.cpp` may be edited. Treat
the checked-in `workspace-template/` as read-only; do not modify the generator,
tester, verifier, task documentation, or other frozen inputs, and do not use
network access.

Use the maintained `agent_guided` strategy and a Git worktree-backed Search
with the fixed two candidate lanes. Continue the same native candidate sessions
while useful; do not replace a lane because of score or rank. At closeout,
drain the lanes, select and promote the best verifier-backed valid result,
settle the Goal Plus record, and produce the final compact result and canonical
report. Keep the promotion patch as an artifact under `.gp/` and do not apply
it back to `workspace-template/`; the report and promotion artifact are the
only deliverables for this local example. After `search_promote`, do not run
`git apply` or any other write operation against the returned `source_path`.

Treat `search-spec.json` as the authoritative task contract. Normalize its
repository-relative `source_path` to
`"workspace-template"` for this current-directory run. Start the two initial
lanes immediately with complementary hypotheses: one target-area rectangle
construction and one collision-aware expansion or placement strategy. Inspect
framework internals only if a Goal Plus tool call rejects the supplied
contract. Before terminal settlement, confirm that `workspace-template/`
remains unchanged.
