from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from tests.st.hosts import link_host_assets
from tests.st.helpers.codex_runner import CodexRunner


pytestmark = [pytest.mark.st, pytest.mark.st_codex]


def test_codex_goal_plus_required_final_checker_smoke(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    delivery = st_project_root / "delivery.txt"
    delivery.write_text("goal-plus-final-check-smoke\n", encoding="utf-8")
    runner = CodexRunner(
        project_root=st_project_root,
        log_dir=st_log_dir,
        default_timeout=600,
    )
    raw_goal = (
        "Read delivery.txt and prove that its complete trimmed content is exactly "
        "goal-plus-final-check-smoke. Do not edit any file. Finish through the required "
        "independent Codex final checker."
    )
    result = runner.run_streaming(
        (
            "$goal-plus-with-final-check "
            f"{raw_goal}\n\n"
            "The host entrypoint already created the required-final-check record. "
            "Record Goal Mode triage, inspect the file read-only, then call "
            "goal_plus_prepare_final_check(checker_host='codex'). Launch the exact returned "
            "foreground reviewer payload, wait for it, and do not submit the verdict on its "
            "behalf. Finish only after goal_plus_status reports complete."
        ),
        scenario="codex_goal_plus_final_check",
        timeout=600,
    )

    print(f"\n[codex_goal_plus_final_check] log: {result.log_path}")
    assert not result.timed_out, result.log_path
    goal_files = sorted((st_project_root / ".gp" / "goal-plus").glob("gp_*/goal.json"))
    assert len(goal_files) == 1, result.log_path
    record = json.loads(goal_files[0].read_text(encoding="utf-8"))
    assert record["status"] == "complete", result.log_path
    assert record["entrypoint_status"] == "host_verified", result.log_path
    assert len(record["host_command_invocations"]) == 1, result.log_path
    invocation = record["host_command_invocations"][0]
    assert invocation["host"] == "codex", result.log_path
    assert invocation["transport"] == "codex_user_prompt_submit", result.log_path
    assert invocation["command"] == "$goal-plus-with-final-check", result.log_path
    assert invocation["action"] == "start", result.log_path
    assert record["policy"]["final_check"]["mode"] == "required"
    assert record["goal_revision"] == 1
    assert record["final_checks"][-1]["checker_host"] == "codex"
    assert record["final_checks"][-1]["status"] == "passed"


def test_codex_final_checker_rejects_then_accepts_task_skill_example(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    example = Path(__file__).resolve().parents[2] / "examples" / "final-check-skill-guard"
    workspace = st_project_root / f"final-check-skill-guard-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            sys.executable,
            str(example / "prepare_workspace.py"),
            "--output",
            str(workspace),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    link_host_assets(workspace, Path(__file__).resolve().parents[2])
    prompt = (workspace / "codex-goal-prompt.md").read_text(encoding="utf-8")
    runner = CodexRunner(
        project_root=workspace,
        log_dir=st_log_dir,
        default_timeout=1800,
    )
    result = runner.run_streaming(
        prompt,
        scenario="codex_final_check_skill_guard",
        timeout=1800,
    )

    print(f"\n[codex_final_check_skill_guard] log: {result.log_path}")
    assert not result.timed_out, result.log_path
    assert result.returncode == 0, result.log_path
    goal_files = sorted((workspace / ".gp" / "goal-plus").glob("gp_*/goal.json"))
    assert len(goal_files) == 1, result.log_path
    record = json.loads(goal_files[0].read_text(encoding="utf-8"))
    checks = record["final_checks"]
    assert record["status"] == "complete", result.log_path
    assert record["entrypoint_status"] == "host_verified", result.log_path
    assert [check["status"] for check in checks] == ["failed", "passed"], result.log_path
    assert checks[0]["check_id"] != checks[1]["check_id"], result.log_path
    assert checks[0]["findings"], result.log_path
    assert checks[1]["evidence"], result.log_path
    guard = subprocess.run(
        [
            sys.executable,
            ".agents/skills/preserve-score-semantics/scripts/check_score_semantics.py",
        ],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert guard.returncode == 0, guard.stdout + guard.stderr
