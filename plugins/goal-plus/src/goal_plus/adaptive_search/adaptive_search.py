"""Record-driven Adaptive Search: append, decide, execute, return."""

from collections.abc import Callable
import logging
from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
from goal_plus.adaptive_search.allocation import AllocationExecutor
from goal_plus.domain.adaptive.models import AllocationDecisionStatus
from goal_plus.domain.adaptive.policy import (
    AllocationPolicySpec,
    evaluate_allocation_policy,
)
from goal_plus.domain.verifier import VerifierRecord


class AdaptiveSearch:
    def __init__(self, *, allocation: AllocationExecutor, snapshots: AdaptiveSnapshotBuilder) -> None:
        self._allocation, self._snapshots = allocation, snapshots

    def accept_verifier_record(self, record: VerifierRecord, *, policy: AllocationPolicySpec,
                               max_parallel: int,
                               execute: Callable[[str, str], object]) -> None:
        snapshot = self._snapshots.append(record, policy=policy,
            max_parallel=max_parallel)
        if record.outcome == "failed":
            return
        # Durable handoff ends this event. Execution recovery belongs to the Executor.
        if any(d.based_on_snapshot_revision == snapshot.snapshot_revision
               for d in self._allocation.list(snapshot.run_id)):
            return
        # Calculation errors are a refused decision, never a retry of inference.
        try:
            plan = evaluate_allocation_policy(snapshot, policy=policy)
        except Exception as exc:
            self._snapshots.state.write_policy_error(snapshot, f"{type(exc).__name__}: {exc}")
            return
        if plan is None:
            return
        decision = self._allocation.record(snapshot, plan)
        if decision.status not in {"applied", "superseded", "terminal_error"}:
            try:
                execute(decision.run_id, decision.decision_id)
            except Exception:
                # The durable Decision is the recovery input, never the Verifier task.
                logging.getLogger(__name__).exception("Allocation execution deferred: %s", decision.decision_id)

    def list_allocation_decisions(self, run_id: str, status: AllocationDecisionStatus | None = None):
        return self._allocation.list(run_id, status)

    def recover_allocation(self, run_id: str, *, cancel, dispatch):
        self._allocation.recover(run_id, cancel=cancel, dispatch=dispatch)

    def apply_allocation_decision(self, run_id: str, decision_id: str, *, cancel, dispatch):
        return self._allocation.apply(run_id, decision_id, cancel=cancel, dispatch=dispatch)
