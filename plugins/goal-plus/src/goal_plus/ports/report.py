"""Bounded report inputs for one Search task, captured before rendering."""

from dataclasses import dataclass
from typing import Any, Protocol
from pathlib import Path

from goal_plus.domain.search import AgentSessionRecord, FrozenSpec, SearchPlan
from goal_plus.domain.adaptive.models import (
    AllocationDecision,
    ScheduleTask,
    SchedulerRewardRecord,
)
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.domain.evidence import ResultLedgerEntry


@dataclass(frozen=True)
class SearchSchedulerViews:
    schedule_tasks: tuple[ScheduleTask, ...]
    reward_records: tuple[SchedulerRewardRecord, ...]
    allocation_decisions: tuple[AllocationDecision, ...]


@dataclass(frozen=True)
class ReportTaskInputs:
    run: RunRecord
    frozen: FrozenSpec
    plans: list[SearchPlan]
    candidates: list[CandidateRecord]
    sessions: list[AgentSessionRecord]
    observations: dict[str, dict[str, Any]]
    scheduler_views: SearchSchedulerViews | None
    iterations: dict[tuple[str, int], dict[str, Any]]


@dataclass(frozen=True)
class MarkdownReportInputs:
    run: RunRecord
    frozen: FrozenSpec
    candidates: list[CandidateRecord]
    plans: list[SearchPlan]
    sessions: list[AgentSessionRecord]
    path: Path
    history: dict[str, dict[str, Any]]
    ledger_projections: dict[str, str]
    ledgers: dict[str, list[ResultLedgerEntry]]
    existing_ledgers: set[Path]
    handles: dict[str, str]


@dataclass(frozen=True)
class LinkedReportGoal:
    goal_plus_id: str
    status: str
    control_state: str | None


class ReportQueries(Protocol):
    def linked_goals(self, run_id: str) -> list[LinkedReportGoal]: ...
    def markdown_inputs(self, run_id: str) -> MarkdownReportInputs: ...
    def read_run(self, run_id: str) -> RunRecord: ...
    def markdown_exists(self, run_id: str) -> bool: ...


class ReportOutput(Protocol):
    def write_markdown(
        self, inputs: MarkdownReportInputs, lifecycle: RunRecord | None = None
    ) -> Path: ...
    def write_html(self, run_id: str) -> None: ...
