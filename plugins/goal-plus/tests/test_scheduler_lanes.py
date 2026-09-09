"""Scheduler lane APIs against temporary files, without Runtime or model hosts."""

from types import SimpleNamespace

import pytest

from goal_plus.application.scheduler import SchedulerWorkflow
from goal_plus.application.scheduler_lanes import SchedulerLanes
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.annotation import EvidenceAnnotationTask, EvidenceViewRecord
from goal_plus.domain.evidence import ScoreReport
from goal_plus.domain.search import CandidateTask, RunState
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.scheduler import FileSchedulerState
from goal_plus.ports.scheduler_lanes import (
    SchedulerCandidateProjection,
    SchedulerReportProjection,
)
from goal_plus.ports.search_context import SearchHostSettings
from goal_plus.ports.state import CandidateRecord
from tests.test_annotation_workflow import Clock
from tests.test_scheduler_context import ContextState
from tests.test_scheduler_workflow import proposed_result, result


class Annotations:
    def __init__(self):
        self.records = {}

    def task(self, run_id, candidate_id, iteration):
        task = self.records.get((run_id, candidate_id, iteration))
        return task.model_copy(deep=True) if task else None


def save_candidate(store, record):
    write_json(
        store.run_dir("run_test") / "candidates/c001/candidate.json",
        record.model_dump(mode="json"),
    )


def snapshot(store):
    return {
        str(path.relative_to(store.root_dir)): path.read_bytes()
        for path in store.root_dir.rglob("*.json")
    }


def hard_state(store):
    run = store.read_run("run_test").model_dump(exclude={"next_candidate_index"})
    candidate = store.read_candidate("run_test", "c001").model_dump()
    candidate.pop("schedule_settlement_watermark")
    for iteration in candidate["iterations"]:
        for field in ("schedule_task_id", "scheduler_status", "scheduler_error"):
            iteration.pop(field, None)
    for field in ("schedule_task_id", "scheduler_status"):
        candidate["score_report"].pop(field, None)
    return run, candidate


@pytest.fixture
def lanes(tmp_path):
    clock = Clock()
    context = ContextState(clock)
    store = FileSchedulerState(tmp_path / ".gp")
    context.run.next_candidate_index = 2
    context.run.candidates_total = 1
    write_json(
        store.run_dir("run_test") / "run.json", context.run.model_dump(mode="json")
    )
    write_json(
        store.root_dir / "specs/frozen_test/frozen_spec.json",
        context.frozen.model_dump(mode="json"),
    )
    record = CandidateRecord(
        candidate_id="c001",
        status="evaluated",
        iterations=[context.iteration],
        task=CandidateTask(
            run_id="run_test",
            candidate_id="c001",
            hypothesis="worker contract",
            workspace=context.records[0].workspace,
            workspace_backend="git_worktree",
            allowed_files=["main.py"],
            denied_files=["verify.py"],
        ),
        score_report=ScoreReport(
            run_id="run_test",
            candidate_id="c001",
            validity_passed=True,
            process_passed=True,
            aggregate_score=2.5,
            verifier_results=[],
        ),
        inner_agent_state={"owned_elsewhere": "preserve"},
    )
    save_candidate(store, record)
    write_json(
        store.run_dir("run_test") / "candidates/c001/task.json",
        record.task.model_dump(mode="json"),
    )
    annotation = Annotations()
    workflow = SchedulerWorkflow(tasks=store, runs=store, clock=clock)
    api = SchedulerLanes(
        workflow=workflow,
        source=store,
        annotation=annotation,
        context=SearchContext(context),
    )
    return SimpleNamespace(api=api, store=store, clock=clock, annotations=annotation)


def annotate(lanes, iteration=1, *, state="completed"):
    item = lanes.store.read_candidate("run_test", "c001").iterations[iteration - 1]
    task = EvidenceAnnotationTask(
        task_id=f"annotation_{item.settlement_id}",
        settlement_id=item.settlement_id,
        run_id="run_test",
        candidate_id="c001",
        iteration=iteration,
        attempt_base_commit=item.attempt_base_git_head,
        attempt_commit=item.git_head,
        state=state,
        created_at=lanes.clock.timestamp(),
        updated_at=lanes.clock.timestamp(),
        view=(
            EvidenceViewRecord(
                run_id="run_test",
                candidate_id="c001",
                iteration=iteration,
                attempt_commit=item.git_head,
                description="Published observation",
                created_at=lanes.clock.timestamp(),
            )
            if state == "completed"
            else None
        ),
        last_error="annotation failed" if state == "terminal_error" else None,
        error_fingerprint="annotation_error" if state == "terminal_error" else None,
    )
    lanes.annotations.records[("run_test", "c001", iteration)] = task
    return task


def append_iteration(lanes):
    record = lanes.store.read_candidate("run_test", "c001")
    number = len(record.iterations) + 1
    record.iterations.append(
        record.iterations[-1].model_copy(
            deep=True,
            update={
                "iteration": number,
                "settlement_id": f"settled_{number}",
                "schedule_task_id": None,
                "scheduler_status": None,
            },
        )
    )
    save_candidate(lanes.store, record)


def finish(lanes, iteration=1, *, allocate=False, decision=False):
    claimed = lanes.api.workflow.claim("run_test", "c001", iteration)
    observed = (
        proposed_result(lanes.clock, decision=decision)
        if allocate
        else result(lanes.clock)
    )
    assert lanes.api.workflow.complete(claimed, result=observed)


def prepare_confirmation(lanes):
    annotate(lanes)
    lanes.api.ensure_tasks("run_test")
    finish(lanes, allocate=True)
    append_iteration(lanes)


@pytest.mark.parametrize(
    "annotation_state",
    ["pending", "retry_wait", "cancelled", "completed", "terminal_error"],
)
def test_admission_waits_for_terminal_annotation_but_does_not_require_success(
    lanes, annotation_state
):
    annotation = annotate(lanes, state=annotation_state)
    before = hard_state(lanes.store)
    ensured = lanes.api.ensure_tasks("run_test")
    task = lanes.store.load_schedule_task("run_test", "c001", 1)
    if annotation_state in {"completed", "terminal_error"}:
        assert ensured == [("c001", 1)]
        assert task.annotation_task_id == annotation.task_id
        assert task.annotation_state == annotation_state
        assert task.evidence_view == (
            "Published observation" if annotation_state == "completed" else None
        )
        assert task.reserved_candidate_ids == []
        assert lanes.api.ensure_tasks("run_test") == ensured
        assert lanes.store.load_schedule_task("run_test", "c001", 1) == task
    else:
        assert ensured == [] and task is None
    assert hard_state(lanes.store) == before


@pytest.mark.parametrize(
    "mismatch", ["settlement_id", "attempt_commit", "attempt_base_commit"]
)
def test_annotation_identity_mismatch_cannot_admit_a_task(lanes, mismatch):
    task = annotate(lanes)
    setattr(task, mismatch, "other")
    before = snapshot(lanes.store)
    with pytest.raises(RuntimeError, match="settled iteration"):
        lanes.api.ensure_tasks("run_test")
    assert snapshot(lanes.store) == before


@pytest.mark.parametrize(
    "field",
    [
        "attempt_commit",
        "frozen_spec_id",
        "settlement_id",
        "annotation_task_id",
        "reward_policy_ref",
    ],
)
def test_locked_admission_rejects_changes_to_existing_task_identity(lanes, field):
    annotate(lanes)
    lanes.api.ensure_tasks("run_test")
    task = lanes.store.load_schedule_task("run_test", "c001", 1)
    lanes.store.write_schedule_task(task.model_copy(update={field: "other"}))
    before = snapshot(lanes.store)
    with lanes.store.lock_run("run_test"):
        run = lanes.store.read_run("run_test")
        frozen = lanes.store.read_frozen_spec(run.frozen_spec_id)
        record = lanes.store.read_candidate("run_test", "c001")
        with pytest.raises(RuntimeError, match="immutable"):
            lanes.api.create_task_locked(run, frozen, record, record.iterations[0])
    assert snapshot(lanes.store) == before


def test_consumption_rejects_a_task_bound_to_another_hard_commit(lanes):
    annotate(lanes)
    lanes.api.ensure_tasks("run_test")
    finish(lanes)
    record = lanes.store.read_candidate("run_test", "c001")
    record.iterations[0].git_head = "different_commit"
    save_candidate(lanes.store, record)
    before = snapshot(lanes.store)
    with pytest.raises(RuntimeError, match="hard-settled iteration"):
        lanes.api.consume_tasks("run_test", "c001")
    assert snapshot(lanes.store) == before


def test_admission_and_consumption_advance_in_lane_iteration_order(lanes):
    annotate(lanes)
    append_iteration(lanes)
    annotate(lanes, 2)
    before = hard_state(lanes.store)
    assert lanes.api.ensure_tasks("run_test") == [("c001", 1)]
    lanes.api.consume_tasks("run_test", "c001")
    assert (
        lanes.store.read_candidate("run_test", "c001").schedule_settlement_watermark
        == 0
    )
    finish(lanes)
    assert lanes.api.ensure_tasks("run_test") == [("c001", 1), ("c001", 2)]
    finish(lanes, 2)
    lanes.api.consume_tasks("run_test", "c001", through_iteration=1)
    assert (
        lanes.store.read_candidate("run_test", "c001").schedule_settlement_watermark
        == 1
    )
    assert lanes.store.load_schedule_task("run_test", "c001", 2).consumed_at is None
    lanes.api.consume_tasks("run_test", "c001")
    consumed = lanes.store.load_schedule_tasks("run_test")
    assert len(lanes.api.list_rewards("run_test")) == 2
    assert (
        lanes.store.read_candidate("run_test", "c001").schedule_settlement_watermark
        == 2
    )
    lanes.clock.epoch += 30
    lanes.api.consume_tasks("run_test", "c001")
    assert lanes.store.load_schedule_tasks("run_test") == consumed
    assert hard_state(lanes.store) == before


@pytest.mark.parametrize("stage", ["task", "projection", "index"])
def test_admission_replays_the_same_task_after_partial_write(lanes, monkeypatch, stage):
    annotate(lanes)
    name = {
        "task": "write_schedule_task",
        "projection": "write_candidate_projection",
        "index": "write_candidate_index",
    }[stage]
    original = getattr(lanes.store, name)

    def fail(*args, **kwargs):
        raise OSError("injected admission write failure")

    before = hard_state(lanes.store)
    monkeypatch.setattr(lanes.store, name, fail)
    with pytest.raises(OSError, match="admission write failure"):
        lanes.api.ensure_tasks("run_test")
    first = lanes.store.load_schedule_task("run_test", "c001", 1)
    assert (first is None) == (stage == "task")
    monkeypatch.setattr(lanes.store, name, original)
    lanes.api.ensure_tasks("run_test")
    tasks = lanes.store.load_schedule_tasks("run_test")
    assert len(tasks) == 1 and (first is None or tasks[0] == first)
    assert hard_state(lanes.store) == before


@pytest.mark.parametrize(
    "stage", ["write_candidate_projection", "write_candidate_index"]
)
@pytest.mark.parametrize("consumed", [False, True])
def test_confirmation_reservation_counter_recovers_after_partial_admission(
    lanes, monkeypatch, stage, consumed
):
    prepare_confirmation(lanes)
    annotate(lanes, 2)
    original = getattr(lanes.store, stage)

    def fail(*args, **kwargs):
        raise OSError("reservation projection failed")

    before = hard_state(lanes.store)
    monkeypatch.setattr(lanes.store, stage, fail)
    with pytest.raises(OSError, match="reservation projection failed"):
        lanes.api.ensure_tasks("run_test")
    admitted = lanes.store.load_schedule_task("run_test", "c001", 2)
    assert admitted.reserved_candidate_ids == ["c002"]
    assert lanes.store.read_run("run_test").next_candidate_index == 2
    monkeypatch.setattr(lanes.store, stage, original)
    if consumed:
        finish(lanes, 2)
        lanes.api.consume_tasks("run_test", "c001")
        admitted = lanes.store.load_schedule_task("run_test", "c001", 2)
    lanes.api.ensure_tasks("run_test")
    assert lanes.store.load_schedule_task("run_test", "c001", 2) == admitted
    run = lanes.store.read_run("run_test")
    assert run.next_candidate_index == 3
    with lanes.store.lock_run("run_test"):
        more = lanes.api.reserve_candidate_ids(
            run, lanes.store.read_frozen_spec(run.frozen_spec_id)
        )
    assert more == ["c003"]
    assert hard_state(lanes.store) == before


@pytest.mark.parametrize("terminal", ["completed", "terminal_error"])
def test_terminal_reservations_release_capacity_without_reusing_identity(
    lanes, terminal
):
    prepare_confirmation(lanes)
    annotate(lanes, 2)
    lanes.api.ensure_tasks("run_test")
    if terminal == "completed":
        finish(lanes, 2)
    else:
        task = lanes.store.load_schedule_task("run_test", "c001", 2)
        lanes.store.write_schedule_task(
            task.model_copy(update={"state": terminal, "last_error": "failed"})
        )
    before = snapshot(lanes.store)
    with lanes.store.lock_run("run_test"):
        run = lanes.store.read_run("run_test")
        run.next_candidate_index = 2
        reserved = lanes.api.reserve_candidate_ids(
            run, lanes.store.read_frozen_spec(run.frozen_spec_id)
        )
    assert reserved == ["c003"] and run.next_candidate_index == 4
    assert snapshot(lanes.store) == before


def test_each_reservation_skips_sparse_allocated_ids(lanes):
    prepare_confirmation(lanes)
    annotate(lanes, 2)
    lanes.api.ensure_tasks("run_test")
    task = lanes.store.load_schedule_task("run_test", "c001", 2)
    lanes.store.write_schedule_task(
        task.model_copy(update={"reserved_candidate_ids": ["c003"]})
    )
    peer = lanes.store.read_candidate("run_test", "c001").model_copy(deep=True)
    peer.candidate_id = peer.task.candidate_id = peer.score_report.candidate_id = "c009"
    write_json(
        lanes.store.run_dir("run_test") / "candidates/c009/candidate.json",
        peer.model_dump(mode="json"),
    )
    before = snapshot(lanes.store)
    with lanes.store.lock_run("run_test"):
        run = lanes.store.read_run("run_test")
        run.next_candidate_index = 2
        frozen = lanes.store.read_frozen_spec(run.frozen_spec_id)
        frozen.spec.budget.max_parallel = 3
        reserved = lanes.api.reserve_candidate_ids(run, frozen)
    assert reserved == ["c002", "c004"] and run.next_candidate_index == 5
    assert snapshot(lanes.store) == before


@pytest.mark.parametrize("stage", ["write_schedule_task", "write_candidate_projection"])
def test_consumption_retries_without_repeating_inference_or_usage(
    lanes, monkeypatch, stage
):
    annotate(lanes)
    lanes.api.ensure_tasks("run_test")
    finish(lanes)
    original = getattr(lanes.store, stage)

    def fail(*args, **kwargs):
        raise OSError("injected consumption write failure")

    before = hard_state(lanes.store)
    monkeypatch.setattr(lanes.store, stage, fail)
    with pytest.raises(OSError, match="consumption write failure"):
        lanes.api.consume_tasks("run_test", "c001")
    assert (
        lanes.store.read_candidate("run_test", "c001").schedule_settlement_watermark
        == 0
    )
    monkeypatch.setattr(lanes.store, stage, original)
    lanes.api.consume_tasks("run_test", "c001")
    assert (
        lanes.store.read_candidate("run_test", "c001").schedule_settlement_watermark
        == 1
    )
    assert lanes.store.load_schedule_task("run_test", "c001", 1).usage == {
        "total_tokens": 17
    }
    assert hard_state(lanes.store) == before


def test_confirmation_barrier_tracks_task_and_decision_not_host_liveness(lanes):
    prepare_confirmation(lanes)
    record = lanes.store.read_candidate("run_test", "c001")
    assert lanes.api.candidate_execution_barrier(record)
    annotate(lanes, 2)
    lanes.api.ensure_tasks("run_test")
    task = lanes.store.load_schedule_task("run_test", "c001", 2)
    assert task.reserved_candidate_ids == ["c002"]
    assert lanes.store.read_run("run_test").next_candidate_index == 3
    assert lanes.api.candidate_execution_barrier(record)
    finish(lanes, 2, allocate=True, decision=True)
    assert lanes.api.candidate_execution_barrier(record)
    assert lanes.api.pending_decision_tasks("run_test") == []
    lanes.api.consume_tasks("run_test", "c001")
    pending = lanes.api.pending_decision_tasks("run_test")
    assert len(pending) == 1 and pending[0].task_id == task.task_id
    lanes.api.bind_decision(pending[0], "allocation_1", ["c002"])
    assert lanes.api.candidate_execution_barrier(record) is None
    assert lanes.api.pending_decision_tasks("run_test") == []
    lanes.api.bind_decision(pending[0], "allocation_1", ["c002"])
    before = snapshot(lanes.store)
    with pytest.raises(RuntimeError, match="another Decision"):
        lanes.api.bind_decision(pending[0], "allocation_2", ["c003"])
    assert snapshot(lanes.store) == before


@pytest.mark.parametrize("outcome", ["decline", "terminal_error"])
def test_terminal_confirmation_can_release_the_lane_without_a_decision(lanes, outcome):
    prepare_confirmation(lanes)
    annotate(lanes, 2)
    lanes.api.ensure_tasks("run_test")
    if outcome == "decline":
        finish(lanes, 2)
    else:
        task = lanes.store.load_schedule_task("run_test", "c001", 2)
        lanes.store.write_schedule_task(
            task.model_copy(update={"state": "terminal_error", "last_error": "failed"})
        )
    assert (
        lanes.api.candidate_execution_barrier(
            lanes.store.read_candidate("run_test", "c001")
        )
        is None
    )
    before = hard_state(lanes.store)
    lanes.api.consume_tasks("run_test", "c001")
    assert (
        lanes.store.read_candidate("run_test", "c001").schedule_settlement_watermark
        == 2
    )
    assert hard_state(lanes.store) == before


@pytest.mark.parametrize("closed", [False, True])
def test_inactive_run_rejects_admission_and_consumption_without_writes(lanes, closed):
    annotate(lanes)
    run = lanes.store.read_run("run_test")
    if closed:
        run.state = RunState.PROMOTED
    else:
        run.invalidated_at = lanes.clock.timestamp()
    write_json(
        lanes.store.run_dir("run_test") / "run.json", run.model_dump(mode="json")
    )
    before = snapshot(lanes.store)
    assert (
        lanes.api.ensure_tasks("run_test")
        == lanes.api.unsettled_tasks("run_test")
        == []
    )
    lanes.api.consume_tasks("run_test", "c001")
    assert snapshot(lanes.store) == before


@pytest.mark.parametrize("deadline", ["invalid", "expired", "future"])
def test_scheduler_freezes_host_deadline_without_changing_annotation_policy(
    lanes, monkeypatch, deadline
):
    annotate(lanes)
    value = (
        "not-a-deadline"
        if deadline == "invalid"
        else str(lanes.clock.now() + (60 if deadline == "future" else 0))
    )
    monkeypatch.setattr(
        lanes.api.context.source,
        "read_host_settings",
        lambda: SearchHostSettings(outer_deadline_at=value),
    )
    before = hard_state(lanes.store)
    lanes.api.ensure_tasks("run_test")
    task = lanes.store.load_schedule_task("run_test", "c001", 1)
    assert task.outer_deadline_at == value
    assert task.state == ("pending" if deadline == "future" else "terminal_error")
    assert (task.profile is not None) == (deadline == "future")
    assert hard_state(lanes.store) == before


def test_store_projection_cannot_overwrite_other_owners_fields(lanes):
    old = lanes.store.read_candidate("run_test", "c001")
    projection = SchedulerCandidateProjection(
        candidate_id="c001",
        iterations=(),
        settlement_watermark=0,
        report=SchedulerReportProjection("schedule_1", "pending"),
    )
    current = old.model_copy(deep=True)
    current.score_report.aggregate_score = 8
    current.inner_agent_state = {"owned_elsewhere": "new fact"}
    current.iterations[0].score = 8
    save_candidate(lanes.store, current)
    task_path = lanes.store.run_dir("run_test") / "candidates/c001/task.json"
    task_bytes = task_path.read_bytes()
    with lanes.store.lock_run("run_test"):
        lanes.store.write_candidate_projection("run_test", projection)
    refreshed = lanes.store.read_candidate("run_test", "c001")
    assert refreshed.score_report.aggregate_score == refreshed.iterations[0].score == 8
    assert refreshed.inner_agent_state == {"owned_elsewhere": "new fact"}
    assert refreshed.score_report.schedule_task_id == "schedule_1"
    assert task_path.read_bytes() == task_bytes
    task_path.unlink()
    with lanes.store.lock_run("run_test"):
        lanes.store.write_candidate_projection("run_test", projection)
    assert task_path.read_bytes() == task_bytes
