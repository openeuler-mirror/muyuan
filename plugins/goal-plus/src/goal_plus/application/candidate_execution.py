"""Shared recovery/generation gates for new and reused candidate bindings."""

from goal_plus.domain.search import AgentSessionRecord, FrozenSpec, RunState
from goal_plus.domain.agents import validate_search_execution
from goal_plus.ports.state import CandidateRecord, RunRecord


WORKER_ITERATION_RUN_STATES = frozenset(
    {
        RunState.RUNNING,
        RunState.WAITING_FOR_WORKERS,
        RunState.SELECTING,
        RunState.SELECTION_BLOCKED,
    }
)


def require_run_not_invalidated(run: RunRecord, operation: str) -> None:
    if run.invalidated_at:
        raise RuntimeError(
            f"cannot {operation}: run {run.run_id} was invalidated because "
            f"{run.invalidation_reason}"
        )


def require_worker_iteration_state(run: RunRecord, operation: str) -> None:
    require_run_not_invalidated(run, operation)
    if run.state not in WORKER_ITERATION_RUN_STATES:
        raise RuntimeError(
            f"cannot {operation}: run {run.run_id} is in state {run.state}"
        )


def require_candidate_not_recovering(record: CandidateRecord) -> None:
    if record.recovery is not None and record.recovery.state != "restored":
        raise RuntimeError(
            "CandidateRecoveryRequired: finish the prepared recovery first"
        )


def require_no_pending_verification(record: CandidateRecord) -> None:
    pending = record.verification
    if pending is not None:
        reason = "VerifierInProgress" if pending.state == "running" else "VerifierRecoveryRequired"
        raise RuntimeError(
            f"{reason}: attempt {pending.attempt_id} remains active; "
            "a client timeout does not cancel verification. Query search_get_agent_context; "
            "do not edit or submit another attempt until it settles or is recovered."
        )


def is_recovery_replacement(session: AgentSessionRecord, record: CandidateRecord) -> bool:
    """Return whether ``session`` is the post-recovery provider continuation.

    A fork's CandidateOrigin remains immutable provenance.  ThinkThread recovery
    may nevertheless create a new Child/branch from the settled snapshot after
    the original provider resource has been fenced and closed.
    """
    recovery = record.recovery
    return bool(
        recovery is not None
        and recovery.state == "restored"
        and session.execution_generation == record.execution_generation
        and session.agent_session_id not in recovery.fenced_session_ids
    )


def require_fork_continuation(
    record: CandidateRecord, *, allow_recovery_replacement: bool = False,
) -> None:
    if record.task.candidate_origin is not None and not (
        allow_recovery_replacement
        and record.recovery is not None
        and record.recovery.state == "restored"
    ):
        raise RuntimeError(
            "ForkContinuationRequired: resume the exact native branch; "
            "a fresh session requires Main review and a successor run"
        )


def require_current_session(
    session: AgentSessionRecord, record: CandidateRecord
) -> AgentSessionRecord:
    validate_search_execution(session.agent_harness, session.runtime_provider, record.task.workspace_provider)
    if (session.agent_harness, session.runtime_provider) != (
        session.session_handle.agent_harness, session.session_handle.runtime_provider,
    ):
        raise ValueError("session handle does not match execution identity")
    if (
        session.run_id != record.task.run_id
        or session.candidate_id != record.candidate_id
    ):
        raise RuntimeError(
            "AgentSessionOwnershipMismatch: candidate binding does not match"
        )
    if session.execution_generation != record.execution_generation:
        raise RuntimeError(
            "StaleAgentSession: candidate execution generation was fenced"
        )
    if session.candidate_origin != record.task.candidate_origin:
        raise RuntimeError("ForkBindingMismatch: task and session binding differ")
    binding = session.candidate_origin
    replacement = is_recovery_replacement(session, record)
    if binding is not None and (
        (binding.worker_identity != {"agent_session_id": session.agent_session_id, "candidate_id": session.candidate_id}
         and not replacement)
        or binding.runtime_provider != session.runtime_provider
        or binding.workspace != session.workspace
        or (binding.provider_session_id is not None
            and binding.provider_session_id != session.session_handle.external_id
            and not replacement)
    ):
        raise RuntimeError("ForkBindingMismatch: native branch does not match this worker identity")
    require_candidate_not_recovering(record)
    return session


def require_session_execution(session: AgentSessionRecord, frozen: FrozenSpec) -> None:
    if (session.agent_harness, session.runtime_provider) != (frozen.agent_harness, frozen.runtime_provider):
        raise ValueError("session execution identity does not match frozen binding")


def require_fork_ready(run: RunRecord, frozen: FrozenSpec, records: list[CandidateRecord]) -> None:
    if not frozen.spec.strategy.fork_enabled:
        return
    fork = run.dispatch_group
    if fork is None or fork.status not in {"started", "closed"}:
        raise RuntimeError("ForkIncomplete: fork group is missing, partial or failed")
    if fork.frozen_spec_id != run.frozen_spec_id or fork.runtime_provider != frozen.runtime_provider or len(fork.targets) < 2:
        raise RuntimeError("ForkBindingMismatch: group does not match the frozen spec")
    for record in records:
        matches = [b for b in fork.targets if b.candidate_id == record.candidate_id]
        binding = record.task.candidate_origin
        if (len(matches) != 1 or binding is None or matches[0].binding != binding
                or binding.frozen_spec_id != run.frozen_spec_id
                or binding.runtime_provider != frozen.runtime_provider
                or binding.source_artifact_ref != run.source_artifact_ref
                or binding.workspace != record.task.workspace or matches[0].launch_state != "started"
                or binding.provider_session_id != matches[0].provider_session_id
                or binding.worker_identity != {"agent_session_id": matches[0].agent_session_id, "candidate_id": record.candidate_id}):
            raise RuntimeError("ForkBindingMismatch: revised or inconsistent branch requires Main review and a successor run")
        if frozen.frozen_spec_id == binding.frozen_spec_id:
            if frozen.spec_hash != binding.spec_hash:
                raise RuntimeError("ForkBindingMismatch: initial spec hash differs")
        elif not (
            (frozen.dispatch_group_id, frozen.dispatch_target_index) == (binding.dispatch_group_id, binding.dispatch_target_index)
            and any(item.frozen_spec_id == frozen.frozen_spec_id and item.spec_hash == frozen.spec_hash
                    for item in record.spec_revisions)
        ):
            raise RuntimeError("ForkBindingMismatch: spec revision does not belong to this branch")
        if binding.runtime_provider == "thinkthread":
            attachment = binding.workspace_attachment
            provider_handle = record.task.workspace_provider_handle
            original_handle_matches = (
                attachment is not None
                and provider_handle.get("thinkthread_id") == binding.provider_session_id
                and provider_handle.get("branch_id") == attachment.fs_branch_id
                and provider_handle.get("agent_session_id") == matches[0].agent_session_id
            )
            recovered_handle_matches = (
                record.recovery is not None
                and record.recovery.state == "restored"
                and record.task.base_artifact_ref is not None
                and record.task.base_artifact_ref.kind == "fs_snapshot"
                and record.task.base_artifact_ref.provider == "thinkthread"
                and provider_handle.get("base_snapshot_id") == record.task.base_artifact_ref.id
                and bool(provider_handle.get("thinkthread_id"))
                and bool(provider_handle.get("branch_id"))
                and bool(provider_handle.get("agent_session_id"))
            )
            if (attachment is None or attachment.provider != "thinkthread"
                    or attachment.root_thinkthread_id != run.source_provider_handle.get("root_thinkthread_id")
                    or not (original_handle_matches or recovered_handle_matches)):
                raise RuntimeError("ForkBindingMismatch: private Fs is not bound to the fork Child")
