"""Checkpoint restoration I/O and durable host approval observations."""

from typing import Any, Protocol
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import CandidateRecovery
from goal_plus.ports.state import CandidateRecord


class CandidateRecoveryWorkspace(Protocol):
    def new_recovery_id(self) -> str: ...
    def require_recoverable(self, record: CandidateRecord) -> None: ...
    def read_approval(self, recovery_id: str) -> dict[str, Any]: ...
    def restore(
        self,
        record: CandidateRecord,
        recovery: CandidateRecovery,
        preserved_files: dict[str, str],
    ) -> ArtifactRef: ...
    def matches_checkpoint(
        self, record: CandidateRecord, expected: ArtifactRef, restored: ArtifactRef
    ) -> bool: ...
    def has_unsettled_changes(
        self, record: CandidateRecord, expected: ArtifactRef
    ) -> bool | None:
        """Return None when live workspace inspection is unavailable."""
        ...
