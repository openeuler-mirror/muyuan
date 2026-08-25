from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "vliw-kernel-optimization"
WORKSPACE = EXAMPLE / "workspace-template"
pytestmark = pytest.mark.example


def test_vliw_public_baseline_emits_goal_plus_metric() -> None:
    completed = subprocess.run(
        [sys.executable, ".goal-plus-verifiers/vliw_score.py"],
        cwd=WORKSPACE,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "cycles": 147734.0
    }


def test_vliw_example_contains_only_public_evaluation_assets() -> None:
    required = (
        "problem.py",
        "solution.py",
        "runner.py",
        "verifier.py",
        "test_cases/public_cases.json",
        "public_tests/smoke_test.py",
        ".goal-plus-verifiers/vliw_score.py",
    )
    assert all((WORKSPACE / path).is_file() for path in required)
    assert not any("hidden" in path.name.lower() for path in EXAMPLE.rglob("*"))
    assert (EXAMPLE / "LICENSE.edgebench").is_file()
