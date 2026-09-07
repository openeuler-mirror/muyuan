"""Provider read mechanics without Search runtime, agents or model processes."""

import subprocess
import sys
from pathlib import Path

import pytest

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.infrastructure.artifacts.git import git_output_bounded
from goal_plus.infrastructure.artifacts.reader import ProviderArtifactReader
from goal_plus.ports.artifacts import ArtifactComparison, ArtifactReadError
from tests.test_thinkthread_artifacts import (
    SnapshotClient,
    file_entry,
    fs_path,
    snapshot,
)


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=goal-plus",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        cwd=workspace,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


@pytest.mark.parametrize("backend", ["copy", "git_worktree"])
def test_git_reads_exact_commits_after_live_workspace_changes(tmp_path, backend):
    git(tmp_path, "init", "-q")
    program = tmp_path / "main.py"
    program.write_text("VALUE = 1\n")
    git(tmp_path, "add", "main.py")
    git(tmp_path, "commit", "-qm", "baseline")
    base = git(tmp_path, "rev-parse", "HEAD")
    program.write_text("VALUE = 2\n")
    (tmp_path / "results.tsv").write_text("ledger\n")
    git(tmp_path, "add", "main.py", "results.tsv")
    git(tmp_path, "commit", "-qm", "attempt")
    head = git(tmp_path, "rev-parse", "HEAD")
    program.write_text("LIVE = 999\n")
    reader = ProviderArtifactReader(
        lambda: pytest.fail("Git must not construct the TT SDK")
    )
    comparison = ArtifactComparison(
        tmp_path,
        ArtifactRef(kind="git_commit", provider=backend, id=base),
        ArtifactRef(kind="git_commit", provider=backend, id=head),
    )
    assert reader.commit_available(tmp_path, base)
    assert not reader.commit_available(tmp_path, "absent-commit")
    assert reader.changed_files(comparison) == ["main.py"]
    result = reader.diff(
        comparison, paths=["main.py"], max_bytes=4096, function_context=True
    )
    assert not result.truncated
    assert "-VALUE = 1" in result.text and "+VALUE = 2" in result.text
    assert "LIVE" not in result.text and "ledger" not in result.text
    with pytest.raises(
        ArtifactReadError, match="RuntimeError: annotation diff exceeds 8 bytes"
    ):
        reader.diff(comparison, paths=["main.py"], max_bytes=8, function_context=False)


@pytest.mark.parametrize("size", [7, 8, 9])
def test_bounded_capture_keeps_original_byte_boundary(tmp_path, size):
    command = [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {size})"]
    if size <= 8:
        assert git_output_bounded(tmp_path, command, max_bytes=8) == "x" * size
    else:
        with pytest.raises(RuntimeError, match="exceeds 8 bytes"):
            git_output_bounded(tmp_path, command, max_bytes=8)


def test_snapshot_reader_maps_only_authorized_source_paths_and_never_mutates():
    client = SnapshotClient()
    client.changes = [
        {"path": fs_path("project/main.py"), "kind": "modified"},
        {"path": fs_path("outside.py"), "kind": "modified"},
    ]
    for snap, content in [
        ("fsnap-base", b"VALUE = 1\n"),
        ("fsnap-head", b"VALUE = 2\n"),
    ]:
        client.files[(snap, "project/main.py")] = content
        client.entries[(snap, "project/main.py")] = file_entry(
            "project/main.py", content
        )
    client.entries[("fsnap-head", "outside.py")] = file_entry("outside.py", b"outside")
    reader = ProviderArtifactReader(lambda: client)
    comparison = ArtifactComparison(
        Path("/unused"), snapshot("fsnap-base"), snapshot("fsnap-head"), "project"
    )
    changed = reader.changed_files(comparison)
    assert changed == ["../outside.py", "main.py"]
    before = dict(client.files)
    result = reader.diff(
        comparison, paths=changed, max_bytes=4096, function_context=True
    )
    assert "+VALUE = 2" in result.text and not result.truncated
    assert client.files == before
    assert {name for name, _ in client.operations} <= {
        "fs.snapshot.diff.all",
        "fs.snapshot.stat",
        "fs.snapshot.pread",
    }
    assert all(
        args.get("path") != "outside.py"
        for name, args in client.operations if name == "fs.snapshot.pread"
    )


def test_snapshot_path_escape_is_rejected_before_provider_access():
    client = SnapshotClient()
    reader = ProviderArtifactReader(lambda: client)
    comparison = ArtifactComparison(
        Path("/unused"), snapshot("fsnap-base"), snapshot("fsnap-head"), "project"
    )
    with pytest.raises(ValueError, match="escapes"):
        reader.diff(
            comparison, paths=["/absolute.py"], max_bytes=4096, function_context=False
        )
    assert client.operations == []
