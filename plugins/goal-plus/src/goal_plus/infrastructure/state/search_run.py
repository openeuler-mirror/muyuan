"""Initial run and plan records in the existing Search layout."""

from pathlib import Path

from goal_plus.domain.artifacts import WorkspaceProvider
from goal_plus.domain.search import SearchPlan
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_text,
    write_json,
)
from goal_plus.ports.state import RunRecord


class FileSearchRunState(FileCandidateQueries):
    def new_run_id(self) -> str:
        """Mint a short sequential id: run_NNNN, following the gp_NNNN pattern.

        Run ids ride along in every tool call, roster and injection; a hex or
        timestamp suffix costs ~20 tokens per occurrence. Sequence numbers stay
        collision-free within one runtime root, sort by creation in ls output,
        and leave legacy long-format run dirs untouched.
        """
        runs_dir = self.root_dir / "runs"
        with exclusive_file_lock(self.root_dir / "run-id.lock"):
            highest = 0
            if runs_dir.is_dir():
                for entry in runs_dir.iterdir():
                    suffix = entry.name.removeprefix("run_")
                    if entry.is_dir() and entry.name != suffix and suffix.isdigit():
                        highest = max(highest, int(suffix))
            counter = runs_dir / ".next-run-number"
            try:
                next_number = int(counter.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError):
                next_number = 1
            next_number = max(next_number, highest + 1)
            write_text(counter, str(next_number + 1))
            return f"run_{next_number:04d}"

    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def write_run(self, run: RunRecord) -> None:
        write_json(self.run_dir(run.run_id) / "run.json", run.model_dump(mode="json"))

    def read_plan(self, run_id: str, plan_id: str) -> SearchPlan:
        return SearchPlan.model_validate(
            load_json(self.run_dir(run_id) / "plans" / f"{plan_id}.json")
        )

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
