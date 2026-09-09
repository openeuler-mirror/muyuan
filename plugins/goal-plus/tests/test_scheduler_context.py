"""Scheduler input API checks without Runtime, live workspaces or model inference."""

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from goal_plus.application.scheduler_context import SchedulerContextBuilder
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.adaptive.models import ExpansionSource
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import CandidateTask
from goal_plus.domain.serialization import sha256_text
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.frozen_spec import load_frozen_spec
from goal_plus.infrastructure.state.scheduler import FileSchedulerState
from goal_plus.ports.scheduler_context import (
    MAX_SCHEDULER_DIFF_BYTES,
    SchedulerCandidateContext,
    SchedulerContext,
    SchedulerEvidenceContext,
    SchedulerExecutionContext,
)
from goal_plus.ports.search_context import SearchHostSettings
from goal_plus.ports.state import CandidateRecord
from tests.test_annotation_admission import frozen_spec, goal
from tests.test_annotation_context import Artifacts
from tests.test_annotation_workflow import Clock
from tests.test_scheduler_workflow import State, prepare_confirmation, result


class ContextState(State):
    def __init__(self, clock):
        super().__init__(clock)
        self.frozen = frozen_spec(clock, adaptive=True)
        self.task.task_context_sha256 = sha256_text(self.frozen.spec.objective)
        self.task.task_context_ref = "frozen_objective:run_test"
        self.iteration = IterationRecord(
            iteration=1,
            created_at=clock.timestamp(),
            settlement_id=self.task.settlement_id,
            git_head=self.task.attempt_commit,
            attempt_base_git_head=self.task.attempt_base_commit,
            attempt_changed_files=["main.py"],
            score=2.5,
            metrics={"score": 2.5},
            process_passed=True,
            artifact_hash="artifact_1",
            hypothesis="Change the measured implementation",
            disposition="keep",
            model_provenance={"private_note": "model_provenance_not_for_prompt"},
            log_paths=["private_raw_log"],
        )
        self.records = [
            SchedulerCandidateContext(
                candidate_id="c001",
                workspace=Path("/not-a-live-workspace"),
                iterations=[self.iteration],
                allocation_eligibility="eligible",
                allocation_depth=0,
                expansion_source=None,
            )
        ]
        self.goals = {}
        self.extra_tasks = []

    def read_frozen_spec(self, frozen_spec_id):
        assert frozen_spec_id == self.frozen.frozen_spec_id
        return self.frozen.model_copy(deep=True)

    def read_context_candidate(self, run_id, candidate_id):
        return next(
            r
            for r in self.read_context_candidates(run_id)
            if r.candidate_id == candidate_id
        )

    def read_context_candidates(self, run_id):
        assert run_id == self.run.run_id
        return deepcopy(self.records)

    def load_schedule_tasks(self, run_id, candidate_id=None):
        assert run_id == self.run.run_id
        return [
            task.model_copy(deep=True)
            for task in [
                *self.other_tasks.values(),
                *self.extra_tasks,
                self.task,
            ]
            if task is not None
            and (candidate_id is None or task.candidate_id == candidate_id)
        ]

    def load_schedule_task(self, run_id, candidate_id, iteration):
        return next(
            (
                task
                for task in self.load_schedule_tasks(run_id, candidate_id)
                if task.iteration == iteration
            ),
            None,
        )

    def read_goals(self):
        return deepcopy(self.goals)

    def read_host_settings(self):
        return SearchHostSettings()

    def snapshot(self):
        return (
            self.run.model_dump(),
            self.frozen.model_dump(),
            [asdict(record) for record in self.records],
            [task.model_dump() for task in self.load_schedule_tasks("run_test")],
        )


@pytest.fixture
def context_api():
    clock = Clock()
    state = ContextState(clock)
    artifacts = Artifacts()
    builder = SchedulerContextBuilder(
        tasks=state,
        source=state,
        context=SearchContext(state),
        artifacts=artifacts,
        root_dir=Path("/not-a-runtime-directory"),
    )
    return builder, state, artifacts, clock


def build(api, iteration=1):
    return api.build("run_test", "c001", iteration)


def confirm(state, clock):
    prepare_confirmation(state, clock)
    state.records[0].iterations.append(
        state.iteration.model_copy(
            deep=True,
            update={
                "iteration": 2,
                "settlement_id": state.task.settlement_id,
            },
        )
    )


def add_peer(state, clock):
    peer_iteration = state.iteration.model_copy(
        deep=True,
        update={
            "settlement_id": "peer_settlement",
            "git_head": "peer_head",
            "artifact_hash": "peer_hash",
            "hypothesis": "Peer public observation",
        },
    )
    state.records.append(
        replace(
            state.records[0],
            candidate_id="c009",
            iterations=[peer_iteration],
            workspace=Path("/peer-private-workspace"),
        )
    )
    state.extra_tasks.append(
        state.task.model_copy(
            deep=True,
            update={
                "task_id": "peer_schedule",
                "candidate_id": "c009",
                "iteration": 1,
                "node_id": "c009/iteration-0001",
                "settlement_id": "peer_settlement",
                "attempt_commit": "peer_head",
                "state": "completed",
                "output": result(clock).output,
                "reserved_candidate_ids": [],
            },
        )
    )


def test_exact_input_keeps_execution_and_private_fields_out_of_model_context(
    context_api,
):
    api, state, artifacts, _ = context_api
    before = state.snapshot()
    context = build(api)
    assert set(context) == SchedulerContext.__required_keys__
    assert (
        set(context["scheduler_context"]) == SchedulerEvidenceContext.__required_keys__
    )
    assert set(context["_execution"]) == SchedulerExecutionContext.__required_keys__
    assert (
        context["scheduler_context"]["current_attempt"]["actual_diff"] == "base -> head"
    )
    assert context["scheduler_context"]["public_result"] == {
        "metric_name": "score",
        "metric_direction": "maximize",
        "score": 2.5,
        "metrics": {"score": 2.5},
        "process_passed": True,
        "disposition": "keep",
    }
    comparison, paths, budget, function_context = artifacts.requests[0]
    assert (comparison.base.id, comparison.head.id) == ("base", "head")
    assert comparison.base.provider == comparison.head.provider == "git_worktree"
    assert (
        paths == ["main.py"] and budget == MAX_SCHEDULER_DIFF_BYTES and function_context
    )
    visible = repr(
        {key: value for key, value in context.items() if key != "_execution"}
    )
    for private in [
        "not-a-runtime-directory",
        "not-a-live-workspace",
        "test-model",
        "private_raw_log",
        "model_provenance_not_for_prompt",
    ]:
        assert private not in visible
    assert state.snapshot() == before


@pytest.mark.parametrize(
    "field",
    [
        "attempt_commit",
        "attempt_base_commit",
        "attempt_changed_files",
        "frozen_spec_id",
        "settlement_id",
        "node_id",
        "reward_policy_ref",
        "allocation_policy_ref",
        "task_context_sha256",
        "task_context_source",
        "task_context_ref",
        "profile",
        "missing_task",
        "missing_iteration",
        "missing_commit",
        "unavailable",
        "missing_configuration",
        "missing_policies",
    ],
)
def test_invalid_snapshot_is_rejected_without_writes(context_api, field):
    api, state, artifacts, _ = context_api
    if field == "missing_task":
        state.task = None
    elif field == "missing_iteration":
        state.records[0].iterations.clear()
    elif field == "missing_commit":
        state.iteration.git_head = None
    elif field == "unavailable":
        artifacts.available = False
    elif field == "missing_configuration":
        state.frozen.spec.strategy.search_scheduler = None
    elif field == "missing_policies":
        state.frozen.search_scheduler_policies = None
    else:
        setattr(
            state.task,
            field,
            None
            if field == "profile"
            else ["other.py"]
            if field == "attempt_changed_files"
            else "other",
        )
    before = state.snapshot()
    with pytest.raises(RuntimeError):
        build(api)
    assert state.snapshot() == before
    assert not artifacts.opened


@pytest.mark.parametrize("truncated", [False, True])
def test_diff_failure_is_fatal_and_does_not_inline_a_partial_diff(
    context_api, truncated
):
    api, state, artifacts, _ = context_api
    artifacts.truncated = truncated
    if not truncated:
        artifacts.fail_diff.add("head")
    before = state.snapshot()
    with pytest.raises(RuntimeError, match="diff"):
        build(api)
    assert state.snapshot() == before


def test_unchanged_attempt_needs_no_diff_read(context_api):
    api, state, artifacts, _ = context_api
    state.iteration.attempt_changed_files = state.task.attempt_changed_files = []
    assert build(api)["scheduler_context"]["current_attempt"]["actual_diff"] == ""
    assert artifacts.requests == []


def test_failed_annotation_keeps_missing_view_without_fabricating_evidence(context_api):
    api, state, _, _ = context_api
    state.task.annotation_state = "terminal_error"
    state.task.evidence_view = None
    evidence = build(api)["scheduler_context"]["qualitative_evidence"]
    assert evidence["state"] == "terminal_error" and evidence["view"] is None
    assert evidence["guidance"]


@pytest.mark.parametrize("phase", ["screen", "confirm"])
def test_reward_history_and_peer_visibility_follow_persisted_confirmation(
    context_api, phase
):
    api, state, _, clock = context_api
    if phase == "confirm":
        confirm(state, clock)
    add_peer(state, clock)
    before = state.snapshot()
    context = build(api, 2 if phase == "confirm" else 1)
    reward, allocation = context["reward_context"], context["allocation_context"]
    assert reward["uct"]["total_visits"] == (2 if phase == "confirm" else 1)
    assert reward["uct"]["node_visits"] == (1 if phase == "confirm" else 0)
    assert "peer_settlement" not in repr(reward)
    assert allocation["phase"] == phase and len(allocation["peer_frontiers"]) == 2
    if phase == "screen":
        assert (
            "candidates" not in allocation
            and "reserved_candidate_ids" not in allocation
        )
        assert "derivation_instructions" not in allocation["policy"]
    else:
        assert allocation["reserved_candidate_ids"] == ["c002"]
        current = allocation["candidates"][0]["nodes"][-1]
        assert current["parent_node_id"] == "c001/iteration-0001"
        assert current["reward_source"] == "current_scheduler_output"
        assert current["reward"] is None
    assert "peer-private-workspace" not in repr(context)
    assert state.snapshot() == before


@pytest.mark.parametrize(
    ("maximum", "reservation_state", "decision", "expected"),
    [
        (None, "pending", None, 1),
        (2, "pending", None, 0),
        (None, "completed", None, 2),
        (None, "completed", "decision_1", 1),
        (None, "terminal_error", None, 2),
        (None, "terminal_error", "decision_1", 1),
    ],
)
def test_capacity_counts_only_live_or_decision_bound_reservations(
    context_api, maximum, reservation_state, decision, expected
):
    api, state, _, clock = context_api
    add_peer(state, clock)
    peer_task = state.extra_tasks[0]
    peer_task.state = reservation_state
    peer_task.reserved_candidate_ids = ["c010"]
    peer_task.decision_id = decision
    state.frozen.spec.budget.max_candidates = maximum
    capacity = build(api)["allocation_context"]["capacity"]
    assert capacity["materialized_candidates"] == 2
    assert capacity["available_replacement_slots"] == expected


def test_confirmation_first_node_preserves_derived_source_ancestry(context_api):
    api, state, _, clock = context_api
    confirm(state, clock)
    state.records[0] = replace(
        state.records[0],
        allocation_depth=1,
        expansion_source=ExpansionSource(
            candidate_id="c009",
            iteration=3,
            node_id="c009/iteration-0003",
            settled_commit="source_commit",
            artifact_hash="source_hash",
        ),
    )
    nodes = build(api, 2)["allocation_context"]["candidates"][0]["nodes"]
    assert nodes[0]["parent_node_id"] == "c009/iteration-0003"
    assert nodes[1]["parent_node_id"] == "c001/iteration-0001"


def test_historical_goal_context_must_match_the_task_snapshot(context_api):
    api, state, _, clock = context_api
    state.goals["gp_one"] = goal(clock, revision=2, linked_revision=1)
    state.task.task_context_source = "goal_plus_raw_goal"
    state.task.task_context_ref = "goal_plus:gp_one:revision:1"
    state.task.task_context_sha256 = sha256_text("Goal revision 1")
    context = build(api)["scheduler_context"]
    assert context["task_context"] == "Goal revision 1"
    state.goals["gp_one"] = goal(clock, revision=2, linked_revision=2)
    before = state.snapshot()
    with pytest.raises(RuntimeError, match="snapshot"):
        build(api)
    assert state.snapshot() == before


def test_file_source_reopens_typed_input_without_exposing_private_candidate_fields(
    context_api, tmp_path
):
    _, state, artifacts, _ = context_api
    store = FileSchedulerState(tmp_path / ".gp")
    run_path = store.run_dir("run_test") / "run.json"
    frozen_path = store.root_dir / "specs/frozen_test/frozen_spec.json"
    candidate_path = store.run_dir("run_test") / "candidates/c001/candidate.json"
    write_json(run_path, state.run.model_dump(mode="json"))
    write_json(frozen_path, state.frozen.model_dump(mode="json"))
    record = CandidateRecord(
        candidate_id="c001",
        status="evaluated",
        iterations=[state.iteration],
        task=CandidateTask(
            run_id="run_test",
            candidate_id="c001",
            workspace=state.records[0].workspace,
            workspace_backend="git_worktree",
            hypothesis="local contract",
            allowed_files=["main.py"],
            denied_files=["verify.py"],
            process_environment={"PRIVATE_HOST_VALUE": "not_for_context"},
        ),
    )
    write_json(candidate_path, record.model_dump(mode="json"))
    store.write_schedule_task(state.task)
    assert store.read_context_candidate("run_test", "c001") == state.records[0]
    assert store.read_context_candidates("run_test") == state.records
    assert "process_environment" not in asdict(
        store.read_context_candidate("run_test", "c001")
    )
    before = [path.read_bytes() for path in (run_path, frozen_path, candidate_path)]
    api = SchedulerContextBuilder(
        tasks=store,
        source=store,
        context=SearchContext(state),
        artifacts=artifacts,
        root_dir=store.root_dir,
    )
    assert (
        build(api)["scheduler_context"]["identity"]["settlement_id"]
        == state.task.settlement_id
    )
    assert [
        path.read_bytes() for path in (run_path, frozen_path, candidate_path)
    ] == before


@pytest.mark.parametrize("legacy", [False, True])
def test_shared_frozen_reader_preserves_existing_backend_contract_without_rewriting(
    tmp_path, legacy
):
    frozen = frozen_spec(Clock())
    payload = frozen.model_dump(mode="json")
    if legacy:
        payload["spec"].pop("workspace")
    path = tmp_path / "frozen_spec.json"
    write_json(path, payload)
    before = path.read_bytes()
    loaded = load_frozen_spec(path)
    assert loaded.spec.workspace.backend == ("copy" if legacy else "git_worktree")
    assert (
        loaded.frozen_spec_id == frozen.frozen_spec_id
        and loaded.spec_hash == frozen.spec_hash
    )
    assert path.read_bytes() == before
