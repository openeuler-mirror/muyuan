"""Relay completed verifier score rows into immutable search snapshots."""

from __future__ import annotations

from goal_plus.application.verifier import VerifierWorkflow
from goal_plus.domain.adaptive.models import (
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    snapshot_identity,
)
from goal_plus.domain.verifier import (
    VerifierRecord,
    verifier_subject_identity,
)
from goal_plus.ports.adaptive import AdaptiveSnapshotState


class AdaptiveSnapshotBuilder:
    def __init__(
        self,
        *,
        state: AdaptiveSnapshotState,
        verifier: VerifierWorkflow,
    ) -> None:
        self.state = state
        self.verifier = verifier

    def build(self, run_id: str) -> SearchSnapshot:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        allocation = frozen.spec.strategy.allocation
        if allocation is None:
            raise RuntimeError(
                "adaptive snapshot requires verifier allocation scores and allocation"
            )
        rules = allocation.verifier_rules()
        records = [
            item
            for item in self.verifier.list_records(run_id)
            if item.subject.frozen_spec_id == frozen.frozen_spec_id
            and item.allocation_policy_id == rules.policy_id
            and sorted(item.allocation_scores or {}) == sorted(rules.score_names)
        ]
        records_by_candidate: dict[str, list[VerifierRecord]] = {}
        for record in records:
            records_by_candidate.setdefault(
                record.subject.candidate_id, []
            ).append(record)
        candidates: list[SnapshotCandidate] = []
        for candidate in sorted(
            self.state.read_candidates(run_id), key=lambda item: item.candidate_id
        ):
            nodes: list[SnapshotNode] = []
            for record in sorted(
                records_by_candidate.get(candidate.candidate_id, []),
                key=lambda item: (
                    item.subject.iteration,
                    item.subject.settlement_id,
                    item.record_id,
                ),
            ):
                nodes.append(
                    SnapshotNode(
                        candidate_id=candidate.candidate_id,
                        iteration=record.subject.iteration,
                        settlement_id=record.subject.settlement_id,
                        node_id=(
                            f"{record.subject.candidate_id}/iteration-"
                            f"{record.subject.iteration:04d}"
                        ),
                        verifier_record_id=record.record_id,
                        verifier_subject_hash=verifier_subject_identity(
                            record.subject
                        ),
                        quality_score=record.quality_score,
                        allocation_scores=dict(record.allocation_scores or {}),
                    )
                )
            candidates.append(
                SnapshotCandidate(
                    candidate_id=candidate.candidate_id,
                    allocation_eligibility=candidate.allocation_eligibility,
                    allocation_depth=candidate.task.allocation_depth,
                    nodes=nodes,
                )
            )
        history = [
            item.decision_id
            for item in self.state.load_allocation_decisions(run_id)
        ]
        body = {
            "schema_version": 2,
            "run_id": run_id,
            "frozen_spec_id": frozen.frozen_spec_id,
            "policy_id": allocation.policy,
            "max_parallel": frozen.spec.budget.max_parallel,
            "max_candidates": frozen.spec.budget.max_candidates,
            "candidates_total": max(run.candidates_total, len(candidates)),
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "historical_decision_ids": history,
        }
        revision, input_hash = snapshot_identity(body)
        snapshot = SearchSnapshot(
            **body, snapshot_revision=revision, input_hash=input_hash
        )
        self.state.write_search_snapshot(snapshot)
        return snapshot


__all__ = ["AdaptiveSnapshotBuilder"]
