# Examples

This directory contains only user-facing Goal Plus scenarios with a distinct
reason to exist. Test-only SearchSpecs live beside their owning fixtures under
`tests/fixtures/`; deterministic protocol diagnostics live under `scripts/`.

An example belongs here only when it has a concrete user entry, a measurable
acceptance contract, and a capability that is not already demonstrated more
simply by another scenario. Unvalidated future ideas and duplicate environment
preparers do not belong in the supported examples index.

## Start Here: Five-Minute Live Run

Use [Circle Packing](circle-packing/README.md) to observe a real Pi `/goal-plus`
or Codex `$goal-plus` flow. It launches two native candidate lanes, runs a public
correctness-and-score verifier, selects and promotes a valid result, and settles
the Goal Plus record within a five-minute whole-task contract. The workspace
uses only the Python standard library and its verifier is fast enough for
several optimization iterations during the short run.

## Why Each Example Exists

| Scenario | Unique motivation | Entry | Requirements |
| --- | --- | --- | --- |
| [Circle Packing](circle-packing/README.md) | shortest end-to-end progress and completion-card demo | Pi or Codex Goal Plus, 5 minutes | Python standard library |
| [Model Optimize](model-optimize/README.md) | realistic CPU/PyTorch optimization on both maintained hosts | Pi or Codex Goal Plus, 10 minutes | PyTorch |
| [VLIW Kernel Optimization](vliw-kernel-optimization/README.md) | program/schedule optimization with a simulator correctness gate | Pi Goal Plus, 8 minutes | Python standard library |
| [EdgeBench Ad Placement](edgebench-ad-placement/README.md) | compiled C++ optimization with ten public cases | Pi Goal Plus, 8 minutes | C++17 compiler |
| [AscendC Direct Search](ascendc-direct-search/README.md) | the single supported dynamic NPU task-generation path | Pi Goal Plus, 60 minutes | target Ascend NPU and references |

Circle Packing and Ad Placement own checked-in SearchSpecs beside their
read-only workspace templates. The other scenarios intentionally exercise Goal
Plus Spec Discovery or user-supplied target material.

Normal Git/ThinkThread SearchSpecs default to
`workspace.promotion_mode="apply"`. Read-only templates and test fixtures must
declare `artifact_only` explicitly.

Every paste-ready example makes its Search policy explicit in the leading typed
`key=value` command-config tokens. The first non-config token starts the
natural-language goal. These values are persisted in
`GoalPlusRecord.command_config` and mechanically checked against a ready
SearchSpec draft. The examples are deliberately explicit even when a value
equals the runtime default; ordinary user requests may omit any field and let
the Main Agent decide it.

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

Maintained host-backed SearchSpecs declare `orchestration_mode`, `worker_host`,
and a finite `worker_budget`. `worker_budget` limits one worker dispatch or
renewable lease; it is not the maximum duration of a whole Goal Plus task.
Paste-ready goal scenarios state their whole-goal limit separately in natural
language and reserve explicit closeout time.

Set `budget.max_parallel` for normal runs. It is the initial number of
long-lived candidate lanes.
Each candidate workspace is a long-lived lane, not a disposable attempt.

Optional multi-model selection belongs in the same frozen strategy:

```json
{
  "models": [
    {"model": "gpt-5.6-terra", "count": 1},
    {"model": "gpt-5.6-luna", "count": 1}
  ]
}
```

The main agent first calls `goal_plus_list_models` for the selected host.
Counts must sum to `max_parallel`; omit all counts to round-robin the listed
models. The runtime generates `selected_models`, so there is no separate
creation-time allocation argument.

Ordinary paste-ready prompts omit `main=`, `annotator=`, and `workers=` so they
use the active host model and installed defaults. The
[Circle Packing Codex prompt](circle-packing/codex-goal-prompt.md) is the one
intentional exception: it demonstrates one Terra worker lane and one Luna
worker lane, with Terra as the evidence annotator.

## Goal-driven scenario contract

Paste-ready Goal Plus scenarios use the same task-level contract:

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

Test-only SearchSpec fixtures do not create a Goal Plus record, so a whole-goal
time limit is not applicable to them.

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

## Hidden-Answer QA

Do not expose hidden correctness labels through a worker-visible verifier.
Use only format validation during Search, then grade finalized answers in a
parent evaluator with a predeclared gold-independent aggregation rule.
