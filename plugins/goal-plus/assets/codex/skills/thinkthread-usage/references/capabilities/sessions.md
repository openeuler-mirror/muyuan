# Sessions and Communication

Use for Root startup, managed Child calls, message delivery, and lifecycle controls. Start with a
known host/Profile; use the actual Root/native session and returned Goal Plus identities thereafter.

## Start the selected host

~~~bash
tt run --profile goal-plus-pi --fs /absolute/path/to/workspace
tt run --profile goal-plus-codex --fs /absolute/path/to/workspace
~~~

Run one command for the selected host through its supported terminal/PTY. The Profile is a
registered alias, not a file path. Wait for stable readiness, then submit the complete task:
Pi uses `/goal-plus` with `workspace_provider=thinkthread`; Codex TT uses the exposed
`goal_plus_host_start` with that provider and the task prompt. Retain the returned Goal identity.
Resume an existing task through [recovery](observation.md), not another host-start operation.

The managed Codex adapter owns its invocation. Do not append `codex exec`, `--json -`, or an
entire Codex command to `tt run --`. Do not pass the Root prompt through `tt run -- -p`, because
`self` Children can inherit agent arguments. Preserve the task body through the host entrypoint.

## Dispatch and observe managed work

TT exposes Child spawn/get/list, message send/receive, wait/signal, and destruction operations.
In Goal Plus, use `goal_plus_session_run`, `goal_plus_session_wait_any`, and
`goal_plus_session_message`; its host owns native Child creation and request reconciliation.
Retain Goal/run/candidate, native session, call, message and request identities returned by the
relevant operation. A request receipt or queued message is not a completed action.

For a verifier submission, follow `candidate_action`: query the original attempt when told to wait,
or end the candidate turn when told to end_turn so snapshot settlement can proceed. Accepted is
not passed; do not submit another attempt or mutate an attempt awaiting settlement.

## Choose the lifecycle operation

| Operation | Meaning | What still needs verification |
| --- | --- | --- |
| Wait | Observe for a bounded interval | Timeout does not stop execution or renew its budget |
| Message | Steer an active call | Delivery and actual processing, not merely enqueue |
| Interrupt | End the current turn through host controls | Host acknowledgement and resulting call state |
| Stop | End native execution while preserving supported retained state | Quiescence and task-owned external services |
| Close | Release the session controller/resources | No further use of that closed controller |

For a fork candidate, subsequent calls reuse the original Child and native context, including
when `new_session=true` is supplied. If that context cannot continue, Main must choose successor
work; a fresh Child from a snapshot cannot retain the original fork provenance.

For a non-fork candidate, retained Child reuse and snapshot respawn are host decisions. Codex TT
retirement after settlement can require respawn from the settled snapshot. `new_session=true`
requests a fresh conversation; it does not restore a checkpoint.
Distinguish candidate end_turn settlement from Main continuation:
the latter requires the host mechanism listed in [support boundaries](../capabilities.md).

Before selection, stop active work and confirm quiescence while retaining scopes needed for
promotion/reporting. After the Goal is terminal, close sessions and invoke Codex TT Main's
`goal_plus_host_close_scopes(goal_plus_id)`. Keep non-closed scopes visible in the result.

## Verify or diagnose

Success is a receipt/state transition for the intended identity, plus the required execution or
cleanup evidence. Return prose, `busy`, a terminal disconnect, or an OS signal is insufficient.
For queued/uncertain requests, use [reconciliation](observation.md#reconcile-an-interrupted-call).
For startup waits, use [native readiness](observation.md#native-readiness).
