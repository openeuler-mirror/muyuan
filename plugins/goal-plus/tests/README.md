# Tests

Default tests are fast and host-free. Module APIs and controlled intermediate
results cover business behavior without launching agents. Real-host system
tests (ST), including full E2E, run only when explicitly requested by the developer;
a code change or phase boundary is not an automatic ST trigger.

## Test Layers

| Layer | Command | Proves |
| --- | --- | --- |
| Default fast gate | `pytest -q` | models, runtimes, workspaces, verifiers, APIs, Codex/Pi assets |
| Integration slice | `pytest -m integration -q` | one-plan parallel search, freeze+plan+batch+verify end-to-end |
| Example slice | `pytest -m example -q` | `examples/*` fixtures drive real generated assets |
| Codex fast slice | `pytest -m codex -q` | Codex adapter, hooks, assets, pool contract |
| Pi fast slice | `pytest -m pi -q` | Pi extension, driver, supervisor, assets |
| Runtime-focused | `pytest tests/test_runtime_unit.py` | Search state machine without a host |
| Adaptive Search-focused | `pytest tests/adaptive_search -q` | scheduler tasks, lazy allocation, runtime integration, and architecture boundaries |
| Real-host ST | `pytest --run-system-tests -n 0 -m "(st and (st_codex or st_pi_rpc or st_pi_thinkthread)) or st_pi" -v -s` | maintained native launch, hooks/events, worker lifecycle, final evidence |

Default tests must never launch Codex or Pi. A host behavior claim requires
the matching ST; if it cannot run, report that gap.
The suite excludes Torch and AscendC scenarios, including their asset and
hardware tests.
ST requires `--run-system-tests`; `-m`, `-k`, and direct node selection alone
never authorize a host launch or preflight. Keep the flag out of default
configuration, installers, and automatic development loops. Collection-only
does not probe hosts, even with the flag. Preflight runs only for selected hosts.
With the `dev` extra installed, the default gate uses 32 workers. On smaller
machines, override this with `pytest -n 2 --dist=load -q` (or another count no
greater than half the available CPUs). Run real-host ST with `-n 0` or
`-o addopts=''` so host processes, model calls, and machine resources do not
interfere with one another.

`integration` and `example` tests are skipped by default via
`tests/conftest.py`. Add a marker name to `-m` only when intentionally auditing
that slice.

## Hook Tests

Default hook tests drive `goal_plus.goal_plus_stop_hook.main()` in-process via
`run_host_hook_inprocess` in `tests/_runtime_helpers.py`: cwd, stdin/stdout/stderr
and the environment are restored after each call, including failures. The returned
`HookResult` (`returncode` / `stdout` / `stderr`) asserts like a
`subprocess.CompletedProcess`. Do not spawn one interpreter per hook event —
each cold start costs about a second of imports, and per-event subprocess calls
previously dominated total suite time. Default tests also never launch a real
model; paths that would run `codex exec` (for example the model preflight) are
monkeypatched — see the explicit-role-models test for the pattern.

A fixed, small set of tests keeps the real entry points covered:

- `tests/test_cli_entry.py` — the thin `goal_plus.cli` console dispatcher; also
  guards that importing it never loads the MCP server stack.
- The nested-cwd cases in `tests/test_codex_goal_plus_hooks.py` — the legacy
  `python -m goal_plus.server` entry.

Tests of process isolation, exit or environment inheritance also retain real
subprocesses; the in-process helper does not emulate these properties.
Source subprocess tests must set `PYTHONPATH` to the repository `src/` so they
exercise the checked-out source, not whichever `goal_plus` build happens to be
installed in the environment. Package installation tests instead exercise their
own isolated installation.

## System-Test Markers

| Marker | Runner | Default model |
| --- | --- | --- |
| `st_codex` | `codex exec` | `gpt-5.6-terra` or `ST_CODEX_MODEL` |
| `st_pi_rpc` | `goal-plus-pi-worker` + Pi RPC | host default or `ST_PI_MODEL` |
| `st_pi` | native Pi `/goal-plus` print/TUI | host default or `ST_PI_MODEL` |
| `st_pi_thinkthread` | installed `goal-plus-pi` ThinkThread Profile | Profile default Pi model |

Every `tests/st/` case has `st` plus exactly one host marker. Native Pi command
tests live in `tests/st_pi/`. `-s` is required so failure log paths remain
visible.

## Common Real-Host Checks

```bash
# Codex ordinary-language entrypoint guard: the MCP mutation is blocked and
# the isolated workspace remains free of `.gp` state
ST_CODEX_MODEL=gpt-5.6-luna \
  pytest --run-system-tests -n 0 -m "st and st_codex" \
  -k "goal_plus_entrypoint_guard or exact_host_command_can_start or goal_plus_required_final_checker" \
  -v -s -rs

# Pi ordinary-language guard plus exact native-command positive control
ST_PI_MODEL=openai-codex/gpt-5.6-luna ST_PI_THINKING=xhigh \
  pytest --run-system-tests -n 0 -m st_pi \
  -k "plain_prompt_cannot_start or exact_command_can_start or with_final_check_runs_pi_reviewer" \
  -v -s -rs

# Codex state redispatch and fixed parallel-loop continuation
pytest --run-system-tests -n 0 -m "st and st_codex" -k codex_redispatch -v -s -rs
ST_CODEX_MODEL=gpt-5.6-luna \
  pytest --run-system-tests -n 0 -m "st and st_codex" -k codex_parallel_loop_cycle -v -s -rs

# Bounded native Codex Circle: two candidates, same-session second turn,
# required Annotation, artifact-only publication, report and scope cleanup
ST_CODEX_MODEL=gpt-5.6-sol ST_CODEX_REASONING_EFFORT=low \
  pytest --run-system-tests -n 0 \
  'tests/st/test_st_scenarios.py::test_scenario[codex_circle_packing_cycle]' -v -s -rs

# Codex native child hook isolation and main-session binding
pytest --run-system-tests -n 0 -m "st and st_codex" -k codex_native_child_hooks -v -s -rs

# Real Codex Interrupt: ordinary chat stays stopped, explicit Goal Plus resume completes
pytest --run-system-tests -n 0 -m "st and st_codex" -k exec_native_resume_after_interrupt -v -s -rs

# Native Main resume -> active Search checkpoint recovery -> fresh worker Evidence
pytest --run-system-tests -n 0 -m "st and st_codex" tests/st/test_st_search_recovery.py -v -s -rs
ST_PI_BINARY="$PWD/.goal-plus-dev/bin/pi-goal-plus" \
  pytest --run-system-tests -n 0 -m "st and st_pi_rpc" tests/st/test_st_search_recovery.py -v -s -rs

# Codex one-worker AutoResearch lease: 5 minute lower bound, 7 minute watchdog
ST_CODEX_TIMEOUT=1200 \
  pytest --run-system-tests -n 0 -m "st and st_codex" -k codex_autoresearch_lease -v -s -rs

# Codex revises one goal after result 1, refreezes, and completes result 2
pytest --run-system-tests -n 0 -m "st and st_codex" -k goal_plus_spec_revision -v -s -rs

# Pi durable wait-any pool
ST_PI_CYCLE_WORKER_SECONDS=120 \
  pytest --run-system-tests -n 0 -m "st and st_pi_rpc" -k managed_pool_wait_any -v -s -rs
ST_PI_MODEL=openai-codex/gpt-5.6-luna ST_PI_CYCLE_WORKER_SECONDS=90 \
  pytest --run-system-tests -n 0 -m "st and st_pi_rpc" -k parallel_loop_cycle -v -s -rs

# Native Pi Goal Plus lifecycle
pytest --run-system-tests -n 0 -m st_pi -v -s -rs

# Native Pi Git outer + common inner-agent Circle apply
pytest --run-system-tests -n 0 -m st_pi -k circle_packing_git_common_agent_apply -v -s -rs

# Native Pi ends a turn early, then the Stop gate triggers a follow-up turn
pytest --run-system-tests -n 0 -m st_pi -k stop_gate_intercepts -v -s -rs

# Process loss / RPC abort, JSON no-op/chat, then explicit Goal Plus resume
# PTY cases cover the --resume picker and command-bearing native session restore
# user-message priority; JSON cases cover exact --session and --continue.
ST_PI_BINARY="$PWD/.goal-plus-dev/bin/pi-goal-plus" \
  pytest --run-system-tests -n 0 -m st_pi -k native_session_resume -v -s -rs

# Native interactive Codex resume plus fork isolation
pytest --run-system-tests -n 0 -m "st and st_codex" \
  -k "interactive_native_resume or interactive_fork" -v -s -rs

# Pi + ThinkThread V3: Root/ABI, K=2, Annotation Artifact Views,
# cancellation, optimistic apply, conflict, recovery, and cleanup
pytest --run-system-tests -o addopts='' tests/st_thinkthread \
  -m "st and st_pi_thinkthread" -v -s -rs

# Required independent final checks
pytest --run-system-tests -n 0 -m "st and st_codex" -k goal_plus_required_final_checker -v -s -rs
pytest --run-system-tests -n 0 -m "st and st_codex" -k final_checker_rejects_then_accepts -v -s -rs
pytest --run-system-tests -n 0 -m st_pi -k with_final_check_runs_pi_reviewer -v -s -rs

# All maintained hosts
pytest --run-system-tests -n 0 -m "(st and (st_codex or st_pi_rpc or st_pi_thinkthread)) or st_pi" -v -s -rs
```

Useful overrides:

```bash
ST_CODEX_MODEL=gpt-5.6-luna pytest --run-system-tests -n 0 -m "st and st_codex" -v -s
ST_PI_MODEL=openai-codex/gpt-5.6-luna ST_PI_THINKING=high \
  pytest --run-system-tests -n 0 -m "st and st_pi_rpc" -v -s
ST_CODEX_TIMEOUT=3600 pytest --run-system-tests -n 0 -m "st and st_codex" -v -s
ST_THINKTHREAD_TIMEOUT=1800 ST_THINKTHREAD_ANNOTATION_TIMEOUT=600 \
  pytest --run-system-tests -o addopts='' tests/st_thinkthread \
  -m "st and st_pi_thinkthread" -v -s
```

## Contracts

### TT VLIW Retained Turns

With explicit authorization, run the bounded VLIW role test serially:

```bash
ST_PI_MODEL=sub2api-openai/gpt-5.6-sol ST_PI_THINKING=low \
  pytest --run-system-tests -o addopts='' tests/st_thinkthread/test_vliw.py -q

ST_PI_MODEL=zai/glm-5.3-flash ST_PI_THINKING=low \
  pytest --run-system-tests -o addopts='' tests/st_thinkthread/test_vliw.py -q
```

The existing TT controller creates a single VLIW candidate and requests two small
address-reuse edits. Real Pi turns, the same retained Child/session, distinct
attempt/snapshot/Git identities, two ledger rows, frozen verifier scores, two
Annotation Views, artifact-only publication, reports and cleanup are checked.
Main planning is deterministic; this does not measure autonomous optimization or
Main deadline handling. The annotator inspects immutable code; this single-lane
case does not cover peer comparison or Adaptive Scheduler/Allocation.

`ST_PI_MODEL` and `ST_PI_THINKING` now also apply to the TT fixture through native
Pi arguments, inherited by Children. `ST_THINKTHREAD_PRESERVE_ON_FAILURE=1`
preserves Runtime state after stopping Roots (also on success); use a unique
`--basetemp` outside the checkout to avoid pytest retention deleting diagnostics.
Without this override teardown stops and removes only the fixture's own Roots.

### Pi Circle Role Checks

With explicit developer authorization, Annotation and Scheduler can run on the
same pinned Pi model without a native Main or candidate inference:

```bash
ST_PI_MODEL=zai/glm-5.3-flash ST_PI_THINKING=low \
  pytest --run-system-tests -n 0 tests/st_pi/test_circle_internal_roles.py -v -s -rs
```

This test prepares a fresh Circle Git source, settles one deterministic change
through the normal verifier, and drives real role adapters through claim,
context, completion and reward consumption. Both processes register in a real
scope owned by pytest; cleanup runs on failure too. It verifies hard Evidence
and exact artifacts remain unchanged. It does not cover native Main, candidate
inference, detached drainer wakeups, allocation confirmation or TT Child recovery.
`ST_PI_ROLE_TIMEOUT` bounds each role invocation (default 180 seconds).

Circle pool wait/continuation tests in `st/test_st_pi_rpc.py` also accept
`ST_PI_MODEL` and `ST_PI_THINKING`. The TT suite currently uses a non-Circle
fixture and the installed Profile model, so selecting it does not inherit these
overrides or satisfy a Circle-only validation request.

The Circle native Main test uses [a bounded ST prompt](st_pi/circle-common-prompt.txt):
two candidates, one small change and one verifier call per dispatch, followed by
independent publication verification, apply and report. Its 900-second Goal budget
includes Main setup and closeout; `ST_PI_SEARCH_TIMEOUT` defaults to 1200 seconds
for that test's external watchdog. The user-facing optimization example is separate.

### Module Boundaries

Test admission, claim, completion, retry, isolation, and recovery at their
typed owner boundaries. An isolated real-agent ST should drive one role through
legal APIs without requiring a full Main/candidate/annotation E2E. Such module
ST still requires explicit developer request; it is not a default unit test.

One simple normal-path LLM ST per model role is sufficient for module acceptance;
reuse applicable evidence. Deterministic modules do not need an LLM, and complete
E2E runs or model matrices are not prerequisites for module delivery.

Add failure branches when a real incident or a concrete change exposes a risk.
Locate the owning state and smallest input sequence, then reproduce it through
normal APIs with controlled port responses, a clock, or a necessary local process.
Check observable errors, preserved Evidence, allowed operational changes and,
where supported, retry idempotency and recovery. Avoid copying raw transcripts or
an entire `.gp` run into fixtures. Do not enumerate speculative fault combinations;
keep useful behavior coverage and consolidate redundant tests. See
[development rules](../AGENTS.md#故障分支测试增补).

Host changes require proportional evidence, not automatic ST execution. When
ST is not requested, record the unverified native behavior:

- worker launch, wait, continuation, or budget changes: fast adapter tests plus
  the matching real-host ST;
- hook/extension or `/goal-plus` lifecycle changes: host asset tests plus a
  native lifecycle ST;
- monitor feature projections: focused plugin tests plus the deterministic
  host-comparison example and terminal renderer test;
- planner or round changes: a multi-candidate/multi-round scenario;
- verifier/workspace changes: runtime integration tests that restore and
  re-verify exact candidate commits.

ST prompts end with a fenced `st_report` JSON block. The shared required fields
are `scenario`, `run_id`, candidate summaries, `selected_candidate_id`,
`best_score`, and `report_path`; scenario additions are documented in
`tests/st/prompts/_schema.md`.

The checked-in `fixtures/ultra_smoke_codex/` directory preserves source and a
sanitized evidence projection from one real Codex Goal Plus orchestration run.
It records model/effort, the native worker handle, event ordering, pytest
counts, and the local transcript digest. Raw `.gp` state and transcript content
remain untracked.

## Layout

```text
tests/
  test_*.py                 # default unit/integration/asset tests
  test_examples.py            # lightweight user-facing example guards
  _runtime_helpers.py         # project/spec builders and the in-process hook runner
  fixtures/                 # shared source workspaces
  st/
    conftest.py             # host preflight and prompt loading
    hosts.py                # marker-to-runner mapping
    helpers/                # Codex runner and report parser
    prompts/                # scenario contracts
    test_st_*.py            # real host-worker scenarios
  st_pi/
    conftest.py             # native Pi preflight
    test_goal_plus_pi.py    # real Pi command/TUI lifecycle
  st_thinkthread/
    conftest.py             # real Runtime/Profile preflight and Root cleanup
    driver.py               # Root-side ABI and Goal Plus scenario driver
    test_linux_v3.py        # serial Pi + ThinkThread V3 system tests
```

ST preflight checks the selected host binary, `goal-plus`, MCP configuration,
fixtures, and the nested-ST guard. Host subprocesses set
`GOAL_PLUS_ST_ACTIVE=<scenario>` so an agent cannot recursively start ST from
inside a running scenario.

## Failure Logs

System tests write complete host output under:

```text
.tmp/st-logs/<test-node>/<scenario>.log
```

Search evidence remains under `.gp/runs/`; host-specific logs are under
`.gp/host-logs/`. Use [runtime debugging](../docs/debugging-runtime.md) to
correlate `run_id`, `candidate_id`, and `agent_session_id`.

## Pytest Configuration

`pytest.ini` defines markers. `tests/conftest.py` skips `integration` and
`example` unless their marker is selected, and gates every `st` / `st_*` case
behind `--run-system-tests`. This also covers tests outside the ST directories.
With the flag, nested ST and xdist workers are rejected before host preflight.
The per-host conftests check prerequisites after pytest applies `-m`/`-k`.
Keep marker registration, this page, and scenario files in sync whenever an
ST is added, renamed, or removed.
