"""Local diagnostic archives from already bound TT Children; never execution authority."""

import base64
import hashlib
import io
import gzip
import json
import logging
import os
from pathlib import Path
import re
import time

from goal_plus.execution.thinkthread_rpc import MESSAGE_RECEIVE_LIMIT, PROTOCOL, RESPONSE_CHUNK_BYTES, message_payload
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json

# Match the extension's bounded, compressed diagnostic Message batches.
MAX_CHUNKS = 32
MAX_DECODED_BYTES = 16 * 1024 * 1024
COLLECTION_SECONDS = 5.0
logger = logging.getLogger(__name__)


def _directory(root, binding):
    child = binding["thinkthread_id"]
    if not isinstance(child, str) or not re.fullmatch(r"tt-[A-Za-z0-9-]+", child):
        raise ValueError("invalid diagnostic Child identity")
    return Path(root) / "host-logs" / "session-diagnostics" / child


def _identity(binding):
    return {key: binding.get(key) for key in (
        "root_thinkthread_id", "thinkthread_id", "agent_session_id", "run_id", "candidate_id",
    )}


def _index(directory, binding):
    path = directory / "index.json"
    index = load_json(path) if path.exists() else {
        "version": 1, "source": "thinkthread_messages", "agent_harness": "pi",
        "identity": _identity(binding), "batches": {}, "pending_batches": {}, "status": "unavailable",
    }
    if index["identity"] != _identity(binding):
        raise PermissionError("diagnostic archive binding mismatch")
    return index


def archive_diagnostic_message(root, binding, message):
    """Return whether this is a diagnostic envelope, including disabled/invalid ones."""
    payload = message_payload(message)
    if payload is None or payload.get("protocol") != PROTOCOL or payload.get("type") != "session_diagnostics":
        return False
    if os.environ.get("GOAL_PLUS_SESSION_DIAGNOSTICS") == "0":
        return True
    try:
        if (message.get("senderThinkthreadId") != binding.get("thinkthread_id")
                or payload.get("agent_session_id") != binding.get("agent_session_id")):
            raise PermissionError("diagnostic Message binding mismatch")
        digest, count, number = payload["batch_sha256"], payload["chunk_count"], payload["chunk_index"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid diagnostic batch digest")
        if type(count) is not int or type(number) is not int or not 1 <= count <= MAX_CHUNKS or not 0 <= number < count:
            raise ValueError("invalid diagnostic chunk identity")
        data = base64.b64decode(payload["data_base64"], validate=True)
        if len(data) > RESPONSE_CHUNK_BYTES or hashlib.sha256(data).hexdigest() != payload.get("chunk_sha256"):
            raise ValueError("invalid diagnostic chunk hash")
        directory = _directory(root, binding)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_file_lock(directory / "archive.lock"):
            index = _index(directory, binding)
            if digest in index["batches"]:
                return True
            chunk_dir = directory / digest
            write_json(chunk_dir / f"{number}.json", {"count": count, "data": payload["data_base64"]})
            index["pending_batches"][digest] = count
            index["status"] = "partial"
            write_json(directory / "index.json", index)
            if not all((chunk_dir / f"{n}.json").is_file() for n in range(count)):
                return True
            chunks = [load_json(chunk_dir / f"{n}.json") for n in range(count)]
            if any(chunk["count"] != count for chunk in chunks):
                raise ValueError("diagnostic chunk count changed")
            compressed = b"".join(base64.b64decode(chunk["data"], validate=True) for chunk in chunks)
            if hashlib.sha256(compressed).hexdigest() != digest:
                raise ValueError("diagnostic batch hash mismatch")
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(MAX_DECODED_BYTES + 1)
            if len(raw) > MAX_DECODED_BYTES:
                raise ValueError("diagnostic batch exceeds decoded limit")
            batch = json.loads(raw)
            if (batch.get("version") != 1 or not isinstance(batch.get("session_id"), str)
                    or not isinstance(batch.get("stream_id"), str)
                    or type(batch.get("sequence")) is not int or batch["sequence"] < 0
                    or not isinstance(batch.get("entries"), list)):
                raise ValueError("invalid diagnostic batch")
            if index.get("session_id", batch["session_id"]) != batch["session_id"]:
                raise ValueError("diagnostic native session changed")
            # Archive entries are diagnostic input only; no paths or commands are consumed.
            write_json(directory / f"{digest}.json", batch)
            index["session_id"] = batch["session_id"]
            index["batches"][digest] = {"stream_id": batch["stream_id"], "sequence": batch["sequence"],
                                         "entries": len(batch["entries"]), "compressed_bytes": len(compressed),
                                         "decoded_bytes": len(raw), "captured_at": batch["captured_at"],
                                         "content_sha256": hashlib.sha256(json.dumps(batch, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()}
            index["pending_batches"].pop(digest)
            index["status"] = "partial" if index["pending_batches"] else "recorded"
            write_json(directory / "index.json", index)
            for n in range(count):
                (chunk_dir / f"{n}.json").unlink()
            chunk_dir.rmdir()
    except Exception as exc:
        logger.warning("session diagnostic archive unavailable: %s", type(exc).__name__)
    return True


def collect_child_diagnostics(root, binding, client):
    """Drain only diagnostic Messages before deletion, using an independent cursor."""
    if os.environ.get("GOAL_PLUS_SESSION_DIAGNOSTICS") == "0" or not binding.get("thinkthread_id"):
        return
    try:
        directory = _directory(root, binding)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_file_lock(directory / "collect.lock"):
            index = _index(directory, binding)
            cursor = index.get("collection_cursor")
            deadline = time.monotonic() + COLLECTION_SECONDS
            while time.monotonic() < deadline:
                params = {"senderThinkthreadId": binding["thinkthread_id"], "limit": MESSAGE_RECEIVE_LIMIT}
                if cursor is not None:
                    params["after"] = cursor
                page = client.invoke("message.receive", params, timeout_seconds=max(0.1, deadline - time.monotonic()))
                if not isinstance(page.get("messages"), list) or not isinstance(page.get("nextCursor"), str):
                    raise ValueError("invalid diagnostic Message page")
                for message in page["messages"]:
                    archive_diagnostic_message(root, binding, message)
                next_cursor = page["nextCursor"]
                with exclusive_file_lock(directory / "archive.lock"):
                    index = _index(directory, binding)
                    index["collection_cursor"] = next_cursor
                    index["collection_complete"] = page.get("hasMore") is False
                    write_json(directory / "index.json", index)
                if not page.get("hasMore") or next_cursor == cursor:
                    break
                cursor = next_cursor
    except Exception as exc:
        logger.warning("session diagnostic collection unavailable: %s", type(exc).__name__)
