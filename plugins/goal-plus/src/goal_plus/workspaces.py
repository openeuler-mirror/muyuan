from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Literal, Mapping

from goal_plus.domain.artifacts import (
    ArtifactRef,
    ReferenceWorkspaceAccess,
    WorkspaceProvider,
    WorkspaceCapacityEstimate,
)
from goal_plus.infrastructure.artifacts.submodules import inspect_submodules
from goal_plus.ports.workspace_creation import WorkspaceMaterialization
from goal_plus.ports.publication import (
    PreparedPromotion,
    PromotionApplyResult,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT

IGNORED_NAMES = {
    ".git",
    DEFAULT_RUNTIME_ROOT,
    LEGACY_RUNTIME_ROOT,
    ".tmp",
    ".pytest_cache",
    "__pycache__",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
GENERATED_SOURCE_DIRECTORIES = {"build", "dist", "CMakeFiles"}
GENERATED_SOURCE_SUFFIXES = {".o", ".obj", ".so", ".whl"}
GENERATED_SOURCE_FILES = {
    "CMakeCache.txt",
    "cmake_install.cmake",
    "compile_commands.json",
}


class UnsupportedWorkspaceCapability(RuntimeError):
    def __init__(self, workspace_provider: WorkspaceProvider, capability: str) -> None:
        self.workspace_provider = workspace_provider
        self.capability = capability
        super().__init__(
            f"workspace provider {workspace_provider!r} does not implement capability "
            f"{capability!r}"
        )


def should_ignore(path: Path) -> bool:
    if any(part in IGNORED_NAMES for part in path.parts):
        return True
    return path.suffix in IGNORED_SUFFIXES


def list_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = [
            name
            for name in directory_names
            if name not in IGNORED_NAMES
            and not (Path(directory) / name).is_symlink()
        ]
        for name in file_names:
            path = Path(directory) / name
            if path.is_file() and not should_ignore(path.relative_to(root)):
                files.append(path)
    return sorted(files)


def list_source_files(root: Path) -> list[Path]:
    """List source inputs without carrying untracked build products forward.

    Ordinary ignored files remain valid source inputs. Generated-looking files
    are retained only when the source repository deliberately tracks them.
    """
    if root.is_file():
        return [root]

    try:
        tracked = _git_list_paths(
            root,
            ["git", "ls-files", "--cached", "-z", "--", "."],
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        tracked = set()

    paths: list[Path] = []
    for path in list_files(root):
        rel_path = path.relative_to(root)
        rel_value = rel_path.as_posix()
        if (
            rel_value in tracked
            or not _looks_like_generated_source_artifact(rel_path)
        ):
            paths.append(path)
    return sorted(paths)


def estimate_workspace_capacity(
    *,
    source: Path,
    workspace_provider: WorkspaceProvider,
    worktree_nodes: int,
    destination_root: Path,
) -> WorkspaceCapacityEstimate:
    """Estimate storage before materializing any candidate workspace.

    Git worktrees share one run-local object store, but every node still checks
    out a complete tracked tree. The estimate therefore reports the source
    scope, effective materialized input, per-node checkout, shared objects, and
    a conservative upper bound separately.
    """

    if worktree_nodes <= 0:
        raise ValueError("worktree_nodes must be > 0")
    source = source.resolve()
    source_scope_files, source_scope_bytes = _tree_usage(source)
    materialized_paths = list_source_files(source)
    materialized_source_files = len(materialized_paths)
    materialized_source_bytes = sum(_path_size(path) for path in materialized_paths)
    available_bytes = _available_bytes(destination_root)
    notes = [
        "Estimates exclude candidate edits, build outputs, runtime logs, and later Git objects.",
        "source_scope includes ignored/runtime files; materialized_source "
        "applies workspace exclusions.",
    ]

    if workspace_provider == "thinkthread":
        return WorkspaceCapacityEstimate(
            source_path=str(source),
            workspace_provider=workspace_provider,
            basis="thinkthread_remote",
            worktree_nodes=worktree_nodes,
            source_scope_files=source_scope_files,
            source_scope_bytes=source_scope_bytes,
            materialized_source_files=materialized_source_files,
            materialized_source_bytes=materialized_source_bytes,
            available_bytes=available_bytes,
            fits_available_space=None,
            notes=[
                *notes,
                "ThinkThread snapshot and remote node storage are provider-owned "
                "and unavailable locally.",
            ],
        )

    layout = _git_source_layout(source) if workspace_provider == "git_worktree" else None
    if layout is not None:
        repository_root, _source_subdir, source_head = layout
        checkout_files, checkout_bytes = _git_checkout_usage(
            repository_root,
            source_head,
        )
        shared_object_store_bytes = _git_object_store_usage(repository_root)
        candidate_checkouts_bytes = checkout_bytes * worktree_nodes
        estimated_incremental_bytes = candidate_checkouts_bytes
        conservative_upper_bound_bytes = (
            candidate_checkouts_bytes + shared_object_store_bytes
        )
        fits = (
            conservative_upper_bound_bytes <= available_bytes
            if available_bytes is not None
            else None
        )
        return WorkspaceCapacityEstimate(
            source_path=str(source),
            workspace_provider=workspace_provider,
            basis="git_tracked_checkout",
            worktree_nodes=worktree_nodes,
            source_scope_files=source_scope_files,
            source_scope_bytes=source_scope_bytes,
            materialized_source_files=materialized_source_files,
            materialized_source_bytes=materialized_source_bytes,
            checkout_scope_path=str(repository_root),
            checkout_files_per_node=checkout_files,
            checkout_bytes_per_node=checkout_bytes,
            candidate_checkouts_bytes=candidate_checkouts_bytes,
            shared_object_store_bytes=shared_object_store_bytes,
            estimated_incremental_bytes=estimated_incremental_bytes,
            conservative_upper_bound_bytes=conservative_upper_bound_bytes,
            available_bytes=available_bytes,
            fits_available_space=fits,
            notes=[
                *notes,
                "The run-local --local mirror normally hard-links existing Git "
                "objects; the upper bound counts them once more.",
                "Each worktree node checks out the whole repository, even when "
                "source_path is a subdirectory.",
                "Git clean/smudge filters such as LFS can expand checkout files "
                "beyond tracked blob sizes.",
            ],
        )

    if workspace_provider == "git_worktree":
        checkout_bytes = materialized_source_bytes
        checkout_files = materialized_source_files
        candidate_checkouts_bytes = checkout_bytes * worktree_nodes
        estimated_incremental_bytes = checkout_bytes + candidate_checkouts_bytes
        conservative_upper_bound_bytes = estimated_incremental_bytes * 2
        basis = "synthetic_git_checkout"
        shared_object_store_bytes = None
        notes.append(
            "A non-Git source needs one synthetic baseline repository in addition "
            "to candidate worktrees."
        )
    else:
        checkout_bytes = materialized_source_bytes
        checkout_files = materialized_source_files
        candidate_checkouts_bytes = checkout_bytes * worktree_nodes
        estimated_incremental_bytes = candidate_checkouts_bytes
        conservative_upper_bound_bytes = candidate_checkouts_bytes * 2
        basis = "source_tree_copy"
        shared_object_store_bytes = None
        notes.append(
            "The copy workspace_provider initializes an independent Git baseline in every "
            "candidate; the upper bound allows one extra source-sized object copy "
            "per node."
        )
    fits = (
        conservative_upper_bound_bytes <= available_bytes
        if available_bytes is not None
        else None
    )
    return WorkspaceCapacityEstimate(
        source_path=str(source),
        workspace_provider=workspace_provider,
        basis=basis,
        worktree_nodes=worktree_nodes,
        source_scope_files=source_scope_files,
        source_scope_bytes=source_scope_bytes,
        materialized_source_files=materialized_source_files,
        materialized_source_bytes=materialized_source_bytes,
        checkout_scope_path=str(source),
        checkout_files_per_node=checkout_files,
        checkout_bytes_per_node=checkout_bytes,
        candidate_checkouts_bytes=candidate_checkouts_bytes,
        shared_object_store_bytes=shared_object_store_bytes,
        estimated_incremental_bytes=estimated_incremental_bytes,
        conservative_upper_bound_bytes=conservative_upper_bound_bytes,
        available_bytes=available_bytes,
        fits_available_space=fits,
        notes=notes,
    )


def _tree_usage(root: Path) -> tuple[int, int]:
    if root.is_file() or root.is_symlink():
        return 1, _path_size(root)
    files = 0
    total = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        symlink_directories = [
            Path(directory) / name
            for name in directory_names
            if (Path(directory) / name).is_symlink()
        ]
        directory_names[:] = [
            name
            for name in directory_names
            if not (Path(directory) / name).is_symlink()
        ]
        for name in file_names:
            path = Path(directory) / name
            files += 1
            total += _path_size(path)
        for path in symlink_directories:
            files += 1
            total += _path_size(path)
    return files, total


def _path_size(path: Path) -> int:
    try:
        return max(0, path.lstat().st_size)
    except OSError:
        return 0


def _git_checkout_usage(repository_root: Path, revision: str) -> tuple[int, int]:
    output = _git_output_bytes(
        repository_root,
        "ls-tree",
        "-r",
        "-l",
        "-z",
        revision,
    )
    files = 0
    total = 0
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, _separator, _path = record.partition(b"\t")
        size = metadata.rsplit(b" ", 1)[-1]
        if not size.isdigit():
            continue
        files += 1
        total += int(size)
    return files, total


def _git_object_store_usage(repository_root: Path) -> int:
    common_dir = Path(_git_output(repository_root, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (repository_root / common_dir).resolve()
    _files, total = _tree_usage(common_dir / "objects")
    return total


def _available_bytes(destination_root: Path) -> int | None:
    probe = destination_root.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _looks_like_generated_source_artifact(path: Path) -> bool:
    if any(
        part in GENERATED_SOURCE_DIRECTORIES or part.endswith(".egg-info")
        for part in path.parts[:-1]
    ):
        return True
    return (
        path.name in GENERATED_SOURCE_FILES
        or path.suffix in GENERATED_SOURCE_SUFFIXES
    )


def _git_list_paths(
    cwd: Path,
    command: list[str],
) -> set[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return {
        value.decode("utf-8", errors="surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
    }


def copy_source_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source.is_file():
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination / source.name)
        return

    included_files = {
        path.relative_to(source).as_posix() for path in list_source_files(source)
    }
    included_directories = {
        parent.as_posix()
        for rel_path in included_files
        for parent in Path(rel_path).parents
        if parent != Path(".")
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        rel_directory = Path(directory).relative_to(source)
        for name in names:
            rel_path = (rel_directory / name).as_posix()
            path = Path(directory) / name
            if (
                name in IGNORED_NAMES
                or Path(name).suffix in IGNORED_SUFFIXES
                or (
                    path.is_dir()
                    and not path.is_symlink()
                    and rel_path not in included_directories
                )
                or (
                    (not path.is_dir() or path.is_symlink())
                    and rel_path not in included_files
                )
            ):
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


def initialize_workspace_git_baseline(workspace: Path) -> str | None:
    try:
        subprocess.run(
            ["git", "init", "-q"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    files = [path.relative_to(workspace).as_posix() for path in list_files(workspace)]
    if not files:
        return None

    try:
        subprocess.run(
            ["git", "add", "-f", "--", *files],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=goal-plus",
                "-c",
                "user.email=goal-plus@example.invalid",
                "commit",
                "-q",
                "--no-verify",
                "-m",
                "search candidate baseline",
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return None
    return _git_output(workspace, "rev-parse", "HEAD")


def _materialize_git_candidate_workspace(
    *,
    workspace_provider: WorkspaceProvider,
    run_dir: Path,
    source: Path,
    workspace: Path,
    run_id: str,
    candidate_id: str,
    base_workspace: Path | None = None,
    base_revision: str | None = None,
) -> WorkspaceMaterialization:
    if workspace_provider == "copy":
        copy_source_tree(base_workspace or source, workspace)
        revision = initialize_workspace_git_baseline(workspace)
        (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
        return WorkspaceMaterialization(
            workspace_provider="copy",
            workspace=workspace,
            branch=None,
            base_revision=revision,
            managed_root=workspace,
        )

    repository, source_subdir, source_repository = _ensure_worktree_repository(
        run_dir, source, run_id
    )
    baseline_branch = _branch_name(run_id, "baseline")
    revision = base_revision or _git_output(
        repository, "rev-parse", baseline_branch
    )
    branch = _branch_name(run_id, candidate_id)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if not _is_expected_worktree(repository, workspace, branch, revision):
        _git_run(repository, "worktree", "prune")
        branch_ref = f"refs/heads/{branch}"
        if _git_succeeds(repository, "show-ref", "--verify", "--quiet", branch_ref):
            branch_revision = _git_output(repository, "rev-parse", branch_ref)
            if branch_revision != revision:
                raise RuntimeError(
                    f"candidate branch {branch} exists at {branch_revision}, "
                    f"expected {revision}"
                )
            _git_run(
                repository,
                "worktree",
                "add",
                "-q",
                str(workspace),
                branch,
            )
        else:
            _git_run(
                repository,
                "worktree",
                "add",
                "-q",
                "-b",
                branch,
                str(workspace),
                revision,
            )
    _materialize_submodules(workspace)
    logical_workspace = (
        workspace
        if source_subdir == "."
        else workspace / Path(source_subdir)
    )
    if not logical_workspace.is_dir():
        raise RuntimeError(
            "git source subdirectory is missing from candidate worktree: "
            f"{source_subdir}"
        )
    (logical_workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    return WorkspaceMaterialization(
        workspace_provider="git_worktree",
        workspace=logical_workspace,
        branch=branch,
        base_revision=revision,
        managed_root=workspace,
        source_subdir=source_subdir,
        provider_handle={
            "repository": str(repository),
            **(
                {"source_repository": str(source_repository)}
                if source_repository is not None
                else {}
            ),
        },
    )


LiteralGitBackend = Literal["copy", "git_worktree"]


def require_local_backend(workspace_provider: WorkspaceProvider, capability: str) -> LiteralGitBackend:
    if workspace_provider not in {"copy", "git_worktree"}:
        raise UnsupportedWorkspaceCapability(workspace_provider, capability)
    return workspace_provider


def _require_scoped_repository(workspace_provider: LiteralGitBackend, workspace: Path) -> None:
    if workspace_provider != "copy":
        return
    expected = workspace.resolve()
    try:
        actual = Path(
            _git_output(workspace, "rev-parse", "--show-toplevel")
        ).resolve()
    except RuntimeError as exc:
        raise RuntimeError(
            f"copy workspace has no independent Git repository: {workspace}"
        ) from exc
    if actual != expected:
        raise RuntimeError(
            "copy workspace Git repository escaped its managed root: "
            f"expected {expected}, found {actual}"
        )


def _require_git_artifact(artifact_ref: ArtifactRef) -> None:
    if artifact_ref.kind != "git_commit":
        raise ValueError("Git workspace provider requires a git_commit artifact")


class LocalGitWorkspace:
    def __init__(self, workspace_provider: WorkspaceProvider) -> None:
        self.workspace_provider = require_local_backend(workspace_provider, "local_workspace")

    def checkpoint(
        self,
        workspace: Path,
        message: str,
        pathspecs: list[str],
    ) -> ArtifactRef | None:
        try:
            _require_scoped_repository(self.workspace_provider, workspace)
            _git_run(workspace, "add", "-A", "-f", "--", *pathspecs)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--quiet", "--", *pathspecs],
                cwd=workspace,
                check=False,
                capture_output=True,
                text=True,
            ).returncode
            if staged not in {0, 1}:
                return None
            if staged == 1:
                _git_run(
                    workspace,
                    "-c",
                    "user.name=goal-plus",
                    "-c",
                    "user.email=goal-plus@example.invalid",
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    message,
                )
            return self.current_artifact(workspace)
        except RuntimeError:
            return None

    def restore(
        self,
        workspace: Path,
        artifact_ref: ArtifactRef,
        preserved_files: Mapping[str, str],
        message: str,
    ) -> ArtifactRef:
        _require_git_artifact(artifact_ref)
        _require_scoped_repository(self.workspace_provider, workspace)
        _git_run(
            workspace,
            "restore",
            f"--source={artifact_ref.id}",
            "--staged",
            "--worktree",
            "--",
            ".",
        )
        for relative_path, content in preserved_files.items():
            path = _contained_path(workspace, relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            _git_run(workspace, "add", "--", relative_path)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        if staged == 1:
            _git_run(
                workspace,
                "-c",
                "user.name=goal-plus",
                "-c",
                "user.email=goal-plus@example.invalid",
                "commit",
                "-q",
                "--no-verify",
                "-m",
                message,
            )
        elif staged != 0:
            raise RuntimeError("could not inspect staged restoration")
        restored = self.current_artifact(workspace)
        if restored is None:
            raise RuntimeError("candidate restoration produced no artifact")
        return restored

    def recover(
        self,
        workspace: Path,
        artifact_ref: ArtifactRef,
        *,
        managed_root: Path,
        recovery_dir: Path,
        preserved_files: Mapping[str, str],
        run_dir: Path,
        candidate_id: str,
        provider_handle: Mapping[str, object],
    ) -> ArtifactRef:
        """Restore a settled tree, archiving uncommitted and scratch state first.

        Only a runtime-owned checkout may be passed here. Unlike normal verifier
        rollback, crash recovery also removes untracked/ignored attempt residue.
        The original archive is never overwritten on a retry. Git history stays
        reachable; restoring the tree does not rewind the branch ref.
        """
        _require_git_artifact(artifact_ref)
        root = managed_root.resolve()
        expected = (run_dir / "workspace" / candidate_id).resolve()
        if managed_root.is_symlink() or root != expected:
            raise RuntimeError("RecoveryWorkspaceMismatch: not a runtime-owned checkout")
        actual = Path(_git_output(workspace, "rev-parse", "--show-toplevel")).resolve()
        if actual != root or not workspace.resolve().is_relative_to(root):
            raise RuntimeError("RecoveryWorkspaceMismatch: not the managed checkout")
        if recovery_dir.resolve().is_relative_to(root):
            raise RuntimeError("RecoveryArchiveInsideWorkspace")
        _git_run(root, "cat-file", "-e", f"{artifact_ref.id}^{{commit}}")
        recovery_dir.mkdir(parents=True, exist_ok=True)
        archive = recovery_dir / "interrupted-workspace.tar"
        if not archive.exists():
            temporary = recovery_dir / "interrupted-workspace.tar.tmp"
            with tarfile.open(temporary, "w", dereference=False) as output:
                for path in sorted(root.iterdir()):
                    if path.name != ".git":
                        output.add(path, arcname=path.name)
            temporary.replace(archive)
        for path in root.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        relative = workspace.resolve().relative_to(root)
        preserved = {str(relative / key): value for key, value in preserved_files.items()}
        return self.restore(
            root, artifact_ref, preserved, f"goal-plus recover {recovery_dir.name}"
        )

    def current_artifact(self, workspace: Path) -> ArtifactRef | None:
        try:
            _require_scoped_repository(self.workspace_provider, workspace)
            revision = _git_output(workspace, "rev-parse", "HEAD")
        except RuntimeError:
            return None
        return ArtifactRef(kind="git_commit", id=revision, provider=self.workspace_provider)

    def is_descendant(
        self,
        workspace: Path,
        base_artifact_ref: ArtifactRef,
        target_artifact_ref: ArtifactRef,
    ) -> bool:
        _require_git_artifact(base_artifact_ref)
        _require_git_artifact(target_artifact_ref)
        _require_scoped_repository(self.workspace_provider, workspace)
        return _git_succeeds(
            workspace,
            "merge-base",
            "--is-ancestor",
            base_artifact_ref.id,
            target_artifact_ref.id,
        )

    def changed_files(
        self,
        workspace: Path,
        base_artifact_ref: ArtifactRef,
        target_artifact_ref: ArtifactRef,
        pathspecs: list[str],
    ) -> list[str]:
        _require_git_artifact(base_artifact_ref)
        _require_git_artifact(target_artifact_ref)
        _require_scoped_repository(self.workspace_provider, workspace)
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--relative",
                "-z",
                base_artifact_ref.id,
                target_artifact_ref.id,
                "--",
                *pathspecs,
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        return sorted(
            value.decode("utf-8", errors="surrogateescape")
            for value in result.stdout.split(b"\0")
            if value
        )

    def status(
        self,
        workspace: Path,
        *,
        ignore_runtime_noise: bool = False,
    ) -> list[str]:
        _require_scoped_repository(self.workspace_provider, workspace)
        try:
            return git_status(workspace, ignore_runtime_noise=ignore_runtime_noise)
        except RuntimeError:
            return []


class GitReferences:
    def __init__(self, workspace_provider: WorkspaceProvider) -> None:
        self.workspace_provider = require_local_backend(workspace_provider, "reference_workspace")

    def materialize_reference(
        self,
        source_workspace: Path,
        artifact_ref: ArtifactRef,
        destination: Path,
    ) -> Path:
        _require_git_artifact(artifact_ref)
        _require_scoped_repository(self.workspace_provider, source_workspace)
        source_root = Path(
            _git_output(source_workspace, "rev-parse", "--show-toplevel")
        ).resolve()
        source_subdir = source_workspace.resolve().relative_to(source_root)
        if destination.exists():
            try:
                existing = _git_output(destination, "rev-parse", "HEAD")
                status = _git_output(destination, "status", "--porcelain")
            except RuntimeError as exc:
                raise RuntimeError(
                    f"reference workspace already exists but is invalid: {destination}"
                ) from exc
            if existing != artifact_ref.id or status:
                raise RuntimeError(
                    f"reference workspace does not match immutable artifact: {destination}"
                )
            return destination / source_subdir
        destination.parent.mkdir(parents=True, exist_ok=True)
        _git_run(
            source_workspace,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(destination),
            artifact_ref.id,
        )
        logical_destination = destination / source_subdir
        if not logical_destination.is_dir():
            raise RuntimeError(
                "reference worktree is missing source subdirectory: "
                f"{source_subdir.as_posix()}"
            )
        return logical_destination

    def remove_reference(
        self,
        source_workspace: Path,
        artifact_ref: ArtifactRef,
        destination: Path,
    ) -> bool:
        _require_git_artifact(artifact_ref)
        _require_scoped_repository(self.workspace_provider, source_workspace)
        clean = False
        if destination.exists():
            try:
                worktree_root = Path(
                    _git_output(destination, "rev-parse", "--show-toplevel")
                ).resolve()
                clean = (
                    _git_output(destination, "rev-parse", "HEAD") == artifact_ref.id
                    and not _git_output(worktree_root, "status", "--porcelain")
                )
            except RuntimeError:
                clean = False
                worktree_root = destination
            _git_run(
                source_workspace,
                "worktree",
                "remove",
                "--force",
                str(worktree_root),
            )
        _git_run(source_workspace, "worktree", "prune")
        return clean

    def open_artifact_view(
        self,
        source_workspace: Path,
        artifact_ref: ArtifactRef,
        destination: Path,
    ) -> ReferenceWorkspaceAccess:
        return ReferenceWorkspaceAccess(
            kind="filesystem_path",
            root=self.materialize_reference(
                source_workspace,
                artifact_ref,
                destination,
            ),
            readonly_contract="soft_detected",
        )


class GitPublication:
    workspace_provider: LiteralGitBackend = "git_worktree"

    def prepare_promotion(
        self,
        *,
        run_dir: Path,
        run_id: str,
        candidate_id: str,
        source: Path,
        workspace: Path,
        source_subdir: str,
        base_artifact_ref: ArtifactRef,
        selected_artifact_ref: ArtifactRef,
        changed_files: list[str],
    ) -> PreparedPromotion:
        _require_git_artifact(base_artifact_ref)
        _require_git_artifact(selected_artifact_ref)

        repository = Path(
            _git_output(workspace, "rev-parse", "--git-common-dir")
        )
        if not repository.is_absolute():
            repository = (workspace / repository).resolve()
        promotion_dir = run_dir / "promotion"
        promotion_dir.mkdir(parents=True, exist_ok=True)
        patch_path = promotion_dir / f"{candidate_id}.patch"
        publication_ref = (
            "refs/goal-plus/promotions/"
            f"{_safe_git_component(run_id)}/{_safe_git_component(candidate_id)}"
        )
        top_pathspecs = [
            _top_level_pathspec(source_subdir, relative_path)
            for relative_path in changed_files
        ]

        with tempfile.TemporaryDirectory(prefix="goal-plus-index-") as temporary:
            index_path = Path(temporary) / "index"
            env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            _git_run_env(repository, env, "read-tree", base_artifact_ref.id)
            if top_pathspecs:
                patch = _git_output_bytes(
                    workspace,
                    "diff",
                    "--binary",
                    "--full-index",
                    base_artifact_ref.id,
                    selected_artifact_ref.id,
                    "--",
                    *top_pathspecs,
                )
                _git_run_env(
                    repository,
                    env,
                    "apply",
                    "--cached",
                    "--binary",
                    "--whitespace=nowarn",
                    stdin=patch,
                )
            tree = _git_output_env(repository, env, "write-tree")

        promotion_marker = hashlib.sha256(
            (
                f"goal-plus:git:{run_id}:{candidate_id}:"
                f"{base_artifact_ref.id}:{selected_artifact_ref.id}"
            ).encode("utf-8")
        ).hexdigest()
        message = (
            f"goal-plus: promote {candidate_id}\n\n"
            f"Goal-Plus-Run: {run_id}\n"
            f"Goal-Plus-Candidate: {candidate_id}\n"
            f"Goal-Plus-Selected-Artifact: {selected_artifact_ref.id}\n"
            f"Goal-Plus-Promotion: {promotion_marker}\n"
        )
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "goal-plus",
            "GIT_AUTHOR_EMAIL": "goal-plus@example.invalid",
            "GIT_COMMITTER_NAME": "goal-plus",
            "GIT_COMMITTER_EMAIL": "goal-plus@example.invalid",
        }
        publication_commit = _git_output_env(
            repository,
            commit_env,
            "commit-tree",
            tree,
            "-p",
            base_artifact_ref.id,
            stdin=message.encode("utf-8"),
        )
        _git_run(repository, "update-ref", publication_ref, publication_commit)
        patch_path.write_bytes(
            _git_output_bytes(
                repository,
                "format-patch",
                "--stdout",
                "--binary",
                "--full-index",
                "-1",
                publication_commit,
            )
        )
        source_repository = _git_source_layout(source)
        if source_repository is None:
            raise RuntimeError(
                "squash promotion requires the original source to remain a Git repository"
            )
        return PreparedPromotion(
            provider=self.workspace_provider,
            strategy="squash_commit",
            base_artifact_ref=base_artifact_ref,
            selected_artifact_ref=selected_artifact_ref,
            publication_artifact_ref=ArtifactRef(
                kind="git_commit",
                id=publication_commit,
                provider=self.workspace_provider,
            ),
            artifact_path=patch_path,
            source_repository=source_repository[0],
            source_subdir=source_subdir,
            changed_files=tuple(changed_files),
            provider_handle={
                "repository": str(repository),
                "publication_ref": publication_ref,
                "promotion_marker": promotion_marker,
                "no_op": "true" if not changed_files else "false",
            },
        )

    def apply_promotion(
        self,
        prepared: PreparedPromotion,
    ) -> PromotionApplyResult:
        if prepared.strategy != "squash_commit":
            raise ValueError("Git provider can only apply squash_commit promotions")
        if prepared.source_repository is None:
            raise RuntimeError("Git promotion has no source repository")
        publication = prepared.publication_artifact_ref
        if publication is None:
            raise RuntimeError("Git promotion has no publication commit")
        repository = prepared.source_repository
        if prepared.provider_handle.get("no_op") == "true":
            return PromotionApplyResult(
                source_revision=_git_output(repository, "rev-parse", "HEAD"),
                details={
                    "status": "applied",
                    "method": "git_noop",
                    "publication_commit": publication.id,
                },
            )
        promotion_marker = prepared.provider_handle.get("promotion_marker")
        if promotion_marker:
            existing_revision = _git_output(
                repository,
                "log",
                "--format=%H",
                "--fixed-strings",
                f"--grep=Goal-Plus-Promotion: {promotion_marker}",
                "-n",
                "1",
                "HEAD",
            )
            if existing_revision:
                return PromotionApplyResult(
                    source_revision=existing_revision,
                    details={
                        "status": "applied",
                        "method": "git_existing_cherry_pick",
                        "publication_commit": publication.id,
                    },
                )
        if git_status(repository, ignore_runtime_noise=True):
            raise RuntimeError(
                "cannot apply Git promotion to a dirty source repository"
            )
        run_repository = prepared.provider_handle.get("repository")
        if not run_repository:
            raise RuntimeError("Git promotion has no run-local repository")
        _git_run(
            repository,
            "fetch",
            "--no-tags",
            str(run_repository),
            publication.id,
        )
        try:
            _git_run(
                repository,
                "-c",
                "user.name=goal-plus",
                "-c",
                "user.email=goal-plus@example.invalid",
                "cherry-pick",
                "--no-edit",
                publication.id,
            )
        except RuntimeError:
            if _git_succeeds(repository, "rev-parse", "--verify", "CHERRY_PICK_HEAD"):
                _git_run(repository, "cherry-pick", "--abort")
            raise
        revision = _git_output(repository, "rev-parse", "HEAD")
        return PromotionApplyResult(
            source_revision=revision,
            details={
                "status": "applied",
                "method": "git_cherry_pick",
                "publication_commit": publication.id,
            },
        )


def materialize_candidate_workspace(
    *,
    workspace_provider: WorkspaceProvider,
    run_dir: Path,
    source: Path,
    workspace: Path,
    run_id: str,
    candidate_id: str,
    base_workspace: Path | None = None,
    base_revision: str | None = None,
) -> WorkspaceMaterialization:
    return _materialize_git_candidate_workspace(
        workspace_provider=require_local_backend(workspace_provider, "materialize_candidate"),
        run_dir=run_dir,
        source=source,
        workspace=workspace,
        run_id=run_id,
        candidate_id=candidate_id,
        base_workspace=base_workspace,
        base_revision=base_revision,
    )


def _contained_path(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {relative_path}") from exc
    return candidate


def _git_source_layout(source: Path) -> tuple[Path, str, str] | None:
    source_root = source.resolve() if source.is_dir() else source.resolve().parent
    try:
        repository_root = Path(
            _git_output(source_root, "rev-parse", "--show-toplevel")
        ).resolve()
        head = _git_output(source_root, "rev-parse", "HEAD")
        source_subdir = source_root.relative_to(repository_root).as_posix()
    except (RuntimeError, ValueError):
        return None
    return repository_root, source_subdir or ".", head


def _ensure_worktree_repository(
    run_dir: Path,
    source: Path,
    run_id: str,
) -> tuple[Path, str, Path | None]:
    legacy_repository = run_dir / "workspace-repository"
    if legacy_repository.exists() and (legacy_repository / ".git").exists():
        baseline_branch = _branch_name(run_id, "baseline")
        try:
            _git_output(legacy_repository, "rev-parse", baseline_branch)
        except RuntimeError:
            pass
        else:
            return legacy_repository, ".", None

    layout = _git_source_layout(source)
    if layout is None:
        return (
            _ensure_synthetic_worktree_repository(run_dir, source, run_id),
            ".",
            None,
        )

    source_repository, source_subdir, source_head = layout
    repository = run_dir / "workspace-repository.git"
    baseline_branch = _branch_name(run_id, "baseline")
    if repository.exists():
        try:
            is_bare = _git_output(
                repository, "rev-parse", "--is-bare-repository"
            ) == "true"
        except RuntimeError:
            is_bare = False
        if not is_bare:
            shutil.rmtree(repository)
        else:
            try:
                _git_output(repository, "rev-parse", baseline_branch)
                return repository, source_subdir, source_repository
            except RuntimeError:
                shutil.rmtree(repository)

    staging = run_dir / "workspace-repository.git.init"
    shutil.rmtree(staging, ignore_errors=True)
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--mirror",
                "--local",
                str(source_repository),
                str(staging),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(
            "failed to create run-local Git mirror from "
            f"{source_repository}: {detail.strip()}"
        ) from exc
    _git_run(staging, "update-ref", f"refs/heads/{baseline_branch}", source_head)
    staging.replace(repository)
    return repository, source_subdir, source_repository


def _ensure_synthetic_worktree_repository(
    run_dir: Path,
    source: Path,
    run_id: str,
) -> Path:
    repository = run_dir / "workspace-repository"
    baseline_branch = _branch_name(run_id, "baseline")
    if repository.exists():
        if not (repository / ".git").exists():
            shutil.rmtree(repository)
        else:
            try:
                _git_output(repository, "rev-parse", baseline_branch)
                return repository
            except RuntimeError:
                shutil.rmtree(repository)

    staging = run_dir / "workspace-repository.init"
    copy_source_tree(source, staging)
    baseline_revision = initialize_workspace_git_baseline(staging)
    if baseline_revision is None:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("git_worktree workspace_provider requires a non-empty source and Git")
    _git_run(staging, "branch", "-M", baseline_branch)
    staging.replace(repository)
    return repository


def _materialize_submodules(workspace: Path) -> None:
    """Populate formal submodules in a candidate worktree and verify them."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        ],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"failed to materialize Git submodules in {workspace}: {detail[:1000]}"
        )
    # Parent-only repositories remain a valid fast path.
    has_gitlinks = any(
        line.startswith("160000 commit ")
        for line in _git_output(
            workspace,
            "ls-tree",
            "-r",
            "HEAD",
        ).splitlines()
    )
    if (workspace / ".gitmodules").exists() or has_gitlinks:
        inspect_submodules(workspace)


def _is_expected_worktree(
    repository: Path,
    workspace: Path,
    branch: str,
    revision: str,
) -> bool:
    if not workspace.exists():
        return False
    if not any(workspace.iterdir()):
        workspace.rmdir()
        return False
    try:
        current_branch = _git_output(workspace, "branch", "--show-current")
        current_revision = _git_output(workspace, "rev-parse", "HEAD")
        common_dir = _resolve_git_path(
            workspace, _git_output(workspace, "rev-parse", "--git-common-dir")
        )
        repository_git_dir = _resolve_git_path(
            repository, _git_output(repository, "rev-parse", "--git-common-dir")
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"candidate workspace already exists but is not a valid worktree: {workspace}"
        ) from exc
    if (
        current_branch != branch
        or current_revision != revision
        or common_dir != repository_git_dir
    ):
        raise RuntimeError(
            f"candidate workspace {workspace} does not match expected branch/base"
        )
    return True


def _resolve_git_path(workspace: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _branch_name(run_id: str, candidate_id: str) -> str:
    safe_run_id = _safe_git_component(run_id)
    safe_candidate_id = _safe_git_component(candidate_id)
    if not safe_run_id or not safe_candidate_id:
        raise ValueError("run_id and candidate_id must contain a Git-safe character")
    return f"gp/{safe_run_id}/{safe_candidate_id}"


def _safe_git_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def _top_level_pathspec(source_subdir: str, relative_path: str) -> str:
    value = (
        relative_path
        if source_subdir == "."
        else f"{source_subdir.rstrip('/')}/{relative_path}"
    )
    return f":(top,literal){value}"


def git_status(
    workspace: Path,
    *,
    ignore_runtime_noise: bool = False,
    ignore_submodules: bool = False,
) -> list[str]:
    command = ["status", "--porcelain=v1", "--untracked-files=all"]
    if ignore_submodules:
        command.append("--ignore-submodules=all")
    if ignore_runtime_noise:
        exclusions = [
            pattern
            for name in sorted(IGNORED_NAMES - {".git"})
            for pattern in (
                f":(exclude){name}/**",
                f":(exclude)**/{name}/**",
            )
        ]
        exclusions.extend(
            f":(exclude)**/*{suffix}" for suffix in sorted(IGNORED_SUFFIXES)
        )
        command.extend(["--", ".", *exclusions])
    value = _git_output(workspace, *command)
    return [line for line in value.splitlines() if line.strip()]


def _git_run(repository: Path, *args: str) -> None:
    try:
        subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(
            f"git workspace command failed in {repository}: git {' '.join(args)}: "
            f"{detail.strip()}"
        ) from exc


def _git_output(repository: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(
            f"git workspace command failed in {repository}: git {' '.join(args)}: "
            f"{detail.strip()}"
        ) from exc


def _git_output_bytes(repository: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git workspace command failed in {repository}: git {' '.join(args)}: "
            f"{str(detail or exc).strip()}"
        ) from exc


def _git_run_env(
    repository: Path,
    env: Mapping[str, str],
    *args: str,
    stdin: bytes | None = None,
) -> None:
    try:
        subprocess.run(
            ["git", *args],
            cwd=repository,
            env=dict(env),
            input=stdin,
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git workspace command failed in {repository}: git {' '.join(args)}: "
            f"{str(detail or exc).strip()}"
        ) from exc


def _git_output_env(
    repository: Path,
    env: Mapping[str, str],
    *args: str,
    stdin: bytes | None = None,
) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            env=dict(env),
            input=stdin,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8", errors="replace").strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git workspace command failed in {repository}: git {' '.join(args)}: "
            f"{str(detail or exc).strip()}"
        ) from exc


def _git_succeeds(repository: Path, *args: str) -> bool:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        ).returncode == 0
    except FileNotFoundError:
        return False
