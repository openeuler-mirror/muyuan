# Tests

Default tests are fast and host-free. Module APIs and controlled intermediate
results cover business behavior without launching agents. Real-host system
tests (ST), including full E2E, run only when explicitly requested by the developer;
a code change or phase boundary is not an automatic ST trigger.

## Checkout setup

From `plugins/goal-plus/`, prepare the locked Python 3.12 checkout environment
before running tests:

```bash
uv sync --locked
uv run --frozen python -m pytest -q
```

The commands below assume that this checkout's `.venv` is active; otherwise
prefix them with `uv run --frozen`. The `dev` dependency group is included by
`uv sync`. The test gate rejects a different Python version or a test runner
outside the checkout environment. See [Development](../docs/development.md#测试环境).
For a smaller machine, override the default 32 workers, for example with
`uv run --frozen python -m pytest -n 2 -q`.

## Test Layers

安装/卸载实测使用独立 Codex、Pi、runtime 和 XDG 配置目录，不读取认证或调用模型。
它覆盖首次安装、MCP 握手、Pi 扩展加载、取消/确认重装、分宿主卸载和 runtime 清理：

```bash
python -m pytest --run-system-tests -n 0 -q tests/test_installation_system.py
```

前置条件为可执行的 Codex、npm 安装的 Pi、Node.js 和 uv；首次运行需要下载受管 Python/依赖。

| Layer | Command | Proves |
| --- | --- | --- |
| Default fast gate | `pytest -q` | models, runtimes, workspaces, verifiers, APIs, Codex/Pi assets |
| Integration slice | `pytest -m integration -q` | packaged wheel assets and shared MCP freeze/plan/verify/select/publication with controlled edits and a real verifier; no model calls |
| Example slice | `pytest -m example -q` | `examples/*` fixtures drive real generated assets |
| Codex fast slice | `pytest -m codex -q` | Codex adapter, hooks, assets, native session contract |
| Pi fast slice | `pytest -m pi -q` | Pi extension, native protocol, metrics, assets |
| Progress UI slice | `pytest -m ui -q` | Pi progress widget (5s refresh/hide boundaries with Node mock timers) and the hidden Main continuation |
| Runtime-focused | `pytest tests/test_runtime_unit.py` | Search state machine without a host |
| LLM Verifier-focused | `pytest tests/test_llm_verifier.py -q` | task lifecycle, immutable records, structured output, prompt binding, and FIFO terminal delivery |
| Adaptive Search-focused | `pytest tests/adaptive_search -q` | deterministic allocation, record-driven snapshot, allocation recovery, one-shot steering, diagnostic actions, shadow and atomic limits |
| Real-host ST | `pytest --run-system-tests -n 0 -m "(st and (st_codex or st_pi_rpc or st_pi_thinkthread)) or st_pi" -v -s` | maintained native launch, hooks/events, worker lifecycle, final evidence |

Default tests must never launch Codex or Pi. A host behavior claim requires
the matching ST; if it cannot run, report that gap.

### Per-module model probes

Each model-backed module has a standalone ST that exercises its real host path
without a full Goal Plus run; select the Pi model with `ST_PI_MODEL`
(`provider/model`, e.g. `zai/glm-5.3-flash`):

| Module | Command |
| --- | --- |
| Native session (RPC driver, two turns) | `ST_PI_MODEL=... pytest --run-system-tests -n 0 -q tests/st/test_st_native_session.py -k "pi"` |
| Annotation + LLM Verifier (structured inference) | `ST_PI_MODEL=... pytest --run-system-tests -n 0 -q tests/st/test_st_internal_roles.py -k "pi"` |
| Product candidate session host (continuation/recovery) | `ST_PI_MODEL=... pytest --run-system-tests -n 0 -q tests/st/test_st_session_host.py -k "pi"` |

The removed native pool/watchdog workflows are covered by shared owners:
`test_st_session_host.py` checks explicit continuation and recovery,
`test_st_unified_main.py` checks Main's two-candidate Circle workflow, and
`test_candidate_delivery.py` checks Evidence reuse and missing verification.
Time advisories remain covered by `test_time_advisory.py` and Codex hook tests;
the old ST that treated an expired deadline as informational is removed and
does not establish native lifecycle coverage for the current deadline contract.
Native Pi ST uses the installed package by default; use `ST_PI_BINARY` to select
a prepared `pi-goal-plus` development launcher. It does not inject the checkout
or pytest's Python into the host. ThinkThread ST copies only its test harness into
the disposable workspace and imports the Profile's installed Python package.
The suite excludes Torch and AscendC scenarios, including their asset and
hardware tests.

The Codex ThinkThread bridge probe uses the managed release referenced by the
installed `goal-plus-pi` Profile and creates a disposable native Codex Profile.
It checks two retained Child turns through the real MCP/Message bridge and
resource release, without enabling Codex Search admission:

```bash
python -m pytest --run-system-tests -n 0 -s -q tests/st_thinkthread/test_codex_bridge.py
```

`ST_CODEX_PROFILE=<name>` overlays the user's `NAME.config.toml` Codex profile
layer (model, provider, reasoning effort, and model catalog) so the bridge probe
and the circle acceptance can run a non-default model such as DeepSeek without
changing the base Codex config; set `CODEX_HOME` when the real config home is
not the default. The catalog and system CA bundles are projected into the
sandbox read allow-list automatically.

The probe itself still runs two retained Child turns without enabling Codex
Search admission. Codex ThinkThread Search admission is supported and covered by
the reduced circle acceptance in `tests/st_thinkthread/test_codex_circle_main.py`;
see [Codex support scope](../docs/codex.md#支持范围与验证) for the verified scope
and remaining gaps.

ST requires `--run-system-tests`; `-m`, `-k`, and direct node selection alone
never authorize a host launch or preflight. Keep the flag out of default
configuration, installers, and automatic development loops. Collection-only
does not probe hosts, even with the flag. Preflight runs only for selected hosts.
With the `dev` dependency group installed, the default gate uses 32 workers. On smaller
machines, override this with `pytest -n 2 --dist=load -q` (or another count no
greater than half the available CPUs). Run real-host ST with `-n 0` or
`-o addopts=''` so host processes, model calls, and machine resources do not
interfere with one another.

`integration` and `example` tests are skipped by default via
`tests/conftest.py`. Add a marker name to `-m` only when intentionally auditing
that slice.

## Execution Coverage

GP has no Linux/macOS process identity adapters or guardian. Scope tests verify
session/activation admission and explicit receipt release without PID inspection.
Drainer tests verify single-flight generations and explicit release, including stale
consumer rejection. Direct-handle tests cover timeout, bounded output, and closure
without signalling unrelated processes. No process-tree cleanup guarantee is tested
or claimed. Native lifecycle coverage still belongs to the corresponding host ST.

## Hook Tests

Default hook tests drive `goal_plus.goal_plus_stop_hook.main()` in-process via
`run_host_hook_inprocess` in `tests/_runtime_helpers.py`: cwd, stdin/stdout/stderr
and the environment are restored after each call, including failures. The returned
`HookResult` (`returncode` / `stdout` / `stderr`) asserts like a
`subprocess.CompletedProcess`. Do not spawn one interpreter per hook event —
each cold start costs about a second of imports, and per-event subprocess calls
previously dominated total suite time. Default tests also never launch a real
model; native execution uses controlled transport/process fixtures. Goal model
admission is separately checked to avoid provider subprocess calls.

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
| `st_pi_rpc` | common session host + Pi RPC | host default or `ST_PI_MODEL` |
| `st_pi` | native Pi `/goal-plus` print/TUI | host default or `ST_PI_MODEL` |
| `st_pi_thinkthread` | installed `goal-plus-pi` ThinkThread Profile | Profile default Pi model |

Every `tests/st/` case has `st` plus exactly one host marker. Native Pi command
tests live in `tests/st_pi/`. `-s` is required so failure log paths remain
visible.

## Common Real-Host Checks

The default candidate launcher uses the same MCP run/wait/close contract
for both hosts. Its ST verifies two edits, exact Evidence, native hook binding
(Codex with an isolated current installation), and scope/process cleanup:

```bash
python -m pytest --run-system-tests -n 0 -s -q tests/st/test_st_session_host.py
```

The `timeout_resume` case verifies that a new call resumes the same native session
after a timed-out process is closed, preserves a conversation-only token and
settled Evidence, and finishes both process resources. Its Pi case uses the local
Pi account configuration with the existing checkout development environment.
It does not install a package or change user configuration.

`test_st_unified_main.py` separately exercises native Main with no GP control
skills installed. Ordinary Goal completion does not establish Search orchestration
or post-compaction visibility; these need their own execution evidence.
Its Pi `circle_example_plain`, `circle_example_llm`, and `circle_example_adaptive`
cases read the public Git apply prompts verbatim and retain their 300-second Goal
budgets, with a 1200-second external watchdog. The separate
`circle_example_replacement` case freezes a 900-second Goal, two lanes, three total
candidates, and `swe_search_uct/v1`; it requires an applied
Decision and a derived native candidate with real Evidence. Run each case serially
with `--run-system-tests -n 0 -s`. Build, session, scoring, selection, publication
and cleanup evidence is written to the test log directory.
For Pi, this test uses checkout assets by default; set `ST_PI_PACKAGE` to the
immutable package path reported by `./install.sh --runtime-info` to verify a
normal installed build with isolated native configuration.
Pi isolation copies `auth.json`, `models.json` and the native `models-store.json`
catalog cache when present, so Main and RPC discovery see the same model inventory.
These files stay in the temporary private configuration and are never committed.
`test_st_session_compaction.py` explicitly compacts a short native Main conversation,
then checks retained instructions and a real status tool call. Its isolated Pi
configuration keeps a short recent window; user settings are not changed.

`test_st_final_check_host.py` runs a real independent checker through the common
MCP launcher on both hosts. The `final_check` cases in `test_st_unified_main.py`
also verify native Main discovers this path with the old control skills disabled.
Identity, stale activation, missing verdict and timeout cleanup use controlled
processes in `test_final_check_host.py`.

The Pi loading probe uses a disposable extension, consumes the real Goal Plus MCP
schema, and forwards a read-only call without inference. It closes its process;
native two-turn behavior belongs to `test_st_native_session.py`, which exercises
the product session adapters and scope registration.

```bash
python -m pytest --run-system-tests -n 0 -s -q tests/st/test_st_session_control.py
```

The Pi probe uses `ST_PI_BINARY` or `pi` and a 60-second bound. It does not install
assets or switch the production execution mechanism.
`ST_SESSION_PROBE_TIMEOUT` still controls the product native-session and candidate
session-host ST bounds (180 seconds by default).

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

# Shared candidate recovery with fresh session identity and retained Evidence
pytest --run-system-tests -n 0 -s -q tests/st/test_st_session_host.py -k cold_recovery

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

# Codex revises one goal after result 1, refreezes, and completes result 2
pytest --run-system-tests -n 0 -m "st and st_codex" -k goal_plus_spec_revision -v -s -rs

# Shared session return, explicit continuation and cleanup on both native hosts
pytest --run-system-tests -n 0 -s -q tests/st/test_st_session_host.py -k same_session

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

### Codex Verifier Wait

Run the verifier module ST with explicit authorization:

```bash
ST_CODEX_REASONING_EFFORT=low \
  pytest --run-system-tests -n 0 tests/st/test_st_codex_verifier_wait.py -v -s --tb=short
```

The controller prepares one candidate through normal APIs and launches one native Codex worker
against the checkout's MCP server with a test-local 60-second client timeout and 30-second queries.
The production configuration is unchanged. A controller-owned resource lock holds the submitted
attempt beyond the test client's timeout; native tool events must show a
`pending` response, readable attempt identity, and rejection of one deliberate duplicate submission.
The worker then uses `goal_plus_search_run_verifier` to await that exact attempt without reloading
context.
The controller then changes the original workspace and releases the lock. The test checks exact
snapshot scoring, one Evidence entry, preservation of the later edit, worker observation of
`needs_recovery`, and same-attempt retry after native process exit, preserving the later edit.
The shared MCP integration test also interrupts a real verifier controller while queued and while
executing, checks inherited execution locks, and resumes the exact snapshot without checkpoint rollback.

This is a module ST, not an optimization E2E. Installed plugins/hooks are disabled only for the
test worker, so it does not validate plugin discovery, Main orchestration, or native child hooks.
It uses the configured Codex provider and `ST_CODEX_MODEL` (default `gpt-5.6-terra`), usually takes
a few minutes, and writes native JSONL under the ST scratch log directory. The test requires the development
Python environment and its console scripts on `PATH`; no host installation is changed.

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
case does not cover peer comparison or LLM Verifier/Adaptive Allocation.

`ST_PI_MODEL` and `ST_PI_THINKING` now also apply to the TT fixture through native
Pi arguments, inherited by Children. `ST_THINKTHREAD_PRESERVE_ON_FAILURE=1`
preserves Runtime state after stopping Roots (also on success). Leave it unset
for routine checks so teardown stops and removes the fixture's own Roots.
For diagnostics, use a unique `--basetemp` under this plugin's `.tmp/`; keep the
Runtime state outside the workspace granted to an Agent. After inspection, remove
the test instances with `tt rm`, retain only the needed reports under `.tmp/`, and
delete temporary workspaces and credential copies. Do not leave test directories
in the checkout's parent directory.

### Circle Role Checks

The `circle_sandbox` cases in `st/test_st_unified_main.py` reuse the
`examples/circle-packing-git-apply` source preparation and public verifier.
Two real candidates attempt outside/source/symlink/child writes before their
Circle edits; workspace writes must succeed and outside writes must return an
OS permission error. Acceptance also requires three verified deliveries,
same-session continuation, artifact-only promotion, unchanged source, reports,
and closed scopes. This tests Search candidates; Pi Main remains a host process.
The same Pi case runs on Linux (Bubblewrap) and macOS (Seatbelt); there is no
separate macOS workflow. The shared `test_sandbox_launch.py` suite also executes
real write/read-only, symlink/child, temporary-directory fallback and cleanup
checks on either OS. Protected structured-inference tests run on both OSes.

Run against available models and a prepared Pi development launcher:

```bash
ST_CODEX_MODEL="$CODEX_TEST_MODEL" uv run pytest --run-system-tests -o addopts='' \
  'tests/st/test_st_unified_main.py::test_native_main_without_control_skills[codex-circle_sandbox]' -v -s

ST_PI_MODEL="$PI_TEST_MODEL" \
ST_PI_BINARY=/path/to/pi-dev/bin/pi-goal-plus \
GOAL_PLUS_PYTHON=/path/to/pi-dev/venv/bin/python \
  uv run pytest --run-system-tests -o addopts='' \
  'tests/st/test_st_unified_main.py::test_native_main_without_control_skills[pi-circle_sandbox]' -v -s
```

Set `CODEX_TEST_MODEL` and `PI_TEST_MODEL` to models available in the selected
hosts. Dependencies, writable-root limits and Pi development interpreter paths
are documented in [Agent sandbox](../docs/sandbox.md). Codex uses a
disposable installed plugin/config through the existing installation fixture;
user host registrations remain untouched.

With explicit developer authorization, Annotation and LLM Verifier can run on the
selected Codex/Pi models without a native Main or candidate inference:

```bash
ST_PI_MODEL=zai/glm-5.3-flash ST_PI_THINKING=low \
  pytest --run-system-tests -n 0 tests/st/test_st_internal_roles.py -v -s -rs
```

This test prepares a fresh Circle Git source, settles one deterministic change
through the normal verifier, and drives real role adapters through claim,
context, completion and metric consumption. Both processes register in a real
scope owned by pytest; cleanup runs on failure too. It verifies hard Evidence
and exact artifacts remain unchanged. It does not cover native Main, candidate
inference, detached drainer wakeups, deterministic allocation or TT Child recovery.
`ST_ROLE_TIMEOUT` bounds Annotation (default 180 seconds); LLM Verifier uses its
fixed 900-second operational timeout.

The former Pi pool workflow ST has been removed. Its native two-turn and recovery
coverage now lives in `st/test_st_session_host.py`; Main orchestration uses
`st/test_st_unified_main.py`. TT candidate lifecycle uses its own fixtures;
`st_thinkthread/test_circle_main.py` separately covers the Pi TT native Main
Circle workflow; `st_thinkthread/test_codex_circle_main.py` covers the Codex TT
native Main path. They use the installed Profile and their documented overrides.

The TT Circle test has three prompt variants: `[bounded]` checks exactly four
deliveries; `[example]` reads the public
[`pi-goal-prompt.md`](../examples/circle-packing-thinkthread-apply/pi-goal-prompt.md)
verbatim and follows its global stop contract. Both check exact snapshot Evidence,
Annotation, the highest verified score, publication/apply and cleanup. To run the
public example with GLM and collect tool-contract diagnostics:

```bash
ST_PI_MODEL=zai/glm-5.3-flash ST_THINKTHREAD_PRESERVE_ON_FAILURE=1 \
  pytest --run-system-tests -n 0 -s -q \
  'tests/st_thinkthread/test_circle_main.py::test_native_pi_main_completes_circle_thinkthread_apply[example]'
```

See [session analysis](../docs/debugging-runtime.md#分析工具描述与调用失败) for the
offline request-contract and tool-error report.
`[three_rounds]` requires at least three settled deliveries per retained Child,
with DEBUG and the session opt-out unset in the native launch. It verifies default
Main observations, incremental Child archives after cleanup, exact identities,
unique request IDs and offline tool-contract analysis, and writes
`session-diagnostics.json`. Select it instead of `[example]` in the command above.
The preserve flag retains stopped test instances for diagnosis (also on success);
without it the fixture removes their session-private histories during teardown.
The public prompt may stop an unfinished continuation after another lane meets
the goal. Such a Message must be closed with no accepted attempt or fabricated
Evidence; the bounded variant still requires all four deliveries.
Autonomous improvements may fail edit-surface admission or the frozen verifier.
The public variant checks exact failed Evidence, zero score and restoration to a
prior valid iteration, then selects only the best valid result. A rejected attempt
need not have verifier metrics when admission prevented verifier execution.

The direct Circle native Main test uses [a bounded ST prompt](st_pi/circle-common-prompt.txt):
two candidates, one small change and one verifier call per dispatch, followed by
independent publication verification, apply and report. Its 900-second Goal budget
includes Main setup and closeout; `ST_PI_SEARCH_TIMEOUT` defaults to 1200 seconds
for that test's external watchdog. The user-facing optimization example is separate.

### Core Search Test Design

Verifier and Adaptive Search tests are organized around owner contracts, not
a catalog of speculative failures. The focused suite keeps one or two
representative cases per durable boundary:

| Owner | Stable behavior covered |
| --- | --- |
| Settlement + Verifier | current submission, immutable complete/failed record, FIFO terminal delivery, bounded local prompt |
| AdaptiveSearch + Snapshot | one terminal record appends one snapshot, no-policy path, exact lineage and rollback boundary |
| Policy + Algorithms | fixed policy version, deterministic ranking and compatible alternatives, UCT view and pruning |
| AllocationExecutor | durable Decision, run-lock cancellation/materialization/dispatch, ranked fallback, same-identity recovery, selected retirement fence |
| Host wiring | independently wakes Verifier, Annotation and Executor; no owner failure suppresses another |

Do not add a test merely for a hypothetical timeout, lease, callback ordering, or
host exception. Add a fault branch only when it changes one of these durable
contracts or reproduces an observed incident. Prefer one representative
recovery case over a matrix of transport-specific variants.

## Module Boundaries

Session unification tests follow the owning layer, not the former Pi workflow:

The actual Pi MCP client tests require Node and `npm ci --prefix assets/pi`.
The supported `./install.sh --pi-dev` prepares these locked dependencies too.
Tests do not download dependencies automatically; missing dependencies are reported as skipped.

| Layer | Tests | Contract |
| --- | --- | --- |
| Shared session | `test_native_session.py`, `test_session_rpc.py`, `test_session_control.py` | Repeated invocations, exact call identities and results, wait timeout without cancellation, interrupt/message/close, registration and bounded transport; the same cases exercise both adapters |
| Native adapters | `test_codex_session.py`, `test_pi_session.py`, `test_pi_mcp_connection.py` | Codex app-server callbacks and exact invocation identity; Pi settled/queue semantics and persistent standard MCP transport |
| Shared candidate use cases | `test_candidate_sessions.py`, `test_candidate_delivery.py` | Provenance preparation, continuation/refresh admission, exact Evidence reuse and verification decisions |
| Shared MCP flow | `test_shared_tool_contract.py`, `test_session_control_mcp.py`, `test_mcp_search_flow.py` | Actual schema/call, bound roles and identities, session control across client reconnects, two independent candidates through selection/publication |

The old Pi candidate pool/driver/workflow has been removed. Keep shared behavior
in its owner tests and thin Pi/Codex wiring tests for native protocols and callbacks;
do not restore obsolete step-order, workflow-name or private-state assertions.

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

Native Main Circle acceptance reads durable Goal, candidate, Evidence, publication
and scope state. It does not depend on a model-written `st_report` block or the
retired four-session workflow. See [prompt contracts](st/prompts/README.md).
Other module STs validate their own typed results and native observations.

The checked-in `fixtures/ultra_smoke_codex/` directory preserves source and a
sanitized evidence projection from one real Codex Goal Plus orchestration run.
It records model/effort, the native worker handle, event ordering, pytest
counts, and the local transcript digest. Raw `.gp` state and transcript content
remain untracked.

## Layout

```text
tests/
  test_*.py                 # default unit/integration/asset tests
  # example guards live in the per-example test modules
  _runtime_helpers.py       # project/spec builders and the in-process hook runner
  _st_tmp.py                # scratch and evidence outside the checkout
  fixtures/                 # swe_bench_20212, signal_processing, ultra_smoke_codex
  st/
    conftest.py             # host preflight and prompt loading
    hosts.py                # marker-to-runner mapping
    helpers/               # native runner and durable Circle-state acceptance
    prompts/               # bounded Circle prompt and contract documentation
    test_st_*.py            # real host-worker scenarios
  st_pi/
    conftest.py             # native Pi preflight
    test_goal_plus_pi.py    # real Pi command/TUI lifecycle
    circle-common-prompt.txt # Pi Main Circle prompt
  st_thinkthread/
    conftest.py             # real Runtime/Profile preflight and Root cleanup
    driver.py               # Root-side ABI and Goal Plus scenario driver
    test_linux_v3.py        # serial Pi + ThinkThread V3 system tests
    unified_session_scenario.py # shared TT session lifecycle scenarios
    test_circle_main.py    # native Pi TT Main Circle
    test_codex_bridge.py   # diagnostic probe, not supported Codex TT Search
    test_vliw.py           # retained-turn VLIW validation
```

ST preflight checks the selected host binary, `goal-plus`, MCP configuration,
fixtures, and the nested-ST guard. Host subprocesses set
`GOAL_PLUS_ST_ACTIVE=<scenario>` so an agent cannot recursively start ST from
inside a running scenario.

## Failure Logs

System tests write complete host output under the ST scratch base, keyed by test
node:

```text
<st-scratch>/st-logs/<test-node>/<scenario>.log
```

`<st-scratch>` defaults to a directory *outside* the checkout and is set by
`GOAL_PLUS_ST_TMP`. Per-scenario run directories are removed once a test
finishes; failures keep theirs, and `GOAL_PLUS_TEST_DEBUG=1` keeps every run
for debugging. This replaces `GOAL_PLUS_ST_KEEP_RUNS`. See `tests/_st_tmp.py`
for the scratch location and the shared retention rules below.

Search evidence remains under `.gp/runs/`; host-specific logs are under
`.gp/host-logs/`. Use [runtime debugging](../docs/debugging-runtime.md) to
correlate `run_id`, `candidate_id`, and `agent_session_id`.

Verifier resource locks default to `.tmp/verifier-locks/` inside the checkout
(`tests/conftest.py`), not the machine-wide `/tmp/goal-plus-verifier-locks/`,
because on shared machines that `/tmp` directory belongs to whichever user
created it first and blocks everyone else. Export
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` to override.

## Pytest Configuration

`GOAL_PLUS_TEST_DEBUG` controls test artifact retention independently of the
runtime's `GOAL_PLUS_DEBUG` diagnostic output:

| Value | Ordinary `tmp_path` / `tmpdir` and ST run directories |
| --- | --- |
| Unset or `0` | Remove successful test directories; retain failures in setup, test body, or teardown |
| `1` | Retain successful and failed test directories for debugging |

Only the exact value `1` enables retention of successful tests. The value is read
once when pytest configures each process. Cleanup runs after all fixture teardown,
handles read-only test directories without following external symlinks, and emits
a pytest warning if removal fails. ST log directories remain available in both modes.
Ordinary fixtures receive a workspace inside a per-test container, so adjacent
runtime state created through `tmp_path.parent` follows the same retention policy.

Pytest still creates and rotates ordinary temporary session directories, keeping
the latest three numbered sessions by default (`tmp_path_retention_count`). Keep
`tmp_path_retention_policy=all`: the shared test hook handles per-test outcomes
after teardown, later than pytest's built-in `failed` policy. Debug mode does not
disable historical rotation. ST run directories live outside that rotation;
retained ST failures and debug runs need manual cleanup when no longer needed.
Forced termination can prevent cleanup. Direct `tempfile.TemporaryDirectory`
contexts retain their own lifecycle; this switch does not change runtime cleanup.

For filesystems with limited inodes, point `TMPDIR` (ordinary pytest directories)
and `GOAL_PLUS_ST_TMP` (ST scratch space) at an existing directory on a data disk.

`pytest.ini` defines markers. `tests/conftest.py` skips `integration` and
`example` unless their marker is selected, and gates every `st` / `st_*` case
behind `--run-system-tests`. This also covers tests outside the ST directories.
With the flag, nested ST and xdist workers are rejected before host preflight.
The per-host conftests check prerequisites after pytest applies `-m`/`-k`.
Keep marker registration, this page, and scenario files in sync whenever an
ST is added, renamed, or removed.
