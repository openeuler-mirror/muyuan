from goal_plus.application.selection import _QualityScoreSource
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.selection import SelectionOption, SelectionSubjectKey
from goal_plus.domain.settlement import SettlementEvidence, SettlementReceipt
from goal_plus.domain.verifier import (
    VerifierInvocationRecord,
    VerifierRecord,
    VerifierSubject,
    VerifierTask,
    verifier_subject_identity,
)


def _subject() -> VerifierSubject:
    artifact = ArtifactRef(kind="git_commit", id="a" * 40, provider="git_worktree")
    return VerifierSubject(
        frozen_spec_id="frozen_1",
        settlement=SettlementReceipt(
            settlement_id="settlement_1",
            run_id="run_1",
            candidate_id="c001",
            iteration=1,
            attempt_artifact_ref=artifact,
            attempt_artifact_hash="b" * 64,
            evidence=SettlementEvidence(
                metric_name="score",
                hard_score=0.0,
                process_passed=True,
                artifact_clean=True,
            ),
            disposition="keep",
            post_settlement_artifact_ref=artifact,
            ledger_artifact_ref=artifact,
            created_at="2026-01-01T00:00:00Z",
        ),
    )


def _selection_option(subject: VerifierSubject) -> SelectionOption:
    return SelectionOption(
        subject=SelectionSubjectKey(
            candidate_id=subject.candidate_id,
            iteration=subject.iteration,
            settlement_id=subject.settlement_id,
            artifact_hash=subject.settlement.attempt_artifact_hash,
            verifier_subject_hash=verifier_subject_identity(subject),
        ),
        hard_score=subject.settlement.evidence.hard_score,
        original_index=0,
    )


def _completed_assessment(
    token: str,
    subject: VerifierSubject,
    quality_score: float,
    *,
    allocation_policy_id: str | None = None,
    allocation_scores: dict[str, float] | None = None,
) -> tuple[VerifierTask, VerifierRecord]:
    record_id = f"record_{token}"
    score_names = list(allocation_scores or {})
    task = VerifierTask(
        task_id=f"verifier_{token * 24}",
        queue_sequence=1,
        subject=subject,
        allocation_policy_id=allocation_policy_id,
        allocation_score_names=score_names,
        state="completed",
        output_record_id=record_id,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
    )
    record = VerifierRecord(
        record_id=record_id,
        task_id=task.task_id,
        subject=subject,
        allocation_policy_id=allocation_policy_id,
        quality_score=quality_score,
        allocation_scores=allocation_scores,
        rationale="selection test record",
        invocation=VerifierInvocationRecord(
            invocation_id=f"invocation_{token}",
            backend="codex",
            model="test-model",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            elapsed_seconds=1,
        ),
        created_at="2026-01-01T00:00:01Z",
    )
    return task, record


def test_quality_source_uses_the_complete_verifier_task_identity() -> None:
    subject = _subject()
    minimal = _completed_assessment("a", subject, 0.1)
    adaptive = _completed_assessment(
        "b",
        subject,
        0.9,
        allocation_policy_id="priority_replace/v1",
        allocation_scores={"progress_reward": 0.4, "allocation_utility": 0.8},
    )
    source = _QualityScoreSource(
        allocation_policy_id="priority_replace/v1",
        allocation_score_names=["progress_reward", "allocation_utility"],
        tasks=[minimal[0], adaptive[0]],
        records=[minimal[1], adaptive[1]],
    )

    result = source.resolve(_selection_option(subject))

    assert result.state == "value"
    assert result.value == 0.9


def test_quality_source_rejects_changed_artifact_or_evidence_identity() -> None:
    original = _subject()
    task, record = _completed_assessment("a", original, 0.8)
    source = _QualityScoreSource(
        allocation_policy_id=None,
        allocation_score_names=[],
        tasks=[task],
        records=[record],
    )
    replacement = ArtifactRef(
        kind="git_commit", id="c" * 40, provider="git_worktree"
    )
    changed_artifact = original.model_copy(
        update={
            "settlement": original.settlement.model_copy(
                update={"attempt_artifact_ref": replacement}
            )
        }
    )
    changed_evidence = original.model_copy(
        update={
            "settlement": original.settlement.model_copy(
                update={
                    "evidence": original.settlement.evidence.model_copy(
                        update={"hard_score": 0.25}
                    )
                }
            )
        }
    )

    assert source.resolve(_selection_option(changed_artifact)).state == "unavailable"
    assert source.resolve(_selection_option(changed_evidence)).state == "unavailable"


def test_quality_source_requires_the_task_output_record_pointer() -> None:
    subject = _subject()
    task, record = _completed_assessment("a", subject, 0.8)
    source = _QualityScoreSource(
        allocation_policy_id=None,
        allocation_score_names=[],
        tasks=[task.model_copy(update={"output_record_id": "record_wrong"})],
        records=[record],
    )

    assert source.resolve(_selection_option(subject)).state == "unavailable"
