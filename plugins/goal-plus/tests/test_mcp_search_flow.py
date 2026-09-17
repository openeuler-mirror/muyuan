"""Shared MCP business flow with controlled edits and the real frozen verifier."""

from pathlib import Path

import pytest

from goal_plus.application.candidate_delivery import CandidateDelivery
from goal_plus.candidate_host import candidate_host_queries
from goal_plus.mcp_client import call_mcp_tool, McpSessionToolBridge
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


pytestmark = pytest.mark.integration


def test_two_candidates_continue_verify_select_and_publish_through_shared_mcp(tmp_path):
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\nfrom pathlib import Path\nvalues = {}\n"
        "exec(Path('initial_program.py').read_text(), values)\n"
        "print(json.dumps({'combined_score': float(values['VALUE'])}))\n"
    )
    source_before = {path.name: path.read_bytes() for path in project.iterdir()}
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)

    def main(name, **arguments):
        return call_mcp_tool(root, name, arguments, caller_role="main")

    spec = spec_for(project, max_parallel=2).model_dump(mode="json", exclude_defaults=True)
    spec["strategy"]["inner_agent"] = "common"
    spec["promotion_verifiers"] = [{
        "name": "independent_score", "role": "promotion_gate",
        "command": ["python", "evaluator.py"], "timeout_seconds": 30,
    }]
    frozen = main("goal_plus_search_freeze_spec", goal_plus_id=goal.goal_plus_id, spec=spec,
                  verifier_artifact_paths=["evaluator.py"])
    run_id = main("goal_plus_search_create", goal_plus_id=goal.goal_plus_id,
                  frozen_spec_id=frozen["frozen_spec_id"], request_id="initial")["run_id"]
    plan = main("goal_plus_search_plan_next", run_id=run_id, requested_k=2)
    tasks = main("goal_plus_search_start_batch", run_id=run_id, plan_id=plan["plan_id"])
    runtime = FileSearchRuntime(root)
    delivery = CandidateDelivery(runtime.queries, candidate_host_queries(root))
    from goal_plus.projections.session_tools import session_receipt
    sessions = [session_receipt(runtime.start_agent_session(run_id, task["candidate_id"]))
                for task in tasks]
    assert all("launch" not in session for session in sessions)
    assert all(candidate_host_queries(root).session(session["agent_session_id"]).launch
               for session in sessions)
    bridges = [McpSessionToolBridge(root, caller_role="candidate", caller_agent_session_id=session["agent_session_id"])
               for session in sessions]
    for session in sessions:
        runtime.candidate_sessions.bind_agent_handle(session["agent_session_id"], {
            "agent_harness": session["agent_harness"], "external_id": "controlled-" + session["agent_session_id"],
        })

    for iteration in (1, 2):
        for lane, (task, session, bridge) in enumerate(zip(tasks, sessions, bridges)):
            candidate_id, session_id = task["candidate_id"], session["agent_session_id"]
            if iteration == 2:
                continued = runtime.continue_agent_session(session_id)
                assert continued.agent_session_id == session_id
            context = bridge.call("goal_plus_search_get_agent_context", {"agent_session_id": session_id})
            assert context["candidate_id"] == candidate_id
            score = iteration + lane * 10
            (Path(context["candidate_task"]["workspace"]) / "initial_program.py").write_text(f"VALUE = {score}\n")
            if lane == 0:
                report = bridge.call("goal_plus_search_run_verifier", {
                    "run_id": run_id, "candidate_id": candidate_id, "agent_session_id": session_id,
                    "hypothesis": "raise VALUE",
                })
                assert delivery.review(run_id, candidate_id).action == "reuse"
            else:
                assert delivery.review(run_id, candidate_id).action == "verify"
                report = main("goal_plus_search_run_verifier", run_id=run_id, candidate_id=candidate_id)
            assert report["aggregate_score"] == score
            records = runtime.list_iterations(run_id, candidate_id)
            assert len(records) == iteration
            assert records[-1]["agent_session_id"] == (session_id if lane == 0 else None)

    before_history = runtime.list_history(run_id, top_n=1)
    history = main("goal_plus_search_list_history", run_id=run_id, top_n=1)
    assert history["returned_candidates"] == 1
    assert history["total_candidates"] == 2
    assert history["candidates"][0]["score"] == 12.0
    assert history["candidates"][0]["candidate_id"] == tasks[1]["candidate_id"]
    assert "agent_sessions" not in history["candidates"][0]
    detailed_history = main("goal_plus_search_list_history", run_id=run_id, top_n=1, detail=True)
    assert detailed_history == before_history
    assert detailed_history["candidates"][0]["agent_sessions"]
    assert runtime.list_history(run_id, top_n=1) == before_history

    selected = main("goal_plus_search_select", run_id=run_id)
    assert selected["selected_candidate_id"] == tasks[1]["candidate_id"]
    published = main("goal_plus_search_promote", run_id=run_id, candidate_id=selected["selected_candidate_id"])
    assert Path(published["artifact_path"]).is_file()
    record = runtime.candidate_creation.state.read_candidate(run_id, selected["selected_candidate_id"])
    assert record.promotion_report.aggregate_score == 12.0
    assert record.promotion_report.promotion_passed
    assert record.promotion_evidence.artifact_ref == record.score_report.best_artifact_ref
    assert record.promotion_evidence.selected_artifact_ref == record.score_report.best_artifact_ref
    assert {path.name: path.read_bytes() for path in project.iterdir()} == source_before
