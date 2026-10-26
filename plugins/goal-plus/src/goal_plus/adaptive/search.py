"""Record-driven Adaptive Search: append, decide, execute, return."""

from collections.abc import Callable
import logging
from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
from goal_plus.adaptive.allocation import AllocationExecutor
from goal_plus.adaptive.steering import Steering
from goal_plus.ports.steering import SteeringHost
from goal_plus.domain.adaptive.models import AllocationDecisionStatus
from goal_plus.domain.adaptive.policy import (
    AllocationPolicySpec,
    SteeringPolicySpec,
    evaluate_allocation_policy,
)
from goal_plus.domain.verifier import VerifierRecord


class AdaptiveSearch:
    def __init__(self, *, allocation: AllocationExecutor, snapshots: AdaptiveSnapshotBuilder,
                 steering: Steering | None = None) -> None:
        self._allocation, self._snapshots = allocation, snapshots
        self._steering = steering

    def accept_verifier_record(self, record: VerifierRecord, *, policy: AllocationPolicySpec | None,
                               max_parallel: int,
                               execute: Callable[[str, str], object],
                               steering_host: SteeringHost | None = None,
                               steering_policy: SteeringPolicySpec | None = None) -> None:
        snapshot = (self._snapshots.append(record, policy=policy, max_parallel=max_parallel)
                    if policy is not None else None)
        if record.outcome == "failed":
            return
        plan = None
        if policy is not None:
            # Durable handoff ends this event. Execution recovery belongs to the Executor.
            if any(d.based_on_snapshot_revision == snapshot.snapshot_revision
                   for d in self._allocation.list(snapshot.run_id)):
                logging.getLogger(__name__).info("Adaptive branch=existing_allocation snapshot=%s", snapshot.snapshot_revision)
                return
            # Calculation errors are a refused decision, never a retry of inference.
            try:
                plan = evaluate_allocation_policy(snapshot, policy=policy)
            except Exception as exc:
                self._snapshots.state.write_policy_error(snapshot, f"{type(exc).__name__}: {exc}")
                return
        if plan is None:
            logging.getLogger(__name__).info("Adaptive branch=no_allocation record=%s", record.record_id)
            if self._steering is not None and steering_host is not None:
                self._steering.accept(record, host=steering_host, policy=steering_policy)
            return
        decision = self._allocation.record(snapshot, plan)
        logging.getLogger(__name__).info("Adaptive branch=allocation snapshot=%s", snapshot.snapshot_revision)
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
