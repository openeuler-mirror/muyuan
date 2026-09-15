from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.allocation_apply import AllocationApply
from goal_plus.application.allocation_preparation import AllocationPreparation
from goal_plus.application.adaptive_admission import AdaptiveAdmission
from goal_plus.application.adaptive_reconciliation import (
    AdaptiveDecisionReconciliation,
)
from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.verifier import VerifierWorkflow, verifier_subject
from goal_plus.domain.adaptive.models import (
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    compute_allocation_plan,
    compute_feature_view,
    snapshot_identity,
)
from goal_plus.domain.evidence import IterationRecord, ResultLedgerEntry
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.settlement import (
    RollbackReceipt,
    SettlementEvidence,
    SettlementReceipt,
)
from goal_plus.domain.search import AgentSessionRecord, CandidateTask, RunState, SearchPlan
from goal_plus.infrastructure.state.allocation import FileAllocationState
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.goal import FileGoalQueries
from goal_plus.infrastructure.state.verifier import FileVerifierState
from goal_plus.infrastructure.state.scope import FileScopeState, FileAllocationCancellationProof
from goal_plus.domain.verifier import (
    VerifierRecord,
    VerifierSubject,
    verifier_subject_identity,
)
from goal_plus.ports.state import CandidateRecord, RunRecord
from tests.test_annotation_admission import frozen_spec
from tests.test_annotation_workflow import Clock


def _write_candidate(state: FileAllocationState, record: CandidateRecord) -> None:
    directory = state.candidate_dir("run_test", record.candidate_id)
    write_json(directory / "candidate.json", record.model_dump(mode="json"))
    write_json(directory / "task.json", record.task.model_dump(mode="json"))


def _ensure_allocation_tasks(
    state: FileAllocationState,
    workflow: VerifierWorkflow,
):
    frozen = state.read_frozen_spec("frozen_test")
    policy = frozen.spec.strategy.allocation
    assert policy is not None
    rules = policy.verifier_rules()
    return [
        workflow.ensure_assessment(
            verifier_subject(frozen, candidate, iteration),
            rules.policy_id,
            rules.score_names,
        )
        for candidate in state.read_candidates("run_test")
        for iteration in candidate.iterations
    ]


def _allocation_state(tmp_path: Path, *, failed_source: bool = False):
    clock = Clock()
    frozen = frozen_spec(clock, adaptive=True)
    state = FileAllocationState(tmp_path / ".gp")
    write_json(
        state.root_dir / "specs/frozen_test/frozen_spec.json",
        frozen.model_dump(mode="json"),
    )
    run = RunRecord(
        run_id="run_test",
        frozen_spec_id="frozen_test",
        state=RunState.RUNNING,
        source_path="project",
        created_at=clock.timestamp(),
        candidates_total=2,
        next_candidate_index=3,
    )
    write_json(state.run_dir("run_test") / "run.json", run.model_dump(mode="json"))
    plan = SearchPlan(
        run_id="run_test",
        plan_id="plan_test",
        strategy=frozen.spec.strategy,
        requested_k=2,
        planned_k=2,
        remaining_budget=0,
        created_at=clock.timestamp(),
    )
    write_json(
        state.run_dir("run_test") / "plans/plan_test.json",
        plan.model_dump(mode="json"),
    )
    candidates = []
    for candidate_id, quality, utility in (
        ("c001", 0.2, 0.1),
        ("c002", 0.9, 0.8),
    ):
        commit = f"commit_{candidate_id}"
        artifact_ref = ArtifactRef(
            kind="git_commit", id=commit, provider="git_worktree"
        )
        is_failed_source = failed_source and candidate_id == "c002"
        attempt_base_ref = (
            ArtifactRef(
                kind="git_commit",
                id="rollback_target",
                provider="git_worktree",
            )
            if is_failed_source
            else None
        )
        restored_ref = (
            ArtifactRef(
                kind="git_commit",
                id="restore_commit",
                provider="git_worktree",
            )
            if is_failed_source
            else artifact_ref
        )
        ledger_ref = (
            ArtifactRef(
                kind="git_commit",
                id="rollback_ledger",
                provider="git_worktree",
            )
            if is_failed_source
            else artifact_ref
        )
        receipt = SettlementReceipt(
            settlement_id=f"settlement_{candidate_id}",
            run_id="run_test",
            candidate_id=candidate_id,
            iteration=1,
            attempt_base_artifact_ref=attempt_base_ref,
            attempt_artifact_ref=artifact_ref,
            attempt_artifact_hash=f"artifact_{candidate_id}",
            evidence=SettlementEvidence(
                metric_name="score",
                hard_score=quality,
                process_passed=not is_failed_source,
                artifact_clean=True,
            ),
            disposition="failure" if is_failed_source else "keep",
            rollback=(
                RollbackReceipt(
                    target_artifact_ref=attempt_base_ref,
                    restored_artifact_ref=restored_ref,
                    restored_artifact_hash="restored_hash",
                )
                if attempt_base_ref is not None
                else None
            ),
            post_settlement_artifact_ref=restored_ref,
            ledger_artifact_ref=ledger_ref,
            created_at=clock.timestamp(),
        )
        iteration = IterationRecord(
            iteration=1,
            settlement_id=f"settlement_{candidate_id}",
            score=quality,
            process_passed=not is_failed_source,
            git_head=commit,
            attempt_base_git_head=(
                attempt_base_ref.id if attempt_base_ref is not None else None
            ),
            artifact_ref=artifact_ref,
            attempt_base_artifact_ref=attempt_base_ref,
            ledger_git_head=ledger_ref.id,
            ledger_artifact_ref=ledger_ref,
            restored_to_git_head=(
                attempt_base_ref.id if attempt_base_ref is not None else None
            ),
            restored_to_artifact_ref=(
                restored_ref if is_failed_source else None
            ),
            workspace_git_head_after_settlement=ledger_ref.id,
            workspace_artifact_after_settlement=ledger_ref,
            artifact_hash=f"artifact_{candidate_id}",
            disposition="failure" if is_failed_source else "keep",
            settlement_receipt=receipt,
            created_at=clock.timestamp(),
        )
        record = CandidateRecord(
            candidate_id=candidate_id,
            status="evaluated",
            task=CandidateTask(
                run_id="run_test",
                candidate_id=candidate_id,
                plan_id="plan_test",
                hypothesis="initial candidate",
                workspace=tmp_path / candidate_id,
                workspace_provider="git_worktree",
                allowed_files=["main.py"],
                denied_files=["verify.py"],
                strategy_metadata={"slot": 1},
            ),
            iterations=[iteration],
            results_ledger_git_head=commit,
            results_ledger=[
                ResultLedgerEntry(
                    source_run_id="run_test",
                    source_candidate_id=candidate_id,
                    iteration=1,
                    git_head=commit,
                    ledger_git_head=ledger_ref.id,
                    artifact_ref=artifact_ref,
                    ledger_artifact_ref=ledger_ref,
                    metric_name="score",
                    score=quality,
                    status="pass",
                )
            ],
        )
        _write_candidate(state, record)
        candidates.append(
            SnapshotCandidate(
                candidate_id=candidate_id,
                allocation_eligibility="eligible",
                allocation_depth=0,
                nodes=[
                    SnapshotNode(
                        candidate_id=candidate_id,
                        iteration=1,
                        settlement_id=iteration.settlement_id,
                        node_id=f"{candidate_id}/iteration-0001",
                        verifier_record_id=f"record_{candidate_id}",
                        verifier_subject_hash=verifier_subject_identity(
                            VerifierSubject(
                                frozen_spec_id="frozen_test",
                                settlement=receipt,
                            )
                        ),
                        quality_score=quality,
                        allocation_scores={
                            "progress_reward": 0.1,
                            "allocation_utility": utility,
                        },
                    )
                ],
            )
        )
    body = {
        "schema_version": 2,
        "run_id": "run_test",
        "frozen_spec_id": "frozen_test",
        "policy_id": "priority_replace/v1",
        "max_parallel": 2,
        "max_candidates": None,
        "candidates_total": 2,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, input_hash = snapshot_identity(body)
    snapshot = SearchSnapshot(
        **body, snapshot_revision=revision, input_hash=input_hash
    )
    state.write_search_snapshot(snapshot)
    features = compute_feature_view(snapshot, frozen.spec.strategy.allocation.features)
    allocation_plan = compute_allocation_plan(
        snapshot,
        features,
        policy=frozen.spec.strategy.allocation,
    )
    assert allocation_plan is not None
    decisions = AllocationDecisions(state=state, clock=clock)
    return state, clock, decisions, allocation_plan


def test_decision_is_deterministic_and_does_not_reserve_candidate_ids(
    tmp_path: Path,
) -> None:
    state, _clock, decisions, plan = _allocation_state(tmp_path)

    decision = decisions.record("run_test", plan)

    assert decisions.record("run_test", plan) == decision
    assert decision.decision_id == f"allocation_{plan.plan_hash[:24]}"
    assert decision.expansions[0].new_candidate_id is None
    assert state.read_run("run_test").next_candidate_index == 3
    forged = plan.model_copy(
        update={
            "reason_codes": ["forged"],
        }
    )
    with pytest.raises(ValueError, match="deterministic result"):
        decisions.record("run_test", forged)


def test_record_allows_only_one_pending_decision_per_run(tmp_path: Path) -> None:
    state, _clock, decisions, first_plan = _allocation_state(tmp_path)
    first_snapshot = state.load_search_snapshot(
        "run_test", first_plan.based_on_snapshot_revision
    )
    body = first_snapshot.model_dump(
        mode="json", exclude={"snapshot_revision", "input_hash"}
    )
    body["historical_decision_ids"] = ["allocation_prior"]
    revision, input_hash = snapshot_identity(body)
    second_snapshot = SearchSnapshot(
        **body,
        snapshot_revision=revision,
        input_hash=input_hash,
    )
    state.write_search_snapshot(second_snapshot)
    frozen = state.read_frozen_spec("frozen_test")
    allocation = frozen.spec.strategy.allocation
    assert allocation is not None
    second_plan = compute_allocation_plan(
        second_snapshot,
        compute_feature_view(second_snapshot, allocation.features),
        policy=allocation,
    )
    assert second_plan is not None
    assert second_plan.plan_hash != first_plan.plan_hash

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda plan: decisions.record("run_test", plan),
                [first_plan, second_plan],
            )
        )

    assert sum(item is not None for item in results) == 1
    assert len(decisions.list("run_test", "pending")) == 1


def test_decision_resolves_score_row_to_attempt_instead_of_rollback(
    tmp_path: Path,
) -> None:
    state, _clock, decisions, plan = _allocation_state(
        tmp_path, failed_source=True
    )
    candidate = state.read_candidate("run_test", "c002")
    original = candidate.iterations[0]
    assert original.artifact_ref is not None
    assert original.settlement_receipt is not None
    assert original.settlement_receipt.rollback is not None

    decision = decisions.record("run_test", plan)
    source = decision.expansions[0].source

    assert source.candidate_id == "c002"
    assert source.attempt_artifact_ref == original.artifact_ref
    assert source.attempt_artifact_hash == original.artifact_hash
    assert source.ledger_artifact_ref.id == "rollback_ledger"
    assert source.attempt_artifact_ref.id == "commit_c002"
    assert source.attempt_artifact_ref.id != "restore_commit"


def test_decision_rejects_receipt_rebound_after_snapshot(tmp_path: Path) -> None:
    state, _clock, decisions, plan = _allocation_state(tmp_path)
    candidate = state.read_candidate("run_test", "c002")
    original = candidate.iterations[0]
    assert original.settlement_receipt is not None
    forged_receipt = original.settlement_receipt.model_copy(
        update={"attempt_artifact_hash": "forged_artifact"}
    )
    candidate.iterations = [
        IterationRecord.model_validate(
            {
                **original.model_dump(mode="python"),
                "artifact_hash": "forged_artifact",
                "settlement_receipt": forged_receipt,
            }
        )
    ]
    _write_candidate(state, candidate)

    with pytest.raises(ValueError, match="does not match its score row"):
        decisions.record("run_test", plan)


def test_apply_assigns_id_and_recovers_after_partial_retirement(
    tmp_path: Path,
) -> None:
    state, clock, decisions, plan = _allocation_state(tmp_path)
    decision = decisions.record("run_test", plan)
    fail_once = True

    def materialize(request):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("injected materialization interruption")
        record = CandidateRecord(
            candidate_id=request.candidate_id,
            status="created",
            task=CandidateTask(
                run_id="run_test",
                candidate_id=request.candidate_id,
                plan_id=request.plan.plan_id,
                hypothesis=request.proposal.intent,
                workspace=tmp_path / request.candidate_id,
                workspace_provider="git_worktree",
                allowed_files=["main.py"],
                denied_files=["verify.py"],
                expansion_source=request.expansion_source,
            ),
            results_ledger=list(request.results_ledger or []),
        )
        _write_candidate(state, record)
        return record

    def start_session(run_id, candidate_id, directive=None, *, worker_budget=None):
        candidate = state.read_candidate(run_id, candidate_id)
        session = AgentSessionRecord(
            agent_session_id=f"agent_{candidate_id}",
            run_id=run_id,
            candidate_id=candidate_id,
            execution_generation=candidate.execution_generation,
            directive=directive,
            workspace=candidate.task.workspace,
            created_at=clock.timestamp(),
            updated_at=clock.timestamp(),
            launch={"worker_budget": worker_budget},
        )
        write_json(
            state.run_dir(run_id)
            / "agent_sessions"
            / f"{session.agent_session_id}.json",
            session.model_dump(mode="json"),
        )
        return session

    application = AllocationApply(
        state=state,
        cancellation=FileAllocationCancellationProof(FileScopeState(state.root_dir, clock)),
        preparation=AllocationPreparation(
            state, GoalAuthorization(FileGoalQueries(state.root_dir))
        ),
        materialize=materialize,
        start_session=start_session,
        clock=clock,
    )
    with pytest.raises(OSError, match="interruption"):
        application.apply("run_test", decision.decision_id)
    interrupted = state.load_allocation_decision("run_test", decision.decision_id)
    assert interrupted.status == "pending"
    assert interrupted.expansions[0].new_candidate_id == "c003"
    assert state.read_candidate("run_test", "c001").allocation_eligibility == "retired"

    applied = application.apply("run_test", decision.decision_id)

    assert applied.decision.status == "applied"
    assert [item.candidate_id for item in applied.candidate_tasks] == ["c003"]
    assert [item.candidate_id for item in applied.agent_sessions] == ["c003"]
    assert state.read_run("run_test").candidates_total == 3


def test_completed_verifier_record_is_the_only_decision_trigger(
    tmp_path: Path,
) -> None:
    state, clock, decisions, _plan = _allocation_state(tmp_path)
    workflow = VerifierWorkflow(FileVerifierState(state.root_dir), clock)
    reconciliation = AdaptiveDecisionReconciliation(
        state=state,
        verifier=workflow,
        snapshots=AdaptiveSnapshotBuilder(state=state, verifier=workflow),
        decisions=decisions,
    )
    tasks = _ensure_allocation_tasks(state, workflow)
    assert len(tasks) == 2
    assert decisions.list("run_test") == []

    quality = {"c001": 0.2, "c002": 0.9}
    utility = {"c001": 0.1, "c002": 0.8}
    decision = None
    while (task := workflow.claim_task("worker", 60, run_id="run_test")) is not None:
        record = workflow.complete_task(
            task.task_id,
            {
                "quality_score": quality[task.candidate_id],
                "allocation_scores": {
                    "progress_reward": 0.1,
                    "allocation_utility": utility[task.candidate_id],
                },
                "rationale": "bounded hard evidence supports this quality estimate",
            },
            {
                "invocation_id": f"invocation_{task.candidate_id}",
                "agent_harness": "codex",
                "model": "test-model",
                "started_at": clock.timestamp(),
                "completed_at": clock.timestamp(),
                "elapsed_seconds": 0,
            },
        )
        decision = reconciliation.on_verifier_record_completed(record) or decision

    assert decision is not None
    assert decision.retire_candidate_ids == ["c001"]
    assert decision.expansions[0].source.candidate_id == "c002"
    snapshot = state.load_search_snapshot(
        "run_test", decision.based_on_snapshot_revision
    )
    assert snapshot.candidates[0].nodes[0].verifier_record_id
    assert snapshot.candidates[0].nodes[0].allocation_scores[
        "allocation_utility"
    ] == 0.1


def test_reconciliation_replays_completed_verifier_records(tmp_path: Path) -> None:
    state, clock, decisions, _plan = _allocation_state(tmp_path)
    verifier_state = FileVerifierState(state.root_dir)
    workflow = VerifierWorkflow(verifier_state, clock)
    reconciliation = AdaptiveDecisionReconciliation(
        state=state,
        verifier=workflow,
        snapshots=AdaptiveSnapshotBuilder(state=state, verifier=workflow),
        decisions=decisions,
    )
    _ensure_allocation_tasks(state, workflow)
    quality = {"c001": 0.2, "c002": 0.9}
    utility = {"c001": 0.1, "c002": 0.8}
    records = []
    while (task := workflow.claim_task("worker", 60, run_id="run_test")) is not None:
        records.append(
            workflow.complete_task(
                task.task_id,
                {
                    "quality_score": quality[task.candidate_id],
                    "allocation_scores": {
                        "progress_reward": 0.1,
                        "allocation_utility": utility[task.candidate_id],
                    },
                    "rationale": "durable completed record awaiting reconciliation",
                },
                {
                    "invocation_id": f"invocation_{task.candidate_id}",
                    "agent_harness": "codex",
                    "model": "test-model",
                    "started_at": clock.timestamp(),
                    "completed_at": clock.timestamp(),
                    "elapsed_seconds": 0,
                },
            )
        )

    while (record := reconciliation.next_pending_record("run_test")) is not None:
        reconciliation.on_verifier_record_completed(record)

    assert len(decisions.list("run_test")) == 1
    assert all(
        state.load_adaptive_reconciliation("run_test", record.record_id).state
        == "completed"
        for record in records
    )
    assert reconciliation.next_pending_record("run_test") is None


def test_prepared_reconciliation_replays_existing_decision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state, clock, decisions, _plan = _allocation_state(tmp_path)
    workflow = VerifierWorkflow(FileVerifierState(state.root_dir), clock)
    reconciliation = AdaptiveDecisionReconciliation(
        state=state,
        verifier=workflow,
        snapshots=AdaptiveSnapshotBuilder(state=state, verifier=workflow),
        decisions=decisions,
    )
    _ensure_allocation_tasks(state, workflow)
    first_task = workflow.claim_task("worker", 60, run_id="run_test")
    assert first_task is not None and first_task.candidate_id == "c001"
    first_record = workflow.complete_task(
        first_task.task_id,
        {
            "quality_score": 0.2,
            "allocation_scores": {
                "progress_reward": 0.1,
                "allocation_utility": 0.1,
            },
            "rationale": "first lane is not enough to allocate",
        },
        {
            "invocation_id": "invocation_c001",
            "agent_harness": "codex",
            "model": "test-model",
            "started_at": clock.timestamp(),
            "completed_at": clock.timestamp(),
            "elapsed_seconds": 0,
        },
    )
    assert reconciliation.on_verifier_record_completed(first_record) is None

    second_task = workflow.claim_task("worker", 60, run_id="run_test")
    assert second_task is not None and second_task.candidate_id == "c002"
    second_record = workflow.complete_task(
        second_task.task_id,
        {
            "quality_score": 0.9,
            "allocation_scores": {
                "progress_reward": 0.1,
                "allocation_utility": 0.8,
            },
            "rationale": "second lane can replace the weaker lane",
        },
        {
            "invocation_id": "invocation_c002",
            "agent_harness": "codex",
            "model": "test-model",
            "started_at": clock.timestamp(),
            "completed_at": clock.timestamp(),
            "elapsed_seconds": 0,
        },
    )
    original_write = state.write_adaptive_reconciliation

    def interrupt_completed_receipt(record):
        if record.state == "completed" and record.decision_id is not None:
            raise OSError("interruption after decision commit")
        original_write(record)

    monkeypatch.setattr(
        state, "write_adaptive_reconciliation", interrupt_completed_receipt
    )
    with pytest.raises(OSError, match="interruption after decision commit"):
        reconciliation.on_verifier_record_completed(second_record)

    [recorded] = decisions.list("run_test")
    prepared = state.load_adaptive_reconciliation(
        "run_test", second_record.record_id
    )
    assert prepared is not None and prepared.state == "prepared"
    monkeypatch.setattr(state, "write_adaptive_reconciliation", original_write)

    replayed = reconciliation.on_verifier_record_completed(second_record)

    assert replayed is not None and replayed.decision_id == recorded.decision_id
    assert len(decisions.list("run_test")) == 1
    completed = state.load_adaptive_reconciliation(
        "run_test", second_record.record_id
    )
    assert completed is not None and completed.state == "completed"
    assert completed.decision_id == recorded.decision_id


def test_uncompleted_verifier_record_cannot_trigger_snapshot(
    tmp_path: Path,
) -> None:
    state, clock, decisions, _plan = _allocation_state(tmp_path)
    workflow = VerifierWorkflow(FileVerifierState(state.root_dir), clock)
    [task, *_] = _ensure_allocation_tasks(state, workflow)
    record = VerifierRecord(
        record_id="record_unpersisted",
        task_id=task.task_id,
        subject=task.subject,
        allocation_policy_id=task.allocation_policy_id,
        quality_score=0.5,
        allocation_scores={
            "progress_reward": 0.5,
            "allocation_utility": 0.5,
        },
        rationale="an uncompleted task cannot publish an allocation score row",
        invocation={
            "invocation_id": "invocation_unpersisted",
            "agent_harness": "codex",
            "model": "test-model",
            "started_at": clock.timestamp(),
            "completed_at": clock.timestamp(),
            "elapsed_seconds": 0,
        },
        created_at=clock.timestamp(),
    )
    reconciliation = AdaptiveDecisionReconciliation(
        state=state,
        verifier=workflow,
        snapshots=AdaptiveSnapshotBuilder(state=state, verifier=workflow),
        decisions=decisions,
    )
    snapshot_dir = state.run_dir("run_test") / "adaptive-snapshots"
    snapshots_before = sorted(snapshot_dir.glob("*.json"))

    with pytest.raises(ValueError, match="completed verifier task"):
        reconciliation.on_verifier_record_completed(record)
    assert sorted(snapshot_dir.glob("*.json")) == snapshots_before
    assert decisions.list("run_test") == []


def test_pending_verifier_tasks_do_not_block_candidate_continuation(
    tmp_path: Path,
) -> None:
    state, clock, _decisions, _plan = _allocation_state(tmp_path)
    workflow = VerifierWorkflow(FileVerifierState(state.root_dir), clock)
    tasks = _ensure_allocation_tasks(state, workflow)
    assert tasks
    assert all(task.state == "pending" for task in tasks)

    admission = AdaptiveAdmission(state)
    admission.assert_candidate_execution_allowed(
        state.read_candidate("run_test", "c001"),
        "continue_session",
    )


def test_snapshot_includes_failed_attempt_verifier_scores(
    tmp_path: Path,
) -> None:
    state, clock, _decisions, _plan = _allocation_state(tmp_path)
    frozen = state.read_frozen_spec("frozen_test")
    candidate = state.read_candidate("run_test", "c001")
    original = candidate.iterations[0]
    attempt_ref = original.artifact_ref
    assert attempt_ref is not None
    base_ref = ArtifactRef(
        kind="git_commit", id="base_commit", provider="git_worktree"
    )
    restored_ref = ArtifactRef(
        kind="git_commit", id="restore_commit", provider="git_worktree"
    )
    ledger_ref = ArtifactRef(
        kind="git_commit", id="ledger_commit", provider="git_worktree"
    )
    receipt = SettlementReceipt(
        settlement_id=str(original.settlement_id),
        run_id="run_test",
        candidate_id="c001",
        iteration=1,
        attempt_base_artifact_ref=base_ref,
        attempt_artifact_ref=attempt_ref,
        attempt_artifact_hash=str(original.artifact_hash),
        evidence=SettlementEvidence(
            metric_name="score",
            hard_score=original.score,
            process_passed=False,
            changed_files=["main.py"],
            attempt_changed_files=["main.py"],
            artifact_clean=True,
            failure_class="VerifierCommandFailed",
        ),
        disposition="failure",
        rollback=RollbackReceipt(
            target_artifact_ref=base_ref,
            restored_artifact_ref=restored_ref,
            restored_artifact_hash="restored_hash",
        ),
        post_settlement_artifact_ref=restored_ref,
        ledger_artifact_ref=ledger_ref,
        created_at=clock.timestamp(),
    )
    iteration = IterationRecord.model_validate(
        {
            **original.model_dump(mode="python"),
            "process_passed": False,
            "attempt_base_artifact_ref": base_ref,
            "attempt_changed_files": ["main.py"],
            "changed_files": ["main.py"],
            "failure_class": "VerifierCommandFailed",
            "disposition": "failure",
            "restored_to_git_head": base_ref.id,
            "restored_to_artifact_ref": restored_ref,
            "ledger_git_head": ledger_ref.id,
            "ledger_artifact_ref": ledger_ref,
            "workspace_git_head_after_settlement": ledger_ref.id,
            "workspace_artifact_after_settlement": ledger_ref,
            "settlement_receipt": receipt,
        }
    )
    candidate.iterations = [iteration]
    _write_candidate(state, candidate)
    workflow = VerifierWorkflow(FileVerifierState(state.root_dir), clock)
    task = workflow.ensure_assessment(
        verifier_subject(frozen, candidate, iteration),
        "priority_replace/v1",
        ["progress_reward", "allocation_utility"],
    )

    claimed = workflow.claim_task("worker", 60, run_id="run_test")
    assert claimed is not None
    workflow.complete_task(
        claimed.task_id,
        {
            "quality_score": 0.6,
            "allocation_scores": {
                "progress_reward": 0.7,
                "allocation_utility": 0.8,
            },
            "rationale": "the failed attempt contains reusable partial value",
        },
        {
            "invocation_id": "invocation_failure",
            "agent_harness": "codex",
            "model": "test-model",
            "started_at": clock.timestamp(),
            "completed_at": clock.timestamp(),
            "elapsed_seconds": 0,
        },
    )
    snapshot = AdaptiveSnapshotBuilder(
        state=state,
        verifier=workflow,
    ).build("run_test")

    failure_row = snapshot.candidates[0].nodes[0]
    assert failure_row.settlement_id == receipt.settlement_id
    assert failure_row.verifier_record_id
    assert failure_row.allocation_scores["allocation_utility"] == 0.8
