from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

import pytest

import goal_plus.adaptive_search.scheduler as scheduler_module
from goal_plus.adaptive_search.allocation import (
    AllocationProposal,
    AllocationReplacementProposal,
)
from goal_plus.adaptive_search.scheduler import (
    ResolvedSchedulerProfile,
    SCHEDULER_INSTRUCTIONS,
    SchedulerExecutionRequest,
    SchedulerExecutionResult,
    SchedulerInvocationRecord,
    SchedulerManager,
    SchedulerOutput,
    SchedulerResult,
    SchedulerReward,
    SearchScheduler,
    TransientSchedulerError,
    drain_schedule_tasks,
    resolve_candidate_schedules,
)
from goal_plus.models import EvidenceViewRecord, ScoreReport, SearchSpec
from goal_plus.runtime import FileSearchRuntime, sha256_text, utc_timestamp

from tests._runtime_helpers import make_project, spec_for


@pytest.mark.parametrize(
    ("host", "executor_name"),
    (("codex", "CodexSchedulerExecutor"), ("pi-rpc", "PiSchedulerExecutor")),
)
def test_scheduler_routes_to_host_without_a_session(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    executor_name: str,
) -> None:
    calls: list[SchedulerExecutionRequest] = []
    expected = SchedulerExecutionResult(
        payload={
            "reward": {},
            "allocation_flag": False,
            "allocation_rationale": "continue",
            "allocation_confidence": 1.0,
        },
        usage={},
        invocation=SchedulerInvocationRecord(
            invocation_id="invocation-test",
            host=host,  # type: ignore[arg-type]
            model="test-model",
            reasoning_effort="low",
            started_at="2026-08-31T00:00:00Z",
            completed_at="2026-08-31T00:00:01Z",
            elapsed_seconds=1.0,
        ),
    )

    class StubSchedulerExecutor:
        def __init__(self, *, popen: object) -> None:
            self.popen = popen

        def execute(
            self,
            request: SchedulerExecutionRequest,
        ) -> SchedulerExecutionResult:
            calls.append(request)
            return expected

    monkeypatch.setattr(
        scheduler_module,
        executor_name,
        StubSchedulerExecutor,
    )
    request = SchedulerExecutionRequest(
        profile=ResolvedSchedulerProfile(
            host=host,  # type: ignore[arg-type]
            model="test-model",
            reasoning_effort="low",
            timeout_seconds=30,
        ),
        instructions="Compute scheduler output.",
        prompt="Use this settled task.",
        output_schema={"type": "object"},
        identity={"run_id": "run_test"},
    )

    assert SearchScheduler(popen=object())._execute(request) is expected
    assert calls == [request]


def search_scheduler_spec(
    project: Path,
    *,
    max_candidates: int | None = None,
    max_parallel: int = 2,
) -> SearchSpec:
    data = spec_for(project, max_parallel=max_parallel).model_dump(mode="json")
    data["budget"] = {
        "max_parallel": max_parallel,
        "max_candidates": max_candidates,
    }
    data["workspace"] = {"backend": "git_worktree"}
    data["strategy"].update(
        {
            "name": "random",
            "orchestration_mode": "adaptive_search",
            "search_scheduler": {
                "host": "codex",
                "model": "gpt-5.2",
                "reasoning_effort": "low",
                "timeout_seconds": 90,
                "reward": (
                    "evidence_llm_value/v3 @prompt "
                    "Compute reward from rubric plus exploration."
                ),
                "allocation": "value_guided_replace/v2",
            },
        }
    )
    return SearchSpec.model_validate(data)


class _StaticScheduler:
    def __init__(self, decisions: list[str]) -> None:
        self.decisions = iter(decisions)
        self.contexts: list[dict[str, object]] = []

    def schedule(self, context: dict[str, object]) -> SchedulerResult:
        self.contexts.append(context)
        decision = next(self.decisions)
        allocation_context = context.get("allocation_context")
        scheduler_context = context.get("scheduler_context")
        assert isinstance(scheduler_context, dict)
        identity = scheduler_context["identity"]
        assert isinstance(identity, dict)
        plan = None
        if (
            decision == "reschedule"
            and isinstance(allocation_context, dict)
            and allocation_context["phase"] == "confirm"
        ):
            reserved = list(allocation_context["reserved_candidate_ids"])
            assert reserved
            plan = AllocationProposal(
                replacements=[
                    AllocationReplacementProposal(
                        retire_candidate_id=str(identity["candidate_id"]),
                        new_candidate_id=reserved[0],
                        source_candidate_id=str(identity["candidate_id"]),
                        source_iteration=1,
                        source_node_id=(
                            f"{identity['candidate_id']}/iteration-0001"
                        ),
                        rationale="The lane has confirmed low process value.",
                        exploration_direction=(
                            "Rebuild from the first settled node using a distinct mechanism."
                        ),
                        worker_budget={"max_runtime_seconds": 120},
                    )
                ],
                rationale="Two scheduler observations confirm rescheduling.",
            )
        output = SchedulerOutput(
            reward=SchedulerReward(
                reward=-0.5,
                rubric_score=-20,
                exploitation=-0.4,
                exploration=0.1,
                uct=-0.3,
                components={"public_progress": -0.5},
                rationale="Public evidence shows no credible net progress.",
            ),
            allocation_flag=decision == "reschedule",
            allocation_rationale="Historical and current rewards are weak.",
            allocation_confidence=0.9,
            decision=plan,
        )
        return SchedulerResult(
            output=output,
            usage={"input_tokens": 100, "output_tokens": 30},
            invocation=SchedulerInvocationRecord(
                invocation_id=f"invocation-{identity['iteration']}",
                host="codex",
                model="gpt-5.2",
                reasoning_effort="low",
                started_at="2026-08-31T00:00:00Z",
                completed_at="2026-08-31T00:00:01Z",
                elapsed_seconds=1.0,
            ),
        )

class _FlakyScheduler:
    def __init__(self) -> None:
        self.calls = 0
        self.delegate = _StaticScheduler(["continue"])

    def schedule(self, context: dict[str, object]) -> SchedulerResult:
        self.calls += 1
        if self.calls == 1:
            raise TransientSchedulerError("temporary provider failure")
        return self.delegate.schedule(context)

class _ConcurrentScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def schedule(self, context: dict[str, object]) -> SchedulerResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.1)
            return _StaticScheduler(["continue"]).schedule(context)
        finally:
            with self._lock:
                self.active -= 1

def _start_runtime(
    tmp_path: Path,
    *,
    max_candidates: int | None = None,
    max_parallel: int = 2,
) -> tuple[FileSearchRuntime, str, list[object]]:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        search_scheduler_spec(
            project,
            max_candidates=max_candidates,
            max_parallel=max_parallel,
        ),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=max_parallel)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    return runtime, run_id, tasks


def test_candidate_reservations_honor_parallel_and_optional_total_limits(
    tmp_path: Path,
) -> None:
    unbounded_root = tmp_path / "unbounded"
    bounded_root = tmp_path / "bounded"
    unbounded_root.mkdir()
    bounded_root.mkdir()

    unbounded, unbounded_run_id, _ = _start_runtime(
        unbounded_root,
        max_candidates=None,
    )
    with unbounded._run_transaction(unbounded_run_id):
        run = unbounded._load_run(unbounded_run_id)
        frozen = unbounded._load_frozen_spec(run.frozen_spec_id)
        unbounded_ids = SchedulerManager(unbounded)._reserve_candidate_ids(
            run,
            frozen,
        )

    bounded, bounded_run_id, _ = _start_runtime(
        bounded_root,
        max_candidates=3,
    )
    with bounded._run_transaction(bounded_run_id):
        run = bounded._load_run(bounded_run_id)
        frozen = bounded._load_frozen_spec(run.frozen_spec_id)
        bounded_ids = SchedulerManager(bounded)._reserve_candidate_ids(
            run,
            frozen,
        )

    assert len(unbounded_ids) == 2
    assert len(bounded_ids) == 1


def _settle_annotation(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate_id: str,
    iteration: int,
    *,
    terminal_error: bool = False,
) -> None:
    with runtime._run_transaction(run_id):
        task = runtime._load_evidence_annotation_task(
            run_id,
            candidate_id,
            iteration,
        )
        assert task is not None
        if terminal_error:
            error = "AnnotationTask test failure"
            task = task.model_copy(
                update={
                    "state": "terminal_error",
                    "last_error": error,
                    "error_fingerprint": sha256_text(error),
                    "updated_at": utc_timestamp(),
                }
            )
        else:
            task = task.model_copy(
                update={
                    "state": "completed",
                    "view": EvidenceViewRecord(
                        run_id=run_id,
                        candidate_id=candidate_id,
                        iteration=iteration,
                        attempt_commit=task.attempt_commit,
                        description="The settled diff has focused public evidence.",
                        created_at=utc_timestamp(),
                    ),
                    "updated_at": utc_timestamp(),
                }
            )
        runtime._write_evidence_annotation_task(task)
    SchedulerManager(runtime).ensure_tasks(run_id)


def _prepare_late_confirmation(
    tmp_path: Path,
) -> tuple[FileSearchRuntime, str, Any, ScoreReport]:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    candidate = tasks[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)

    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="first late scheduler observation",
    )
    _settle_annotation(runtime, run_id, candidate.candidate_id, 1)

    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 0\n",
        encoding="utf-8",
    )
    second_report = runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="confirmation settled before the previous flag",
    )
    _settle_annotation(runtime, run_id, candidate.candidate_id, 2)

    unresolved = runtime._adaptive_search.resolve_scheduler_barrier(
        run_id,
        candidate.candidate_id,
        second_report,
    )
    assert unresolved.allocation_decision is None
    assert drain_schedule_tasks(
        runtime.root_dir,
        run_id,
        scheduler=_StaticScheduler(["reschedule", "reschedule"]),
    ) == 2
    return runtime, run_id, candidate, second_report


def test_barrier_promotes_a_late_completed_confirmation(tmp_path: Path) -> None:
    runtime, run_id, candidate, report = _prepare_late_confirmation(tmp_path)

    resolved = runtime._adaptive_search.resolve_scheduler_barrier(
        run_id,
        candidate.candidate_id,
        report,
    )

    decision = resolved.allocation_decision
    assert decision is not None
    task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        2,
    )
    assert task is not None
    assert task.decision_id == decision.decision_id
    assert runtime._load_candidate_record(
        run_id,
        candidate.candidate_id,
    ).allocation_eligibility == "retired"


def test_barrier_logs_scheduler_error_and_skips_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, candidate, report = _prepare_late_confirmation(tmp_path)

    def fail_annotation_drain(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated scheduler barrier failure")

    monkeypatch.setattr(
        runtime,
        "_drain_evidence_annotations",
        fail_annotation_drain,
    )
    resolved = SchedulerManager(runtime).resolve_barrier(
        run_id,
        candidate.candidate_id,
        report,
    )

    assert resolved.allocation_decision is None
    scheduler_log = (
        runtime.root_dir / "runs" / run_id / "scheduler" / "scheduler.log"
    ).read_text(encoding="utf-8")
    assert "barrier failed; Decision skipped" in scheduler_log
    assert "simulated scheduler barrier failure" in scheduler_log


def test_allocation_apply_rechecks_live_candidate_limit(tmp_path: Path) -> None:
    runtime, run_id, candidate, report = _prepare_late_confirmation(tmp_path)
    resolved = runtime._adaptive_search.resolve_scheduler_barrier(
        run_id,
        candidate.candidate_id,
        report,
    )
    decision = resolved.allocation_decision
    assert decision is not None

    retired = runtime._load_candidate_record(run_id, candidate.candidate_id)
    runtime._write_candidate_record(
        run_id,
        retired.model_copy(
            update={
                "allocation_eligibility": "eligible",
                "retired_by_decision_id": None,
            }
        ),
    )

    with pytest.raises(RuntimeError, match="exceed budget.max_parallel"):
        runtime.apply_allocation_decision(run_id, decision.decision_id)


def test_existing_decision_reconciles_after_projection_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, candidate, report = _prepare_late_confirmation(tmp_path)
    original_write = runtime._write_candidate_record
    failed = False

    def fail_once(run_id: str, record: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated projection write failure")
        original_write(run_id, record)

    monkeypatch.setattr(runtime, "_write_candidate_record", fail_once)
    assert runtime._adaptive_search._record_pending_decisions(run_id) == []
    decisions = runtime.list_allocation_decisions(run_id)
    assert len(decisions) == 1
    assert runtime._load_candidate_record(
        run_id,
        candidate.candidate_id,
    ).allocation_eligibility == "eligible"

    monkeypatch.setattr(runtime, "_write_candidate_record", original_write)
    resolved = runtime._adaptive_search.resolve_scheduler_barrier(
        run_id,
        candidate.candidate_id,
        report,
    )

    assert resolved.allocation_decision == decisions[0]
    assert len(runtime.list_allocation_decisions(run_id)) == 1
    assert runtime._load_run(run_id).next_allocation_decision_index == 2


def test_annotation_terminal_hook_uses_frozen_scheduler_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        runtime._adaptive_search,
        "kick_schedule_tasks",
        calls.append,
    )

    assert runtime.search_scheduler_enabled(run_id) is True
    [plan] = runtime._load_plans(run_id)
    assert all(
        item.metadata["strategy"] == "adaptive_search"
        for item in plan.work_orders
    )
    runtime.on_evidence_annotation_terminal(run_id)

    assert calls == [run_id]


def test_scheduler_call_indexes_rewards_and_keeps_lazy_decision(tmp_path: Path) -> None:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    candidate = tasks[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    scheduler = _StaticScheduler(["reschedule", "reschedule"])

    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    first_report = runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="first weak direction",
    )
    first_task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        1,
    )
    assert first_report.scheduler_status == "pending"
    assert first_task is None
    _settle_annotation(runtime, run_id, candidate.candidate_id, 1)
    first_task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        1,
    )
    assert first_task is not None
    assert first_task.profile is not None
    assert first_task.profile.host == "codex"
    assert first_task.profile.model == "gpt-5.2"
    assert first_task.profile.reasoning_effort == "low"
    assert first_task.profile.timeout_seconds == 90
    assert first_task.evidence_view == "The settled diff has focused public evidence."
    assert first_task.annotation_state == "completed"
    assert "await_result" not in first_task.model_dump(mode="json")
    assert first_task.reserved_candidate_ids == []
    resolve_candidate_schedules(
        runtime.root_dir,
        run_id,
        candidate.candidate_id,
        through_iteration=1,
        scheduler=scheduler,
    )

    first_context = scheduler.contexts[0]
    assert set(first_context) == {
        "scheduler_context",
        "reward_context",
        "allocation_context",
        "_execution",
    }
    first_allocation = first_context["allocation_context"]
    first_reward = first_context["reward_context"]
    assert isinstance(first_allocation, dict)
    assert isinstance(first_reward, dict)
    assert first_allocation["phase"] == "screen"
    assert first_reward["candidate_history"]["recent_nodes"] == []
    assert "candidates" not in first_allocation
    assert "reserved_candidate_ids" not in first_allocation
    assert "derivation_instructions" not in first_allocation["policy"]
    scheduler_prompt = scheduler_module._scheduler_prompt(first_context)
    assert "scheduler_context" in scheduler_prompt
    assert "reward_context" in scheduler_prompt
    assert "allocation_context" in scheduler_prompt
    assert "runtime_root" not in scheduler_prompt
    assert "outer_deadline_at" not in scheduler_prompt
    rewards = runtime._adaptive_search.list_scheduler_rewards(
        run_id,
        candidate.candidate_id,
    )
    assert len(rewards) == 1
    assert rewards[0].reward.reward == pytest.approx(-0.5)
    assert rewards[0].allocation_flag is True
    assert rewards[0].schedule_task_id == first_task.task_id
    assert rewards[0].settlement_id == first_task.settlement_id
    assert rewards[0].node_id == first_task.node_id
    assert runtime.list_allocation_decisions(run_id) == []
    SchedulerManager(runtime).consume_tasks(
        run_id,
        candidate.candidate_id,
        through_iteration=1,
    )
    assert runtime._adaptive_search.list_scheduler_rewards(
        run_id,
        candidate.candidate_id,
    ) == rewards
    assert not runtime.root_dir.joinpath(
        "runs",
        run_id,
        "candidates",
        candidate.candidate_id,
        "value-tree.json",
    ).exists()

    peer = tasks[1]
    peer_session = runtime.start_agent_session(run_id, peer.candidate_id)
    peer.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 3\n",
        encoding="utf-8",
    )
    runtime._run_verifier(
        run_id,
        peer.candidate_id,
        agent_session_id=peer_session.agent_session_id,
        hypothesis="independent peer reward",
    )
    _settle_annotation(runtime, run_id, peer.candidate_id, 1)
    resolve_candidate_schedules(
        runtime.root_dir,
        run_id,
        peer.candidate_id,
        through_iteration=1,
        scheduler=_StaticScheduler(["continue"]),
    )

    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 0\n",
        encoding="utf-8",
    )
    second_report = runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="second weak direction confirms reschedule",
    )
    assert second_report.scheduler_status == "pending"
    assert SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        2,
    ) is None
    _settle_annotation(runtime, run_id, candidate.candidate_id, 2)
    second_task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        2,
    )
    assert second_task is not None
    assert len(second_task.reserved_candidate_ids) == 2
    resolve_candidate_schedules(
        runtime.root_dir,
        run_id,
        candidate.candidate_id,
        through_iteration=2,
        scheduler=scheduler,
    )
    resolved_report = runtime._adaptive_search.resolve_scheduler_barrier(
        run_id,
        candidate.candidate_id,
        second_report,
    )
    decision = resolved_report.allocation_decision
    second_context = scheduler.contexts[1]
    second_allocation = second_context["allocation_context"]
    second_reward = second_context["reward_context"]
    assert isinstance(second_allocation, dict)
    assert isinstance(second_reward, dict)
    assert second_allocation["phase"] == "confirm"
    assert "derivation_instructions" in second_allocation["policy"]
    assert len(second_reward["candidate_history"]["recent_nodes"]) == 1
    previous_node = second_reward["candidate_history"]["previous_node"]
    assert previous_node["settlement_id"] == first_task.settlement_id
    assert previous_node["schedule_task_id"] == first_task.task_id
    allocation_candidates = second_allocation["candidates"]
    assert allocation_candidates
    assert all("expansion_source" in item for item in allocation_candidates)
    bound_nodes = [
        node
        for item in allocation_candidates
        for node in item["nodes"]
    ]
    assert all(node["settlement_id"] for node in bound_nodes)
    assert all(node["reward_task_id"] for node in bound_nodes)
    assert len(second_allocation["peer_frontiers"]) == 2
    assert "reward_history" not in second_allocation
    assert decision is not None
    assert decision.schedule_task_id == second_task.task_id
    assert len(decision.replacements) == 1
    assert decision.replacements[0].exploration_direction.startswith("Rebuild")
    assert decision.replacements[0].worker_budget is not None
    assert decision.replacements[0].worker_budget.max_runtime_seconds == 120
    persisted_task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        2,
    )
    assert persisted_task is not None
    assert persisted_task.reserved_candidate_ids == [
        decision.replacements[0].new_candidate_id
    ]

    record = runtime._load_candidate_record(run_id, candidate.candidate_id)
    assert record.allocation_eligibility == "retired"
    assert record.retired_by_decision_id == decision.decision_id
    with pytest.raises(RuntimeError, match="retired"):
        runtime.continue_agent_session(session.agent_session_id)
    with pytest.raises(RuntimeError, match="retired"):
        runtime._run_verifier(run_id, candidate.candidate_id)

    applied = runtime.apply_allocation_decision(run_id, decision.decision_id)
    assert applied["decision"]["status"] == "applied"
    assert len(applied["candidate_tasks"]) == 1
    assert len(applied["agent_sessions"]) == 1
    child = applied["candidate_tasks"][0]
    assert child["candidate_id"] == decision.replacements[0].new_candidate_id
    assert child["proposal"]["intent"].startswith("Rebuild")

    repeated = runtime.apply_allocation_decision(run_id, decision.decision_id)
    assert repeated["decision"]["status"] == "applied"
    assert repeated["candidate_tasks"][0]["candidate_id"] == child["candidate_id"]
    assert repeated["agent_sessions"] == applied["agent_sessions"]
    assert len(runtime._load_agent_sessions(run_id)) == 3
    assert runtime._load_run(run_id).candidates_total == 3


def test_scheduler_retries_transient_failure_and_recovers_running_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    candidate = tasks[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 7\n",
        encoding="utf-8",
    )
    runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="exercise scheduler recovery",
    )
    _settle_annotation(runtime, run_id, candidate.candidate_id, 1)
    task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        1,
    )
    assert task is not None
    SchedulerManager(runtime).save_task(
        task.model_copy(
            update={
                "state": "running",
                "attempts": 1,
                "next_attempt_at": "2000-01-01T00:00:00Z",
                "attempt_history": [
                    {
                        "attempt": 1,
                        "started_at": "2000-01-01T00:00:00Z",
                        "error": "worker process exited",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        "goal_plus.adaptive_search.scheduler.SCHEDULER_RETRY_BACKOFF_SECONDS",
        (0, 0, 0),
    )
    scheduler = _FlakyScheduler()
    resolve_candidate_schedules(
        runtime.root_dir,
        run_id,
        candidate.candidate_id,
        through_iteration=1,
        scheduler=scheduler,
    )

    recovered = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        1,
    )
    assert recovered is not None
    assert recovered.state == "completed"
    assert recovered.attempts == 3
    assert scheduler.calls == 2
    assert "temporary provider failure" in recovered.attempt_history[1]["error"]


def test_scheduler_drains_candidates_concurrently(tmp_path: Path) -> None:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    for index, candidate in enumerate(tasks, start=1):
        session = runtime.start_agent_session(run_id, candidate.candidate_id)
        candidate.workspace.joinpath("initial_program.py").write_text(
            f"VALUE = {index}\n",
            encoding="utf-8",
        )
        runtime._run_verifier(
            run_id,
            candidate.candidate_id,
            agent_session_id=session.agent_session_id,
            hypothesis=f"candidate {index} scheduler input",
        )
        _settle_annotation(runtime, run_id, candidate.candidate_id, 1)

    scheduler = _ConcurrentScheduler()
    assert drain_schedule_tasks(
        runtime.root_dir,
        run_id,
        scheduler=scheduler,
    ) == 2
    assert scheduler.max_active == 2
    rewards = runtime._adaptive_search.list_scheduler_rewards(run_id)
    assert {item.candidate_id for item in rewards} == {
        candidate.candidate_id for candidate in tasks
    }
    assert len(rewards) == 2


def test_scheduler_policy_inputs_are_frozen_and_never_executed(tmp_path: Path) -> None:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    candidate = tasks[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="inspect frozen scheduler context",
    )
    _settle_annotation(runtime, run_id, candidate.candidate_id, 1)
    context = SchedulerManager(runtime).build_context(
        run_id,
        candidate.candidate_id,
        1,
    )
    reward_context = context["reward_context"]
    allocation_context = context["allocation_context"]
    scheduler_context = context["scheduler_context"]
    reward = reward_context["policy"]
    allocation = allocation_context["policy"]
    assert isinstance(reward, dict)
    assert reward == {
        "kind": "reward",
        "reference": "evidence_llm_value/v3",
        "name": "evidence_llm_value",
        "version": 3,
        "instructions": reward["instructions"],
        "supplemental_prompt": "Compute reward from rubric plus exploration.",
    }
    assert "marginal value" in reward["instructions"]
    assert "outcome_gain" in reward["instructions"]
    assert "information_gain" in reward["instructions"]
    assert "direction_gain" in reward["instructions"]
    assert "repeated public pass" in reward["instructions"]
    assert "C=20" in reward["instructions"]
    assert allocation == {
        "kind": "allocation",
        "reference": "value_guided_replace/v2",
        "name": "value_guided_replace",
        "version": 2,
        "pruning_instructions": allocation["pruning_instructions"],
    }
    assert "two consecutive attempts" in allocation["pruning_instructions"]
    assert "peer frontier" in allocation["pruning_instructions"]
    assert "fixed replacement-pressure threshold" in allocation["pruning_instructions"]
    assert "derivation_instructions" not in allocation
    assert "allocation_flag" not in allocation["pruning_instructions"]
    assert "lazy" not in allocation["pruning_instructions"].lower()
    assert "current attempt's marginal contribution" in SCHEDULER_INSTRUCTIONS
    assert "expected remaining marginal value" in SCHEDULER_INSTRUCTIONS
    assert "opportunity cost" in SCHEDULER_INSTRUCTIONS
    assert "selected policies" in SCHEDULER_INSTRUCTIONS
    assert "output protocol is runtime-owned" in SCHEDULER_INSTRUCTIONS
    assert "lazy confirmation" in SCHEDULER_INSTRUCTIONS
    assert "untrusted scheduler input" in " ".join(
        scheduler_context["input_limitations"]  # type: ignore[arg-type]
    )
    assert allocation_context["phase"] == "screen"
    assert allocation_context["peer_frontiers"]
    assert reward_context["candidate_history"]["previous_node"] is None


def test_terminal_annotation_injects_missing_view_guidance(tmp_path: Path) -> None:
    runtime, run_id, tasks = _start_runtime(tmp_path)
    candidate = tasks[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    candidate.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 4\n",
        encoding="utf-8",
    )
    runtime._run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="exercise missing View context",
    )

    assert SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        1,
    ) is None
    _settle_annotation(
        runtime,
        run_id,
        candidate.candidate_id,
        1,
        terminal_error=True,
    )
    task = SchedulerManager(runtime).load_task(
        run_id,
        candidate.candidate_id,
        1,
    )
    assert task is not None
    assert task.annotation_state == "terminal_error"
    assert task.evidence_view is None
    context = SchedulerManager(runtime).build_context(
        run_id,
        candidate.candidate_id,
        1,
    )
    assert context["scheduler_context"]["qualitative_evidence"] == {
        "annotation_task_id": task.annotation_task_id,
        "state": "terminal_error",
        "view": None,
        "guidance": (
            "AnnotationTask ERROR， Evidence View缺失，建议通过历史观测自行决定补偿方案"
        ),
    }


def test_reward_v3_scores_marginal_information_and_direction() -> None:
    from goal_plus.adaptive_search import resolve_scheduler_policy

    policy = resolve_scheduler_policy("evidence_llm_value/v3", kind="reward")

    assert "marginal value" in policy.instructions
    assert "new candidate-best public result" in policy.instructions
    assert "recovery to an already observed best result" in policy.instructions
    assert "repeated public pass" in policy.instructions
    assert "must receive R=0" in policy.instructions
    assert "gamma=0.8" in policy.instructions
    assert "parent_node_id" in policy.instructions
    assert "reward_task_id" in policy.instructions
    assert "projected_uct" in policy.instructions
    assert "never modify a ScheduleTask" in policy.instructions


def test_allocation_v2_retires_regression_stagnation_and_redundancy() -> None:
    from goal_plus.adaptive_search import resolve_scheduler_policy

    policy = resolve_scheduler_policy("value_guided_replace/v2", kind="allocation")

    assert "public regression" in policy.pruning_instructions
    assert "two consecutive attempts" in policy.pruning_instructions
    assert "peer frontier" in policy.pruning_instructions
    assert "old high rewards must not hide" in policy.pruning_instructions
    assert "No source-minus-retiree numeric gap" in policy.derivation_instructions
    assert "strong source node may seed multiple replacements" in (
        policy.derivation_instructions
    )


def test_active_policies_build_matching_contexts() -> None:
    from goal_plus.adaptive_search.policies import (
        SchedulerPolicyContextInput,
        build_scheduler_policy_contexts,
        resolve_search_scheduler_policies,
    )

    previous = {
        "node_id": "c001/iteration-0001",
        "parent_node_id": "run/root",
        "settlement_id": "settlement-1",
        "reward_task_id": "schedule-1",
        "reward": {"reward": 20.0, "components": {"outcome": 15.0}},
        "reward_source": "completed_schedule_task",
        "iteration": 1,
        "commit": "a" * 40,
        "artifact_hash": "b" * 64,
        "score": 1.0,
        "hypothesis": "first direction",
        "disposition": "keep",
        "changed_files": ["candidate.py"],
    }
    current = {
        **previous,
        "node_id": "c001/iteration-0002",
        "parent_node_id": previous["node_id"],
        "settlement_id": "settlement-2",
        "reward_task_id": "schedule-2",
        "reward": None,
        "reward_source": "current_scheduler_output",
        "iteration": 2,
        "commit": "c" * 40,
        "artifact_hash": "d" * 64,
        "score": 0.5,
        "hypothesis": "second direction",
    }
    source = SchedulerPolicyContextInput(
        current_candidate_id="c001",
        current_node_id=current["node_id"],
        metric_direction="maximize",
        phase="screen",
        candidates=[
            {
                "candidate_id": "c001",
                "allocation_eligibility": "eligible",
                "allocation_depth": 0,
                "expansion_source": None,
                "nodes": [previous, current],
            }
        ],
        uct={"total_visits": 1, "node_visits": 1, "mean_reward": 20.0},
        capacity={
            "max_parallel": 2,
            "max_candidates": None,
            "materialized_candidates": 1,
            "available_replacement_slots": 1,
        },
    )

    marginal = build_scheduler_policy_contexts(
        resolve_search_scheduler_policies(
            reward="evidence_llm_value/v3",
            allocation="value_guided_replace/v2",
        ),
        source.model_copy(
            update={"phase": "confirm", "reserved_candidate_ids": ["c002"]}
        ),
    )
    assert "milestones" in marginal["reward_context"]["candidate_history"]
    assert "peer_frontiers" in marginal["allocation_context"]
    assert marginal["allocation_context"]["reserved_candidate_ids"] == ["c002"]
    assert "derivation_instructions" in marginal["allocation_context"]["policy"]


def test_superseded_policies_are_not_registered() -> None:
    from goal_plus.adaptive_search import resolve_scheduler_policy

    for reference in ("evidence_llm_value/v1", "evidence_llm_value/v2"):
        with pytest.raises(ValueError, match="unknown reward scheduler policy"):
            resolve_scheduler_policy(reference, kind="reward")
    for reference in (
        "value_guided_replace/v1",
        "fixed_reward_threshold/v1",
        "shadow_noop/v1",
    ):
        with pytest.raises(ValueError, match="unknown allocation scheduler policy"):
            resolve_scheduler_policy(reference, kind="allocation")
    assert (
        resolve_scheduler_policy("evidence_llm_value/v3", kind="reward").version
        == 3
    )
    assert resolve_scheduler_policy(
        "value_guided_replace/v2", kind="allocation"
    ).version == 2


def test_scheduler_decision_is_separate_from_boolean_allocation_flag() -> None:
    with pytest.raises(ValueError, match="allocation_flag=false"):
        SchedulerOutput(
            reward=SchedulerReward(
                reward=0.0,
                rubric_score=0.0,
                exploitation=0.0,
                exploration=0.0,
                uct=0.0,
                rationale="No visible progress.",
            ),
            allocation_flag=False,
            allocation_rationale="continue",
            allocation_confidence=1.0,
            decision=AllocationProposal(
                replacements=[
                    AllocationReplacementProposal(
                        retire_candidate_id="c001",
                        new_candidate_id="c002",
                        source_candidate_id="c001",
                        source_iteration=1,
                        source_node_id="c001/iteration-0001",
                        rationale="replace",
                        exploration_direction="new direction",
                    )
                ],
                rationale="replace",
            ),
        )

    with pytest.raises(ValueError):
        SchedulerOutput.model_validate(
            {
                "reward": {
                    "reward": 0.0,
                    "rubric_score": 0.0,
                    "exploitation": 0.0,
                    "exploration": 0.0,
                    "uct": 0.0,
                    "rationale": "No visible progress.",
                },
                "allocation_flag": {
                    "decision": "reschedule",
                    "rationale": "retry elsewhere",
                    "confidence": 0.8,
                },
            }
        )
