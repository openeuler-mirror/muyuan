from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from goal_plus import __version__
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.pi_tool import call_pi_tool
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.state.files import write_json
from goal_plus.server import create_mcp
from goal_plus.tools import SearchTools
from tests._fastmcp_helpers import tools_by_name
from tests._runtime_helpers import make_project, spec_with_strategy


def _pi_rpc_spec(project: Path):
    return spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_budget": {
                "max_runtime_seconds": 600,
                "max_turns": 8,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=2,
    )


def test_goal_monitor_scope_prefers_current_goal_over_historical_run(tmp_path: Path) -> None:
    from goal_plus.bootstrap import build_scope_state
    from goal_plus.infrastructure.monitor_inputs import _monitor_scope

    goal = FileGoalPlusRuntime(tmp_path).create_goal("Inspect current scope")
    state = build_scope_state(tmp_path)
    state.write({"scope_id": "scope_old", "run_ids": ["run_old"],
                 "state": "closed", "created_at": "2026-01-01T00:00:00Z"})
    state.write({"scope_id": "scope_current", "run_ids": [goal.goal_plus_id],
                 "state": "open", "created_at": "2026-01-02T00:00:00Z"})
    assert _monitor_scope(tmp_path, goal, "run_old")["scope_id"] == "scope_current"
    assert _monitor_scope(tmp_path, goal, None)["scope_id"] == "scope_current"
    assert _monitor_scope(tmp_path, None, "run_old")["scope_id"] == "scope_old"


def test_goal_plus_monitor_snapshot_summarizes_run_subagents_and_pi_metrics(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    first, second = runtime.start_batch(run_id, plan.plan_id)
    session = runtime.start_agent_session(run_id, first.candidate_id)
    event_log = runtime_root / "host-logs" / "pi-rpc-agent.jsonl"
    session_file = runtime_root / "host-logs" / "pi-rpc-session.jsonl"
    event_log.parent.mkdir(parents=True, exist_ok=True)
    event_log.write_text('{"type":"tool_call","name":"search_run_verifier"}\n', encoding="utf-8")
    session_file.write_text('{"type":"message","role":"assistant"}\n', encoding="utf-8")

    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "pi-rpc",
            "external_id": session.agent_session_id,
            "metadata": {
                "event_log": str(event_log),
                "session_file": str(session_file),
                "timed_out": False,
                "runner_failed": False,
                "soft_closeout_seconds": 45,
                "soft_closeout_sent": True,
                "time_advisory_sent": True,
                "time_advisory": {
                    "remaining_seconds": 30,
                    "average_submission_seconds": 90,
                    "total_verifier_count": 1,
                    "message": "time advisory",
                },
                "raw_logging": False,
                "pi_metrics": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "duration_seconds": 12.5,
                    "usage_delta": {
                        "assistantMessages": 2,
                        "input": 100,
                        "output": 25,
                        "cacheRead": 50,
                        "cacheWrite": 0,
                        "costTotal": 0.12,
                    },
                    "usage_total": {
                        "assistantMessages": 3,
                        "input": 150,
                        "output": 35,
                        "cacheRead": 75,
                        "cacheWrite": 0,
                        "costTotal": 0.18,
                    },
                    "session_stats": {
                        "toolCalls": 5,
                        "toolResults": 5,
                        "contextUsage": {
                            "tokens": 12345,
                            "contextWindow": 272000,
                            "percent": 4.5386,
                        },
                        "tokens": {"total": 260},
                        "cost": 0.18,
                    },
                },
            },
        },
    )
    SearchTools(runtime).search_run_verifier(
        run_id,
        first.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Monitor worker evidence",
    )
    runtime.get_global_evidence(
        session.agent_session_id,
        source="verifier_injected",
    )
    runtime.get_global_evidence(session.agent_session_id)
    write_json(
        runtime_root
        / "runs"
        / run_id
        / "evidence-annotator"
        / "attempts"
        / "c001-iteration-0001-attempt-01.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "candidate_id": first.candidate_id,
            "iteration": 1,
            "attempt": 1,
            "host": "pi-rpc",
            "model": "gpt-5.6-sol",
            "state": "running",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:05Z",
            "elapsed_seconds": 5.0,
            "stdout_bytes": 123,
            "stderr_bytes": 0,
            "json_lines": 3,
            "event_type_counts": {"message_update": 3},
            "last_events": [{"type": "message_update"}],
        },
    )

    snapshot = goal_plus_monitor_snapshot(
        root_dir=runtime_root,
        run_id=run_id,
        stale_after_seconds=600,
    )

    assert snapshot["run"]["run_id"] == run_id
    assert snapshot["run"]["state"] == "waiting_for_workers"
    assert snapshot["run"]["host_cleanup"]["state"] == "not_applicable"
    assert snapshot["run"]["candidates_total"] == 2
    assert snapshot["run"]["candidates_evaluated"] == 1
    annotation = snapshot["run"]["evidence_annotations"]
    assert annotation["tasks"] == 1
    assert annotation["views_published"] == 0
    assert annotation["monitor_files"] == 1
    assert annotation["active_attempts"][0]["state"] == "running"
    assert annotation["active_attempts"][0]["json_lines"] == 3
    evidence_reads = snapshot["run"]["evidence_reads"]
    assert evidence_reads["global_total"] == 2
    assert evidence_reads["global_explicit"] == 1
    assert evidence_reads["global_verifier_injected"] == 1
    assert evidence_reads["detail_total"] == 0
    assert evidence_reads["sessions_with_global_reads"] == 1
    assert evidence_reads["sessions_with_detail_reads"] == 0
    assert isinstance(evidence_reads["latest_global_read_at"], str)
    assert evidence_reads["latest_detail_read_at"] is None
    assert snapshot["strategy"] == {
        "name": "random",
        "native_host": "pi",
        "orchestration_mode": "parallel_loops",
        "worker_host": "pi-rpc",
        "inner_agent": "autoresearch",
        "legacy_worker_agent_type": None,
        "latest_plan": {
            "plan_id": plan.plan_id,
            "status": "started",
            "requested_k": 2,
            "planned_k": 2,
            "started_candidate_ids": [first.candidate_id, second.candidate_id],
            "selection_rule": "独立源码分支",
            "state": {},
        },
    }
    assert snapshot["main_agent"]["subagent_count"] == 1
    assert snapshot["main_agent"]["verifier_count"] == 1
    assert snapshot["main_agent"]["estimated_cost_total"] == 0.18
    results_tsv = snapshot["candidates"][first.candidate_id]["results_tsv"]
    assert snapshot["candidates"][first.candidate_id]["result_ledger_source"] == "gp"
    assert snapshot["candidates"][first.candidate_id]["result_count"] == 1
    assert (
        snapshot["candidates"][first.candidate_id]["workspace_ledger_projection"]
        == "results_tsv"
    )
    assert results_tsv["path"].endswith(f"/{first.candidate_id}/results.tsv")
    assert results_tsv["exists"] is True
    assert results_tsv["row_count"] == 1

    [subagent] = snapshot["subagents"]
    assert subagent["agent_session_id"] == session.agent_session_id
    assert subagent["candidate_id"] == first.candidate_id
    assert subagent["attempt_count"] == 1
    assert subagent["verifier_count"] == 1
    assert subagent["context"]["tokens"] == 12345
    assert subagent["usage_total"]["costTotal"] == 0.18
    assert subagent["duration_seconds"] == 12.5
    assert subagent["soft_closeout_seconds"] == 45
    assert subagent["soft_closeout_sent"] is True
    assert subagent["time_advisory_sent"] is True
    assert subagent["time_advisory"]["remaining_seconds"] == 30
    assert subagent["raw_logging"] is False
    assert subagent["evidence_reads"]["global_explicit"] == 1
    assert subagent["evidence_reads"]["global_verifier_injected"] == 1
    assert subagent["evidence_reads"]["detail_total"] == 0
    assert subagent["runner_failed"] is False
    assert subagent["progress_handoff"] is None
    assert subagent["liveness"] == "evaluated"
    assert subagent["observability"]["source"] == "pi_metrics"
    assert subagent["observability"]["usage"]["cost_usd"] == 0.18
    assert subagent["observability"]["usage"]["processed_tokens"] == 260
    assert subagent["observability"]["usage"]["tool_calls"] == 5
    assert subagent["observability"]["context"]["tokens"] == 12345

    statistics = snapshot["statistics"]["selected_run"]
    assert statistics["workers"]["models"] == {"gpt-5.6-luna": 1}
    assert statistics["workers"]["providers"] == {"openai-codex": 1}
    assert statistics["workers"]["productive_sessions"] == 1
    assert statistics["verifiers"]["process_runs"] == 1
    assert statistics["verifiers"]["worker_process_runs"] == 1
    assert statistics["usage"]["processed_tokens"] == 260
    assert statistics["workspace_operations"]["provider"] == "copy"
    assert (
        statistics["workspace_operations"]["workspace_creation"][
            "attempts_observed"
        ]
        == 2
    )
    assert statistics["timing"]["workspace_creation_candidates_observed"] == 2

    assert snapshot["candidates"][second.candidate_id]["status"] == "created"
    assert snapshot["candidates"][second.candidate_id]["agent_session_count"] == 0
    assert snapshot["candidates"][second.candidate_id]["results_tsv"]["row_count"] == 0
    assert any(warning["kind"] == "candidate_without_agent_session" for warning in snapshot["warnings"])


def test_pi_terminal_finalize_reports_combines_worker_annotator_and_main_usage(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    search = FileSearchRuntime(runtime_root)
    frozen = search.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = search.create_run(frozen.frozen_spec_id)
    plan = search.plan_next(run_id, requested_k=1)
    [candidate] = search.start_batch(run_id, plan.plan_id)
    session = search.start_agent_session(run_id, candidate.candidate_id)
    search.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "pi-rpc",
            "external_id": session.agent_session_id,
            "metadata": {
                "pi_metrics": {
                    "usage_total": {
                        "assistantMessages": 2,
                        "input": 100,
                        "output": 25,
                        "cacheRead": 50,
                        "cacheWrite": 0,
                        "costTotal": 0.18,
                    }
                }
            },
        },
    )
    SearchTools(search).search_run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Produce annotator usage",
    )
    annotation = search.annotation.task(
        run_id, candidate.candidate_id, 1
    )
    assert annotation is not None
    search.annotation.tasks.write_task(
        annotation.model_copy(
            update={
                "attempts": 1,
                "usage": {
                    "input_tokens": 20,
                    "cached_input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 25,
                    "cost_usd": 0.05,
                },
            }
        )
    )

    goals = FileGoalPlusRuntime(runtime_root)
    goal = goals.create_goal("Report complete Goal Plus model usage")
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    goals.set_status(
        goal.goal_plus_id,
        status="abandoned",
        reason="synthetic run intentionally omits promotion",
    )
    search.report(run_id)
    preliminary_html = (runtime_root / "runs" / run_id / "report.html").read_text(
        encoding="utf-8"
    )
    assert "Known processed tokens" in preliminary_html

    finalized = call_pi_tool(
        runtime_root,
        "pi_goal_plus_finalize_reports",
        {
            "goal_plus_id": goal.goal_plus_id,
            "usage": {
                "assistantMessages": 3,
                "toolCalls": 4,
                "input": 40,
                "output": 10,
                "cacheRead": 50,
                "cacheWrite": 0,
                "cost": 0.2,
            },
            "started_at": "2026-01-01T00:00:00Z",
        },
    )

    assert finalized["reports"] == [str(runtime_root / "runs" / run_id / "report.md")]
    assert (runtime_root / "runs" / run_id / "report.html").is_file()
    snapshot = goal_plus_monitor_snapshot(
        root_dir=runtime_root,
        goal_plus_id=goal.goal_plus_id,
    )
    assert snapshot["statistics"]["orchestrator"]["source"] == "pi_goal_stats"
    assert snapshot["statistics"]["annotator_usage"]["processed_tokens"] == 35
    assert snapshot["statistics"]["total_usage"]["processed_tokens"] == 310
    assert snapshot["statistics"]["total_usage"]["cost_usd"] == pytest.approx(0.43)
    html = (runtime_root / "runs" / run_id / "report.html").read_text(
        encoding="utf-8"
    )
    assert "Total processed tokens" in html
    assert "Estimated total cost" in html


def test_goal_plus_monitor_snapshot_does_not_attach_unlinked_latest_run(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    goal_runtime = FileGoalPlusRuntime(runtime_root)
    goal = goal_runtime.create_goal("Analyze a model optimization target")

    search_runtime = FileSearchRuntime(runtime_root)
    frozen = search_runtime.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")
    unrelated_run_id = search_runtime.create_run(frozen.frozen_spec_id)

    snapshot = goal_plus_monitor_snapshot(
        root_dir=runtime_root,
        goal_plus_id=goal.goal_plus_id,
    )

    assert snapshot["goal_plus"]["goal_plus_id"] == goal.goal_plus_id
    assert snapshot["goal_plus"]["goal_revision"] == 1
    assert snapshot["goal_plus"]["goal_revisions_total"] == 1
    assert snapshot["goal_plus"]["final_check_policy"] == {"mode": "disabled"}
    assert snapshot["goal_plus"]["latest_final_check"] is None
    assert snapshot["goal_plus"]["linked_search"] is None
    assert snapshot["run"] is None
    assert snapshot["subagents"] == []
    assert snapshot["candidates"] == {}
    assert not any(warning["kind"] == "inferred_latest_run" for warning in snapshot["warnings"])
    assert unrelated_run_id is not None


def test_goal_plus_monitor_snapshot_keeps_session_and_candidate_verifier_counts_separate(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    first = runtime.start_agent_session(run_id, task.candidate_id)
    second = runtime.redispatch_candidate(run_id, task.candidate_id)
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=second.agent_session_id,
        hypothesis="Count redispatched evidence",
    )

    snapshot = goal_plus_monitor_snapshot(root_dir=runtime_root, run_id=run_id)
    by_session = {
        subagent["agent_session_id"]: subagent for subagent in snapshot["subagents"]
    }

    assert by_session[first.agent_session_id]["verifier_count"] == 0
    assert by_session[first.agent_session_id]["candidate_verifier_count"] == 1
    assert by_session[second.agent_session_id]["verifier_count"] == 1
    assert by_session[second.agent_session_id]["candidate_verifier_count"] == 1


def test_goal_plus_monitor_snapshot_aggregates_multiple_search_tasks(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    goal_runtime = FileGoalPlusRuntime(runtime_root)
    goal = goal_runtime.create_goal("Run two verifier-backed searches")
    search_runtime = FileSearchRuntime(runtime_root)
    frozen = search_runtime.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")

    first_run_id = search_runtime.create_run(frozen.frozen_spec_id)
    search_runtime.plan_next(first_run_id, requested_k=1)
    goal_runtime.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, first_run_id)

    second_run_id = search_runtime.create_run(frozen.frozen_spec_id)
    second_plan = search_runtime.plan_next(second_run_id, requested_k=2)
    search_runtime.start_batch(second_run_id, second_plan.plan_id)
    goal_runtime.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, second_run_id)
    stale = goal_runtime.status(goal.goal_plus_id)
    goal_runtime.state.write_goal(
        stale.model_copy(update={"status": "complete", "next_action": None})
    )

    snapshot = goal_plus_monitor_snapshot(
        root_dir=runtime_root,
        goal_plus_id=goal.goal_plus_id,
    )

    assert snapshot["goal_plus"]["search_tasks_total"] == 2
    assert snapshot["goal_plus"]["current_search_run_id"] == second_run_id
    assert snapshot["selected_run_id"] == second_run_id
    assert [task["run_id"] for task in snapshot["search_tasks"]] == [
        first_run_id,
        second_run_id,
    ]
    assert snapshot["search_tasks"][0]["planning_rounds_total"] == 1
    assert snapshot["search_tasks"][0]["started_rounds_total"] == 0
    assert snapshot["search_tasks"][1]["planning_rounds_total"] == 1
    assert snapshot["search_tasks"][1]["started_rounds_total"] == 1
    assert snapshot["search_tasks"][1]["strategy"] == {
        "name": "random",
        "worker_host": "pi-rpc",
    }
    aggregate = snapshot["search_task_aggregate"]
    assert {
        key: aggregate[key]
        for key in (
            "search_tasks_total",
            "planning_rounds_total",
            "started_rounds_total",
            "candidates_total",
            "candidates_evaluated",
            "worker_sessions_total",
            "verifier_runs_total",
            "estimated_cost_total",
        )
    } == {
        "search_tasks_total": 2,
        "planning_rounds_total": 2,
        "started_rounds_total": 1,
        "candidates_total": 2,
        "candidates_evaluated": 0,
        "worker_sessions_total": 0,
        "verifier_runs_total": 0,
        "estimated_cost_total": 0.0,
    }
    assert aggregate["statistics"]["runs_total"] == 2
    assert aggregate["statistics"]["workers"]["sessions_total"] == 0
    assert (
        aggregate["statistics"]["workspace_operations"]["workspace_creation"][
            "attempts_observed"
        ]
        == 2
    )
    assert snapshot["run"]["run_id"] == second_run_id
    assert snapshot["run"]["planning_rounds_total"] == 1
    assert snapshot["run"]["started_rounds_total"] == 1
    assert any(
        warning["kind"] == "superseded_search_task_not_terminal"
        and warning["run_id"] == first_run_id
        for warning in snapshot["warnings"]
    )
    assert any(
        warning["kind"] == "completed_goal_current_search_not_promoted"
        and warning["run_id"] == second_run_id
        for warning in snapshot["warnings"]
    )


def test_goal_plus_monitor_snapshot_is_exposed_to_mcp_and_pi_facade(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(tools_by_name(mcp))

    assert "goal_plus_monitor_snapshot" in tools
    assert "run_id" in tools["goal_plus_monitor_snapshot"].parameters["properties"]
    assert "goal_plus_id" in tools["goal_plus_monitor_snapshot"].parameters["properties"]
    assert "search_get_agent_observability" in tools

    result = call_pi_tool(
        runtime_root,
        "goal_plus_monitor_snapshot",
        {"run_id": run_id},
    )

    assert result["schema_version"] == 1
    assert result["goal_plus_version"] == __version__
    assert result["run"]["run_id"] == run_id
    assert result["strategy"]["name"] == "random"
    assert result["strategy"]["latest_plan"] is None


def test_goal_plus_monitor_snapshot_projects_host_execution_scope(
    tmp_path: Path,
    scope_owner,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(_pi_rpc_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    scope = ensure_execution_scope(
        root_dir=runtime_root,
        host="pi",
        owner_session_id="monitor-owner",
        owner_pid=os.getpid(),
        run_id=run_id,
        start_guardian=False,
    )
    close_execution_scope(
        root_dir=runtime_root,
        scope_id=scope["scope_id"],
        reason="monitor-test",
    )

    snapshot = goal_plus_monitor_snapshot(root_dir=runtime_root, run_id=run_id)

    assert snapshot["execution_scope"] == {
        "scope_id": scope["scope_id"],
        "host": "pi",
        "owner_session_id": "monitor-owner",
        "state": "closed",
        "active_count": 0,
        "close_reason": "monitor-test",
        "cleanup_errors": [],
        "updated_at": snapshot["execution_scope"]["updated_at"],
    }

    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    observability = call_pi_tool(
        runtime_root,
        "search_get_agent_observability",
        {"agent_session_id": session.agent_session_id},
    )
    assert observability["host"] == "pi-rpc"
