"""Allocation apply state fields and its composed, non-persistent result."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from goal_plus.domain.adaptive.models import AllocationDecision, SearchSnapshot
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import AgentSessionRecord, CandidateTask
from goal_plus.ports.state import CandidateRecord, RunRecord


class AllocationCancellationProof(Protocol):
    def require_accepted(
        self, decision: AllocationDecision, sessions: list[AgentSessionRecord],
    ) -> None: ...


@dataclass(frozen=True)
class AllocationMaterializationProjection:
    candidates_total: int
    source_artifact_ref: ArtifactRef | None
    source_subdir: str
    source_repository: Path | None


@dataclass(frozen=True)
class AllocationApplied:
    decision: AllocationDecision
    candidate_tasks: list[CandidateTask]
    agent_sessions: list[AgentSessionRecord]


class AllocationApplyState(Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def find_candidate(
        self, run_id: str, candidate_id: str
    ) -> CandidateRecord | None: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def read_agent_sessions(self, run_id: str) -> list[AgentSessionRecord]: ...
    def load_allocation_decision(
        self, run_id: str, decision_id: str
    ) -> AllocationDecision: ...
    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]: ...
    def load_search_snapshot(
        self, run_id: str, snapshot_revision: str
    ) -> SearchSnapshot: ...
    def write_allocation_decision(self, decision: AllocationDecision) -> None: ...
    def write_retirement(
        self, run_id: str, candidate_id: str, decision_id: str
    ) -> None: ...
    def write_candidate_index(self, run_id: str, next_candidate_index: int) -> None: ...
    def write_materialization_projection(
        self, run_id: str, projection: AllocationMaterializationProjection
    ) -> None: ...
