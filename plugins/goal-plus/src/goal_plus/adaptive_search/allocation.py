"""Public Allocation facade and existing schema re-exports."""

from typing import Any, Literal

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.allocation_apply import AllocationApply
from goal_plus.domain.adaptive.models import (
    AllocationDecision as AllocationDecision,
    AllocationDecisionStatus as AllocationDecisionStatus,
    AllocationEligibility as AllocationEligibility,
    AllocationProposal as AllocationProposal,
    AllocationReplacement as AllocationReplacement,
    AllocationReplacementProposal as AllocationReplacementProposal,
    AllocationTrigger as AllocationTrigger,
    ExpansionSource as ExpansionSource,
    RewardedSource as RewardedSource,
)
from goal_plus.infrastructure.state.allocation import (
    load_allocation_decisions as load_allocation_decisions,
)

__all__ = [
    "AllocationDecision", "AllocationEligibility", "AllocationExecutor",
    "AllocationProposal", "AllocationReplacement", "AllocationReplacementProposal",
    "AllocationTrigger", "ExpansionSource", "RewardedSource", "load_allocation_decisions",
]


class AllocationExecutor:
    def __init__(
        self, decisions: AllocationDecisions, application: AllocationApply,
    ) -> None:
        self.decisions = decisions
        self.application = application

    def record(
        self, run_id: str, trigger: AllocationTrigger, draft: AllocationProposal,
        rewarded_sources: list[RewardedSource],
    ) -> AllocationDecision | None:
        return self.decisions.record(run_id, trigger, draft, rewarded_sources)

    def list(
        self, run_id: str, status: Literal["pending", "applied"] | None = None,
    ) -> list[AllocationDecision]:
        return self.decisions.list(run_id, status)

    def apply(self, run_id: str, decision_id: str) -> dict[str, Any]:
        result = self.application.apply(run_id, decision_id)
        return {
            "decision": result.decision.model_dump(mode="json"),
            "candidate_tasks": [item.model_dump(mode="json") for item in result.candidate_tasks],
            "agent_sessions": [item.model_dump(mode="json") for item in result.agent_sessions],
        }
