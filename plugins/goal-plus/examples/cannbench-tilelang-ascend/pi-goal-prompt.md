/goal-plus max_parallel=2 Run the CANNBench TileLang-Ascend GEMM optimization example.

The entire Goal Plus task must reach a terminal status within 60 minutes of
creation. Do not start or continue candidate work after 50 minutes; reserve the
remaining time to preserve the best valid result and complete final validation,
selection, reporting, and status finalization. Do not promote automatically.
No worker lease or verifier invocation may exceed 40 minutes.

Read `examples/cannbench-tilelang-ascend/README.md` first. Reuse local
CANNBench and AKG checkouts when available; otherwise clone the repositories
named there. Confirm that the AKG TileLang-Ascend skills are present.

Prepare the workspace with:

python examples/cannbench-tilelang-ascend/prepare_workspace.py \
  --cann-bench-root <local cann-bench root> \
  --akg-agents-root <local akg_agents root> \
  --task-dir bench_lab/tilelang_ascend_bench/gemm \
  --operator Gemm \
  --function-name gemm \
  --timeout-seconds 2400 \
  --output-dir .tmp/cannbench-tilelang-gemm/workspace \
  --force

Use the generated `workspace.gp_spec.json` and
`workspace.gp_spec.verifier_artifacts.json` as the Search contract. Confirm the
workspace, public verifier, metric, edit surface, and frozen artifacts before
starting Search. Workers may edit only `cann_bench/`; they must read the frozen
task and TileLang-Ascend skill files and run the verifier after the first
complete implementation and every meaningful optimization.

Use the generated fixed Pi RPC lanes without score-based replacement. Drain
the pool at the closeout boundary, select the best verifier-backed candidate,
complete the Goal Plus audit without promotion, set terminal status, and call
`search_report` exactly once afterward. Report the run id, every candidate's
`overall_score`, selected candidate id, report path, and latest CANNBench JSON
report path.
