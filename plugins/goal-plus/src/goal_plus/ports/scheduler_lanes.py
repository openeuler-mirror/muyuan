"""Scheduler-owned candidate fields; stores cannot replace hard settlements."""

from dataclasses import dataclass
from typing import Protocol

from goal_plus.domain.adaptive.models import ScheduleTaskState
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.scheduler import SchedulerRunSource
from goal_plus.ports.state import CandidateRecord


@dataclass(frozen=True)
class SchedulerIterationProjection:
    iteration: int
    schedule_task_id: str | None
    scheduler_status: ScheduleTaskState | None
    scheduler_error: str | None


@dataclass(frozen=True)
class SchedulerReportProjection:
    schedule_task_id: str | None
    scheduler_status: ScheduleTaskState | None


@dataclass(frozen=True)
class SchedulerCandidateProjection:
    candidate_id: str
    iterations: tuple[SchedulerIterationProjection, ...]
    settlement_watermark: int
    report: SchedulerReportProjection | None


class SchedulerLaneState(SchedulerRunSource, Protocol):
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def write_candidate_projection(
        self, run_id: str, projection: SchedulerCandidateProjection
    ) -> None: ...
    def write_candidate_index(self, run_id: str, next_candidate_index: int) -> None: ...
