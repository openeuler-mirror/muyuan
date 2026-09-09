"""Scheduler task API tests without Runtime, candidates or model inference."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from goal_plus.application.scheduler import SchedulerWorkflow
from goal_plus.domain.adaptive.models import (
    ResolvedSchedulerProfile,
    SchedulerInvocationRecord,
    SchedulerOutput,
    SchedulerReward,
    ScheduleTask,
)
from goal_plus.domain.adaptive.scheduler import (
    PermanentSchedulerError,
    TransientSchedulerError,
)
from goal_plus.domain.search import RunState
from goal_plus.domain.serialization import sha256_text
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.scheduler import FileSchedulerState
from goal_plus.ports.scheduler import SchedulerResult
from goal_plus.ports.state import RunRecord
from tests.test_annotation_workflow import Clock


class State:
    def __init__(self, clock):
        self.run = RunRecord(
            run_id="run_test",
            frozen_spec_id="frozen_test",
            source_path="project",
            state=RunState.RUNNING,
            created_at=clock.timestamp(),
            best_score=2.5,
        )
        self.task = ScheduleTask(
            task_id="schedule_1",
            frozen_spec_id="frozen_test",
            settlement_id="settled_1",
            node_id="c001/iteration-0001",
            annotation_task_id="annotation_1",
            annotation_state="completed",
            evidence_view="Changed implementation",
            reward_policy_ref="evidence_llm_value/v3",
            allocation_policy_ref="value_guided_replace/v2",
            run_id="run_test",
            candidate_id="c001",
            iteration=1,
            attempt_base_commit="base",
            attempt_commit="head",
            attempt_changed_files=["main.py"],
            task_context_source="frozen_objective",
            task_context_sha256=sha256_text("objective"),
            profile=ResolvedSchedulerProfile(
                host="codex",
                model="test-model",
                reasoning_effort="low",
                timeout_seconds=90,
            ),
            created_at=clock.timestamp(),
            updated_at=clock.timestamp(),
        )
        self.locked = False
        self.fail_write = False
        self.other_tasks = {}

    @contextmanager
    def lock_run(self, run_id):
        assert run_id == self.run.run_id and not self.locked
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def read_run(self, run_id):
        assert run_id == self.run.run_id
        return self.run.model_copy(deep=True)

    def load_schedule_task(self, run_id, candidate_id, iteration):
        assert (run_id, candidate_id) == ("run_test", "c001")
        task = (
            self.task
            if self.task and self.task.iteration == iteration
            else self.other_tasks.get(iteration)
        )
        return task.model_copy(deep=True) if task else None

    def write_schedule_task(self, task):
        assert self.locked
        if self.fail_write:
            raise OSError("task write failed")
        self.task = task.model_copy(deep=True)

    def load_schedule_tasks(self, run_id, candidate_id=None):
        return [self.load_schedule_task(run_id, "c001", 1)] if self.task else []

    def attempt_monitor_reference(self, run_id, candidate_id, iteration, attempt):
        return f"runs/{run_id}/scheduler/attempts/{candidate_id}-iteration-{iteration:04d}-attempt-{attempt:02d}.json"


@pytest.fixture
def workflow():
    clock = Clock()
    state = State(clock)
    return SchedulerWorkflow(tasks=state, runs=state, clock=clock), state, clock


def claim(api):
    return api.claim("run_test", "c001", 1)


def result(clock):
    return SchedulerResult(
        output=SchedulerOutput(
            reward=SchedulerReward(
                reward=1,
                rubric_score=10,
                exploitation=1,
                exploration=0,
                uct=1,
                rationale="Measured progress",
            ),
            allocation_flag=False,
            allocation_rationale="Continue",
            allocation_confidence=1,
        ),
        usage={"total_tokens": 17},
        invocation=SchedulerInvocationRecord(
            invocation_id="inference_1",
            host="codex",
            model="test-model",
            reasoning_effort="low",
            started_at=clock.timestamp(),
            completed_at=clock.timestamp(),
            elapsed_seconds=0,
        ),
    )


def test_success_and_duplicate_completion_leave_hard_state_unchanged(workflow):
    api, state, clock = workflow
    hard = state.run.model_dump()
    claimed = claim(api)
    assert claimed.state == "running" and claimed.attempts == 1
    assert outer_deadline_epoch(claimed.next_attempt_at) == clock.now() + 90
    assert claimed.attempt_history[0]["monitor_path"].endswith("attempt-01.json")
    observed = result(clock)
    assert api.complete(claimed, result=observed)
    assert not api.complete(claimed, result=observed)
    assert (
        state.task.output == observed.output
        and state.task.invocation == observed.invocation
    )
    assert state.task.usage == {"total_tokens": 17}
    assert state.task.next_attempt_at is None
    assert state.run.model_dump() == hard


def test_live_lease_cannot_be_claimed_and_expired_attempt_is_fenced(workflow):
    api, state, clock = workflow
    first = claim(api)
    clock.epoch += 89
    before = state.task.model_dump()
    assert claim(api) is None and state.task.model_dump() == before
    clock.epoch += 1
    second = claim(api)
    assert second.attempts == 2
    assert not api.complete(first, result=result(clock))
    assert api.complete(second, result=result(clock))
    assert state.task.usage == {"total_tokens": 17}


def test_retry_policy_uses_scheduler_backoff_and_accumulates_usage(workflow):
    api, state, clock = workflow
    for attempt, delay in [(1, 1), (2, 5)]:
        claimed = claim(api)
        assert claimed.attempts == attempt
        assert not api.complete(
            claimed,
            error=TransientSchedulerError("temporary", usage={"total_tokens": 3}),
        )
        assert state.task.state == "retry_wait"
        assert outer_deadline_epoch(state.task.next_attempt_at) == clock.now() + delay
        assert claim(api) is None
        clock.epoch += delay
    assert api.complete(claim(api), result=result(clock))
    assert state.task.usage == {"total_tokens": 23}
    assert len(state.task.attempt_history) == 3
    assert state.task.error_fingerprint is None


@pytest.mark.parametrize("reason", ["permanent", "attempt_limit", "closed"])
def test_failed_attempt_becomes_terminal_at_its_own_gate(workflow, reason):
    api, state, clock = workflow
    if reason == "attempt_limit":
        state.task.attempts = 2
    claimed = claim(api)
    if reason == "closed":
        state.run.state = RunState.PROMOTED
    error_type = (
        PermanentSchedulerError if reason == "permanent" else TransientSchedulerError
    )
    assert not api.complete(claimed, error=error_type("failure"))
    assert state.task.state == "terminal_error" and state.task.next_attempt_at is None
    assert state.task.output is None


@pytest.mark.parametrize(
    "reason",
    ["missing", "closed", "invalidated", "completed", "terminal", "deadline", "limit"],
)
def test_claim_rejects_invalid_prestates_without_changing_hard_results(
    workflow, reason
):
    api, state, clock = workflow
    if reason == "missing":
        state.task = None
    elif reason == "closed":
        state.run.state = RunState.PROMOTED
    elif reason == "invalidated":
        state.run.invalidated_at = clock.timestamp()
    elif reason == "completed":
        api.complete(claim(api), result=result(clock))
    elif reason == "terminal":
        api.complete(claim(api), error=PermanentSchedulerError("failure"))
    elif reason == "deadline":
        state.task.outer_deadline_at = clock.timestamp()
    else:
        state.task.attempts = 3
    before = state.task.model_dump() if state.task else None
    hard = state.run.model_dump()
    assert claim(api) is None
    assert state.run.model_dump() == hard
    if reason in {"deadline", "limit"}:
        assert state.task.state == "terminal_error"
    else:
        assert (state.task.model_dump() if state.task else None) == before


@pytest.mark.parametrize("stage", ["claim", "completion"])
def test_storage_failure_does_not_publish_or_double_count(workflow, stage):
    api, state, clock = workflow
    claimed = claim(api) if stage == "completion" else None
    before = state.task.model_dump()
    state.fail_write = True
    with pytest.raises(OSError, match="write failed"):
        if claimed:
            api.complete(claimed, result=result(clock))
        else:
            claim(api)
    assert not state.locked and state.task.model_dump() == before
    state.fail_write = False
    assert api.complete(claimed or claim(api), result=result(clock))
    assert state.task.usage == {"total_tokens": 17}


def test_file_layout_reopens_the_same_task_and_claim(tmp_path):
    clock = Clock()
    fixture = State(clock)
    store = FileSchedulerState(tmp_path / ".gp")
    write_json(
        store.run_dir("run_test") / "run.json", fixture.run.model_dump(mode="json")
    )
    store.write_schedule_task(fixture.task)
    api = SchedulerWorkflow(tasks=store, runs=store, clock=clock)
    claimed = claim(api)
    reopened = FileSchedulerState(store.root_dir)
    assert reopened.load_schedule_task("run_test", "c001", 1) == claimed
    assert reopened.load_schedule_tasks("run_test", "c001") == [claimed]
    assert (
        store.root_dir
        / "runs/run_test/candidates/c001/schedule-tasks/iteration-0001.json"
    ).is_file()
    assert SchedulerWorkflow(tasks=reopened, runs=reopened, clock=clock).complete(
        claimed, result=result(clock)
    )


def proposed_result(clock, *, decision):
    observed = result(clock)
    output = observed.output.model_dump()
    output["allocation_flag"] = True
    if decision:
        output["decision"] = {
            "rationale": "Try a distinct direction",
            "replacements": [
                {
                    "retire_candidate_id": "c001",
                    "new_candidate_id": "c002",
                    "source_candidate_id": "c001",
                    "source_iteration": 1,
                    "source_node_id": "c001/iteration-0001",
                    "rationale": "Stalled lane",
                    "exploration_direction": "Different implementation",
                }
            ],
        }
    return replace(observed, output=SchedulerOutput.model_validate(output))


def prepare_confirmation(state, clock):
    state.other_tasks[1] = state.task.model_copy(
        deep=True,
        update={
            "state": "completed",
            "output": proposed_result(clock, decision=False).output,
        },
    )
    state.task = state.task.model_copy(
        deep=True,
        update={
            "task_id": "schedule_2",
            "iteration": 2,
            "settlement_id": "settled_2",
            "node_id": "c001/iteration-0002",
            "reserved_candidate_ids": ["c002"],
        },
    )


@pytest.mark.parametrize("phase", ["screen", "confirm"])
def test_direct_completion_rejects_wrong_confirmation_output_without_writes(
    workflow, phase
):
    api, state, clock = workflow
    if phase == "confirm":
        prepare_confirmation(state, clock)
    claimed = api.claim("run_test", "c001", state.task.iteration)
    before = state.task.model_dump()
    hard = state.run.model_dump()
    observed = proposed_result(clock, decision=phase == "screen")
    with pytest.raises(TransientSchedulerError, match="Decision"):
        api.complete(claimed, result=observed)
    assert state.task.model_dump() == before
    assert state.run.model_dump() == hard


@pytest.mark.parametrize("phase", ["screen", "confirm"])
def test_direct_completion_accepts_the_canonical_confirmation_phase(workflow, phase):
    api, state, clock = workflow
    if phase == "confirm":
        prepare_confirmation(state, clock)
    claimed = api.claim("run_test", "c001", state.task.iteration)
    observed = proposed_result(clock, decision=phase == "confirm")
    assert api.complete(claimed, result=observed)
    assert state.task.output == observed.output


def test_confirmation_can_decline_allocation(workflow):
    api, state, clock = workflow
    prepare_confirmation(state, clock)
    claimed = api.claim("run_test", "c001", 2)
    assert api.complete(claimed, result=result(clock))
    assert not state.task.output.allocation_flag


def test_protocol_error_can_be_recorded_as_a_retry_without_publishing(workflow):
    api, state, clock = workflow
    claimed = claim(api)
    with pytest.raises(TransientSchedulerError) as failure:
        api.complete(claimed, result=proposed_result(clock, decision=True))
    assert not api.complete(claimed, error=failure.value)
    assert state.task.state == "retry_wait" and state.task.output is None
    assert state.task.usage == {}


def test_stale_claim_cannot_validate_or_publish_a_protocol_error(workflow):
    api, state, clock = workflow
    first = claim(api)
    clock.epoch += 90
    claim(api)
    before = state.task.model_dump()
    assert not api.complete(first, result=proposed_result(clock, decision=True))
    assert state.task.model_dump() == before
