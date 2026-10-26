from __future__ import annotations

import base64
import difflib
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
    decode_snapshot_pread,
)
from goal_plus.workspaces import IGNORED_NAMES, IGNORED_SUFFIXES

MAX_ARTIFACT_VIEW_READ_BYTES = 1024 * 1024
MAX_ARTIFACT_VIEW_ENTRIES = 4096


def _snapshot_id(reference: ArtifactRef) -> str:
    if reference.kind != "fs_snapshot" or reference.provider != "thinkthread":
        raise TypeError("ThinkThread artifacts require an fs_snapshot reference")
    return reference.id


def _safe_path(value: str) -> str:
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Artifact View paths must remain snapshot-relative")
    return path.as_posix()


def fs_path_text(value: Any) -> str:
    if not isinstance(value, dict):
        raise AgentPosixBridgeError("Agent POSIX filesystem path is not an object")
    utf8 = value.get("utf8")
    if not isinstance(utf8, str):
        encoded = value.get("bytesBase64")
        detail = encoded if isinstance(encoded, str) else "unavailable"
        raise AgentPosixBridgeError(
            "ThinkThread snapshot contains a non-UTF-8 path that cannot be "
            f"validated against the Search edit surface: {detail}"
        )
    return _safe_path(utf8)


def _validated_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentPosixBridgeError("Agent POSIX filesystem entry is not an object")
    fs_path_text(value.get("path"))
    kind = value.get("kind")
    if kind not in {"file", "directory", "symlink"}:
        raise AgentPosixBridgeError("Agent POSIX filesystem entry has an invalid kind")
    if kind == "symlink":
        fs_path_text(value.get("symlinkTarget"))
    return value


def candidate_artifact_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).name == "results.tsv":
        return False
    if any(part in IGNORED_NAMES for part in parts):
        return False
    return not any(path.endswith(suffix) for suffix in IGNORED_SUFFIXES)


def _artifact_view_path(value: str, *, allow_root: bool = False) -> str:
    path = _safe_path(value)
    if path == "." and allow_root:
        return path
    if not candidate_artifact_path(path):
        raise PermissionError(f"Artifact View private path is unavailable: {path}")
    return path


class ThinkThreadSnapshotReader:
    """Read immutable ThinkThread snapshots through the official Agent POSIX ABI."""

    def __init__(self, client: AgentPosixSdkClient) -> None:
        self.client = client

    def changes(
        self,
        base: ArtifactRef,
        target: ArtifactRef,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for raw in self.client.snapshot_diff_all(
            _snapshot_id(base),
            _snapshot_id(target),
        ):
            path = fs_path_text(raw.get("path"))
            if path in result:
                raise AgentPosixBridgeError(
                    f"fs.snapshot.diff returned duplicate path {path!r}"
                )
            result[path] = raw
        return result

    def changed_files(
        self,
        base: ArtifactRef,
        target: ArtifactRef,
    ) -> list[str]:
        base_id = _snapshot_id(base)
        target_id = _snapshot_id(target)
        changed: list[str] = []
        for path, change in self.changes(base, target).items():
            if not candidate_artifact_path(path):
                continue
            snapshot_id = base_id if change.get("kind") == "deleted" else target_id
            entry = self.client.invoke(
                "fs.snapshot.stat",
                {"snapshotId": snapshot_id, "path": path},
            )
            _validated_entry(entry)
            if entry.get("kind") != "directory":
                changed.append(path)
        return sorted(changed)

    def read_file(
        self,
        artifact: ArtifactRef,
        path: str,
        *,
        max_bytes: int = MAX_ARTIFACT_VIEW_READ_BYTES,
    ) -> bytes:
        return self.client.snapshot_read_file(
            _snapshot_id(artifact),
            _safe_path(path),
            max_bytes=max_bytes,
        )

    def stat(self, artifact: ArtifactRef, path: str = ".") -> dict[str, Any]:
        entry = self.client.invoke(
            "fs.snapshot.stat",
            {"snapshotId": _snapshot_id(artifact), "path": _safe_path(path)},
        )
        return _validated_entry(entry)

    def file_digest(self, artifact: ArtifactRef, path: str) -> str:
        normalized = _safe_path(path)
        entry = self.stat(artifact, normalized)
        if entry.get("kind") != "file":
            raise AgentPosixBridgeError(
                f"ThinkThread snapshot path is not a file: {normalized}"
            )
        length = entry.get("len")
        if not isinstance(length, int) or length < 0:
            raise AgentPosixBridgeError("fs.snapshot.stat omitted file len")
        return self._file_digest(_snapshot_id(artifact), normalized, length)

    def _read_prefix(self, snapshot_id: str, path: str, length: int) -> bytes:
        output = bytearray()
        while len(output) < length:
            offset = len(output)
            requested_length = min(64 * 1024, length - offset)
            result = self.client.invoke(
                "fs.snapshot.pread",
                {
                    "snapshotId": snapshot_id,
                    "path": path,
                    "offset": offset,
                    "length": requested_length,
                },
            )
            chunk, eof = decode_snapshot_pread(
                result,
                expected_offset=offset,
                requested_length=requested_length,
            )
            if not chunk:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread reached EOF before the stat length"
                )
            output.extend(chunk)
            if eof and len(output) < length:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread reached EOF before the stat length"
                )
        return bytes(output)

    def _file_digest(self, snapshot_id: str, path: str, length: int) -> str:
        digest = hashlib.sha256()
        offset = 0
        while offset < length:
            requested_length = min(64 * 1024, length - offset)
            result = self.client.invoke(
                "fs.snapshot.pread",
                {
                    "snapshotId": snapshot_id,
                    "path": path,
                    "offset": offset,
                    "length": requested_length,
                },
            )
            chunk, eof = decode_snapshot_pread(
                result,
                expected_offset=offset,
                requested_length=requested_length,
            )
            if not chunk:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread returned no bytes before expected EOF"
                )
            digest.update(chunk)
            offset += len(chunk)
            if eof and offset < length:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread reached EOF before the stat length"
                )
        return digest.hexdigest()

    def _entry_projection(
        self,
        snapshot_id: str,
        path: str,
        *,
        absent: bool,
        max_bytes: int,
    ) -> bytes:
        if absent:
            return b""
        entry = self.client.invoke(
            "fs.snapshot.stat",
            {"snapshotId": snapshot_id, "path": path},
        )
        _validated_entry(entry)
        kind = entry.get("kind")
        if kind == "file":
            length = entry.get("len")
            if not isinstance(length, int) or length < 0:
                raise AgentPosixBridgeError("fs.snapshot.stat omitted file len")
            if length > max_bytes:
                prefix = self._read_prefix(snapshot_id, path, max_bytes)
                return prefix + b"\n[content truncated]\n"
            return self.client.snapshot_read_file(
                snapshot_id,
                path,
                max_bytes=max_bytes,
            )
        if kind == "symlink":
            return (fs_path_text(entry.get("symlinkTarget")) + "\n").encode()
        return (
            json.dumps(
                {"kind": kind, "mode": entry.get("mode"), "len": entry.get("len")},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    def diff(
        self,
        base: ArtifactRef,
        target: ArtifactRef,
        *,
        paths: list[str] | None = None,
        max_bytes: int = MAX_ARTIFACT_VIEW_READ_BYTES,
    ) -> tuple[str, bool]:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        changes = self.changes(base, target)
        selected = (
            sorted(path for path in changes if candidate_artifact_path(path))
            if paths is None
            else [_safe_path(path) for path in paths]
        )
        base_id = _snapshot_id(base)
        target_id = _snapshot_id(target)
        output = bytearray()
        truncated = False
        for path in selected:
            change = changes.get(path)
            if change is None:
                raise ValueError(f"path is not changed between snapshots: {path}")
            kind = change.get("kind")
            before = self._entry_projection(
                base_id,
                path,
                absent=kind == "added",
                max_bytes=max_bytes,
            ).decode("utf-8", errors="replace")
            after = self._entry_projection(
                target_id,
                path,
                absent=kind == "deleted",
                max_bytes=max_bytes,
            ).decode("utf-8", errors="replace")
            piece = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            if not piece:
                piece = (
                    f"--- a/{path}\n+++ b/{path}\n@@ metadata change @@\n"
                    + json.dumps(
                        {"path": path, "kind": kind},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            encoded = piece.encode("utf-8", errors="replace")
            remaining = max_bytes - len(output)
            if len(encoded) > remaining:
                output.extend(encoded[: max(0, remaining)])
                truncated = True
                break
            output.extend(encoded)
        return output.decode("utf-8", errors="replace"), truncated

    def canonical_digest(self, base: ArtifactRef, target: ArtifactRef) -> str:
        target_id = _snapshot_id(target)
        manifest: list[dict[str, Any]] = []
        for path, change in sorted(self.changes(base, target).items()):
            if not candidate_artifact_path(path):
                continue
            if change.get("kind") == "deleted":
                manifest.append({"path": path, "kind": "deleted"})
                continue
            entry = self.client.invoke(
                "fs.snapshot.stat",
                {"snapshotId": target_id, "path": path},
            )
            _validated_entry(entry)
            item: dict[str, Any] = {
                "path": path,
                "kind": entry.get("kind"),
                "mode": entry.get("mode"),
                "len": entry.get("len"),
            }
            if entry.get("kind") == "file":
                length = entry.get("len")
                if not isinstance(length, int) or length < 0:
                    raise AgentPosixBridgeError("fs.snapshot.stat omitted file len")
                item["sha256"] = self._file_digest(target_id, path, length)
            elif entry.get("kind") == "symlink":
                item["symlink_target"] = fs_path_text(entry.get("symlinkTarget"))
            manifest.append(item)
        return hashlib.sha256(
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()


class ThinkThreadArtifactViewRouter:
    """Serve bounded reads against an explicit immutable view allowlist."""

    def __init__(
        self,
        client: AgentPosixSdkClient,
        views: dict[str, ArtifactRef],
    ) -> None:
        self.client = client
        self.views = dict(views)

    def _view(self, view_id: str) -> ArtifactRef:
        try:
            reference = self.views[view_id]
        except KeyError as exc:
            raise ValueError(f"unknown Artifact View: {view_id}") from exc
        _snapshot_id(reference)
        return reference

    def stat(self, view_id: str, path: str = ".") -> dict[str, Any]:
        reference = self._view(view_id)
        entry = self.client.invoke(
            "fs.snapshot.stat",
            {
                "snapshotId": reference.id,
                "path": _artifact_view_path(path, allow_root=True),
            },
        )
        return _validated_entry(entry)

    def readdir(self, view_id: str, path: str = ".") -> list[dict[str, Any]]:
        reference = self._view(view_id)
        entries = self.client.snapshot_readdir_all(
            reference.id,
            _artifact_view_path(path, allow_root=True),
        )
        if len(entries) > MAX_ARTIFACT_VIEW_ENTRIES:
            raise ValueError("Artifact View directory exceeds the bounded entry limit")
        validated = [_validated_entry(entry) for entry in entries]
        return [
            entry
            for entry in validated
            if candidate_artifact_path(fs_path_text(entry.get("path")))
        ]

    def pread(
        self,
        view_id: str,
        path: str,
        *,
        offset: int = 0,
        length: int = 64 * 1024,
    ) -> dict[str, Any]:
        if offset < 0 or length < 1 or length > 64 * 1024:
            raise ValueError("Artifact View pread range is invalid")
        reference = self._view(view_id)
        result = self.client.invoke(
            "fs.snapshot.pread",
            {
                "snapshotId": reference.id,
                "path": _artifact_view_path(path),
                "offset": offset,
                "length": length,
            },
        )
        decode_snapshot_pread(
            result,
            expected_offset=offset,
            requested_length=length,
        )
        return result

    def diff(
        self,
        base_view_id: str,
        target_view_id: str,
    ) -> list[dict[str, Any]]:
        base = self._view(base_view_id)
        target = self._view(target_view_id)
        changes = self.client.snapshot_diff_all(base.id, target.id)
        validated = [
            (fs_path_text(change.get("path")), change)
            for change in changes
        ]
        return [
            change
            for path, change in validated
            if candidate_artifact_path(path)
        ]


def decode_fs_run_output(result: dict[str, Any]) -> tuple[str, str]:
    chunks = result.get("outputChunks")
    if not isinstance(chunks, list):
        raise AgentPosixBridgeError("fs.run omitted outputChunks")
    ordered: list[tuple[int, Literal["stdout", "stderr"], bytes]] = []
    seen: set[int] = set()
    for raw in chunks:
        if not isinstance(raw, dict):
            raise AgentPosixBridgeError("fs.run output chunk is not an object")
        sequence = raw.get("sequence")
        stream = raw.get("stream")
        encoded = raw.get("dataBase64")
        if (
            not isinstance(sequence, int)
            or sequence < 0
            or sequence in seen
            or stream not in {"stdout", "stderr"}
            or not isinstance(encoded, str)
        ):
            raise AgentPosixBridgeError("fs.run returned an invalid output chunk")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise AgentPosixBridgeError("fs.run returned invalid output base64") from exc
        seen.add(sequence)
        ordered.append((sequence, stream, data))
    ordered.sort(key=lambda item: item[0])
    stdout = b"".join(data for _, stream, data in ordered if stream == "stdout")
    stderr = b"".join(data for _, stream, data in ordered if stream == "stderr")
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
