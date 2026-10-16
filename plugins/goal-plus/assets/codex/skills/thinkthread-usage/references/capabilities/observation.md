# Observation and Recovery

Use for task inspection, uncertain execution, native readiness, or recovery. Start from retained
identities and their actual runtime roots; logs explain observations but do not define the contract.

## Establish public state

Read `goal_plus_status(detail=true)` and, when available for the task,
`goal_plus_monitor_snapshot`. Follow returned control, next_action and reconciliation instructions.
Monitor is observation, not a controller. A failed lookup in an outer shell's `.gp` does not prove
the Goal in the original TT host is missing. Match build, Root, Goal, run, candidate and call first.

Use TT object/request status to distinguish active, stopped, failed, and uncertain operations.
`fs.stat` storage counters can identify active-request/snapshot quota pressure; release resources
through their owners only after references and active uses are accounted for.
With a Goal Plus checkout, reuse `scripts/monitor_goal_plus.sh --once <runtime-root>` and
`scripts/check_session_instructions.py --recent 6` for bounded diagnostics, not task recovery.

## Native readiness

Preserve the Root, native session, state-root path and Profile binding. Observe that state root
explicitly. A CLI start wait may expire while the supervisor is still preparing the TUI; check
the eventual native state before retrying. Inspect native-session loading, extension readiness,
PTY/terminal and MCP initialization separately. Shared warnings in working and failed startups
are insufficient root-cause evidence.

Recover through the installed host's supported terminal/session path. Acceptance requires the
original Root interactive, actual message submission acknowledged, and the original Goal readable.
A new diagnostic Root, open TUI or `busy` proves none of these. Preserve the old session unless
its unrecoverability is established. A monitor disconnect must not automatically stop its tree.
For persistent state-root placement or relocation, see [state and access boundaries](state-and-access.md).

## Reconcile an interrupted call

1. Identify the failure boundary: handshake rejection before send, enqueued request, completed
   operation with response/printing failure, or completion unknown. Preserve returned identities.
2. Inspect the original operation journal and supported request/session status. TT filesystem
   requests have `fs.request.status`; Goal Plus's owner coordinates managed request recovery.
3. For a managed session, retrying the same call_id with identical parameters observes/reconciles
   that call. Once it has ended and cleanup is confirmed, continuation uses a new call_id through
   `goal_plus_session_run`; active calls accept `goal_plus_session_message`.
4. Keep unresolved resource ownership until execution is reconciled. Use supported stop/close and
   verified quiescence; a wait timeout, controller exit or lock-file absence is not that proof.

Native session continuation, request reconciliation and candidate checkpoint recovery are distinct.
Consult [support boundaries](../capabilities.md#establish-actual-support) before selecting a path.
`goal_plus_search_recovery_status` can describe the candidate state; do not prescribe
`goal_plus_search_prepare_recovery` / `goal_plus_search_recover_candidate` for TT's unsupported
checkpoint capability. Never manufacture a recovery identity or edit `.gp` to clear a gate.

## Replace a defective verifier contract

After restoring the original host binding, invalidate the defective run through
`goal_plus_search_invalidate_run` with its evidence. Coordinate session stop/close and reconcile
workers, requests and leases. Repair the contract and freeze a replacement linked by
`source_run_id` only after unknown background execution is resolved. Preserve prior Evidence.
Installation refresh or workspace rebuild does not perform this recovery.

## Verify the recovery result

Check the original task's public state, exact operation outcome and remaining resources. Resume
only the affected workflow step after its prerequisite passes. Missing host recovery support or
unknown device/process ownership is an explicit blocker, not permission to start duplicate work.
For final successful closeout, use
[the short diagnosis path](../workflows/diagnose.md#successful-closeout).
