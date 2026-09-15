"""Read-only candidate admission, session binding and settled-result projections."""

from collections.abc import Mapping
from typing import Any

from goal_plus.application.candidate_execution import (
    require_current_session,
    require_worker_iteration_state,
)
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.search import AgentSessionRecord, CandidateRecovery
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.candidate_host import CandidateFileWriteBoundary, CandidateHostSource


class CandidateHostQueries:
    def __init__(self, source: CandidateHostSource, context: SearchContext) -> None:
        self.source = source
        self.context = context

    def uses_adaptive_search(self, run_id: str) -> bool:
        try:
            run = self.source.read_run(run_id)
            strategy = self.source.read_frozen_spec(run.frozen_spec_id).spec.strategy
        except (OSError, ValueError):
            return False
        return strategy.orchestration_mode == "adaptive_search"

    def requires_immediate_stop(self, session: AgentSessionRecord) -> bool:
        try:
            record = self.source.read_candidate(session.run_id, session.candidate_id)
        except (OSError, ValueError):
            return False
        for iteration in reversed(record.iterations):
            if iteration.agent_session_id not in {None, session.agent_session_id}:
                continue
            return (
                iteration.failure_class == "VerifierWorkspaceSideEffect"
                or iteration.metrics.get("infrastructure_failure") is True
                or iteration.metrics.get("candidate_action") == "stop_and_report"
            )
        return False

    def pending_retirement_decision_id(
        self, run_id: str, candidate_id: str
    ) -> str | None:
        if not self.uses_adaptive_search(run_id):
            return None
        return next(
            (
                decision.decision_id
                for decision in self.source.load_allocation_decisions(run_id)
                if decision.status == "pending"
                and candidate_id in decision.retire_candidate_ids
            ),
            None,
        )

    def validate_candidate(self, run_id: str, candidate_id: str) -> None:
        record = self.source.read_candidate(run_id, candidate_id)
        if record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot dispatch candidate {candidate_id} in status {record.status}"
            )
        if record.allocation_eligibility != "eligible":
            raise RuntimeError(f"cannot dispatch retired candidate {candidate_id}")
        pending_decision_id = self.pending_retirement_decision_id(
            run_id, candidate_id
        )
        if pending_decision_id is not None:
            raise RuntimeError(
                f"cannot dispatch candidate {candidate_id}: pending allocation "
                f"decision {pending_decision_id} retires this lane"
            )

    def retirement_fenced(self, run_id: str, candidate_id: str) -> bool:
        record = self.source.read_candidate(run_id, candidate_id)
        return record.allocation_eligibility != "eligible" or self.pending_retirement_decision_id(run_id, candidate_id) is not None

    def sessions(
        self, run_id: str, candidate_id: str | None = None, agent_harness: str | None = None
    ) -> list[AgentSessionRecord]:
        return [
            item
            for item in self.source.read_agent_sessions(run_id)
            if (candidate_id is None or item.candidate_id == candidate_id)
            and (agent_harness is None or item.agent_harness == agent_harness)
        ]

    def session(
        self, agent_session_id: str, run_id: str | None = None,
        *, native_session_id: str | None = None,
    ) -> AgentSessionRecord:
        from goal_plus.application.candidate_execution import require_session_execution

        session = self.source.read_agent_session(agent_session_id, run_id)
        run = self.source.read_run(session.run_id)
        require_session_execution(session, self.source.read_frozen_spec(run.frozen_spec_id))
        if native_session_id is not None and session.session_handle.external_id != native_session_id:
            raise PermissionError("native session handle no longer belongs to this controller")
        return require_current_session(
            session, self.source.read_candidate(session.run_id, session.candidate_id)
        )

    def current_session(self, run_id: str, candidate_id: str) -> AgentSessionRecord | None:
        candidate = self.source.read_candidate(run_id, candidate_id)
        session = next((item for item in reversed(self.sessions(run_id, candidate_id))
                        if item.execution_generation == candidate.execution_generation), None)
        return self.session(session.agent_session_id, run_id) if session is not None else None


    def process_environment(self, agent_session_id: str) -> dict[str, str]:
        session = self.session(agent_session_id)
        return dict(self.source.read_candidate(session.run_id, session.candidate_id).task.process_environment)

    def find_session(self, agent_session_id: str) -> AgentSessionRecord | None:
        """Historical host observation only; this does not grant execution admission."""
        try:
            return self.source.read_agent_session(agent_session_id)
        except (OSError, ValueError, RuntimeError):
            return None

    def require_native_execution(
        self, agent_session_id: str, *, now: float,
        native_session_id: str | None = None,
    ) -> AgentSessionRecord:
        session = self.session(agent_session_id, native_session_id=native_session_id)
        run = self.source.read_run(session.run_id)
        require_worker_iteration_state(run, "wake native candidate")
        self.validate_candidate(session.run_id, session.candidate_id)
        deadline = outer_deadline_epoch(self.context.outer_deadline(session.run_id))
        if deadline is not None and now >= deadline:
            raise RuntimeError("cannot wake a native candidate after its Goal deadline")
        return session

    def native_file_write_boundary(
        self, agent_session_id: str, *, now: float,
    ) -> CandidateFileWriteBoundary:
        session = self.require_native_execution(agent_session_id, now=now)
        run = self.source.read_run(session.run_id)
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        record = self.source.read_candidate(session.run_id, session.candidate_id)
        return CandidateFileWriteBoundary(
            workspace=session.workspace,
            denied_files=tuple(dict.fromkeys([
                *record.task.denied_files, *frozen.verifier_hashes, ".git",
            ])),
        )

    def fenced_session(self, agent_session_id: str) -> AgentSessionRecord | None:
        session = self.source.read_agent_session(agent_session_id)
        candidate = self.source.read_candidate(session.run_id, session.candidate_id)
        return (
            session
            if session.execution_generation != candidate.execution_generation
            else None
        )

    def discover_sessions(self) -> list[AgentSessionRecord]:
        return [
            session
            for identity in self.source.read_agent_session_ids()
            if (session := self.find_session(identity)) is not None
        ]

    def recovery(self, run_id: str, candidate_id: str) -> CandidateRecovery | None:
        return self.source.read_candidate(run_id, candidate_id).recovery

    def verifier_infrastructure_report(
        self, run_id: str, candidate_id: str
    ) -> dict[str, Any] | None:
        report = self.source.read_candidate(run_id, candidate_id).score_report
        if report is not None and any(
            result.failure_class == "VerifierWorkspaceSideEffect"
            or result.metrics.get("infrastructure_failure") is True
            or result.metrics.get("candidate_action") == "stop_and_report"
            for result in report.verifier_results
        ):
            return report.model_dump(mode="json")
        return None
