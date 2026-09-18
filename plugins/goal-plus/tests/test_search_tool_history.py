from copy import deepcopy
from unittest.mock import Mock

import pytest

from goal_plus.tools import SearchTools


@pytest.fixture
def history():
    return {
        "run_id": "run_1", "state": "running", "objective": "test runtime",
        "metric_name": "score",
        "metric_direction": "maximize", "best_candidate_id": "c001", "best_score": 0.0,
        "total_candidates": 2, "returned_candidates": 1, "top_n": 1, "sort_by": "score",
        "worker_policy": {"session_control": {"wake_tool": "goal_plus_session_run"}},
        "inherited_research": {},
        "candidates": [{
            "candidate_id": "c001", "status": "evaluated", "intent": "baseline",
            "score": 0.0, "process_passed": True, "best_iteration": 1,
            "evidence_source": "best_iteration", "changed_files": ["program.py"],
            "best_git_head": "commit-1", "touched_denied_files": False,
            "changed_outside_allowed": False, "latest_score": 0.0,
            "latest_process_passed": True, "latest_disposition": "keep",
            "latest_failure_classes": [], "summary": "", "blockers": [],
            "agent_sessions": [{"session_handle": {"metadata": {"dispatches": []}}}],
            "strategy_metadata": {"worker_policy": {"inner_agent": "common"}},
            "key_metrics": {"score": 0.0, "request_id": "provider-request"},
            "log_paths": ["/private/log"], "latest_log_paths": ["/private/log"],
            "key_results": [], "research_summary": {"summary": "", "key_results": []},
        }],
        "feature_ledger": [], "pitfalls": [], "verifier_assessments": [],
        "research_rollup": {"feature_ledger": [], "pitfalls": [], "verifier_assessments": []},
    }


def test_history_default_is_stable_as_host_diagnostics_grow_and_detail_is_complete(history):
    runtime = Mock()
    runtime.list_history.return_value = history
    tools = SearchTools(runtime)
    before = deepcopy(history)

    summary = tools.goal_plus_search_list_history("run_1", top_n=1)
    assert summary["best_score"] == 0.0
    assert summary["objective"] == "test runtime"
    assert summary["candidates"] == [{
        "candidate_id": "c001", "status": "evaluated", "intent": "baseline",
        "score": 0.0, "process_passed": True, "evidence_source": "best_iteration",
        "best_iteration": 1, "changed_files": ["program.py"], "best_git_head": "commit-1",
        "key_metrics": {"score": 0.0, "request_id": "provider-request"},
    }]
    assert "research_rollup" not in summary
    assert "worker_policy" not in summary
    assert tools.goal_plus_search_list_history("run_1", top_n=1, detail=True) == before
    assert history == before

    history["candidates"][0]["agent_sessions"][0]["session_handle"]["metadata"]["dispatches"] = [
        {"invocation_id": str(index), "usage": {"tokens": index}} for index in range(100)
    ]
    assert tools.goal_plus_search_list_history("run_1", top_n=1) == summary
    assert tools.goal_plus_search_list_history("run_1", top_n=1, detail=True) == history
    runtime.list_history.assert_called_with("run_1", top_n=1, sort_by="score")


@pytest.mark.parametrize("changes", [
    {"latest_score": -1.0},
    {"latest_score": None, "latest_process_passed": False,
     "latest_failure_classes": ["VerifierTimeout"]},
    {"latest_disposition": "discard"},
    {"latest_failure_classes": ["EditSurfaceViolation"], "changed_outside_allowed": True},
])
def test_history_retains_best_and_reports_latest_anomalies(history, changes):
    history["candidates"][0].update(changes)
    runtime = Mock()
    runtime.list_history.return_value = history
    candidate = SearchTools(runtime).goal_plus_search_list_history("run_1")["candidates"][0]
    assert candidate["score"] == 0.0
    assert candidate["best_iteration"] == 1
    assert candidate["process_passed"] is True
    for key, value in changes.items():
        if key.startswith("latest_"):
            assert candidate["latest"][key.removeprefix("latest_")] == value
        else:
            assert candidate[key] == value


def test_history_does_not_convert_missing_evidence_to_zero_or_success(history):
    history.update(best_score=None, best_candidate_id=None)
    history["candidates"][0].update(
        status="created", score=None, process_passed=None, best_iteration=None,
        best_git_head=None, latest_score=None, latest_process_passed=None, latest_disposition=None,
    )
    runtime = Mock()
    runtime.list_history.return_value = history
    summary = SearchTools(runtime).goal_plus_search_list_history("run_1")
    assert summary["best_score"] is None
    assert summary["best_candidate_id"] is None
    candidate = summary["candidates"][0]
    assert candidate["score"] is None
    assert candidate["process_passed"] is None
    assert candidate["best_iteration"] is None
    assert "latest" not in candidate


def test_history_keeps_research_outside_frontier_and_invalidation_summary(history):
    feature = {"candidate_id": "c002", "candidate_visible": False, "conclusion": "use caching"}
    concern = {"candidate_id": "c002", "status": "concern", "evidence": ["missing case"]}
    history.update(
        state="aborted", source_run_id="previous", inherited_research={"frontier": ["previous-best"]},
        invalidation_reason="verifier_coverage_inadequate", invalidation_summary="missing case",
        invalidation_evidence=[{"case": "edge"}], replacement_run_id="next",
        feature_ledger=[feature], verifier_assessments=[concern],
        research_rollup={"feature_ledger": [feature], "pitfalls": [], "verifier_assessments": [concern]},
    )
    history["candidates"][0].update(summary="tried baseline", next_ideas=["try caching"], blockers=["missing case"])
    before = deepcopy(history)
    runtime = Mock()
    runtime.list_history.return_value = history
    tools = SearchTools(runtime)
    summary = tools.goal_plus_search_list_history("run_1", top_n=1)
    assert summary["research_rollup"] == {"feature_ledger": [feature], "verifier_assessments": [concern]}
    assert "feature_ledger" not in summary
    assert summary["inherited_research_available"] is True
    assert summary["invalidation_reason"] == "verifier_coverage_inadequate"
    assert summary["invalidation_summary"] == "missing case"
    assert summary["replacement_run_id"] == "next"
    assert summary["candidates"][0]["blockers"] == ["missing case"]
    assert summary["candidates"][0]["next_ideas"] == ["try caching"]
    assert tools.goal_plus_search_list_history("run_1", detail=True) == before
    assert history == before
