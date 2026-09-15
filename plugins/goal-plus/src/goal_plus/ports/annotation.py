"""Annotation task persistence, settled inputs, and publication contracts."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import Field

from goal_plus.domain.annotation import (
    EvidenceAnnotationQueue,
    EvidenceAnnotationTask,
    EvidenceComparisonReference,
    SupplementalEvaluation,
    ToolViewOutput,
)
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.artifacts import WorkspaceProvider
from goal_plus.domain.base import SearchModel
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.shared_tools import SharedToolRecord
from goal_plus.ports.state import RunRecord


@dataclass(frozen=True)
class EvidenceAnnotationResult:
    description: str
    usage: dict[str, int | float]
    supplemental_evaluation: SupplementalEvaluation | None = None
    comparison_basis: list[EvidenceComparisonReference] | None = None
    tool_views: list[ToolViewOutput] = field(default_factory=list)


@dataclass(frozen=True)
class AnnotationCompletion:
    accepted: bool
    published: bool


@dataclass(frozen=True)
class ThinkThreadAnnotationDispatch:
    baseline_snapshot_id: str
    queue: EvidenceAnnotationQueue
    task: EvidenceAnnotationTask


@dataclass(frozen=True)
class AnnotationCandidateEvidence:
    candidate_id: str
    workspace_provider: WorkspaceProvider
    iterations: list[IterationRecord]


class AnnotationTaskKey(SearchModel):
    candidate_id: str
    iteration: int = Field(ge=1)


class AnnotationAdmissionError(AnnotationTaskKey):
    error: str


class AnnotationAdmissionResult(SearchModel):
    state: Literal["open", "closed"]
    created: list[AnnotationTaskKey]
    errors: list[AnnotationAdmissionError]
    queue: EvidenceAnnotationQueue


class AnnotationAdmissionSource(Protocol):
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate_evidence(
        self, run_id: str
    ) -> list[AnnotationCandidateEvidence]: ...


class AnnotationTaskQuery(Protocol):
    def task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> EvidenceAnnotationTask | None: ...


class AnnotationTaskState(Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def lock_tasks(self, run_id: str) -> AbstractContextManager[None]: ...
    def read_queue(self, run_id: str) -> EvidenceAnnotationQueue | None: ...
    def write_queue(self, queue: EvidenceAnnotationQueue) -> None: ...
    def read_task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> EvidenceAnnotationTask | None: ...
    def write_task(self, task: EvidenceAnnotationTask) -> None: ...
    def list_tasks(self, run_id: str) -> list[EvidenceAnnotationTask]: ...
    def attempt_monitor_reference(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
        attempt: int,
    ) -> str: ...


class AnnotationEvidenceSource(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_iterations(self, run_id: str) -> dict[str, list[IterationRecord]]: ...
    def read_candidate_iterations(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[IterationRecord]: ...


class AnnotationToolFamilies(Protocol):
    def pending_tool_ids(self, run_id: str) -> set[str]: ...
    def advance_heads(self, run_id: str, tools: list[SharedToolRecord]) -> None: ...
    def reject_heads(self, run_id: str, tools: list[SharedToolRecord]) -> None: ...
