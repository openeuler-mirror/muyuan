# Goal Plus (GP)

English | [简体中文](README_zh.md)

Goal Plus is a host-neutral runtime for long-running agent work. `/goal-plus`
handles ordinary goals directly and upgrades measurable optimization tasks to
Search Mode: freeze the evaluation contract, explore isolated candidates, and
promote the best verifier-backed result.

Pi and Codex are the supported host paths.

## Quick Start

Clone Muyuan and enter the isolated Goal Plus source directory:

```bash
git clone git@gitcode.com:yiyanzhi_akane1/muyuan.git
cd muyuan/plugins/goal-plus
```

### Codex plugin (recommended)

Install Goal Plus as an enabled local Codex plugin:

```bash
python scripts/install_codex_plugin.py
```

This is a real Codex plugin installation, not only a shell-environment setup.
The installer performs the complete local flow:

1. installs the `goal-plus` Python runtime editable from this checkout;
2. stages `.codex-plugin/plugin.json`, `skills/`, `hooks/hooks.json`, and
   `.mcp.json` under the persistent
   `${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus` marketplace;
3. registers the `goal-plus-local` marketplace when needed; and
4. installs or refreshes `goal-plus@goal-plus-local` with a new Codex
   cachebuster.

It does not add marketplace metadata to Muyuan or require a project-local
`.codex/config.toml`. The published plugin assets are copied into the stable
marketplace, while the Python runtime remains an editable installation backed
by this checkout; keep the checkout at its installed path.

For first use:

1. start a new Codex thread so the plugin is discovered;
2. open `/hooks`, review Goal Plus, and trust all six lifecycle hooks; and
3. start another fresh thread so every hook is active from `SessionStart`.

Verify the installed plugin from the workspace where it will run:

```bash
codex plugin list
python -m goal_plus.codex_doctor --project-root .
```

Before hook review, the doctor is expected to report only the plugin hook trust
check as not ready. After pulling a new Goal Plus revision, rerun the same
installer command; it replaces only its managed staging copy, applies one fresh
cachebuster, and reinstalls the plugin:

```bash
python scripts/install_codex_plugin.py
```

### Pi and project-local development

Pi uses the checkout's `.pi/` assets. For Pi development or the older
project-local Codex flow, install the development dependencies explicitly:

```bash
python -m pip install -e ".[dev]"
```

For project-local Codex, also copy `.codex/config.example.toml` to the ignored
`.codex/config.toml`, start Codex from this `goal-plus/` directory, trust the
project-local hooks, and use a new thread. This remains useful while editing
Codex assets, but it is not a `codex plugin` installation.

Every host launches the same stdio MCP server:

```text
goal-plus --root .gp
```

Then start a goal in the host:

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus Optimize p95 latency for two hours without changing correctness.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
```

Codex and Pi also expose:

```text
/goal-plus edit <full revised goal>
/goal-plus resume
/goal-plus-with-final-check <goal>
```

One request starts an autonomous run. The agent decides whether Goal Mode is
enough or a frozen verifier makes parallel Search useful; entering Search does
not require an extra approval step. `mode=autonomous` (the default) gives
every initial candidate substantial, renewable same-workspace exploration leases;
`mode=probe` asks for short feasibility probes first. This exploration mode is
stored as guidance in the final line of `raw_goal`, not as a scheduler state.

## Hosts

| Host | Project assets | Entry | Search worker path |
|---|---|---|---|
| Pi | `.pi/` | `/goal-plus` or `pi -p "/goal-plus ..."` | durable Pi RPC pool; see [Pi](docs/pi.md) |
| Codex | `.codex/` | `goal-plus` skill or `/goal-plus` prompt | fixed parallel loops with native same-worker continuation; Codex 0.144.1+ hooks cover `UserPromptSubmit`, `PreToolUse`, and `SubagentStop`; see [Codex](docs/codex.md) |

For project-local Codex, copy `.codex/config.example.toml` to the ignored local
`.codex/config.toml`. An installed plugin instead loads top-level `skills/`,
`hooks/hooks.json`, and `.mcp.json`; these are kept aligned with the
project-local surface by asset tests. Codex plugins do not publish the
project-local `.codex/agents/` roles: Search worker messages therefore use the
built-in `default` role, while required final checks prefer the custom role
when the current `spawn_agent` schema exposes it and otherwise use the
self-contained `default` fallback. Host differences and strategy coverage are
summarized in [Agent Host Adapters](docs/agent-host-adapters.md).

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
|---|---|
| Architecture, shared Evidence, rollback, and end-to-end flow | [Shared Plane](docs/shared-plane.md) |
| Current MCP and Pi-local tools | [API](docs/api.md) |
| Host capability comparison | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Ultra work orchestration | [Ultra Orchestration](docs/ultra.md) |
| Optional monitor projections | [Monitor Feature Plugins](docs/feature-plugins.md) |
| Runtime and host logs | [Debugging](docs/debugging-runtime.md) |
| Specs and runnable examples | [Examples](examples/README.md) |
| Tests and real-host evidence | [Tests](tests/README.md) |

Benchmark-specific tasks, evaluators, campaigns, and comparison evidence are
not part of this isolated runtime directory.

## Development

```bash
python -m pytest -q
goal-plus --version
python scripts/sync_codex_plugin_assets.py --check
python -m goal_plus.codex_doctor --project-root .
```

The maintained strategy set for Pi and Codex is `agent_guided`
(`agent`/`default`) and `random` (`random_mode`).
