from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.artifacts.search_source import discover_git_tracked_task_skills


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "final-check-skill-guard"
MATERIALIZER = EXAMPLE / "prepare_workspace.py"
TASK_SKILL = ".agents/skills/preserve-score-semantics/SKILL.md"
pytestmark = pytest.mark.example


def _load_materializer():
    spec = importlib.util.spec_from_file_location(
        "final_check_skill_guard_materializer",
        MATERIALIZER,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(workspace: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=workspace,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _materialize(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    assert _load_materializer().materialize(workspace) == workspace.resolve()
    return workspace


def test_materialized_guard_separates_ranking_from_semantic_acceptance(
    tmp_path: Path,
) -> None:
    workspace = _materialize(tmp_path)
    ranking = _run(
        workspace,
        sys.executable,
        ".goal-plus-verifiers/score_throughput.py",
    )
    guard = _run(
        workspace,
        sys.executable,
        ".agents/skills/preserve-score-semantics/scripts/check_score_semantics.py",
    )

    assert ranking.returncode == 0, ranking.stderr
    ranking_result = json.loads(ranking.stdout)
    assert ranking_result["valid"] is True
    assert ranking_result["calls_per_second"] > 0
    assert guard.returncode == 1, guard.stderr
    guard_result = json.loads(guard.stdout)
    assert guard_result["valid"] is False
    assert "stale_result_after_input_mutation" in guard_result["violations"]
    assert "input_object_identity_shortcut" in guard_result["violations"]
    assert discover_git_tracked_task_skills(workspace) == [TASK_SKILL]
    assert _run(workspace, "git", "status", "--porcelain").stdout == ""

    spec_data = json.loads(
        (workspace / "search-spec.json").read_text(encoding="utf-8")
    )
    spec_data["source_path"] = str(workspace)
    frozen = FileSearchRuntime(tmp_path / ".gp").freeze_spec(
        SearchSpec.model_validate(spec_data),
        [workspace / ".goal-plus-verifiers" / "score_throughput.py"], native_host="codex",
    )
    assert frozen.task_skill_paths == [TASK_SKILL]
    with pytest.raises(FileExistsError, match="choose a new --output path"):
        _load_materializer().materialize(workspace)


def test_final_check_rejection_requires_a_fresh_reviewer(tmp_path: Path) -> None:
    workspace = _materialize(tmp_path)
    runtime = FileGoalPlusRuntime(workspace / ".gp")
    record = runtime.create_goal(
        "Optimize score_batch under repository acceptance constraints",
        source_path=str(workspace),
        policy={"final_check": {"mode": "required"}},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )

    first = runtime.prepare_final_check(record.goal_plus_id, "codex")
    assert TASK_SKILL in first["launch"]["message"]
    assert "plugins/goal-plus/skills/unrelated/SKILL.md" not in first["launch"][
        "message"
    ]
    failed = runtime.submit_final_check(
        record.goal_plus_id,
        check_id=first["check"]["check_id"],
        goal_revision=1,
        verdict="fail",
        summary="The task Skill rejects stale identity-cached output.",
        findings=[{"requirement": TASK_SKILL, "problem": "stale cached output"}],
        evidence=[{"kind": "skill_check", "result": "failed"}],
    )
    assert failed.status == "active"
    assert failed.next_action is not None
    assert failed.next_action.kind == "address_final_check_findings"

    (workspace / "scoring.py").write_text(
        "from __future__ import annotations\n\n"
        "def score_batch(values: list[float]) -> list[float]:\n"
        "    return [float(value) * float(value) + 1.0 for value in values]\n",
        encoding="utf-8",
    )
    guard = _run(
        workspace,
        sys.executable,
        ".agents/skills/preserve-score-semantics/scripts/check_score_semantics.py",
    )
    assert guard.returncode == 0, guard.stdout + guard.stderr

    second = runtime.prepare_final_check(record.goal_plus_id, "codex")
    assert second["check"]["check_id"] != first["check"]["check_id"]
    assert second["launch"]["task_name"] != first["launch"]["task_name"]
    completed = runtime.submit_final_check(
        record.goal_plus_id,
        check_id=second["check"]["check_id"],
        goal_revision=1,
        verdict="pass",
        summary="The repaired delivery passes the task-local semantic guard.",
        evidence=[{"kind": "skill_check", "result": "passed"}],
    )
    assert completed.status == "complete"
    assert [check.status for check in completed.final_checks] == ["failed", "passed"]
