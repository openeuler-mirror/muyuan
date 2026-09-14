"""Goal-linked Search coordinates and tracked task-skill discovery."""

from pathlib import Path
from goal_plus.domain.goal import GoalPlusRecord
from goal_plus.ports.goal_search import LinkedSearchState, FinalCheckInputs
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.artifacts.search_source import (
    discover_git_tracked_task_skills,
)


class FileGoalSearchArtifacts:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def read_search(self, run_id: str) -> LinkedSearchState | None:
        path = self.root_dir / "runs" / run_id / "run.json"
        if not path.exists():
            return None
        payload = load_json(path)
        promotion = payload.get("promotion")
        promotion = promotion if isinstance(promotion, dict) else {}
        return LinkedSearchState(
            payload.get("frozen_spec_id"),
            payload.get("state"),
            payload.get("selected_candidate_id"),
            promotion.get("promotion_mode"),
            promotion.get("state"),
        )

    def artifact_available(self, path: str) -> bool:
        return Path(path).is_file()

    def final_check_inputs(self, record: GoalPlusRecord) -> FinalCheckInputs:
        workspace = self.source_workspace(record)
        return FinalCheckInputs(
            self.root_dir,
            workspace,
            discover_git_tracked_task_skills(workspace),
            [
                {
                    "run_id": task.run_id,
                    "report_path": self.report_path(task.run_id, None),
                    "html_report_path": self.html_report_path(task.run_id),
                }
                for task in record.search_tasks
                if task.goal_revision == record.goal_revision
                and task.run_id
                and task.result_recorded_at
            ],
        )

    def report_path(self, run_id: str, fallback: str | None) -> str:
        report_path = self.root_dir / "runs" / run_id / "report.md"
        return str(report_path.resolve())

    def html_report_path(self, run_id: str) -> str:
        report_path = self.root_dir / "runs" / run_id / "report.html"
        return str(report_path.resolve())

    def promotion_artifact_path(
        self,
        run_id: str,
        selected_candidate_id: str | None,
        fallback: str | None,
    ) -> str | None:
        if selected_candidate_id:
            patch_path = (
                self.root_dir
                / "runs"
                / run_id
                / "promotion"
                / f"{selected_candidate_id}.patch"
            )
            if patch_path.exists():
                return str(patch_path.resolve())
        return fallback

    def source_workspace(self, record: GoalPlusRecord) -> Path:
        source = Path(record.source_path or ".")
        if not source.is_absolute():
            source = self.root_dir.parent / source
        return source.resolve()
