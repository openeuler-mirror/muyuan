# Before Execution: Detect and Configure

Use for a new task or an authorized environment change. For a retained failed task, start with
[diagnosis](diagnose.md) instead. Use the task's declared environment requirements.
No model or device run is needed merely to inspect configuration.

## Preparation scope and blockers

Distinguish an inspection request from a configuration request. For configuration, complete the
authorized reversible setup and validate the effective result; listing proposed changes alone is
not completion. Starting a Root/Child, model or service remains a separate execution step.

Classify a blocker by the action that actually depends on it. Missing verifier device wiring blocks
device-backed verification, not independent overlay edits, input-path checks or bounded host
dependency probes. Diagnose that boundary and continue unaffected preparation in the same task.
Do not let the prepare/diagnose cross-reference turn a downstream failure into a global stop.
If a setup action itself requires an unresolved path, grant or cross-task resource decision,
record that dependency and complete the remaining independent actions.

Separate configuration applied/validated, host dependencies tested, TT runtime checks pending,
and blocked execution steps. A configuration can be ready for runtime checks while deployment is
still blocked. An inspection can be complete while requested configuration remains unfinished.

## 1. Identify the task and execution surface

Read the benchmark's own task inputs and workspace instructions. Record host/platform, source
workspace, requested provider, models, authorized devices, delivery mode and whether work is new
or retained. A direct benchmark uses its own dependencies and acceptance criteria; do not inherit
Playground deployment, model-service, device, or container defaults.
Preserve provider choice and inspect the actual launch route; a task requesting TT does not prove
that its launcher starts a TT Root.

Exit: target and scope are known. An installation-only request does not enter business execution.
For an intentionally non-TT provider, use only applicable environment guidance and its normal task
workflow; do not introduce TT probes or convert the provider as part of setup.

## 2. Inspect the binding and required capabilities

Use [binding inspection](../capabilities/binding.md#inspect-and-install-the-binding) to compare
registration, Profile/SDK and actual host identity when a host already exists. Select only task
requirements from [the capability map](../capabilities.md), including role and model delegation.
Distinguish a missing executable/configuration, unsupported integration, and unverified runtime.

Read any linked follow-up repair/install receipt available with the report. Bind each finding to
its build and workspace: a later remote installation may differ from the local checkout. Recheck
the installed implementation before carrying a historical defect forward. A repair receipt or
passing protocol tests does not establish real TT execution on the updated build.

Exit: each required capability has evidence or a named blocker. Do not infer support from a version
label, silently change models/providers, or reinstall a working task backend to repair TT wiring.

## 3. Configure the three planes

Separate TT/host integration, Goal Plus contract and task environment using
[configuration ownership](../capabilities/configuration.md#separate-configuration-ownership).
Refresh only the selected managed integration when needed, then rebase the local workspace overlay.
Preserve managed role maps and session variables; add narrow dependency paths and task values.
Review a redacted diff, parse/apply atomically, and reread effective configuration.

Use [effective destination checks](../capabilities/configuration.md#check-effective-destinations)
for dependency paths, inventory, MCP forwarding and shared-lock writes. Plan temporary outputs
with [isolated execution](../capabilities/execution.md#temporary-space-and-results).
For state-root placement, shared-lock permissions and role grants, use
[state and access boundaries](../capabilities/state-and-access.md).
Inventory registration is configuration, not a device allocation; do not pin device visibility in
the Profile to compensate for a missing allocator. Keep unrelated prepared changes even when one
item remains blocked, provided the effective configuration is valid and its limits are recorded.

Resolve the actual task interpreter, activation script and imports with
[toolchain guidance](../capabilities/execution.md#establish-the-toolchain). Activation itself runs
code; inspect it first and perform only checks authorized for preparation.

Separate metadata inspection from bounded dynamic host preflight. Reuse an existing task
probe to test the actual imports/native libraries without model loading, accelerator allocation or
service startup when that is within setup scope. Record sandbox/host/TT as different execution
surfaces: an outer sandbox permission failure does not diagnose TT. Use the supported permission
path when already authorized, or report the unavailable host check; do not broaden grants blindly.

Exit: configuration provenance, rollback source, readable inputs and writable outputs are known.
Keep a retained session's existing configuration; new bindings require their supported launch path.

## 4. Design verification and the probe plan

For Search, prepare metric/input cases, protected verifier/reference inputs, edit surface, budgets,
resources, and promotion mode. Read `goal_plus_get_tool_schema` from the authorized Main surface
when available; defer unresolved schema checks until host startup rather than guessing fields.
Use the task's existing correctness and cleanup requirements.

Use [isolated execution](../capabilities/execution.md) to plan interpreter, device/lock mapping,
temporary output, export and complete time budget. Identify checks needed at origin, candidate and
promotion; plan probes through the actual execution path, not just the outer shell.
Select the minimum [probe level](execute.md#choose-a-probe). Cold-start budget and required resource
ownership must be plausible before expensive execution; hardware support can remain unverified.

When migrating from another provider, inspect the selected SDK/runtime's accepted timeout and
output limits as well as the verifier budget. A formerly valid direct-run timeout may be rejected
before TT execution. Keep its old timings as estimates only; a smaller configuration value does
not prove cold start, requests, cleanup and export now fit. Static limit inspection needs no Goal.

Exit: an executable verification plan with explicit pending checks. Installation/configuration
success is not evidence that a model, Child or service has run.

## 5. Hand off or stop at the requested boundary

Summarize actual binding, configuration changes, checks passed, unresolved capabilities, and the
next authorized probe with its expected output and cleanup. Reuse existing task records for this
summary; no mandatory new state database or report format is needed.

For each requested configuration item, state what was applied and checked, or the exact dependency
preventing it. Distinguish pending runtime observation from an observed failure. Include the
selected effective Profile/host config, rollback reference, host preflight results and the earliest
blocked execution step. Avoid an undifferentiated "prepare complete / blocked" result that hides
unapplied setup. Resource-idle observations have a timestamp, not reservation authority.
Continue to [execution](execute.md) only within task scope. Route a failed prerequisite to
[diagnosis](diagnose.md), retaining its failure phase and exact identity.
