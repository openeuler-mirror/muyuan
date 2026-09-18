import pytest

from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
from goal_plus.domain.adaptive.policy import SweSearchUctPolicySpec
from goal_plus.domain.settlement import RollbackReceipt
from goal_plus.domain.verifier import VerifierRecord, verifier_score_contract
from tests.test_llm_verifier import subject, invocation


class State:
    def __init__(self):
        self.snapshots = []

    def snapshot_for_record(self, run_id, record_id):
        return next((s for s in self.snapshots if s.verifier_record_ids[-1] == record_id), None)

    def latest_search_snapshot(self, run_id):
        return self.snapshots[-1] if self.snapshots else None

    def write_search_snapshot(self, snapshot):
        self.snapshots.append(snapshot)


def record(sequence, *, base=None, failed=False, rollback_to=None):
    original = subject(settlement_id=f"settlement_{sequence}")
    receipt = original.settlement
    initial = receipt.attempt_base_artifact_ref
    attempt = receipt.attempt_artifact_ref.model_copy(update={"id": f"attempt_{sequence}"})
    ledger = attempt.model_copy(update={"id": f"ledger_{sequence}"})
    updates = dict(iteration=sequence, attempt_base_artifact_ref=base or initial,
                   attempt_artifact_ref=attempt, post_settlement_artifact_ref=attempt,
                   ledger_artifact_ref=ledger)
    if rollback_to is not None:
        restored = attempt.model_copy(update={"id": f"attempt_{rollback_to}"})
        updates.update(disposition="discard", post_settlement_artifact_ref=restored,
                       rollback=RollbackReceipt(target_artifact_ref=restored,
                           restored_artifact_ref=restored, restored_to_iteration=rollback_to,
                           restored_artifact_hash="restored"))
    exact = original.model_copy(update={"settlement": receipt.model_copy(update=updates)})
    contract = verifier_score_contract(["progress_reward", "allocation_utility"])
    fields = dict(record_id=f"record_{sequence}", task_id=f"task_{sequence}", subject=exact,
                  queue_sequence=sequence,
                  score_contract_hash=contract.contract_hash, initial_artifact_ref=initial,
                  created_at="2026-01-01T00:00:00Z")
    if failed:
        return VerifierRecord(**fields, outcome="failed", error="attempt limit exhausted")
    return VerifierRecord(**fields, quality_score=0.8, invocation=invocation(),
                          scores={"progress_reward": 0.5, "allocation_utility": 0.8},
                          score_rationales={"quality_score": "valid", "progress_reward": "progress",
                                            "allocation_utility": "useful"})


def append(builder, output):
    return builder.append(output, policy=SweSearchUctPolicySpec(policy="swe_search_uct/v1"),
                          max_parallel=1)


def test_failed_assessment_breaks_lineage_without_losing_later_scores():
    state = State()
    builder = AdaptiveSnapshotBuilder(state=state)
    first = record(1)
    before = append(builder, first).model_dump()
    failed = record(2, base=first.subject.settlement.ledger_artifact_ref, failed=True)
    gap = append(builder, failed)
    assert gap.candidates[0].tip_node_id is None
    third = record(3, base=failed.subject.settlement.ledger_artifact_ref)
    after = append(builder, third)
    assert [n.iteration for n in after.candidates[0].nodes] == [1, 3]
    assert after.candidates[0].nodes[-1].parent_node_id is None
    assert state.snapshots[0].model_dump() == before
    assert append(builder, third) == after
    assert len(state.snapshots) == 3


def test_rollback_continuation_attaches_to_restored_node_not_discarded_attempt():
    builder = AdaptiveSnapshotBuilder(state=State())
    first = record(1)
    append(builder, first)
    second = record(2, base=first.subject.settlement.ledger_artifact_ref, rollback_to=1)
    rollback = append(builder, second)
    assert rollback.candidates[0].tip_node_id == "c001/iteration-0001"
    third = record(3, base=second.subject.settlement.ledger_artifact_ref)
    after = append(builder, third)
    assert after.candidates[0].nodes[-1].parent_node_id == "c001/iteration-0001"


def test_snapshot_rejects_out_of_order_and_does_not_join_unknown_base():
    builder = AdaptiveSnapshotBuilder(state=State())
    first = record(1)
    append(builder, first)
    with pytest.raises(ValueError, match="FIFO"):
        append(builder, record(3))
    snapshot = append(builder, record(2))
    assert len(snapshot.candidates[0].nodes) == 2
    assert snapshot.candidates[0].nodes[-1].parent_node_id is None
    assert snapshot.candidates[0].tip_node_id == "c001/iteration-0002"


def test_snapshot_consumes_required_keys_and_leaves_other_scores_to_their_consumers():
    output = record(1)
    output = VerifierRecord.model_validate({**output.model_dump(),
        "scores": {**output.scores, "maintainability": 0.9},
        "score_rationales": {**output.score_rationales, "maintainability": "clear"}})
    state = State()
    builder = AdaptiveSnapshotBuilder(state=state)
    mismatched = VerifierRecord.model_validate({**output.model_dump(),
        "scores": {key: value for key, value in output.scores.items() if key != "progress_reward"},
        "score_rationales": {key: value for key, value in output.score_rationales.items() if key != "progress_reward"}})
    with pytest.raises(ValueError, match="score keys"):
        append(builder, mismatched)
    assert state.snapshots == []
    snapshot = append(builder, output)
    assert set(snapshot.candidates[0].nodes[0].allocation_scores) == {"progress_reward", "allocation_utility"}
    assert output.scores["maintainability"] == 0.9
