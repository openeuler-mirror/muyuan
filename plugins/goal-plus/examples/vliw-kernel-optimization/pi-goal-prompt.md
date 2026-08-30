/goal-plus max_parallel=2 Optimize the read-only VLIW template in the current directory and deliver the result as a report without modifying the template.

The entire Goal Plus task must reach a terminal status within 8 minutes of
creation. Do not start or continue candidate work after 6 minutes; reserve the
remaining two minutes to preserve the best correct result and complete final
validation, promotion, reporting, and status finalization. No worker lease may
exceed 4 minutes.

Minimize the correct `cycles` value emitted by
`workspace-template/.goal-plus-verifiers/vliw_score.py`. Preserve the simulator
semantics and all public-case correctness. Within isolated candidate
workspaces, only `solution.py` may be edited. Treat the checked-in
`workspace-template/` as read-only; do not modify the runner, verifier,
simulator, test cases, or score adapter. Do not hard-code case seeds or use
network access.

Use the maintained `agent_guided` strategy and a Git worktree-backed Search
with the fixed two candidate lanes. Freeze
`workspace.promotion_mode="artifact_only"` because this tracked template is a
read-only demonstration exception to the normal apply default. Continue the same native candidate sessions
while useful; do not replace a lane because of score or rank. At closeout,
drain the lanes, select and promote the best verifier-backed correct result,
settle the Goal Plus record, and produce the final compact result and canonical
report. Keep the promotion patch as an artifact under `.gp/` and do not apply
it back to `workspace-template/`; the report and promotion artifact are the
only deliverables for this local example. After promotion, do not run
`git apply` or any other write operation against the returned `source_path`.

Use `workspace-template` as the Search source path. Start the two initial lanes
with complementary hypotheses: one instruction-scheduling or bundling
improvement and one memory-access or register-pressure improvement. Report the
selected verifier-backed cycle count and the improvement over the 147734-cycle
starter implementation. Before terminal settlement, confirm that
`workspace-template/` remains unchanged.
