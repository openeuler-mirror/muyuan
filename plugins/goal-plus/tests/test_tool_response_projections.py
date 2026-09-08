"""Hot-path replies preserve actionable facts without replaying durable history."""

import asyncio
import json

from fastmcp import Client
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.pi_tool import call_pi_tool
from goal_plus.projections.goal_tools import reconciliation_summary
from goal_plus.projections.monitor_tools import monitor_tool_summary
from goal_plus.projections.pool_tools import pool_tool_summary
from goal_plus.server import create_mcp
from goal_plus.tools import GoalPlusTools
from tests._runtime_helpers import start_host_verified_goal


def test_goal_status_does_not_grow_with_gate_audit_and_context_is_explicit(tmp_path):
    runtime = FileGoalPlusRuntime(tmp_path)
    record = runtime.create_goal("full objective " * 1000)
    tools = GoalPlusTools(runtime)
    before = tools.goal_plus_status(record.goal_plus_id)
    for _ in range(50):
        runtime.gate(record.goal_plus_id, "pre_tool_use", {"tool_name": "read"})
    assert tools.goal_plus_status(record.goal_plus_id) == before
    assert "raw_goal" not in before and "evidence_log" not in before
    assert tools.goal_plus_status(record.goal_plus_id, "context")["raw_goal"] == record.raw_goal
    events = runtime.list_events(record.goal_plus_id)
    page = tools.goal_plus_history(record.goal_plus_id, "events", limit=20)
    assert page["items"] == events[:20]
    assert page["next_offset"] == 20
    assert tools.goal_plus_history(record.goal_plus_id, "events", offset=40)["items"] == events[40:]
    assert tools.goal_plus_history(record.goal_plus_id, "events", offset=40)["next_offset"] is None
    assert tools.goal_plus_history(record.goal_plus_id, "goal_revisions")["items"][0]["raw_goal"] == record.raw_goal
    with pytest.raises(ValueError):
        tools.goal_plus_history(record.goal_plus_id, "events", limit=101)


def test_work_receipt_preserves_attempt_and_ready_result_until_accepted(tmp_path):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path)
    runtime = FileGoalPlusRuntime(root)
    tools = GoalPlusTools(runtime)
    triage = tools.goal_plus_record_triage(goal.goal_plus_id, {
        "is_optimization": False, "confidence": "high", "recommended_phase": "goal",
    })
    assert "raw_goal" not in triage
    dispatched = tools.goal_plus_record_work_event(goal.goal_plus_id, "work", "dispatch", "implement", host="codex")
    item = dispatched["work_item"]
    identity = {"attempt_id": item["attempt_id"], "generation": item["generation"]}
    assert item["status"] == "launching" and identity["attempt_id"]
    tools.goal_plus_record_work_event(goal.goal_plus_id, "work", "bind", "bound", agent_id="child", **identity)
    ready = tools.goal_plus_record_work_event(goal.goal_plus_id, "work", "result", "verified result",
                                            evidence=[{"result": "detailed evidence"}], **identity)
    assert ready["work_item"]["result_summary"] == "verified result"
    assert tools.goal_plus_status(goal.goal_plus_id)["work_items"][0]["status"] == "result_ready"
    assert "evidence" not in ready["work_item"]
    history = tools.goal_plus_history(goal.goal_plus_id, "work_items")["items"][0]
    assert history["evidence"]
    tools.goal_plus_record_work_event(goal.goal_plus_id, "work", "accepted", "reviewed", **identity)
    assert tools.goal_plus_status(goal.goal_plus_id)["work_items"] == []
    assert tools.goal_plus_history(goal.goal_plus_id, "work_items")["items"][0]["status"] == "accepted"


def test_recovery_summary_preserves_action_identity_and_errors():
    summary = reconciliation_summary({"resume_eligible": True, "runs": [{
        "run_id": "run_1", "state": "waiting_for_workers", "candidates": [{
            "candidate_id": "c001", "generation": 4, "next_step": "start_fresh_worker",
            "recovery": {"recovery_id": "recovery_4", "checkpoint": "large"},
            "checkpoint_error": None, "workspace_inspection_error": "unavailable",
            "checkpoint": {"history": "large"}, "sessions": [{"history": "large"}],
        }],
    }]})
    candidate = summary["runs"][0]["candidates"][0]
    assert candidate == {"candidate_id": "c001", "generation": 4,
                         "next_step": "start_fresh_worker", "recovery_id": "recovery_4",
                         "workspace_inspection_error": "unavailable"}


def test_pool_replies_keep_outcomes_and_pending_jobs_without_launch_history():
    result = {"ok": True, "agent_session_id": "agent_1", "launch": {"prompt": "large"},
              "bound_session": {"evidence_reads": ["large"]}, "steps": ["large"],
              "final_score_report": {"aggregate_score": 2.4, "best_artifact_ref": {"id": "exact"}},
              "lease": {"satisfied": True, "release_reason": "allocation_decision", "dispatch_count": 2}}
    jobs = [{"job_id": f"job_{n}", "candidate_id": "c001", "status": "completed",
             "delivered_at": "now", "result": result} for n in range(50)]
    jobs[0]["delivered_at"] = None
    snapshot = pool_tool_summary({"pool_id": "pool_1", "state": "closed", "jobs": jobs})
    assert [j["job_id"] for j in snapshot["jobs"]] == ["job_0", "job_49"]
    assert snapshot["jobs_total"] == 50
    reply = pool_tool_summary({"events": [{"event_id": "event_1", "result": result}]})
    event = reply["events"][0]
    assert event["result"]["final_score_report"] == result["final_score_report"]
    assert event["result"]["lease"] == result["lease"]
    assert "launch" not in event["result"] and "steps" not in event["result"]
    assert "launch" in result


def test_monitor_keeps_annotation_barrier_and_cleanup_errors():
    snapshot = {"ok": True, "run": {"run_id": "run_1", "state": "selection_blocked",
        "evidence_annotations": {"tasks": 3, "states": {"pending": 1, "completed": 2},
                                 "recent_attempts": ["large"]}},
        "execution_scope": {"state": "closing", "active_count": 1, "cleanup_errors": ["busy"]},
        "statistics": {"history": "large"}, "warnings": [{"kind": "worker_failed"}]}
    reply = monitor_tool_summary(snapshot)
    assert reply["run"]["evidence_annotations"]["states"]["pending"] == 1
    assert reply["execution_scope"]["cleanup_errors"] == ["busy"]
    assert reply["warnings"] == snapshot["warnings"]
    assert "recent_attempts" not in reply["run"]["evidence_annotations"]
    assert "statistics" not in reply


def test_mcp_and_pi_expose_same_summary_context_and_paged_history(tmp_path):
    runtime = FileGoalPlusRuntime(tmp_path)
    goal = runtime.create_goal("exact user objective")

    async def check():
        async with Client(create_mcp(tmp_path)) as client:
            for tool, arguments in [
                ("goal_plus_status", {"goal_plus_id": goal.goal_plus_id}),
                ("goal_plus_status", {"goal_plus_id": goal.goal_plus_id, "view": "context"}),
                ("goal_plus_history", {"goal_plus_id": goal.goal_plus_id, "collection": "goal_revisions", "limit": 1}),
            ]:
                result = await client.call_tool(tool, arguments)
                assert json.loads(result.content[0].text) == call_pi_tool(tmp_path, tool, arguments)

    asyncio.run(check())
