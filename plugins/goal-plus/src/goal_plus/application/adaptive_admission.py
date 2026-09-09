"""Reconcile allocation decisions and enforce candidate admission without execution."""

from typing import Literal
from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.scheduler_lanes import SchedulerLanes
from goal_plus.domain.adaptive.models import (
    AllocationDecision,
    AllocationTrigger,
    RewardedSource,
    ScheduleTask,
)
from goal_plus.ports.candidate_host import CandidateHostSource
from goal_plus.ports.state import CandidateRecord


class AdaptiveAdmission:
    def __init__(
        self,
        source: CandidateHostSource,
        lanes: SchedulerLanes,
        decisions: AllocationDecisions,
    ) -> None:
        self.source, self.lanes, self.decisions = source, lanes, decisions

    def assert_candidate_execution_allowed(
        self,
        record: CandidateRecord,
        operation: Literal[
            "recover",
            "redispatch",
            "start_session",
            "continue_session",
            "run_verifier",
        ],
    ) -> None:
        candidate_id = record.candidate_id
        if record.allocation_eligibility != "eligible":
            messages = {
                "recover": f"cannot recover retired candidate {candidate_id}",
                "redispatch": f"cannot redispatch retired candidate {candidate_id}",
                "start_session": f"cannot start session for retired candidate {candidate_id}",
                "continue_session": f"cannot continue retired candidate {candidate_id}",
                "run_verifier": (
                    f"candidate {candidate_id} was retired by scheduler allocation"
                ),
            }
            raise RuntimeError(messages[operation])
        barrier = self.lanes.candidate_execution_barrier(record)
        if barrier is not None:
            raise RuntimeError(
                f"cannot {operation} candidate {candidate_id}: {barrier}"
            )

    def reconcile_candidate_execution_allowed(
        self,
        run_id: str,
        candidate_id: str,
        operation: Literal[
            "recover",
            "redispatch",
            "start_session",
            "continue_session",
            "run_verifier",
        ],
    ) -> None:
        """Reconcile completed decisions, then enforce the lane barrier."""

        self.record_pending_decisions(run_id)
        self.assert_candidate_execution_allowed(
            self.source.read_candidate(run_id, candidate_id),
            operation,
        )

    def record_pending_decisions(self, run_id: str) -> list[AllocationDecision]:
        decisions: list[AllocationDecision] = []
        for task in self.lanes.pending_decision_tasks(run_id):
            decision = self._record_decision(task)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def _record_decision(self, task: ScheduleTask) -> AllocationDecision | None:
        if task.output is None or task.output.decision is None:
            return None
        trigger = AllocationTrigger(
            schedule_task_id=task.task_id,
            settlement_id=task.settlement_id,
            candidate_id=task.candidate_id,
            iteration=task.iteration,
            commit=task.attempt_commit,
            reserved_candidate_ids=task.reserved_candidate_ids,
        )
        rewarded_sources = [
            RewardedSource(
                schedule_task_id=reward.schedule_task_id,
                candidate_id=reward.candidate_id,
                iteration=reward.iteration,
                node_id=reward.node_id,
                settlement_id=reward.settlement_id,
            )
            for reward in self.lanes.list_rewards(task.run_id)
        ]
        decision = self.decisions.record(
            task.run_id,
            trigger,
            task.output.decision,
            rewarded_sources,
        )
        if decision is not None:
            self.lanes.bind_decision(
                task,
                decision.decision_id,
                [item.new_candidate_id for item in decision.replacements],
            )
        return decision
