"""Decision storage and Allocation-owned projections, without execution access."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from goal_plus.domain.adaptive.models import AllocationDecision, AllocationEligibility
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class AllocationIterationProjection:
    iteration: int
    decision_id: str | None
    error: str | None


@dataclass(frozen=True)
class AllocationCandidateProjection:
    candidate_id: str
    eligibility: AllocationEligibility
    retired_by_decision_id: str | None
    iterations: tuple[AllocationIterationProjection, ...] = ()
    report_decision: AllocationDecision | None = None


class AllocationState(Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def load_allocation_decision(
        self, run_id: str, decision_id: str
    ) -> AllocationDecision: ...
    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]: ...
    def write_allocation_decision(self, decision: AllocationDecision) -> None: ...
    def write_candidate_projection(
        self, run_id: str, projection: AllocationCandidateProjection
    ) -> None: ...
    def write_decision_index(self, run_id: str, next_decision_index: int) -> None: ...
