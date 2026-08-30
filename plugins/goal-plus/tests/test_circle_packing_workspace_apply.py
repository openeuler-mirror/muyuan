from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing-workspace-apply"
TRACKED_TEMPLATE = ROOT / "examples" / "circle-packing" / "workspace-template"
PREPARE = EXAMPLE / "prepare_workspace.py"
pytestmark = pytest.mark.example


def _git(workspace: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=workspace,
        text=True,
    ).strip()


def _prepare(tmp_path: Path) -> Path:
    workspace = tmp_path / "circle-workspace"
    completed = subprocess.run(
        [sys.executable, str(PREPARE), "--destination", str(workspace)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return workspace


def test_prepare_circle_workspace_creates_clean_independent_git_baseline(
    tmp_path: Path,
) -> None:
    workspace = _prepare(tmp_path)

    assert Path(_git(workspace, "rev-parse", "--show-toplevel")) == workspace
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(workspace, "rev-list", "--count", "HEAD") == "1"
    assert json.loads((workspace / "search-spec.json").read_text(encoding="utf-8"))[
        "source_path"
    ] == "."
    assert "promotion_mode" not in json.loads(
        (workspace / "search-spec.json").read_text(encoding="utf-8")
    )["workspace"]

    completed = subprocess.run(
        [sys.executable, ".goal-plus-verifiers/circle_packing_score.py"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    score = json.loads(completed.stdout.strip().splitlines()[-1])
    assert score["valid"] is True
    assert score["sum_radii"] == pytest.approx(1.7983485547704765)

    ignored_probe = EXAMPLE / "workspace" / "ignored-probe.txt"
    assert subprocess.run(
        ["git", "check-ignore", "--quiet", str(ignored_probe)],
        cwd=ROOT,
        check=False,
    ).returncode == 0


def test_circle_workspace_promotion_applies_one_commit_and_reports_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _prepare(tmp_path)
    source_head = _git(workspace, "rev-parse", "HEAD")
    tracked_baseline = (TRACKED_TEMPLATE / "initial_program.py").read_bytes()
    spec = json.loads((workspace / "search-spec.json").read_text(encoding="utf-8"))
    monkeypatch.chdir(workspace)
    tools = SearchTools(FileSearchRuntime(tmp_path / ".gp"))

    frozen = tools.search_freeze_spec(
        spec,
        [".goal-plus-verifiers/circle_packing_score.py"],
    )
    run_id = tools.search_create(frozen["frozen_spec_id"])["run_id"]
    plan = tools.search_plan_next(run_id, 1)
    [task] = tools.search_start_batch(
        run_id,
        plan["plan_id"],
        proposals=[{"intent": "verify a harmless publication change"}],
    )
    candidate_program = Path(task["workspace"]) / "initial_program.py"
    candidate_program.write_text(
        candidate_program.read_text(encoding="utf-8")
        + "\n# Applied by the workspace-apply example.\n",
        encoding="utf-8",
    )
    tools.search_run_verifier(run_id, task["candidate_id"])

    selected = tools.search_select(run_id)
    promoted = tools.search_promote(run_id, selected["selected_candidate_id"])
    applied = tools.search_apply_promotion(run_id)
    report = tools.search_report(run_id)
    monitor = tools.goal_plus_monitor_snapshot(run_id=run_id)

    assert promoted["strategy"] == "squash_commit"
    assert applied["state"] == "applied"
    assert applied["apply_timing"]["outcome"] == "succeeded"
    assert applied["apply_timing"]["duration_seconds"] >= 0
    assert _git(workspace, "rev-list", "--count", f"{source_head}..HEAD") == "1"
    assert _git(workspace, "status", "--porcelain") == ""
    assert "Applied by the workspace-apply example" in (
        workspace / "initial_program.py"
    ).read_text(encoding="utf-8")
    assert (TRACKED_TEMPLATE / "initial_program.py").read_bytes() == tracked_baseline
    assert Path(report["report_path"]).is_file()
    assert Path(report["html_report_path"]).is_file()

    statistics = monitor["statistics"]["selected_run"]
    assert statistics["workspace_operations"]["provider"] == "git_worktree"
    assert statistics["workspace_operations"]["workspace_creation"][
        "attempts_observed"
    ] == 1
    assert statistics["workspace_operations"]["promotion_apply"]["observed"] is True
    assert statistics["timing"]["promotion_apply_seconds"] >= 0
