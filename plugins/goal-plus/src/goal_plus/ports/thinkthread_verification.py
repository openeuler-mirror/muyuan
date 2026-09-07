"""Exact provider-backed verification I/O without state transitions."""

from dataclasses import dataclass
from typing import Literal, Protocol
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import IterationDisposition, ScoreReport
from goal_plus.domain.search import FrozenSpec, VerifierCommand
from goal_plus.ports.state import RunRecord
from goal_plus.ports.candidate_settlement import InnerGitSettlement


@dataclass(frozen=True)
class AttemptChildObservation:
    thinkthread_id: str
    parent_thinkthread_id: str
    branch_id: str
    ready: bool
    pending_wake: bool


@dataclass(frozen=True)
class AttemptSnapshot:
    artifact_ref: ArtifactRef
    branch_id: str
    request_id: str


@dataclass(frozen=True)
class InnerGitSettlementResult:
    artifact_ref: ArtifactRef
    request_id: str
    settlement: InnerGitSettlement


class ThinkThreadAttemptIO(Protocol):
    def read_child(self, thinkthread_id: str) -> AttemptChildObservation: ...
    def changed_files(self, base: ArtifactRef, target: ArtifactRef) -> list[str]: ...
    def canonical_digest(self, base: ArtifactRef, target: ArtifactRef) -> str: ...
    def capture(
        self,
        *,
        run_id: str,
        candidate_id: str,
        branch_id: str,
        iteration: int,
        attempt_id: str,
        recovery_deadline: float | None,
    ) -> AttemptSnapshot: ...
    def run_commands(
        self,
        *,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str,
        artifact_ref: ArtifactRef,
        commands: list[VerifierCommand],
        phase: Literal["candidate"],
        iteration: int,
        recovery_deadline: float | None,
    ) -> ScoreReport: ...
    def settle_inner_git(
        self,
        *,
        run_id: str,
        candidate_id: str,
        attempt_id: str,
        attempt_artifact_ref: ArtifactRef,
        source_relative_path: str,
        iteration: int,
        metric_name: str,
        score: float | None,
        status: str,
        hypothesis: str,
        disposition: IterationDisposition,
        attempt_git_head: str,
        result_count_before: int,
        restore_git_head: str | None,
        recovery_deadline: float | None,
    ) -> InnerGitSettlementResult: ...
