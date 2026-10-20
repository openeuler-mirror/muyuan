from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.domain.search_input import SearchSpecInput


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing-thinkthread-apply"
pytestmark = pytest.mark.example


def test_public_example_accepts_closed_unsettled_continuation_only():
    from tests.st_thinkthread.test_circle_main import assert_acknowledged_invocations

    dispatches = [{"invocation_id": "first-delivery"}]
    resource = {"acknowledged_messages": {"first": "first-delivery", "second": "unfinished"},
                "active_message_id": "unfinished", "cleanup_state": "closed", "status": "stopped",
                "invocation_result": None, "attempt_request": None}
    assert_acknowledged_invocations(resource, dispatches, bounded=False)
    with pytest.raises(AssertionError):
        assert_acknowledged_invocations(resource, dispatches, bounded=True)
    for mutation in [
        {"cleanup_state": "closing"}, {"status": "running"},
        {"active_message_id": "peer-message"}, {"attempt_request": {"state": "accepted"}},
        {"invocation_result": {"state": "completed"}},
        {"acknowledged_messages": {"second": "unfinished"}},
    ]:
        with pytest.raises(AssertionError):
            assert_acknowledged_invocations({**resource, **mutation}, dispatches, bounded=False)


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
    spec = SearchSpecInput.model_validate_json(
        destination.joinpath("search-spec.json").read_text(encoding="utf-8")
    ).to_runtime_spec()
    assert spec.source_path == "."
    assert spec.budget.max_parallel == 2
    assert spec.strategy.initial_context == "fresh"
    assert "worker_host" not in spec.strategy.model_dump()
    assert spec.strategy.inner_agent == "autoresearch"
    assert spec.strategy.worker_launch is None
    assert "agent_harness" not in spec.strategy.evidence_annotator.model_dump()
    assert spec.workspace.provider == "thinkthread"
    assert spec.workspace.promotion_mode == "apply"
    assert spec.edit_surface.allow == ["initial_program.py"]
    assert spec.constraints["success_threshold"] == 2.2
    assert len(spec.root_hypotheses) == 2

    verifier = destination / ".goal-plus-verifiers/circle_packing_score.py"

    def score() -> dict[str, object]:
        original_files = {
            path.relative_to(destination): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }
        result = subprocess.run(
            [sys.executable, str(verifier)],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        assert {
            path.relative_to(destination): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        } == original_files
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
        "workspace_provider=thinkthread promotion_mode=apply "
        "strategy=agent_guided inner_agent=autoresearch "
    )
    assert "c001" in prompt and "candidate-hints/rectangular.py" in prompt
    assert "c002" in prompt and "candidate-hints/staggered.py" in prompt
    assert "sum_radii >= 2.2" in prompt
    for internal_detail in (
        "GOAL_PLUS_ROOT",
        "instances/",
        "host-pools/",
        "Artifact View",
        "Child",
        "fs_snapshot",
    ):
        assert internal_detail not in prompt


def test_circle_packing_thinkthread_variant_prompts_bind_expected_modes() -> None:
    llm = EXAMPLE.joinpath("pi-goal-prompt-llm-verifier.md").read_text(encoding="utf-8")
    adaptive = EXAMPLE.joinpath("pi-goal-prompt-adaptive-search.md").read_text(encoding="utf-8")

    for prompt in (llm, adaptive):
        assert prompt.startswith(
            "/goal-plus mode=probe max_parallel=2 max_duration_seconds=1200 "
            "workspace_provider=thinkthread promotion_mode=apply "
            "strategy=agent_guided inner_agent=autoresearch "
        )
        assert "candidate-hints/rectangular.py" in prompt
        assert "candidate-hints/staggered.py" in prompt
        assert "sum_radii >= 2.4" in prompt

    assert "GOAL_PLUS_DEBUG=1" in llm
    assert "initial_context: fresh" in llm
    assert "ranking_keys: [quality_score, hard_score]" in llm
    assert "initial_context: fresh" in adaptive
    assert "adaptive_search:" in adaptive
    assert "max_candidates: 4" in adaptive
    assert "policy: swe_search_uct/v1" in adaptive
    assert "policy: search_diagnostic_guidance/v1" in adaptive


def test_circle_packing_thinkthread_codex_prompt_uses_host_entrypoint() -> None:
    prompt = EXAMPLE.joinpath("codex-goal-prompt.md").read_text(encoding="utf-8")

    assert "`goal_plus_host_start`" in prompt
    assert "$goal-plus" not in prompt
    assert (
        "mode=probe max_parallel=2 max_duration_seconds=1200 "
        "workspace_provider=thinkthread promotion_mode=apply "
        "strategy=agent_guided inner_agent=common "
    ) in prompt
    assert "candidate-hints/rectangular.py" in prompt
    assert "candidate-hints/staggered.py" in prompt
    assert "sum_radii >= 2.2" in prompt
    assert "goal_plus_host_close_scopes" not in prompt
