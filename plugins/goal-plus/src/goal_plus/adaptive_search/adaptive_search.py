"""Adaptive Search integration facade for the Search Scheduler."""

from typing import Literal
from goal_plus.adaptive_search.allocation import AllocationExecutor
from goal_plus.application.adaptive_admission import AdaptiveAdmission
from goal_plus.ports.candidate_host import CandidateHostSource
from goal_plus.adaptive_search.scheduler import SchedulerManager
from goal_plus.domain.adaptive.models import AllocationDecision, SchedulerRewardRecord
from goal_plus.domain.evidence import ScoreReport


class AdaptiveSearch:
    """Coordinate adaptive Search hooks without owning scheduler internals."""

    def __init__(
        self,
        *,
        source: CandidateHostSource,
        admission: AdaptiveAdmission,
        allocation: AllocationExecutor,
        scheduler: SchedulerManager,
    ) -> None:
        self.source, self.admission = source, admission
        self._allocation, self._scheduler = allocation, scheduler

    def list_allocation_decisions(
        self,
        run_id: str,
        status: Literal["pending", "applied"] | None = None,
    ) -> list[AllocationDecision]:
        self.admission.record_pending_decisions(run_id)
        return self._allocation.list(run_id, status)

    def apply_allocation_decision(
        self,
        run_id: str,
        decision_id: str,
    ) -> dict[str, object]:
        return self._allocation.apply(run_id, decision_id)

    def list_scheduler_rewards(
        self,
        run_id: str,
        candidate_id: str | None = None,
    ) -> list[SchedulerRewardRecord]:
        return self._scheduler.list_rewards(run_id, candidate_id)

    def resolve_scheduler_barrier(
        self,
        run_id: str,
        candidate_id: str,
        report: ScoreReport,
    ) -> ScoreReport:
        self.admission.record_pending_decisions(run_id)
        current = self.source.read_candidate(run_id, candidate_id)
        if current.allocation_eligibility == "retired":
            return current.score_report or report
        resolved = self._scheduler.resolve_barrier(run_id, candidate_id, report)
        self.admission.record_pending_decisions(run_id)
        refreshed = self.source.read_candidate(run_id, candidate_id)
        return refreshed.score_report or resolved
