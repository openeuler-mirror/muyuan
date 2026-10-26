"""Git/Copy/ThinkThread immutable reads behind the same narrow capability."""

import subprocess
from collections.abc import Callable
from pathlib import Path

from goal_plus.domain.artifact_paths import (
    thinkthread_logical_changed_files,
    thinkthread_snapshot_path,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
    ReferenceWorkspaceAccess,
    WorkspaceProvider,
)
from goal_plus.infrastructure.artifacts.git import (
    git_changed_files,
    git_output_bounded,
    git_returncode,
)
from goal_plus.ports.artifacts import (
    ArtifactComparison,
    ArtifactDiff,
    ArtifactReadError,
)
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError, AgentPosixSdkClient
from goal_plus.thinkthread_artifacts import ThinkThreadSnapshotReader
from goal_plus.workspaces import GitReferences


class ProviderArtifactReader:
    def __init__(self, client_factory: Callable[[], AgentPosixSdkClient]) -> None:
        self.client_factory = client_factory

    def commit_available(self, workspace: Path, commit: str) -> bool:
        return (
            git_returncode(workspace, ["git", "cat-file", "-e", f"{commit}^{{commit}}"])
            == 0
        )

    def changed_files(self, comparison: ArtifactComparison) -> list[str]:
        try:
            if comparison.head.kind == "fs_snapshot":
                reader = ThinkThreadSnapshotReader(self.client_factory())
                return thinkthread_logical_changed_files(
                    comparison.source_relative_path,
                    reader.changed_files(comparison.base, comparison.head),
                )
            return git_changed_files(
                comparison.workspace, comparison.base.id, comparison.head.id
            )
        except (
            AgentPosixBridgeError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
        ) as exc:
            raise ArtifactReadError(f"{type(exc).__name__}: {exc}") from exc

    def diff(
        self,
        comparison: ArtifactComparison,
        *,
        paths: list[str],
        max_bytes: int,
        function_context: bool,
    ) -> ArtifactDiff:
        snapshot_paths = (
            [
                thinkthread_snapshot_path(comparison.source_relative_path, path)
                for path in paths
                if not path.startswith("../")
            ]
            if comparison.head.kind == "fs_snapshot"
            else []
        )
        try:
            if comparison.head.kind == "fs_snapshot":
                reader = ThinkThreadSnapshotReader(self.client_factory())
                text, truncated = reader.diff(
                    comparison.base,
                    comparison.head,
                    paths=snapshot_paths,
                    max_bytes=max_bytes,
                )
                return ArtifactDiff(text, truncated)
            if not paths:
                return ArtifactDiff("")
            context = ["--function-context", "--unified=10"] if function_context else []
            return ArtifactDiff(
                git_output_bounded(
                    comparison.workspace,
                    [
                        "git",
                        "diff",
                        "--full-index",
                        "--no-ext-diff",
                        *context,
                        comparison.base.id,
                        comparison.head.id,
                        "--",
                        *paths,
                    ],
                    max_bytes=max_bytes,
                )
            )
        except (
            AgentPosixBridgeError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
        ) as exc:
            raise ArtifactReadError(f"{type(exc).__name__}: {exc}") from exc

    def open_view(
        self,
        workspace: Path,
        artifact: ArtifactRef,
        destination: Path,
        *,
        workspace_provider: WorkspaceProvider,
    ) -> ReferenceWorkspaceAccess:
        if workspace_provider == "thinkthread":
            return thinkthread_artifact_view(artifact)
        provider = GitReferences(workspace_provider)
        if destination.exists():
            provider.remove_reference(workspace, artifact, destination)
        return provider.open_artifact_view(workspace, artifact, destination)


def thinkthread_artifact_view(
    artifact_ref: ArtifactRef,
) -> ReferenceWorkspaceAccess:
    if artifact_ref.kind != "fs_snapshot" or artifact_ref.provider != "thinkthread":
        raise ValueError("ThinkThread View requires an fs_snapshot artifact")
    # The retained ThinkThread annotator consumes this immutable provider
    # handle; no host-side workspace materialization is required.
    return ReferenceWorkspaceAccess(
        kind="provider_tools",
        readonly_contract="enforced",
        provider_handle={
            "provider": "thinkthread",
            "snapshot_id": artifact_ref.id,
            "operations": [
                "fs.snapshot.readdir",
                "fs.snapshot.stat",
                "fs.snapshot.pread",
                "fs.snapshot.diff",
                "fs.run.discard_writes",
            ],
        },
    )
