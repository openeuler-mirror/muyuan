from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from goal_plus.models import ArtifactRef
from goal_plus.infrastructure.artifacts.candidate_workspace import FileCandidateWorkspaceCreation
from goal_plus.infrastructure.artifacts.thinkthread_publication import ThinkThreadPublicationSnapshots
from goal_plus.workspaces import (
    LocalGitWorkspace,
    UnsupportedWorkspaceCapability,
    estimate_workspace_capacity,
    materialize_candidate_workspace,
)


def test_git_capacity_estimate_counts_four_full_repository_checkouts(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "packages" / "task"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    root_content = "root file\n"
    task_content = "VALUE = 0\n"
    (repository / "README.md").write_text(root_content, encoding="utf-8")
    (source / "program.py").write_text(task_content, encoding="utf-8")
    git_commit(repository, "capacity baseline")

    estimate = estimate_workspace_capacity(
        source=source,
        backend="git_worktree",
        worktree_nodes=4,
        destination_root=tmp_path / "run" / ".gp",
    )

    checkout_bytes = len(root_content.encode()) + len(task_content.encode())
    assert estimate.basis == "git_tracked_checkout"
    assert estimate.worktree_nodes == 4
    assert estimate.checkout_scope_path == str(repository)
    assert estimate.checkout_files_per_node == 2
    assert estimate.checkout_bytes_per_node == checkout_bytes
    assert estimate.candidate_checkouts_bytes == checkout_bytes * 4
    assert estimate.shared_object_store_bytes is not None
    assert estimate.shared_object_store_bytes > 0
    assert estimate.conservative_upper_bound_bytes == (
        estimate.candidate_checkouts_bytes + estimate.shared_object_store_bytes
    )
    assert estimate.available_bytes is not None
    assert estimate.fits_available_space is True


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "program.py").write_text("VALUE = 0\n", encoding="utf-8")
    return source


def git_output(workspace: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=workspace, text=True
    ).strip()


def git_commit(workspace: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            message,
        ],
        cwd=workspace,
        check=True,
    )
    return git_output(workspace, "rev-parse", "HEAD")


def common_git_dir(workspace: Path) -> Path:
    raw = Path(git_output(workspace, "rev-parse", "--git-common-dir"))
    return raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()


def test_copy_backend_creates_independent_git_workspaces(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    run_dir = tmp_path / "run"
    first_path = run_dir / "workspace" / "c001"
    second_path = run_dir / "workspace" / "c002"

    first = materialize_candidate_workspace(
        backend="copy",
        run_dir=run_dir,
        source=source,
        workspace=first_path,
        run_id="run_test",
        candidate_id="c001",
    )
    second = materialize_candidate_workspace(
        backend="copy",
        run_dir=run_dir,
        source=source,
        workspace=second_path,
        run_id="run_test",
        candidate_id="c002",
    )

    assert first.backend == "copy"
    assert first.branch is None
    assert first.base_revision is not None
    assert second.backend == "copy"
    assert (first_path / "program.py").read_text(encoding="utf-8") == "VALUE = 0\n"
    assert common_git_dir(first_path) != common_git_dir(second_path)


def test_copy_checkpoint_refuses_parent_repository_after_git_dir_is_removed(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = make_source(repository)
    (repository / ".gitignore").write_text("/run/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    git_commit(repository, "source baseline")
    workspace = repository / "run" / "workspace" / "c001"
    materialize_candidate_workspace(
        backend="copy",
        run_dir=repository / "run",
        source=source,
        workspace=workspace,
        run_id="run_test",
        candidate_id="c001",
    )
    shutil.rmtree(workspace / ".git")
    (workspace / "program.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_head = git_output(repository, "rev-parse", "HEAD")

    artifact = LocalGitWorkspace("copy").checkpoint(
        workspace,
        "search verifier iteration c001:1",
        ["."],
    )

    assert artifact is None
    assert git_output(repository, "rev-parse", "HEAD") == source_head
    assert git_output(repository, "status", "--porcelain=v1") == ""


def test_git_worktree_backend_shares_objects_but_keeps_branches_independent(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    run_dir = tmp_path / "run"
    first_path = run_dir / "workspace" / "c001"
    second_path = run_dir / "workspace" / "c002"

    first = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=first_path,
        run_id="run_test",
        candidate_id="c001",
    )
    second = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=second_path,
        run_id="run_test",
        candidate_id="c002",
    )

    assert first.branch == "gp/run_test/c001"
    assert second.branch == "gp/run_test/c002"
    assert common_git_dir(first_path) == common_git_dir(second_path)
    assert git_output(first_path, "branch", "--show-current") == first.branch
    assert git_output(second_path, "branch", "--show-current") == second.branch

    (first_path / "program.py").write_text("VALUE = 1\n", encoding="utf-8")
    first_head = git_commit(first_path, "candidate one")
    (second_path / "program.py").write_text("VALUE = 2\n", encoding="utf-8")
    second_head = git_commit(second_path, "candidate two")

    assert first_head != second_head
    assert (first_path / "program.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (second_path / "program.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_candidate_creation_requires_relocation_only_for_git_worktrees(tmp_path) -> None:
    creation = FileCandidateWorkspaceCreation(tmp_path)
    assert creation.requires_relocation("git_worktree") is True
    assert creation.requires_relocation("copy") is False
    assert creation.requires_relocation("thinkthread") is False


def test_git_worktree_clones_full_history_and_returns_nested_logical_workspace(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "packages" / "task"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    (repository / "README.md").write_text("root\n", encoding="utf-8")
    (source / "program.py").write_text("VALUE = 0\n", encoding="utf-8")
    first = git_commit(repository, "first source commit")
    (repository / "README.md").write_text("root v2\n", encoding="utf-8")
    second = git_commit(repository, "second source commit")

    materialized = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=tmp_path / "run",
        source=source,
        workspace=tmp_path / "run" / "workspace" / "c001",
        run_id="run_test",
        candidate_id="c001",
    )

    assert materialized.source_subdir == "packages/task"
    assert materialized.managed_root == tmp_path / "run" / "workspace" / "c001"
    assert materialized.workspace == materialized.managed_root / "packages" / "task"
    assert materialized.provider_handle is not None
    mirror = Path(materialized.provider_handle["repository"])
    assert mirror.name == "workspace-repository.git"
    assert git_output(mirror, "rev-parse", "--is-bare-repository") == "true"
    assert git_output(materialized.workspace, "rev-parse", "HEAD") == second
    assert git_output(materialized.workspace, "cat-file", "-t", first) == "commit"
    assert (materialized.managed_root / "README.md").read_text(
        encoding="utf-8"
    ) == "root v2\n"


def test_git_worktree_baseline_tracks_files_ignored_by_source_gitignore(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    (source / ".gitignore").write_text("generated.txt\n", encoding="utf-8")
    (source / "generated.txt").write_text("snapshot me\n", encoding="utf-8")
    workspace = tmp_path / "run" / "workspace" / "c001"

    materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=tmp_path / "run",
        source=source,
        workspace=workspace,
        run_id="run_test",
        candidate_id="c001",
    )

    assert (workspace / "generated.txt").read_text(encoding="utf-8") == "snapshot me\n"
    tracked = git_output(workspace, "ls-files", "generated.txt")
    assert tracked == "generated.txt"


def test_git_worktree_materialization_is_idempotent_after_workspace_creation(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    run_dir = tmp_path / "run"
    workspace = run_dir / "workspace" / "c001"

    first = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=workspace,
        run_id="run_test",
        candidate_id="c001",
    )
    second = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=workspace,
        run_id="run_test",
        candidate_id="c001",
    )

    assert second == first
    assert git_output(workspace, "branch", "--show-current") == first.branch


def test_git_worktree_recovers_incomplete_run_repository(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    run_dir = tmp_path / "run"
    repository = run_dir / "workspace-repository"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    (repository / "partial.txt").write_text("incomplete\n", encoding="utf-8")
    workspace = run_dir / "workspace" / "c001"

    materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=workspace,
        run_id="run_test",
        candidate_id="c001",
    )

    assert (workspace / "program.py").read_text(encoding="utf-8") == "VALUE = 0\n"
    assert not (workspace / "partial.txt").exists()


def test_git_worktree_child_starts_from_explicit_parent_revision(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    run_dir = tmp_path / "run"
    parent_path = run_dir / "workspace" / "c001"
    child_path = run_dir / "workspace" / "c003"

    parent = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=parent_path,
        run_id="run_test",
        candidate_id="c001",
    )
    (parent_path / "program.py").write_text("VALUE = 7\n", encoding="utf-8")
    parent_revision = git_commit(parent_path, "parent winner")

    child = materialize_candidate_workspace(
        backend="git_worktree",
        run_dir=run_dir,
        source=source,
        workspace=child_path,
        run_id="run_test",
        candidate_id="c003",
        base_revision=parent_revision,
    )

    assert parent.base_revision != parent_revision
    assert child.base_revision == parent_revision
    assert child.branch == "gp/run_test/c003"
    assert git_output(child_path, "rev-parse", "HEAD") == parent_revision
    assert (child_path / "program.py").read_text(encoding="utf-8") == "VALUE = 7\n"
    assert common_git_dir(parent_path) == common_git_dir(child_path)


def test_thinkthread_cannot_materialize_a_local_workspace(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        UnsupportedWorkspaceCapability,
        match="materialize_candidate",
    ):
        materialize_candidate_workspace(
            backend="thinkthread",
            run_dir=tmp_path / "run",
            source=make_source(tmp_path),
            workspace=tmp_path / "run" / "workspace" / "c001",
            run_id="run_test",
            candidate_id="c001",
        )


def test_thinkthread_promotion_plan_reads_only_selected_product_changes() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def snapshot_diff_all(self, _base: str, _target: str) -> list[dict[str, Any]]:
            return [
                {"path": {"utf8": path}, "kind": "added"}
                for path in (
                    "src/new",
                    "src/new/app.py",
                    "src/.git/index",
                    "src/results.tsv",
                )
            ]

        def snapshot_read_file(
            self,
            _snapshot: str,
            _path: str,
            *,
            max_bytes: int,
        ) -> bytes:
            assert max_bytes >= 4
            return b"new\n"

        def invoke(
            self,
            operation: str,
            params: dict[str, Any],
            **_: Any,
        ) -> dict[str, Any]:
            self.calls.append((operation, dict(params)))
            if operation == "fs.snapshot.stat":
                path = str(params["path"])
                return {
                    "path": {"utf8": path},
                    "kind": "directory" if path == "src/new" else "file",
                    "mode": 0o755 if path == "src/new" else 0o644,
                    "len": 4,
                }
            raise AssertionError(operation)

    client = FakeClient()
    provider = ThinkThreadPublicationSnapshots(lambda: client, None)
    plan = provider.plan_thinkthread(
        source_subdir="src",
        base_artifact_ref=ArtifactRef(
            kind="fs_snapshot", id="fsnap-base", provider="thinkthread"
        ),
        selected_artifact_ref=ArtifactRef(
            kind="fs_snapshot", id="fsnap-selected", provider="thinkthread"
        ),
        changed_files=["new/app.py", ".git/index", "results.tsv"],
        dependencies=[],
    )

    assert plan.changed_files == ("new/app.py",)
    from goal_plus.ports.publication import PublicationChange
    assert plan.changes == (
        PublicationChange("make_directory", "src/new", 0o755),
        PublicationChange("put_file", "src/new/app.py", 0o644, b"new\n"),
    )
    assert ".git" not in str(plan.changes)
    assert "results.tsv" not in str(plan.changes)
    assert {operation for operation, _params in client.calls} == {
        "fs.snapshot.stat"
    }

def test_thinkthread_publication_plan_orders_complete_typed_mutations() -> None:
    large_content = b"x" * (1024 * 1024 + 17)
    diff = {
        "src/gone/child.txt": "deleted",
        "src/gone": "deleted",
        "src/file-to-dir": "type_changed",
        "src/dir-to-file": "type_changed",
        "src/modified.bin": "modified",
        "src/mode.py": "mode_changed",
        "src/new-dir": "added",
        "src/new-dir/app.py": "added",
    }
    base_kinds = {
        "src/gone/child.txt": "file",
        "src/gone": "directory",
        "src/file-to-dir": "file",
        "src/dir-to-file": "directory",
    }
    selected = {
        "src/file-to-dir": ("directory", 0o750, b""),
        "src/dir-to-file": ("file", 0o600, b"replacement\n"),
        "src/modified.bin": ("file", 0o644, large_content),
        "src/mode.py": ("file", 0o755, b"print('mode')\n"),
        "src/new-dir": ("directory", 0o755, b""),
        "src/new-dir/app.py": ("file", 0o644, b"new\n"),
    }

    class FakeClient:
        def snapshot_diff_all(self, _base: str, _target: str) -> list[dict[str, Any]]:
            return [
                {"path": {"utf8": path}, "kind": kind}
                for path, kind in diff.items()
            ]

        def snapshot_read_file(
            self,
            _snapshot: str,
            path: str,
            *,
            max_bytes: int,
        ) -> bytes:
            content = selected[path][2]
            assert len(content) <= max_bytes
            return content

        def invoke(
            self,
            operation: str,
            params: dict[str, Any],
            **_: Any,
        ) -> dict[str, Any]:
            assert operation == "fs.snapshot.stat"
            path = str(params["path"])
            if params["snapshotId"] == "fsnap-base":
                return {"kind": base_kinds[path], "mode": 0o755}
            kind, mode, content = selected[path]
            return {"kind": kind, "mode": mode, "len": len(content)}

    provider = ThinkThreadPublicationSnapshots(FakeClient, None)
    plan = provider.plan_thinkthread(
        base_artifact_ref=ArtifactRef(
            kind="fs_snapshot", id="fsnap-base", provider="thinkthread"
        ),
        selected_artifact_ref=ArtifactRef(
            kind="fs_snapshot", id="fsnap-selected", provider="thinkthread"
        ),
        source_subdir="src",
        changed_files=[path.removeprefix("src/") for path in diff],
        dependencies=[
            {"path": "config.json", "scope": "content"},
            {"path": "pkg", "scope": "tree_content"},
        ],
    )

    assert [item.kind for item in plan.changes] == [
        "remove_file",
        "remove_directory",
        "remove_file",
        "remove_directory",
        "make_directory",
        "make_directory",
        "put_file",
        "put_file",
        "put_file",
        "put_file",
    ]
    assert [item.path for item in plan.changes] == [
        "src/gone/child.txt",
        "src/dir-to-file",
        "src/file-to-dir",
        "src/gone",
        "src/file-to-dir",
        "src/new-dir",
        "src/dir-to-file",
        "src/mode.py",
        "src/modified.bin",
        "src/new-dir/app.py",
    ]
    from goal_plus.infrastructure.artifacts.thinkthread_publication import publication_change_manifest
    large_manifest = next(
        item for item in publication_change_manifest(plan.changes) if item["path"] == "src/modified.bin"
    )
    assert large_manifest["bytes"] == len(large_content)
    assert large_manifest["sha256"] == hashlib.sha256(large_content).hexdigest()
    assert "dataBase64" not in large_manifest
    assert plan.dependencies == (
        {"path": "src/config.json", "scope": "content"},
        {"path": "src/pkg", "scope": "tree_content"},
    )
