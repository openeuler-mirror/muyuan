"""Candidate-owned trace capture without affecting scoring or authorization."""

from __future__ import annotations

from typing import Any

from goal_plus.domain.candidate_trace import (
    CandidateTraceEvent,
    CandidateTraceIdentity,
    CandidateTraceKind,
    CandidateTraceSnapshot,
    OperationFailedPayload,
    validate_candidate_trace_payload,
)
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.ports.candidate_trace import CandidateTraceStore
from goal_plus.ports.clock import Clock


class CandidateTrace:
    def __init__(self, store: CandidateTraceStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    @staticmethod
    def identity(session: AgentSessionRecord) -> CandidateTraceIdentity:
        return CandidateTraceIdentity(
            run_id=session.run_id,
            candidate_id=session.candidate_id,
            agent_session_id=session.agent_session_id,
            execution_generation=session.execution_generation,
        )

    def new_operation_id(self, prefix: str) -> str:
        return self.store.new_operation_id(prefix)

    def snapshot(
        self, session: AgentSessionRecord, content: Any
    ) -> CandidateTraceSnapshot:
        return self.store.write_snapshot(self.identity(session), content)

    def record(
        self,
        session: AgentSessionRecord,
        *,
        kind: CandidateTraceKind,
        operation_id: str,
        payload: dict[str, Any] | Any,
        host_call_id: str | None = None,
        occurred_at: str | None = None,
        deduplicate: bool = False,
    ) -> CandidateTraceEvent:
        validated = validate_candidate_trace_payload(kind, payload)
        return self.store.append_event(
            self.identity(session),
            occurred_at=occurred_at or self.clock.timestamp(),
            kind=kind,
            operation_id=operation_id,
            host_call_id=host_call_id,
            payload=validated,
            deduplicate=deduplicate,
        )

    def failed(
        self,
        session: AgentSessionRecord,
        *,
        operation: str,
        operation_id: str,
        error: BaseException,
        verified_target: dict[str, Any] | None = None,
    ) -> None:
        payload = OperationFailedPayload(
            operation=operation,
            verified_target=verified_target or {},
            error_category=type(error).__name__,
        )
        try:
            self.record(
                session,
                kind="operation.failed",
                operation_id=operation_id,
                payload=payload,
            )
        except Exception:
            # A failed diagnostic sink cannot provide a second durable diagnostic.
            pass
