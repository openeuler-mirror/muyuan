from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from goal_plus.models import ArtifactRef
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError
from goal_plus.thinkthread_artifacts import (
    ThinkThreadArtifactViewRouter,
    ThinkThreadSnapshotReader,
    decode_fs_run_output,
)


def snapshot(snapshot_id: str) -> ArtifactRef:
    return ArtifactRef(
        kind="fs_snapshot",
        id=snapshot_id,
        provider="thinkthread",
    )


def fs_path(path: str) -> dict[str, str]:
    return {
        "utf8": path,
        "bytesBase64": base64.b64encode(path.encode()).decode("ascii"),
    }


class SnapshotClient:
    def __init__(self) -> None:
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self.changes: list[dict[str, Any]] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}
        self.directory_entries: list[dict[str, Any]] = []
        self.directory_entries_by_path: dict[
            tuple[str, str], list[dict[str, Any]]
        ] = {}
        self.pread_overrides: dict[str, Any] = {}

    def snapshot_diff_all(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
    ) -> list[dict[str, Any]]:
        self.operations.append(
            (
                "fs.snapshot.diff.all",
                {
                    "baseSnapshotId": base_snapshot_id,
                    "targetSnapshotId": target_snapshot_id,
                },
            )
        )
        return list(self.changes)

    def snapshot_readdir_all(
        self,
        snapshot_id: str,
        path: str,
    ) -> list[dict[str, Any]]:
        self.operations.append(
            ("fs.snapshot.readdir.all", {"snapshotId": snapshot_id, "path": path})
        )
        return list(
            self.directory_entries_by_path.get(
                (snapshot_id, path),
                self.directory_entries,
            )
        )

    def snapshot_read_file(
        self,
        snapshot_id: str,
        path: str,
        *,
        max_bytes: int,
    ) -> bytes:
        content = self.files[(snapshot_id, path)]
        if len(content) > max_bytes:
            raise ValueError("file too large")
        return content

    def invoke(
        self,
        operation: str,
        params: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.operations.append((operation, dict(params)))
        if operation == "fs.snapshot.stat":
            return dict(self.entries[(str(params["snapshotId"]), str(params["path"]))])
        if operation == "fs.snapshot.pread":
            content = self.files[(str(params["snapshotId"]), str(params["path"]))]
            offset = int(params["offset"])
            length = int(params["length"])
            chunk = content[offset : offset + length]
            return {
                "offset": offset,
                "dataBase64": base64.b64encode(chunk).decode("ascii"),
                "bytesRead": len(chunk),
                "eof": offset + len(chunk) >= len(content),
                **self.pread_overrides,
            }
        raise AssertionError(operation)


def file_entry(path: str, content: bytes, *, mode: int = 0o644) -> dict[str, Any]:
    return {
        "path": fs_path(path),
        "kind": "file",
        "mode": mode,
        "len": len(content),
    }


def test_snapshot_reader_binds_changed_files_diff_and_digest_to_exact_snapshot() -> None:
    client = SnapshotClient()
    client.changes = [
        {"path": fs_path("src/model.py"), "kind": "modified"},
        {"path": fs_path("results.tsv"), "kind": "modified"},
        {"path": fs_path("src/old.py"), "kind": "deleted"},
    ]
    before = b"score = 1\n"
    after = b"score = 2\n"
    old = b"obsolete = True\n"
    client.files.update(
        {
            ("fsnap-S0", "src/model.py"): before,
            ("fsnap-Sk", "src/model.py"): after,
            ("fsnap-S0", "src/old.py"): old,
        }
    )
    client.entries.update(
        {
            ("fsnap-S0", "src/model.py"): file_entry("src/model.py", before),
            ("fsnap-Sk", "src/model.py"): file_entry("src/model.py", after),
            ("fsnap-S0", "src/old.py"): file_entry("src/old.py", old),
        }
    )
    reader = ThinkThreadSnapshotReader(client)  # type: ignore[arg-type]

    assert reader.changed_files(snapshot("fsnap-S0"), snapshot("fsnap-Sk")) == [
        "src/model.py",
        "src/old.py",
    ]
    diff, omitted = reader.diff(snapshot("fsnap-S0"), snapshot("fsnap-Sk"))
    assert omitted is False
    assert "-score = 1" in diff
    assert "+score = 2" in diff
    assert "-obsolete = True" in diff
    expected_manifest = (
        '[{"kind":"file","len":10,"mode":420,"path":"src/model.py",'
        f'"sha256":"{hashlib.sha256(after).hexdigest()}"}},'
        '{"kind":"deleted","path":"src/old.py"}]'
    )
    assert reader.canonical_digest(
        snapshot("fsnap-S0"), snapshot("fsnap-Sk")
    ) == hashlib.sha256(expected_manifest.encode()).hexdigest()


def test_snapshot_reader_streams_large_file_digest_and_bounds_diff() -> None:
    client = SnapshotClient()
    before = b"a" * (3 * 64 * 1024 + 7)
    after = b"b" * (3 * 64 * 1024 + 9)
    client.changes = [{"path": fs_path("large.bin"), "kind": "modified"}]
    client.files[("fsnap-S0", "large.bin")] = before
    client.files[("fsnap-Sk", "large.bin")] = after
    client.entries[("fsnap-S0", "large.bin")] = file_entry("large.bin", before)
    client.entries[("fsnap-Sk", "large.bin")] = file_entry("large.bin", after)
    reader = ThinkThreadSnapshotReader(client)  # type: ignore[arg-type]

    diff, omitted = reader.diff(
        snapshot("fsnap-S0"), snapshot("fsnap-Sk"), max_bytes=128
    )
    assert omitted is True
    assert len(diff.encode()) == 128
    digest = reader.canonical_digest(snapshot("fsnap-S0"), snapshot("fsnap-Sk"))
    expected_manifest = json.dumps(
        [{"kind": "file", "len": len(after), "mode": 0o644,
          "path": "large.bin", "sha256": hashlib.sha256(after).hexdigest()}],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert digest == hashlib.sha256(expected_manifest.encode()).hexdigest()
    preads = [params for operation, params in client.operations if operation == "fs.snapshot.pread"]
    assert len(preads) >= 4
    assert all(int(params["length"]) <= 64 * 1024 for params in preads)

    router = ThinkThreadArtifactViewRouter(
        client,  # type: ignore[arg-type]
        {"target": snapshot("fsnap-Sk")},
    )
    chunks: list[bytes] = []
    offset = 0
    while offset < len(after):
        result = router.pread("target", "large.bin", offset=offset)
        chunk = base64.b64decode(result["dataBase64"])
        assert chunk
        chunks.append(chunk)
        offset += len(chunk)
    assert b"".join(chunks) == after


def test_snapshot_digest_accepts_missing_terminal_eof_at_stat_length() -> None:
    client = SnapshotClient()
    content = b"provider terminal eof compatibility\n"
    client.files[("fsnap-target", "file.txt")] = content
    client.entries[("fsnap-target", "file.txt")] = file_entry(
        "file.txt", content
    )
    client.pread_overrides = {"eof": False}
    reader = ThinkThreadSnapshotReader(client)  # type: ignore[arg-type]

    digest = reader.file_digest(snapshot("fsnap-target"), "file.txt")

    assert digest == hashlib.sha256(content).hexdigest()


def test_snapshot_reader_rejects_non_utf8_or_escaping_changed_paths() -> None:
    client = SnapshotClient()
    reader = ThinkThreadSnapshotReader(client)  # type: ignore[arg-type]
    client.changes = [
        {"path": {"utf8": None, "bytesBase64": "/wA="}, "kind": "modified"}
    ]
    with pytest.raises(AgentPosixBridgeError, match="non-UTF-8"):
        reader.changes(snapshot("fsnap-S0"), snapshot("fsnap-Sk"))

    client.changes = [{"path": fs_path("../root.txt"), "kind": "modified"}]
    with pytest.raises(ValueError, match="snapshot-relative"):
        reader.changes(snapshot("fsnap-S0"), snapshot("fsnap-Sk"))


def test_artifact_view_router_authorizes_only_frozen_views_and_read_operations() -> None:
    client = SnapshotClient()
    target_entry = file_entry("src/target.py", b"target\n")
    peer_entry = file_entry("src/peer.py", b"peer\n")
    client.entries[("fsnap-target", "src/target.py")] = target_entry
    client.entries[("fsnap-peer", "src/peer.py")] = peer_entry
    client.directory_entries = [target_entry]
    client.changes = [{"path": fs_path("src/target.py"), "kind": "modified"}]
    client.files[("fsnap-target", "src/target.py")] = b"target\n"
    router = ThinkThreadArtifactViewRouter(
        client,  # type: ignore[arg-type]
        {"target": snapshot("fsnap-target"), "peer": snapshot("fsnap-peer")},
    )

    assert router.stat("target", "src/target.py")["kind"] == "file"
    assert router.readdir("target", ".") == [target_entry]
    assert base64.b64decode(
        router.pread("target", "src/target.py", length=7)["dataBase64"]
    ) == b"target\n"
    assert router.diff("peer", "target") == client.changes
    assert {operation for operation, _ in client.operations} <= {
        "fs.snapshot.stat",
        "fs.snapshot.readdir.all",
        "fs.snapshot.pread",
        "fs.snapshot.diff.all",
    }

    with pytest.raises(PermissionError, match="private path"):
        router.stat("target", ".git/HEAD")
    with pytest.raises(PermissionError, match="private path"):
        router.pread("target", "results.tsv", length=1)

    with pytest.raises(ValueError, match="unknown Artifact View"):
        router.stat("root", ".")
    with pytest.raises(ValueError, match="snapshot-relative"):
        router.pread("target", "../secret", length=1)


def test_artifact_view_router_rejects_non_utf8_directory_entries() -> None:
    client = SnapshotClient()
    client.directory_entries = [
        {
            "path": {"utf8": None, "bytesBase64": "/wA="},
            "kind": "file",
            "mode": 0o644,
            "len": 1,
        }
    ]
    router = ThinkThreadArtifactViewRouter(
        client,  # type: ignore[arg-type]
        {"target": snapshot("fsnap-target")},
    )

    with pytest.raises(AgentPosixBridgeError, match="non-UTF-8"):
        router.readdir("target")


def test_artifact_view_filters_private_entries_without_snapshot_mutation() -> None:
    client = SnapshotClient()
    source_entry = {
        "path": fs_path("src"),
        "kind": "directory",
        "mode": 0o755,
        "len": 0,
    }
    public_entry = file_entry("src/program.py", b"public\n")
    git_entry = {
        "path": fs_path("src/.git"),
        "kind": "directory",
        "mode": 0o755,
        "len": 0,
    }
    ledger_entry = file_entry("src/results.tsv", b"private\n")
    client.directory_entries_by_path = {
        ("fsnap-target", "."): [source_entry],
        ("fsnap-target", "src"): [public_entry, git_entry, ledger_entry],
    }
    client.entries[("fsnap-target", "src/.git")] = git_entry
    client.entries[("fsnap-target", "src/results.tsv")] = ledger_entry
    client.changes = [
        {"path": fs_path("src/program.py"), "kind": "modified"},
        {"path": fs_path("src/.git/HEAD"), "kind": "modified"},
        {"path": fs_path("src/results.tsv"), "kind": "modified"},
    ]
    router = ThinkThreadArtifactViewRouter(
        client,  # type: ignore[arg-type]
        {"target": snapshot("fsnap-target"), "base": snapshot("fsnap-base")},
    )

    assert router.readdir("target", "src") == [public_entry]
    assert router.diff("base", "target") == [client.changes[0]]
    assert {operation for operation, _ in client.operations} == {
        "fs.snapshot.readdir.all",
        "fs.snapshot.diff.all",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"offset": 7},
        {"bytesRead": 1},
        {"eof": "yes"},
    ],
)
def test_snapshot_digest_and_artifact_view_share_strict_pread_validation(
    override: dict[str, Any],
) -> None:
    client = SnapshotClient()
    content = b"content\n"
    client.files[("fsnap-target", "file.txt")] = content
    client.entries[("fsnap-target", "file.txt")] = file_entry(
        "file.txt", content
    )
    client.pread_overrides = override
    reader = ThinkThreadSnapshotReader(client)  # type: ignore[arg-type]
    router = ThinkThreadArtifactViewRouter(
        client,  # type: ignore[arg-type]
        {"target": snapshot("fsnap-target")},
    )

    with pytest.raises(AgentPosixBridgeError):
        reader.file_digest(snapshot("fsnap-target"), "file.txt")
    with pytest.raises(AgentPosixBridgeError):
        router.pread("target", "file.txt", length=len(content))


def test_decode_fs_run_output_orders_stream_chunks() -> None:
    result = {
        "outputChunks": [
            {
                "sequence": 2,
                "stream": "stderr",
                "dataBase64": base64.b64encode(b"problem\n").decode(),
            },
            {
                "sequence": 1,
                "stream": "stdout",
                "dataBase64": base64.b64encode(b"score=1\n").decode(),
            },
        ]
    }

    assert decode_fs_run_output(result) == ("score=1\n", "problem\n")
    result["outputChunks"].append(result["outputChunks"][0])
    with pytest.raises(AgentPosixBridgeError, match="invalid output chunk"):
        decode_fs_run_output(result)
