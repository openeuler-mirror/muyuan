from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "model-optimize"
WORKSPACE_TEMPLATE = EXAMPLE / "workspace-template"
VERIFIER = (
    WORKSPACE_TEMPLATE / ".goal-plus-verifiers" / "torch_cpu_tokens.py"
)
pytestmark = pytest.mark.example


def test_torch_cpu_source_owned_verifier_accepts_the_baseline() -> None:
    for mode in ("--validate", "--rank"):
        completed = subprocess.run(
            [sys.executable, str(VERIFIER), mode],
            cwd=WORKSPACE_TEMPLATE,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        if mode == "--validate":
            assert result == {"valid": True}
        else:
            assert result["tokens_per_second"] > 0
