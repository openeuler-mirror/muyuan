"""Annotation API behavior without Runtime, model executors, or workspaces."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.domain.annotation import (
    AnnotationOutputError,
    EvidenceAnnotationQueue,
    EvidenceAnnotationTask,
    EvidenceComparisonReference,
    PermanentAnnotationError,
    ToolViewOutput,
    TransientAnnotationError,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import RunState
from goal_plus.domain.shared_tools import SharedToolRecord
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.infrastructure.state.annotation import FileAnnotationState
from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.annotation import AnnotationCompletion, EvidenceAnnotationResult
from goal_plus.ports.state import RunRecord


class Clock:
    def __init__(self) -> None:
        self.epoch = 1_783_382_400.0

    def now(self) -> float:
        return self.epoch

    def timestamp(self) -> str:
        return self.timestamp_from_epoch(self.epoch)

    def timestamp_from_epoch(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )


class State:
    def __init__(self, clock: Clock) -> None:
        self.run = RunRecord(
            run_id="run_test",
            state=RunState.RUNNING,
            frozen_spec_id="frozen_test",
            source_path="project",
            created_at=clock.timestamp(),
            best_score=2.5,
        )
        self.queue = EvidenceAnnotationQueue(
            run_id=self.run.run_id,
            revision=1,
            accepted_tasks=1,
            created_at=clock.timestamp(),
            updated_at=clock.timestamp(),
        )
        artifact = ArtifactRef(
            kind="fs_snapshot", provider="thinkthread", id="fsnap-attempt"
        )
        self.task = EvidenceAnnotationTask(
            task_id="annotation_settled",
            settlement_id="settled",
            run_id=self.run.run_id,
            candidate_id="c001",
            iteration=1,
            queue_revision=1,
            attempt_artifact_ref=artifact,
            attempt_base_artifact_ref=ArtifactRef(
                kind="fs_snapshot",
                provider="thinkthread",
                id="fsnap-base",
            ),
            created_at=clock.timestamp(),
            updated_at=clock.timestamp(),
        )
        self.iteration = IterationRecord(
            iteration=1,
            settlement_id="settled",
            agent_session_id="session_c001",
            artifact_ref=artifact,
            score=2.5,
            process_passed=True,
            created_at=clock.timestamp(),
        )
        self.run_locked = False
        self.tasks_locked = False
        self.fail_write = False
        self.fail_publish = False
        self.pending_ids: set[str] = set()
        self.published: list[SharedToolRecord] = []
        self.rejected: list[SharedToolRecord] = []

    @contextmanager
    def lock_run(self, run_id: str):
        assert run_id == self.run.run_id and not self.run_locked
        assert not self.tasks_locked, "run lock cannot be acquired inside task lock"
        self.run_locked = True
        try:
            yield
        finally:
            self.run_locked = False

    @contextmanager
    def lock_tasks(self, run_id: str):
        assert run_id == self.run.run_id and not self.tasks_locked
        self.tasks_locked = True
        try:
            yield
        finally:
            self.tasks_locked = False

    def read_run(self, run_id: str) -> RunRecord:
        assert run_id == self.run.run_id
        return self.run.model_copy(deep=True)

    def read_queue(self, run_id: str) -> EvidenceAnnotationQueue | None:
        assert run_id == self.run.run_id
        return self.queue.model_copy(deep=True) if self.queue is not None else None

    def write_queue(self, queue: EvidenceAnnotationQueue) -> None:
        assert self.tasks_locked
        self.queue = queue.model_copy(deep=True)

    def read_task(
        self, run_id: str, candidate_id: str, iteration: int
    ) -> EvidenceAnnotationTask:
        assert (run_id, candidate_id, iteration) == (self.run.run_id, "c001", 1)
        return self.task.model_copy(deep=True)

    def write_task(self, task: EvidenceAnnotationTask) -> None:
        assert self.tasks_locked
        if self.fail_write:
            raise OSError("task write failed")
        self.task = task.model_copy(deep=True)

    def list_tasks(self, run_id: str) -> list[EvidenceAnnotationTask]:
        return [self.read_task(run_id, "c001", 1)]

    def read_iterations(self, run_id: str) -> dict[str, list[IterationRecord]]:
        return {"c001": self.read_candidate_iterations(run_id, "c001")}

    def read_candidate_iterations(
        self, run_id: str, candidate_id: str
    ) -> list[IterationRecord]:
        assert (run_id, candidate_id) == (self.run.run_id, "c001")
        return [self.iteration.model_copy(deep=True)]

    def attempt_monitor_reference(
        self, run_id: str, candidate_id: str, iteration: int, attempt: int
    ) -> str:
        return f"runs/{run_id}/attempts/{candidate_id}-{iteration}-{attempt}.json"

    def pending_tool_ids(self, run_id: str) -> set[str]:
        assert run_id == self.run.run_id
        return set(self.pending_ids)

    def advance_heads(self, run_id: str, tools: list[SharedToolRecord]) -> None:
        assert run_id == self.run.run_id and self.run_locked
        assert self.task.state == "completed" and self.task.view is not None
        if self.fail_publish:
            raise OSError("head write failed")
        self.published.extend(tools)
        self.pending_ids.difference_update(tool.tool_id for tool in tools)

    def reject_heads(self, run_id: str, tools: list[SharedToolRecord]) -> None:
        assert run_id == self.run.run_id and self.run_locked
        self.rejected.extend(tools)
        self.pending_ids.difference_update(tool.tool_id for tool in tools)


@pytest.fixture
def workflow():
    clock = Clock()
    state = State(clock)
    return (
        AnnotationWorkflow(tasks=state, evidence=state, tools=state, clock=clock),
        state,
        clock,
    )


@pytest.mark.parametrize("field", ["attempts", "queue_generation", "queue_revision"])
def test_bound_task_rejects_stale_host_identity_without_mutation(workflow, field):
    api, state, _ = workflow
    claimed = claim(api)
    identity = {
        "run_id": claimed.run_id, "candidate_id": claimed.candidate_id,
        "iteration": claimed.iteration, "attempts": claimed.attempts,
        "queue_generation": claimed.queue_generation, "queue_revision": claimed.queue_revision,
    }
    assert api.bound_task(**identity) == claimed
    before = state.task.model_dump(mode="json")
    identity[field] += 1
    with pytest.raises(PermissionError, match="binding is stale"):
        api.bound_task(**identity)
    assert state.task.model_dump(mode="json") == before


def claim(api: AnnotationWorkflow) -> EvidenceAnnotationTask:
    task = api.claim("run_test", "c001", 1)
    assert task is not None
    return task


def result(**kwargs) -> EvidenceAnnotationResult:
    return EvidenceAnnotationResult(
        description="Measured artifact change.", usage={"total_tokens": 17}, **kwargs
    )


def test_claim_complete_and_duplicate_preserve_hard_evidence(workflow) -> None:
    api, state, clock = workflow
    hard_before = (state.run.model_dump(), state.iteration.model_dump())
    assert api.pending("run_test") == [("c001", 1)]
    task = claim(api)
    assert task.state == "retry_wait" and task.attempts == 1
    assert outer_deadline_epoch(task.next_attempt_at) == clock.epoch + 30
    assert task.attempt_history == [
        {
            "attempt": 1,
            "started_at": clock.timestamp(),
            "monitor_path": "runs/run_test/attempts/c001-1-1.json",
        }
    ]
    assert api.complete(task, result=result()) == AnnotationCompletion(True, True)
    assert state.task.state == "completed" and state.task.next_attempt_at is None
    assert state.task.view.attempt_artifact_ref == task.attempt_artifact_ref
    assert state.task.view.attempt_commit is None
    assert state.task.usage == {"total_tokens": 17}
    assert state.task.attempt_history[-1]["finished_at"] == clock.timestamp()
    committed = state.task.model_dump()
    assert api.complete(task, result=result()) == AnnotationCompletion(False, False)
    assert api.claim("run_test", "c001", 1) is None
    assert state.task.model_dump() == committed
    assert api.pending("run_test") == []
    assert (state.run.model_dump(), state.iteration.model_dump()) == hard_before


def test_retries_keep_usage_and_exact_attempt_ceiling(workflow) -> None:
    api, state, clock = workflow
    for attempt, delay in [(1, 30), (2, 120), (3, 120)]:
        task = claim(api)
        assert task.attempts == attempt
        completion = api.complete(
            task, error=TransientAnnotationError("retry", usage={"total_tokens": 2})
        )
        assert completion == AnnotationCompletion(True, False)
        assert state.task.usage == {"total_tokens": attempt * 2}
        if attempt < 3:
            assert api.retry_delay("run_test") == delay
            unchanged = state.task.model_dump()
            clock.epoch += delay - 1
            assert api.claim("run_test", "c001", 1) is None
            assert state.task.model_dump() == unchanged
            clock.epoch += 1
    assert state.task.state == "terminal_error"
    assert state.task.next_attempt_at is None
    assert api.retry_delay("run_test") is None


@pytest.mark.parametrize("change", ["generation", "revision", "attempt", "closed"])
def test_stale_completion_has_no_domain_write(workflow, change: str) -> None:
    api, state, _ = workflow
    task = claim(api)
    if change == "generation":
        state.queue.generation += 1
    elif change == "revision":
        state.task.queue_revision += 1
    elif change == "attempt":
        state.task.attempts += 1
    else:
        with state.lock_run("run_test"):
            api.close_queue("run_test", reason="selected")
    before = (
        state.task.model_dump(),
        state.queue.model_dump(),
        state.run.model_dump(),
        state.iteration.model_dump(),
    )
    assert api.complete(task, result=result()) == AnnotationCompletion(False, False)
    assert (
        state.task.model_dump(),
        state.queue.model_dump(),
        state.run.model_dump(),
        state.iteration.model_dump(),
    ) == before


@pytest.mark.parametrize("stage", ["claim", "publish", "rescan"])
def test_deadline_at_boundary_does_not_publish(workflow, stage: str) -> None:
    api, state, clock = workflow
    state.task.outer_deadline_at = clock.timestamp_from_epoch(clock.epoch + 1)
    task = claim(api) if stage == "publish" else None
    clock.epoch += 1
    if stage == "publish":
        assert api.complete(task, result=result()) == AnnotationCompletion(True, False)
        assert state.task.usage == {"total_tokens": 17}
    elif stage == "rescan":
        assert api.retry_delay("run_test") is None
    else:
        assert api.claim("run_test", "c001", 1) is None
    assert state.task.state == "terminal_error"
    assert state.task.view is None
    assert "deadline" in state.task.last_error


def test_replaced_attempt_rejects_late_result_after_retry(workflow) -> None:
    api, state, clock = workflow
    first = claim(api)
    clock.epoch += 30
    second = claim(api)
    assert api.complete(first, result=result()) == AnnotationCompletion(False, False)
    assert api.complete(second, result=result()) == AnnotationCompletion(True, True)
    assert state.task.usage == {"total_tokens": 17}


def test_invalid_comparison_rejected_before_task_write(workflow) -> None:
    api, state, _ = workflow
    task = claim(api)
    before = state.task.model_dump()
    with pytest.raises(AnnotationOutputError, match="comparison basis") as raised:
        api.complete(
            task,
            result=result(
                comparison_basis=[
                    EvidenceComparisonReference(
                        candidate_id="peer",
                        iteration=1,
                        commit="peer-commit",
                    )
                ]
            ),
        )
    assert raised.value.usage == {"total_tokens": 17}
    assert state.task.model_dump() == before


def tool(state: State, clock: Clock) -> SharedToolRecord:
    record = SharedToolRecord(
        tool_id="tool_1",
        family_id="family_1",
        version=1,
        candidate_id="c001",
        iteration=1,
        snapshot_hash="immutable-hash",
        source_commit="source-commit",
        name="helper",
        entrypoint="helper.py",
        source_relative_path="shared_tools/helper",
        read_only_path=Path("tools/helper"),
        size_bytes=10,
        created_at=clock.timestamp(),
    )
    state.iteration.shared_tools = [record]
    state.pending_ids.add(record.tool_id)
    return record


def tool_output(tool_id: str = "tool_1") -> ToolViewOutput:
    return ToolViewOutput(
        tool_id=tool_id,
        summary="Reusable helper",
        capabilities=["transformation"],
        when_to_use="For this input",
        entrypoint="untrusted-entrypoint",
        inputs=["data"],
        outputs=["result"],
        dependencies=[],
        adoption_steps=["Call helper"],
        limitations=["limited scope"],
    )


def test_tool_view_bound_before_publication_and_recoverable_after_head_failure(
    workflow,
) -> None:
    api, state, clock = workflow
    record = tool(state, clock)
    task = claim(api)
    hard_before = state.iteration.model_dump()
    state.fail_publish = True
    with pytest.raises(OSError, match="head write"):
        api.complete(task, result=result(tool_views=[tool_output()]))
    assert state.task.state == "completed"
    view = state.task.view.tool_views[0]
    assert (view.snapshot_hash, view.source_commit, view.entrypoint) == (
        record.snapshot_hash,
        record.source_commit,
        record.entrypoint,
    )
    assert state.published == []
    state.fail_publish = False
    with state.lock_run("run_test"):
        api.reconcile_tool_heads("run_test")
        api.reconcile_tool_heads("run_test")
    assert state.published == [record]
    assert state.iteration.model_dump() == hard_before
    assert api.complete(
        task, result=result(tool_views=[tool_output()])
    ) == AnnotationCompletion(False, False)


@pytest.mark.parametrize("outputs", [[], [tool_output("unknown")]])
def test_tool_identity_mismatch_is_retryable_without_publication(
    workflow, outputs
) -> None:
    api, state, clock = workflow
    tool(state, clock)
    task = claim(api)
    before = state.task.model_dump()
    with pytest.raises(AnnotationOutputError, match="Tool View") as raised:
        api.complete(task, result=result(tool_views=outputs))
    assert raised.value.usage == {"total_tokens": 17}
    assert state.task.model_dump() == before
    assert state.published == []


def test_terminal_failure_rejects_pending_tool_head(workflow) -> None:
    api, state, clock = workflow
    record = tool(state, clock)
    assert api.complete(
        claim(api), error=PermanentAnnotationError("invalid artifact")
    ) == AnnotationCompletion(True, False)
    assert state.task.state == "terminal_error"
    assert state.rejected == [record] and state.published == []


def test_failed_task_write_can_retry_without_reclaiming(workflow) -> None:
    api, state, _ = workflow
    task = claim(api)
    before = state.task.model_dump()
    state.fail_write = True
    with pytest.raises(OSError, match="task write"):
        api.complete(task, result=result())
    assert state.task.model_dump() == before
    assert not state.run_locked and not state.tasks_locked
    state.fail_write = False
    assert api.complete(task, result=result()) == AnnotationCompletion(True, True)


def test_close_fence_survives_task_cancellation_failure(workflow) -> None:
    api, state, _ = workflow
    task = claim(api)
    state.fail_write = True
    with pytest.raises(OSError, match="task write"), state.lock_run("run_test"):
        api.close_queue("run_test", reason="selected")
    assert state.queue.state == "closed" and state.queue.generation == 2
    assert api.complete(task, result=result()) == AnnotationCompletion(False, False)
    assert api.claim("run_test", "c001", 1) is None
    state.fail_write = False
    with state.lock_run("run_test"):
        assert api.close_queue("run_test", reason="selected").generation == 2


def test_file_adapter_preserves_layout_and_reopens_without_runtime(
    tmp_path: Path,
) -> None:
    clock = Clock()
    fixture = State(clock)
    store = FileAnnotationState(tmp_path)
    write_json(tmp_path / "runs/run_test/run.json", fixture.run.model_dump(mode="json"))
    api = AnnotationWorkflow(tasks=store, evidence=store, tools=store, clock=clock)
    assert api.queue("run_test").state == "open"
    assert not (tmp_path / "runs/run_test/evidence-annotator/queue.json").exists()
    with store.lock_run("run_test"), store.lock_tasks("run_test"):
        store.write_queue(fixture.queue)
        store.write_task(fixture.task)
    task = claim(api)
    assert task.attempt_history[0]["monitor_path"] == (
        "runs/run_test/evidence-annotator/attempts/c001-iteration-0001-attempt-01.json"
    )
    reopened = FileAnnotationState(tmp_path)
    assert reopened.read_task("run_test", "c001", 1) == task
    assert reopened.list_tasks("run_test") == [task]
    assert (
        tmp_path
        / "runs/run_test/candidates/c001/evidence-annotations/iteration-0001.json"
    ).is_file()
    with store.lock_run("run_test"):
        api.close_queue("run_test", reason="selected")
    assert reopened.read_task("run_test", "c001", 1).state == "cancelled"
    assert reopened.read_queue("run_test").generation == 2
