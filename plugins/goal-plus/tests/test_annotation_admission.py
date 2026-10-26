"""Admission and Goal context APIs with typed inputs and no Runtime or model host."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.annotation import EvidenceAnnotationTask
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.goal import GoalPlusRecord
from goal_plus.domain.search import FrozenSpec, SearchSpec
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.search_context import FileSearchContextSource
from goal_plus.ports.annotation import AnnotationCandidateEvidence
from goal_plus.ports.search_context import SearchHostSettings
from tests.test_annotation_workflow import Clock, State


def frozen_spec(clock: Clock, *, adaptive: bool = False) -> FrozenSpec:
    data = {
        "objective": "Improve the measured result",
        "metric_name": "score",
        "metric_direction": "maximize",
        "source_path": "project",
        "edit_surface": {"allow": ["main.py"], "deny": ["verify.py"]},
        "budget": {"max_parallel": 2},
        "process_verifiers": [
            {
                "name": "score",
                "role": "ranking_signal",
                "command": ["python", "verify.py"],
            }
        ],
        "strategy": {"name": "random"},
    }
    if adaptive:
        data["strategy"].update(
            {
                "initial_context": "fresh",
                "selection": {
                    "ranking_keys": ["quality_score", "hard_score"],
                    "ranking_tolerance": [0.05, 0.0],
                },
                "verifier": {"scores": ["progress_reward", "allocation_utility"]},
                "adaptive_search": {"allocation": {"policy": "swe_search_uct/v1"}},
            }
        )
    spec = SearchSpec.model_validate(data)
    return FrozenSpec(runtime_provider="direct", agent_harness="codex",
        frozen_spec_id="frozen_test",
        spec_hash="hash",
        spec=spec,
        verifier_hashes={},
        frozen_verifier_paths={},
        created_at=clock.timestamp(),
    )


class AdmissionState(State):
    def __init__(self, clock: Clock, *, adaptive: bool = False) -> None:
        super().__init__(clock)
        self.queue.revision = self.queue.accepted_tasks = 0
        self.frozen = frozen_spec(clock, adaptive=adaptive)
        self.iteration.git_head = "attempt"
        self.iteration.attempt_base_git_head = "base"
        self.iteration.artifact_ref = ArtifactRef(
            kind="git_commit", provider="git_worktree", id="attempt"
        )
        self.iteration.attempt_base_artifact_ref = ArtifactRef(
            kind="git_commit", provider="git_worktree", id="base"
        )
        self.iteration.artifact_clean = True
        self.records = [
            AnnotationCandidateEvidence("c001", "git_worktree", [self.iteration])
        ]
        self.task_records: dict[tuple[str, int], EvidenceAnnotationTask] = {}
        self.goals: dict[str, GoalPlusRecord] = {}
        self.settings = SearchHostSettings()
        self.fail_queue = False

    def read_task(self, run_id: str, candidate_id: str, iteration: int):
        assert run_id == self.run.run_id
        task = self.task_records.get((candidate_id, iteration))
        return task.model_copy(deep=True) if task is not None else None

    def write_task(self, task: EvidenceAnnotationTask) -> None:
        super().write_task(task)
        self.task_records[(task.candidate_id, task.iteration)] = task.model_copy(
            deep=True
        )

    def write_queue(self, queue) -> None:
        if self.fail_queue:
            raise OSError("queue write failed")
        super().write_queue(queue)

    def list_tasks(self, run_id: str) -> list[EvidenceAnnotationTask]:
        assert run_id == self.run.run_id
        return [task.model_copy(deep=True) for task in self.task_records.values()]

    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec:
        assert frozen_spec_id == self.frozen.frozen_spec_id
        return self.frozen.model_copy(deep=True)

    def read_candidate_evidence(self, run_id: str) -> list[AnnotationCandidateEvidence]:
        assert run_id == self.run.run_id
        return [
            replace(
                record, iterations=[i.model_copy(deep=True) for i in record.iterations]
            )
            for record in self.records
        ]

    def read_iterations(self, run_id: str):
        return {
            record.candidate_id: record.iterations
            for record in self.read_candidate_evidence(run_id)
        }

    def read_goals(self):
        return {key: goal.model_copy(deep=True) for key, goal in self.goals.items()}

    def read_host_settings(self) -> SearchHostSettings:
        return self.settings


@pytest.fixture
def admission():
    clock = Clock()
    state = AdmissionState(clock)
    workflow = AnnotationWorkflow(tasks=state, evidence=state, tools=state, clock=clock)
    context = SearchContext(state)
    return (
        AnnotationAdmission(workflow=workflow, source=state, context=context),
        state,
        clock,
    )


def create(api: AnnotationAdmission):
    with api.tasks.lock_run("run_test"):
        return api.create("run_test", "c001", 1)


def test_claim_next_uses_durable_task_and_closed_queue_fence(admission):
    api, state, _ = admission
    admitted = create(api)
    queue, claimed = api.claim_next("run_test")
    assert claimed.task_id == admitted.task_id
    assert claimed.attempts == 1
    assert queue.revision >= claimed.queue_revision
    state.queue.state = "closed"
    state.queue.generation += 1
    before = state.task_records.copy()
    closed, missing = api.claim_next("run_test")
    assert closed.state == "closed" and missing is None
    assert state.task_records == before


@pytest.mark.parametrize("baseline", [None, ArtifactRef(
    kind="git_commit", provider="git_worktree", id="git-base",
)])
def test_thinkthread_dispatch_rejects_missing_or_foreign_s0_before_admission(admission, baseline):
    api, state, _ = admission
    state.run.source_artifact_ref = baseline
    before = state.queue.model_copy(deep=True)
    with pytest.raises(RuntimeError, match="runtime-owned S0"):
        api.prepare_thinkthread_dispatch("run_test")
    assert state.queue == before
    assert state.task_records == {}


@pytest.mark.parametrize("failed_write", ["task", "queue"])
def test_admission_recovers_committed_task_without_duplicate_identity(
    admission, failed_write: str
) -> None:
    api, state, _ = admission
    hard_before = (state.run.model_dump(), state.iteration.model_dump())
    state.fail_write = failed_write == "task"
    state.fail_queue = failed_write == "queue"
    with pytest.raises(OSError, match="write failed"):
        create(api)
    assert bool(state.task_records) is (failed_write == "queue")
    assert state.queue.revision == 0
    assert api.workflow.claim("run_test", "c001", 1) is None
    state.fail_write = state.fail_queue = False
    repaired = api.reconcile("run_test")
    assert repaired.state == "open" and repaired.errors == []
    assert len(repaired.created) == (failed_write == "task")
    task = api.workflow.task("run_test", "c001", 1)
    assert task.task_id == "annotation_settled"
    assert task.queue_revision == repaired.queue.revision == 1
    assert repaired.queue.accepted_tasks == 1
    assert api.reconcile("run_test").created == []
    assert create(api) == task
    assert (state.run.model_dump(), state.iteration.model_dump()) == hard_before


@pytest.mark.parametrize(
    "change",
    ["missing", "no_worker", "artifact", "settlement", "closed", "invalidated"],
)
def test_invalid_admission_does_not_write_task_or_queue(admission, change: str) -> None:
    api, state, clock = admission
    if change == "missing":
        state.records = []
    elif change == "no_worker":
        state.iteration.agent_session_id = None
    elif change == "artifact":
        state.iteration.artifact_ref = state.iteration.git_head = None
    elif change == "settlement":
        state.iteration.settlement_id = None
    elif change == "closed":
        state.queue.state = "closed"
    else:
        state.run.invalidated_at = clock.timestamp()
    queue_before = state.queue.model_dump()
    with pytest.raises(RuntimeError):
        create(api)
    assert state.task_records == {}
    assert state.queue.model_dump() == queue_before


def test_existing_admission_rejects_changed_settlement(admission) -> None:
    api, state, _ = admission
    original = create(api)
    state.iteration.settlement_id = "different-settlement"
    with pytest.raises(RuntimeError, match="immutable"):
        create(api)
    assert api.workflow.task("run_test", "c001", 1) == original


@pytest.mark.parametrize("deadline", ["expired", "invalid"])
def test_deadline_failure_creates_terminal_task_without_claim(
    admission, deadline: str
) -> None:
    api, state, clock = admission
    state.settings = SearchHostSettings(
        outer_deadline_at=clock.timestamp() if deadline == "expired" else "invalid"
    )
    task = create(api)
    assert task.state == "terminal_error" and task.profile is None
    assert state.queue.accepted_tasks == 1
    assert api.eligible("run_test") == []


def test_profile_uses_only_current_named_host_settings(admission) -> None:
    api, state, _ = admission
    state.settings = SearchHostSettings(annotation_reasoning_effort="high")
    task = create(api)
    assert task.profile.reasoning_effort == "high"
    state.settings = SearchHostSettings(annotation_reasoning_effort="low")
    assert create(api).profile.reasoning_effort == "high"


def test_reconcile_does_not_reopen_closed_generation(admission) -> None:
    api, state, _ = admission
    with state.lock_run("run_test"):
        api.workflow.close_queue("run_test", reason="selected")
    closed = api.workflow.queue("run_test").model_copy(deep=True)
    result = api.reconcile("run_test")
    assert result.state == "closed" and result.created == result.errors == []
    assert result.queue.generation == 2 and state.task_records == {}
    assert api.workflow.queue("run_test") == closed


def test_adaptive_parent_admission_remains_distinct_from_worker_rescan(
    admission,
) -> None:
    api, state, clock = admission
    state.frozen = frozen_spec(clock, adaptive=True)
    state.iteration.agent_session_id = None
    assert api.reconcile("run_test").created == []
    api.ensure("run_test")
    assert state.queue.accepted_tasks == 1
    assert api.workflow.task("run_test", "c001", 1).state == "pending"
    assert api.eligible("run_test") == [("c001", 1)]
    _, task = api.claim_next("run_test")
    assert task is not None and task.state == "retry_wait" and task.attempts == 1


def test_comparison_basis_uses_best_eligible_peer_and_freezes_artifacts(
    admission,
) -> None:
    api, state, clock = admission
    state.frozen.supplemental_evaluation_enabled = True
    peers = []
    for number in range(1, 11):
        candidate_id = f"p{number:03d}"
        good = state.iteration.model_copy(
            deep=True,
            update={
                "iteration": 2,
                "score": float(number),
                "created_at": clock.timestamp_from_epoch(clock.epoch + number),
                "git_head": f"peer-{number}",
                "artifact_ref": ArtifactRef(
                    kind="git_commit", provider="git_worktree", id=f"peer-{number}"
                ),
            },
        )
        failed = good.model_copy(
            update={"iteration": 3, "score": 100.0, "changed_outside_allowed": True}
        )
        earlier = good.model_copy(update={"iteration": 1})
        peers.append(
            AnnotationCandidateEvidence(
                candidate_id, "git_worktree", [earlier, good, failed]
            )
        )
    state.records.extend(peers)
    basis = api.comparison_basis("run_test", target_candidate_id="c001")
    assert [reference.candidate_id for reference in basis] == [
        f"p{i:03d}" for i in range(3, 11)
    ]
    assert all(reference.iteration == 2 for reference in basis)
    task = create(api)
    assert task.comparison_basis == basis
    assert len(task.artifact_views) == 9 and task.artifact_views[0].role == "target"
    for view, reference in zip(task.artifact_views[1:], basis, strict=True):
        assert (view.candidate_id, view.iteration, view.artifact_ref) == (
            reference.candidate_id,
            reference.iteration,
            reference.artifact_ref,
        )
    state.records = state.records[:1]
    assert create(api) == task


def goal(
    clock: Clock,
    goal_id: str = "gp_one",
    *,
    revision: int = 1,
    linked_revision: int = 1,
) -> GoalPlusRecord:
    return GoalPlusRecord.model_validate(
        {
            "goal_plus_id": goal_id,
            "raw_goal": f"Goal revision {revision}",
            "goal_revision": revision,
            "created_at": clock.timestamp(),
            "updated_at": clock.timestamp(),
            "goal_revisions": [
                {
                    "revision": i,
                    "raw_goal": f"Goal revision {i}",
                    "created_at": clock.timestamp(),
                }
                for i in range(1, revision + 1)
            ],
            "search_tasks": [{"run_id": "run_test", "goal_revision": linked_revision}],
        }
    )


def test_goal_context_keeps_linked_revision_and_ambiguous_fallback(admission) -> None:
    api, state, clock = admission
    state.goals["gp_one"] = goal(clock, revision=2)
    context = api.context.task_context("run_test", fallback="Frozen objective")
    assert (context.text, context.source, context.reference) == (
        "Goal revision 1",
        "goal_plus_raw_goal",
        "goal_plus:gp_one:revision:1",
    )
    task = create(api)
    assert task.task_context_ref == context.reference
    assert task.task_context_sha256 is not None
    assert "task_context" not in task.model_dump()
    state.goals["gp_two"] = goal(clock, "gp_two")
    assert (
        api.context.task_context("run_test", fallback="Frozen objective").text
        == "Frozen objective"
    )


def test_deadline_resolver_ignores_other_runs_and_terminal_goals(admission) -> None:
    api, state, clock = admission
    active = goal(clock)
    active.outer_deadline_at = str(clock.epoch + 60)
    other = goal(clock, "gp_other")
    other.search_tasks[0].run_id = "other_run"
    other.linked_search.run_id = "other_run"
    other.outer_deadline_at = str(clock.epoch)
    terminal = goal(clock, "gp_terminal")
    terminal.status = "complete"
    terminal.outer_deadline_at = "invalid"
    state.goals = {item.goal_plus_id: item for item in [active, other, terminal]}
    state.settings = SearchHostSettings(outer_deadline_at=str(clock.epoch + 120))
    assert api.context.outer_deadline("run_test") == active.outer_deadline_at
    state.settings = SearchHostSettings(outer_deadline_at="invalid-host-deadline")
    assert api.context.outer_deadline("run_test") == "invalid-host-deadline"


def test_file_goal_context_reads_validated_records_and_only_named_environment(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    record = goal(clock)
    write_json(tmp_path / "goal-plus/gp_one/goal.json", record.model_dump(mode="json"))
    write_json(tmp_path / "goal-plus/gp_invalid/goal.json", {"untrusted": "not a Goal"})
    monkeypatch.setenv("GOAL_PLUS_OUTER_DEADLINE_AT", "123")
    monkeypatch.setenv("GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT", "high")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-enter-settings")
    source = FileSearchContextSource(tmp_path)
    assert source.read_goals() == {"gp_one": record}
    assert source.read_host_settings() == SearchHostSettings("123", "high")
