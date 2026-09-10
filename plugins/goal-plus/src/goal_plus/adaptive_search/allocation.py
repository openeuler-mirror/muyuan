"""Public deterministic Allocation facade."""

from typing import Any, Literal

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.allocation_apply import AllocationApply
from goal_plus.domain.adaptive.models import (
    AllocationDecision as AllocationDecision,
    AllocationDecisionStatus as AllocationDecisionStatus,
    AllocationEligibility as AllocationEligibility,
    AllocationPlan as AllocationPlan,
    ExpansionSource as ExpansionSource,
)
from goal_plus.infrastructure.state.allocation import (
    load_allocation_decisions as load_allocation_decisions,
)

__all__ = [
    "AllocationDecision",
    "AllocationEligibility",
    "AllocationExecutor",
    "AllocationPlan",
    "ExpansionSource",
    "load_allocation_decisions",
]


class AllocationExecutor:
    def __init__(
        self, decisions: AllocationDecisions, application: AllocationApply
    ) -> None:
        self._decisions = decisions
        self.application = application

    def list(
        self,
        run_id: str,
        status: Literal[
            "pending", "applied", "superseded", "terminal_error"
        ]
        | None = None,
    ) -> list[AllocationDecision]:
        return self._decisions.list(run_id, status)

    def apply(self, run_id: str, decision_id: str) -> dict[str, Any]:
        result = self.application.apply(run_id, decision_id)
        return {
            "decision": result.decision.model_dump(mode="json"),
            "candidate_tasks": [
                item.model_dump(mode="json") for item in result.candidate_tasks
            ],
            "agent_sessions": [
                item.model_dump(mode="json") for item in result.agent_sessions
            ],
        }
