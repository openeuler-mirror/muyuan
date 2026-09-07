"""Original ScheduleTask file layout; state transitions belong to Application."""

from contextlib import AbstractContextManager
from pathlib import Path

from goal_plus.domain.adaptive.models import ScheduleTask
from goal_plus.domain.search import FrozenSpec
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_json,
)
from goal_plus.infrastructure.state.frozen_spec import load_frozen_spec
from goal_plus.ports.scheduler_context import SchedulerCandidateContext
from goal_plus.ports.scheduler_lanes import SchedulerCandidateProjection
from goal_plus.ports.state import CandidateRecord, RunRecord


def candidate_state_dir(run_dir: Path | str, candidate_id: str) -> Path:
    return Path(run_dir) / "candidates" / candidate_id


def schedule_task_path(
    run_dir: Path | str,
    candidate_id: str,
    iteration: int,
) -> Path:
    return (
        candidate_state_dir(run_dir, candidate_id)
        / "schedule-tasks"
        / f"iteration-{iteration:04d}.json"
    )


def schedule_task_paths(
    run_dir: Path | str,
    candidate_id: str | None = None,
) -> list[Path]:
    pattern = (
        f"candidates/{candidate_id}/schedule-tasks/iteration-*.json"
        if candidate_id is not None
        else "candidates/*/schedule-tasks/iteration-*.json"
    )
    return sorted(Path(run_dir).glob(pattern))


def load_schedule_task(
    run_dir: Path | str,
    candidate_id: str,
    iteration: int,
) -> ScheduleTask | None:
    path = schedule_task_path(run_dir, candidate_id, iteration)
    if not path.exists():
        return None
    return ScheduleTask.model_validate(load_json(path))


def load_schedule_tasks(
    run_dir: Path | str,
    candidate_id: str | None = None,
) -> list[ScheduleTask]:
    return [
        ScheduleTask.model_validate(load_json(path))
        for path in schedule_task_paths(run_dir, candidate_id)
    ]


class FileSchedulerState:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def schedule_task_path(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> Path:
        return schedule_task_path(self.run_dir(run_id), candidate_id, iteration)

    def load_schedule_task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> ScheduleTask | None:
        return load_schedule_task(
            self.run_dir(run_id),
            candidate_id,
            iteration,
        )

    def write_schedule_task(self, task: ScheduleTask) -> None:
        write_json(
            self.schedule_task_path(task.run_id, task.candidate_id, task.iteration),
            task.model_dump(mode="json"),
        )

    def load_schedule_tasks(
        self,
        run_id: str,
        candidate_id: str | None = None,
    ) -> list[ScheduleTask]:
        return load_schedule_tasks(self.run_dir(run_id), candidate_id)

    def lock_run(self, run_id: str) -> AbstractContextManager[None]:
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def read_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(load_json(self.run_dir(run_id) / "run.json"))

    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec:
        return load_frozen_spec(
            self.root_dir / "specs" / frozen_spec_id / "frozen_spec.json"
        )

    def read_context_candidate(
        self, run_id: str, candidate_id: str
    ) -> SchedulerCandidateContext:
        return self._context_record(self.read_candidate(run_id, candidate_id))

    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord:
        return CandidateRecord.model_validate(
            load_json(
                candidate_state_dir(self.run_dir(run_id), candidate_id)
                / "candidate.json"
            )
        )

    def read_candidates(self, run_id: str) -> list[CandidateRecord]:
        return [
            CandidateRecord.model_validate(load_json(path))
            for path in sorted(self.run_dir(run_id).glob("candidates/*/candidate.json"))
        ]

    def read_context_candidates(self, run_id: str) -> list[SchedulerCandidateContext]:
        return [self._context_record(record) for record in self.read_candidates(run_id)]

    def write_candidate_index(self, run_id: str, next_candidate_index: int) -> None:
        run = self.read_run(run_id)
        run.next_candidate_index = next_candidate_index
        write_json(self.run_dir(run_id) / "run.json", run.model_dump(mode="json"))

    def write_candidate_projection(
        self, run_id: str, projection: SchedulerCandidateProjection
    ) -> None:
        record = self.read_candidate(run_id, projection.candidate_id)
        iterations = {item.iteration: item for item in record.iterations}
        for projected in projection.iterations:
            iteration = iterations[projected.iteration]
            iteration.schedule_task_id = projected.schedule_task_id
            iteration.scheduler_status = projected.scheduler_status
            iteration.scheduler_error = projected.scheduler_error
        record.schedule_settlement_watermark = projection.settlement_watermark
        if projection.report is not None and record.score_report is not None:
            record.score_report.schedule_task_id = projection.report.schedule_task_id
            record.score_report.scheduler_status = projection.report.scheduler_status
        write_json(
            candidate_state_dir(self.run_dir(run_id), projection.candidate_id)
            / "candidate.json",
            record.model_dump(mode="json"),
        )
        write_json(
            candidate_state_dir(self.run_dir(run_id), projection.candidate_id)
            / "task.json",
            record.task.model_dump(mode="json"),
        )

    @staticmethod
    def _context_record(record: CandidateRecord) -> SchedulerCandidateContext:
        return SchedulerCandidateContext(
            candidate_id=record.candidate_id,
            workspace=record.task.workspace,
            iterations=record.iterations,
            allocation_eligibility=record.allocation_eligibility,
            allocation_depth=record.task.allocation_depth,
            expansion_source=record.task.expansion_source,
        )

    def attempt_monitor_reference(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
        attempt: int,
    ) -> str:
        safe_candidate = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in candidate_id
        )
        return str(
            Path("runs")
            / run_id
            / "scheduler"
            / "attempts"
            / f"{safe_candidate}-iteration-{iteration:04d}-attempt-{attempt:02d}.json"
        )
