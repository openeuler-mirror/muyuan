"""Initial run and plan records in the existing Search layout."""

from pathlib import Path
import time
import uuid

from goal_plus.domain.artifacts import WorkspaceProvider
from goal_plus.domain.search import SearchPlan
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_json,
)
from goal_plus.ports.state import RunRecord


class FileSearchRunState(FileCandidateQueries):
    def new_run_id(self, now: float) -> str:
        return f"run_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(now))}_{uuid.uuid4().hex[:8]}"

    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def write_run(self, run: RunRecord) -> None:
        write_json(self.run_dir(run.run_id) / "run.json", run.model_dump(mode="json"))

    def read_plan(self, run_id: str, plan_id: str) -> SearchPlan:
        return SearchPlan.model_validate(
            load_json(self.run_dir(run_id) / "plans" / f"{plan_id}.json")
        )

    def read_plans(self, run_id: str) -> list[SearchPlan]:
        return [
            SearchPlan.model_validate(load_json(path))
            for path in sorted((self.run_dir(run_id) / "plans").glob("plan_*.json"))
        ]

    def write_plan(self, plan: SearchPlan) -> None:
        write_json(
            self.run_dir(plan.run_id) / "plans" / f"{plan.plan_id}.json",
            plan.model_dump(mode="json"),
        )

    def initialize_run_layout(self, run_id: str, workspace_provider: WorkspaceProvider) -> None:
        directories = [
            "candidates",
            *(["workspace"] if workspace_provider != "thinkthread" else []),
            "plans",
            "agent_sessions",
        ]
        for name in directories:
            (self.run_dir(run_id) / name).mkdir(parents=True, exist_ok=True)

    def relocation_log_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "bootstrap" / "workspace-relocation"
