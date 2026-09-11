"""Deterministic candidate verifier execution and immutable input checks."""

from pathlib import Path
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol
from goal_plus.domain.search import CandidateTask, FrozenSpec, VerifierCommand
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import ScoreReport
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class CandidateVerificationRequest:
    run_id: str
    candidate_id: str
    scope: Literal["process", "promotion"] = "process"


class CandidateVerifiers(Protocol):
    termination_grace_seconds: float

    def workspace(self, record: CandidateRecord, artifact: ArtifactRef) -> AbstractContextManager[CandidateTask]: ...

    def frozen_hash_failures(
        self, frozen: FrozenSpec, workspace: Path
    ) -> dict[str, dict[str, str | None]]: ...
    def run_commands(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        commands: list[VerifierCommand],
        scope: Literal["process", "promotion"],
    ) -> ScoreReport: ...
