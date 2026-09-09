"""Allocation apply/recovery with file state and controlled creation APIs, no agents."""

import fcntl

import pytest

from goal_plus.application.allocation_apply import AllocationApply
from goal_plus.domain.search import AgentSessionRecord, CandidateTask
from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.state import CandidateRecord
from tests.test_allocation_decisions import allocation as allocation
from tests.test_allocation_decisions import save_candidate, snapshot
from tests.test_allocation_preparation import preparation as preparation


def assert_run_lock(store, *, held):
    with (store.run_dir("run_test") / "run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            assert held
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            assert not held


class Creation:
    def __init__(self, fixture):
        self.fixture = fixture
        self.materializations = []
        self.sessions = []
        self.fail_materialization = None
        self.fail_session = None

    def materialize(self, request):
        assert_run_lock(self.fixture.store, held=True)
        self.materializations.append(request)
        assert request.expansion_source.settled_commit == "workspace_c002"
        assert request.results_ledger[-1].ledger_git_head == "workspace_c002"
        assert request.proposal.metadata["allocation_decision_id"] == self.fixture.decision.decision_id
        if self.fail_materialization == "before":
            raise OSError("materialization failed before candidate commit")
        record = CandidateRecord(
            candidate_id=request.candidate_id,
            status="created",
            task=CandidateTask(
                run_id=request.run.run_id,
                candidate_id=request.candidate_id,
                workspace=request.source_record.task.workspace.parent / request.candidate_id,
                workspace_backend="git_worktree",
                plan_id=request.plan.plan_id,
                hypothesis=request.proposal.intent,
                proposal=request.proposal,
                allowed_files=request.frozen.spec.edit_surface.allow,
                denied_files=request.frozen.spec.edit_surface.deny,
                expansion_source=request.expansion_source,
            ),
            results_ledger=request.results_ledger,
        )
        save_candidate(self.fixture.store, record)
        if self.fail_materialization == "after":
            raise OSError("materialization failed after candidate commit")
        return record

    def start_session(self, run_id, candidate_id, directive=None, *, worker_budget=None):
        store = self.fixture.store
        assert_run_lock(store, held=False)
        self.sessions.append(candidate_id)
        assert store.load_allocation_decision(run_id, self.fixture.decision.decision_id).status == "applied"
        if self.fail_session == "before":
            raise OSError("session failed before provenance commit")
        candidate = store.read_candidate(run_id, candidate_id)
        session = AgentSessionRecord(
            agent_session_id="agent_" + candidate_id,
            run_id=run_id,
            candidate_id=candidate_id,
            execution_generation=candidate.execution_generation,
            directive=directive,
            workspace=candidate.task.workspace,
            created_at=self.fixture.clock.timestamp(),
            updated_at=self.fixture.clock.timestamp(),
            launch={"worker_budget": worker_budget, "unchanged": "launch"},
        )
        write_json(
            store.run_dir(run_id) / "agent_sessions" / f"{session.agent_session_id}.json",
            session.model_dump(mode="json"),
        )
        if self.fail_session == "after":
            raise OSError("session failed after provenance commit")
        return session


@pytest.fixture
def apply_api(preparation):
    preparation.creation = Creation(preparation)
    preparation.apply = AllocationApply(
        state=preparation.store,
        preparation=preparation.preparation,
        materialize=preparation.creation.materialize,
        start_session=preparation.creation.start_session,
        clock=preparation.clock,
    )
    return preparation


def apply(fixture):
    return fixture.apply.apply("run_test", fixture.decision.decision_id)


def parent_evidence(fixture):
    return [
        fixture.store.read_candidate("run_test", candidate_id).model_dump(
            exclude={"allocation_eligibility", "retired_by_decision_id"}
        )
        for candidate_id in ("c001", "c002")
    ]


def test_apply_creates_exact_child_and_launch_once_without_overwriting_other_owners(apply_api):
    before = parent_evidence(apply_api)
    original_run = apply_api.store.read_run("run_test")
    result = apply(apply_api)
    after = snapshot(apply_api.store)
    assert apply(apply_api) == result
    assert snapshot(apply_api.store) == after
    assert parent_evidence(apply_api) == before
    assert result.decision.status == "applied"
    assert [item.candidate_id for item in result.candidate_tasks] == ["c003"]
    assert len(apply_api.creation.materializations) == 1
    assert apply_api.creation.sessions == ["c003"]
    session = result.agent_sessions[0]
    assert session.directive["expansion_source"]["settled_commit"] == "workspace_c002"
    assert session.launch["worker_budget"]["max_runtime_seconds"] == 30
    assert session.directive["exploration_direction"] == result.decision.replacements[0].exploration_direction
    run = apply_api.store.read_run("run_test")
    assert run.candidates_total == 3
    assert run.model_dump(exclude={"candidates_total"}) == original_run.model_dump(exclude={"candidates_total"})


@pytest.mark.parametrize("stage", ["before", "after"])
def test_materialization_failure_replays_from_existing_candidate_identity(apply_api, stage):
    before = parent_evidence(apply_api)
    apply_api.creation.fail_materialization = stage
    with pytest.raises(OSError, match="materialization failed"):
        apply(apply_api)
    assert apply_api.store.load_allocation_decision("run_test", apply_api.decision.decision_id).status == "pending"
    assert apply_api.creation.sessions == []
    assert parent_evidence(apply_api) == before
    apply_api.creation.fail_materialization = None
    result = apply(apply_api)
    assert result.decision.status == "applied"
    assert apply_api.store.read_run("run_test").candidates_total == 3
    assert len(apply_api.creation.materializations) == (2 if stage == "before" else 1)
    assert apply_api.creation.sessions == ["c003"]
    assert parent_evidence(apply_api) == before


@pytest.mark.parametrize("operation", ["write_retirement", "write_allocation_decision", "write_materialization_projection"])
@pytest.mark.parametrize("after_write", [False, True])
def test_partial_state_write_replays_without_duplicate_materialization_or_score_changes(
    apply_api, monkeypatch, operation, after_write
):
    before = parent_evidence(apply_api)
    original = getattr(apply_api.store, operation)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            if after_write:
                original(*args, **kwargs)
            raise OSError("injected apply write failure")
        patch.setattr(apply_api.store, operation, fail)
        with pytest.raises(OSError, match="injected apply write failure"):
            apply(apply_api)
    assert apply_api.creation.sessions == []
    result = apply(apply_api)
    assert result.decision.status == "applied"
    assert apply_api.store.read_run("run_test").candidates_total == 3
    assert len(apply_api.creation.materializations) == 1
    assert apply_api.creation.sessions == ["c003"]
    assert parent_evidence(apply_api) == before


@pytest.mark.parametrize("stage", ["before", "after"])
def test_session_failure_does_not_undo_applied_decision_and_reuses_saved_provenance(apply_api, stage):
    before = parent_evidence(apply_api)
    apply_api.creation.fail_session = stage
    with pytest.raises(OSError, match="session failed"):
        apply(apply_api)
    decision = apply_api.store.load_allocation_decision("run_test", apply_api.decision.decision_id)
    assert decision.status == "applied"
    apply_api.creation.fail_session = None
    assert apply(apply_api).decision == decision
    assert len(apply_api.creation.materializations) == 1
    assert len(apply_api.creation.sessions) == (2 if stage == "before" else 1)
    assert parent_evidence(apply_api) == before


def test_entire_batch_is_checked_before_any_retirement_or_creation(apply_api):
    decision = apply_api.decision
    decision.replacements.append(decision.replacements[0].model_copy(update={
        "new_candidate_id": "c004", "retire_candidate_id": "missing",
    }, deep=True))
    apply_api.store.write_allocation_decision(decision)
    before = snapshot(apply_api.store)
    with pytest.raises(RuntimeError, match="retire candidate is unavailable"):
        apply(apply_api)
    assert snapshot(apply_api.store) == before
    assert apply_api.creation.materializations == []
    assert apply_api.creation.sessions == []


def test_apply_retirement_preserves_existing_legacy_ledger_import(apply_api):
    store = apply_api.store
    retiree = store.read_candidate("run_test", "c001")
    retiree.task.workspace.mkdir(parents=True)
    (retiree.task.workspace / "results.tsv").write_text(
        "commit\tmetric\tstatus\thypothesis\nattempt_c001\t2.5\tpass\tlegacy result\n"
    )
    assert retiree.results_ledger == []
    apply(apply_api)
    retired = store.read_candidate("run_test", "c001")
    assert retired.results_ledger[0].git_head == "attempt_c001"
    assert retired.results_ledger[0].hypothesis == "legacy result"
    assert retired.iterations == retiree.iterations


def test_partially_materialized_multi_replacement_keeps_batch_order_and_identity(
    apply_api, monkeypatch
):
    store = apply_api.store
    decision = apply_api.decision
    decision.replacements.append(decision.replacements[0].model_copy(update={
        "new_candidate_id": "c004", "retire_candidate_id": "c002",
    }, deep=True))
    store.write_allocation_decision(decision)
    retiree = store.read_candidate("run_test", "c002")
    retiree.allocation_eligibility = "retired"
    retiree.retired_by_decision_id = decision.decision_id
    save_candidate(store, retiree)
    before = parent_evidence(apply_api)
    materialize = apply_api.creation.materialize
    with monkeypatch.context() as patch:
        def second_fails(request):
            if request.candidate_id == "c004":
                raise OSError("second materialization unavailable")
            return materialize(request)
        patch.setattr(apply_api.apply, "_materialize", second_fails)
        with pytest.raises(OSError, match="second materialization unavailable"):
            apply(apply_api)
    assert store.find_candidate("run_test", "c003") is not None
    assert store.find_candidate("run_test", "c004") is None
    assert apply_api.creation.sessions == []
    result = apply(apply_api)
    assert [task.candidate_id for task in result.candidate_tasks] == ["c003", "c004"]
    assert [request.candidate_id for request in apply_api.creation.materializations] == ["c003", "c004"]
    assert apply_api.creation.sessions == ["c003", "c004"]
    assert store.read_run("run_test").candidates_total == 4
    assert parent_evidence(apply_api) == before
    assert apply(apply_api) == result
