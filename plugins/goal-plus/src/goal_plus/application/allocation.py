"""Validate and record Decisions; reconcile only Allocation-owned fields."""

from typing import Literal

from goal_plus.domain.adaptive.models import (
    AllocationDecision,
    AllocationProposal,
    AllocationReplacement,
    AllocationTrigger,
    ExpansionSource,
    RewardedSource,
)
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.allocation import (
    AllocationCandidateProjection,
    AllocationIterationProjection,
    AllocationState,
)
from goal_plus.ports.clock import Clock
from goal_plus.ports.state import RunRecord


class AllocationDecisions:
    def __init__(self, *, state: AllocationState, clock: Clock) -> None:
        self.state = state
        self.clock = clock

    @staticmethod
    def _advance_decision_index(
        run: RunRecord,
        decisions: list[AllocationDecision],
    ) -> None:
        indices: list[int] = []
        for decision in decisions:
            prefix, separator, suffix = decision.decision_id.rpartition("_")
            if separator and prefix == "allocation" and suffix.isdigit():
                indices.append(int(suffix))
        if indices:
            run.next_allocation_decision_index = max(
                run.next_allocation_decision_index,
                max(indices) + 1,
            )

    def _reconcile_decision_locked(
        self,
        run: RunRecord,
        decision: AllocationDecision,
    ) -> None:
        """Idempotently project one durable Decision onto mutable lane records."""

        records = self.state.read_candidates(run.run_id)
        by_id = {item.candidate_id: item for item in records}
        trigger_record = by_id.get(decision.trigger_candidate_id)
        trigger_iteration = (
            next(
                (
                    item
                    for item in trigger_record.iterations
                    if item.iteration == decision.trigger_iteration
                ),
                None,
            )
            if trigger_record is not None
            else None
        )
        if (
            trigger_iteration is None
            or trigger_iteration.git_head != decision.trigger_commit
        ):
            raise ValueError("allocation Decision trigger no longer matches settlement")

        changed_ids = {decision.trigger_candidate_id}
        for replacement in decision.replacements:
            retired = by_id.get(replacement.retire_candidate_id)
            if retired is None:
                raise ValueError("allocation Decision retire candidate is unavailable")
            if retired.retired_by_decision_id not in {None, decision.decision_id}:
                raise ValueError("candidate was retired by another allocation Decision")
            retired.allocation_eligibility = "retired"
            retired.retired_by_decision_id = decision.decision_id
            changed_ids.add(retired.candidate_id)

        for candidate_id in changed_ids:
            record = by_id[candidate_id]
            is_trigger = candidate_id == decision.trigger_candidate_id
            self.state.write_candidate_projection(
                run.run_id,
                AllocationCandidateProjection(
                    candidate_id=record.candidate_id,
                    eligibility=record.allocation_eligibility,
                    retired_by_decision_id=record.retired_by_decision_id,
                    iterations=(
                        (
                            AllocationIterationProjection(
                                iteration=trigger_iteration.iteration,
                                decision_id=decision.decision_id,
                                error=None,
                            ),
                        )
                        if is_trigger
                        else ()
                    ),
                    report_decision=decision if is_trigger else None,
                ),
            )
        self.state.write_decision_index(run.run_id, run.next_allocation_decision_index)

    def _create_decision_locked(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        trigger: AllocationTrigger,
        draft: AllocationProposal,
        rewarded_sources: list[RewardedSource],
    ) -> AllocationDecision | None:
        decisions = self.state.load_allocation_decisions(run.run_id)
        self._advance_decision_index(run, decisions)
        existing = next(
            (
                decision
                for decision in decisions
                if decision.schedule_task_id == trigger.schedule_task_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.trigger_candidate_id != trigger.candidate_id
                or existing.trigger_iteration != trigger.iteration
                or existing.trigger_commit != trigger.commit
            ):
                raise ValueError("existing Decision does not match allocation trigger")
            self._reconcile_decision_locked(run, existing)
            return existing
        if frozen.spec.strategy.search_scheduler is None:
            return None

        records = self.state.read_candidates(run.run_id)
        by_id = {item.candidate_id: item for item in records}
        trigger_record = by_id.get(trigger.candidate_id)
        trigger_iteration = (
            next(
                (
                    item
                    for item in trigger_record.iterations
                    if item.iteration == trigger.iteration
                ),
                None,
            )
            if trigger_record is not None
            else None
        )
        if (
            trigger_iteration is None
            or trigger_iteration.settlement_id != trigger.settlement_id
            or trigger_iteration.git_head != trigger.commit
        ):
            raise ValueError("allocation trigger does not match settled iteration")
        reserved = set(trigger.reserved_candidate_ids)
        rewarded_by_node = {
            (source.candidate_id, source.iteration): source
            for source in rewarded_sources
        }
        replacements: list[AllocationReplacement] = []
        for item in draft.replacements:
            if item.new_candidate_id not in reserved:
                raise ValueError("scheduler used an unreserved candidate id")
            retired = by_id.get(item.retire_candidate_id)
            source_record = by_id.get(item.source_candidate_id)
            if retired is None or retired.allocation_eligibility != "eligible":
                raise ValueError(
                    "scheduler selected an ineligible retirement candidate"
                )
            if source_record is None:
                raise ValueError("scheduler selected an unavailable source candidate")
            source_iteration = next(
                (
                    iteration
                    for iteration in source_record.iterations
                    if iteration.iteration == item.source_iteration
                ),
                None,
            )
            rewarded_source = rewarded_by_node.get(
                (item.source_candidate_id, item.source_iteration)
            )
            expected_node_id = (
                f"{source_record.candidate_id}/iteration-{item.source_iteration:04d}"
            )
            if (
                source_iteration is None
                or source_iteration.git_head is None
                or source_iteration.artifact_hash is None
                or item.source_node_id != expected_node_id
                or rewarded_source is None
                or rewarded_source.node_id != expected_node_id
                or rewarded_source.settlement_id != source_iteration.settlement_id
            ):
                raise ValueError(
                    "scheduler source node is not bound to a completed reward"
                )
            source_depth = source_record.task.allocation_depth
            replacements.append(
                AllocationReplacement(
                    retire_candidate_id=item.retire_candidate_id,
                    new_candidate_id=item.new_candidate_id,
                    source=ExpansionSource(
                        candidate_id=source_record.candidate_id,
                        iteration=source_iteration.iteration,
                        node_id=expected_node_id,
                        settled_commit=(
                            source_iteration.workspace_git_head_after_settlement
                            or source_iteration.ledger_git_head
                            or source_iteration.git_head
                        ),
                        score=source_iteration.score,
                        artifact_hash=source_iteration.artifact_hash,
                        allocation_depth=source_depth,
                    ),
                    rationale=item.rationale,
                    exploration_direction=item.exploration_direction,
                    worker_budget=item.worker_budget,
                )
            )

        eligible_count = sum(
            item.allocation_eligibility == "eligible" for item in records
        )
        projected_eligible = (
            eligible_count
            - len({item.retire_candidate_id for item in replacements})
            + len(replacements)
        )
        if projected_eligible > frozen.spec.budget.max_parallel:
            raise ValueError("scheduler decision would exceed budget.max_parallel")

        decision_id = f"allocation_{run.next_allocation_decision_index:04d}"
        run.next_allocation_decision_index += 1
        decision = AllocationDecision(
            decision_id=decision_id,
            run_id=run.run_id,
            schedule_task_id=trigger.schedule_task_id,
            trigger_candidate_id=trigger.candidate_id,
            trigger_iteration=trigger.iteration,
            trigger_commit=trigger.commit,
            rationale=draft.rationale,
            replacements=replacements,
            created_at=self.clock.timestamp(),
        )
        self.state.write_allocation_decision(decision)
        self._reconcile_decision_locked(run, decision)
        return decision

    def record(
        self,
        run_id: str,
        trigger: AllocationTrigger,
        draft: AllocationProposal,
        rewarded_sources: list[RewardedSource],
    ) -> AllocationDecision | None:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            if not trigger.reserved_candidate_ids:
                return None
            try:
                return self._create_decision_locked(
                    run,
                    frozen,
                    trigger,
                    draft,
                    rewarded_sources,
                )
            except Exception as exc:
                record = self.state.read_candidate(
                    run_id,
                    trigger.candidate_id,
                )
                trigger_iteration = next(
                    item
                    for item in record.iterations
                    if item.iteration == trigger.iteration
                )
                trigger_iteration.allocation_decision_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                self.state.write_candidate_projection(
                    run_id,
                    AllocationCandidateProjection(
                        candidate_id=record.candidate_id,
                        eligibility=record.allocation_eligibility,
                        retired_by_decision_id=record.retired_by_decision_id,
                        iterations=(
                            AllocationIterationProjection(
                                iteration=trigger_iteration.iteration,
                                decision_id=trigger_iteration.allocation_decision_id,
                                error=trigger_iteration.allocation_decision_error,
                            ),
                        ),
                    ),
                )
                return None

    def list(
        self,
        run_id: str,
        status: Literal["pending", "applied"] | None = None,
    ) -> list[AllocationDecision]:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.search_scheduler is None:
            raise RuntimeError("allocation decisions require strategy.search_scheduler")
        decisions = self.state.load_allocation_decisions(run_id)
        if status is not None:
            decisions = [item for item in decisions if item.status == status]
        return decisions
