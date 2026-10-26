"""Pure contracts and structural invariants."""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    model_validator,
)

from goal_plus.domain.artifacts import (
    ArtifactRef,
    WorkspaceProvider,
    WorkspaceOperationTiming,
)
from goal_plus.domain.base import SearchModel

PromotionState = Literal[
    "prepared",
    "applying",
    "applied",
    "awaiting_main_integration",
    "recovering",
    "apply_failed",
]


PromotionStrategy = Literal["legacy_patch", "squash_commit", "fs_apply"]


SearchResultState = Literal["unknown", "passed", "failed"]


PublicationState = Literal[
    "unsupported",
    "prepared",
    "applying",
    "applied",
    "awaiting_main_integration",
    "recovering",
    "failed",
]


PublicationApplyOutcome = Literal[
    "unknown",
    "applied",
    "conflict",
    "policy_rejected",
    "needs_recovery",
    "failed",
]


NotificationState = Literal["not_applicable", "pending", "delivered"]


class PublicationRecoveryRecord(SearchModel):
    request_id: str = Field(min_length=1)
    state: Literal["pending", "resolved"] = "pending"
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None
    updated_at: str


class MainAgentNotification(SearchModel):
    notification_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    publication_revision: int = Field(ge=1)
    kind: Literal["publication_applied", "publication_pending_integration"]
    state: NotificationState = "pending"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    delivered_at: str | None = None


class PublicationOutcome(SearchModel):
    """Durable result distinct from whether Search found a passing candidate."""

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    state: PublicationState = "unsupported"
    base_artifact_ref: ArtifactRef | None = None
    selected_artifact_ref: ArtifactRef | None = None
    publication_artifact_ref: ArtifactRef | None = None
    apply_request_id: str | None = Field(default=None, min_length=1)
    apply_outcome: PublicationApplyOutcome = "unknown"
    changed_paths: list[str] = Field(default_factory=list)
    dependency_summary: dict[str, Any] = Field(default_factory=dict)
    pending_artifact_path: Path | None = None
    notification_state: NotificationState = "not_applicable"
    recovery: PublicationRecoveryRecord | None = None
    provider_error: dict[str, Any] = Field(default_factory=dict)
    updated_at: str

    @model_validator(mode="after")
    def state_has_consistent_provider_outcome(self) -> "PublicationOutcome":
        refs = (
            self.base_artifact_ref,
            self.selected_artifact_ref,
            self.publication_artifact_ref,
        )
        if self.state != "unsupported":
            if any(reference is None for reference in refs):
                raise ValueError(
                    "supported publication state requires base, selected, and "
                    "publication artifact references"
                )
            if any(
                reference is not None and reference.provider != "thinkthread"
                for reference in refs
            ):
                raise ValueError(
                    "ThinkThread publication references must use the thinkthread provider"
                )
        if self.state == "applied" and self.apply_outcome != "applied":
            raise ValueError("applied publication requires apply_outcome='applied'")
        if self.state == "awaiting_main_integration":
            if self.apply_outcome not in {"conflict", "policy_rejected", "failed"}:
                raise ValueError(
                    "pending integration requires a confirmed non-applied outcome"
                )
            if self.pending_artifact_path is None:
                raise ValueError(
                    "pending integration requires a durable artifact path"
                )
        if self.state == "recovering":
            if self.apply_outcome != "needs_recovery" or self.recovery is None:
                raise ValueError(
                    "recovering publication requires needs_recovery and recovery state"
                )
            if self.notification_state != "not_applicable":
                raise ValueError("recovering publication cannot queue a notification")
        return self


class PendingIntegrationRecord(SearchModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    status: Literal["awaiting_main_integration"] = "awaiting_main_integration"
    provider: Literal["thinkthread"] = "thinkthread"
    base_artifact_ref: ArtifactRef
    selected_artifact_ref: ArtifactRef
    publication_artifact_ref: ArtifactRef
    apply_request_id: str = Field(min_length=1)
    apply_outcome: Literal["conflict", "policy_rejected", "failed"]
    changed_paths: list[str] = Field(default_factory=list)
    dependency_summary: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    provider_error: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @model_validator(mode="after")
    def artifacts_are_authoritative_snapshots(self) -> "PendingIntegrationRecord":
        for reference in (
            self.base_artifact_ref,
            self.selected_artifact_ref,
            self.publication_artifact_ref,
        ):
            if reference.kind != "fs_snapshot" or reference.provider != self.provider:
                raise ValueError(
                    "pending integration requires ThinkThread fs_snapshot artifacts"
                )
        return self


class PromotionEvidence(SearchModel):
    frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    candidate_id: str
    selected_git_head: str | None = None
    git_head: str | None = None
    selected_artifact_ref: ArtifactRef | None = None
    artifact_ref: ArtifactRef | None = None
    artifact_hash: str
    passed: bool
    created_at: str


class PromotionRecord(SearchModel):
    frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    """Prepared provider-native publication and its explicit apply result."""

    promotion_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    provider: WorkspaceProvider
    strategy: PromotionStrategy
    promotion_mode: Literal["apply", "artifact_only"] = "apply"
    state: PromotionState = "prepared"
    base_artifact_ref: ArtifactRef
    selected_artifact_ref: ArtifactRef
    publication_artifact_ref: ArtifactRef | None = None
    artifact_path: Path | None = None
    source_repository: Path | None = None
    source_subdir: str = "."
    changed_files: list[str] = Field(default_factory=list)
    provider_handle: dict[str, Any] = Field(default_factory=dict)
    apply_request_id: str | None = None
    applied_source_revision: str | None = None
    apply_result: dict[str, Any] = Field(default_factory=dict)
    apply_timing: WorkspaceOperationTiming | None = None
    error: str | None = None
    created_at: str
    applied_at: str | None = None

    @model_validator(mode="after")
    def strategy_matches_provider(self) -> "PromotionRecord":
        if self.strategy == "squash_commit" and self.provider != "git_worktree":
            raise ValueError("squash_commit promotion requires git_worktree")
        if self.strategy == "fs_apply" and self.provider != "thinkthread":
            raise ValueError("fs_apply promotion requires thinkthread")
        if self.base_artifact_ref.provider != self.provider:
            raise ValueError("promotion base artifact provider must match promotion")
        if self.selected_artifact_ref.provider != self.provider:
            raise ValueError(
                "promotion selected artifact provider must match promotion"
            )
        if self.strategy != "legacy_patch":
            publication = self.publication_artifact_ref
            if publication is None or publication.provider != self.provider:
                raise ValueError(
                    "provider-native promotion requires a matching publication artifact"
                )
        return self
