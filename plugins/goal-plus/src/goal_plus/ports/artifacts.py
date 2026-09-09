"""Narrow immutable artifact reads, without workspace or worker supervision."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from goal_plus.domain.artifacts import (
    ArtifactRef,
    ReferenceWorkspaceAccess,
    WorkspaceBackend,
)


@dataclass(frozen=True)
class ArtifactComparison:
    workspace: Path
    base: ArtifactRef
    head: ArtifactRef
    source_relative_path: str = "."


@dataclass(frozen=True)
class ArtifactDiff:
    text: str
    truncated: bool = False


class ArtifactReadError(RuntimeError):
    """A provider read failed; the message retains its original diagnostic type."""


class ArtifactReader(Protocol):
    def commit_available(self, workspace: Path, commit: str) -> bool: ...
    def changed_files(self, comparison: ArtifactComparison) -> list[str]: ...
    def diff(
        self,
        comparison: ArtifactComparison,
        *,
        paths: list[str],
        max_bytes: int,
        function_context: bool,
    ) -> ArtifactDiff: ...
    def open_view(
        self,
        workspace: Path,
        artifact: ArtifactRef,
        destination: Path,
        *,
        backend: WorkspaceBackend,
    ) -> ReferenceWorkspaceAccess: ...
