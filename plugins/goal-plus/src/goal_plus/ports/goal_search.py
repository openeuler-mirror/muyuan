"""Narrow Search result coordinates and final-check input facts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from goal_plus.domain.goal import GoalPlusRecord


@dataclass(frozen=True)
class LinkedSearchState:
    frozen_spec_id: str | None
    state: str | None
    selected_candidate_id: str | None
    promotion_mode: str | None
    promotion_state: str | None


@dataclass(frozen=True)
class FinalCheckInputs:
    root_dir: Path
    workspace: Path
    task_skill_paths: list[str]
    report_targets: list[dict[str, str]]


class GoalSearchArtifacts(Protocol):
    def read_search(self, run_id: str) -> LinkedSearchState | None: ...
    def report_path(self, run_id: str, fallback: str | None) -> str: ...
    def html_report_path(self, run_id: str) -> str: ...
    def promotion_artifact_path(
        self, run_id: str, candidate_id: str | None, fallback: str | None
    ) -> str | None: ...
    def artifact_available(self, path: str) -> bool: ...
    def final_check_inputs(self, record: GoalPlusRecord) -> FinalCheckInputs: ...
