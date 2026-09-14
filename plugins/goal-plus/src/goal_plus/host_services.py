"""Candidate host assembly; Search provider mutations retain their existing owner."""

from pathlib import Path
from typing import Any
from goal_plus.infrastructure.host_bindings import candidate_inner_agent_binding
from goal_plus.application.candidate_host import ThinkThreadCandidatePreparation
from goal_plus.candidate_host import candidate_host_queries
from goal_plus.domain.evidence import ScoreReport
from goal_plus.domain.shared_tools import ToolizationDecision
from goal_plus.ports.state import CandidateRecord
from goal_plus.runtime import FileSearchRuntime
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient


class SearchHostService(ThinkThreadCandidatePreparation):
    def __init__(
        self, root_dir: Path | str, *, runtime: FileSearchRuntime | None = None
    ) -> None:
        self.runtime = runtime or FileSearchRuntime(root_dir)
        super().__init__(
            candidate_host_queries(root_dir),
            continue_session=self.runtime.candidate_sessions.continue_agent_session,
            resolve_binding=candidate_inner_agent_binding,
        )

    def bind_thinkthread_candidate(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        thinkthread_id: str,
        branch_id: str,
    ) -> CandidateRecord:
        return self.runtime.candidate_sessions.bind_thinkthread_candidate(
            run_id=run_id,
            candidate_id=candidate_id,
            agent_session_id=agent_session_id,
            thinkthread_id=thinkthread_id,
            branch_id=branch_id,
        )

    def verify_thinkthread_candidate_attempt(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        thinkthread_id: str,
        branch_id: str,
        hypothesis: str,
        attempt_id: str,
        inner_git_attempt: dict[str, Any] | None,
        client: AgentPosixSdkClient,
        toolization_decision: ToolizationDecision | None = None,
        recovery_deadline: float | None = None,
    ) -> ScoreReport:
        return self.runtime.verify_thinkthread_candidate_attempt(
            run_id=run_id,
            candidate_id=candidate_id,
            agent_session_id=agent_session_id,
            thinkthread_id=thinkthread_id,
            branch_id=branch_id,
            hypothesis=hypothesis,
            attempt_id=attempt_id,
            inner_git_attempt=inner_git_attempt,
            toolization_decision=toolization_decision,
            client=client,
            recovery_deadline=recovery_deadline,
        )
