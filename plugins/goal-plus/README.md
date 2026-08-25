# Goal Plus (GP)

English | [简体中文](README_zh.md)

Goal Plus is a host-neutral runtime for long-running agent work. Use
`/goal-plus` in Pi or `$goal-plus` in Codex to handle ordinary goals directly
and upgrade measurable optimization tasks to Search Mode: freeze the evaluation
contract, explore isolated candidates, and promote the best verifier-backed
result.

Pi and Codex are the supported host paths.

## Installation

### User installation

Obtain Muyuan from its current GitCode repository and enter the Goal Plus
source directory:

```bash
git clone git@gitcode.com:yiyanzhi_akane1/muyuan.git
cd muyuan/plugins/goal-plus
python -m pip install .
```

This installs the Goal Plus commands into the active local Python environment.
If that environment already provides `goal-plus`, no separate runtime install
is needed.

Install or remove the Pi package with Pi itself:

```bash
pi install .
pi remove .
```

Install or remove the Codex plugin with Codex itself:

```bash
codex plugin marketplace add .
codex plugin add goal-plus@goal-plus

codex plugin remove goal-plus@goal-plus
codex plugin marketplace remove goal-plus
```

After removing Goal Plus from every host, optionally remove it from that Python
environment with:

```bash
python -m pip uninstall -y goal-plus
```

Keep this source directory while either host is installed. Pi loads the local
package from it, and Codex retains it as the configured marketplace source.

On first Codex use, start a new thread, review and trust Goal Plus in `/hooks`,
then start one more fresh thread. Verify it with:

```bash
codex plugin list
goal-plus-codex-doctor --project-root .
```

### Development checkout

For editable development, enter the same plugin directory and install the
checkout with development dependencies:

```bash
cd muyuan/plugins/goal-plus
python -m pip install -e ".[dev]"
```

Pi can load the checkout directly:

```bash
pi install .
pi remove .
```

Codex uses the local refresh script while plugin assets are being edited:

```bash
python scripts/install_codex_plugin.py --skip-runtime-install
python scripts/install_codex_plugin.py --uninstall --keep-runtime
```

Run `python -m pip uninstall -y goal-plus` after neither development host needs
the editable runtime. Detailed development and update behavior belongs in
[Pi](docs/pi.md) and [Codex](docs/codex.md).

Start a goal in Pi:

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus Optimize p95 latency for two hours without changing correctness.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
```

The equivalent Codex entry uses the installed skill:

```text
$goal-plus Fix this bug and verify the test suite.
$goal-plus Optimize p95 latency for two hours without changing correctness.
$goal-plus mode=probe Check whether vectorization is viable.
$goal-plus mode=autonomous Deeply optimize the kernel.
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

One request starts an autonomous run. The agent decides whether Goal Mode is
enough or a frozen verifier makes parallel Search useful; entering Search does
not require an extra approval step. `mode=autonomous` (the default) gives
every initial candidate substantial, renewable same-workspace exploration leases;
`mode=probe` asks for short feasibility probes first. This exploration mode is
stored as guidance in the final line of `raw_goal`, not as a scheduler state.

## Hosts

| Host | Integration assets | Entry | Search worker path |
|---|---|---|---|
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
pi --model <provider/main-model> --thinking max
```

Then submit `$goal-plus <goal>` in Codex or `/goal-plus <goal>` in Pi. In both
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
| Lightweight Search progress UX and rollout plan | [Progress UX](docs/usability-progress.md) |
| Runtime and host logs | [Debugging](docs/debugging-runtime.md) |
| Checkout development, plugin refresh, and multi-developer isolation | [Development](docs/development.md) |
| Specs and runnable examples | [Examples](examples/README.md) |
| Tests and real-host evidence | [Tests](tests/README.md) |

Benchmark-specific tasks, evaluators, campaigns, and comparison evidence are
not part of this isolated runtime directory.
