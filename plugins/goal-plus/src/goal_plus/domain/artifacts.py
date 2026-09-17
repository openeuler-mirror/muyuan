"""Pure contracts and structural invariants."""

from __future__ import annotations

from pathlib import (
    Path,
    PurePosixPath,
)
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from goal_plus.domain.base import SearchModel

WorkspaceProvider = Literal["copy", "git_worktree", "thinkthread"]


ArtifactKind = Literal["git_commit", "fs_snapshot"]


WorkspaceAccessKind = Literal["soft_path", "protected_execution"]


ExecutionScope = Literal["native_root", "thinkthread_private"]


WorkspaceOperationOutcome = Literal["succeeded", "failed"]


class PublicationDependency(SearchModel):
    path: str = Field(min_length=1)
    scope: Literal["content", "entries", "tree_entries", "tree_content"]

    @field_validator("path")
    @classmethod
    def path_is_snapshot_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("publication dependency path must be snapshot-relative")
        return path.as_posix()


class WorkspaceSpec(SearchModel):
    provider: WorkspaceProvider = "git_worktree"
    promotion_mode: Literal["apply", "artifact_only"] = Field(
        default="apply", description="copy requires artifact_only; other providers default to apply.",
    )
    dependencies: list[PublicationDependency] = Field(
        default_factory=list,
        max_length=256,
        exclude_if=lambda value: not value,
        description="ThinkThread publication dependencies only; omit for git_worktree/copy. Not a skill/file include list.",
    )

    @model_validator(mode="before")
    @classmethod
    def copy_backend_defaults_to_artifact_only(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if value.get("provider") != "copy" or "promotion_mode" in value:
            return value
        payload = dict(value)
        payload["promotion_mode"] = "artifact_only"
        return payload

    @model_validator(mode="after")
    def copy_backend_cannot_apply_provider_native_promotion(self) -> "WorkspaceSpec":
        if self.provider == "copy" and self.promotion_mode == "apply":
            raise ValueError(
                "workspace provider 'copy' requires promotion_mode='artifact_only'"
            )
        if self.provider != "thinkthread" and self.dependencies:
            raise ValueError(
                "workspace publication dependencies require provider='thinkthread'"
            )
        identities = [(item.path, item.scope) for item in self.dependencies]
        if len(identities) != len(set(identities)):
            raise ValueError("workspace publication dependencies must be unique")
        self.dependencies.sort(key=lambda item: (item.path, item.scope))
        return self


class WorkspaceCapacityEstimate(SearchModel):
    """Pre-materialization storage estimate for one frozen SearchSpec."""

    source_path: str
    workspace_provider: WorkspaceProvider
    basis: Literal[
        "git_tracked_checkout",
        "synthetic_git_checkout",
        "source_tree_copy",
        "thinkthread_remote",
    ]
    worktree_nodes: int = Field(ge=1)
    source_scope_files: int = Field(ge=0)
    source_scope_bytes: int = Field(ge=0)
    materialized_source_files: int = Field(ge=0)
    materialized_source_bytes: int = Field(ge=0)
    checkout_scope_path: str | None = None
    checkout_files_per_node: int | None = Field(default=None, ge=0)
    checkout_bytes_per_node: int | None = Field(default=None, ge=0)
    candidate_checkouts_bytes: int | None = Field(default=None, ge=0)
    shared_object_store_bytes: int | None = Field(default=None, ge=0)
    estimated_incremental_bytes: int | None = Field(default=None, ge=0)
    conservative_upper_bound_bytes: int | None = Field(default=None, ge=0)
    available_bytes: int | None = Field(default=None, ge=0)
    fits_available_space: bool | None = None
    notes: list[str] = Field(default_factory=list)


class ArtifactRef(SearchModel):
    """Provider-neutral identity for one immutable candidate artifact."""

    kind: ArtifactKind
    id: str = Field(min_length=1)
    provider: WorkspaceProvider

    @model_validator(mode="after")
    def kind_matches_backend(self) -> "ArtifactRef":
        expected = "fs_snapshot" if self.provider == "thinkthread" else "git_commit"
        if self.kind != expected:
            raise ValueError(
                f"artifact kind {self.kind} is incompatible with {self.provider}; "
                f"expected {expected}"
            )
        return self


class WorkspaceAccess(SearchModel):
    logical_root: str = "workspace"
    native_cwd: Path | None = None
    bootstrap_required: bool = True
    confinement: WorkspaceAccessKind = "soft_path"
    writable_surfaces: list[str] = Field(default_factory=list)
    managed_root: Path | None = None
    source_subdir: str = "."
    provider_handle: dict[str, Any] = Field(default_factory=dict)


class WorkspaceOperationTiming(SearchModel):
    """Durable provider-neutral timing for one workspace operation."""

    started_at: str
    completed_at: str
    duration_seconds: float = Field(ge=0)
    outcome: WorkspaceOperationOutcome


class ReferenceWorkspaceAccess(SearchModel):
    kind: Literal["filesystem_path", "provider_tools"]
    root: Path | None = None
    readonly_contract: Literal["soft_detected", "enforced"]
    provider_handle: dict[str, Any] = Field(default_factory=dict)


class ReferenceWorkspaceRecord(SearchModel):
    reference_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    requester_agent_session_id: str = Field(min_length=1)
    source_candidate_id: str = Field(min_length=1)
    source_iteration: int = Field(ge=1)
    artifact_ref: ArtifactRef
    display_name: str = Field(min_length=1)
    workspace_provider: WorkspaceProvider
    access: ReferenceWorkspaceAccess
    state: Literal["open", "closed"] = "open"
    integrity_status: Literal["clean", "contaminated", "unavailable"] = "clean"
    created_at: str
    expires_at: str | None = None
    closed_at: str | None = None
