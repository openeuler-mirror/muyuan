"""Provider materialization, integrity observation and reference removal."""

from pathlib import Path
from goal_plus.domain.artifacts import ArtifactRef, ReferenceWorkspaceRecord
from goal_plus.ports.state import CandidateRecord
from goal_plus.workspaces import GitReferences


class ProviderReferenceWorkspace:
    def remove(
        self, source: CandidateRecord, reference: ReferenceWorkspaceRecord
    ) -> tuple[bool, bool]:
        root = reference.access.root
        available = root is not None and root.is_dir()
        clean = False
        if root is not None:
            clean = GitReferences(
                reference.workspace_provider
            ).remove_reference(source.task.workspace, reference.artifact_ref, root)
        return available, clean

    def materialize(
        self, source: CandidateRecord, artifact: ArtifactRef, destination: Path
    ) -> Path:
        return GitReferences(
            source.task.workspace_provider
        ).materialize_reference(source.task.workspace, artifact, destination)
