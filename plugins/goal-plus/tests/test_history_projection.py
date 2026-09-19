"""History projection shape: static config and duplicated evidence stay bounded."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
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


def test_summary_history_includes_objective_and_key_metrics(
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
    assert summary["objective"] == "test runtime"
    assert summary["best_score"] == 2.0
    row = summary["candidates"][0]
    assert row["key_metrics"]["combined_score"] == 2.0
    assert "strategy_metadata" not in row
    assert "worker_policy" not in summary
    assert "research_rollup" not in summary
