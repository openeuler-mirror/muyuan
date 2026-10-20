"""Enforce deterministic Adaptive Search candidate admission."""

from typing import Literal

from goal_plus.ports.candidate_host import CandidateHostSource
from goal_plus.ports.state import CandidateRecord


CandidateOperation = Literal[
    "recover",
    "redispatch",
    "start_session",
    "continue_session",
    "run_verifier",
]


class AdaptiveAdmission:
    """Read-only guard for candidate execution under Adaptive Search."""

    def __init__(self, source: CandidateHostSource) -> None:
        self.source = source

    def assert_candidate_execution_allowed(
        self, record: CandidateRecord, operation: CandidateOperation
    ) -> None:
        candidate_id = record.candidate_id
        if record.allocation_eligibility != "eligible":
            raise RuntimeError(
                f"cannot {operation} retired candidate {candidate_id}"
            )
        run = self.source.read_run(record.task.run_id)
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        if not frozen.spec.strategy.adaptive_search_enabled:
            return
        active_retirements = {
            candidate_id
            for decision in self.source.load_allocation_decisions(record.task.run_id)
            if decision.status in {"pending", "interrupted"}
            for candidate_id in [decision.selected_retirement]
            if candidate_id is not None
        }
        if candidate_id in active_retirements:
            raise RuntimeError(
                f"cannot {operation} candidate {candidate_id}: "
                "an active allocation decision retires this lane"
            )
