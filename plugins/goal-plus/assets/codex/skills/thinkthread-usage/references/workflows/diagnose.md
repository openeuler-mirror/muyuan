# Recheck, Diagnose, and Recover

Use on failure in any phase, when resuming retained work, or after execution. Load only the failing
capability. Do not wait for the task to end before investigating a blocker.
Successful work takes the short path below.

During preparation, scope the stop to dependent actions. After identifying an execution blocker,
return to [independent setup](prepare.md#preparation-scope-and-blockers) and complete authorized
configuration and host checks that do not require the failed capability. For a not-yet-started
task, record runtime identities as not created; do not invoke Goal recovery tools without a binding.

## Successful closeout

Verify the delivered artifact/revision, required Evidence and apply/publication result. Confirm
task-owned resource cleanup and actual Goal terminal state through public tools. Finish
[session/scope cleanup](../capabilities/sessions.md#choose-the-lifecycle-operation), then report
results, evidence paths and any residual limitation. A passing request with unknown cleanup is not
successful closeout; use the failure path.

## 1. Preserve and recheck the original operation

Record build, Root/native session, state root, Goal/run/candidate, call/request and snapshot when
present. Preserve exact errors, bounded output and task-owned resource identities. Avoid dumping
credentials or full environments. Use [public observation](../capabilities/observation.md) before
repeating anything: a caller timeout does not establish execution failure or release of ownership.

Exit: known current state and unresolved facts. Pause dependent work while completion is uncertain;
do not rebuild a workspace, create a replacement Root or remove leases as an observation shortcut.

## 2. Locate the failing layer

Use the six-category [symptom index](../troubleshooting.md). Separate TT/host wiring, Goal Plus
integration and task environment from the business verifier's own result. Compare the earliest
failed boundary with a working one: outer shell, Root, Child, snapshot invocation, or service.
Check actual imports/cwd/env in that layer instead of inferring from container metadata.

Exit: a bounded hypothesis supported by observations. A warning present in a working startup,
a package version difference, or a direct-run score alone does not establish the root cause.

## 3. Reproduce with the smallest relevant probe

Choose from [execution probes](execute.md#choose-a-probe). Preserve source/snapshot, command,
interpreter and relevant inputs while isolating the suspected boundary. Reuse existing diagnostic
scripts before writing another observer. Do not repeat a model cold start to diagnose an SDK or
temporary-path failure. Preserve a valid existing result rather than running probes unnecessarily.

Exit: observed failure, explicit inability to reproduce, or a named missing diagnostic capability.
Record the comparison and its limits; protocol tests do not replace real-host/device evidence.

## 4. Choose the responsible repair path

| Finding | Action | Return condition |
| --- | --- | --- |
| Missing/stale task or host configuration | Apply the narrow authorized configuration repair through [preparation](prepare.md) | Same failing boundary passes with intended values |
| Invocation, role or lifecycle misuse | Correct the supported call/role and reconcile the existing operation | Original identity has a known result; no duplicate work |
| Goal Plus or TT implementation gap | Record build, minimal reproduction, expected/actual behavior and owning capability; develop a source fix only within scope | Traced new build passes targeted checks and is bound through supported installation/recovery |
| Defective frozen verifier contract | Follow [run invalidation and replacement](../capabilities/observation.md#replace-a-defective-verifier-contract) | Old work reconciled; repaired linked run admitted |
| Valid business failure | Apply only task-authorized candidate changes or report the result | Frozen acceptance criteria stay independent and satisfied |
| Insufficient evidence or unknown ownership | Preserve state and report the exact missing fact | Supported observation/recovery establishes it |

For an interrupted native session, follow
[readiness and request recovery](../capabilities/observation.md).
Do not edit an installed release or private Goal state to make a gate disappear.

## 5. Recheck and rejoin the workflow

Repeat the previously failing check with the repaired binding, then necessary dependent checks.
Confirm prior requests, services and locks no longer have unresolved owners. Rejoin the precise
preparation/execution step that was blocked; do not restart all business work by default.

Report confirmed cause versus hypothesis, repair and build/config provenance, checks performed,
evidence locations, unresolved resources and the next supported action. Keep direct diagnostics,
formal candidate Evidence, promotion and final replay distinct. A runtime fix outside current
scope remains a clear handoff, not a skill-level claim that the capability now works.
