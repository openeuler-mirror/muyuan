from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from goal_plus.ports.thinkthread import AgentPosixBridgeError, AgentPosixSdkMismatch, capability_ids

AGENT_POSIX_CONTRACT_FINGERPRINT = (
    "fcc80b665cd990f9d1e3681a9d384cb99994f2b739cd4fbddc97bdda01391131"
)
AGENT_POSIX_CONTROL_PROTOCOL_VERSION = 2


def new_request_id() -> str:
    return f"req-{uuid.uuid4()}"


def decode_snapshot_pread(
    result: object,
    *,
    expected_offset: int,
    requested_length: int,
) -> tuple[bytes, bool]:
    if not isinstance(result, dict):
        raise AgentPosixBridgeError("fs.snapshot.pread returned a non-object result")
    response_offset = result.get("offset")
    if (
        not isinstance(response_offset, int)
        or isinstance(response_offset, bool)
        or response_offset != expected_offset
    ):
        raise AgentPosixBridgeError("fs.snapshot.pread returned an invalid offset")
    encoded = result.get("dataBase64")
    if not isinstance(encoded, str):
        raise AgentPosixBridgeError("fs.snapshot.pread omitted dataBase64")
    try:
        chunk = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise AgentPosixBridgeError(
            "fs.snapshot.pread returned invalid base64"
        ) from exc
    bytes_read = result.get("bytesRead")
    if (
        not isinstance(bytes_read, int)
        or isinstance(bytes_read, bool)
        or bytes_read < 0
        or bytes_read != len(chunk)
        or bytes_read > requested_length
    ):
        raise AgentPosixBridgeError("fs.snapshot.pread returned invalid bytesRead")
    eof = result.get("eof")
    if not isinstance(eof, bool):
        raise AgentPosixBridgeError("fs.snapshot.pread returned an invalid eof flag")
    if bytes_read == 0 and not eof:
        raise AgentPosixBridgeError("fs.snapshot.pread returned no bytes before EOF")
    return chunk, eof


def validate_thinkthread_root(client: AgentPosixSdkClient) -> dict[str, Any]:
    """Fail closed unless the caller is the supported Pi Root controller."""

    client.preflight()
    root = client.self_view()
    if root.get("parentThinkthreadId") is not None:
        raise RuntimeError("ThinkThread pool controller must run in a Root")
    required = {
        "thinkthread.child",
        "thinkthread.message",
        "thinkthread.fs.self",
        "thinkthread.fs",
    }
    missing = required - capability_ids(root.get("capabilities"))
    if missing:
        raise RuntimeError(
            "ThinkThread Root lacks capabilities: " + ", ".join(sorted(missing))
        )
    profiles = root.get("profiles")
    profile = (
        next(
            (
                item
                for item in profiles
                if isinstance(item, dict) and item.get("alias") == "self"
            ),
            None,
        )
        if isinstance(profiles, list)
        else None
    )
    if not isinstance(profile, dict) or profile.get("agent") != "pi":
        raise RuntimeError("ThinkThread Profile self must select the Pi Agent")
    derivations = {
        (item.get("session"), item.get("fs"))
        for item in profile.get("allowedDerivations", [])
        if isinstance(item, dict)
    }
    if ("fresh", "private_branch") not in derivations:
        raise RuntimeError(
            "ThinkThread Profile self must allow a fresh private branch derivation"
        )
    delegated = capability_ids(profile.get("allowedCapabilities"))
    if delegated != {"thinkthread.message", "thinkthread.fs.self"}:
        raise RuntimeError(
            "ThinkThread Profile self must delegate only Message and fs.self"
        )
    attachment = client.invoke("fs.stat")
    if attachment.get("kind") != "direct" or attachment.get("state") != "attached":
        raise RuntimeError("ThinkThread Root filesystem must be directly attached")
    storage = attachment.get("storage")
    if isinstance(storage, dict):
        if int(storage.get("activeRequestCount", 0)) >= int(
            storage.get("activeRequestLimit", 1)
        ):
            raise RuntimeError("ThinkThread active filesystem request quota is exhausted")
        if int(storage.get("snapshotCount", 0)) + int(
            storage.get("pendingSnapshotCreations", 0)
        ) >= int(storage.get("snapshotLimit", 1)):
            raise RuntimeError("ThinkThread snapshot quota is exhausted")
    return root


class AgentPosixSdkClient:
    """Thin process bridge to ThinkThread's official TypeScript SDK.

    The bridge owns no protocol DTOs and never accesses the Control socket
    directly. Each call executes exactly one validated SDK operation in the
    caller's Root or Child execution domain.
    """

    supports_detached_service = True

    def __init__(
        self,
        *,
        bridge_path: Path | str | None = None,
        node: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        configured_bridge = bridge_path or os.environ.get(
            "GOAL_PLUS_AGENT_POSIX_BRIDGE"
        )
        self.bridge_path = (
            Path(configured_bridge).expanduser().resolve()
            if configured_bridge
            else Path(__file__).resolve().parent
            / "assets"
            / "thinkthread-agent-posix-bridge.mjs"
        )
        self.node = node or os.environ.get("GOAL_PLUS_NODE", "node")
        self.environment = environment

    def _env(self) -> dict[str, str]:
        environment = (
            os.environ.copy() if self.environment is None else dict(self.environment)
        )
        # Pi's TypeScript loader uses NODE_OPTIONS=--import=... for its own
        # process. ThinkThread rejects that loader injection in nested Node
        # processes, and the standalone SDK bridge does not need it.
        environment.pop("NODE_OPTIONS", None)
        return environment

    def invoke(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        if not self.bridge_path.is_file():
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge is missing: {self.bridge_path}"
            )
        request = json.dumps(
            {"operation": operation, "params": params or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            completed = subprocess.run(
                [self.node, str(self.bridge_path)],
                input=request,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge requires Node.js: {self.node}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge timed out during {operation}",
                error={
                    "category": "transport",
                    "delivery": "completion_unknown",
                    "operation": operation,
                },
            ) from exc
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            stderr = completed.stderr.strip()
            detail = f": {stderr[-2000:]}" if stderr else ""
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge returned invalid JSON during {operation} "
                f"(exit {completed.returncode}){detail}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge returned an invalid envelope during {operation}"
            )
        if payload["ok"] is not True:
            error = payload.get("error")
            normalized = error if isinstance(error, dict) else {}
            message = normalized.get("message")
            raise AgentPosixBridgeError(
                str(message or f"Agent POSIX operation {operation} failed"),
                error=normalized,
            )
        if completed.returncode != 0:
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge exited {completed.returncode} after success"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AgentPosixBridgeError(
                f"Agent POSIX operation {operation} returned a non-object result"
            )
        return result

    def preflight(self) -> dict[str, Any]:
        result = self.invoke("bridge.meta")
        fingerprint = result.get("contractFingerprint")
        protocol = result.get("controlProtocolVersion")
        if (
            fingerprint != AGENT_POSIX_CONTRACT_FINGERPRINT
            or protocol != AGENT_POSIX_CONTROL_PROTOCOL_VERSION
        ):
            logging.getLogger(__name__).warning(
                "ThinkThread Agent POSIX SDK differs from the tested baseline: "
                f"fingerprint={fingerprint!r}, protocol={protocol!r}"
            )
        return result

    def self_view(self) -> dict[str, Any]:
        return self.invoke("self")

    def snapshot_diff_all(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
        *,
        page_limit: int = 256,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(page_limit, int)
            or isinstance(page_limit, bool)
            or page_limit <= 0
        ):
            raise ValueError("snapshot diff page_limit must be a positive integer")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        changes: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "baseSnapshotId": base_snapshot_id,
                "targetSnapshotId": target_snapshot_id,
                "limit": page_limit,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = self.invoke("fs.snapshot.diff", params)
            raw_changes = page.get("changes")
            if not isinstance(raw_changes, list):
                raise AgentPosixBridgeError("fs.snapshot.diff omitted changes")
            if any(not isinstance(item, dict) for item in raw_changes):
                raise AgentPosixBridgeError(
                    "fs.snapshot.diff returned a non-object change"
                )
            changes.extend(raw_changes)
            has_more = page.get("hasMore")
            if not isinstance(has_more, bool):
                raise AgentPosixBridgeError(
                    "fs.snapshot.diff returned an invalid hasMore flag"
                )
            if not has_more:
                return changes
            next_cursor = page.get("nextCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
                or next_cursor in seen_cursors
            ):
                raise AgentPosixBridgeError(
                    "fs.snapshot.diff hasMore=true without a new nextCursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def snapshot_read_file(
        self,
        snapshot_id: str,
        path: str,
        *,
        max_bytes: int = 1024 * 1024,
        chunk_bytes: int = 64 * 1024,
    ) -> bytes:
        if max_bytes < 0:
            raise ValueError("snapshot max_bytes must be non-negative")
        if (
            not isinstance(chunk_bytes, int)
            or isinstance(chunk_bytes, bool)
            or chunk_bytes <= 0
            or chunk_bytes > 64 * 1024
        ):
            raise ValueError("snapshot chunk_bytes must be between 1 and 65536")
        offset = 0
        output = bytearray()
        while True:
            requested_length = min(chunk_bytes, max_bytes + 1 - len(output))
            result = self.invoke(
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
            output.extend(chunk)
            if len(output) > max_bytes:
                raise ValueError(f"snapshot file exceeds {max_bytes} bytes: {path}")
            if eof:
                return bytes(output)
            offset += len(chunk)

    def snapshot_readdir_all(
        self,
        snapshot_id: str,
        path: str,
        *,
        page_limit: int = 256,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(page_limit, int)
            or isinstance(page_limit, bool)
            or page_limit <= 0
        ):
            raise ValueError("snapshot readdir page_limit must be a positive integer")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        entries: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "snapshotId": snapshot_id,
                "path": path,
                "limit": page_limit,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = self.invoke("fs.snapshot.readdir", params)
            raw_entries = page.get("entries")
            if not isinstance(raw_entries, list):
                raise AgentPosixBridgeError("fs.snapshot.readdir omitted entries")
            if any(not isinstance(item, dict) for item in raw_entries):
                raise AgentPosixBridgeError(
                    "fs.snapshot.readdir returned a non-object entry"
                )
            entries.extend(raw_entries)
            has_more = page.get("hasMore")
            if not isinstance(has_more, bool):
                raise AgentPosixBridgeError(
                    "fs.snapshot.readdir returned an invalid hasMore flag"
                )
            if not has_more:
                return entries
            next_cursor = page.get("nextCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
                or next_cursor in seen_cursors
            ):
                raise AgentPosixBridgeError(
                    "fs.snapshot.readdir hasMore=true without a new nextCursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
