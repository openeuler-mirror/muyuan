from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import subprocess
import threading
from unittest.mock import Mock

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import (
    AgentSessionHandle,
    AgentSessionRecord,
    ArtifactRef,
    CandidateProposal,
    CandidateTask,
    FrozenSpec,
    GoalPlusGateResult,
    GoalPlusNextAction,
    GoalPlusRecord,
    GoalPlusWorkItem,
    GoalPlusLinkedSearch,
    GoalPlusFinalCheck,
    MainAgentNotification,
    PromotionRecord,
    RunRecord,
    RunState,
    RunSummary,
    ScoreReport,
    SearchPlan,
    SearchSpec,
    StrategySpec,
    VerifierResult,
    VerifierRole,
    WorkspaceCapacityEstimate,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import GoalPlusTools, SearchTools, require_tool_role
from tests._runtime_helpers import create_candidate, make_project, spec_for, start_host_verified_goal


@pytest.mark.parametrize("returncode", [0, 1])
def test_wait_verifier_delivers_exact_attempt_without_resubmitting(tmp_path, monkeypatch, returncode):
    runtime = FileSearchRuntime(tmp_path / ".gp")
    run_id, candidate_id, _ = create_candidate(runtime, make_project(tmp_path))
    session = runtime.start_agent_session(run_id, candidate_id)
    tools = SearchTools(runtime)
    started, release = threading.Event(), threading.Event()

    def execute(command, **kwargs):
        started.set()
        assert release.wait(10)
        return subprocess.CompletedProcess(command, returncode, '{"combined_score":1}\n', "")

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", execute)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(tools.goal_plus_search_run_verifier, run_id, candidate_id,
                                 agent_session_id=session.agent_session_id, hypothesis="one attempt")
        try:
            assert started.wait(10)
            before = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
            args = dict(run_id=run_id, candidate_id=candidate_id, attempt_id=before.verification.attempt_id,
                        agent_session_id=session.agent_session_id)
            assert tools.goal_plus_search_run_verifier(**args)["state"] == "pending"
            with pytest.raises(ValueError, match="VerifierAttemptUnavailable"):
                tools.goal_plus_search_run_verifier(**{**args, "attempt_id": "unknown"})
            with pytest.raises(ValueError, match="GoalPlusSessionMismatch"):
                tools.goal_plus_search_run_verifier(**{**args, "candidate_id": "c999"})
            with pytest.raises(ValueError, match="VerifierQueryInvalid"):
                tools.goal_plus_search_run_verifier(**args, hypothesis="another attempt")
            assert runtime.candidate_creation.state.read_candidate(run_id, candidate_id) == before
        finally:
            release.set()
        report = future.result(timeout=10)
        result = tools.goal_plus_search_run_verifier(**args)
        assert result == report
        with runtime.settlement.state.lock_verifier(run_id, candidate_id):
            assert tools.goal_plus_search_run_verifier(**args)["state"] == "pending"
        assert result["process_passed"] is (returncode == 0)
        # A new facade must recover the result from state, not a server-local task cache.
        assert SearchTools(FileSearchRuntime(runtime.root_dir)).goal_plus_search_run_verifier(**args) == result
        iteration, = runtime.list_iterations(run_id, candidate_id)
        assert iteration["hypothesis"] == "one attempt"


def spec_dict() -> dict:
    return {
        "objective": "maximize toy score",
        "metric_name": "combined_score",
        "metric_direction": "maximize",
        "source_path": ".",
        "edit_surface": {
            "allow": ["initial_program.py"],
            "deny": ["evaluator.py"],
        },
        "budget": {
            "max_parallel": 2,
        },
        "process_verifiers": [
            {
                "name": "score",
                "role": "ranking_signal",
                "command": ["python", "evaluator.py"],
            }
        ],
    }


def frozen_spec(*, task_skill_paths: list[str] | None = None) -> FrozenSpec:
    spec = SearchSpec.model_validate(spec_dict())
    return FrozenSpec(runtime_provider="direct", agent_harness="codex",
        frozen_spec_id="spec_123",
        spec_hash="hash",
        spec=spec,
        verifier_hashes={"evaluator.py": "abc"},
        frozen_verifier_paths={"evaluator.py": "/tmp/evaluator.py"},
        task_skill_paths=task_skill_paths or [],
        workspace_capacity_estimate=WorkspaceCapacityEstimate(
            source_path="/tmp/source",
            workspace_provider="git_worktree",
            basis="git_tracked_checkout",
            worktree_nodes=2,
            source_scope_files=3,
            source_scope_bytes=100,
            materialized_source_files=2,
            materialized_source_bytes=80,
            checkout_scope_path="/tmp/source",
            checkout_files_per_node=2,
            checkout_bytes_per_node=40,
            candidate_checkouts_bytes=80,
            shared_object_store_bytes=20,
            estimated_incremental_bytes=80,
            conservative_upper_bound_bytes=100,
            available_bytes=1000,
            fits_available_space=True,
        ),
        created_at="2026-06-24T00:00:00Z",
    )


def goal_plus_record() -> GoalPlusRecord:
    return GoalPlusRecord(
        goal_plus_id="gp_0001",
        raw_goal="Optimize a benchmark if possible",
        status="active",
        phase="intake",
        next_action=GoalPlusNextAction(
            kind="record_triage",
            description="classify the goal",
        ),
        created_at="2026-07-06T00:00:00Z",
        updated_at="2026-07-06T00:00:00Z",
    )


def test_candidate_role_cannot_manage_adaptive_allocation() -> None:
    for tool_name in (
        "goal_plus_search_list_allocation_decisions",
        "goal_plus_search_apply_allocation_decision",
    ):
        with pytest.raises(PermissionError, match="GoalPlusRoleDenied"):
            require_tool_role(
                "candidate",
                tool_name,
                caller_agent_session_id="agent_001",
                requested_agent_session_ids=[],
            )


def test_search_bootstrap_requires_active_host_verified_goal(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(root)
    tools = SearchTools(runtime)
    spec = spec_for(project, max_parallel=1)

    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        tools.goal_plus_search_freeze_spec(
            "gp_missing", spec, [str(project / "evaluator.py")]
        )
    assert not (root / "specs").exists()

    frozen_without_facade = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")
    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        tools.goal_plus_search_create("gp_missing", frozen_without_facade.frozen_spec_id, "create")
    assert not (root / "runs").exists()

    first = start_host_verified_goal(root, project)
    second = start_host_verified_goal(
        root,
        project,
        session_id="session-other",
        invocation_id="codex:other:start",
    )

    frozen = tools.goal_plus_search_freeze_spec(
        first.goal_plus_id,
        spec,
        ["evaluator.py"],
    )
    second_frozen = tools.goal_plus_search_freeze_spec(
        second.goal_plus_id,
        spec,
        ["evaluator.py"],
    )
    assert runtime.search_run.state.read_frozen_spec(frozen["frozen_spec_id"]).goal_plus_id == first.goal_plus_id
    assert runtime.search_run.state.read_frozen_spec(second_frozen["frozen_spec_id"]).goal_plus_id == second.goal_plus_id
    assert frozen["frozen_spec_id"] != second_frozen["frozen_spec_id"]
    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        tools.goal_plus_search_create(second.goal_plus_id, frozen["frozen_spec_id"], "create")
    created = tools.goal_plus_search_create(first.goal_plus_id, frozen["frozen_spec_id"], "create")
    assert created["run_id"].startswith("run_")


def test_tool_facades_reject_targets_outside_active_goal(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    project = make_project(tmp_path)
    start_host_verified_goal(root, project)
    goal_runtime = FileGoalPlusRuntime(root)
    record = goal_runtime.require_host_verified_active_goal()
    before = record.model_dump(mode="json")

    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        GoalPlusTools(goal_runtime).goal_plus_set_status("gp_9999", "complete")
    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        SearchTools(FileSearchRuntime(root), goal_runtime).goal_plus_search_select(
            "run_unlinked"
        )
    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        SearchTools(FileSearchRuntime(root), goal_runtime).goal_plus_search_create(
            record.goal_plus_id,
            "spec_missing",
            "create",
            source_run_id="run_unlinked",
        )

    assert goal_runtime.status(record.goal_plus_id).model_dump(mode="json") == before
    assert not (root / "runs").exists()


def test_search_freeze_spec_resolves_source_relative_artifacts(
    tmp_path: Path,
) -> None:
    runtime = Mock()
    runtime.freeze_spec.return_value = frozen_spec()
    tools = SearchTools(runtime, Mock())
    spec = spec_dict()
    spec["source_path"] = str(tmp_path)

    result = tools.goal_plus_search_freeze_spec("gp_0001", spec, ["evaluator.py"])

    spec_arg, paths_arg = runtime.freeze_spec.call_args.args
    assert isinstance(spec_arg, SearchSpec)
    assert paths_arg == [tmp_path / "evaluator.py"]
    assert runtime.freeze_spec.call_args.kwargs == {"goal_plus_id": "gp_0001"}
    assert result == {"frozen_spec_id": "spec_123", "state": "frozen"}


@pytest.mark.parametrize("artifact", ["../evaluator.py", "/tmp/evaluator.py"])
def test_search_freeze_spec_rejects_non_source_relative_artifacts(
    tmp_path: Path,
    artifact: str,
) -> None:
    spec = spec_dict()
    spec["source_path"] = str(tmp_path / "source")

    with pytest.raises(ValueError, match="source_path-relative"):
        SearchTools(Mock(), Mock()).goal_plus_search_freeze_spec(
            "gp_0001", spec, [artifact]
        )


def test_search_freeze_spec_does_not_echo_task_local_skills() -> None:
    runtime = Mock()
    runtime.freeze_spec.return_value = frozen_spec(
        task_skill_paths=[".agents/skills/review/SKILL.md"]
    )

    result = SearchTools(runtime, Mock()).goal_plus_search_freeze_spec(
        "gp_0001", spec_dict(), ["evaluator.py"]
    )

    assert result == {"frozen_spec_id": "spec_123", "state": "frozen"}
    assert runtime.freeze_spec.return_value.task_skill_paths == [".agents/skills/review/SKILL.md"]


def test_search_tools_delegate_runtime_calls_with_models() -> None:
    runtime = Mock()
    agent_session = AgentSessionRecord(
        agent_session_id="agent_001",
        run_id="run_1",
        candidate_id="c001",
        created_at="2026-06-24T00:00:00Z",
        updated_at="2026-06-24T00:00:00Z",
        workspace=Path("/tmp/c001"),
        launch={
            "agent_type": "search_candidate_agent",
            "description": "c001 try one",
            "prompt": "agent_session_id=agent_001; candidate_id=c001; idea: try one",
        },
    )
    runtime.create_run.return_value = "run_1"
    runtime.status.return_value = RunSummary(
        run_id="run_1",
        state=RunState.RUNNING,
        frozen_spec_id="spec_123",
        candidates_total=0,
        candidates_evaluated=0,
    )
    runtime.invalidate_run.return_value = RunRecord(
        run_id="run_1",
        state=RunState.ABORTED,
        frozen_spec_id="spec_123",
        source_path=".",
        created_at="2026-06-24T00:00:00Z",
        invalidated_at="2026-06-24T00:01:00Z",
        invalidation_reason="verifier_coverage_inadequate",
        invalidation_summary="missing required case",
        invalidation_evidence=[{"case": "edge"}],
    )
    runtime.list_history.return_value = {
        "run_id": "run_1",
        "candidates": [],
    }
    runtime.plan_next.return_value = SearchPlan(
        run_id="run_1",
        plan_id="plan_001",
        strategy=StrategySpec(),
        requested_k=1,
        planned_k=1,
        remaining_budget=4,
        created_at="2026-06-24T00:00:00Z",
    )
    runtime.start_batch.return_value = [
        CandidateTask(
            run_id="run_1",
            candidate_id="c001",
            hypothesis="try",
            workspace=Path("/tmp/c001"),
            task_skill_paths=[
                Path("/tmp/c001/.agents/skills/review/SKILL.md")
            ],
            allowed_files=["initial_program.py"],
            denied_files=["evaluator.py"],
        )
    ]
    runtime.start_agent_session.return_value = agent_session
    runtime.get_global_evidence.return_value = [
        {
            "candidate_id": "c001",
            "iteration": 1,
            "score": 1.0,
            "disposition": "keep",
            "commit": "abc123",
            "view": "Changed the candidate value.",
        }
    ]
    runtime.get_evidence_detail.return_value = {
        "candidate_id": "c001",
        "iteration": 1,
        "supplemental_evaluation": {"summary": "Changed the candidate value."},
    }
    runtime.redispatch_candidate.return_value = agent_session.model_copy(
        update={
            "agent_session_id": "agent_002",
            "launch": {
                "agent_type": "search_candidate_agent_deep",
                "description": "c001 resume",
                "prompt": "state_level_resume=true; agent_session_id=agent_002",
            },
        }
    )
    generic_bound_session = agent_session.model_copy(
        update={
            "agent_harness": "codex",
            "session_handle": AgentSessionHandle(agent_harness="codex", external_id="native-session-001"),
        }
    )
    continued_session = generic_bound_session.model_copy(
        update={
            "launch": {
                "task_name": "search_agent_001",
                "agent_type": "search_candidate_agent",
                "description": "c001 continue try one",
                "prompt": "continue_existing_agent_session=true; agent_session_id=agent_001",
            }
        }
    )
    runtime.bind_agent_handle.return_value = generic_bound_session
    runtime.continue_agent_session.return_value = continued_session
    runtime.get_agent_context.return_value = {"agent_session_id": "agent_001"}
    runtime.get_agent_observability.return_value = {
        "agent_session_id": "agent_001",
        "source": "codex_session_jsonl",
    }
    runtime.should_inject_global_evidence_after_verifier.return_value = False
    runtime.run_verifier.return_value = ScoreReport(
        run_id="run_1",
        candidate_id="c001",
        validity_passed=True,
        process_passed=True,
        aggregate_score=1.0,
        verifier_results=[
            VerifierResult(
                name="score",
                role=VerifierRole.RANKING_SIGNAL,
                passed=True,
                score=1.0,
                metrics={"stderr_tail": "candidate diagnostic"},
                log_path=Path("/tmp/iteration-0001-score-a1b2c3d4.log"),
            )
        ],
    )
    runtime.list_agent_iterations.return_value = [
        {"iteration": 1, "score": 0.4, "agent_session_id": "agent_001"},
        {"iteration": 2, "score": 0.7, "agent_session_id": "agent_001"},
    ]
    runtime.list_allocation_decisions.return_value = []
    runtime.apply_allocation_decision.return_value = {
        "decision": {"decision_id": "allocation_0001", "status": "applied"},
        "candidate_tasks": [],
        "agent_sessions": [agent_session.model_dump(mode="json")],
    }
    runtime.select.return_value = {"selected_candidate_id": "c001"}
    runtime.report.return_value = Path("/tmp/report.md")
    runtime.promote.return_value = Path("/tmp/c001.patch")
    runtime.promotion_record.return_value = PromotionRecord(
        promotion_id="promotion_c001",
        run_id="run_1",
        candidate_id="c001",
        provider="git_worktree",
        strategy="squash_commit",
        base_artifact_ref=ArtifactRef(
            kind="git_commit", id="base123", provider="git_worktree"
        ),
        selected_artifact_ref=ArtifactRef(
            kind="git_commit", id="selected123", provider="git_worktree"
        ),
        publication_artifact_ref=ArtifactRef(
            kind="git_commit", id="publish123", provider="git_worktree"
        ),
        artifact_path=Path("/tmp/c001.patch"),
        created_at="2026-06-24T00:00:00Z",
    )
    runtime.apply_promotion.return_value = (
        runtime.promotion_record.return_value.model_copy(
            update={
                "state": "applied",
                "applied_source_revision": "source456",
                "applied_at": "2026-06-24T00:01:00Z",
            }
        )
    )
    notification = MainAgentNotification(
        notification_id="notification-1",
        run_id="run_1",
        publication_revision=1,
        kind="publication_applied",
        payload={"publication_state": "applied"},
        created_at="2026-06-24T00:01:00Z",
    )
    runtime.main_agent_notifications.return_value = [notification]
    runtime.acknowledge_main_agent_notification.return_value = (
        notification.model_copy(
            update={
                "state": "delivered",
                "delivered_at": "2026-06-24T00:02:00Z",
            }
        )
    )
    runtime.promotion_apply_coordinates.return_value = (
        Path("/tmp/source/repository/task"),
        Path("/tmp/source/repository"),
        "task",
    )
    tools = SearchTools(runtime, Mock())

    invalidated = tools.goal_plus_search_invalidate_run(
        "run_1",
        "verifier_coverage_inadequate",
        "missing required case",
        [{"case": "edge"}],
    )
    assert invalidated["state"] == "aborted"
    assert tools.goal_plus_search_status("run_1")["state"] == "running"
    assert tools.goal_plus_search_list_history("run_1", top_n=3, sort_by="created") == {
        "run_id": "run_1",
        "candidates": [],
        "details": {"tool": "goal_plus_search_list_history", "detail": True},
    }
    assert tools.goal_plus_search_plan_next("run_1", requested_k=1)["plan_id"] == "plan_001"
    batch = tools.goal_plus_search_start_batch(
        "run_1",
        "plan_001",
        [{"intent": "try an independent approach"}],
    )
    assert batch[0]["candidate_id"] == "c001"
    assert "instructions" not in batch[0]
    assert runtime.start_batch.return_value[0].task_skill_paths
    assert tools.search_start_agent_session(
        "run_1",
        "c001",
        {"goal": "try one"},
    )["agent_session_id"] == "agent_001"
    redispatched = tools.search_redispatch_candidate(
        "run_1",
        "c001",
        worker_budget={"max_turns": 16},
    )
    assert redispatched["agent_session_id"] == "agent_002"
    assert "launch" not in redispatched
    assert tools.search_bind_agent_handle(
        "agent_001",
        {"agent_harness": "codex", "external_id": "native-session-001"},
    )["session_handle"]["external_id"] == "native-session-001"
    continued = tools.search_continue_agent_session("agent_001")
    assert continued["agent_session_id"] == "agent_001"
    assert "launch" not in continued
    assert tools.goal_plus_search_get_agent_context("agent_001") == {"agent_session_id": "agent_001"}
    assert tools.goal_plus_search_get_global_evidence("agent_001")[0]["view"] == (
        "Changed the candidate value."
    )
    runtime.stage_shared_tool.return_value = {
        "staged_name": "trace-helper",
        "staging_path": "/tmp/c001/.tmp/share-out/trace-helper",
    }
    assert tools.goal_plus_search_stage_shared_tool(
        "agent_001",
        "trace-helper",
        "Collect a bounded trace.",
        "trace.py:main",
        [".tmp/tool-drafts/trace.py"],
    )["staged_name"] == "trace-helper"
    assert tools.goal_plus_search_get_evidence_detail("agent_001", "c001", 1)[
        "supplemental_evaluation"
    ]["summary"] == "Changed the candidate value."
    runtime.get_evidence_detail.assert_called_once_with("agent_001", "c001", 1)
    assert tools.goal_plus_search_get_agent_observability("agent_001") == {
        "agent_session_id": "agent_001",
        "source": "codex_session_jsonl",
    }
    verifier_report = tools.goal_plus_search_run_verifier("run_1", "c001")
    assert verifier_report["aggregate_score"] == 1.0
    assert verifier_report["verifier_results"][0]["metrics"]["stderr_tail"] == (
        "candidate diagnostic"
    )
    assert verifier_report["verifier_results"][0]["log_path"].endswith(
        "iteration-0001-score-a1b2c3d4.log"
    )
    worker_verifier_report = tools.goal_plus_search_run_verifier(
        "run_1",
        "c001",
        agent_session_id="agent_001",
        hypothesis="try a fused path",
        toolization_decision=None,
        on_submitted=None,
    )
    assert worker_verifier_report["aggregate_score"] == 1.0
    assert "global_evidence_injected" not in worker_verifier_report
    assert "global_evidence_snapshot" not in worker_verifier_report
    runtime.run_verifier.assert_called_with(
        "run_1",
        "c001",
        scope="process",
        agent_session_id="agent_001",
        hypothesis="try a fused path",
        toolization_decision=None,
        on_submitted=None,
    )
    iterations = tools.goal_plus_search_list_iterations("agent_001")["items"]
    assert len(iterations) == 2
    assert iterations[0]["iteration"] == 1
    assert iterations[1]["score"] == 0.7
    runtime.list_agent_iterations.assert_called_once_with("agent_001")
    assert tools.goal_plus_search_list_allocation_decisions("run_1", "pending") == {"items": [], "total": 0, "next_offset": None}
    runtime.list_allocation_decisions.assert_called_once_with("run_1", "pending")
    applied = tools.goal_plus_search_apply_allocation_decision("run_1", "allocation_0001")
    assert applied["decision"]["status"] == "applied"
    assert applied["agent_sessions"][0]["agent_session_id"] == "agent_001"
    assert "launch" not in applied["agent_sessions"][0]
    assert runtime.apply_allocation_decision.return_value["agent_sessions"][0]["launch"]
    runtime.apply_allocation_decision.assert_called_once_with(
        "run_1", "allocation_0001"
    )
    assert tools.goal_plus_search_select("run_1") == {"selected_candidate_id": "c001"}
    assert tools.goal_plus_search_report("run_1") == {
        "report_path": "/tmp/report.md",
        "html_report_path": "/tmp/report.html",
    }
    assert tools.goal_plus_search_promote("run_1", "c001") == {
        "artifact_path": "/tmp/c001.patch",
        "source_path": "/tmp/source/repository/task",
        "promotion_id": "promotion_c001",
        "promotion_mode": "apply",
        "apply_required": True,
        "state": "prepared",
    }
    assert tools.goal_plus_search_apply_promotion("run_1")["state"] == "applied"
    assert tools.search_main_agent_notifications("run_1") == {
        "run_id": "run_1",
        "notifications": [notification.model_dump(mode="json")],
    }
    assert tools.search_ack_main_agent_notification(
        "run_1",
        "notification-1",
    )["state"] == "delivered"

    proposal_arg = runtime.start_batch.call_args.args[2][0]
    assert isinstance(proposal_arg, CandidateProposal)
    runtime.list_history.assert_called_once_with("run_1", top_n=3, sort_by="created")
    runtime.invalidate_run.assert_called_once_with(
        "run_1",
        reason="verifier_coverage_inadequate",
        summary="missing required case",
        evidence=[{"case": "edge"}],
    )
    runtime.plan_next.assert_called_once_with("run_1", requested_k=1)
    runtime.start_agent_session.assert_called_once_with(
        run_id="run_1",
        candidate_id="c001",
        directive={"goal": "try one"},
        worker_budget=None,
    )
    runtime.redispatch_candidate.assert_called_once_with(
        run_id="run_1",
        candidate_id="c001",
        worker_budget={"max_turns": 16},
    )
    runtime.bind_agent_handle.assert_called_once_with(
        agent_session_id="agent_001",
        handle={"agent_harness": "codex", "external_id": "native-session-001"},
    )
    runtime.continue_agent_session.assert_called_once_with(
        agent_session_id="agent_001",
        worker_budget=None,
    )
    runtime.get_agent_observability.assert_called_once_with("agent_001")
    runtime.get_global_evidence.assert_called_once_with("agent_001")
    runtime.stage_shared_tool.assert_called_once_with(
        agent_session_id="agent_001",
        name="trace-helper",
        summary="Collect a bounded trace.",
        entrypoint="trace.py:main",
        candidate_relative_source_paths=[".tmp/tool-drafts/trace.py"],
        publication_intent="new",
        supersedes_tool_id=None,
        capability_ids=None,
        coverage_keys=None,
    )


def _successful_score_report() -> ScoreReport:
    return ScoreReport(
        run_id="run_1",
        candidate_id="c001",
        validity_passed=True,
        process_passed=True,
        aggregate_score=1.0,
        verifier_results=[],
    )


def test_worker_verifier_injects_global_evidence_when_enabled() -> None:
    runtime = Mock()
    runtime.run_verifier.return_value = _successful_score_report()
    snapshot = [{"candidate_id": "c002", "iteration": 3, "score": 2.0}]
    runtime.should_inject_global_evidence_after_verifier.return_value = True
    runtime.get_global_evidence.return_value = snapshot

    result = SearchTools(runtime).goal_plus_search_run_verifier(
        "run_1",
        "c001",
        agent_session_id="agent_001",
        hypothesis="Try a different layout",
    )

    assert result["global_evidence_snapshot"] == snapshot
    assert result["global_evidence_injected"] is True
    assert result["global_evidence_entry_count"] == 1
    runtime.get_global_evidence.assert_called_once_with(
        "agent_001",
        source="verifier_injected",
    )


def test_thinkthread_context_injects_deferred_global_evidence_when_enabled() -> None:
    runtime = Mock()
    runtime.get_agent_context.return_value = {
        "agent_session_id": "agent_001",
        "run_id": "run_1",
        "candidate_id": "c001",
        "agent_harness": "pi", "runtime_provider": "thinkthread",
        "iteration_count": 1,
    }
    snapshot = [{"candidate_id": "c001", "iteration": 1, "score": 2.0}]
    runtime.should_inject_global_evidence_after_verifier.return_value = True
    runtime.get_global_evidence.return_value = snapshot

    result = SearchTools(runtime).goal_plus_search_get_agent_context("agent_001")

    assert result["global_evidence_snapshot"] == snapshot
    assert result["global_evidence_injected"] is True
    assert result["global_evidence_entry_count"] == 1
    runtime.should_inject_global_evidence_after_verifier.assert_called_once_with(
        "run_1"
    )
    runtime.get_global_evidence.assert_called_once_with(
        "agent_001",
        source="verifier_injected",
    )


def test_worker_verifier_keeps_success_when_evidence_injection_fails() -> None:
    runtime = Mock()
    runtime.run_verifier.return_value = _successful_score_report()
    runtime.should_inject_global_evidence_after_verifier.return_value = True
    runtime.get_global_evidence.side_effect = RuntimeError("view unavailable")

    result = SearchTools(runtime).goal_plus_search_run_verifier(
        "run_1",
        "c001",
        agent_session_id="agent_001",
        hypothesis="Try a different layout",
    )

    assert result["aggregate_score"] == 1.0
    assert result["global_evidence_injected"] is False
    assert "view unavailable" in result["global_evidence_warning"]
    assert "global_evidence_snapshot" not in result


def test_parent_and_promotion_verifiers_do_not_inject_global_evidence() -> None:
    runtime = Mock()
    runtime.run_verifier.return_value = _successful_score_report()
    tools = SearchTools(runtime)

    assert "global_evidence_injected" not in tools.goal_plus_search_run_verifier(
        "run_1", "c001"
    )
    assert "global_evidence_injected" not in tools.goal_plus_search_run_verifier(
        "run_1", "c001", scope="promotion"
    )
    runtime.should_inject_global_evidence_after_verifier.assert_not_called()


def test_goal_plus_list_models_delegates_to_host_adapter() -> None:
    runtime = Mock()
    runtime.list_available_models.return_value = {
        "agent_harness": "pi",
        "adapter_version": "pi-rpc-v1",
        "models": [{"model": "openai-codex/gpt-5.6-sol"}],
    }
    tools = SearchTools(runtime)

    assert tools.goal_plus_list_models("pi", "direct", "sol") == (
        runtime.list_available_models.return_value
    )
    runtime.list_available_models.assert_called_once_with("pi", "direct", "sol")


def test_search_tools_expose_no_lifecycle_methods() -> None:
    runtime = Mock()
    tools = SearchTools(runtime)
    for deleted in (
        "search_update_agent_status",
        "search_list_agent_status",
        "search_finish_agent_session",
        "search_abort_agent_session",
        "search_abort_all_agent_sessions",
        "search_publish_observation",
        "search_list_observations",
        "search_wait_agent_events",
        "search_submit_candidate",
        "search_submit_iteration_plan",
        "search_get_global_plan",
    ):
        assert not hasattr(tools, deleted), f"SearchTools should not expose {deleted}"


def test_goal_plus_tools_delegate_runtime_calls_with_models() -> None:
    runtime = Mock()
    record = goal_plus_record()
    runtime.create_goal.return_value = record
    runtime.status.return_value = record
    runtime.update_goal.return_value = record.model_copy(update={"goal_revision": 2})
    runtime.list_events.return_value = [{"event_type": "created"}]
    runtime.reconciliation.return_value = {"resume_eligible": True, "runs": []}
    runtime.record_triage.return_value = record
    runtime.record_work_event.return_value = record.model_copy(update={"work_items": [
        GoalPlusWorkItem(work_item_id="inspect", objective="inspect", goal_revision=1,
                         status="launching", created_at=record.created_at, updated_at=record.updated_at),
    ]})
    runtime.save_spec_draft.return_value = record
    linked = record.model_copy(update={"search_tasks": [GoalPlusLinkedSearch(run_id="run_1")]})
    runtime.link_search_run.return_value = linked
    runtime.record_search_result.return_value = linked
    runtime.prepare_final_check.return_value = {
        "goal_plus_id": "gp_0001",
        "goal_revision": 1,
        "check": {"check_id": "fc_0001_r1_001", "status": "pending"},
        "launch": {"tool": "spawn_agent"},
    }
    runtime.submit_final_check.return_value = record.model_copy(update={"status": "complete", "final_checks": [
        GoalPlusFinalCheck(check_id="fc_0001_r1_001", goal_revision=1, checker_harness="codex",
                           requested_phase="final_check", requested_at=record.created_at),
    ]})
    runtime.set_status.return_value = record.model_copy(update={"status": "complete"})
    runtime.gate.return_value = GoalPlusGateResult(
        decision="block",
        phase="intake",
        status="active",
        reason="needs triage",
        continuation_prompt="continue triage",
    )
    tools = GoalPlusTools(runtime)

    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        tools.goal_plus_create(
            raw_goal="Optimize a benchmark if possible",
            source_path=".",
            policy={"max_discovery_turns": 1},
        )
    status = tools.goal_plus_status("gp_0001")
    assert "evidence_log" not in status
    runtime.list_events.assert_not_called()
    assert "search_tasks_total" not in status
    assert status["current_search_run_id"] is None
    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        tools.goal_plus_update_goal(
            "gp_0001",
            "Revised objective",
            expected_revision=1,
            reason="user edit",
        )
    assert tools.goal_plus_record_triage(
        "gp_0001",
        {
            "is_optimization": True,
            "confidence": "medium",
            "recommended_phase": "spec_discovery",
        },
    )["phase"] == "intake"
    assert tools.goal_plus_record_work_event(
        "gp_0001",
        "inspect",
        "dispatch",
        "Codex dispatched the work item.",
        host="codex",
        agent_id="agent-1",
    )["goal_plus_id"] == "gp_0001"
    assert tools.goal_plus_save_spec_draft(
        "gp_0001",
        {
            "baseline": {},
            "metric": {},
            "correctness_gate": {},
            "edit_surface": {},
            "search_spec": spec_dict(),
            "promotion_rule": "must pass",
            "confidence": "high",
        },
    )["goal_plus_id"] == "gp_0001"
    assert tools.goal_plus_link_search_run("gp_0001", "spec_1", "run_1")["goal_plus_id"] == "gp_0001"
    assert tools.goal_plus_record_search_result(
        "gp_0001",
        run_id="run_1",
        selected_candidate_id="c001",
        report_path="/tmp/report.md",
        promotion_artifact_path="/tmp/c001.patch",
        summary="c001 selected",
    )["goal_plus_id"] == "gp_0001"
    prepared = tools.goal_plus_prepare_final_check("gp_0001", "codex")
    assert prepared["launch"]["tool"] == "spawn_agent"
    assert tools.goal_plus_submit_final_check(
        "gp_0001",
        "fc_0001_r1_001",
        1,
        "pass",
        "all requirements proven",
        evidence=[{"kind": "pytest"}],
    )["status"] == "complete"
    assert tools.goal_plus_set_status(
        "gp_0001",
        status="complete",
        reason="done",
        evidence=[{"kind": "test"}],
    )["status"] == "complete"
    assert tools.goal_plus_gate(
        "gp_0001",
        event="stop",
        context={"turn_id": "turn_1"},
    )["continuation_prompt"] == "continue triage"

    runtime.create_goal.assert_not_called()
    runtime.update_goal.assert_not_called()
    runtime.link_search_run.assert_called_once_with("gp_0001", "spec_1", "run_1")
