from dataclasses import replace

import pytest

from goal_plus.application.selection import _QualityScoreSource
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence_policy import iteration_artifact_ref
from goal_plus.domain.selection import (
    ControllerSelectionDirective,
    ControllerSelectionTarget,
    SelectionOption,
    SelectionSubjectKey,
)
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


def test_quality_source_resolves_completed_adaptive_assessment() -> None:
    subject = _subject()
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
    from goal_plus.domain.search import SearchSpec
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
    spec_data = spec.model_dump(mode="json")
    spec_data["workspace"]["provider"] = "git_worktree"
    spec_data["strategy"]["selection"] = {
        "ranking_keys": ["quality_score", "hard_score"],
        "ranking_tolerance": [0.05, 0.0],
    }
    spec = SearchSpec.model_validate(spec_data)
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
        workflow.acknowledge_delivery(task.task_id)
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


def _exact_selection_run(tmp_path, scores, *, fork=False):
    from goal_plus.domain.search import SearchSpec
    from goal_plus.ports.session import ProviderDispatchReceipt
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for

    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\nfrom initial_program import VALUE\n"
        "print(json.dumps({'combined_score': float(VALUE)}))\n"
    )
    spec_data = spec_for(project, max_parallel=2).model_dump(mode="json")
    spec_data["workspace"]["provider"] = "git_worktree"
    if fork:
        spec_data["strategy"] = {
            "name": "random",
            "initial_context": "fork",
            "llm_validation": "required",
        }
    spec = SearchSpec.model_validate(spec_data)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        spec,
        [project / "evaluator.py"],
        agent_harness="codex",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    candidates = runtime.start_batch(run_id, plan.plan_id)
    if fork:
        fork = runtime.candidate_sessions.prepare_dispatch_group(
            run_id, "main-native", "turn-7", "completed_turn",
        )
        for branch in fork.branches:
            runtime.candidate_sessions.record_dispatch_receipt(
                run_id,
                branch.dispatch_target_index,
                ProviderDispatchReceipt(
                    f"branch-{branch.dispatch_target_index}",
                    fork.parent_native_session_id,
                    branch.binding.workspace,
                    "started",
                    context_projection_digest="c" * 64,
                ),
            )
        runtime.candidate_sessions.finish_dispatch_group(run_id)
    reports = []
    for candidate, score in zip(candidates, scores, strict=True):
        (candidate.workspace / "initial_program.py").write_text(
            f"VALUE = {score}\n",
            encoding="utf-8",
        )
        reports.append(runtime.run_verifier(run_id, candidate.candidate_id))
    return runtime, run_id, candidates, reports


def _complete_fork_audits(workflow, run_id):
    from tests.test_llm_verifier import invocation

    while task := workflow.claim_task("fork-audit", 60, run_id=run_id):
        workflow.complete_task(task.task_id, {
            "quality_score": 0.8, "scores": None,
            "score_rationales": {"quality_score": "Exact fork audit"},
        }, invocation(), worker_id=task.lease.worker_id, attempt=task.attempts)
        workflow.acknowledge_delivery(task.task_id)


@pytest.mark.parametrize(
    "mismatch",
    [
        "runtime_provider", "main_session_id", "checkpoint_kind",
        "workspace_attachment", "workspace_hash", "context_projection_digest",
        "parent_frozen_spec_id", "missing_base", "extra_artifact", "duplicate_artifact",
    ],
)
def test_required_fork_audit_rejects_shared_context_mismatch(tmp_path, mismatch):
    runtime, run_id, candidates, _reports = _exact_selection_run(
        tmp_path, [1, 2], fork=True,
    )
    workflow = runtime.selection.verifier
    task = workflow.list_tasks(run_id)[0]
    audit = task.input.dispatch_audit_context
    assert audit is not None
    if mismatch == "runtime_provider":
        updates = {"runtime_provider": "thinkthread"}
    elif mismatch == "workspace_attachment":
        updates = {"workspace_attachment": audit.workspace_attachment.model_copy(
            update={"path": candidates[1].workspace},
        )}
    elif mismatch == "missing_base":
        assert len(audit.artifact_refs) == 2
        updates = {"artifact_refs": audit.artifact_refs[:1]}
    elif mismatch in {"extra_artifact", "duplicate_artifact"}:
        extra = audit.artifact_refs[0]
        if mismatch == "extra_artifact":
            extra = extra.model_copy(update={"id": "f" * 40})
        updates = {"artifact_refs": (*audit.artifact_refs, extra)}
    else:
        updates = {mismatch: "another-context"}
    changed_input = task.input.model_copy(update={
        "dispatch_audit_context": audit.model_copy(update=updates),
    })
    # A projection bug can commit the same incorrect provenance to both
    # outbound objects; their exact equality alone cannot authorize selection.
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    iteration = record.iterations[0]
    iteration.verifier_submission = iteration.verifier_submission.model_copy(
        update={"input": changed_input},
    )
    runtime.candidate_creation.state.write_candidate(run_id, record)
    workflow.state.write_verifier_task(task.model_copy(update={"input": changed_input}))
    _complete_fork_audits(workflow, run_id)
    before = [runtime.list_iterations(run_id, c.candidate_id) for c in candidates]

    with pytest.raises(RuntimeError, match="required fork LLM audit failed"):
        runtime.select(run_id)

    run = runtime.search_run.state.read_run(run_id)
    assert run.state == "selection_blocked"
    assert run.selected_candidate_id is None
    assert run.fork.branches[0].audit_state == "failed"
    assert [runtime.list_iterations(run_id, c.candidate_id) for c in candidates] == before


def test_required_fork_audit_waits_and_retains_settlement_input_after_close(tmp_path):
    from goal_plus.bootstrap import build_settlement_verifier

    runtime, run_id, candidates, _reports = _exact_selection_run(
        tmp_path, [1, 2], fork=True,
    )
    before = [runtime.list_iterations(run_id, c.candidate_id) for c in candidates]
    with pytest.raises(RuntimeError, match="required fork LLM audit is pending"):
        runtime.select(run_id)
    assert runtime.search_run.state.read_run(run_id).selected_candidate_id is None
    _complete_fork_audits(runtime.selection.verifier, run_id)
    tasks = runtime.selection.verifier.list_tasks(run_id)
    for branch in runtime.search_run.state.read_run(run_id).fork.branches:
        runtime.candidate_sessions.record_dispatch_close(
            branch.agent_session_id, branch.provider_session_id,
        )
    assert all(b.closed_at for b in runtime.search_run.state.read_run(run_id).fork.branches)

    build_settlement_verifier(runtime.root_dir).replay(run_id)
    selected = runtime.select(run_id)

    assert selected["selected_candidate_id"] == candidates[1].candidate_id
    assert all(b.audit_state == "completed" for b in runtime.search_run.state.read_run(run_id).fork.branches)
    assert runtime.selection.verifier.list_tasks(run_id) == tasks
    assert [runtime.list_iterations(run_id, c.candidate_id) for c in candidates] == before


def _controller_directive(runtime, run_id, candidate_id, *, receipt="1" * 64):
    record = runtime.search_run.state.read_candidate(run_id, candidate_id)
    iteration = record.iterations[-1]
    artifact_ref = iteration_artifact_ref(
        iteration,
        record.task.workspace_provider,
    )
    assert iteration.settlement_id is not None
    assert iteration.artifact_hash is not None
    return ControllerSelectionDirective(
        target=ControllerSelectionTarget(
            candidate_id=candidate_id,
            iteration=iteration.iteration,
            settlement_id=iteration.settlement_id,
            artifact_ref=artifact_ref,
            artifact_hash=iteration.artifact_hash,
        ),
        receipt_sha256=receipt,
    )


def test_controller_exact_selection_breaks_only_a_frozen_hard_tie(tmp_path) -> None:
    runtime, run_id, candidates, _reports = _exact_selection_run(tmp_path, [1, 1])
    first, second = candidates
    directive = _controller_directive(runtime, run_id, first.candidate_id)
    conflicting_target = _controller_directive(
        runtime,
        run_id,
        second.candidate_id,
    )

    assert runtime.status(run_id).best_candidate_id == second.candidate_id
    selected = runtime.select_for_controller(run_id, directive)

    assert selected["selected_candidate_id"] == first.candidate_id
    assert selected["controller_selection"] == directive.model_dump(mode="json")
    assert selected["selection_trace"][-1]["controller_directive"] == (
        directive.model_dump(mode="json")
    )
    assert runtime.select_for_controller(run_id, directive) == selected

    with pytest.raises(RuntimeError, match="conflicts with the persisted selection"):
        runtime.select_for_controller(run_id, conflicting_target)
    with pytest.raises(RuntimeError, match="conflicts with the persisted selection"):
        runtime.select_for_controller(
            run_id,
            directive.model_copy(update={"receipt_sha256": "2" * 64}),
        )
    run = runtime.search_run.state.read_run(run_id)
    assert run.selected_candidate_id == first.candidate_id
    assert run.controller_selection == directive


def test_controller_exact_selection_cannot_bypass_frozen_hard_ranking(tmp_path) -> None:
    runtime, run_id, candidates, _reports = _exact_selection_run(tmp_path, [1, 3])
    directive = _controller_directive(runtime, run_id, candidates[0].candidate_id)

    with pytest.raises(RuntimeError, match="outside the frozen selection gate"):
        runtime.select_for_controller(run_id, directive)

    run = runtime.search_run.state.read_run(run_id)
    assert run.state == "selection_blocked"
    assert run.selected_candidate_id is None
    assert run.controller_selection is None


@pytest.mark.parametrize(
    "change",
    ["iteration", "settlement_id", "artifact_ref", "artifact_hash"],
)
def test_controller_exact_selection_rejects_stale_identity(
    tmp_path,
    change,
) -> None:
    runtime, run_id, candidates, _reports = _exact_selection_run(tmp_path, [1, 1])
    directive = _controller_directive(runtime, run_id, candidates[0].candidate_id)
    target = directive.target
    if change == "iteration":
        target = target.model_copy(update={"iteration": target.iteration + 1})
    elif change == "settlement_id":
        target = target.model_copy(
            update={"settlement_id": target.settlement_id + "-stale"}
        )
    elif change == "artifact_ref":
        assert target.artifact_ref is not None
        target = target.model_copy(
            update={
                "artifact_ref": target.artifact_ref.model_copy(
                    update={"id": "f" * 40}
                )
            }
        )
    else:
        target = target.model_copy(update={"artifact_hash": "f" * 64})
    stale = directive.model_copy(update={"target": target})

    with pytest.raises(RuntimeError, match="not an eligible exact selection option"):
        runtime.select_for_controller(run_id, stale)

    run = runtime.search_run.state.read_run(run_id)
    assert run.state == "selection_blocked"
    assert run.selected_candidate_id is None


def test_controller_exact_selection_does_not_fallback_after_revalidation_failure(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, run_id, candidates, reports = _exact_selection_run(tmp_path, [1, 1])
    selected_candidate = candidates[0]
    directive = _controller_directive(
        runtime,
        run_id,
        selected_candidate.candidate_id,
    )
    artifact_state = runtime.selection.artifacts.candidate_artifact_state

    def changed_state(run, frozen, record):
        state = artifact_state(run, frozen, record)
        if record.candidate_id == selected_candidate.candidate_id:
            return replace(state, artifact_hash="f" * 64)
        return state

    monkeypatch.setattr(
        runtime.selection.artifacts,
        "candidate_artifact_state",
        changed_state,
    )
    selecting = runtime.selection.select(
        run_id,
        controller_directive=directive,
    )
    request = next(selecting)
    assert request.candidate_id == selected_candidate.candidate_id
    with pytest.raises(RuntimeError, match="failed hard artifact revalidation"):
        selecting.send(
            reports[0].model_copy(
                update={"process_passed": False, "aggregate_score": None}
            )
        )

    run = runtime.search_run.state.read_run(run_id)
    assert run.state == "selection_blocked"
    assert run.selected_candidate_id is None
    assert [item["candidate_id"] for item in run.rejected_selection_subjects] == [
        selected_candidate.candidate_id
    ]
    assert len(run.selection_trace) == 1
