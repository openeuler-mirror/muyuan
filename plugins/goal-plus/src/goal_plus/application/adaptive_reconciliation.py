"""Consume completed verifier records and coordinate Adaptive allocation."""

from __future__ import annotations

from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.verifier import VerifierWorkflow
from goal_plus.domain.adaptive.models import (
    AdaptiveReconciliationRecord,
    AllocationDecision,
    adaptive_reconciliation_identity,
    compute_allocation_plan,
    compute_feature_view,
)
from goal_plus.domain.verifier import VerifierRecord
from goal_plus.ports.adaptive import AdaptiveReconciliationState


class AdaptiveDecisionReconciliation:
    """Handle the sole event allowed to produce an AllocationDecision."""

    def __init__(
        self,
        *,
        state: AdaptiveReconciliationState,
        verifier: VerifierWorkflow,
        snapshots: AdaptiveSnapshotBuilder,
        decisions: AllocationDecisions,
    ) -> None:
        self.state = state
        self.verifier = verifier
        self.snapshots = snapshots
        self.decisions = decisions

    def next_pending_record(
        self,
        run_id: str,
        *,
        excluded_record_ids: set[str] | None = None,
    ) -> VerifierRecord | None:
        excluded = excluded_record_ids or set()
        for task in self.verifier.list_tasks(run_id):
            if (
                task.state != "completed"
                or task.allocation_policy_id is None
                or task.output_record_id is None
                or task.output_record_id in excluded
            ):
                continue
            reconciliation = self.state.load_adaptive_reconciliation(
                run_id, task.output_record_id
            )
            if reconciliation is not None and reconciliation.state == "completed":
                continue
            record = next(
                (
                    item
                    for item in self.verifier.list_records(run_id)
                    if item.record_id == task.output_record_id
                    and item.task_id == task.task_id
                ),
                None,
            )
            if record is None:
                raise RuntimeError(
                    f"completed verifier task {task.task_id} has no immutable output record"
                )
            return record
        return None

    def has_pending_record(
        self,
        run_id: str,
        *,
        excluded_record_ids: set[str] | None = None,
    ) -> bool:
        return (
            self.next_pending_record(
                run_id, excluded_record_ids=excluded_record_ids
            )
            is not None
        )

    def on_verifier_record_completed(
        self, record: VerifierRecord
    ) -> AllocationDecision | None:
        run_id = record.subject.run_id
        with self.state.lock_adaptive_reconciliation(run_id):
            reconciliation = self.state.load_adaptive_reconciliation(
                run_id, record.record_id
            )
            if reconciliation is not None:
                if (
                    reconciliation.reconciliation_id
                    != adaptive_reconciliation_identity(record.record_id)
                    or reconciliation.run_id != run_id
                    or reconciliation.task_id != record.task_id
                    or reconciliation.verifier_record_id != record.record_id
                ):
                    raise ValueError("adaptive reconciliation identity does not match")
                if reconciliation.state == "completed":
                    return (
                        self.state.load_allocation_decision(
                            run_id, reconciliation.decision_id
                        )
                        if reconciliation.decision_id is not None
                        else None
                    )

            run = self.state.read_run(run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            allocation = frozen.spec.strategy.allocation
            if allocation is None:
                raise RuntimeError(
                    "adaptive reconciliation requires the frozen allocation contract"
                )
            rules = allocation.verifier_rules()
            if (
                record.subject.frozen_spec_id != frozen.frozen_spec_id
                or record.allocation_policy_id != rules.policy_id
                or sorted(record.allocation_scores or {})
                != sorted(rules.score_names)
            ):
                raise ValueError(
                    "completed verifier record does not match the frozen allocation policy"
                )
            task = self.verifier.get_task(record.task_id)
            if (
                task.state != "completed"
                or task.output_record_id != record.record_id
                or task.subject != record.subject
            ):
                raise ValueError(
                    "allocation reconciliation requires a completed verifier task"
                )
            persisted = next(
                (
                    item
                    for item in self.verifier.list_records(
                        run_id,
                        candidate_id=record.subject.candidate_id,
                        iteration=record.subject.iteration,
                        settlement_id=record.subject.settlement_id,
                    )
                    if item.record_id == record.record_id
                    and item.task_id == record.task_id
                ),
                None,
            )
            if persisted != record:
                raise ValueError(
                    "allocation reconciliation requires a persisted verifier record"
                )
            candidate = next(
                (
                    item
                    for item in self.state.read_candidates(run_id)
                    if item.candidate_id == record.subject.candidate_id
                ),
                None,
            )
            iteration = (
                next(
                    (
                        item
                        for item in candidate.iterations
                        if item.iteration == record.subject.iteration
                        and item.settlement_id == record.subject.settlement_id
                    ),
                    None,
                )
                if candidate is not None
                else None
            )
            if (
                iteration is None
                or iteration.settlement_receipt != record.subject.settlement
            ):
                raise ValueError(
                    "allocation reconciliation requires the persisted settlement receipt"
                )

            if reconciliation is None:
                snapshot = self.snapshots.build(run_id)
                plan = compute_allocation_plan(
                    snapshot,
                    compute_feature_view(snapshot, allocation.features),
                    policy=allocation,
                )
                reconciliation = AdaptiveReconciliationRecord(
                    reconciliation_id=adaptive_reconciliation_identity(
                        record.record_id
                    ),
                    run_id=run_id,
                    task_id=record.task_id,
                    verifier_record_id=record.record_id,
                    state="prepared",
                    snapshot_revision=snapshot.snapshot_revision,
                    plan_hash=plan.plan_hash if plan is not None else None,
                )
                self.state.write_adaptive_reconciliation(reconciliation)
            else:
                snapshot = self.state.load_search_snapshot(
                    run_id, reconciliation.snapshot_revision
                )
                plan = compute_allocation_plan(
                    snapshot,
                    compute_feature_view(snapshot, allocation.features),
                    policy=allocation,
                )
                if (plan.plan_hash if plan is not None else None) != (
                    reconciliation.plan_hash
                ):
                    raise RuntimeError(
                        "prepared adaptive reconciliation plan is not deterministic"
                    )

            decision = (
                self.decisions.record(run_id, plan) if plan is not None else None
            )
            completed = reconciliation.model_copy(
                update={
                    "state": "completed",
                    "outcome": (
                        "decision_recorded" if decision is not None else "no_decision"
                    ),
                    "decision_id": (
                        decision.decision_id if decision is not None else None
                    ),
                }
            )
            self.state.write_adaptive_reconciliation(completed)
            return decision


__all__ = ["AdaptiveDecisionReconciliation"]
