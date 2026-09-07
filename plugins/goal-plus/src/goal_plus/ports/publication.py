"""Publication persistence and provider operation results."""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, Any, Literal, Mapping
from dataclasses import dataclass
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceBackend
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.evidence import ScoreReport
from goal_plus.ports.state import RunRecord, CandidateRecord
from goal_plus.domain.promotion import MainAgentNotification, PendingIntegrationRecord


class PromotionState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def write_candidate(self, run_id: str, record: CandidateRecord) -> None: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...


class ThinkThreadPublicationState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def write_candidate(self, run_id: str, record: CandidateRecord) -> None: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def publication_path(self, run_id: str, candidate_id: str) -> Path: ...


class PublicationApplyState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def write_pending(self, record: PendingIntegrationRecord) -> Path: ...


class PublicationNotificationState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def read_notification(
        self, run_id: str, revision: int
    ) -> MainAgentNotification | None: ...
    def read_notifications(self, run_id: str) -> list[MainAgentNotification]: ...
    def write_notification(self, notification: MainAgentNotification) -> None: ...
    def report_path(self, run_id: str) -> Path: ...


@dataclass(frozen=True)
class PreparedPromotion:
    provider: WorkspaceBackend
    strategy: Literal["legacy_patch", "squash_commit", "fs_apply"]
    base_artifact_ref: ArtifactRef
    selected_artifact_ref: ArtifactRef
    publication_artifact_ref: ArtifactRef | None
    artifact_path: Path | None
    source_repository: Path | None
    source_subdir: str
    changed_files: tuple[str, ...]
    provider_handle: Mapping[str, object]


@dataclass(frozen=True)
class PromotionApplyResult:
    source_revision: str | None
    details: Mapping[str, object]


@dataclass(frozen=True)
class PublicationChange:
    kind: Literal["remove_directory", "remove_file", "make_directory", "put_file"]
    path: str
    mode: int | None = None
    content: bytes | None = None


@dataclass(frozen=True)
class ThinkThreadPublicationPlan:
    base_artifact_ref: ArtifactRef
    selected_artifact_ref: ArtifactRef
    source_subdir: str
    changed_files: tuple[str, ...]
    changes: tuple[PublicationChange, ...]
    dependencies: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class PublicationSnapshotIntent:
    run_id: str
    candidate_id: str
    root_thinkthread_id: str
    plan: ThinkThreadPublicationPlan


@dataclass(frozen=True)
class PublicationSnapshotResult:
    request_id: str
    artifact_ref: ArtifactRef
    owner_thinkthread_id: str


@dataclass(frozen=True)
class PublicationApplyIntent:
    run_id: str
    publication_revision: int
    base_artifact_ref: ArtifactRef
    target_artifact_ref: ArtifactRef
    dependencies: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class PublicationRequest:
    request_id: str
    identity: Mapping[str, Any]


@dataclass(frozen=True)
class PublicationApplyObservation:
    state: Literal["applied", "conflict", "policy_rejected", "failed", "needs_recovery"]
    request_id: str
    base_artifact_ref: ArtifactRef | None = None
    target_artifact_ref: ArtifactRef | None = None
    details: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None


class LocalPublicationPreparation(Protocol):
    def patch_path(self, run_id: str, candidate_id: str) -> Path: ...
    def native_promotion(self, run: RunRecord, record: CandidateRecord) -> bool: ...
    def prepare_local(
        self,
        run: RunRecord,
        record: CandidateRecord,
        selected: ArtifactRef,
        changed: list[str],
    ) -> PreparedPromotion: ...
    def write_patch(
        self,
        source: Path,
        workspace: Path,
        selected_revision: str,
        changed_files: list[str],
        patch_path: Path,
    ) -> None: ...
    def apply_coordinates(self, source_path: Path) -> tuple[Path, Path, str]: ...


class LocalPublicationApply(Protocol):
    def apply_local(self, prepared: PreparedPromotion) -> PromotionApplyResult: ...


class ThinkThreadPublicationArtifacts(Protocol):
    def plan_thinkthread(
        self,
        *,
        base_artifact_ref: ArtifactRef,
        selected_artifact_ref: ArtifactRef,
        source_subdir: str,
        changed_files: list[str],
        dependencies: list[dict[str, Any]],
    ) -> ThinkThreadPublicationPlan: ...
    def write_manifest(
        self,
        path: Path,
        *,
        run_id: str,
        candidate_id: str,
        plan: ThinkThreadPublicationPlan,
        publication_request_id: str | None,
        state: str,
        publication_snapshot_id: str | None = None,
        apply_request_id: str | None = None,
    ) -> None: ...
    def prepare_snapshot(self, intent: PublicationSnapshotIntent) -> PublicationRequest: ...
    def materialize_snapshot(self, intent: PublicationSnapshotIntent) -> PublicationSnapshotResult: ...
    def canonical_digest(self, base: ArtifactRef, target: ArtifactRef) -> str: ...


class PublicationVerification(Protocol):
    def verify_publication(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str,
        artifact_ref: ArtifactRef,
        iteration: int,
    ) -> ScoreReport: ...


class ThinkThreadPublicationApply(Protocol):
    def describe_apply(self, intent: PublicationApplyIntent) -> PublicationRequest: ...
    def prepare_apply(self, intent: PublicationApplyIntent) -> PublicationRequest: ...
    def apply(self, intent: PublicationApplyIntent) -> PublicationApplyObservation: ...
