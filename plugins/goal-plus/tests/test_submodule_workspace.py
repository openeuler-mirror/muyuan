"""Real Git topology checks for recursive formal submodules."""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest

from goal_plus.domain.submodules import SubmoduleCheckError
from goal_plus.infrastructure.artifacts.source_repository import SourceRepository
from goal_plus.infrastructure.artifacts.submodules import (
    ensure_thinkthread_submodules_supported,
    inspect_submodules,
    thinkthread_submodule_paths,
)
from goal_plus.workspaces import materialize_candidate_workspace
from tests._runtime_helpers import spec_for

create_workspace = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "examples/submodule-e2e/prepare_workspace.py")
)["create_workspace"]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=goal-plus-e2e", "-c", "user.email=e2e@example.invalid", *args],
        cwd=root, text=True,
    ).strip()


def test_recursive_submodule_topology_and_dirty_child(tmp_path: Path) -> None:
    source = create_workspace(tmp_path / "fixture")
    root = Path(source["parent"])
    topology = inspect_submodules(root)
    assert topology.parent_head == source["P0"]
    assert {item.path: item.gitlink for item in topology.nodes} == {
        "modules/repo-a": source["A0"],
        "modules/repo-a/modules/plugin": source["AP0"],
        "modules/repo-b": source["B0"],
    }
    child = root / "modules/repo-a"
    (child / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(SubmoduleCheckError) as error:
        inspect_submodules(root)
    assert error.value.failure_class == "SubmoduleWorkspaceDirty"
    assert error.value.path == "modules/repo-a"
    assert "reason:" in str(error.value)


def test_committed_child_head_mismatch_and_missing_checkout(tmp_path: Path) -> None:
    source = create_workspace(tmp_path / "fixture")
    root = Path(source["parent"])
    child = root / "modules/repo-a"
    (child / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(child, "add", "value.py")
    git(child, "commit", "-qm", "candidate child attempt")
    with pytest.raises(SubmoduleCheckError) as error:
        inspect_submodules(root)
    assert error.value.failure_class == "SubmoduleHeadMismatch"
    assert error.value.path == "modules/repo-a"
    assert error.value.expected == source["A0"]
    assert error.value.actual == git(child, "rev-parse", "HEAD")

    git(child, "checkout", "-q", source["A0"])
    git(root, "submodule", "deinit", "-q", "-f", "modules/repo-b")
    with pytest.raises(SubmoduleCheckError) as error:
        inspect_submodules(root)
    assert error.value.failure_class == "SubmoduleUninitialized"
    assert error.value.path == "modules/repo-b"


def test_git_freeze_rejects_dirty_child_before_verifier(tmp_path: Path) -> None:
    source = create_workspace(tmp_path / "fixture")
    root = Path(source["parent"])
    child = root / "modules/repo-a"
    (child / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    base_spec = spec_for(root)
    spec = base_spec.model_copy(
        update={
            "source_path": str(root),
            "workspace": base_spec.workspace.model_copy(
                update={"provider": "git_worktree"}
            ),
        }
    )

    with pytest.raises(SubmoduleCheckError, match="SubmoduleWorkspaceDirty"):
        SourceRepository().validate_clean_source_repository(spec)


def test_git_worktree_materializes_recursive_submodules(tmp_path: Path) -> None:
    source = create_workspace(tmp_path / "fixture")
    parent = Path(source["parent"])
    materialized = materialize_candidate_workspace(
        workspace_provider="git_worktree",
        run_dir=tmp_path / "run",
        source=parent,
        workspace=tmp_path / "run" / "workspace" / "c001",
        run_id="run_submodule",
        candidate_id="c001",
    )
    topology = inspect_submodules(materialized.managed_root or materialized.workspace)
    assert topology.parent_head == source["P0"]
    assert (materialized.workspace / "modules/repo-a/value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (materialized.workspace / "modules/repo-a/modules/plugin/plugin.py").is_file()


def test_thinkthread_probe_allows_a_plain_git_root(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("plain\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "initial")

    assert thinkthread_submodule_paths(root) == []
    ensure_thinkthread_submodules_supported(root)

    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert thinkthread_submodule_paths(empty) == []
    ensure_thinkthread_submodules_supported(empty)


def test_thinkthread_does_not_apply_git_clean_gate_to_plain_root(tmp_path: Path) -> None:
    root = tmp_path / "dirty"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "initial")
    (root / "README.md").write_text("snapshot input\n", encoding="utf-8")

    base_spec = spec_for(root)
    spec = base_spec.model_copy(
        update={
            "source_path": str(root),
            "workspace": base_spec.workspace.model_copy(update={"provider": "thinkthread"}),
        }
    )
    SourceRepository().validate_clean_source_repository(spec)


def test_thinkthread_rejects_recursive_submodule_workspace(tmp_path: Path) -> None:
    source = create_workspace(tmp_path / "fixture")
    root = Path(source["parent"])

    paths = thinkthread_submodule_paths(root)
    assert ".gitmodules" in paths
    assert "modules/repo-a" in paths
    with pytest.raises(ValueError, match="ThinkThreadSubmoduleUnsupported.*stage=thinkthread-admission.*reason:"):
        ensure_thinkthread_submodules_supported(root)


def test_thinkthread_rejects_nested_git_marker_but_allows_root_marker(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("plain\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "initial")
    nested = root / "vendor" / "local"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /tmp/elsewhere\n", encoding="utf-8")

    paths = thinkthread_submodule_paths(root)
    assert paths == ["vendor/local"]
    with pytest.raises(ValueError, match="ThinkThreadSubmoduleUnsupported.*reason:.*vendor/local"):
        ensure_thinkthread_submodules_supported(root)


def test_thinkthread_probe_is_limited_to_source_subtree(tmp_path: Path) -> None:
    fixture = create_workspace(tmp_path / "fixture")
    root = Path(fixture["parent"])
    source = root / "tasks" / "circle"
    source.mkdir(parents=True)
    unrelated = root / "scratch" / "nested"
    unrelated.mkdir(parents=True)
    (unrelated / ".git").write_text("gitdir: /unrelated\n", encoding="utf-8")

    assert thinkthread_submodule_paths(source) == []
    ensure_thinkthread_submodules_supported(source)

    nested = source / "vendor" / "local"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /nested\n", encoding="utf-8")
    assert thinkthread_submodule_paths(source) == ["vendor/local"]
    with pytest.raises(ValueError, match="ThinkThreadSubmoduleUnsupported.*vendor/local"):
        ensure_thinkthread_submodules_supported(source)

    git(root, "submodule", "deinit", "-q", "-f", "modules/repo-b")
    modules = root / "modules"
    assert thinkthread_submodule_paths(modules) == ["repo-a", "repo-b"]
    with pytest.raises(ValueError, match="ThinkThreadSubmoduleUnsupported.*repo-b"):
        ensure_thinkthread_submodules_supported(modules)
