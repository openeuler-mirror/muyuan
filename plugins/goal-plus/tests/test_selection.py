from dataclasses import replace

import pytest

from goal_plus.application.selection import _QualityScoreSource
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.selection import SelectionOption, SelectionSubjectKey
from goal_plus.domain.settlement import SettlementEvidence, SettlementReceipt
from goal_plus.domain.verifier import (
    VerifierInput,
    VerifierInvocationRecord,
    VerifierRecord,
    VerifierScoreContract,
    VerifierSubject,
    VerifierTask,
    verifier_score_contract,
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
    scores: dict[str, float] | None = None,
) -> tuple[VerifierTask, VerifierRecord]:
    record_id = f"record_{token}"
    score_contract = (
        verifier_score_contract(["progress_reward", "allocation_utility"])
        if scores is not None
        else VerifierScoreContract.from_rules()
    )
    task = VerifierTask(
        task_id=f"verifier_{token * 24}",
        queue_sequence=1,
        subject=subject,
        score_contract=score_contract,
        input=VerifierInput(objective="selection", candidate_acceptance=[], hypothesis="",
                            artifact_diff="", agent_harness="codex"),
        state="completed",
        output_record_id=record_id,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
    )
    record = VerifierRecord(
        record_id=record_id,
        task_id=task.task_id,
        queue_sequence=task.queue_sequence,
        subject=subject,
        score_contract_hash=score_contract.contract_hash,
        quality_score=quality_score,
        scores=scores,
        score_rationales={
            name: "selection test record"
            for name in {"quality_score", *(scores or {})}
        },
        invocation=VerifierInvocationRecord(
            invocation_id=f"invocation_{token}",
            agent_harness="codex",
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
        scores={"progress_reward": 0.4, "allocation_utility": 0.8},
    )
    source = _QualityScoreSource(
        tasks=[adaptive[0]],
        records=[adaptive[1]],
    )

    result = source.resolve(_selection_option(subject))

    assert result.state == "value"
    assert result.value == 0.9


def test_quality_source_rejects_changed_artifact_or_evidence_identity() -> None:
    original = _subject()
    task, record = _completed_assessment("a", original, 0.8)
    source = _QualityScoreSource(
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


def test_quality_source_waits_for_unadmitted_submission() -> None:
    source = _QualityScoreSource(tasks=[], records=[])
    assert source.resolve(_selection_option(_subject())).state == "pending"


def test_quality_source_requires_the_task_output_record_pointer() -> None:
    subject = _subject()
    task, record = _completed_assessment("a", subject, 0.8)
    source = _QualityScoreSource(
        tasks=[task.model_copy(update={"output_record_id": "record_wrong"})],
        records=[record],
    )

    assert source.resolve(_selection_option(subject)).state == "unavailable"


@pytest.fixture
def quality_run(tmp_path):
    from goal_plus.application.verifier import VerifierWorkflow
    from goal_plus.domain.selection import SelectionSpec
    from goal_plus.infrastructure.clock import SystemClock
    from goal_plus.infrastructure.state.verifier import FileVerifierState
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for

    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\nfrom initial_program import VALUE\n"
        "print(json.dumps({'combined_score': float(VALUE)}))\n"
    )
    spec = spec_for(project, max_parallel=2)
    spec.workspace.provider = "git_worktree"
    spec.strategy.selection = SelectionSpec(
        ranking_keys=["quality_score", "hard_score"], ranking_tolerance=[0.05, 0.0],
    )
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    candidates = runtime.start_batch(run_id, plan.plan_id)
    reports = []
    for candidate, score in zip(candidates, [1, 3]):
        (candidate.workspace / "initial_program.py").write_text(f"VALUE = {score}\n")
        reports.append(runtime.run_verifier(run_id, candidate.candidate_id))
    workflow = VerifierWorkflow(FileVerifierState(runtime.root_dir), SystemClock())
    return runtime, run_id, candidates, reports, workflow


@pytest.mark.parametrize("outcome", ["quality", "terminal_score", "all_scores_failed", "revalidation_failed"])
@pytest.mark.todo(reason="adaptive_check_later")
@pytest.mark.xfail(reason="mark.todo.adaptive_check_later", strict=True)
def test_selection_waits_for_quality_and_preserves_hard_evidence(quality_run, monkeypatch, outcome):
    from goal_plus.bootstrap import build_internal_agent_work
    from tests.test_llm_verifier import invocation

    runtime, run_id, candidates, reports, workflow = quality_run
    before = [runtime.list_iterations(run_id, item.candidate_id) for item in candidates]
    with pytest.raises(RuntimeError, match="waiting for required verifier scores"):
        runtime.select(run_id)
    assert runtime.search_run.state.read_run(run_id).selected_candidate_id is None
    assert build_internal_agent_work(runtime.root_dir).inspect(run_id).accepts_work
    assert len(workflow.list_tasks(run_id)) == 2
    while task := workflow.claim_task("quality-test", 60, run_id=run_id):
        if outcome == "all_scores_failed" or (outcome == "terminal_score" and task.candidate_id == "c001"):
            workflow.fail_task(task.task_id, "controlled terminal inference failure", retryable=False,
                               worker_id=task.lease.worker_id, attempt=task.attempts)
        else:
            workflow.complete_task(task.task_id, {
                "quality_score": 0.9 if task.candidate_id == "c001" else 0.2,
                "scores": None, "score_rationales": {"quality_score": "Controlled quality assessment"},
            }, invocation(), worker_id=task.lease.worker_id, attempt=task.attempts)
    if outcome == "all_scores_failed":
        with pytest.raises(RuntimeError, match="no candidate satisfies"):
            runtime.select(run_id)
        assert runtime.search_run.state.read_run(run_id).selected_candidate_id is None
    else:
        if outcome == "revalidation_failed":
            artifact_state = runtime.selection.artifacts.candidate_artifact_state

            def changed_state(run, frozen, record):
                state = artifact_state(run, frozen, record)
                return replace(state, artifact_hash="f" * 64) if record.candidate_id == "c001" else state

            monkeypatch.setattr(runtime.selection.artifacts, "candidate_artifact_state", changed_state)
            selecting = runtime.selection.select(run_id)
            assert next(selecting).candidate_id == "c001"
            with pytest.raises(StopIteration) as completed:
                selecting.send(reports[0].model_copy(update={"process_passed": False, "aggregate_score": None}))
            result = completed.value.value
            assert len(result["selection_trace"]) == 2
        else:
            result = runtime.select(run_id)
        assert result["selected_candidate_id"] == ("c001" if outcome == "quality" else "c002")
        assert result["selected_quality_score"] == (0.9 if outcome == "quality" else 0.2)
    assert [runtime.list_iterations(run_id, item.candidate_id) for item in candidates] == before
    assert len(workflow.list_tasks(run_id)) == 2


def test_quality_source_rejects_a_record_from_another_score_contract() -> None:
    subject = _subject()
    task, record = _completed_assessment("a", subject, 0.8)
    source = _QualityScoreSource(
        tasks=[task],
        records=[
            record.model_copy(update={"score_contract_hash": "0" * 64})
        ],
    )

    assert source.resolve(_selection_option(subject)).state == "unavailable"
