"""Allocation preflight without materialized workspaces, Runtime, or host agents."""

import pytest

from goal_plus.application.allocation_preparation import AllocationPreparation
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.goal import GoalPlusControl, GoalPlusLinkedSearch, GoalPlusRecord
from goal_plus.domain.search import (
    AgentSessionRecord,
    CandidateRecovery,
    CandidateRecoveryPoint,
    RunState,
    SearchPlan,
)
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.goal import FileGoalQueries
from tests.test_allocation_decisions import allocation as allocation
from tests.test_allocation_decisions import record, save_candidate, snapshot


@pytest.fixture
def preparation(allocation):
    store = allocation.store
    frozen = store.read_frozen_spec("frozen_test")
    plan = SearchPlan(
        run_id="run_test",
        plan_id="plan_test",
        strategy=frozen.spec.strategy,
        requested_k=2,
        planned_k=2,
        remaining_budget=2,
        created_at=allocation.clock.timestamp(),
    )
    write_json(
        store.run_dir("run_test") / "plans/plan_test.json", plan.model_dump(mode="json")
    )
    source = store.read_candidate("run_test", "c002")
    source.task.plan_id = plan.plan_id
    source.results_ledger_git_head = "workspace_c002"
    source.results_ledger = [
        ResultLedgerEntry(
            source_run_id="run_test",
            source_candidate_id="c002",
            iteration=1,
            git_head="attempt_c002",
            ledger_git_head="workspace_c002",
            metric_name=frozen.spec.metric_name,
            score=2.5,
            status="pass",
            hypothesis="settled source",
        )
    ]
    save_candidate(store, source)
    allocation.decision = record(allocation)
    allocation.preparation = AllocationPreparation(
        store, GoalAuthorization(FileGoalQueries(store.root_dir))
    )
    return allocation


def prepare(fixture):
    with fixture.store.lock_run("run_test"):
        return fixture.preparation.prepare_locked(
            "run_test", fixture.decision.decision_id
        )


def save_child(fixture):
    source = fixture.store.read_candidate("run_test", "c002")
    replacement = fixture.decision.replacements[0]
    child = source.model_copy(
        update={
            "candidate_id": replacement.new_candidate_id,
            "iterations": [],
            "task": source.task.model_copy(
                update={
                    "candidate_id": replacement.new_candidate_id,
                    "expansion_source": replacement.source,
                }
            ),
        },
        deep=True,
    )
    save_candidate(fixture.store, child)
    return child


def test_preparation_is_repeatable_and_does_not_materialize_or_write(preparation):
    before = snapshot(preparation.store)
    prepared = prepare(preparation)
    again = prepare(preparation)
    assert prepared == again
    assert prepared.decision.status == "pending"
    assert prepared.run.candidates_total == 2
    assert prepared.replacements[0].replacement == preparation.decision.replacements[0]
    assert prepared.replacements[0].plan.plan_id == "plan_test"
    assert (
        prepared.replacements[0].results_ledger[0].ledger_git_head == "workspace_c002"
    )
    assert not prepared.replacements[0].source.task.workspace.exists()
    assert not (preparation.store.run_dir("run_test") / "workspace").exists()
    assert snapshot(preparation.store) == before
    prepared.replacements[0].results_ledger[0].hypothesis = "local copy"
    assert (
        prepare(preparation).replacements[0].results_ledger[0].hypothesis
        == "settled source"
    )
    assert snapshot(preparation.store) == before


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("foreign_decision", "another run"),
        ("missing_retiree", "retire candidate is unavailable"),
        ("other_retirement", "retired by another decision"),
        ("missing_source", "source candidate is unavailable"),
        ("missing_plan", "no initial plan"),
        ("missing_iteration", "no longer matches settled Evidence"),
        ("different_commit", "no longer matches settled Evidence"),
        ("different_artifact", "no longer matches settled Evidence"),
        ("missing_ledger_revision", "ledger revision is not present"),
        ("live_capacity", "exceed budget.max_parallel"),
        ("total_capacity", "exceed budget.max_candidates"),
        ("foreign_child", "does not match allocation decision"),
    ],
)
def test_invalid_preparation_preserves_all_domain_json(preparation, case, reason):
    store = preparation.store
    decision = preparation.decision
    source = store.read_candidate("run_test", "c002")
    if case == "foreign_decision":
        decision.run_id = "another-run"
        write_json(
            store.run_dir("run_test")
            / "allocation-decisions"
            / f"{decision.decision_id}.json",
            decision.model_dump(mode="json"),
        )
    elif case == "missing_retiree":
        decision.replacements[0].retire_candidate_id = "missing"
        store.write_allocation_decision(decision)
    elif case == "other_retirement":
        retiree = store.read_candidate("run_test", "c001")
        retiree.retired_by_decision_id = "another-decision"
        save_candidate(store, retiree)
    elif case == "missing_source":
        decision.replacements[0].source.candidate_id = "missing"
        store.write_allocation_decision(decision)
    elif case == "missing_plan":
        source.task.plan_id = None
    elif case == "missing_iteration":
        source.iterations = []
    elif case == "different_commit":
        source.iterations[0].workspace_git_head_after_settlement = "other-commit"
    elif case == "different_artifact":
        source.iterations[0].artifact_hash = "different-artifact"
    elif case == "missing_ledger_revision":
        source.results_ledger_git_head = "other-commit"
        source.results_ledger[0].ledger_git_head = "other-commit"
    elif case == "live_capacity":
        retiree = store.read_candidate("run_test", "c001")
        retiree.allocation_eligibility = "eligible"
        save_candidate(store, retiree)
    elif case == "total_capacity":
        frozen = store.read_frozen_spec("frozen_test")
        frozen.spec.budget.max_candidates = 2
        write_json(
            store.root_dir / "specs/frozen_test/frozen_spec.json",
            frozen.model_dump(mode="json"),
        )
    elif case == "foreign_child":
        child = save_child(preparation)
        child.task.expansion_source = None
        save_candidate(store, child)
    save_candidate(store, source)
    before = snapshot(store)
    with pytest.raises((RuntimeError, ValueError), match=reason):
        prepare(preparation)
    assert snapshot(store) == before


@pytest.mark.parametrize("state", list(RunState))
def test_preparation_uses_the_shared_worker_run_state_gate(preparation, state):
    store = preparation.store
    run = store.read_run("run_test")
    run.state = state
    write_json(store.run_dir("run_test") / "run.json", run.model_dump(mode="json"))
    before = snapshot(store)
    if state in {
        RunState.RUNNING,
        RunState.WAITING_FOR_WORKERS,
        RunState.SELECTING,
        RunState.SELECTION_BLOCKED,
    }:
        assert prepare(preparation).run.state == state
    else:
        with pytest.raises(RuntimeError, match="cannot apply allocation decision"):
            prepare(preparation)
    assert snapshot(store) == before


def test_invalidated_run_cannot_prepare_allocation(preparation):
    store = preparation.store
    run = store.read_run("run_test")
    run.invalidated_at = preparation.clock.timestamp()
    run.invalidation_reason = "verifier_target_mismatch"
    write_json(store.run_dir("run_test") / "run.json", run.model_dump(mode="json"))
    before = snapshot(store)
    with pytest.raises(RuntimeError, match="was invalidated"):
        prepare(preparation)
    assert snapshot(store) == before


def test_stopped_goal_blocks_preparation_without_touching_standalone_policy(
    preparation,
):
    store = preparation.store
    goal = GoalPlusRecord(
        goal_plus_id="gp_0001",
        raw_goal="Linked Goal",
        control=GoalPlusControl(state="paused"),
        search_tasks=[GoalPlusLinkedSearch(run_id="run_test")],
        created_at=preparation.clock.timestamp(),
        updated_at=preparation.clock.timestamp(),
    )
    write_json(
        store.root_dir / "goal-plus/gp_0001/goal.json", goal.model_dump(mode="json")
    )
    before = snapshot(store)
    with pytest.raises(RuntimeError, match="GoalPlusResumeRequired"):
        prepare(preparation)
    assert snapshot(store) == before


@pytest.mark.parametrize("issue", [None, "stale", "foreign_run", "recovery"])
def test_preparation_revalidates_already_materialized_child_and_session(
    preparation, issue
):
    child = save_child(preparation)
    session = AgentSessionRecord(
        agent_session_id="agent_child_001",
        run_id="run_test",
        candidate_id=child.candidate_id,
        execution_generation=child.execution_generation,
        directive={"allocation_decision_id": preparation.decision.decision_id},
        workspace=child.task.workspace,
        created_at=preparation.clock.timestamp(),
        updated_at=preparation.clock.timestamp(),
        launch={"preserved": "original-budget"},
    )
    reason = None
    if issue == "stale":
        session.execution_generation -= 1
        reason = "StaleAgentSession"
    elif issue == "foreign_run":
        session.run_id = "foreign-run"
        reason = "AgentSessionOwnershipMismatch"
    elif issue == "recovery":
        artifact = ArtifactRef(
            kind="git_commit", provider="git_worktree", id="workspace_c002"
        )
        child.recovery = CandidateRecovery(
            recovery_id="recovery_" + "a" * 32,
            generation=child.execution_generation,
            point=CandidateRecoveryPoint(
                artifact_ref=artifact, iteration=0, result_count=1
            ),
            fenced_session_ids=[session.agent_session_id],
            created_at=preparation.clock.timestamp(),
        )
        save_candidate(preparation.store, child)
        reason = "CandidateRecoveryRequired"
    write_json(
        preparation.store.run_dir("run_test")
        / "agent_sessions"
        / f"{session.agent_session_id}.json",
        session.model_dump(mode="json"),
    )
    before = snapshot(preparation.store)
    if reason:
        with pytest.raises(RuntimeError, match=reason):
            prepare(preparation)
    else:
        prepared = prepare(preparation)
        assert (
            prepared.replacements[0].replacement.new_candidate_id == child.candidate_id
        )
    assert snapshot(preparation.store) == before


def test_preparation_reconstructs_after_a_query_failure(preparation, monkeypatch):
    before = snapshot(preparation.store)
    read_plan = preparation.store.read_plan
    with monkeypatch.context() as patch:

        def unavailable(*args):
            raise OSError("query failed")

        patch.setattr(preparation.store, "read_plan", unavailable)
        with pytest.raises(OSError, match="query failed"):
            prepare(preparation)
    assert preparation.store.read_plan == read_plan
    preparation.preparation = AllocationPreparation(
        preparation.store,
        GoalAuthorization(FileGoalQueries(preparation.store.root_dir)),
    )
    assert prepare(preparation).decision.decision_id == preparation.decision.decision_id
    assert snapshot(preparation.store) == before
