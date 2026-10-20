# During Execution: Interfaces and Probes

Use after [preparation](prepare.md), or resume at the first unverified step of an existing task.
Read only the relevant capability sections. A failure enters [diagnosis](diagnose.md)
immediately; do not keep running expensive steps that depend on a failed environment check.

## 1. Establish the actual Root and Goal

Follow [host startup](../capabilities/sessions.md#start-the-selected-host), or verify the retained
Root binding. Check actual loaded build, native readiness and current role. Submit the task through
the selected host entrypoint and retain the returned Goal identity. Read the current Goal Plus
schema before constructing a nontrivial SearchSpec.

Pass: the intended Root and Goal are bound and public state is readable. A TUI opening, launcher
receipt or provider flag alone is insufficient. A setup-only inspection can stop before Goal start.

## Choose a probe

Select the smallest probe that answers the current uncertainty; these are not mandatory stages.
Use exposed tools and the installed native schema. Where no supported diagnostic surface exists,
report that gap instead of reaching into private state or inventing a helper command.

| Probe | Preconditions and side effects | Passing evidence | Cleanup |
| --- | --- | --- | --- |
| Read-only binding/status | Actual host; no new Child or device work | Build, role, attachment and requested capability agree | None beyond closing diagnostic handles |
| Snapshot invocation | Authorized immutable snapshot and fs.run owner; bounded temporary writes | Named env/imports, expected content, writable isolated tmp, unchanged source, exported result readable | Finish/reconcile request and remove owned temporary data through supported paths |
| Child/communication | Authorized managed worker lifecycle; may invoke a model even for a small prompt | Exact Child/branch, delivered message and expected role-bound result | Host stop/close after quiescence; retain resources still used by the task |
| Device/service | Authorized allocation, locks, workload and finite budget | Execution and resource/cleanup evidence required by the task | Stop task-owned processes, verify release and retain uncertain owners |

For each selected probe, record the target identity, exact operation, expected result, budget,
side effects and cleanup method before execution. Afterward retain a bounded sanitized receipt.
Do not create an extra candidate merely for inspection when existing managed execution can answer
the question. A direct diagnostic probe never becomes Search Evidence by renaming its output.

## 2. Verify the snapshot execution environment

On the supported exact snapshot path, inspect only required variables, interpreter/import identity
and cwd. Test a small write/read/export in invocation-local temporary space and verify source
immutability. Check effective allocation and resolved lock identity using
[execution guidance](../capabilities/execution.md); a no-model probe cannot prove physical service
usage or cross-task exclusion. Repeat only where execution phase or changed configuration warrants.

If a snapshot becomes available only during freeze/bootstrap, put the necessary environment checks
at the beginning of the frozen verifier and gate expensive stages on their success. Inspect the
admission receipt; do not invent a separate candidate or bypass the snapshot owner to run a probe.

Pass: required data survives discard, output is bounded, and workload prerequisites are established.
If the build lacks necessary injection or export support, report the integration gap before relying
on a formal run. Do not manually pin a shell device and call it runtime allocation.

## 3. Freeze and execute the task

For Search, use `goal_plus_search_freeze_spec` and `goal_plus_search_create`, then the current
plan/batch flow and managed `goal_plus_session_run`. Some origin verification occurs during
admission: perform its prerequisite checks beforehand, and inspect its receipt before proceeding.
Optimization needs a valid comparable baseline; first deployment may allow a failing origin only
when the installed contract supports it. Do not fabricate an admission pass.

The default omits `strategy.adaptive_search` and uses an initial plan with fixed lanes. For an
explicit `strategy.adaptive_search` policy, preserve its allocation/steering contract and let
runtime consume settled records.
Inspect `goal_plus_search_list_allocation_decisions` and apply a pending decision through
`goal_plus_search_apply_allocation_decision` with its returned identity; do not add a scheduler.

Observe calls and steer through [session tools](../capabilities/sessions.md). Check actual Child
role/context and branch, then follow candidate verifier receipts through settlement. For devices
and services, run only the task's authorized checks. Retain exact artifact, resource,
request and outcome evidence; narrative success does not establish a passing attempt.

## 4. Select, deliver and close out

At the stopping condition, confirm worker quiescence while retaining scopes needed for final work.
Use [artifact delivery](../capabilities/filesystem.md#deliver-the-verified-artifact) for selection,
promotion and authorized apply. Check required final replay in its actual authorized environment;
Root reading a candidate's report is not independent replay. If a required host final-check path
is unsupported, report the blocker rather than substituting self-review.

Pass: the exact delivered artifact and required verification are identified, with apply status and
remaining limitations. Continue to [successful closeout](diagnose.md#successful-closeout) for
terminal-state and resource checks. Enter failure diagnosis if outcome or cleanup is unknown.
