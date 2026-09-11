"""Persistence required by candidate settlement and checkpoint projections."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from goal_plus.domain.evidence import BestArtifactRecord
from goal_plus.ports.candidate_creation import CandidateCreationState
from goal_plus.ports.state import CandidateRecord


@dataclass(frozen=True)
class InnerGitSettlement:
    attempt_git_head: str
    baseline_git_head: str
    ledger_git_head: str
    result_count: int
    restored_to_git_head: str | None


class CandidateSettlementState(CandidateCreationState, Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def lock_verifier(
        self, run_id: str, candidate_id: str, *, blocking: bool = True
    ) -> AbstractContextManager[None]: ...
    def relative_workspace(self, run_id: str, record: CandidateRecord) -> str: ...
    def write_best(self, best: BestArtifactRecord) -> None: ...
