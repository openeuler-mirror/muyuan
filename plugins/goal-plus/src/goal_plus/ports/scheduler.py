"""Scheduler task storage and completed inference input, without executors."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from goal_plus.domain.adaptive.models import (
    SchedulerInvocationRecord,
    SchedulerOutput,
    ScheduleTask,
)
from goal_plus.ports.state import RunRecord


@dataclass(frozen=True)
class SchedulerResult:
    output: SchedulerOutput
    usage: dict[str, int | float]
    invocation: SchedulerInvocationRecord


class SchedulerTaskQuery(Protocol):
    def load_schedule_task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> ScheduleTask | None: ...
    def load_schedule_tasks(
        self,
        run_id: str,
        candidate_id: str | None = None,
    ) -> list[ScheduleTask]: ...


class SchedulerTaskState(SchedulerTaskQuery, Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def write_schedule_task(self, task: ScheduleTask) -> None: ...
    def attempt_monitor_reference(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
        attempt: int,
    ) -> str: ...


class SchedulerRunSource(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
