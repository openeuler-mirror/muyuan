"""Decision API and file recovery checks without Runtime, providers or agents."""

from types import SimpleNamespace

import pytest

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.domain.adaptive.models import (
    AllocationProposal,
    AllocationReplacementProposal,
    AllocationTrigger,
    RewardedSource,
)
from goal_plus.domain.agents import WorkerBudget
from goal_plus.domain.evidence import IterationRecord, ScoreReport
from goal_plus.domain.search import CandidateTask, RunState
from goal_plus.infrastructure.state.allocation import FileAllocationState
from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.allocation import AllocationCandidateProjection
from goal_plus.ports.state import CandidateRecord, RunRecord
from tests.test_annotation_admission import frozen_spec
from tests.test_annotation_workflow import Clock


def save_candidate(store, record):
    directory = store.candidate_dir("run_test", record.candidate_id)
    write_json(directory / "candidate.json", record.model_dump(mode="json"))
    write_json(directory / "task.json", record.task.model_dump(mode="json"))


def snapshot(store):
    return {
        str(path.relative_to(store.root_dir)): path.read_bytes()
        for path in store.root_dir.rglob("*.json")
    }


def nonallocation_state(store):
    run = store.read_run("run_test").model_dump(
        exclude={"next_allocation_decision_index"}
    )
    candidates = []
    for record in store.read_candidates("run_test"):
        value = record.model_dump(
            exclude={"allocation_eligibility", "retired_by_decision_id"}
        )
        for iteration in value["iterations"]:
            iteration.pop("allocation_decision_id", None)
            iteration.pop("allocation_decision_error", None)
        if value["score_report"] is not None:
            value["score_report"].pop("allocation_decision", None)
        candidates.append(value)
    return run, candidates, store.read_frozen_spec("frozen_test")


@pytest.fixture
def allocation(tmp_path):
    clock = Clock()
    frozen = frozen_spec(clock, adaptive=True)
    frozen.spec.budget.max_parallel = 2
    store = FileAllocationState(tmp_path / ".gp")
    write_json(
        store.root_dir / "specs/frozen_test/frozen_spec.json",
        frozen.model_dump(mode="json"),
    )
    run = RunRecord(
        run_id="run_test",
        frozen_spec_id="frozen_test",
        state=RunState.RUNNING,
        source_path="/not-a-live-source",
        created_at=clock.timestamp(),
        candidates_total=2,
        next_candidate_index=4,
        best_candidate_id="c002",
        best_score=2.5,
        budget_used={"tokens": 17},
    )
    write_json(store.run_dir("run_test") / "run.json", run.model_dump(mode="json"))
    for candidate_id in ("c001", "c002"):
        record = CandidateRecord(
            candidate_id=candidate_id,
            status="evaluated",
            task=CandidateTask(
                run_id="run_test",
                candidate_id=candidate_id,
                hypothesis="visible contract",
                workspace=tmp_path / "unmaterialized" / candidate_id,
                workspace_backend="git_worktree",
                allowed_files=["main.py"],
                denied_files=["verify.py"],
            ),
            iterations=[
                IterationRecord(
                    iteration=1,
                    created_at=clock.timestamp(),
                    settlement_id=f"settled_{candidate_id}",
                    git_head=f"attempt_{candidate_id}",
                    ledger_git_head=f"ledger_{candidate_id}",
                    workspace_git_head_after_settlement=f"workspace_{candidate_id}",
                    artifact_hash=f"artifact_{candidate_id}",
                    score=2.5,
                    process_passed=True,
                    disposition="keep",
                    scheduler_status="completed",
                    schedule_task_id=f"schedule_{candidate_id}",
                )
            ],
            score_report=ScoreReport(
                run_id="run_test",
                candidate_id=candidate_id,
                validity_passed=True,
                process_passed=True,
                aggregate_score=2.5,
                verifier_results=[],
            ),
            inner_agent_state={"owned_elsewhere": "keep"},
            execution_generation=2,
        )
        save_candidate(store, record)
    trigger = AllocationTrigger(
        schedule_task_id="schedule_c001",
        settlement_id="settled_c001",
        candidate_id="c001",
        iteration=1,
        commit="attempt_c001",
        reserved_candidate_ids=["c003"],
    )
    draft = AllocationProposal(
        rationale="Explore a new direction from settled Evidence",
        replacements=[
            AllocationReplacementProposal(
                retire_candidate_id="c001",
                new_candidate_id="c003",
                source_candidate_id="c002",
                source_iteration=1,
                source_node_id="c002/iteration-0001",
                rationale="Published source",
                exploration_direction="Explore a different implementation",
                worker_budget=WorkerBudget(
                    max_runtime_seconds=30, on_exceed="interrupt"
                ),
            )
        ],
    )
    rewards = [
        RewardedSource(
            schedule_task_id="schedule_c002",
            candidate_id="c002",
            iteration=1,
            node_id="c002/iteration-0001",
            settlement_id="settled_c002",
        )
    ]
    api = AllocationDecisions(state=store, clock=clock)
    return SimpleNamespace(
        api=api, store=store, clock=clock, trigger=trigger, draft=draft, rewards=rewards
    )


def record(allocation):
    return allocation.api.record(
        "run_test", allocation.trigger, allocation.draft, allocation.rewards
    )


@pytest.mark.parametrize("source_head", ["workspace", "ledger", "attempt"])
def test_decision_binds_settled_source_and_replays_without_reinference(
    allocation, source_head
):
    source = allocation.store.read_candidate("run_test", "c002")
    if source_head != "workspace":
        source.iterations[0].workspace_git_head_after_settlement = None
    if source_head == "attempt":
        source.iterations[0].ledger_git_head = None
    save_candidate(allocation.store, source)
    before = nonallocation_state(allocation.store)
    decision = record(allocation)
    assert decision.decision_id == "allocation_0001"
    assert decision.replacements[0].source.settled_commit == f"{source_head}_c002"
    assert decision.replacements[0].source.artifact_hash == "artifact_c002"
    assert (
        decision.replacements[0].worker_budget
        == allocation.draft.replacements[0].worker_budget
    )
    candidate = allocation.store.read_candidate("run_test", "c001")
    assert candidate.allocation_eligibility == "retired"
    assert candidate.retired_by_decision_id == decision.decision_id
    assert candidate.iterations[0].allocation_decision_id == decision.decision_id
    assert candidate.score_report.allocation_decision == decision
    assert nonallocation_state(allocation.store) == before
    after = snapshot(allocation.store)
    allocation.draft.replacements[0].new_candidate_id = "not-reserved"
    assert record(allocation) == decision
    assert snapshot(allocation.store) == after
    assert not (allocation.store.root_dir.parent / "unmaterialized").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "unreserved",
        "ineligible_retiree",
        "missing_retiree",
        "missing_source",
        "missing_iteration",
        "wrong_node",
        "missing_reward",
        "wrong_reward_node",
        "wrong_reward_settlement",
        "missing_artifact",
        "missing_commit",
        "wrong_trigger_settlement",
        "wrong_trigger_commit",
        "capacity",
    ],
)
def test_rejected_proposals_only_record_the_allocation_error(allocation, invalid):
    item = allocation.draft.replacements[0]
    store = allocation.store
    if invalid == "unreserved":
        item.new_candidate_id = "c004"
    elif invalid == "missing_retiree":
        item.retire_candidate_id = "c009"
    elif invalid == "missing_source":
        item.source_candidate_id = "c009"
    elif invalid == "missing_iteration":
        item.source_iteration = 2
    elif invalid == "wrong_node":
        item.source_node_id = "c001/iteration-0001"
    elif invalid == "missing_reward":
        allocation.rewards = []
    elif invalid == "wrong_reward_node":
        allocation.rewards[0].node_id = "c002/iteration-0002"
    elif invalid == "wrong_reward_settlement":
        allocation.rewards[0].settlement_id = "other"
    elif invalid in {"missing_artifact", "missing_commit"}:
        source = store.read_candidate("run_test", "c002")
        field = "artifact_hash" if invalid == "missing_artifact" else "git_head"
        setattr(source.iterations[0], field, None)
        save_candidate(store, source)
    elif invalid == "ineligible_retiree":
        candidate = store.read_candidate("run_test", "c001")
        candidate.allocation_eligibility = "retired"
        candidate.retired_by_decision_id = "allocation_0099"
        save_candidate(store, candidate)
    elif invalid == "wrong_trigger_settlement":
        allocation.trigger.settlement_id = "other"
    elif invalid == "wrong_trigger_commit":
        allocation.trigger.commit = "other"
    elif invalid == "capacity":
        frozen = store.read_frozen_spec("frozen_test")
        frozen.spec.budget.max_parallel = 1
        write_json(
            store.root_dir / "specs/frozen_test/frozen_spec.json",
            frozen.model_dump(mode="json"),
        )
    before = nonallocation_state(store)
    retiree = store.read_candidate("run_test", "c001")
    assert record(allocation) is None
    current = store.read_candidate("run_test", "c001")
    assert current.allocation_eligibility == retiree.allocation_eligibility
    assert current.retired_by_decision_id == retiree.retired_by_decision_id
    assert current.iterations[0].allocation_decision_error.startswith("ValueError:")
    assert current.iterations[0].allocation_decision_id is None
    assert store.load_allocation_decisions("run_test") == []
    assert store.read_run("run_test").next_allocation_decision_index == 1
    assert nonallocation_state(store) == before


@pytest.mark.parametrize(
    "stage",
    ["write_allocation_decision", "write_candidate_projection", "write_decision_index"],
)
def test_restart_replays_the_durable_decision_after_partial_commit(
    allocation, monkeypatch, stage
):
    before = nonallocation_state(allocation.store)
    original = getattr(allocation.store, stage)
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("partial allocation write")
        return original(*args, **kwargs)

    monkeypatch.setattr(allocation.store, stage, fail_once)
    assert record(allocation) is None
    persisted = allocation.store.load_allocation_decisions("run_test")
    assert len(persisted) == (0 if stage == "write_allocation_decision" else 1)
    assert nonallocation_state(allocation.store) == before
    allocation.store = FileAllocationState(allocation.store.root_dir)
    allocation.api = AllocationDecisions(state=allocation.store, clock=allocation.clock)
    decision = record(allocation)
    assert decision.decision_id == "allocation_0001"
    if persisted:
        assert decision == persisted[0]
    assert allocation.api.list("run_test") == [decision]
    assert allocation.store.read_run("run_test").next_allocation_decision_index == 2
    candidate = allocation.store.read_candidate("run_test", "c001")
    assert candidate.retired_by_decision_id == decision.decision_id
    assert candidate.iterations[0].allocation_decision_error is None
    assert nonallocation_state(allocation.store) == before


def test_replay_preserves_applied_decision_and_advances_only_decision_counter(
    allocation,
):
    decision = record(allocation).model_copy(
        update={"status": "applied", "applied_at": allocation.clock.timestamp()}
    )
    allocation.store.write_allocation_decision(decision)
    other = decision.model_copy(
        update={"decision_id": "allocation_0042", "schedule_task_id": "another_task"}
    )
    allocation.store.write_allocation_decision(other)
    before = nonallocation_state(allocation.store)
    assert record(allocation) == decision
    assert allocation.store.read_run("run_test").next_allocation_decision_index == 43
    after = snapshot(allocation.store)
    assert allocation.api.list("run_test", "pending") == []
    assert allocation.api.list("run_test", "applied") == [decision, other]
    assert snapshot(allocation.store) == after
    assert nonallocation_state(allocation.store) == before


@pytest.mark.parametrize("conflict", ["trigger", "retirement"])
def test_replay_cannot_replace_a_conflicting_trigger_or_retirement(
    allocation, conflict
):
    decision = record(allocation)
    candidate = allocation.store.read_candidate("run_test", "c001")
    if conflict == "trigger":
        allocation.trigger.commit = "other"
    else:
        candidate.retired_by_decision_id = "allocation_0099"
        save_candidate(allocation.store, candidate)
    before = nonallocation_state(allocation.store)
    assert record(allocation) is None
    assert allocation.store.load_allocation_decisions("run_test") == [decision]
    current = allocation.store.read_candidate("run_test", "c001")
    assert current.retired_by_decision_id == candidate.retired_by_decision_id
    assert current.iterations[0].allocation_decision_error.startswith("ValueError:")
    assert nonallocation_state(allocation.store) == before


def test_projection_keeps_other_owners_latest_fields(allocation):
    store = allocation.store
    stale = store.read_candidate("run_test", "c001")
    projection = AllocationCandidateProjection(
        candidate_id=stale.candidate_id,
        eligibility="retired",
        retired_by_decision_id="allocation_0001",
    )
    current = stale.model_copy(deep=True)
    current.inner_agent_state["new"] = "preserve"
    current.execution_generation += 1
    current.iterations[0].scheduler_error = "new scheduler observation"
    save_candidate(store, current)
    before = nonallocation_state(store)
    with store.lock_run("run_test"):
        store.write_candidate_projection("run_test", projection)
        store.write_decision_index("run_test", 2)
    assert nonallocation_state(store) == before
    candidate = store.read_candidate("run_test", "c001")
    assert candidate.retired_by_decision_id == "allocation_0001"


@pytest.mark.parametrize("disabled", ["no_reservations", "no_scheduler"])
def test_disabled_admission_writes_no_domain_state(allocation, disabled):
    if disabled == "no_reservations":
        allocation.trigger.reserved_candidate_ids = []
    else:
        frozen = frozen_spec(allocation.clock)
        write_json(
            allocation.store.root_dir / "specs/frozen_test/frozen_spec.json",
            frozen.model_dump(mode="json"),
        )
    before = snapshot(allocation.store)
    assert record(allocation) is None
    if disabled == "no_scheduler":
        with pytest.raises(RuntimeError, match="require strategy.search_scheduler"):
            allocation.api.list("run_test")
    assert snapshot(allocation.store) == before
