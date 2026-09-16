"""Durable Allocation decision storage."""

from typing import Protocol
from goal_plus.domain.adaptive.models import AllocationDecision


class AllocationState(Protocol):
    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]: ...
    def write_allocation_decision(self, decision: AllocationDecision) -> None: ...
