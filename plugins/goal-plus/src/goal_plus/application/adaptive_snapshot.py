"""Single-writer append of terminal Verifier facts; no live search-state joins."""

from goal_plus.domain.adaptive.models import (
    ExpansionSource, SearchSnapshot, SnapshotCandidate, SnapshotNode, snapshot_identity,
)
from goal_plus.domain.adaptive.policy import AllocationPolicySpec
from goal_plus.domain.verifier import VerifierRecord, verifier_subject_identity
from goal_plus.ports.adaptive import AdaptiveSnapshotState


class AdaptiveSnapshotBuilder:
    def __init__(self, *, state: AdaptiveSnapshotState) -> None:
        self.state = state

    def append(self, record: VerifierRecord, *, policy: AllocationPolicySpec,
               max_parallel: int) -> SearchSnapshot:
        run_id = record.subject.run_id
        existing = self.state.snapshot_for_record(run_id, record.record_id)
        if existing is not None:
            return existing
        previous = self.state.latest_search_snapshot(run_id)
        if record.outcome == "completed" and not set(policy.required_score_keys).issubset(record.scores or {}):
            raise ValueError("verifier score keys do not satisfy allocation policy")
        if previous is not None and (
            previous.frozen_spec_id != record.subject.frozen_spec_id
            or previous.policy_id != policy.policy
            or record.queue_sequence != previous.verifier_cutoff_sequence + 1
        ):
            raise ValueError("verifier records must append in exact FIFO order")
        if previous is None and record.queue_sequence != 1:
            raise ValueError("first adaptive record must start the FIFO")
        candidates = {c.candidate_id: c.model_copy(deep=True) for c in previous.candidates} if previous else {}
        receipt = record.subject.settlement
        candidate = candidates.get(receipt.candidate_id)
        if candidate is None:
            candidate = SnapshotCandidate(
                candidate_id=receipt.candidate_id,
                allocation_depth=record.allocation_depth, endpoint=record.initial_artifact_ref,
                root_parent_id=record.parent_node_id, tip_node_id=None,
            )
            candidates[candidate.candidate_id] = candidate
        owner = (candidate.tip_node_id or candidate.root_parent_id) if candidate.owner_known else None
        base_known = receipt.attempt_base_artifact_ref == candidate.endpoint and candidate.endpoint is not None
        current = receipt.iteration > candidate.last_iteration
        scored = record.outcome == "completed"
        if not base_known or not current or not any(owner == n.node_id for c in candidates.values() for n in c.nodes):
            owner = None
        node_id = f"{receipt.candidate_id}/iteration-{receipt.iteration:04d}"
        if scored:
            candidate.nodes.append(SnapshotNode(
                candidate_id=receipt.candidate_id, iteration=receipt.iteration,
                node_id=node_id, parent_node_id=owner,
                edge_kind=("initial" if owner is None else "continuation" if owner.startswith(receipt.candidate_id + "/") else "allocation_expansion"),
                settlement_id=receipt.settlement_id, verifier_record_id=record.record_id,
                verifier_subject_hash=verifier_subject_identity(record.subject),
                quality_score=record.quality_score,
                allocation_scores={name: record.scores[name] for name in policy.required_score_keys},
                source=ExpansionSource(
                    candidate_id=receipt.candidate_id, iteration=receipt.iteration,
                    settlement_id=receipt.settlement_id, node_id=node_id, verifier_record_id=record.record_id,
                    attempt_artifact_ref=receipt.attempt_artifact_ref,
                    attempt_artifact_hash=receipt.attempt_artifact_hash,
                    ledger_artifact_ref=receipt.ledger_artifact_ref, allocation_depth=record.allocation_depth,
                ),
            ))
        candidate.nodes.sort(key=lambda n: (n.iteration, n.settlement_id, n.verifier_record_id))
        if current:
            if not scored:
                candidate.owner_known, candidate.tip_node_id = False, None
            elif receipt.rollback is None:
                candidate.owner_known, candidate.tip_node_id = True, node_id
            elif receipt.rollback.restored_to_iteration is None:
                candidate.owner_known = receipt.rollback.restored_artifact_ref == record.initial_artifact_ref
                candidate.tip_node_id = None
            else:
                restored = f"{receipt.candidate_id}/iteration-{receipt.rollback.restored_to_iteration:04d}"
                candidate.owner_known = any(n.node_id == restored and n.source is not None
                    and n.source.attempt_artifact_ref == receipt.rollback.restored_artifact_ref
                    for n in candidate.nodes)
                candidate.tip_node_id = restored if candidate.owner_known else None
            candidate.endpoint = receipt.ledger_artifact_ref
            candidate.last_iteration = receipt.iteration
        body = dict(
            schema_version=6, run_id=run_id, frozen_spec_id=record.subject.frozen_spec_id,
            policy_id=policy.policy, max_parallel=max_parallel,
            verifier_cutoff_sequence=record.queue_sequence,
            candidates=[c.model_dump(mode="json") for c in sorted(candidates.values(), key=lambda c: c.candidate_id)],
            verifier_record_ids=[*(previous.verifier_record_ids if previous else []), record.record_id],
        )
        revision, digest = snapshot_identity(body)
        snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)
        self.state.write_search_snapshot(snapshot)
        return snapshot
