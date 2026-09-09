from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.models import SearchSpec


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing-thinkthread-apply"
pytestmark = pytest.mark.example


def test_circle_packing_thinkthread_apply_example_is_deterministic(
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
    spec = SearchSpec.model_validate_json(
        destination.joinpath("search-spec.json").read_text(encoding="utf-8")
    )
    assert spec.source_path == "."
    assert spec.budget.max_parallel == 2
    assert spec.strategy.orchestration_mode == "parallel_loops"
    assert "worker_host" not in spec.strategy.model_dump()
    assert spec.strategy.inner_agent == "autoresearch"
    assert spec.strategy.worker_launch is None
    assert "host" not in spec.strategy.evidence_annotator.model_dump()
    assert spec.workspace.backend == "thinkthread"
    assert spec.workspace.promotion_mode == "apply"
    assert spec.edit_surface.allow == ["initial_program.py"]
    assert spec.constraints["success_threshold"] == 2.4
    assert len(spec.root_hypotheses) == 2

    verifier = destination / ".goal-plus-verifiers/circle_packing_score.py"

    def score() -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(verifier)],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    baseline = score()
    assert baseline["valid"] is True
    assert float(baseline["sum_radii"]) == pytest.approx(1.79835, abs=1e-4)

    expected_scores = {
        "rectangular.py": 26 * (1.0 / 12.0 - 1e-7),
        "staggered.py": 26 * (1.0 / (2.0 + 5.0 * (3.0**0.5)) - 1e-7),
    }
    for name, expected_score in expected_scores.items():
        hint = destination / "candidate-hints" / name
        destination.joinpath("initial_program.py").write_text(
            hint.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        result = score()
        assert result["valid"] is True
        assert float(result["sum_radii"]) == pytest.approx(expected_score)

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
    restored = score()
    assert restored["valid"] is True
    assert float(restored["sum_radii"]) == pytest.approx(1.79835, abs=1e-4)


def test_circle_packing_thinkthread_prompt_is_outcome_focused() -> None:
    prompt = EXAMPLE.joinpath("pi-goal-prompt.md").read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]

    assert first_line.startswith(
        "/goal-plus mode=probe max_parallel=2 max_duration_seconds=1200 "
        "workspace_backend=thinkthread promotion_mode=apply "
        "strategy=agent_guided inner_agent=autoresearch "
    )
    assert "c001" in prompt and "candidate-hints/rectangular.py" in prompt
    assert "c002" in prompt and "candidate-hints/staggered.py" in prompt
    assert "continuation 同一个 candidate" in prompt
    assert "不要新增 plan、candidate 或 run" in prompt
    assert "sum_radii >= 2.4" in prompt
    for internal_detail in (
        "GOAL_PLUS_ROOT",
        "instances/",
        "host-pools/",
        "Artifact View",
        "Child",
        "fs_snapshot",
    ):
        assert internal_detail not in prompt
