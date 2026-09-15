"""Native Codex Circle through the shared MCP/session workflow."""

from pathlib import Path

import pytest

from .helpers.circle_cycle import run_circle_cycle


SCENARIO_CASES = [
    pytest.param(
        "codex_circle_packing_cycle",
        marks=(pytest.mark.st, pytest.mark.st_codex),
        id="codex_circle_packing_cycle",
    ),
]


@pytest.mark.parametrize("scenario", SCENARIO_CASES)
def test_scenario(scenario: str, st_runner, st_project_root: Path, monkeypatch, installed_codex_config) -> None:
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "1")
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED", "1")
    monkeypatch.delenv("GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED", raising=False)
    run_circle_cycle(st_runner, st_project_root)
