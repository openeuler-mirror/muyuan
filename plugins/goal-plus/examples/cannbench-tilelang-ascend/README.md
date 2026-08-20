# CANNBench TileLang-Ascend Search Example

This example uses `goal-plus` as a generic search runtime and uses
CANNBench only as the verifier. It is intended for Pi RPC workers:
`/goal-plus` freezes the SearchSpec, Pi workers edit a TileLang-Ascend
submission workspace, and `search_run_verifier` calls local CANNBench to produce
the score.

The mcp runtime does not call CANNBench `auto_pipeline`. CANNBench is an
external command in `process_verifiers`.

## Required Repositories

The agent should use existing local checkouts when they are available. If they
are missing, clone them first.

```bash
# CANNBench, used only as the verifier/benchmark runner.
git clone https://gitcode.com/cann/cann-bench.git cann-bench

# AKG, used only as the source of TileLang-Ascend skills.
git clone https://gitcode.com/mindspore/akg.git akg_agents
```

Resolver order:

```text
CANNBench root:
  1. --cann-bench-root
  2. CANN_BENCH_ROOT
  3. common local paths such as ./cann-bench, ./code/cann-bench,
     or a sibling ../cann-bench checkout near this mcp repo

AKG agents root:
  1. --akg-agents-root
  2. AKG_AGENTS_ROOT
  3. common local paths such as ./akg_agents, ./akg/akg_agents,
     ./code/akg/akg_agents, or a sibling ../akg/akg_agents checkout
```

After cloning AKG, the TileLang-Ascend skill directory must exist here:

```text
<akg_agents>/python/akg_agents/op/resources/skills/tilelang-ascend
```

Important TileLang-Ascend skill files:

```text
fundamentals/tilelang-ascend-basics/SKILL.md
fundamentals/tilelang-ascend-api/SKILL.md
fundamentals/tilelang-ascend-optimization/SKILL.md
fundamentals/tilelang-ascend-debugging/SKILL.md
guides/tilelang-ascend-matmul/SKILL.md
guides/tilelang-ascend-attention/SKILL.md
guides/tilelang-ascend-elementwise/SKILL.md
guides/tilelang-ascend-reduction/SKILL.md
examples/tilelang-ascend-example-matmul/SKILL.md
examples/tilelang-ascend-example-attention/SKILL.md
examples/tilelang-ascend-example-softmax/SKILL.md
```

Copy the whole `tilelang-ascend` skill directory into the candidate workspace as
read-only task context. Do not ask the worker to use AscendC skills for this
example.

## Target Task

The smallest default task is GEMM:

```text
Task dir:  bench_lab/tilelang_ascend_bench/gemm
Operator:  Gemm
API:       cann_bench.gemm(Tensor A, Tensor B) -> Tensor C
Backend:   tilelang_ascend
```

FlashAttention is the next target after GEMM works:

```text
Task dir:  bench_lab/tilelang_ascend_bench/flash_attention
Operator:  FlashAttention
API:       cann_bench.flash_attention(query, key, value, scaleValue=-1.0, is_causal=False)
Backend:   tilelang_ascend
```

## Prepare A Search Workspace

Run this from the `goal-plus` checkout on the target NPU machine.
If `CANN_BENCH_ROOT` and `AKG_AGENTS_ROOT` are set, the roots can be omitted:

```bash
python examples/cannbench-tilelang-ascend/prepare_workspace.py \
  --task-dir bench_lab/tilelang_ascend_bench/gemm \
  --operator Gemm \
  --function-name gemm \
  --timeout-seconds 2400 \
  --output-dir .tmp/cannbench-tilelang-gemm/workspace \
  --force
```

Or pass explicit roots:

```bash
python examples/cannbench-tilelang-ascend/prepare_workspace.py \
  --cann-bench-root /path/to/cann-bench \
  --akg-agents-root /path/to/akg_agents \
  --task-dir bench_lab/tilelang_ascend_bench/gemm \
  --operator Gemm \
  --function-name gemm \
  --timeout-seconds 2400 \
  --output-dir .tmp/cannbench-tilelang-gemm/workspace \
  --force
```

The script creates:

```text
.tmp/cannbench-tilelang-gemm/workspace/
  build.sh
  setup.py
  cann_bench/                 # only editable area
  _task/                      # frozen task context copied from CANNBench
  _skills/tilelang-ascend/    # copied from AKG, read-only context
  _verifier/cannbench_eval.py # frozen process verifier

.tmp/cannbench-tilelang-gemm/workspace.gp_spec.json
.tmp/cannbench-tilelang-gemm/workspace.gp_spec.verifier_artifacts.json
```

`source_path` in the generated SearchSpec is the submission workspace, not the
CANNBench repository.

## Verifier Contract

The verifier is `_verifier/cannbench_eval.py`. It runs:

```bash
bash <CANNBench root>/scripts/run_evaluation.sh \
  --bench-name cann \
  --source-dir <candidate workspace> \
  --task-dir bench_lab/tilelang_ascend_bench/gemm \
  --operator Gemm \
  --reports-dir <candidate workspace>/_cannbench_reports
```

Then it reads the newest CANNBench JSON report and prints a final JSON object:

```json
{"overall_score": 73.2, "valid": true, "pass_rate": 1.0}
```

`goal-plus` parses the last JSON line and ranks candidates by:

```json
{
  "metric_name": "overall_score",
  "metric_direction": "maximize"
}
```

If CANNBench writes a report with score 0, that is a valid low-scoring
candidate. If no JSON report is produced, the verifier exits non-zero and the
candidate fails the process verifier.

## Edit Surface

For TileLang-Ascend, the worker may edit:

```text
cann_bench/
```

The worker must not edit:

```text
_verifier/
_task/
_skills/
_cannbench_reports/
build.sh
setup.py
dist/
```

This distinction is important. TileLang-Ascend is a Python DSL implementation
inside `cann_bench/*.py`. AscendC would require `csrc/ops/...` C++/CMake files,
which are intentionally not part of this example.

## Run with Pi

Start Pi from the Goal Plus repository root on the target NPU host and paste
the complete contents of [`pi-goal-prompt.md`](pi-goal-prompt.md). The prompt
sets a 60-minute whole-goal limit, stops candidate work at 50 minutes, and uses
the generated 2,400-second worker/verifier bound while reserving closeout time.

`--timeout-seconds` bounds each generated worker lease and verifier invocation;
it is not the whole-goal limit. The natural-language goal owns the latter.

## Worker Guidance

The Pi worker should do this inside each candidate workspace:

1. Call `search_get_agent_context(agent_session_id)` first.
2. Read `_task/TASK.md`, `proto.yaml`, `cases.yaml`, and `desc.md`.
3. Read the TileLang-Ascend skills copied under `_skills/tilelang-ascend/`.
4. Implement the required `cann_bench.<function_name>` API.
5. Run `search_run_verifier(..., agent_session_id=...)` as soon as the package
   can build and expose the API.
6. Use the CANNBench report to decide the next edit.
7. Commit the best state before returning.

For GEMM, expect the first useful changes to be in `cann_bench/gemm.py` and
`cann_bench/__init__.py`. Reuse the existing TileLang example style from
CANNBench's `examples/tilelang_cann_example`.

## Environment Assumptions

The target machine must have:

```text
CANN / torch_npu usable on the target NPU
tilelang-ascend importable by Python
cann-bench dependencies installed
goal-plus configured for Pi RPC
```

This repository does not emulate NPU scoring. On a non-NPU machine, use the
prepare script and README for format validation only; real scores require the
target environment.
