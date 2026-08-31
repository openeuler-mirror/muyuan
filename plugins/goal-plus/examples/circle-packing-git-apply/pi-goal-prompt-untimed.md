/goal-plus max_parallel=2 workspace_backend=git_worktree promotion_mode=apply strategy=agent_guided Optimize the writable Circle Packing Git workspace in the current directory and apply the best result back to it.

Maximize the valid `sum_radii` emitted by
`workspace/.goal-plus-verifiers/circle_packing_score.py`. A valid solution must
return exactly 26 non-overlapping circles entirely inside the unit square.
Within isolated candidate workspaces, only `initial_program.py` may be edited.
Treat `workspace/` as the writable source Git repository; do not modify the
verifier or other frozen task inputs, and do not use network access.

Use the maintained `agent_guided` strategy and a Git worktree-backed Search
with the fixed two candidate lanes. Preserve candidate identity; do not replace
a lane because of score or rank. Continue each native candidate session until
it has produced at least one settled verifier-backed result. Once both lanes
have settled, do not dispatch continuation work: drain the lanes, select and
promote the best verifier-backed valid result, then call
`search_apply_promotion` to apply the provider-native publication commit back
to `workspace/`. Do not manually copy candidate files, checkout a candidate
branch into source, or run `git apply`. Record the Search result only after
provider apply succeeds, finish the Goal Plus audit and terminal status, then
generate the canonical report exactly once.

Treat `search-spec.json` as the authoritative discovered task contract; use it
directly instead of inspecting repository implementation or test files to
rediscover schemas. Keep its `source_path="workspace"`. Start the two lanes
with complementary hypotheses: one regular row/grid construction and one
staggered-row or relaxed lattice construction. Inspect framework internals only
if a Goal Plus tool call rejects the supplied contract. Before terminal
settlement, confirm that `workspace/` is clean, report whether apply used
`git_cherry_pick` or `git_noop`, and run the public verifier from source.
