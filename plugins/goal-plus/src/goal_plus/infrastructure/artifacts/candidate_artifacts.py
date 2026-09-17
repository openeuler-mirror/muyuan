"""Candidate artifact inspection and checkpoint I/O for Git and Copy."""

from pathlib import Path
import os
import subprocess
import tempfile
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
    def publish_settlement(self, task: CandidateTask, attempt: ArtifactRef, settled: ArtifactRef,
                           *, preserve_workspace: bool = False, ledger_projection: bool = False) -> bool:
        if preserve_workspace:
            head = self.current_artifact(task)
            if head is None or not self.is_descendant(task, attempt, head):
                raise RuntimeError("VerifierReplayUnavailable: workspace history no longer contains the submitted attempt")
            if self.is_descendant(task, settled, head):
                if ledger_projection:
                    git_output(task.workspace, ["git", "restore", f"--source={settled.id}", "--staged", "--worktree", "--", RESULTS_TSV_RELATIVE_PATH])
                return True
            # Join settlement history without checking out its keep/rollback tree.
            # A temporary index preserves both staged and unstaged worker edits.
            with tempfile.TemporaryDirectory(prefix="goal-plus-settlement-index-") as directory:
                env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
                def git(*args: str) -> str:
                    return subprocess.check_output(["git", *args], cwd=task.workspace, env=env, text=True).strip()
                git("read-tree", head.id)
                if ledger_projection:
                    git("restore", f"--source={settled.id}", "--staged", "--", RESULTS_TSV_RELATIVE_PATH)
                tree = git("write-tree")
                commit = git("-c", "user.name=goal-plus", "-c", "user.email=goal-plus@example.invalid",
                             "commit-tree", tree, "-p", head.id, "-p", settled.id,
                             "-m", "goal-plus resume verification settlement")
                git("update-ref", "HEAD", commit, head.id)
            if ledger_projection:
                git_output(task.workspace, ["git", "restore", f"--source={commit}", "--staged", "--worktree", "--", RESULTS_TSV_RELATIVE_PATH])
            return True
        if self.current_artifact(task) != attempt or self.status(task, ignore_runtime_noise=True):
            return False
        try:
            git_output(task.workspace, ["git", "-c", "merge.autostash=false", "merge", "--ff-only", "--no-edit", settled.id])
        except subprocess.CalledProcessError:
            return False
        return self.current_artifact(task) == settled and not self.status(task, ignore_runtime_noise=True)

    def current_artifact(self, task: CandidateTask) -> ArtifactRef | None:
        return LocalGitWorkspace(task.workspace_provider).current_artifact(
            task.workspace
        )

    def status(
        self, task: CandidateTask, *, ignore_runtime_noise: bool = False
    ) -> list[str]:
        return LocalGitWorkspace(task.workspace_provider).status(
            task.workspace, ignore_runtime_noise=ignore_runtime_noise
        )

    def restore(
        self,
        task: CandidateTask,
        artifact: ArtifactRef,
        preserved_files: dict[str, str],
        message: str,
    ) -> ArtifactRef:
        return LocalGitWorkspace(task.workspace_provider).restore(
            task.workspace, artifact, preserved_files, message
        )

    def is_descendant(
        self, task: CandidateTask, base: ArtifactRef, target: ArtifactRef
    ) -> bool:
        return LocalGitWorkspace(task.workspace_provider).is_descendant(
            task.workspace, base, target
        )

    def changed_between(
        self, task: CandidateTask, base: ArtifactRef, target: ArtifactRef
    ) -> list[str]:
        return LocalGitWorkspace(task.workspace_provider).changed_files(
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
        provider = LocalGitWorkspace(record.task.workspace_provider)
        target = artifact_ref or provider.current_artifact(record.task.workspace)
        base = run.source_artifact_ref
        if (
            record.task.workspace_provider == "git_worktree"
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
        return LocalGitWorkspace(record.task.workspace_provider).checkpoint(
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
