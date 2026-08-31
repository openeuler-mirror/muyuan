# Goal Plus (GP)

English | [简体中文](README_zh.md)

Goal Plus is a host-neutral runtime for long-running agent work. Use
`/goal-plus` in Pi or `$goal-plus` in Codex to handle ordinary goals directly
and upgrade measurable optimization tasks to Search Mode: freeze the evaluation
contract, explore isolated candidates, and promote the best verifier-backed
result.

Pi and Codex are the supported host paths.

## Installation

Run the installer from the Goal Plus source directory with the intended Python
environment already active:

```bash
git clone git@gitcode.com:yiyanzhi_akane1/muyuan.git && cd muyuan/plugins/goal-plus
```

| Installation | Command |
| --- | --- |
| Codex only | `./install.sh --codex` |
| Pi only | `./install.sh --pi` |
| Codex and Pi | `./install.sh --codex --pi` |
| Pi development | `./install.sh --pi-dev && source .goal-plus-dev/set_env.sh` |
| Codex plus Pi development | `./install.sh --codex --pi-dev && source .goal-plus-dev/set_env.sh` |
| Check only | `./install.sh --codex --pi --dry-run` |

Normal installation uses the active `python3`; its scripts directory must be
on `PATH`. `--pi` and `--pi-dev` are mutually exclusive. Keep the checkout at
the same path while a host is installed.

On first Codex use, start a new thread, review and trust Goal Plus in `/hooks`,
then start one more fresh thread. Pi development starts with `pi-goal-plus` and
does not modify Pi authentication or global configuration.

## Uninstallation

| Removal | Command |
| --- | --- |
| Pi registration | `pi remove .` |
| Codex registration | `codex plugin remove goal-plus@goal-plus && codex plugin marketplace remove goal-plus` |
| Python runtime | `python -m pip uninstall -y goal-plus` |
| Pi development | `rm -rf -- .goal-plus-dev` |
| Retired Codex local install | `codex plugin remove goal-plus@goal-plus-local && codex plugin marketplace remove goal-plus-local` |

Remove host registrations before the Python runtime, using the same checkout
and Python environment as installation. Uninstallation does not delete target
workspace `.gp/` records or promoted changes. Detailed behavior is documented
in [Development](docs/development.md), [Pi](docs/pi.md), and
[Codex](docs/codex.md).

Start a goal in Pi:

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus Optimize p95 latency for two hours without changing correctness.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
/goal-plus max_parallel=2 promotion_mode=artifact_only Optimize this read-only template.
```

The equivalent Codex entry uses the installed skill:

```text
$goal-plus Fix this bug and verify the test suite.
$goal-plus Optimize p95 latency for two hours without changing correctness.
$goal-plus mode=probe Check whether vectorization is viable.
$goal-plus mode=autonomous Deeply optimize the kernel.
$goal-plus max_parallel=2 promotion_mode=artifact_only Optimize this read-only template.
```

Edit, resume, and independent-final-check commands use the same host-specific
prefix:

```text
Pi:    /goal-plus edit <full revised goal>
Codex: $goal-plus edit <full revised goal>
Pi:    /goal-plus resume
Codex: $goal-plus resume
Pi:    /goal-plus-with-final-check <goal>
Codex: $goal-plus-with-final-check <goal>
```

These exact host commands are the only Goal Plus authority boundary. Natural
language requests, automatic skill loading, direct MCP calls, and the generic
Pi CLI facade cannot create, edit, or resume a Goal Plus record. The legacy
`goal_plus_create` and `goal_plus_update_goal` MCP names remain as deny-only
tombstones so cached clients receive an explicit `GoalPlusEntrypointRequired`
error without mutating `.gp` state. A persisted active record is not
automatically authorized in a later turn; use the exact host-specific
`resume` command.

Leading `key=value` command configuration is parsed and persisted separately
from the natural-language goal. The first non-config token starts the goal.
Explicit values are enforced against a ready SearchSpec draft, while omitted
fields remain Agent decisions. See [API](docs/api.md#host-command-config) for
the supported keys.

One request starts an autonomous run. The agent decides whether Goal Mode is
enough or a frozen verifier makes parallel Search useful; entering Search does
not require an extra approval step. `mode=autonomous` (the default) gives
every initial candidate substantial, renewable same-workspace exploration leases;
`mode=probe` asks for short feasibility probes first. The explicit mode is
recorded in `command_config`; its operational guidance remains in the final line
of `raw_goal`, not as scheduler state.

## Hosts

| Host | Integration assets | Entry | Search worker path |
| --- | --- | --- | --- |
| Pi | installed Pi package | `/goal-plus` or `pi -p "/goal-plus ..."` | durable Pi RPC pool; see [Pi](docs/pi.md) |
| Codex | installed plugin | `$goal-plus` skill prompt | fixed parallel loops with native same-worker continuation; Codex 0.144.1+ hooks cover `UserPromptSubmit`, `PreToolUse`, and `SubagentStop`; see [Codex](docs/codex.md) |

Start an installed Codex plugin from the workspace you want to change:

```bash
cd <target-workspace>
codex -m <main-model> -c model_reasoning_effort=max
```

Start Pi from the workspace you want to change:

```bash
cd <target-workspace>
pi --model <provider/main-model> --thinking xhigh
```

Then submit `$goal-plus <goal>` in Codex or `/goal-plus <goal>` in Pi. Pi keeps
the selected thinking level; `xhigh` is recommended for complex tasks but is not
required or forced. In both
hosts, the main agent is the already-running session selected at launch or with
`/model`; Goal Plus cannot switch it. Omit `main=` in normal usage. Both entries
retain `main=` only as an optional assertion that blocks a mismatch. See the
[Torch CPU example](examples/model-optimize/README.md) for
complete Codex and Pi commands with ready-to-paste prompts.

Host differences and strategy coverage are summarized in
[Agent Host Adapters](docs/agent-host-adapters.md).

## Mental Model

- A **Goal Plus record** is the complete user task.
- A **search task** is one `run_id` over one frozen spec. A goal may link more
  than one search task.
- An initial **SearchPlan** allocates the fixed candidate lanes. It is not a
  per-iteration plan protocol.
- A **candidate** is a long-lived autonomous loop in one isolated workspace
  with verifier history.
- A **worker session** is a host context/provenance handle. Worker lifecycle
  belongs to the host, not the Search runtime.
- The **shared plane** contains the frozen contract, exact verifier commits,
  Global Evidence, asynchronous objective Views, and selection state. It does
  not expose peer reasoning or peer workspaces.
- A **verifier concern** is worker advice. Only the main agent can confirm it;
  confirmation fences the run before all host workers are stopped and a
  successor spec/run is created.

New Pi/Codex Search uses fixed parallel loops: create the initial candidates
once, validate every completion, update the verifier-backed global best, and
resume that same candidate while no global stop condition is true. Main does
not choose later technical directions or replace low-scoring candidates.
Slower workers do not block completed work from being evaluated. See
[Shared Plane](docs/shared-plane.md).

For Git worktrees, freezing first runs the process-verifier suite once in the
clean origin and pins that exact commit. Batch startup then verifies the same
suite in `c001` before materializing the remaining lanes or starting workers.
This catches source-root and relative-path mistakes before candidate work uses
the Search budget; the bootstrap checks are not candidate iterations.

Keep one run for one valid evaluation/edit contract. If a successor is
unavoidable, `source_run_id` preserves bounded frontier/features/scoped
pitfalls as research context, never as reusable scores.

Runtime state lives under `.gp/`. `search_tasks` is append-only; `linked_search`
is only the compatibility view of the current task.

When `promotion_verifiers` are configured, promotion is an independent check,
not a cached pass-through. The runtime checks out the selected verifier-backed
revision, reruns each promotion gate with
`GOAL_PLUS_VERIFIER_PHASE=promotion`, binds the evidence to the selected Git
head and artifact hash, and only then emits a Git-applyable patch. A failed
promotion stays retryable in `ready_to_promote` and emits no patch.

## Documentation

| Need | Read |
| --- | --- |
| Architecture, shared Evidence, rollback, and end-to-end flow | [Shared Plane](docs/shared-plane.md) |
| Current MCP and Pi-local tools | [API](docs/api.md) |
| Host capability comparison | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Ultra work orchestration | [Ultra Orchestration](docs/ultra.md) |
| Optional monitor projections | [Monitor Feature Plugins](docs/feature-plugins.md) |
| Lightweight Search progress UX and rollout plan | [Progress UX](docs/usability-progress.md) |
| Runtime and host logs | [Debugging](docs/debugging-runtime.md) |
| Checkout development, plugin refresh, and multi-developer isolation | [Development](docs/development.md) |
| Specs and runnable examples | [Examples](examples/README.md) |
| Tests and real-host evidence | [Tests](tests/README.md) |

Benchmark-specific tasks, evaluators, campaigns, and comparison evidence are
not part of this isolated runtime directory.

## Workspace and Sandbox Architecture

Goal Plus uses a two-layer workspace model. **Git + Git** combines an outer
Git-worktree workspace with inner Git candidate history. **ThinkThread + Git**
keeps the same inner Git history inside a private ThinkThread COW Child, adding
snapshot-based artifacts and protected execution. Both modes start from a
clean inner Git baseline with no managed untracked product files.

Git + Git is available in Codex and Pi. ThinkThread + Git currently has the
filtered promotion adapter, while Child creation, Search checkpointing,
snapshot-bound verification, references, and host execution remain fail-closed.
Copy remains an explicit artifact-only compatibility path.

See [Workspace and Sandbox Architecture](docs/workspace-sandbox-arch.md) for
the `new -> search -> apply` ASCII flow, design invariants, and the Git/TT
behavior comparison.
