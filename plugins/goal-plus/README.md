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
| Pi + ThinkThread Search | `./install.sh --pi --thinkthread` |
| Pi development | `./install.sh --pi-dev && source .goal-plus-dev/set_env.sh` |
| Show Pi development environments | `./install.sh --env-show` |

Normal installation uses the active `python3`; its scripts directory must be
on `PATH`. `--pi` and `--pi-dev` are mutually exclusive. Keep the checkout at
the same path while a host is installed. Add `--dry-run` to an installation
command to inspect its plan without making changes.

`--thinkthread` requires a separately installed supported ThinkThread Runtime.
It validates the built-in Pi Profile and generates the `goal-plus-pi` Profile;
it never installs, upgrades, or removes ThinkThread itself. Start the protected
Root with `tt run --profile goal-plus-pi --fs <target-workspace>`. From that
Root, `workspace_backend=thinkthread` runs Pi Search candidates in independent
private Children, verifies exact snapshots, and publishes through optimistic
`fs.apply`. Codex does not support the ThinkThread worker host.

On first Codex use, start a new thread, review and trust Goal Plus in `/hooks`,
then start one more fresh thread. Pi development starts with `pi-goal-plus` and
does not modify Pi authentication or global configuration.

## Uninstallation

| Uninstall | Command |
| --- | --- |
| All removable Goal Plus components | `./uninstall.sh` |
| Codex registration | `./uninstall.sh --codex` |
| Pi registration | `./uninstall.sh --pi` |
| Pi development environment | `./uninstall.sh --pi-dev` |
| Goal Plus ThinkThread Profile | `./uninstall.sh --thinkthread` |
| Goal Plus Python package | `./uninstall.sh --runtime` |
| Inspect the full plan | `./uninstall.sh --dry-run` |

With a selector, the uninstaller removes only that component. Codex and normal Pi
registrations are removed regardless of their source checkout. A different source
produces a warning and removal continues without confirmation; `--dry-run` only
prints the plan. Local development directories retain their ownership checks.
Normal host installations use the shared Python runtime, so removing one host preserves
the runtime. Runtime removal fails closed while an unselected registration
still needs it. Pi development uses its own managed environment.
Uninstallation never deletes target workspace `.gp/` records or
reverts promoted changes. `--thinkthread` removes only this checkout's generated
`goal-plus-pi` Profile; it never deletes a ThinkThread Runtime, Root, or state.

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

Edit and independent-final-check commands use the same host-specific prefix:

```text
Pi:    /goal-plus edit <full revised goal>
Codex: $goal-plus edit <full revised goal>
Pi:    /goal-plus-with-final-check <goal>
Codex: $goal-plus-with-final-check <goal>
```

Native session resume restores the conversation, not task execution. Restart an
interrupted Main with `codex exec resume <session-id> '$goal-plus resume'` or
`pi -p --mode json --session <session-id> '/goal-plus resume'`. Ordinary input
steers a running task but never unlocks a stopped one. Both hosts support
summary/pause/resume/clear; clear retains history but permanently revokes execution.
See [Runtime Logic](docs/runtime-logic.md) for the contract and validation boundaries.
An exact start command in a fresh native session creates an independent Goal even
when another session has a nonterminal Goal in the same workspace. Edit, resume,
and control commands remain bound to the session that owns their Goal.

The exact Goal Plus host commands above are the only create/edit authority
boundary. Natural-language requests, automatic skill loading, direct MCP calls,
and the generic Pi CLI facade cannot create or edit a Goal Plus record. The legacy
`goal_plus_create` and `goal_plus_update_goal` MCP names remain as deny-only
tombstones so cached clients receive an explicit `GoalPlusEntrypointRequired`
error without mutating `.gp` state. Native resume never creates or revises a
Goal Plus task; `$goal-plus edit` or `/goal-plus edit` remains required for that.

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
| Codex | installed plugin | `$goal-plus` skill prompt | fixed parallel loops with native same-worker continuation; hooks include `UserPromptSubmit`, `Interrupt`, `PreToolUse`, and `SubagentStop`; see [Codex](docs/codex.md) |

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
`/model`; Goal Plus cannot switch it, and `main=` is not a supported command
configuration key. See the
[Torch CPU example](examples/model-optimize/README.md) for
complete Codex and Pi commands with ready-to-paste prompts.

Host differences and strategy coverage are summarized in
[Agent Host Adapters](docs/agent-host-adapters.md).

The following table groups supported Search paths by the native Main Agent.
ThinkThread provides workspace and Child execution infrastructure; the Agent
inside the supported ThinkThread path is Pi.

| Main Agent | Workspace backend | Candidate driver | Annotation driver | Adaptive Scheduler |
| --- | --- | --- | --- | --- |
| Codex | `git_worktree` / `copy` | Native subagent | Codex CLI | Codex CLI; Git worktree only |
| Pi | `git_worktree` / `copy` | Pi RPC | Pi CLI | Pi CLI; Git worktree only |
| Pi | `thinkthread` | TT Pi Child | TT Pi Child | Not supported |
| Codex | `thinkthread` | Not supported | Not supported | Not supported |

`workspace.backend` identifies the workspace provider. Agent identity comes from
the authorized Main session and is recorded as `FrozenSpec.native_host`; it is
not a SearchSpec option. Runtime derives each role's driver from this identity
and the workspace backend. Scheduler reward and
allocation share one inference call, with no separate reward Agent.

## Mental Model

- A **Goal Plus record** is the complete user task.
- A **search task** is one `run_id` over one frozen spec. A goal may link more
  than one search task.
- An initial **SearchPlan** allocates the fixed candidate lanes. It is not a
  per-iteration plan protocol.
- A **candidate** is a durable lane in one isolated workspace with verifier
  history. `strategy.inner_agent` selects either the iterative `autoresearch`
  worker or a one-attempt-per-dispatch `common` worker.
- A **worker session** is a host context/provenance handle. Worker lifecycle
  belongs to the host, not the Search runtime.
- The **shared plane** contains the frozen contract, exact verifier artifacts,
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
| Stable product and runtime contract | [System Specification](SPEC.md) |
| Architecture, shared Evidence, rollback, and end-to-end flow | [Shared Plane](docs/shared-plane.md) |
| Framework layers, ownership, role capabilities, and glossary | [Architecture Diagrams](docs/architecture.md) |
| Adaptive Search mode and Search Scheduler behavior | [Adaptive Search](docs/adaptive-search.md) |
| Current MCP and Pi-local tools | [API](docs/api.md) |
| Host capability comparison | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Ultra work orchestration | [Ultra Orchestration](docs/ultra.md) |
| Optional monitor projections | [Monitor Feature Plugins](docs/feature-plugins.md) |
| Lightweight Search progress UX and rollout plan | [Progress UX](docs/usability-progress.md) |
| Runtime and host logs | [Debugging](docs/debugging-runtime.md) |
| Checkout development, plugin refresh, and multi-developer isolation | [Development](docs/development.md) |
| Specs and runnable examples | [Examples](examples/README.md) |
| Tests and real-host evidence | [Tests](tests/README.md) |
| Git and ThinkThread workspace, artifact, and publication architecture | [ThinkThread and Git Architecture](docs/workspace-sandbox-arch.md) |

Benchmark-specific tasks, evaluators, campaigns, and comparison evidence are
not part of this isolated runtime directory.

## Workspace and Sandbox Architecture

Goal Plus separates the outer workspace provider from the inner-agent behavior.
The Git outer provider creates candidate worktrees and commit Evidence;
ThinkThread captures one Root-owned baseline, gives every Pi candidate a private
COW Child, and settles attempts as immutable snapshot Evidence. Both outer
providers support the AutoResearch-Git adapter and the MCP-aware
one-attempt-per-dispatch `common` adapter. The compatibility configuration ID
`inner_agent=autoresearch` resolves to AutoResearch-Git: Git outer workspaces use
their candidate branch, while ThinkThread creates an independent Child-private
Git repository with physical `results.tsv`. `common` has neither inner-agent Git
history nor a workspace ledger projection. In every combination `.gp` remains
canonical, and only the outer provider's `git_commit` or `fs_snapshot` is public
Evidence.

Git outer workspaces are available in Codex and Pi. ThinkThread Search is available only
from the installed Pi `goal-plus-pi` Profile after exact release, protocol,
fingerprint, Root capability, and delegated Child capability preflight. It
includes K-way private Children, exact snapshot verification, retained
Annotation Artifact Views, filtered publication, optimistic apply, durable
recovery, and owned-resource cleanup. Copy remains an explicit artifact-only
compatibility path.

See [ThinkThread and Git Workspace Architecture](docs/workspace-sandbox-arch.md) for
the `new -> search -> apply` ASCII flow, design invariants, and the Git/TT
behavior comparison.
