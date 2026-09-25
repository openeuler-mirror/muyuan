from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.domain.goal_commands import (parse_goal_plus_command_config)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import start_host_verified_goal


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing-git-apply"
ORIGINAL_TEMPLATE = ROOT / "examples" / "circle-packing" / "workspace-template"
pytestmark = pytest.mark.example


def test_circle_packing_git_apply_prompts_bind_inner_agents() -> None:
    spec = json.loads((EXAMPLE / "search-spec.json").read_text(encoding="utf-8"))
    assert spec["strategy"]["inner_agent"] == "autoresearch"

    prompt_names = (
        "pi-goal-prompt.md",
        "pi-goal-prompt-common.md",
        "pi-goal-prompt-adaptive-search.md",
        "codex-goal-prompt.md",
        "pi-goal-prompt-untimed.md",
        "codex-goal-prompt-untimed.md",
    )
    for name in prompt_names:
        text = (EXAMPLE / name).read_text(encoding="utf-8")
        first_line, body = text.split("\n", 1)
        objective, config = parse_goal_plus_command_config(
            first_line.split(" ", 1)[1]
        )
        expected_inner_agent = "common" if "common" in name else "autoresearch"

        assert objective
        assert config.inner_agent == expected_inner_agent
        assert config.workspace_provider == "git_worktree"
        assert config.promotion_mode == "apply"
        assert '`source_path="workspace"' in body
        if name == "pi-goal-prompt-common.md":
            assert "goal_plus_search_run_verifier" in body


def test_circle_packing_adaptive_prompt_enables_apply_steering() -> None:
    text = (EXAMPLE / "pi-goal-prompt-adaptive-search.md").read_text(encoding="utf-8")
    required_scores = {
        "progress_reward", "allocation_utility", "correctness_gap", "validation_gap",
        "local_confounder_evidence", "discriminating_test_gap", "boundary_evidence_strength",
    }
    assert all(f"      - {score}" in text for score in required_scores)
    assert "policy: search_diagnostic_guidance/v1" in text
    assert "mode: apply" in text
    for parameter in (
        "correctness_gap_threshold", "validation_gap_threshold", "confounder_threshold",
        "test_gap_threshold", "boundary_probe_threshold", "boundary_confirmed_threshold",
        "cooldown_seconds", "max_steering_attempts_per_candidate",
        "max_steering_attempts_per_issue", "history_limit", "rpc_timeout_seconds",
        "max_source_age_seconds",
    ):
        assert f"{parameter}:" in text


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
    assert "worker_budget" not in spec["strategy"]
    spec["source_path"] = str(source)
    verifier = source / ".goal-plus-verifiers" / "circle_packing_score.py"
    monkeypatch.chdir(EXAMPLE)
    root = tmp_path / ".gp"
    start_host_verified_goal(root, source)
    tools = SearchTools(FileSearchRuntime(root))

    frozen = tools.goal_plus_search_freeze_spec(
        "gp_0001",
        spec,
        [str(verifier.relative_to(source))],
    )
    run_id = tools.goal_plus_search_create("gp_0001", frozen["frozen_spec_id"], "initial")["run_id"]
    plan = tools.goal_plus_search_plan_next(run_id, 2)
    tasks = tools.goal_plus_search_start_batch(
        run_id,
        plan["plan_id"],
        proposals=[
            {"intent": "improve the two ring radii"},
            {"intent": "preserve the baseline"},
        ],
    )

    improved_workspace = tools.runtime.candidate_creation.state.read_candidate(run_id, tasks[0]["candidate_id"]).task.workspace
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
        context = tools.goal_plus_search_get_agent_context(session["agent_session_id"])
        reports[task["candidate_id"]] = tools.goal_plus_search_run_verifier(
            run_id,
            task["candidate_id"],
            agent_session_id=session["agent_session_id"],
            hypothesis=context["candidate_task"]["hypothesis"],
        )

    assert reports[tasks[0]["candidate_id"]]["aggregate_score"] == pytest.approx(
        1.8712160818793184
    )
    selected = tools.goal_plus_search_select(run_id)
    assert selected["selected_candidate_id"] == tasks[0]["candidate_id"]

    promoted = tools.goal_plus_search_promote(run_id, selected["selected_candidate_id"])
    assert tools.runtime.promotion_record(run_id).strategy == "squash_commit"
    assert promoted["promotion_mode"] == "apply"
    assert promoted["apply_required"] is True
    applied = tools.goal_plus_search_apply_promotion(run_id)

    assert applied["state"] == "applied"
    from goal_plus.reporting import build_html_report_data

    data = build_html_report_data(root, run_id)
    projected = data["search_tasks"][0]["run"]
    assert projected["search_result"] == "passed"
    assert projected["publication_outcome"]["state"] == "applied"
    assert projected["publication_outcome"]["publication_artifact_ref"] == tools.runtime.promotion_record(run_id).publication_artifact_ref.model_dump(mode="json")
    assert data["snapshot"]["run"]["publication_outcome"] == projected["publication_outcome"]
    assert tools.runtime.promotion_record(run_id).apply_result["method"] == "git_cherry_pick"
    assert _git(source, "rev-list", "--count", f"{source_head}..HEAD") == "1"
    assert not (source / "results.tsv").exists()
    assert _score(source)["sum_radii"] == pytest.approx(1.8712160818793184)
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert (ORIGINAL_TEMPLATE / "initial_program.py").read_bytes() == original_program
