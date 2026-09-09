"""Candidate artifact inspection and checkpoint I/O for Git and Copy."""

from pathlib import Path
from goal_plus.domain.results_ledger import RESULTS_TSV_RELATIVE_PATH
from goal_plus.infrastructure.artifacts.git import candidate_git_pathspecs, git_output
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import CandidateTask
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.state.files import sha256_file
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.workspaces import (
    IGNORED_NAMES,
    IGNORED_SUFFIXES,
    LocalGitWorkspace,
    list_files,
    list_source_files,
)


class FileCandidateArtifacts:
    def current_artifact(self, task: CandidateTask) -> ArtifactRef | None:
        return LocalGitWorkspace(task.workspace_backend).current_artifact(
            task.workspace
        )

    def status(
        self, task: CandidateTask, *, ignore_runtime_noise: bool = False
    ) -> list[str]:
        return LocalGitWorkspace(task.workspace_backend).status(
            task.workspace, ignore_runtime_noise=ignore_runtime_noise
        )

    def restore(
        self,
        task: CandidateTask,
        artifact: ArtifactRef,
        preserved_files: dict[str, str],
        message: str,
    ) -> ArtifactRef:
        return LocalGitWorkspace(task.workspace_backend).restore(
            task.workspace, artifact, preserved_files, message
        )

    def is_descendant(
        self, task: CandidateTask, base: ArtifactRef, target: ArtifactRef
    ) -> bool:
        return LocalGitWorkspace(task.workspace_backend).is_descendant(
            task.workspace, base, target
        )

    def changed_between(
        self, task: CandidateTask, base: ArtifactRef, target: ArtifactRef
    ) -> list[str]:
        return LocalGitWorkspace(task.workspace_backend).changed_files(
            task.workspace, base, target, candidate_git_pathspecs()
        )

    def detect_changed_files(self, source: Path, workspace: Path) -> list[str]:
        source_hashes = self.hash_tree(source, source_view=True)
        workspace_hashes = self.hash_tree(workspace)
        changed: list[str] = []
        for rel_path in sorted(set(source_hashes) | set(workspace_hashes)):
            if rel_path == RESULTS_TSV_RELATIVE_PATH:
                continue
            if source_hashes.get(rel_path) != workspace_hashes.get(rel_path):
                changed.append(rel_path)
        return changed

    def candidate_changed_files(
        self,
        run: RunRecord,
        record: CandidateRecord,
        artifact_ref: ArtifactRef | None = None,
    ) -> list[str]:
        provider = LocalGitWorkspace(record.task.workspace_backend)
        target = artifact_ref or provider.current_artifact(record.task.workspace)
        base = run.source_artifact_ref
        if (
            record.task.workspace_backend == "git_worktree"
            and base is not None
            and target is not None
            and base.provider == target.provider
        ):
            changed = provider.changed_files(
                record.task.workspace,
                base,
                target,
                candidate_git_pathspecs(),
            )
            logical_changes = [
                path for path in changed if path != RESULTS_TSV_RELATIVE_PATH
            ]
            return sorted(
                set(logical_changes)
                | set(
                    self.git_changes_outside_logical_workspace(
                        record.task,
                        base,
                        target,
                    )
                )
            )
        return self.detect_changed_files(Path(run.source_path), record.task.workspace)

    def git_changes_outside_logical_workspace(
        self,
        task: CandidateTask,
        base: ArtifactRef,
        target: ArtifactRef,
    ) -> list[str]:
        managed_root = task.workspace_managed_root
        source_subdir = task.workspace_source_subdir
        if managed_root is None or source_subdir == ".":
            return []
        value = git_output(
            managed_root,
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                base.id,
                target.id,
                "--",
                ".",
            ],
        )
        source_prefix = f"{source_subdir.rstrip('/')}/"
        parent_prefix = "../" * len(Path(source_subdir).parts)
        outside: list[str] = []
        for path in value.split("\0"):
            if not path or path.startswith(source_prefix):
                continue
            candidate = Path(path)
            if any(part in IGNORED_NAMES for part in candidate.parts):
                continue
            if any(candidate.name.endswith(suffix) for suffix in IGNORED_SUFFIXES):
                continue
            outside.append(f"{parent_prefix}{path}")
        return outside

    def artifact_hash(self, workspace: Path, changed_files: list[str]) -> str:
        payload: dict[str, str | None] = {}
        for rel_path in sorted(changed_files):
            path = workspace / rel_path
            payload[rel_path] = sha256_file(path) if path.is_file() else None
        return sha256_text(canonical_json(payload))

    def commit_workspace_iteration(
        self,
        record: CandidateRecord,
        message: str,
    ) -> ArtifactRef | None:
        return LocalGitWorkspace(record.task.workspace_backend).checkpoint(
            record.task.workspace,
            message,
            candidate_git_pathspecs(),
        )

    def hash_tree(
        self,
        root: Path,
        *,
        source_view: bool = False,
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        paths = list_source_files(root) if source_view else list_files(root)
        for path in paths:
            rel_path = path.relative_to(root).as_posix()
            hashes[rel_path] = sha256_file(path)
        return hashes
