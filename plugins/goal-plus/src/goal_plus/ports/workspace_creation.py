"""Workspace materialization input/output, without candidate state transitions."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from goal_plus.domain.artifacts import (
    ArtifactRef,
    WorkspaceProvider,
    WorkspaceOperationTiming,
)
from goal_plus.domain.search import CandidateTask, FrozenSpec
from goal_plus.ports.candidate_creation import CandidateMaterialization


@dataclass(frozen=True)
class WorkspaceMaterialization:
    workspace_provider: WorkspaceProvider
    workspace: Path
    branch: str | None
    base_revision: str | None
    managed_root: Path | None = None
    source_subdir: str = "."
    provider_handle: Mapping[str, str] | None = None


@dataclass(frozen=True)
class PreparedCandidateWorkspace:
    materialization: WorkspaceMaterialization
    timing: WorkspaceOperationTiming | None


class CandidateWorkspaceCreation(Protocol):
    def requires_relocation(self, workspace_provider: WorkspaceProvider) -> bool: ...
    def current_artifact(self, task: CandidateTask) -> ArtifactRef | None: ...
    def status(self, task: CandidateTask) -> list[str]: ...
    def prepare(
        self, request: CandidateMaterialization
    ) -> PreparedCandidateWorkspace: ...
    def task_skill_paths(self, frozen: FrozenSpec, workspace: Path) -> list[Path]: ...
    def initialize_shared_layout(self, run_id: str, workspace: Path) -> Path: ...
