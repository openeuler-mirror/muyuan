# Codex

Codex is the native multi-agent host for Goal Plus. It uses the shared MCP
runtime and a fixed set of parallel autonomous loops built from Codex
collaboration tools.

## Setup

### User installation

From the Goal Plus source directory in the current Muyuan GitCode checkout,
install the runtime and plugin with their user-facing package managers:

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install .
codex plugin marketplace add .
codex plugin add goal-plus@goal-plus
```

Any local Python environment is valid as long as its `goal-plus` executable is
on the PATH inherited by Codex. Skip the pip command when that is already true.

To remove the Codex integration:

```bash
codex plugin remove goal-plus@goal-plus
codex plugin marketplace remove goal-plus
```

Run `python -m pip uninstall -y goal-plus` only after no Pi or Codex
installation still uses that Python environment's Goal Plus commands.

Keep the source directory while the plugin is installed because it remains the
configured local marketplace source.

After installation, start a new Codex thread, open `/hooks`, review and trust
the Goal Plus handlers, then start a fresh thread so every handler is active
from `SessionStart`. Verify the result from the target workspace:

```bash
codex plugin list
goal-plus-codex-doctor --project-root .
```

### Checkout development

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install -e ".[dev]"
python scripts/install_codex_plugin.py --skip-runtime-install
```

This installs the Python runtime editable with the current interpreter, stages
the publishable Codex assets in the persistent
`${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus` directory, registers the
explicit `goal-plus-local` marketplace when needed, and installs or refreshes
`goal-plus@goal-plus-local`. The staging manifest receives one Codex
cachebuster suffix, so rerunning the command refreshes an existing local
installation without changing the checked-in package version. Pass
`--skip-runtime-install` only when the `goal-plus` command is already installed
from the intended source.

The default development marketplace path is stable, not a deleted temporary
directory. To update after `git pull`, rerun the same command:

```bash
python scripts/install_codex_plugin.py --skip-runtime-install
```

The installer only replaces a staging plugin when the surrounding marketplace
metadata proves that it owns `goal-plus-local`; it refuses an unmanaged target.

The installable surface is:

- `skills/` for the three Codex skills;
- `hooks/hooks.json` for the six lifecycle hooks discovered by Codex 0.144.1+;
- `.mcp.json` for the shared `goal-plus --root .gp` server;
- `.codex-plugin/plugin.json` for plugin metadata.

Codex's plugin manifest does not publish `.codex/agents/` custom roles. This
does not remove Search support: `CodexAdapter` maps the normal
`search_candidate_agent` contract to Codex's built-in `default` role and puts
the complete worker contract in the launch message. For a required independent
final check, the launch payload keeps `goal_plus_final_checker` as the
project-local primary role and also declares `fallback_agent_type="default"`.
The skill uses the primary only when the current `spawn_agent` schema exposes
it; an installed-plugin session otherwise uses the self-contained fallback
with the same independent prompt, `fork_turns="none"`, task identity, and one
required `goal_plus_submit_final_check` call.

Codex intentionally does not accept a `hooks` field in this plugin manifest;
it discovers the conventional `hooks/hooks.json` path. Hook trust is bound to
each normalized current hash, so an updated hook must be reviewed again.

Run the read-only preflight from the workspace you intend to use:

```bash
goal-plus-codex-doctor --project-root .
```

It checks Codex 0.144.1 or newer, the `goal-plus` executable and Python source
provenance, plugin/skill loading, MCP enablement, all required hook paths and
trust hashes, built-in/custom agent-role coverage, and the read-only Codex
model catalog. It exits nonzero for a
required failure, supports `--json`, and always reminds you to use a new thread
after install/update/trust changes. `--skip-model-check` is available only for
offline diagnosis; a real multi-model preflight should not skip it.

Remove the managed development install with:

```bash
python scripts/install_codex_plugin.py --uninstall --keep-runtime
```

Omit `--keep-runtime` when Codex is the only development host using this
checkout and the editable runtime should also be uninstalled.

### Project-local asset loading

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install -e ".[dev]"
cp .codex/config.example.toml .codex/config.toml
```

Start Codex in this directory, review `.codex/hooks.json` through `/hooks`, and
use a new thread. Project-local `.codex/skills/` is the development source of
truth; top-level `skills/` is the installable plugin publication surface. After
editing a Codex skill, synchronize and verify both copies:

```bash
python scripts/sync_codex_plugin_assets.py
python scripts/sync_codex_plugin_assets.py --check
```

The local config starts:

```toml
[mcp_servers.goal-plus]
command = "goal-plus"
args = ["--root", ".gp"]
startup_timeout_sec = 10
tool_timeout_sec = 300
enabled = true
default_tools_approval_mode = "approve"
```

Keep `.codex/config.toml` untracked and omit MCP `cwd`; `codex -C` should decide
the project root for both MCP state and hooks. The example explicitly forwards
the annotator's named environment variables. Add any custom API-key variable
named by `GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV` to the same `env_vars` list.
The server-level approval setting lets non-interactive Ultra runs advance the
Goal Plus state machine without pausing for every MCP transition; filesystem
and shell operations remain governed by the Codex sandbox and approval policy.
The same ephemeral annotator turn also produces shared-dir Tool Views; verifier
settlement never waits for it. The host-neutral lifecycle is documented in
[`shared-plane.md`](shared-plane.md#工具复制与采用结算).

Codex 0.144.1+ ships project-local Goal Plus host hooks in
`.codex/hooks.json`; each calls `goal-plus --goal-plus-host-hook` for
`UserPromptSubmit`, `SessionStart`, `PreToolUse`,
`PostToolUse`, `Stop`, and `SubagentStop`. Review and trust them through
`/hooks`. They pre-create `/goal-plus`, restore active state, gate writes/Search
calls, bind ownership, and keep a candidate alive only until its own verifier
submission is durable. Selection and final audit remain parent-owned.
`PostToolUse(goal_plus_create)` is the fallback ownership-binding path. A
candidate is blocked until its own verifier submission is durable. Ordinary subagents do not inherit the parent's next action.

Use `/goal-plus mode=autonomous <goal>` for substantial renewable candidate
exploration (the default), or `/goal-plus mode=probe <goal>` for short
feasibility/potential/blocker probes. The runtime stores the choice only as the
canonical final line of `raw_goal`.

Top-level `Stop` blocks every still-active record and re-presents the full raw
goal, timestamps, elapsed time, phase, next action, and final-check policy. The
main agent must continue or record a terminal status. Candidate `SubagentStop`
semantics are unchanged. Low-level evidence for each automatic stop-hook
invocation is stored under `.gp/host-logs/codex-hook-events/`; see
[Debugging Runtime State](debugging-runtime.md#codex). Generated HTML reports
show separate Stop/SubagentStop counts and a per-subagent breakdown from that
durable evidence.

Use the `goal-plus` skill as the user entry. The `search` skill is internal.

For ordinary Goal Mode work, `/goal-plus` uses the host-neutral
[`goal-plus-ultra-v1`](ultra.md) work DAG and Codex collaboration tools. Run the
main thread with Codex reasoning effort `max`: hooks record
`native_reasoning_effort=max`
but cannot change an already-started first turn. Search candidate lifecycle
below remains a separate specialized path.

## Parallel Loop Flow

After planning and materializing candidates, `search_start_agent_session`
returns a launch payload like:

```json
{
  "tool": "spawn_agent",
  "task_name": "search_agent_001",
  "agent_type": "default",
  "fork_turns": "none",
  "message": "agent_session_id=agent_001; candidate_id=c001; idea: ..."
}
```

Project it onto the current tool schema. `task_name`, `message`, and the fork
field are stable. The default candidate-worker contract is self-contained in
`message`, and `agent_type="default"` selects Codex's built-in no-config role,
so the child uses native parent-model inheritance without a project role
reload. Pass other optional metadata, including `strategy.worker_launch`
overrides, only when it is present in the returned launch payload and exposed
by the current tool schema; never synthesize it from the schema alone.

### Explicit role models

Codex accepts the same role-named `/goal-plus` parameters as Pi:

```text
/goal-plus main=gpt-5.6-sol annotator=gpt-5.6-terra workers=gpt-5.6-terra,gpt-5.6-sol max_parallel=4 optimize ...
```

`main=` is an assertion about the current Codex session model. The
`UserPromptSubmit` hook validates it against the model reported by Codex before
creating or editing a Goal Plus record. Codex hooks cannot switch the main
session model, so a mismatch blocks the command with instructions to select the
requested Codex model first or omit `main=`.

`annotator=` resolves one model on the same Codex host and freezes it into
`strategy.evidence_annotator.model`. `workers=` resolves one or more models into
`strategy.models`. Existing `models=` remains a compatibility alias for
`workers=`; using both aliases in one request is an error. Omitted roles retain
their normal host default or inheritance behavior and are never inferred from
another explicit role.

For worker allocation, `workers=A,B max_parallel=4` round-robins A, B, A, B.
`workers=A,B A1B3 max_parallel=4` is explicit A1B3, and
`workers=A*1,B*3` is equivalent shorthand. Goal Plus calls
`goal_plus_list_models(host="codex")`, resolves each annotator or worker name
before freezing, and records the generated ordered `selected_models` in the
run. A selected worker model remains fixed for that candidate's continuations.
When neither `workers=` nor `models=` is present, Codex children inherit the
normal parent/default model. The catalog is a discovery surface; dispatch still
obeys the current `spawn_agent` schema and reports a launch error if that schema
cannot accept a chosen model override.

The parent then:

1. freezes `strategy.orchestration_mode="parallel_loops"`, plans once, and
   launches all initial candidates;
2. binds returned handles with `search_bind_agent_handle`;
3. calls targetless `wait_agent` and inspects `list_agents` after each wake;
4. final-verifies every newly terminal candidate immediately;
5. observes any verifier-backed global-best update, then continues that same
   native worker with `search_continue_agent_session` followed by
   `followup_task` unless a global stop condition is true;
6. drains live agents before selection.

Every launch message says the child owns one autonomous candidate loop, not
global initial dispatch or final selection. It reads its candidate-local
context and the narrow Global Evidence view, chooses every later
hypothesis/pivot/rebase, edits its workspace, and verifies with a one-line
description of the realized attempt. A `null` View never blocks it. It must not
create candidates, select, report, promote, or mutate Goal Plus state. Main
never replaces it because of low score or one non-improving completion.

## Worker Deadline

Codex has no spawn-time step or timeout field. `worker_budget` therefore
requires `max_runtime_seconds`; the adapter returns a per-dispatch two-stage
parent watchdog:

```json
{
  "budget_control": {
    "initial_wait_timeout_ms": 555000,
    "closeout_message": "Stop new work, final-verify, write the handoff, and return.",
    "final_wait_timeout_ms": 45000,
    "on_exceed": "interrupt"
  }
}
```

The parent waits until the closeout point, sends one message, waits the final
window, and interrupts only after the second timeout. `max_turns` is a hint,
not an enforceable Codex cap. A continuation may receive a larger one-dispatch
budget without changing the frozen spec.

For a sustained single-worker AutoResearch turn, add
`worker_budget.min_runtime_seconds` and optionally `min_verifier_runs`. These
are lower bounds enforced by the project `SubagentStop` hook; an early final
response receives a continuation prompt inside the same Codex worker turn, so
the main agent remains in `wait_agent`. Timing is anchored to the native child
transcript start (discovered no later than its first candidate tool or stop
event) and is recorded under
`.gp/host-logs/codex-autoresearch-leases/`. `max_runtime_seconds` remains the
parent-owned upper bound. The adapter rejects a configuration whose minimum
would still be active at the watchdog soft-closeout point. For example, a
five-minute research lease can use:

```json
{
  "min_runtime_seconds": 300,
  "min_verifier_runs": 1,
  "max_runtime_seconds": 420,
  "on_exceed": "interrupt"
}
```

This reserves 300 seconds for autonomous research, starts parent closeout at
375 seconds, and keeps the 420-second hard interrupt as a final backstop.
Workers must continue verifier-backed research rather than sleeping. A durable
`VerifierWorkspaceSideEffect` or `candidate_action=stop_and_report` releases
the lease early so infrastructure failures return to the parent immediately.

The `PostToolUse` hook may also inject one informational timing advisory when
the remaining outer/worker time is below observed verifier-submission time.
It never stops the worker. Evidence is visible through
`goal_plus_monitor_snapshot`.

For ordinary Ultra work, the optional `orchestration` monitor feature reads
namespaced work-event metadata and preserves Codex operations such as
`spawn_agent`, `wait_agent`, and `followup_task`. It is a read-only projection;
Codex still owns every worker thread and lifecycle action. See
[Monitor Feature Plugins](feature-plugins.md).

For one worker, call `search_get_agent_observability(agent_session_id)`. Codex
uses the `SubagentStop.agent_transcript_path` when present and otherwise
discovers the native `${CODEX_HOME:-~/.codex}/sessions/...` JSONL from the
bound unique task name. The normalized result includes resolved model and
reasoning effort, active and wall duration, terminal state, token/context
counts, tool/message counts, and the session artifact path. It intentionally
omits prompt, reasoning, and tool payload content. Interrupted workers that do
not emit `SubagentStop` remain discoverable by task name.

## Resume

Prefer native continuation while the Codex worker remains available:

```text
search_continue_agent_session -> followup_task
```

The follow-up directive is neutral: preserve the loaded runtime context and
Evidence while continuing the same autonomous loop. Main does not supply a
preferred technical direction.

If the worker is gone or a fresh context is intentional, use
`search_redispatch_candidate`. The new session uses the same workspace, Git
state, verifier history, and structured `.tmp/handoff.json`.

## Supported Strategies

Codex supports the portable builtin set:

- `agent_guided`, `agent`, `default`
- `random`, `random_mode`

See [Agent Host Adapters](agent-host-adapters.md) before enabling another
strategy.

## Verification

Fast contract tests:

```bash
pytest -m codex -q
```

The dedicated parallel-loop scenario is verified with `gpt-5.6-luna`:

```bash
ST_CODEX_MODEL=gpt-5.6-luna \
  pytest -m "st and st_codex" -k codex_parallel_loop_cycle -v -s -rs
```

`codex_parallel_loop_cycle` proves one initial plan, wait-any behavior,
same-worker follow-up on the same candidate, verifier-backed best updates, and
final selection/promotion after drain. Goal Plus report generation happens once
after the parent record reaches a terminal status.

## Logs

```bash
codex exec --json ... > .gp/host-logs/codex-run.jsonl
RUST_LOG=debug codex -c log_dir=./.codex-log
```

Persisted transcripts are under
`${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl`. Use JSON or
rollout logs when you need tool calls; `codex exec -o` contains only the final
message. Cross-host diagnosis is documented in
[Debugging](debugging-runtime.md).
