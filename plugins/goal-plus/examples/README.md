# Examples

The checked-in examples are either bounded `/goal-plus` scenarios, direct
SearchSpec/runtime fixtures, or deterministic protocol demos. Maintained Search
runs create one initial plan, materialize a fixed set of candidate workspaces,
and resume those same candidates only until the enclosing task's global stop
condition or closeout boundary is true.

## Start Here: Five-Minute Live Run

Use [Circle Packing](circle-packing/README.md) to observe a real Pi `/goal-plus`
or Codex `$goal-plus` flow. It launches two native candidate lanes, runs a public
correctness-and-score verifier, selects and promotes a valid result, and settles
the Goal Plus record within a five-minute whole-task contract. The workspace
uses only the Python standard library and its verifier is fast enough for
several optimization iterations during the short run.

The SearchSpec files below are runtime fixtures, not the recommended end-user
entry. Deterministic scripts are protocol demonstrations and do not prove Pi or
Codex worker execution.

## SearchSpec Files

| Spec | Source | Purpose |
|---|---|---|
| `circle-packing/search-spec.json` | `examples/circle-packing/workspace-template` | five-minute Pi/Codex numeric optimization with two lanes |
| `k_module_search_spec.json` | `tests/fixtures/k_module_problem` | small multi-file correctness and score fixture |
| `signal_processing_search_spec.json` | `tests/fixtures/signal_processing` | wider parallel score fixture |
| `multi-model-signal-spec.json` | `tests/fixtures/signal_processing` | bounded multi-model Codex fixture |
| `edgebench_ad_placement_search_spec.json` | `examples/edgebench-ad-placement/workspace` | public-score C++ workflow |
| `swe_bench_20212_search_spec.json` | `tests/fixtures/swe_bench_20212` | repository patching fixture |

Paths in specs are repository-relative so tests and host agents can load them
without machine-specific configuration.

## Strategy Contract

Maintained hosts are Codex and Pi RPC. Use one of these strategy shapes:

```json
{
  "strategy": {
    "name": "random",
    "orchestration_mode": "parallel_loops",
    "worker_host": "codex",
    "worker_agent_type": "search_candidate_agent",
    "worker_budget": {
      "max_runtime_seconds": 300,
      "max_turns": 8,
      "on_exceed": "interrupt"
    }
  }
}
```

```json
{
  "strategy": {
    "name": "agent_guided",
    "orchestration_mode": "parallel_loops",
    "worker_host": "pi-rpc",
    "worker_budget": {
      "max_runtime_seconds": 600,
      "max_turns": 8,
      "on_exceed": "interrupt"
    }
  }
}
```

`random` creates fixed independent source branches. `agent_guided` requires
the main agent to provide the initial lane intents to `search_start_batch`.
Neither strategy permits a second `search_plan_next` call.

Outside the established EdgeBench Ad regression spec, maintained host-backed
SearchSpecs declare `orchestration_mode`, `worker_host`, and a finite
`worker_budget`. `worker_budget` limits one worker dispatch or renewable lease;
it is not the maximum duration of a whole Goal Plus task. Paste-ready goal
scenarios state their whole-goal limit separately in natural language and
reserve explicit closeout time.

Set `budget.max_parallel` for normal runs. It is the initial number of
long-lived candidate lanes.
Each candidate workspace is a long-lived lane, not a disposable attempt.

Optional multi-model selection belongs in the same frozen strategy:

```json
{
  "models": [
    {"model": "gpt-5.6-terra", "count": 1},
    {"model": "gpt-5.6-sol", "count": 3}
  ]
}
```

The main agent first calls `goal_plus_list_models` for the selected host.
Counts must sum to `max_parallel`; omit all counts to round-robin the listed
models. The runtime generates `selected_models`, so there is no separate
creation-time allocation argument.

## Goal-driven scenario contract

Except for the established EdgeBench Ad Placement and VLIW regression cases,
paste-ready Goal Plus scenarios use the same task-level contract:

1. Start with the native `/goal-plus` or `$goal-plus` entry.
2. State a finite whole-goal limit measured from Goal Plus creation, plus a
   candidate closeout boundary and worker lease that fit inside it.
3. State the metric, correctness gate, edit surface, and frozen inputs before
   candidate work begins.
4. Create the fixed candidate lanes once and continue the same native workers;
   never replace a lane because of score or rank.
5. Drain workers, preserve the best valid result, finish the required
   validation/promotion and Goal Plus audit, set terminal status, then render
   the Search report exactly once.

Direct SearchSpec fixtures and deterministic scripts do not create a Goal Plus
record, so a whole-goal time limit is not applicable to them.

## Host Flow

Codex launches every initial lane using `search_start_agent_session`,
`spawn_agent`, and `search_bind_agent_handle`. When a worker returns and the
global stop condition is false, continue the same native subagent with
`search_continue_agent_session` and `followup_task`.

Pi opens the fixed lane set with `pi_search_pool_open`, consumes terminal
events with `pi_search_pool_wait_any`, and resumes the same candidate/native
session with `pi_search_pool_continue`. `pi_search_pool_snapshot` recovers pool
state after a main-session interruption; `pi_search_pool_close` drains or
interrupts it before final selection. There is no public synchronous batch,
single-candidate runner, or manual pool submit API.

For both hosts:

1. Freeze the verifier-backed spec and create the run.
2. Call `search_plan_next` exactly once.
3. Materialize all initial candidates with `search_start_batch`.
4. Let every lane own its internal hypothesis, edits, verifier calls, and
   evidence commits.
5. Resume completed lanes while time remains; do not create replacement
   candidates based on score or rank.
6. Drain workers, call `search_select`, record/promote the result, finish the
   Goal Plus audit, then call `search_report` after terminal status.

## Scenario Guides

| Scenario | Entry type | Whole-goal limit | Status |
|---|---|---:|---|
| [Circle Packing](circle-packing/README.md) | Pi/Codex Goal Plus | 5 minutes | recommended quick live run; standard library only |
| [Codex/Pi orchestration monitor comparison](orchestration-monitor/README.md) | deterministic script | not applicable | maintained protocol demo |
| [AscendC Direct Search](ascendc-direct-search/README.md) | Pi `/goal-plus` | 60 minutes | target NPU required |
| [CANNBench TileLang Ascend](cannbench-tilelang-ascend/README.md) | Pi `/goal-plus` | 60 minutes | target NPU required |
| [EdgeBench Ad Placement](edgebench-ad-placement/README.md) | dedicated frozen spec | scenario-specific | established regression case; unchanged |
| [Kernel Optimize](kernel-optimize/README.md) | Codex `$goal-plus` template | 30 minutes | environment-specific template |
| [Model Optimize](model-optimize/README.md) | Pi/Codex Goal Plus | 10 minutes | runnable CPU example |
| [Model Optimize GPU](model-opt-gpu/README.md) | Pi `/goal-plus` | 2 hours | WIP; not validated |
| [VLIW Kernel Optimization](vliw-kernel-optimization/README.md) | Pi `/goal-plus` | scenario-specific | established EdgeBench-derived regression case; unchanged |

The GPU model-optimization example is explicitly WIP and is not part of the
maintained validation gate.

## Hidden-Answer QA

Do not expose hidden correctness labels through a worker-visible verifier.
Use only format validation during Search, then grade finalized answers in a
parent evaluator with a predeclared gold-independent aggregation rule.
