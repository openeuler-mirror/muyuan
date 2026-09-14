"""Review a returned candidate workspace before the host delivers its result."""

from dataclasses import dataclass
from typing import Any, Literal

from goal_plus.application.candidate_queries import CandidateHostQueries
from goal_plus.application.search_queries import SearchQueries


@dataclass(frozen=True)
class CandidateDeliveryReview:
    action: Literal["infrastructure_failure", "reuse", "unchanged", "verify", "disabled", "retired"]
    report: dict[str, Any] | None = None

    @property
    def infrastructure_failure(self) -> bool:
        return self.action == "infrastructure_failure"


class CandidateDelivery:
    def __init__(self, queries: SearchQueries, host_queries: CandidateHostQueries) -> None:
        self.queries = queries
        self.host_queries = host_queries

    def review(
        self, run_id: str, candidate_id: str, *, final_verify: bool = True,
    ) -> CandidateDeliveryReview:
        report = self.host_queries.verifier_infrastructure_report(run_id, candidate_id)
        if report is not None:
            return CandidateDeliveryReview("infrastructure_failure", report)
        if self.host_queries.retirement_fenced(run_id, candidate_id):
            return CandidateDeliveryReview("retired")
        if not final_verify:
            return CandidateDeliveryReview("disabled")
        report = self.queries.current_process_evidence(run_id, candidate_id)
        if report is not None:
            return CandidateDeliveryReview("reuse", report)
        if not self.queries.candidate_has_unverified_changes(run_id, candidate_id):
            return CandidateDeliveryReview("unchanged")
        # The host executes this request through the original hard verifier gate.
        return CandidateDeliveryReview("verify")
