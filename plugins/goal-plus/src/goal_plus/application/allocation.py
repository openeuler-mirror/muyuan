"""Persist deterministic Allocation plans without mutating candidate state."""

from typing import Literal

from goal_plus.application.verifier import verifier_subject
from goal_plus.domain.adaptive.models import (
    AllocationDecision,
    AllocationExpansion,
    AllocationPlan,
    ExpansionSource,
    compute_allocation_plan,
    compute_feature_view,
)
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.verifier import verifier_subject_identity
from goal_plus.ports.allocation import AllocationState
from goal_plus.ports.clock import Clock


class AllocationDecisions:
    def __init__(self, *, state: AllocationState, clock: Clock) -> None:
        self.state = state
        self.clock = clock

    def record(
        self, run_id: str, plan: AllocationPlan
    ) -> AllocationDecision | None:
        """Validate and durably record one deterministic policy result."""

        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            spec = frozen.spec.strategy.allocation
            if spec is None:
                raise RuntimeError("allocation decisions require strategy.allocation")
            if plan.policy_id != spec.policy:
                raise ValueError("allocation plan policy does not match FrozenSpec")
            snapshot = self.state.load_search_snapshot(
                run_id, plan.based_on_snapshot_revision
            )
            if (
                snapshot.run_id != run_id
                or snapshot.frozen_spec_id != frozen.frozen_spec_id
                or snapshot.policy_id != spec.policy
            ):
                raise ValueError("allocation plan snapshot does not match the run")
            expected = compute_allocation_plan(
                snapshot,
                compute_feature_view(snapshot, spec.features),
                policy=spec,
            )
            if expected is None or expected != plan:
                raise ValueError(
                    "allocation plan is not the deterministic result for its snapshot"
                )
            decision_id = f"allocation_{plan.plan_hash[:24]}"
            recorded = self.state.load_allocation_decisions(run_id)
            existing = next(
                (
                    item
                    for item in recorded
                    if item.decision_id == decision_id
                ),
                None,
            )
            if existing is not None:
                if existing.plan_hash != plan.plan_hash:
                    raise ValueError("allocation decision identity collision")
                return existing
            if any(item.status == "pending" for item in recorded):
                return None

            candidates = {
                item.candidate_id: item
                for item in self.state.read_candidates(run_id)
            }
            expansions: list[AllocationExpansion] = []
            for index, intent in enumerate(plan.expansions, start=1):
                source = candidates.get(intent.source_candidate_id)
                if source is None:
                    raise ValueError("allocation source candidate is unavailable")
                snapshot_candidate = next(
                    (
                        item
                        for item in snapshot.candidates
                        if item.candidate_id == intent.source_candidate_id
                    ),
                    None,
                )
                snapshot_node = next(
                    (
                        item
                        for item in (
                            snapshot_candidate.nodes if snapshot_candidate else []
                        )
                        if item.iteration == intent.source_iteration
                        and item.settlement_id == intent.source_settlement_id
                        and item.node_id == intent.source_node_id
                        and item.verifier_record_id
                        == intent.source_verifier_record_id
                    ),
                    None,
                )
                if snapshot_node is None:
                    raise ValueError(
                        "allocation source does not match its immutable score row"
                    )
                iteration = next(
                    (
                        item
                        for item in source.iterations
                        if item.iteration == intent.source_iteration
                        and item.settlement_id == intent.source_settlement_id
                    ),
                    None,
                )
                expected_node_id = (
                    f"{source.candidate_id}/iteration-{intent.source_iteration:04d}"
                )
                if (
                    iteration is None
                    or iteration.settlement_receipt is None
                    or intent.source_node_id != expected_node_id
                ):
                    raise ValueError(
                        "allocation source no longer matches settled Evidence"
                    )
                receipt = iteration.settlement_receipt
                if (
                    receipt.run_id != run_id
                    or receipt.candidate_id != source.candidate_id
                    or snapshot_node.verifier_subject_hash
                    != verifier_subject_identity(
                        verifier_subject(frozen, source, iteration)
                    )
                ):
                    raise ValueError(
                        "allocation source receipt does not match its score row"
                    )
                expansion_body = {
                    "plan_hash": plan.plan_hash,
                    "index": index,
                    "direction_key": intent.direction_key,
                    "source_node_id": intent.source_node_id,
                }
                expansions.append(
                    AllocationExpansion(
                        expansion_id=(
                            "expansion_"
                            + sha256_text(canonical_json(expansion_body))[:24]
                        ),
                        source=ExpansionSource(
                            candidate_id=source.candidate_id,
                            iteration=iteration.iteration,
                            settlement_id=receipt.settlement_id,
                            node_id=expected_node_id,
                            verifier_record_id=intent.source_verifier_record_id,
                            attempt_artifact_ref=receipt.attempt_artifact_ref,
                            attempt_artifact_hash=receipt.attempt_artifact_hash,
                            ledger_artifact_ref=receipt.ledger_artifact_ref,
                            allocation_depth=source.task.allocation_depth,
                        ),
                        direction_key=intent.direction_key,
                        rationale=intent.rationale,
                        worker_budget=intent.worker_budget,
                    )
                )
            decision = AllocationDecision(
                decision_id=decision_id,
                run_id=run_id,
                based_on_snapshot_revision=plan.based_on_snapshot_revision,
                feature_hash=plan.feature_hash,
                policy_id=plan.policy_id,
                plan_hash=plan.plan_hash,
                retire_candidate_ids=plan.retire_candidate_ids,
                expansions=expansions,
                reason_codes=plan.reason_codes,
                created_at=self.clock.timestamp(),
            )
            self.state.write_allocation_decision(decision)
            return decision

    def list(
        self,
        run_id: str,
        status: Literal[
            "pending", "applied", "superseded", "terminal_error"
        ]
        | None = None,
    ) -> list[AllocationDecision]:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.allocation is None:
            raise RuntimeError("allocation decisions require strategy.allocation")
        decisions = self.state.load_allocation_decisions(run_id)
        if status is not None:
            decisions = [item for item in decisions if item.status == status]
        return decisions
