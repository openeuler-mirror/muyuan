"""File-backed LLM Verifier tasks and immutable output records."""

from contextlib import AbstractContextManager
from pathlib import Path

from goal_plus.domain.verifier import (
    VerifierRecord,
    VerifierTask,
    VerifierTaskState,
)
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json


class FileVerifierState:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def lock_verifier_queue(self) -> AbstractContextManager[None]:
        return exclusive_file_lock(self.root_dir / "verifier-queue.lock")

    def lock_run(self, run_id: str) -> AbstractContextManager[None]:
        return exclusive_file_lock(self.run_dir(run_id) / "verifier" / "verifier.lock")

    def _task_path(self, run_id: str, task_id: str) -> Path:
        return self.run_dir(run_id) / "verifier" / "tasks" / f"{task_id}.json"

    def _record_path(self, run_id: str, record_id: str) -> Path:
        return self.run_dir(run_id) / "verifier" / "records" / f"{record_id}.json"

    def load_verifier_task(self, task_id: str) -> VerifierTask:
        paths = list(self.root_dir.glob(f"runs/*/verifier/tasks/{task_id}.json"))
        if len(paths) != 1:
            raise FileNotFoundError(f"verifier task not found: {task_id}")
        return VerifierTask.model_validate(load_json(paths[0]))

    def load_verifier_tasks(
        self,
        run_id: str | None = None,
        *,
        candidate_id: str | None = None,
        state: VerifierTaskState | None = None,
    ) -> list[VerifierTask]:
        pattern = (
            f"runs/{run_id}/verifier/tasks/verifier_*.json"
            if run_id is not None
            else "runs/*/verifier/tasks/verifier_*.json"
        )
        tasks = [
            VerifierTask.model_validate(load_json(path))
            for path in sorted(self.root_dir.glob(pattern))
        ]
        filtered = [
            task
            for task in tasks
            if (candidate_id is None or task.candidate_id == candidate_id)
            and (state is None or task.state == state)
        ]
        return sorted(
            filtered,
            key=lambda task: (
                task.run_id,
                task.queue_sequence,
                task.task_id,
            ),
        )

    def write_verifier_task(self, task: VerifierTask) -> None:
        write_json(
            self._task_path(task.run_id, task.task_id),
            task.model_dump(mode="json"),
        )

    def load_verifier_records(
        self,
        run_id: str,
        *,
        candidate_id: str | None = None,
        iteration: int | None = None,
        settlement_id: str | None = None,
    ) -> list[VerifierRecord]:
        records = [
            VerifierRecord.model_validate(load_json(path))
            for path in sorted(
                (self.run_dir(run_id) / "verifier" / "records").glob("record_*.json")
            )
        ]
        filtered = [
            record
            for record in records
            if (candidate_id is None or record.subject.candidate_id == candidate_id)
            and (iteration is None or record.subject.iteration == iteration)
            and (
                settlement_id is None
                or record.subject.settlement_id == settlement_id
            )
        ]
        return sorted(
            filtered,
            key=lambda record: (
                record.subject.candidate_id,
                record.subject.iteration,
                record.created_at,
                record.record_id,
            ),
        )

    def write_verifier_record(self, record: VerifierRecord) -> None:
        path = self._record_path(record.subject.run_id, record.record_id)
        if path.exists():
            existing = VerifierRecord.model_validate(load_json(path))
            if existing != record:
                raise RuntimeError("immutable verifier record already exists with other data")
            return
        write_json(path, record.model_dump(mode="json"))

__all__ = ["FileVerifierState"]
