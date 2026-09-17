/goal-plus max_parallel=2 workspace_provider=git_worktree promotion_mode=apply strategy=agent_guided Implement and optimize the CANNBench Level1 GELU task as an AscendC Direct Invoke operator.

The entire Goal Plus task must reach a terminal status within 60 minutes of
creation. Do not start or continue candidate work after 50 minutes; reserve the
remaining time to preserve the best valid result and complete final validation,
selection, promotion, reporting, and status finalization. No worker lease may
exceed 40 minutes.

Resolve <https://gitcode.com/cann/cann-bench.git> at an exact commit and use
tasks/level1/gelu plus the repository's official evaluation contract as the
semantics, cases, tolerances, Golden, and scoring reference. Run on the
available Ascend NPU.

Follow `examples/ascendc-direct-search/SPEC_DISCOVERY.md`. Do not start Search
until the generated verifier is self-tested, the seed passes the complete
shared correctness contract, the baseline is measured, and all verifier
artifacts are frozen. Then run the fixed candidate lanes, select and promote
the best verifier-backed result, complete the Goal Plus audit, and report the
final metric and artifact paths.

Cadence: once a lane has a plan, make the edit and submit it to the verifier
promptly; do not iterate internally for a long time without submitting.
