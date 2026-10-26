"""Allocation public execution interface within Adaptive Search."""

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.allocation_apply import AllocationApply


class AllocationExecutor:
    def __init__(self, decisions: AllocationDecisions, application: AllocationApply) -> None:
        self._decisions, self.application = decisions, application

    def record(self, snapshot, plan):
        return self._decisions.record(snapshot, plan)

    def list(self, run_id, status=None):
        return self._decisions.list(run_id, status)

    def recover(self, run_id, *, cancel, dispatch):
        self.application.recover(run_id, cancel=cancel, dispatch=dispatch)

    def apply(self, run_id, decision_id, *, cancel, dispatch):
        result = self.application.apply(run_id, decision_id, cancel=cancel, dispatch=dispatch)
        return {"decision": result.decision.model_dump(mode="json"),
                "candidate_tasks": [item.model_dump(mode="json") for item in result.candidate_tasks],
                "agent_sessions": [item.model_dump(mode="json") for item in result.agent_sessions]}
