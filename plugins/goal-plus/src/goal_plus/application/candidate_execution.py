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
    require_candidate_not_recovering(record)
    return session


def require_session_execution(session: AgentSessionRecord, frozen: FrozenSpec) -> None:
    if (session.agent_harness, session.runtime_provider) != (frozen.agent_harness, frozen.runtime_provider):
        raise ValueError("session execution identity does not match frozen binding")
