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
| Provider returns `server_error`/overloaded | Preserve the exact provider/model and request identity, then check the provider's bounded response and retryability | This is provider capacity/transport evidence; do not reinterpret it as a Goal Plus dispatch or fork/fresh defect |
| `tt run` rejects a bare model | Check that `--model` is exactly `provider/model` and is allowed by the Profile | Use the admitted provider/model, for example `sub2api-openai/gpt-6-luna`; this failure is before Root startup |
| Pi says its configuration directory is not real | Inspect inherited `PI_CODING_AGENT_DIR`, symlink target and Profile `configDirDefault` | Remove the stale override for a new managed run; do not diagnose Goal Plus or TT provider behavior yet |
| System test is reported skipped | Check pytest output for `system tests require explicit --run-system-tests` | Rerun with `uv run pytest -n 0 --run-system-tests`; skipped is not passing evidence |

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
| Child fails while loading the Goal Plus manifest | Compare `host_mcp_manifest` timing with dispatch identity binding | Allow schema-only discovery before binding; require the exact candidate identity for every real tool call |
| `goal_plus_search_freeze_spec` reports `ValidationError`, `Extra inputs are not permitted`, or `ConflictingFreezeParameter` | Read `goal_plus_get_tool_schema("goal_plus_search_freeze_spec")`; verify `spec` is one complete nested SearchSpec and do not duplicate `spec.source_path` as the top-level compatibility argument | Remove stale fields from the payload and retry the same freeze request; this is Goal Plus schema admission, not a TT provider or Child startup failure |
| AutoResearch-Git reports `inner Git HEAD does not match the settled ledger` | Read the child dispatch, attempt and settlement receipts before retrying; compare the child filesystem's `.git/goal-plus/state.json` with `git rev-parse HEAD` | Treat it as inner Git/continuation state until proven otherwise; do not blindly respawn or submit another provider operation when an attempt is unresolved |
| Two real-host ST runs collide in `workspace/circle` or PTY | Check the pytest temp root and active `tt` state roots | Run these native Circle cases serially, or provide distinct `ST_THINKTHREAD_BASE_TMP` roots; independent state roots alone do not isolate pytest workspace creation |
| OS signal exits Root or monitor stops tree | Check control used and retained native state | Use supported controls and [readiness recovery](capabilities/observation.md#native-readiness) |
| TT stopped, Goal active; old Root fails readiness | Compare original Root/session/state root and actual Goal binding | [Recover the original host](capabilities/observation.md#native-readiness); new TUI/busy is not recovery |

## Evidence and delivery

| Symptom | Smallest check | Interpretation and next step |
| --- | --- | --- |
| Child returned without passing Evidence | Inspect settled attempt/artifact, failure class and exported outputs | Narrative/diagnostic score is insufficient; [diagnose the verifier/result](workflows/diagnose.md#4-choose-the-responsible-repair-path) |
| Selected artifact but source unchanged | Inspect promotion mode and apply/publication receipt | artifact_only is expected to leave source unchanged; [verify delivery](capabilities/filesystem.md#deliver-the-verified-artifact) |

Business correctness and task-specific cleanup criteria belong to the task's own instructions.
Container-specific paths, devices and model settings are not TT configuration defaults.
