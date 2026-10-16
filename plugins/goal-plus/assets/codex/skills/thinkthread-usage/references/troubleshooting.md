# Symptom Index

Select the symptom, perform its smallest check, then use the linked capability or workflow.
Preserve original identities before retrying. For the complete repair loop, use
[recheck and diagnosis](workflows/diagnose.md); do not read every category.

## Installation and binding

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| Missing/stale Profile or SDK | Compare selected host registration, resolver and actual build | Missing configuration: [refresh the owning integration](capabilities/binding.md#inspect-and-install-the-binding) |
| Unsupported CLI args, TTY or Profile path | Check registered alias, PTY and appended agent command | Launcher misuse: [correct host startup](capabilities/sessions.md#start-the-selected-host) |
| SDK import/fingerprint rejection | Inspect exact resolver entry and handshake result | A version label is insufficient; [check actual support](capabilities.md#establish-actual-support) |
| Model/provider/credential rejection | Compare exact provider/model, allowlist and required credential names | Repair only the affected [authorization/configuration](capabilities/authorization.md); preserve model intent |
| Provider transport failure | Read structured error, endpoint, certificate access and retryability | Configuration is not connectivity; [locate the failed boundary](workflows/diagnose.md#2-locate-the-failing-layer) |

## Environment and permissions

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| Outer variable absent in Root/Child/MCP | Compare only required variables at each boundary | Repair the owning [configuration plane](capabilities/configuration.md#separate-configuration-ownership) |
| Write grant overlaps private TT state | Compare actual roots and role grants | Narrow grants; preserve existing bindings and use supported [state placement/recovery](capabilities/state-and-access.md#filesystem-and-state-roots) |
| Shell works, Child/verifier fails | Compare interpreter/imports, cwd, activation and temporary paths | Recheck through [actual snapshot execution](workflows/execute.md#2-verify-the-snapshot-execution-environment) |
| Permission or Git ownership failure | Record exact UID/GID and failing path | Repair task-owned access or exact Git trust using [access boundaries](capabilities/state-and-access.md#filesystem-and-state-roots) |

## Snapshots and context

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| Git clean but edit-surface violation | Compare baseline/attempt snapshot paths | Ignore rules do not exclude capture; fix output placement or [contract](capabilities/filesystem.md#work-inside-the-frozen-surface) |
| Same cwd or local_workspace looks wrong | Inspect actual Child/branch and controlled write evidence | A diagnostic label does not prove sharing; [verify attachment/isolation](capabilities/filesystem.md#inspect-attachment-and-capacity) |
| Frozen skills differ from Child visibility | Compare frozen context, role and actual task-local files | Main access is not Child loading; [check role-bound context](capabilities/authorization.md#verify-access-and-context) |
| Snapshot or request admission exhausted | Inspect fs.stat capacity and retained owners | Use supported owner cleanup; [observe before releasing](capabilities/observation.md#establish-public-state) |

## Resources and execution

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| OOM, busy device or port conflict | Identify assigned group, lock and service owner | Serialize or wait within budget; [verify resource wiring](capabilities/execution.md#verify-device-and-lock-wiring) |
| Pool registered but device wrong/absent | Compare lane, effective environment, physical process usage and lock identity | Pool/visibility alone is not allocation; [check actual execution](capabilities/execution.md#verify-device-and-lock-wiring) |
| Cleanup uncertain | Check task-owned processes, requests and resource owners | Preserve unresolved ownership; [reconcile before retrying](capabilities/observation.md#reconcile-an-interrupted-call) |
| Longer MCP wait does not prevent timeout | Compare caller, provider and business/cleanup deadlines | Rebudget the [complete invocation](capabilities/execution.md#budget-the-complete-invocation); reconcile timed-out requests |

## Asynchronous execution and recovery

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| Verifier accepted/pending | Read original attempt and candidate_action | Follow [settlement instructions](capabilities/sessions.md#dispatch-and-observe-managed-work); accepted is not passed |
| Message queued, bridge timeout or printing failure | Identify pre-send versus post-send failure and original request status | [Reconcile](capabilities/observation.md#reconcile-an-interrupted-call) before resending |
| OS signal exits Root or monitor stops tree | Check control used and retained native state | Use supported controls and [readiness recovery](capabilities/observation.md#native-readiness) |
| TT stopped, Goal active; old Root fails readiness | Compare original Root/session/state root and actual Goal binding | [Recover the original host](capabilities/observation.md#native-readiness); new TUI/busy is not recovery |

## Evidence and delivery

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| Child returned without passing Evidence | Inspect settled attempt/artifact, failure class and exported outputs | Narrative/diagnostic score is insufficient; [diagnose the verifier/result](workflows/diagnose.md#4-choose-the-responsible-repair-path) |
| Selected artifact but source unchanged | Inspect promotion mode and apply/publication receipt | artifact_only is expected to leave source unchanged; [verify delivery](capabilities/filesystem.md#deliver-the-verified-artifact) |

Business correctness and task-specific cleanup criteria belong to the task's own instructions.
Container-specific paths, devices and model settings are not TT configuration defaults.
