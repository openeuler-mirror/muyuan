# CANNBench + Goal Plus Operator Rules

This file applies to `benchmarks/cannbench/`. It narrows the repository-level
rules for agents that prepare, launch, monitor, retry, or inspect the local
CANN Bench + Goal Plus integration.

## Read First

- For the canonical single-Mish K=4 request, this file and the project-local
  `cannbench-goal-plus` skill are the authoritative runbook. Execute their
  named configs and scripts after preflight; do not rediscover the flow from
  source code.
- Read [README.md](README.md) only for a non-standard profile or broader module
  context.
- Read [THIRD_PARTY.md](THIRD_PARTY.md) only before changing the CANN Bench
  submodule boundary; initialization uses the command below without further
  investigation.
- Before changing Goal Plus behavior, read the owning plugin's
  [AGENTS.md](../../plugins/goal-plus/AGENTS.md) and its host-specific skill.

## Execution Modes

Do not conflate these two modes:

1. **auto_pipeline adapter mode** is the maintained Muyuan benchmark path.
   `GoalPlusGenerator` creates a SearchSpec programmatically and launches Pi
   RPC candidate workers. It does not submit a user-level `/goal-plus` prompt
   and does not create a native Goal Plus goal record.
2. **native Pi `/goal-plus` mode** starts from `pi -p "/goal-plus ..."` and
   follows the complete Pi Goal Plus intake, triage, Search, promotion, and
   terminal-record lifecycle. Use it only when explicitly requested; read
   `plugins/goal-plus/.pi/skills/goal-plus/SKILL.md` and `docs/pi.md` first.

Evidence from one mode is not evidence that the other mode ran.

## Canonical Single-Operator Profile

For the standard request "deploy Goal Plus + CANN Bench and run one operator
with K=4", use this checked-in profile directly. Do not retrace generator,
registry, converter, or third-party source code unless a preflight fails or the
user requests a different profile:

- outer config: `configs/auto-pipeline-goal-plus-mish.yaml`
- inner config: `configs/goal-plus-triton-ascend.npu.yaml`
- task: `tasks/level1/mish`
- backend: `triton_ascend`
- `T=3600` seconds maximum per Pi candidate
- `K=4` Goal Plus Pi candidates inside Mish
- `C=1` outer operator task
- device allocation: `--devices 0 --parallel 1`
- generation/evaluation timeout: 10800 seconds each

From the Muyuan root, deploy and launch with:

```bash
git submodule update --init --recursive benchmarks/cannbench/third_party/cann-bench
uv sync --project benchmarks/cannbench --extra dev

PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh run \
  --config benchmarks/cannbench/configs/auto-pipeline-goal-plus-mish.yaml \
  --workspace . \
  --devices 0 \
  --parallel 1 \
  --gen-timeout 10800 \
  --eval-timeout 10800 \
  --output benchmarks/cannbench/runs/<new-campaign>
```

The command intentionally omits `--foreground`; CANN Bench owns background
execution, monitoring, and retry. Monitor it with:

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh monitor \
  --output benchmarks/cannbench/runs/<new-campaign> --once --json
```

Only create a run-specific config under `runs/<campaign>/control/` when the
task, T, K, backend, or device policy differs from this profile. Never edit the
shared production YAML for a one-off run.

## Run Contract

Resolve and report these values before a real NPU launch:

- `T`: maximum wall-clock exploration time for one candidate worker. In the
  current adapter this maps to `goal_plus.worker_budget.max_runtime_seconds`.
- `K`: candidate workers inside one operator Search. This maps to the frozen
  SearchSpec's `budget.max_parallel` through `goal_plus.max_parallel`.
- `C`: different CANN operator tasks run concurrently by auto_pipeline. This
  maps to CLI `--parallel`.
- `devices`: physical NPU device ids available to outer tasks and verifiers.
- `backend`: the requested code generation target.

Never treat an unqualified "parallel" or "concurrency" number as both `K` and
`C`. Show the resolved meanings before launch. `GoalPlusGenerator` opens one
durable Pi pool per operator with `max_parallel=K`; the maintained production
default is `K=4`. The K Pi coding sessions run concurrently. Their CANN
verifier calls share the operator's assigned NPU and are serialized by the
frozen `npu:<device_id>` resource lock. K therefore does not consume K devices.
Outer `C` still must fit the device allocation unless the user explicitly
accepts `--share-device`.

Current backend status:

- `triton_ascend`: implemented; real NPU verification remains environment
  dependent.
- `ascendc`: target configuration contract documented, adapter not yet
  implemented.

## Launch Gates

Before a production run, verify all of the following:

- the Git submodule is initialized at the recorded gitlink;
- the selected Goal Plus config has `driver: pi-rpc` and `verifier: cann`;
- the selected Goal Plus config has a positive `max_parallel` (normally 4);
- `pi` is available in the effective environment;
- the NPU/CANN/`torch_npu` environment is available;
- requested tasks exist in the pinned CANN Bench checkout;
- the output directory is new or is an existing registered campaign selected
  for monitor/retry; never overwrite prior evidence;
- generation and evaluation timeouts exceed the intended worker and verifier
  work with explicit closeout grace.

The non-NPU scripts validate only the control plane. They are not evidence for
Pi RPC execution, NPU kernels, correctness, performance, or official scoring.

## Monitoring And Recovery

Monitor the outer auto_pipeline state first. Then, when a Goal Plus `run_id`
and attempt-local state root exist, use `goal_plus_monitor_snapshot` against
that state root to inspect candidates, verifier evidence, liveness, and score.
Do not infer inner Search health only from the outer task process.

Use the registered auto_pipeline `retry` command for failed operator tasks.
Do not delete, replace, or silently restart an output directory. Keep outer
task status, inner Search status, selected artifact, final CANN evaluation,
and whether NPU execution occurred as separate evidence fields.

## Change Boundaries

- Keep operator control files, skills, configurations, runs, and reports in
  this benchmark directory.
- Keep the third-party CANN Bench checkout read-only unless a change truly
  belongs upstream.
- Consume Goal Plus through its public Python/CLI interfaces; do not fork its
  lifecycle logic into this benchmark skill.
- Never commit `runs/`, `.gp/`, native transcripts, credentials, or generated
  build artifacts.
