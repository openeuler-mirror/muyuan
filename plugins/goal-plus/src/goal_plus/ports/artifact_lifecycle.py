"""Persisted snapshot references and journaled immutable snapshot removal."""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from goal_plus.ports.state import ProviderMutationRecord, RunRecord, CandidateRecord


@dataclass(frozen=True)
class ExternalSnapshotReferences:
    run_id: str
    run: dict[str, Any]
    candidates: list[dict[str, Any]]


class ArtifactLifecycleState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def external_references(self, run_id: str) -> list[ExternalSnapshotReferences]: ...
    def session_resource_states(self, run_id: str) -> list[tuple[str, str]]: ...
    def annotation_state(self, run_id: str) -> str | None: ...
    def reports_durable(self, run_id: str) -> bool: ...


@dataclass(frozen=True)
class SnapshotRemovalResult:
    state: Literal["removed", "already_absent", "needs_recovery", "failed"]
    request_id: str
    snapshot_id: str
    details: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)


class SnapshotRemoval(Protocol):
    def reconcile_closes(self, run_id: str) -> None: ...
    def prepare_removal(
        self, *, run_id: str, snapshot_id: str
    ) -> ProviderMutationRecord: ...
    def remove(
        self, *, run_id: str, snapshot_id: str
    ) -> SnapshotRemovalResult: ...
