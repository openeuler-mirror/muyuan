from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from goal_plus.models import ArtifactRef
from goal_plus.workspaces import (
    UnsupportedWorkspaceCapability,
    estimate_workspace_capacity,
    get_workspace_provider,
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


def test_workspace_provider_declares_relocation_verification_contract() -> None:
    git_worktree = get_workspace_provider("git_worktree")
    copy = get_workspace_provider("copy")
    thinkthread = get_workspace_provider("thinkthread")

    assert (
        git_worktree.capabilities.requires_relocation_verification is True
    )
    assert copy.capabilities.requires_relocation_verification is False
    assert thinkthread.capabilities.requires_relocation_verification is False


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


def test_thinkthread_backend_declares_future_capabilities_without_fallback(
    tmp_path: Path,
) -> None:
    provider = get_workspace_provider("thinkthread")

    assert provider.capabilities.protected_execution is False
    assert provider.capabilities.reference_workspace is False
    assert provider.capabilities.prepare_promotion is True
    assert provider.capabilities.apply_promotion is True
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


def test_thinkthread_promotion_filters_local_vcs_and_uses_fs_apply(
    tmp_path: Path,
) -> None:
    provider = get_workspace_provider("thinkthread")
    calls: list[tuple[str, ...]] = []

    provider._snapshot_diff = lambda _base, _target: {  # type: ignore[attr-defined,method-assign]
        "src/new": "added",
        "src/new/app.py": "added",
        "src/.git/index": "modified",
        "src/results.tsv": "modified",
    }
    provider._snapshot_stat = lambda _snapshot, path: {  # type: ignore[attr-defined,method-assign]
        "path": path,
        "kind": "directory" if path == "src/new" else "file",
        "mode": 0o755 if path == "src/new" else 0o644,
        "len": 4,
    }
    provider._snapshot_read = lambda _snapshot, _path: b"new\n"  # type: ignore[attr-defined,method-assign]

    def command(*args: str) -> dict[str, object]:
        calls.append(args)
        if args[:3] == ("fs", "snapshot", "patch"):
            return {
                "snapshot": {"snapshotId": "fsnap-publish"},
                "changedPaths": 1,
                "changedBytes": 4,
            }
        if args[:2] == ("fs", "apply"):
            return {
                "status": "applied",
                "baseSnapshotId": "fsnap-base",
                "targetSnapshotId": "fsnap-publish",
                "changedPaths": 1,
                "changedBytes": 4,
            }
        raise AssertionError(args)

    provider._command = command  # type: ignore[attr-defined,method-assign]
    prepared = provider.prepare_promotion(
        run_dir=tmp_path / "run",
        run_id="run_test",
        candidate_id="c001",
        source=tmp_path / "source",
        workspace=Path("src"),
        source_subdir="src",
        base_artifact_ref=ArtifactRef(
            kind="fs_snapshot", id="fsnap-base", provider="thinkthread"
        ),
        selected_artifact_ref=ArtifactRef(
            kind="fs_snapshot", id="fsnap-selected", provider="thinkthread"
        ),
        changed_files=["new/app.py", ".git/index", "results.tsv"],
    )

    assert prepared.strategy == "fs_apply"
    assert prepared.changed_files == ("new/app.py",)
    assert prepared.publication_artifact_ref == ArtifactRef(
        kind="fs_snapshot", id="fsnap-publish", provider="thinkthread"
    )
    patch_args = calls[0]
    assert "src/new:755" in patch_args
    assert "src/new/app.py" in " ".join(patch_args)
    assert ".git" not in " ".join(patch_args)
    assert "results.tsv" not in " ".join(patch_args)
    request_id = prepared.provider_handle["apply_request_id"]
    assert len(request_id) == 40
    assert request_id.startswith("req-")

    applied = provider.apply_promotion(prepared)

    assert applied.source_revision is None
    assert applied.details["status"] == "applied"
    assert calls[-1][:4] == (
        "fs",
        "apply",
        "--base",
        "fsnap-base",
    )
