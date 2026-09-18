# Goal Plus (GP)

English | [简体中文](README_zh.md)

Goal Plus is a host-neutral runtime for long-running agent work. Use
`/goal-plus` in Pi or `$goal-plus` in Codex to handle ordinary goals directly
and upgrade measurable optimization tasks to Search Mode: freeze the evaluation
contract, explore isolated candidates, and promote the best verifier-backed
result.

Pi and Codex are the supported host paths.

## Installation

Run the installer from the Goal Plus source directory:

```bash
git clone git@gitcode.com:yiyanzhi_akane1/muyuan.git && cd muyuan/plugins/goal-plus
```

| Installation | Command |
| --- | --- |
| Codex only | `./install.sh --codex` |
| Pi only | `./install.sh --pi` |
| Pi + ThinkThread Search | `./install.sh --pi --thinkthread` |
| Codex + ThinkThread Search | `./install.sh --codex --thinkthread` |
| Pi development | `./install.sh --pi-dev && source .goal-plus-dev/set_env.sh` |
| Show Pi development environments | `./install.sh --env-show` |

Normal installation provisions managed Python 3.12 with uv and locked dependencies.
Each host binds to an immutable code build, independently of Conda, `PATH`, and
the target workspace. `--pi` and `--pi-dev` are mutually exclusive. Inspect installed
builds with `./install.sh --runtime-info`; see [development](docs/development.md)
for update, diagnostics, and cleanup. `--dry-run` does not change host registrations
or releases; its shell bootstrap may download the installer Python/uv.

Existing Codex/Pi registrations trigger an uninstall-and-reinstall prompt (`y/N`).
Use `--yes` for automation. Product assets live under `assets/codex` and `assets/pi`;
editing this checkout does not automatically load its plugin resources.

`--thinkthread` requires a separately installed supported ThinkThread Runtime.
It validates the built-in host Profile and generates the `goal-plus-pi` or
`goal-plus-codex` Profile; it never installs, upgrades, or removes ThinkThread
itself. Start the protected Root with `tt run --profile goal-plus-pi --fs
<target-workspace>` (or `--profile goal-plus-codex` for Codex). From that Root,
`workspace_provider=thinkthread` runs Search candidates in independent private
Children, verifies exact snapshots, and publishes through optimistic `fs.apply`.
Codex ThinkThread inherits the Profile default model; explicit `workers=`
overrides and TT-Root Stop-hook resume are not supported.

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
| Unused managed releases | `./uninstall.sh --unused-runtime` |
| Inspect the full plan | `./uninstall.sh --dry-run` |

With a selector, the uninstaller removes only that component. Codex, normal Pi, and explicitly
selected ThinkThread registrations are removed regardless of their source checkout. A different source
produces a warning and removal continues without confirmation; `--dry-run` only
prints the plan. Local development directories retain their ownership checks.
Normal host installations use the shared Python runtime, so removing one host preserves
the runtime. `--unused-runtime` garbage-collects only releases that no host
registration references, so it is safe to run at any time. Pi development uses
its own managed environment.
Uninstallation never deletes target workspace `.gp/` records or
reverts promoted changes. `--thinkthread` removes the selected generated
`goal-plus-pi` / `goal-plus-codex` Profiles and shared Agent POSIX SDK after a foreign-ownership warning; it never deletes a ThinkThread
Runtime, Root, or state.

## Start a Goal

Start a goal in Pi:

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus Optimize p95 latency for two hours without changing correctness.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
/goal-plus max_parallel=2 promotion_mode=artifact_only Optimize this read-only template.
```

The equivalent Codex entry uses the exact host command:

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
and the generic Pi CLI facade cannot create or edit a Goal Plus record.
Both hosts load shared workflow guidance from MCP instructions; no Goal Plus
navigation skills are installed. Tool authorization is described in [API](docs/api.md).

Leading `key=value` command configuration is parsed and persisted separately
from the natural-language goal. The first non-config token starts the goal.
Explicit values are enforced against a ready SearchSpec draft, while omitted
fields remain Agent decisions. See [API](docs/api.md#host-command-config) for
the supported keys.

One request starts an autonomous run. The agent decides whether Goal Mode is
enough or a frozen verifier makes parallel Search useful; entering Search does
not require an extra approval step. `mode=autonomous` (the default) guides continued
exploration; `mode=probe` prioritizes bounded feasibility checks. After each candidate
returns, Main decides whether to continue from durable Evidence and the user's
explicit limits. Continuation reuses the candidate and session. There is no minimum
worker duration or mandatory early closeout window, and a wait timeout does not
complete the Goal or automatically start another turn.

## Hosts

| Host | Integration assets | Entry | Search worker path |
| --- | --- | --- | --- |
| Pi | installed Pi package | `/goal-plus` or `pi -p "/goal-plus ..."` | shared session tools over Pi RPC; see [Pi](docs/pi.md) |
| Codex | installed plugin | `$goal-plus` host command | shared session tools over Codex app-server; see [Codex](docs/codex.md) |

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
[Circle Packing example](examples/circle-packing/README.md) for
complete Codex and Pi commands with ready-to-paste prompts.

Host differences and strategy coverage are summarized in
[Agent Host Adapters](docs/agent-host-adapters.md).

The following table groups supported Search paths by the native Main Agent.
ThinkThread provides workspace and Child execution infrastructure; the Agent
inside a ThinkThread path is bound by the Root Profile (Pi or Codex).

| Main Agent | Workspace backend | Candidate driver | Annotation driver | LLM Verifier / Adaptive allocation |
| --- | --- | --- | --- | --- |
| Codex | `git_worktree` / `copy` | Codex app-server | Codex CLI | Codex CLI; Git worktree only |
| Pi | `git_worktree` / `copy` | Pi RPC | Pi CLI | Pi CLI; Git worktree only |
| Pi | `thinkthread` | TT Pi Child | TT Pi Child | Pi CLI; Adaptive supported |
| Codex | `thinkthread` | TT Codex Child | TT Codex Child | Codex CLI; Adaptive supported |

`workspace.provider` identifies the workspace provider. Agent identity comes from
the authorized Main session and is recorded as `FrozenSpec.agent_harness`.
Assembly separately freezes `runtime_provider=direct|thinkthread`. These fields
require no additional user setup: start with `$goal-plus <request>` or
`/goal-plus <request>`. Main prepares a Search contract only when needed.
Quality and optional allocation scores share one LLM Verifier call. Selection and
allocation use deterministic policies.

This protocol does not migrate old records. Before switching builds, use the old
build to close active resources and finish its records; create new records with
the new build. Installation and `.gp` layouts are preserved.

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

The default `parallel_loops` mode creates fixed candidate lanes. Main reviews
each returned result and decides whether to continue that same candidate;
candidates choose their own technical direction. Scores do not trigger replacement.
Git worktree runs may explicitly use [Adaptive Search](docs/adaptive-search.md)
for deterministic allocation and optional worker steering. See [Shared Plane](docs/shared-plane.md)
for the common workflow and Evidence contract.

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
| Complete index and content ownership | [Documentation Map](docs/README.md) |
| Stable product and runtime contract | [System Specification](SPEC.md) |
| Host commands and configuration | [Codex](docs/codex.md), [Pi](docs/pi.md), [API](docs/api.md) |
| Install, update, uninstall, and checkout development | [Development](docs/development.md) |
| Runtime and host logs | [Debugging](docs/debugging-runtime.md) |
| Specs and runnable examples | [Examples](examples/README.md) |
| Tests and real-host evidence | [Tests](tests/README.md) |

Benchmark-specific tasks, evaluators, campaigns, and comparison evidence are
not part of this isolated runtime directory.

## Workspace and Sandbox Architecture

Goal Plus separates workspace isolation from inner-agent behavior. Git worktrees
produce commit Evidence; ThinkThread Children produce snapshot Evidence.
`autoresearch` maintains candidate-local Git history, while `common` relies on
the outer artifact and canonical runtime ledger. Copy is an explicit artifact-only
path. Git worktrees are not a security sandbox; supported combinations and
provider guarantees are defined in the workspace document below.

See [ThinkThread and Git Workspace Architecture](docs/workspace-sandbox-arch.md) for
the `new -> search -> apply` ASCII flow, design invariants, and the Git/TT
behavior comparison.
