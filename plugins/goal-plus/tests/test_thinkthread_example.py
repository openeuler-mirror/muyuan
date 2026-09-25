from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "how-to-work-with-thinkthread"
pytestmark = pytest.mark.example


def test_thinkthread_example_declares_main_route_and_public_configuration() -> None:
    prompt = (EXAMPLE / "pi-goal-prompt.md").read_text(encoding="utf-8")
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    commands = [line for line in prompt.splitlines() if line.startswith("/goal-plus ")]

    assert len(commands) == 1
    command = commands[0]
    assert command.startswith("/goal-plus max_duration_seconds=300 ")
    assert 'route="main"' in command
    assert "agent-posix" not in prompt
    assert "THINKTHREAD_" not in prompt
    assert "workspace_provider=" not in prompt
    assert "git init" not in readme
    assert "git-tt" in readme


def test_thinkthread_goal_mode_workspace_is_disposable(tmp_path: Path) -> None:
    destination = tmp_path / "workspace"
    prepare = EXAMPLE / "prepare_workspace.py"

    result = subprocess.run(
        [sys.executable, str(prepare), "--destination", str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"workspace={destination}" in result.stdout
    assert (destination / ".thinkthread-goal-plus-example").is_file()
    failed = subprocess.run(
        [sys.executable, str(destination / "verify.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 1

    (destination / "greeting.txt").write_text(
        "Hello from ThinkThread!\n",
        encoding="utf-8",
    )
    passed = subprocess.run(
        [sys.executable, str(destination / "verify.py")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(passed.stdout)["status"] == "pass"

    subprocess.run(
        [
            sys.executable,
            str(prepare),
            "--destination",
            str(destination),
            "--reset",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (destination / "greeting.txt").read_text(encoding="utf-8") == "TODO\n"
