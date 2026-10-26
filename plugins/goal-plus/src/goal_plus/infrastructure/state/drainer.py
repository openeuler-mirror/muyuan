"""The existing per-role worker document and locks; no domain task access."""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.ports.drainer import DrainerRecord


class FileDrainerState:
    def __init__(self, directory: Path, log_name: str) -> None:
        self.directory = directory
        self.log_name = log_name

    def worker_lock(self):
        return exclusive_file_lock(self.directory / "worker.lock")

    def drain_lock(self):
        return exclusive_file_lock(self.directory / "drain.lock")

    def read_worker(self) -> DrainerRecord | None:
        try:
            return DrainerRecord.model_validate(load_json(self.directory / "worker.json"))
        except FileNotFoundError:
            return None

    def write_worker(self, worker: DrainerRecord) -> None:
        write_json(self.directory / "worker.json", worker.model_dump(mode="json", exclude_none=True))

    def remove_worker(self) -> None:
        (self.directory / "worker.json").unlink(missing_ok=True)

    @contextmanager
    def open_log(self) -> Iterator[TextIO]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / self.log_name).open("a", encoding="utf-8") as handle:
            yield handle
