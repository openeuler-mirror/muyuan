---
name: thinkthread-usage
description: >-
  Use for a Goal Plus benchmark or integration that selects workspace_provider=thinkthread,
  or explicitly asks to prepare, run, observe, diagnose, recover, or develop its ThinkThread
  path. Also use for Goal Plus reports of ThinkThread-linked Profile, Child, Session,
  Message, request, snapshot, or verifier failures. For PYPTO/BooM deployment workflows managed by
  Playground, prefer its scene skill; this skill covers the shared Goal Plus/TT contract.
---

# ThinkThread Usage for Goal Plus

Use this skill for Goal Plus benchmarks that use ThinkThread directly, such as CannBench tasks
that do not run through Playground, and for Goal Plus/ThinkThread adapter development. The skill is
source-only and is not included in a Goal Plus installation; selection by metadata requires the
host to discover this source skill. It does not supply benchmark-specific acceptance criteria.

For a Goal Plus command containing `workspace_provider=thinkthread`, confirm the public effective
`command_config` after the host starts. A mention in task prose, a directory name, or a process
alone does not establish the active provider. For an explicit environment request without a
running Goal, use the intended host and binding as the preparation target; do not invent a Goal.

For a real-host test, treat the host preflight as part of the evidence. Pytest system tests are
opt-in: a test reported as `skipped` because `--run-system-tests` was omitted has not run. Use
`uv run pytest -n 0 --run-system-tests ...` for the maintained ST entrypoints and keep the exact
model/provider value in the command and report.

Pi and ThinkThread require the exact `provider/model` spelling accepted by the selected Profile;
for the current Sub2API-backed Pi setup this is `sub2api-openai/gpt-6-luna`, not the bare
`gpt-6-luna`. A bare model fails in `tt run` before Root startup. Clear a stale inherited
`PI_CODING_AGENT_DIR` when it points at a missing or cross-checkout symlink, so the managed
Profile can use its declared `configDirDefault`; a Pi error about the config directory being
unreal or missing is a host configuration failure, not a Goal Plus or provider failure.

When a Goal Plus TT Child starts, the extension may request `host_mcp_manifest` before the
dispatch message has bound a candidate identity. That request is schema discovery and may use
the existing schema-only unbound identity. Actual candidate tool calls still require the exact
candidate session identity. A manifest succeeds while an unbound tool call returns
`GoalPlusSessionMismatch` by design; do not weaken the latter gate or classify this pair as a
provider failure.

## Choose the entry

| Intent or current state | Start here | Expected result |
| --- | --- | --- |
| Understand TT/Goal Plus capability or role support | [Capability guide](references/capabilities.md) | Native, integrated, authorized, and observed support |
| Prepare a direct benchmark or change its environment | [Before execution](references/workflows/prepare.md) | Checked binding, effective configuration, and pending probes |
| Run a prepared task or inspect live execution | [During execution](references/workflows/execute.md) | Actual Root/Child, request, and verifier evidence |
| A task failed, needs recovery, or has finished | [Recheck and diagnose](references/workflows/diagnose.md) | Verified outcome, recovery path, or bounded defect report |
| A specific symptom needs immediate lookup | [Symptom index](references/troubleshooting.md) | The smallest relevant check and its responsible layer |
| Develop or review the Goal Plus adapter | [Development guide](references/development.md) | Owning contract, code path, and validation plan |

Choose one entry and follow only its relevant capability links. A failure during preparation or
execution enters diagnosis at that boundary. Successful completion uses its short closeout path.
Adapter development starts in the development guide; read a runtime workflow only when reproducing
an observed behavior. For Playground PYPTO/BooM deployment work, use its environment/scene skill
for models, services, devices, and scenario diagnosis; use this guide only for a shared Goal Plus
contract question.

## Establish scope and ownership

Identify the host, source workspace, actual build, TT Profile/runtime, current role, task outcome,
and authorized resources. Preserve the user's provider, model, parallelism, and delivery choices.
An unknown binding is a missing fact, not permission to infer an identity from cwd.
Domain workflows, dependencies and business acceptance criteria come from the benchmark's own
instructions. This skill checks their TT environment and execution prerequisites, not their score.

Goal Plus owns Goal/Search state, immutable Evidence, and promotion. TT owns its Child/filesystem
execution. The host and task own their launched processes and device use. Use Goal Plus public
tools for its managed work; a diagnostic TT call must not become a second scheduler or bypass its
state ownership. Prepare shared environments from Main/setup, not from a candidate.

An inspect/build request does not authorize starting agents, services, or device workloads.
Apply routine configuration within existing authorization, showing a redacted diff first. Keep
credentials in host authentication channels and record only named, non-secret environment values.
Installing/upgrading TT or repairing runtime code requires that work to be within the user's task.

Report separately what was configured, actually executed, independently verified, and cleaned up.
A checkout is needed for development or installation refresh, not for ordinary task operation.
