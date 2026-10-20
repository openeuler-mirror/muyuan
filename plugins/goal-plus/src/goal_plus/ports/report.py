"""Bounded report inputs for one Search task, captured before rendering."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from goal_plus.domain.adaptive.models import (
    AllocationDecision,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec, SearchPlan
from goal_plus.domain.verifier import VerifierRecord, VerifierTask
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class SearchVerifierViews:
    verifier_tasks: tuple[VerifierTask, ...]
    verifier_records: tuple[VerifierRecord, ...]
    allocation_decisions: tuple[AllocationDecision, ...]


@dataclass(frozen=True)
class ReportReferenceImpact:
    requester_candidate_id: str
    requester_agent_session_id: str
    reference_event_id: str
    opened_at: str
    source_candidate_id: str
    source_iteration: int
    source_artifact_ref: ArtifactRef
    source_summary: str | None
    preceding_iteration: int | None
    preceding_score: float | None
    subsequent_iteration: int | None
    subsequent_score: float | None
    subsequent_artifact_ref: ArtifactRef | None
    subsequent_summary: str | None
    settlement_event_id: str | None
    compared_files: tuple[str, ...]
    comparison_state: Literal[
        "exact_editable_content",
        "different_editable_content",
        "unavailable",
    ]
    added_lines: int | None
    deleted_lines: int | None


@dataclass(frozen=True)
class ReportTaskInputs:
    run: RunRecord
    frozen: FrozenSpec
    candidate_specs: dict[str, FrozenSpec]
    frozen_specs: dict[str, FrozenSpec]
    plans: list[SearchPlan]
    candidates: list[CandidateRecord]
    sessions: list[AgentSessionRecord]
    observations: dict[str, dict[str, Any]]
    verifier_views: SearchVerifierViews | None
    iterations: dict[tuple[str, int], dict[str, Any]]
    reference_impacts: list[ReportReferenceImpact]


@dataclass(frozen=True)
class MarkdownReportInputs:
    run: RunRecord
    frozen: FrozenSpec
    candidate_specs: dict[str, FrozenSpec]
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
    def write_bundle(self, run_id: str) -> Path: ...
