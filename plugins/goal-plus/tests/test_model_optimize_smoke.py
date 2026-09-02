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


def test_search_scheduler_spec_uses_current_workspace_and_scheduler() -> None:
    spec = json.loads(
        WORKSPACE_TEMPLATE.joinpath("search-scheduler-spec.json").read_text(
            encoding="utf-8"
        )
    )
    scheduler = spec["strategy"]["search_scheduler"]

    assert spec["source_path"] == "examples/model-optimize/workspace-template"
    assert spec["workspace"]["backend"] == "git_worktree"
    assert spec["budget"]["max_candidates"] > spec["budget"]["max_parallel"]
    assert spec["strategy"]["orchestration_mode"] == "adaptive_search"
    assert scheduler["host"] == "pi-rpc"
    assert scheduler["model"] == "bench-openai/gpt-5.6-sol"
    assert scheduler["reasoning_effort"] == "low"
    assert scheduler["timeout_seconds"] == 180
    assert scheduler["reward"].startswith("evidence_llm_value/v3 @prompt")
    assert scheduler["allocation"] == "value_guided_replace/v2"
    assert "enabled" not in scheduler
    assert "script_path" not in scheduler
    commands = [item["command"] for item in spec["process_verifiers"]]
    assert all(".goal-plus-verifiers/torch_cpu_tokens.py" in item for item in commands)
