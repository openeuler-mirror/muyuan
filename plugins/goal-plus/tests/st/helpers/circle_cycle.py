"""Bounded native Codex Circle workflow with durable-state acceptance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from goal_plus.host_observability import discover_codex_session_file
from goal_plus.host_scope import close_execution_scope
from goal_plus.models import AgentSessionRecord, GoalPlusRecord
from goal_plus.monitor import goal_plus_monitor_snapshot


ROOT = Path(__file__).resolve().parents[3]


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _initialize_git(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "add", ".")
    _git(path, "-c", "user.name=Goal Plus ST", "-c", "user.email=st@example.invalid",
         "commit", "-qm", "Circle ST baseline")


def prepare_circle(project: Path, model: str, reasoning_effort: str) -> dict[str, str]:
    source = project / "source"
    shutil.copytree(
        ROOT / "tests/st/fixtures/circle_packing",
        source,
        ignore=shutil.ignore_patterns(".tmp"),
    )
    shutil.copytree(
        ROOT / "examples/circle-packing-thinkthread-apply/candidate-hints",
        source / "candidate-hints",
        ignore=shutil.ignore_patterns(".tmp"),
    )
    (source / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    spec = json.loads((source / "spec.json").read_text())
    spec["source_path"] = str(source)
    spec["edit_surface"]["deny"] += ["spec.json", "candidate-hints/**", ".gitignore"]
    spec["workspace"] = {"provider": "git_worktree", "promotion_mode": "artifact_only"}
    spec["strategy"] = {
        "name": "agent_guided", "initial_context": "fresh",
         "inner_agent": "common",
        "models": [{"model": model, "reasoning_effort": reasoning_effort}],
        "evidence_annotator": { "model": model,
                               "reasoning_effort": reasoning_effort, "timeout_seconds": 300},
        "global_evidence_mode": "manual",
    }
    (source / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (source / "initial_program.py", source / "evaluator.py")}
    _initialize_git(source)
    (project / ".gitignore").write_text("source/\n.gp/\n.codex\n.pi\n")
    _initialize_git(project)
    return hashes


def assert_circle_state(
    project: Path, model: str, baseline_hashes: dict[str, str], reasoning_effort: str = "low",
) -> None:
    runtime_root = project / ".gp"
    goals = list((runtime_root / "goal-plus").glob("*/goal.json"))
    assert len(goals) == 1
    goal = GoalPlusRecord.model_validate_json(goals[0].read_text())
    snapshot = goal_plus_monitor_snapshot(runtime_root, goal_plus_id=goal.goal_plus_id)
    assert snapshot["goal_plus"]["status"] == "complete", snapshot["goal_plus"]
    run = snapshot["run"]
    assert run["state"] == "promoted", run
    run_dir = runtime_root / "runs" / run["run_id"]
    assert len(list((runtime_root / "runs").glob("*/run.json"))) == 1
    assert len(list((run_dir / "plans").glob("*.json"))) == 1
    sessions = [AgentSessionRecord.model_validate_json(p.read_text())
                for p in (run_dir / "agent_sessions").glob("*.json")]
    assert len(sessions) == 2
    by_candidate = {session.candidate_id: session for session in sessions}
    assert set(by_candidate) == {"c001", "c002"}
    native_ids = set()
    for candidate_id, expected_sums in (
        ("c001", [2.166664066666667, 2.4389636673796344]),
        ("c002", [2.4389636673796344]),
    ):
        session = by_candidate[candidate_id]
        assert session.session_handle.metadata["transport"] == "app-server"
        dispatches = session.session_handle.metadata["dispatches"]
        assert len(dispatches) == len(expected_sums)
        assert {item["native_session_id"] for item in dispatches} == {session.session_handle.external_id}
        assert len({item["invocation_id"] for item in dispatches}) == len(expected_sums)
        assert all(item["state"] == "completed" for item in dispatches)
        record = json.loads((run_dir / "candidates" / candidate_id / "candidate.json").read_text())
        iterations = record["iterations"]
        # Parent completion verification may add evidence; require the actual worker turns.
        worker_iterations = [item for item in iterations
                             if item.get("agent_session_id") == by_candidate[candidate_id].agent_session_id]
        assert [item["score"] for item in worker_iterations] == pytest.approx(
            [value / 2.635 for value in expected_sums]
        ), worker_iterations
        assert len({item["git_head"] for item in worker_iterations}) == len(expected_sums)
        for item in iterations:
            assert item["process_passed"] and not item["touched_denied_files"]
            assert not item["changed_outside_allowed"]
            assert item["git_head"]
            annotation = json.loads((run_dir / "candidates" / candidate_id / "evidence-annotations"
                                     / f"iteration-{item['iteration']:04d}.json").read_text())
            assert annotation["state"] == "completed", annotation
            assert annotation["attempt_commit"] == item["git_head"]
        transcript = discover_codex_session_file(by_candidate[candidate_id])
        assert transcript is not None and transcript.is_file()
        entries = [json.loads(line) for line in transcript.read_text().splitlines() if line.strip()]
        contexts = [entry["payload"] for entry in entries if entry.get("type") == "turn_context"]
        assert contexts and {entry["model"] for entry in contexts} == {model}, contexts
        assert {entry["effort"] for entry in contexts} == {reasoning_effort}
        native_ids.add(transcript)
    assert len(native_ids) == 2
    assert run["selected_score"] == pytest.approx(2.4389636673796344 / 2.635)
    assert run["promotion"]["promotion_mode"] == "artifact_only"
    assert run["promotion"]["state"] == "prepared"
    assert run["promotion"]["applied_at"] is None
    assert (run_dir / "report.md").is_file() and (run_dir / "report.html").is_file()
    scope = snapshot["execution_scope"]
    assert scope is not None and scope["state"] == "closed", scope
    assert scope["active_count"] == 0, scope
    for name, expected in baseline_hashes.items():
        assert hashlib.sha256((project / "source" / name).read_bytes()).hexdigest() == expected
    assert _git(project / "source", "status", "--porcelain") == ""


def run_circle_cycle(runner, project: Path) -> None:
    model = runner.model
    baseline_hashes = prepare_circle(project, model, runner.reasoning_effort or "low")
    prompt = (ROOT / "tests/st/prompts/codex_circle_packing_cycle.md").read_text()
    try:
        result = runner.run_streaming(prompt, scenario="codex_circle_packing_cycle", timeout=1500)
        assert not result.timed_out and result.returncode == 0, result.log_path
        assert (runner.log_dir / "codex_circle_packing_cycle.final.md").read_text().strip()
        assert_circle_state(project, model, baseline_hashes, runner.reasoning_effort or "low")
    finally:
        root = project / ".gp"
        cleanup = [close_execution_scope(root_dir=root, scope_id=path.parent.name, reason="Circle ST finished")
                   for path in (root / "host-scopes").glob("scope_*/scope.json")]
        (runner.log_dir / "cleanup.json").write_text(json.dumps(cleanup, indent=2))
