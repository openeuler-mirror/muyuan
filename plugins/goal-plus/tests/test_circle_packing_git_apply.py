from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing-git-apply"
ORIGINAL_TEMPLATE = ROOT / "examples" / "circle-packing" / "workspace-template"
pytestmark = pytest.mark.example


def _git(workspace: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(workspace), *arguments],
        text=True,
    ).strip()


def _score(workspace: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, ".goal-plus-verifiers/circle_packing_score.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_circle_packing_git_workspace_applies_selected_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    original_program = (ORIGINAL_TEMPLATE / "initial_program.py").read_bytes()
    prepared = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "prepare_workspace.py"),
            "--destination",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"workspace={source}" in prepared.stdout
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert "__pycache__" not in _git(source, "ls-files")
    assert (source / ".gitignore").read_text(encoding="utf-8") == (
        "__pycache__/\n*.py[cod]\n"
    )
    source_head = _git(source, "rev-parse", "HEAD")
    baseline_score = _score(source)
    assert baseline_score["sum_radii"] == pytest.approx(1.7983485547704765)
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""

    spec = json.loads((EXAMPLE / "search-spec.json").read_text(encoding="utf-8"))
    assert spec["strategy"]["config"]["closeout_reserve_seconds"] == 60
    spec["source_path"] = str(source)
    verifier = source / ".goal-plus-verifiers" / "circle_packing_score.py"
    monkeypatch.chdir(EXAMPLE)
    tools = SearchTools(FileSearchRuntime(tmp_path / ".gp"))

    frozen = tools.search_freeze_spec(spec, [str(verifier)])
    run_id = tools.search_create(frozen["frozen_spec_id"])["run_id"]
    plan = tools.search_plan_next(run_id, 2)
    tasks = tools.search_start_batch(
        run_id,
        plan["plan_id"],
        proposals=[
            {"intent": "improve the two ring radii"},
            {"intent": "preserve the baseline"},
        ],
    )

    improved_workspace = Path(tasks[0]["workspace"])
    improved_program = improved_workspace / "initial_program.py"
    improved_text = improved_program.read_text(encoding="utf-8")
    improved_text = improved_text.replace("0.28 * math.cos", "0.30 * math.cos")
    improved_text = improved_text.replace("0.28 * math.sin", "0.30 * math.sin")
    improved_text = improved_text.replace("0.46 * math.cos", "0.44 * math.cos")
    improved_text = improved_text.replace("0.46 * math.sin", "0.44 * math.sin")
    improved_program.write_text(improved_text, encoding="utf-8")

    reports: dict[str, dict[str, object]] = {}
    for task in tasks:
        session = tools.search_start_agent_session(
            run_id,
            task["candidate_id"],
            {"goal": "verify one Circle Packing candidate"},
        )
        tools.search_get_agent_context(session["agent_session_id"])
        reports[task["candidate_id"]] = tools.search_run_verifier(
            run_id,
            task["candidate_id"],
            agent_session_id=session["agent_session_id"],
            hypothesis=task["proposal"]["intent"],
        )

    assert reports[tasks[0]["candidate_id"]]["aggregate_score"] == pytest.approx(
        1.8712160818793184
    )
    selected = tools.search_select(run_id)
    assert selected["selected_candidate_id"] == tasks[0]["candidate_id"]

    promoted = tools.search_promote(run_id, selected["selected_candidate_id"])
    assert promoted["strategy"] == "squash_commit"
    assert promoted["promotion_mode"] == "apply"
    assert promoted["apply_required"] is True
    applied = tools.search_apply_promotion(run_id)

    assert applied["state"] == "applied"
    assert applied["apply_result"]["method"] == "git_cherry_pick"
    assert _git(source, "rev-list", "--count", f"{source_head}..HEAD") == "1"
    assert not (source / "results.tsv").exists()
    assert _score(source)["sum_radii"] == pytest.approx(1.8712160818793184)
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert (ORIGINAL_TEMPLATE / "initial_program.py").read_bytes() == original_program
