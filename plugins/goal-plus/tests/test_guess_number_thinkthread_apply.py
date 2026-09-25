from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.domain.search_input import SearchSpecInput


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "guess-number-thinkthread-apply"
pytestmark = pytest.mark.example


def test_guess_number_thinkthread_apply_example_is_deterministic(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "workspace"
    prepare = EXAMPLE / "prepare_workspace.py"

    subprocess.run(
        [sys.executable, str(prepare), "--destination", str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    spec = SearchSpecInput.model_validate_json(
        destination.joinpath("search-spec.json").read_text(encoding="utf-8")
    ).to_runtime_spec()
    assert spec.source_path == "."
    assert spec.budget.max_parallel == 2
    assert spec.strategy.orchestration_mode == "parallel_loops"
    assert "worker_host" not in spec.strategy.model_dump()
    assert spec.strategy.worker_launch is None
    assert "agent_harness" not in spec.strategy.evidence_annotator.model_dump()
    assert spec.workspace.provider == "thinkthread"
    assert spec.workspace.promotion_mode == "apply"
    assert spec.edit_surface.allow == ["guess.py"]
    assert len(spec.root_hypotheses) == 2

    verifier = destination / "verify.py"

    def score() -> dict[str, int]:
        result = subprocess.run(
            [sys.executable, str(verifier)],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    assert score() == {"guess_score": -1}
    destination.joinpath("guess.py").write_text("print(0)\n", encoding="utf-8")
    assert score() == {"guess_score": 0}
    destination.joinpath("guess.py").write_text("print(1)\n", encoding="utf-8")
    assert score() == {"guess_score": 1}

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
    assert destination.joinpath("guess.py").read_text(encoding="utf-8") == (
        "print(-1)\n"
    )


def test_guess_number_thinkthread_prompt_is_outcome_focused() -> None:
    prompt = EXAMPLE.joinpath("pi-goal-prompt.md").read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]

    assert first_line.startswith(
        "/goal-plus mode=probe max_parallel=2 max_duration_seconds=600 "
        "workspace_provider=thinkthread promotion_mode=apply "
        "strategy=agent_guided "
    )
    assert "c001" in prompt and "`print(0)`" in prompt
    assert "c002" in prompt and "`print(1)`" in prompt
    for internal_detail in (
        "GOAL_PLUS_ROOT",
        "instances/",
        "host-pools/",
        "Artifact View",
        "Child",
        "fs_snapshot",
    ):
        assert internal_detail not in prompt
