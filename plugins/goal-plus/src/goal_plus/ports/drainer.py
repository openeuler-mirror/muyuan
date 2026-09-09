"""Detached worker registration, separate from business task and queue state."""

from typing import ContextManager, Literal, Protocol, TextIO

from goal_plus.domain.base import SearchModel
from goal_plus.ports.process_identity import ProcessWitness


class DrainerRecord(SearchModel):
    generation: str
    pid: int
    started_at: str
    witness: ProcessWitness | None = None


class DrainerStopResult(SearchModel):
    generation: str
    pid: int
    started_at: str
    status: Literal["stale", "stopped"]
    finished_at: str | None = None


class DrainerState(Protocol):
    def worker_lock(self) -> ContextManager[None]: ...
    def drain_lock(self) -> ContextManager[None]: ...
    def read_worker(self) -> DrainerRecord | None: ...
    def write_worker(self, worker: DrainerRecord) -> None: ...
    def remove_worker(self) -> None: ...
    def open_log(self) -> ContextManager[TextIO]: ...
