"""Adaptive Search facade over Allocation execution."""

from typing import Literal

from goal_plus.adaptive_search.allocation import AllocationExecutor
from goal_plus.domain.adaptive.models import AllocationDecision


class AdaptiveSearch:
    def __init__(
        self,
        *,
        allocation: AllocationExecutor,
    ) -> None:
        self._allocation = allocation

    def list_allocation_decisions(
        self,
        run_id: str,
        status: Literal[
            "pending", "applied", "superseded", "terminal_error"
        ]
        | None = None,
    ) -> list[AllocationDecision]:
        return self._allocation.list(run_id, status)

    def apply_allocation_decision(
        self, run_id: str, decision_id: str
    ) -> dict[str, object]:
        return self._allocation.apply(run_id, decision_id)
