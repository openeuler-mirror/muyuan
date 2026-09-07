"""Immutable candidate artifact observations and exact workspace operations."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import CandidateTask
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class CandidateArtifactState:
    changed_files: list[str]
    touched_denied_files: bool
    changed_outside_allowed: bool
    artifact_hash: str
    artifact_ref: ArtifactRef | None
    artifact_status: list[str]
    artifact_clean: bool
    git_head: str | None
    git_status: list[str]
    git_artifact_clean: bool


class CandidateArtifactWorkspace(Protocol):
    def candidate_changed_files(
        self,
        run: RunRecord,
        record: CandidateRecord,
        artifact_ref: ArtifactRef | None = None,
    ) -> list[str]: ...
    def current_artifact(self, task: CandidateTask) -> ArtifactRef | None: ...
    def status(
        self, task: CandidateTask, *, ignore_runtime_noise: bool = False
    ) -> list[str]: ...
    def artifact_hash(self, workspace: Path, changed_files: list[str]) -> str: ...
    def commit_workspace_iteration(
        self, record: CandidateRecord, message: str
    ) -> ArtifactRef | None: ...
    def restore(
        self,
        task: CandidateTask,
        artifact: ArtifactRef,
        preserved_files: dict[str, str],
        message: str,
    ) -> ArtifactRef: ...
    def is_descendant(
        self, task: CandidateTask, base: ArtifactRef, target: ArtifactRef
    ) -> bool: ...
    def changed_between(
        self, task: CandidateTask, base: ArtifactRef, target: ArtifactRef
    ) -> list[str]: ...
