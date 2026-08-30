/goal-plus max_parallel=2 workspace_backend=git_worktree promotion_mode=artifact_only strategy=agent_guided Optimize the read-only Circle Packing template in the current directory and deliver the result as a report without modifying the template.

Maximize the valid `sum_radii` emitted by
`workspace-template/.goal-plus-verifiers/circle_packing_score.py`. A valid
solution must return exactly 26 non-overlapping circles entirely inside the
unit square. Within isolated candidate workspaces, only `initial_program.py`
may be edited. Treat the checked-in `workspace-template/` as read-only; do not
modify the verifier or other frozen task inputs, and do not use network access.

Use the maintained `agent_guided` strategy and a Git worktree-backed Search
with the fixed two candidate lanes. Preserve candidate identity; do not replace
a lane because of score or rank. Continue each native candidate session until
it has produced at least one settled verifier-backed result. Once both lanes
have settled, do not dispatch continuation work: drain the lanes, select and
promote the best verifier-backed valid result, settle the Goal Plus record, and
produce the final compact result and canonical report. Keep the promotion patch
as an artifact under `.gp/` and do not apply it back to `workspace-template/`;
the report and promotion artifact are the only deliverables for this
demonstration. After `search_promote`, do not run `git apply` or any other write
operation against the returned `source_path`.

Treat `search-spec.json` as the authoritative discovered task contract; use it
directly instead of inspecting repository implementation or test files to
rediscover schemas. Normalize its repository-relative `source_path` to
`"workspace-template"` for this current-directory run. Start the two lanes
with complementary hypotheses: one regular row/grid construction and one
staggered-row or relaxed lattice construction. Inspect framework internals
only if a Goal Plus tool call rejects the supplied contract. Before terminal
settlement, confirm that `workspace-template/` remains unchanged.
