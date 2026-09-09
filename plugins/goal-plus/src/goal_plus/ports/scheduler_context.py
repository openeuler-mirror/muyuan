"""Settled Scheduler inputs and model/execution projections, without I/O."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, Protocol, TypedDict

from goal_plus.domain.adaptive.models import AllocationEligibility, ExpansionSource
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.scheduler import SchedulerRunSource

# Existing Scheduler input limit; an incomplete diff cannot be accepted as exact.
MAX_SCHEDULER_DIFF_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SchedulerCandidateContext:
    candidate_id: str
    workspace: Path
    iterations: list[IterationRecord]
    allocation_eligibility: AllocationEligibility
    allocation_depth: int
    expansion_source: ExpansionSource | None


class SchedulerContextSource(SchedulerRunSource, Protocol):
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_context_candidate(
        self, run_id: str, candidate_id: str
    ) -> SchedulerCandidateContext: ...
    def read_context_candidates(
        self, run_id: str
    ) -> list[SchedulerCandidateContext]: ...


class SchedulerEvidenceContext(TypedDict):
    identity: dict[str, str | int]
    objective: str
    task_context: str
    task_context_source: str
    task_context_ref: str | None
    current_attempt: dict[str, Any]
    public_result: dict[str, Any]
    qualitative_evidence: dict[str, Any]
    input_limitations: list[str]


class SchedulerExecutionContext(TypedDict):
    scheduler: dict[str, Any]
    outer_deadline_at: str | None
    runtime_root: str
    monitor_path: NotRequired[str]
    attempt: NotRequired[int]


class SchedulerContext(TypedDict):
    scheduler_context: SchedulerEvidenceContext
    reward_context: dict[str, Any]
    allocation_context: dict[str, Any]
    _execution: SchedulerExecutionContext
