from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing"
WORKSPACE = EXAMPLE / "workspace"
pytestmark = pytest.mark.example


def test_circle_packing_public_baseline_emits_goal_plus_metric() -> None:
    completed = subprocess.run(
        [sys.executable, ".goal-plus-verifiers/circle_packing_score.py"],
        cwd=WORKSPACE,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["valid"] is True
    assert report["sum_radii"] == pytest.approx(1.7983485547704765)


def test_circle_packing_prompt_is_a_five_minute_interactive_pi_goal() -> None:
    prompt = (EXAMPLE / "pi-goal-prompt.md").read_text(encoding="utf-8")
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]

    assert first_line.startswith("/goal-plus annotator=")
    assert " main=" not in first_line
    assert "annotator=openai-codex/gpt-5.6-terra" in first_line
    assert (
        "workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-luna"
        in first_line
    )
    assert "max_parallel=2" in first_line
    assert "current directory" in first_line
    assert ".goal-plus-verifiers/circle_packing_score.py" in prompt
    assert "within 5 minutes" in prompt
    assert "after 4 minutes" in prompt
    assert "No worker lease may\nexceed 3 minutes" in prompt
    assert "Git worktree-backed Search" in prompt
    assert "cd examples/circle-packing/workspace" in readme
    assert "pi --model openai-codex/gpt-5.6-terra" in readme
    assert "Then paste the complete contents of" in readme
    assert "$(cat examples/circle-packing/pi-goal-prompt.md)" not in readme


def test_circle_packing_example_owns_its_user_facing_workspace() -> None:
    assert (EXAMPLE / "search-spec.json").is_file()
    assert (WORKSPACE / "initial_program.py").is_file()
    assert (
        WORKSPACE / ".goal-plus-verifiers" / "circle_packing_score.py"
    ).is_file()
    assert not (ROOT / "tests" / "fixtures" / "circle_packing").exists()
