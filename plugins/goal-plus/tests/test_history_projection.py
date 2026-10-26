"""History projection shape: static config and duplicated evidence stay bounded."""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from goal_plus.domain.evidence import verifier_feedback_projection
from tests._runtime_helpers import make_project, spec_for


def _runtime_with_candidate(
    tmp_path: Path,
) -> tuple[FileSearchRuntime, str, str, Path]:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_for(project).model_dump(mode="json")),
        [project / "evaluator.py"],
        agent_harness="codex",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    return runtime, run_id, task.candidate_id, task.workspace


def _fake_verifier(program: Path, scores: dict[str, float]):
    def fake_verify(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        value = program.read_text(encoding="utf-8").split("'", 2)[1]
        score = scores.get(value)
        if score is None:
            return subprocess.CompletedProcess(command, 1, "", "invalid candidate")
        return subprocess.CompletedProcess(
            command,
            0,
            f'{{"combined_score": {score}}}\n',
            "",
        )

    return fake_verify


def test_detail_history_carries_static_config_once_and_latest_evidence_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, candidate_id, workspace = _runtime_with_candidate(tmp_path)
    program = workspace / "initial_program.py"
    monkeypatch.setattr(
        runtime.verifiers,
        "execute_verifier_process",
        _fake_verifier(program, {"seed": 1.0}),
    )
    session = runtime.start_agent_session(
        run_id, candidate_id, {"goal": "document session"}
    )
    program.write_text("VALUE = 'seed'\n", encoding="utf-8")
    runtime.run_verifier(run_id, candidate_id, hypothesis="seed")

    detail = runtime.list_history(run_id)
    candidate = detail["candidates"][0]

    assert detail["worker_policy"]
    assert "worker_policy" not in candidate["strategy_metadata"]
    assert candidate["strategy_metadata"]["plan_id"]

    assert candidate["latest_verifiers"][0]["passed"] is True
    for key in ("verifiers", "log_paths", "failure_classes"):
        assert key not in candidate

    for key in (
        "summary", "key_results", "feature_ledger", "next_ideas", "risk_notes",
        "blockers", "verifier_assessment", "research_summary",
    ):
        assert key not in candidate

    session_entry = candidate["agent_sessions"][0]
    assert session_entry["agent_session_id"] == session.agent_session_id
    assert "session_handle" not in session_entry

    program.write_text("VALUE = 'broken'\n", encoding="utf-8")
    runtime.run_verifier(run_id, candidate_id, hypothesis="broken")
    candidate = runtime.list_history(run_id)["candidates"][0]
    assert candidate["best_iteration"] == 1
    assert candidate["score"] == 1.0
    assert candidate["log_paths"]
    assert candidate["latest_failure_classes"]
    assert "verifiers" not in candidate


@pytest.mark.parametrize(("tail", "covered"), [
    ('{"ok":false,"values":[0,null,{"reason":"rejected"}]}', True),
    ('{"ok":0}', False),
    ('{"values":[0.0,null,{"reason":"rejected"}]}', False),
    ('{"ok":true}', False),
    ('{"extra":"diagnosis"}', False),
    ('diagnosis\n{"ok":false}', False),
    ('{"ok":false', False),
    ('{"ok":true,"ok":false}', False),
    ('{"value":NaN}', False),
    ('{"value":1e400}', False),
    ('[false]', False),
])
def test_feedback_preserves_uncertain_log_information(tail, covered):
    metrics = {"ok": False, "values": [0, None, {"reason": "rejected"}], "stdout_tail": tail}
    payload = {"verifier_results": [{"metrics": metrics, "log_path": "/logs/exact.log"}]}
    before = deepcopy(payload)
    projected = verifier_feedback_projection(payload)
    result = projected["verifier_results"][0]
    assert ("stdout_tail" not in result["metrics"]) is covered
    assert result["metrics"]["ok"] is False
    assert result["metrics"]["values"] == [0, None, {"reason": "rejected"}]
    assert result["log_path"] == "/logs/exact.log"
    assert payload == before


def test_feedback_keeps_overflow_json_when_metric_is_nonfinite():
    payload = {
        "verifier_results": [{
            "metrics": {"value": float("inf"), "stdout_tail": '{"value":1e400}'},
        }],
    }
    projected = verifier_feedback_projection(payload)
    assert projected["verifier_results"][0]["metrics"]["stdout_tail"] == '{"value":1e400}'


def test_verifier_reply_history_and_queries_deduplicate_without_rewriting_evidence(tmp_path, monkeypatch):
    runtime, run_id, candidate_id, _ = _runtime_with_candidate(tmp_path)
    session = runtime.start_agent_session(run_id, candidate_id)
    stdout = '{"combined_score":2.0,"ok":false,"cases":[0,null]}\n'
    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 1, stdout, "extra diagnosis"))
    tools = SearchTools(runtime)
    reply = tools.goal_plus_search_run_verifier(
        run_id, candidate_id, agent_session_id=session.agent_session_id,
        hypothesis="measure with structured diagnostics",
    )
    metrics = reply["verifier_results"][0]["metrics"]
    assert "stdout_tail" not in metrics
    assert metrics["combined_score"] == 2.0
    assert metrics["ok"] is False
    assert metrics["cases"] == [0, None]
    assert metrics["stderr_tail"] == "extra diagnosis"
    assert not any(key.startswith("shared_tool_") for key in reply)
    assert tools.goal_plus_search_run_verifier(
        run_id, candidate_id, agent_session_id=session.agent_session_id,
        attempt_id=reply["attempt_id"],
    ) == reply
    stored = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    assert stored.score_report.verifier_results[0].metrics["stdout_tail"] == stdout.strip()
    assert "stdout_tail" in stored.iterations[0].metrics["score"]
    evidence, = tools.goal_plus_search_get_global_evidence(session.agent_session_id)
    assert "stdout_tail" not in evidence["metrics"]["score"]
    iteration, = tools.goal_plus_search_list_iterations(session.agent_session_id)["items"]
    assert iteration["settlement_receipt"] == stored.iterations[0].settlement_receipt.model_dump(mode="json")
    if "metrics" in iteration:
        assert "stdout_tail" not in iteration["metrics"]["score"]
    assert runtime.candidate_creation.state.read_candidate(run_id, candidate_id) == stored


def test_summary_history_includes_objective_and_namespaced_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, candidate_id, workspace = _runtime_with_candidate(tmp_path)
    program = workspace / "initial_program.py"
    monkeypatch.setattr(
        runtime.verifiers,
        "execute_verifier_process",
        _fake_verifier(program, {"seed": 2.0}),
    )
    program.write_text("VALUE = 'seed'\n", encoding="utf-8")
    runtime.run_verifier(run_id, candidate_id, hypothesis="seed")

    summary = SearchTools(runtime).goal_plus_search_list_history(run_id)
    assert summary["frozen_spec_id"] == runtime.search_run.state.read_run(run_id).frozen_spec_id
    assert summary["objective"] == "test runtime"
    assert summary["best_score"] == 2.0
    row = summary["candidates"][0]
    assert row["metrics_omitted"] is True
    assert row["touched_denied_files"] is False
    assert row["changed_outside_allowed"] is False
    assert row["metrics"]["score"]["combined_score"] == 2.0
    assert "key_metrics" not in row
    assert "strategy_metadata" not in row
    assert "worker_policy" not in summary
    assert "research_rollup" not in summary


@pytest.mark.parametrize("with_omissions", [False, True])
def test_history_report_fallback_projects_every_verifier_metric(tmp_path, monkeypatch, with_omissions):
    runtime, run_id, candidate_id, workspace = _runtime_with_candidate(tmp_path)
    program = workspace / "initial_program.py"
    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", _fake_verifier(program, {"seed": 0.0}))
    program.write_text("VALUE = 'seed'\n", encoding="utf-8")
    runtime.run_verifier(run_id, candidate_id, hypothesis="seed")
    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    ranking = record.score_report.verifier_results[0]
    results = [
        ranking.model_copy(update={"name": "score", "metrics": {
            "combined_score": 0.0, "ok": False, **({"nan": float("nan")} if with_omissions else {}),
        }}),
        ranking.model_copy(update={"name": "coverage", "metrics": {"_coverage": 0, "status": "measured"}}),
    ]
    if with_omissions:
        results.append(ranking.model_copy(update={"name": "_candidate_attempt", "metrics": {"id": "private-attempt"}}))
    record = record.model_copy(update={
        "iterations": [], "score_report": record.score_report.model_copy(update={"verifier_results": results}),
    })
    payload = runtime.research.history_candidate_payload(record)
    assert payload["metrics"] == {
        "score": {"combined_score": 0.0, "ok": False},
        "coverage": {"_coverage": 0, "status": "measured"},
    }
    assert payload["key_metrics"] == {"combined_score": 0.0, "ok": False, "_coverage": 0, "status": "measured"}
    assert payload["metrics_omitted"] is with_omissions
    from goal_plus.projections.search_tools import search_history_summary
    summary = search_history_summary({"candidates": [payload]})
    assert summary["candidates"][0]["metrics_omitted"] is with_omissions


@pytest.fixture
def versioned_history(tmp_path):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from tests._runtime_helpers import start_host_verified_goal

    project = make_project(tmp_path)
    (project / "evaluator.py").write_text("print('{\"combined_score\": 3.0}')\n")
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileSearchRuntime(root)
    spec = spec_for(project, max_parallel=2)
    spec.strategy.inner_agent = "common"
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    tasks = runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    sessions = [runtime.start_agent_session(run_id, task.candidate_id) for task in tasks]
    for task, session in zip(tasks, sessions, strict=True):
        runtime.run_verifier(run_id, task.candidate_id, agent_session_id=session.agent_session_id, hypothesis="initial measure")
    original = runtime.candidate_creation.state.read_candidate(run_id, tasks[0].candidate_id)
    revised = runtime.revise_candidate_spec(
        sessions[0].agent_session_id, "read-version-2", frozen.frozen_spec_id, "Minimize a revised measure", {
            "metric_name": "revised_score", "metric_direction": "minimize",
            "process_verifiers": [{"name": "revised", "role": "ranking_signal", "command": [
                "python", "-c", "import json, initial_program; print(json.dumps({'revised_score': initial_program.VALUE}))",
            ]}],
        },
    )
    for value in [0.25, 0.5]:
        (tasks[0].workspace / "initial_program.py").write_text(f"VALUE = {value}\n")
        assert runtime.run_verifier(run_id, tasks[0].candidate_id, agent_session_id=sessions[0].agent_session_id, hypothesis=f"revised measure {value}").process_passed
    return runtime, run_id, tasks, sessions, frozen, revised, original


@pytest.mark.parametrize("view", ["context", "history", "evidence", "current_process"])
def test_read_views_bind_actual_evaluation_version(versioned_history, view):
    runtime, run_id, tasks, sessions, original, revised, before = versioned_history
    first = tasks[0].candidate_id
    if view == "context":
        context = runtime.get_agent_context(sessions[0].agent_session_id)
        assert (context["metric_name"], context["metric_direction"]) == ("revised_score", "minimize")
        assert (context["frozen_spec_id"], context["spec_hash"]) == (revised.frozen_spec_id, revised.spec_hash)
        assert context["best_iteration"]["iteration"] == 2
        assert [row["frozen_spec_id"] for row in context["recent_iterations"]] == [
            original.frozen_spec_id, revised.frozen_spec_id, revised.frozen_spec_id,
        ]
    elif view == "history":
        history = runtime.list_history(run_id, sort_by="score")
        assert [row["candidate_id"] for row in history["candidates"]] == [task.candidate_id for task in tasks]
        assert history["score_comparable"] is False
        assert history["effective_sort_by"] == "created"
        row = history["candidates"][0]
        assert row["score"] == 0.25 and row["best_iteration"] == 2
        assert (row["frozen_spec_id"], row["spec_hash"]) == (revised.frozen_spec_id, revised.spec_hash)
        assert row["metric_name"] == "revised_score" and row["metric_direction"] == "minimize"
    elif view == "evidence":
        entries = [row for row in runtime.get_global_evidence(sessions[0].agent_session_id) if row["candidate_id"] == first]
        assert [row["frozen_spec_id"] for row in entries] == [
            original.frozen_spec_id, revised.frozen_spec_id, revised.frozen_spec_id,
        ]
        assert [row["spec_hash"] for row in entries] == [original.spec_hash, revised.spec_hash, revised.spec_hash]
        assert [row["metric_name"] for row in entries] == ["combined_score", "revised_score", "revised_score"]
    else:
        evidence = runtime.current_process_evidence(run_id, first)
        assert evidence["aggregate_score"] == 0.25 and evidence["best_iteration"] == 2
        assert (evidence["frozen_spec_id"], evidence["spec_hash"]) == (revised.frozen_spec_id, revised.spec_hash)
    assert runtime.candidate_creation.state.read_candidate(run_id, first).iterations[:1] == before.iterations
