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

For Pi×TT failures, classify the earliest boundary before retrying:

| Earliest evidence | Owner | Interpretation |
| --- | --- | --- |
| `tt run` rejects `--model`, or `native-main.log` reports an invalid Pi config directory | TT host/Profile | Fix exact provider/model or inherited config path; no Root/Goal was started |
| Root reaches ready but no Goal state exists | Pi/extension/host entrypoint | Inspect extension loading, PTY, MCP and prompt submission |
| Child fails before the dispatch message and the error names manifest/session identity | Goal Plus bootstrap | Schema discovery must work with schema-only identity; real calls still need candidate identity |
| Freeze admission rejects duplicated `source_path` or stale SearchSpec fields | Goal Plus MCP schema/LLM payload | Read the live tool schema, send the complete SearchSpec under `spec`, and omit compatibility fields unless the live schema requires them |
| Goal/run/candidate state exists and provider receipt is missing | Goal Plus dispatch/provider adapter | Reconcile the persisted target and provider journal; do not blindly resend an unknown operation |
| Goal is active, repeated provider `server_error`/overloaded appears, and no new receipt arrives | Provider transport/capacity | Preserve the original request, stop dependent retries, and report the provider boundary; a longer outer watchdog does not make an unknown request safe to resend |
| Candidate Child reports AutoResearch-Git settled-ledger mismatch | Inner Git continuation/settlement | Inspect the child's retained filesystem and attempt/settlement records; distinguish an unresolved attempt from a provider startup failure before choosing recovery |
| Goal state is complete but artifact, promotion or cleanup is wrong | Goal Plus/task contract | Diagnose settlement, publication and cleanup independently of TT startup |

Run one pure Pi×TT Root/Child probe when the first two layers are uncertain. It must use the same
Profile, exact model/provider, and isolated state root while bypassing Goal Plus. A passing probe
proves the TT runtime, Pi and provider path can start; it does not prove Goal Plus identity,
dispatch, settlement or publication. Conversely, a failure before Root readiness is not evidence
of a fork/fresh defect.

Native Circle ST cases share pytest's temporary workspace naming. Run bounded and fork one after the
other, and clean/reconcile the previous TT state before the next case. A concurrent pair can fail
with `destination already exists` or PTY `EIO` without exercising the product lifecycle.

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
