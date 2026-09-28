"""Transcript collection from local native session histories.

This module replays native Codex/Pi histories and the unified TT session archive.
Raw entries are retained locally for the lifetime needed to diagnose a host
failure and are exported under ``host-logs/``. Monitor projections expose only
bounded metadata and archive availability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterator

from goal_plus.infrastructure.clock import utc_timestamp
from goal_plus.infrastructure.api_debug import api_debug_enabled
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json, write_text
from goal_plus.projections.agent_transcript import (
    AgentTranscriptSession,
    TranscriptEvent,
    render_transcript_html,
    render_transcript_jsonl,
)

logger = logging.getLogger(__name__)


# A stop hook re-renders on every turn end; bound the accepted rollout size so
# the debug opt-in cannot turn into unbounded hook latency on huge sessions.
MAX_DEBUG_SOURCE_BYTES = 32 * 1024 * 1024
DEFAULT_OUTPUT_DIRNAME = "agent-transcript"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class _Parsed:
    session_id: str | None = None
    events: list[TranscriptEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""    # last observed "model (provider)"
    thinking: str = "" # last observed thinking level


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _tool_contracts_summary(data: dict[str, Any]) -> str:
    """Contract snapshots embed serialized schemas; report shape instead of dumping them."""
    summary: dict[str, Any] = {key: data.get(key) for key in ("sha256",) if key in data}
    try:
        snapshot = json.loads(data["serialized"]) if isinstance(data.get("serialized"), str) else None
        if isinstance(snapshot, dict):
            for key in ("registered", "request"):
                tools = snapshot.get(key)
                if isinstance(tools, list):
                    summary[f"{key}_tools"] = len(tools)
    except (json.JSONDecodeError, TypeError):
        summary["serialized"] = "unreadable"
    return _compact(summary)


def _iter_json_lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _text_parts(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        part.get("text") for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _pi_message_events(entry: dict[str, Any]) -> list[TranscriptEvent]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    at = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else ""
    role = message.get("role")
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
    turn_meta: dict[str, Any] = {"stop_reason": message.get("stopReason")}
    if usage is not None:
        turn_meta["usage"] = usage
    for key in ("model", "provider"):
        if isinstance(message.get(key), str):
            turn_meta[key] = message[key]
    events: list[TranscriptEvent] = []
    if isinstance(message.get("errorMessage"), str) and message["errorMessage"]:
        events.append(TranscriptEvent(at=at, kind="error", label="provider error",
                                       content=message["errorMessage"], meta=dict(turn_meta)))
    if role == "user":
        events.append(TranscriptEvent(at=at, kind="user", label="prompt",
                                       content=_text_parts(message.get("content"))))
    elif role == "toolResult":
        meta: dict[str, Any] = {"is_error": message.get("isError") is True}
        events.append(TranscriptEvent(
            at=at, kind="tool_result", label=str(message.get("toolName") or "tool"),
            content=_text_parts(message.get("content")), meta=meta))
    elif role == "assistant":
        content = message.get("content")

        def carries_usage() -> dict[str, Any]:
            # Usage describes the whole provider call; attaching it to every
            # part would double-count when scanning the transcript.
            meta = dict(turn_meta)
            turn_meta.pop("usage", None)
            return meta

        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "thinking" and isinstance(part.get("thinking"), str):
                events.append(TranscriptEvent(at=at, kind="thinking", label="thinking",
                                               content=part["thinking"]))
            elif part_type == "toolCall" and isinstance(part.get("name"), str):
                events.append(TranscriptEvent(
                    at=at, kind="tool_call", label=part["name"],
                    content=_compact(part.get("arguments")), meta=carries_usage()))
            elif part_type == "text" and isinstance(part.get("text"), str):
                events.append(TranscriptEvent(at=at, kind="assistant", label="reply",
                                               content=part["text"], meta=carries_usage()))
        if not any(event.kind in ("tool_call", "assistant") for event in events):
            events.append(TranscriptEvent(at=at, kind="assistant", label="reply", meta=carries_usage()))
    elif isinstance(role, str):
        events.append(TranscriptEvent(at=at, kind="user", label=role,
                                      content=_text_parts(message.get("content"))))
    return events


def _pi_entry_events(entry: dict[str, Any]) -> list[TranscriptEvent]:
    entry_type = entry.get("type")
    at = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else ""
    if entry_type == "session":
        return [TranscriptEvent(at=at, kind="session", label="pi session",
                                content=_compact({key: entry.get(key) for key in ("cwd", "version") if key in entry}))]
    if entry_type == "message":
        return _pi_message_events(entry)
    if entry_type == "custom_message":
        content = entry.get("content")
        return [TranscriptEvent(at=at, kind="injection",
                                label=str(entry.get("customType") or "custom_message"),
                                content=content if isinstance(content, str) else _compact(content))]
    if entry_type == "compaction":
        return [TranscriptEvent(at=at, kind="compaction", label="compaction")]
    if entry_type == "model_change":
        return [TranscriptEvent(at=at, kind="meta",
                                label=f"model {entry.get('modelId')} ({entry.get('provider')})")]
    if entry_type == "thinking_level_change":
        return [TranscriptEvent(at=at, kind="meta", label=f"thinking {entry.get('thinkingLevel')}")]
    if entry_type == "custom":
        custom_type = str(entry.get("customType") or "custom")
        data = entry.get("data")
        if custom_type == "goal-plus-injection-observation":
            kind = data.get("kind") if isinstance(data, dict) else None
            return [TranscriptEvent(at=at, kind="meta", label=f"injection-ledger {kind}",
                                    content=_compact(data))]
        if custom_type == "goal-plus-provider-request":
            return [TranscriptEvent(at=at, kind="context", label="provider request",
                                    content=_compact(data))]
        if custom_type == "goal-plus-tool-contracts" and isinstance(data, dict):
            return [TranscriptEvent(at=at, kind="meta", label="tool contracts",
                                    content=_tool_contracts_summary(data))]
        if custom_type == "goal-plus-runtime-start" and isinstance(data, dict):
            return [TranscriptEvent(at=at, kind="meta", label="runtime start",
                                    content=_compact({key: data.get(key) for key in
                                                      ("agent", "agent_harness", "mode", "project_trusted", "cwd")
                                                      if key in data}))]
        return [TranscriptEvent(at=at, kind="meta", label=custom_type, content=_compact(data))]
    if isinstance(entry_type, str):
        return [TranscriptEvent(at=at, kind="meta", label=f"type {entry_type}")]
    return []


def _pi_branch(records: list[dict[str, Any]], leaf_id: str | None, warnings: list[str]) -> list[dict[str, Any]]:
    """Select the leaf-to-root branch, mirroring the session audit's tree walk."""
    entries: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "session" and isinstance(record.get("id"), str):
            entries[record["id"]] = record
    leaf = leaf_id or next(
        (record["id"] for record in reversed(records)
         if record.get("type") != "session" and isinstance(record.get("id"), str)), None)
    branch: list[dict[str, Any]] = []
    seen: set[str] = set()
    while leaf is not None:
        if leaf in seen or leaf not in entries:
            warnings.append("invalid_branch")
            break
        seen.add(leaf)
        branch.append(entries[leaf])
        parent = entries[leaf].get("parentId")
        leaf = parent if isinstance(parent, str) else None
    branch.reverse()
    return branch


def read_pi_transcript(path: Path, *, leaf_id: str | None = None) -> _Parsed:
    parsed = _Parsed()
    records = list(_iter_json_lines(path))
    parsed.session_id = next(
        (record.get("id") for record in records
         if record.get("type") == "session" and isinstance(record.get("id"), str)), None)
    for record in records:
        if record.get("type") == "session":
            # The header entry sits outside the id chain; surface it before the branch.
            parsed.events.extend(_pi_entry_events(record))
            break
    for entry in _pi_branch(records, leaf_id, parsed.warnings):
        parsed.events.extend(_pi_entry_events(entry))
        if entry.get("type") == "model_change":
            parsed.model = f"{entry.get('modelId')} ({entry.get('provider')})"
        elif entry.get("type") == "thinking_level_change":
            parsed.thinking = str(entry.get("thinkingLevel") or "")
        elif entry.get("type") == "message" and not parsed.model:
            # model_change already carries the provider; messages are only a fallback.
            message = entry.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                provider = message.get("provider")
                parsed.model = (f"{message['model']} ({provider})"
                                if isinstance(provider, str) else message["model"])
    return parsed


def _codex_reasoning_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("summary", "content"):
        block = payload.get(key)
        if isinstance(block, list):
            parts.extend(text for text in (
                item.get("text") for item in block
                if isinstance(item, dict) and isinstance(item.get("text"), str)) if text)
    return "\n".join(parts)


def read_codex_transcript(path: Path) -> _Parsed:
    parsed = _Parsed()
    call_names: dict[str, str] = {}
    for event in _iter_json_lines(path):
        at = event.get("timestamp") if isinstance(event.get("timestamp"), str) else ""
        event_type = event.get("type")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "session_meta":
            parsed.session_id = payload.get("id") if isinstance(payload.get("id"), str) else parsed.session_id
            parsed.events.append(TranscriptEvent(
                at=at, kind="session", label="codex session",
                content=_compact({key: payload.get(key) for key in
                                  ("cwd", "originator", "cli_version", "source") if key in payload})))
        elif event_type == "turn_context":
            parsed.events.append(TranscriptEvent(
                at=at, kind="meta", label="turn context",
                content=_compact({key: payload.get(key) for key in
                                  ("model", "effort", "service_tier") if key in payload})))
            if isinstance(payload.get("model"), str):
                parsed.model = payload["model"]
            if isinstance(payload.get("effort"), str):
                parsed.thinking = payload["effort"]
        elif event_type == "compacted":
            replacement = payload.get("replacement_history")
            parsed.events.append(TranscriptEvent(
                at=at, kind="compaction", label="compaction",
                content=f"replacement_history entries: {len(replacement)}"
                if isinstance(replacement, list) else ""))
        elif event_type == "response_item":
            item_type = payload.get("type")
            if item_type == "message":
                role = payload.get("role")
                label = {"assistant": "reply", "user": "prompt"}.get(
                    role, str(role or "message"))
                parsed.events.append(TranscriptEvent(
                    at=at, kind="assistant" if role == "assistant" else "user", label=label,
                    content=_text_parts(payload.get("content"))))
            elif item_type in ("function_call", "custom_tool_call"):
                name = str(payload.get("name") or item_type)
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    call_names[call_id] = name
                parsed.events.append(TranscriptEvent(
                    at=at, kind="tool_call", label=name, content=_compact(payload.get("arguments"))))
            elif item_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                parsed.events.append(TranscriptEvent(
                    at=at, kind="tool_result",
                    label=call_names.get(call_id, str(call_id or "tool")) if isinstance(call_id, str) else "tool",
                    content=_compact(payload.get("output"))))
            elif item_type == "reasoning":
                content = _codex_reasoning_text(payload)
                if content:
                    parsed.events.append(TranscriptEvent(at=at, kind="thinking", label="reasoning",
                                                         content=content))
        elif event_type == "event_msg":
            message_type = payload.get("type")
            if message_type == "token_count":
                info = payload.get("info")
                usage = info.get("total_token_usage") if isinstance(info, dict) else None
                parsed.events.append(TranscriptEvent(
                    at=at, kind="context", label="token count", content=_compact(usage)))
            elif message_type == "task_complete":
                parsed.events.append(TranscriptEvent(
                    at=at, kind="meta", label="task complete",
                    content=_compact({key: payload.get(key) for key in ("duration_ms") if key in payload})))
            elif message_type == "turn_aborted":
                parsed.events.append(TranscriptEvent(
                    at=at, kind="error", label="turn aborted",
                    content=_compact({key: payload.get(key) for key in
                                      ("reason", "duration_ms") if key in payload})))
            elif message_type == "error":
                parsed.events.append(TranscriptEvent(
                    at=at, kind="error", label="error", content=_compact(payload.get("message"))))
    return parsed


def read_session_diagnostics_archive(directory: Path) -> _Parsed:
    """Replay unified TT Child session batches into the same pi-style event stream."""
    parsed = _Parsed()
    index = load_json(directory / "index.json")
    if index.get("source") != "thinkthread_messages":
        raise ValueError("not a thinkthread diagnostics archive")
    entries: dict[str, dict[str, Any]] = {}
    leaf: str | None = None
    for digest, metadata in sorted(index.get("batches", {}).items(), key=lambda item: (
        item[1].get("captured_at", ""), item[1].get("stream_id", ""), item[1].get("sequence", 0),
    )):
        try:
            batch = load_json(directory / f"{digest}.json")
        except (OSError, ValueError):
            parsed.warnings.append("missing_archive_batch")
            continue
        leaf = batch.get("leaf_id") if isinstance(batch.get("leaf_id"), str) else leaf
        for entry in batch.get("entries", []):
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                entries[entry["id"]] = entry
    parsed.session_id = index.get("session_id") if isinstance(index.get("session_id"), str) else None
    parsed.events.append(TranscriptEvent(kind="session", label="pi session (diagnostic archive)",
                                         content=_compact({"identity": index.get("identity"),
                                                           "batches": len(index.get("batches", {}))})))
    ordered = sorted(entries.values(), key=lambda entry: (
        entry.get("timestamp") or "", entry.get("id") or ""))
    for entry in _pi_branch(ordered, leaf, parsed.warnings):
        parsed.events.extend(_pi_entry_events(entry))
    if index.get("status") != "recorded":
        parsed.warnings.append(f"archive_status_{index.get('status')}")
    return parsed


def _tt_child_raw_history(runtime_root: Path, child_id: str) -> Path | None:
    """Locate a retained TT Child's raw Pi history before release reclaims it.

    The runtime root lives at <instance>/thinkthreads/<root>/session/goal-plus,
    and each child keeps its own session at
    <instance>/thinkthreads/<child>/session/active.jsonl. Once the child is
    released that file is gone and the retained session archive is the recovery
    source.
    """
    try:
        root = Path(runtime_root).resolve()
        session_dir, threads_dir = root.parents[0], root.parents[1]
        if root.name != "goal-plus" or session_dir.name != "session" or threads_dir.parent.name != "thinkthreads":
            return None
        candidate = threads_dir.parent / child_id / "session" / "active.jsonl"
        return candidate if candidate.is_file() else None
    except (IndexError, OSError):
        return None


def _tt_event_store_children(runtime_root: Path) -> dict[str, list[TranscriptEvent]]:
    """Recover released-child turn texts and controller prompts from the TT event store.

    The Root instance keeps events.sqlite3 forever, long after child release
    reclaimed the raw Pi histories: agent.pi turn.completed rows carry each
    child turn's final text, and platform.message rows carry the controller
    prompts verbatim. This is partial recovery — thinking and intermediate
    tool bodies remain archive-only once the child is released.
    """
    try:
        import sqlite3

        root = Path(runtime_root).resolve()
        if root.name != "goal-plus":
            return {}
        # goal-plus -> session -> <root-thread> -> thinkthreads -> <instance>
        store = root.parents[3] / "events.sqlite3"
        if not store.is_file():
            return {}
        root_id = root.parents[1].name
        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            children: dict[str, list[TranscriptEvent]] = {}
            for namespace, kind, at_ms, source_key, payload in connection.execute(
                "SELECT namespace, kind, recorded_at_unix_ms, source_key, payload FROM events"
            ):
                blob = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
                try:
                    at = datetime.fromtimestamp(at_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
                except (TypeError, ValueError, OSError):
                    at = ""
                if namespace == "agent.pi" and kind == "turn.completed" and isinstance(source_key, str):
                    child = source_key.split(":", 1)[0]
                    try:
                        data = json.loads(blob)
                    except ValueError:
                        data = {}
                    body = data.get("text") if isinstance(data, dict) else None
                    if isinstance(body, str) and body:
                        children.setdefault(child, []).append(TranscriptEvent(
                            at=at, kind="assistant", label="turn completed", content=body,
                            meta={"recovered_from": "tt-event-store"}))
                elif namespace == "platform.message" and kind == "accepted":
                    try:
                        data = json.loads(blob)
                    except ValueError:
                        continue
                    if (not isinstance(data, dict) or data.get("senderThinkthreadId") != root_id
                            or data.get("recipientThinkthreadId") in (None, root_id)):
                        continue
                    body = data.get("text")
                    # Protocol envelopes (RPC chunks) carry no review value; keep human prompts.
                    if isinstance(body, str) and body and not body.startswith("{"):
                        children.setdefault(str(data["recipientThinkthreadId"]), []).append(TranscriptEvent(
                            at=at, kind="injection", label="controller prompt", content=body,
                            meta={"recovered_from": "tt-event-store"}))
            return children
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("TT event store recovery unavailable: %s", type(exc).__name__)
        return {}


def _controller_journal_events(record: dict[str, Any]) -> list[TranscriptEvent]:
    """Controller facts for one TT Child: submitted prompts, steering and results."""
    events: list[TranscriptEvent] = []

    def add(kind: str, label: str, content: Any = "", **meta: Any) -> None:
        events.append(TranscriptEvent(kind=kind, label=label,
                                      content=content if isinstance(content, str) else _compact(content),
                                      meta=meta))

    for key in ("spawn_intent", "wake_intent"):
        if record.get(key) is not None:
            add("meta", key, record[key])
    request = record.get("call_request")
    if isinstance(request, dict):
        add("injection", "controller prompt",
           request.get("prompt") if isinstance(request.get("prompt"), str) else _compact(request),
           dispatch_index=record.get("call_dispatch_index"), timeout=request.get("timeout_seconds"))
    for key in ("invocation_result", "attempt_result", "annotation_host_error"):
        if record.get(key) is not None:
            add("meta", key, record[key])
    add("meta", "journal state", {"status": record.get("status"),
                                  "cleanup_state": record.get("cleanup_state")})
    return events


def _session_directory_name(session: AgentTranscriptSession) -> str:
    raw = session.session_id or Path(session.source_path).stem or "unknown"
    safe = _SAFE_NAME.sub("-", f"{session.role}-{raw}").strip("-")
    return safe[:96]


def _load_json_quietly(path: Path) -> dict[str, Any] | None:
    try:
        return load_json(path)
    except (OSError, ValueError, TypeError):
        return None


def collect_runtime_transcripts(root: Path) -> list[AgentTranscriptSession]:
    """Discover every locally readable session history under a runtime root.

    Run records are harvested first so candidate sessions keep their full
    run/candidate identity; the native session-diagnostics indexes then cover
    the sessions only the host knows about (typically the Pi Main).
    """
    root = Path(root).resolve()
    sessions: list[AgentTranscriptSession] = []
    seen_sources: set[str] = set()

    def add(session: AgentTranscriptSession) -> None:
        if session.source_path and session.source_path in seen_sources:
            return
        if session.source_path:
            seen_sources.add(session.source_path)
        sessions.append(session)

    runs = root / "runs"
    if runs.is_dir():
        for run_dir in sorted(path for path in runs.iterdir() if path.is_dir()):
            for record_path in sorted((run_dir / "agent_sessions").glob("*.json")):
                record = _load_json_quietly(record_path)
                if not isinstance(record, dict):
                    continue
                agent_session_id = record.get("agent_session_id") or record_path.stem
                handle = record.get("session_handle") or record.get("host_handle")
                handle = handle if isinstance(handle, dict) else {}
                metadata = handle.get("metadata") if isinstance(handle.get("metadata"), dict) else {}
                host = str(handle.get("agent_harness") or record.get("host") or "")
                harness = "codex" if "codex" in host else "pi" if "pi" in host else None
                source: Path | None = None
                if isinstance(metadata.get("session_file"), str):
                    candidate = Path(metadata["session_file"]).expanduser().resolve()
                    source = candidate if candidate.is_file() else None
                if source is None and harness == "pi":
                    prefix = root / "host-sessions" / "pi"
                    if prefix.is_dir():
                        matches = sorted(prefix.glob(f"*{agent_session_id}.jsonl"))
                        source = matches[-1].resolve() if matches else None
                if source is None and harness == "codex":
                    source = _discover_codex_source(record)
                if source is None or harness is None:
                    continue
                if harness == "pi":
                    parsed = read_pi_transcript(source)
                else:
                    parsed = read_codex_transcript(source)
                identity = {"run_id": run_dir.name, "agent_session_id": agent_session_id}
                if record.get("candidate_id"):
                    identity["candidate_id"] = record["candidate_id"]
                role = "final-checker" if str(agent_session_id).startswith("fc_") else "candidate"
                add(AgentTranscriptSession(
                    session_id=parsed.session_id or str(agent_session_id), harness=harness,
                    role=role, source_path=str(source), identity=identity,
                    events=parsed.events, warnings=parsed.warnings,
                    model=parsed.model, thinking=parsed.thinking))

    diagnostics = root / "host-logs" / "session-diagnostics"
    event_store_children = _tt_event_store_children(root)
    if diagnostics.is_dir():
        for directory in sorted(path for path in diagnostics.iterdir() if path.is_dir()):
            index = _load_json_quietly(directory / "index.json")
            if not index:
                continue
            if index.get("source") == "native" and isinstance(index.get("history"), str):
                history = Path(index["history"]).expanduser().resolve()
                if history.is_file():
                    harness = index.get("agent_harness", "pi")
                    parsed = read_codex_transcript(history) if harness == "codex" else read_pi_transcript(history)
                    add(AgentTranscriptSession(
                        session_id=index.get("session_id") or parsed.session_id or directory.name,
                        harness=harness, role=str(index.get("role") or "unknown"),
                        source_path=str(history), events=parsed.events, warnings=parsed.warnings,
                        model=parsed.model, thinking=parsed.thinking))
            elif index.get("source") == "thinkthread_messages":
                identity = index.get("identity") if isinstance(index.get("identity"), dict) else {}
                raw = _tt_child_raw_history(root, str(identity.get("thinkthread_id") or ""))
                if raw is not None:
                    parsed = read_pi_transcript(raw)
                    add(AgentTranscriptSession(
                        session_id=index.get("session_id") or parsed.session_id or directory.name,
                        harness="pi", role="tt-child", source_path=str(raw),
                        identity=identity, events=parsed.events, warnings=parsed.warnings,
                        model=parsed.model, thinking=parsed.thinking))
                    continue
                try:
                    parsed = read_session_diagnostics_archive(directory)
                except (OSError, ValueError) as exc:
                    logger.warning("transcript archive unavailable: %s", type(exc).__name__)
                    continue
                recovered = event_store_children.get(str(identity.get("thinkthread_id") or ""))
                archive_complete = index.get("status") == "recorded" and index.get("collection_complete") is True
                if recovered and not archive_complete:
                    # Released child: the TT event store still holds turn texts
                    # and controller prompts; merge them over the session archive.
                    parsed.events = sorted([*parsed.events, *recovered], key=lambda item: item.at or "")
                    parsed.warnings.append("partial: recovered turn texts from the TT event store")
                add(AgentTranscriptSession(
                    session_id=index.get("session_id") or parsed.session_id or directory.name,
                    harness="pi", role="tt-child", source_path=str(directory),
                    identity=identity, events=parsed.events, warnings=parsed.warnings,
                    model=parsed.model, thinking=parsed.thinking))

    scopes = root / "host-scopes"
    if scopes.is_dir():
        for journal in sorted(scopes.glob("*/thinkthread-sessions/*.json")):
            record = _load_json_quietly(journal)
            if not isinstance(record, dict):
                continue
            add(AgentTranscriptSession(
                session_id=f"controller:{record.get('agent_session_id') or journal.stem}",
                harness="thinkthread", role="controller-journal", source_path=str(journal),
                identity={key: record.get(key) for key in
                          ("run_id", "candidate_id", "agent_session_id", "thinkthread_id") if record.get(key)},
                events=_controller_journal_events(record),
                model=str(record.get("model") or "")))

    _attach_time_advisories(root, sessions)
    return sessions


def _discover_codex_source(record: dict[str, Any]) -> Path | None:
    try:
        from goal_plus.domain.agents import AgentSessionRecord
        from goal_plus.host_observability import discover_codex_session_file

        return discover_codex_session_file(AgentSessionRecord.model_validate(record))
    except Exception as exc:
        logger.debug("codex transcript discovery unavailable: %s", type(exc).__name__)
        return None


def _attach_time_advisories(root: Path, sessions: list[AgentTranscriptSession]) -> None:
    sent = root / "host-logs" / "codex-time-advisory" / "sent"
    if not sent.is_dir():
        return
    for payload_path in sorted(sent.glob("*.json")):
        payload = _load_json_quietly(payload_path)
        if not isinstance(payload, dict):
            continue
        advisory = payload.get("advisory")
        event = TranscriptEvent(
            at=payload.get("sent_at") if isinstance(payload.get("sent_at"), str) else "",
            kind="injection", label="codex-time-advisory",
            content=advisory if isinstance(advisory, str) else _compact(payload),
            meta={"trigger_tool": payload.get("trigger_tool")})
        owners = [session for session in sessions if session.harness == "codex" and payload_path.stem in (
            session.identity.get("agent_session_id"), session.session_id,
            session.identity.get("external_id"))]
        if owners:
            for owner in owners:
                owner.events.append(event)
                owner.events.sort(key=lambda item: item.at or "")
            continue
        sessions.append(AgentTranscriptSession(
            session_id=payload_path.stem, harness="codex", role="time-advisory",
            source_path=str(payload_path), events=[event]))


def write_transcript_files(
    directory: Path,
    sessions: list[AgentTranscriptSession],
    *,
    generated_at: str | None = None,
    html_max_chars: int = 20000,
    gap_seconds: float = 120.0,
    include_jsonl: bool = True,
    per_session: bool = True,
    combined_name: str = "index.html",
) -> dict[str, Any]:
    """Render and atomically write HTML (plus optional JSONL) transcript artifacts."""
    generated = generated_at or utc_timestamp()
    directory.mkdir(parents=True, exist_ok=True)
    write_text(directory / combined_name,
               render_transcript_html(sessions, generated_at=generated, max_chars=html_max_chars,
                                      gap_seconds=gap_seconds))
    if include_jsonl and sessions:
        write_text(directory / "events.jsonl", render_transcript_jsonl(sessions))
    written: list[dict[str, Any]] = []
    if per_session:
        for session in sessions:
            session_dir = directory / _session_directory_name(session)
            session_dir.mkdir(parents=True, exist_ok=True)
            write_text(session_dir / "transcript.html",
                        render_transcript_html([session], generated_at=generated, max_chars=html_max_chars,
                                               gap_seconds=gap_seconds))
            written.append({"name": _session_directory_name(session), "role": session.role,
                            "harness": session.harness, "session_id": session.session_id,
                            "events": len(session.events)})
    else:
        written = [{"name": combined_name, "role": "/".join(session.role for session in sessions),
                    "harness": sessions[0].harness if sessions else "", "events": len(sessions)}]
    return {"output": str(directory), "generated_at": generated, "sessions": written}


def export_transcripts(root: Path, **options: Any) -> dict[str, Any]:
    """Whole-root debug export into ``host-logs/agent-transcript`` by default."""
    root = Path(root)
    output = options.pop("output", None)
    directory = Path(output).expanduser() if output else root / "host-logs" / DEFAULT_OUTPUT_DIRNAME
    # Collect inside the same lock as publication: concurrent host callbacks must
    # not overwrite a newer whole-runtime snapshot with an older one.
    with exclusive_file_lock(directory / "export.lock"):
        return write_transcript_files(directory, collect_runtime_transcripts(root), **options)


def debug_export_transcripts(root: Path) -> None:
    """Best-effort whole-runtime refresh at host lifecycle/diagnostic boundaries."""
    if not api_debug_enabled():
        return
    try:
        export_transcripts(root)
    except Exception as exc:
        logger.warning("debug transcript export unavailable: %s", type(exc).__name__)


def debug_export_codex_transcript(
    search_root: Path,
    transcript_path: str | Path | None,
    *,
    session_id: str | None = None,
    role: str = "codex",
) -> None:
    """Best-effort opt-in transcript refresh from the Codex stop hook.

    Stops fire per turn end, so an unchanged rollout (same name, same mtime
    rule) must not rewrite identical exports.
    """
    if not api_debug_enabled() or not transcript_path:
        return
    try:
        path = Path(transcript_path).expanduser()
        if not path.is_file():
            return
        if path.stat().st_size > MAX_DEBUG_SOURCE_BYTES:
            logger.warning("debug transcript skipped, source exceeds limit: %s", path)
            return
        # The directory name stays stable across stops, so a prior export whose
        # files are newer than the rollout already reflects this exact content.
        name = _session_directory_name(AgentTranscriptSession(
            session_id=session_id or path.stem, harness="codex", role=role))
        existing = Path(search_root) / "host-logs" / DEFAULT_OUTPUT_DIRNAME / name / "transcript.html"
        identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        index = Path(search_root) / "host-logs/session-diagnostics" / f"codex-{identity}" / "index.json"
        combined = Path(search_root) / "host-logs" / DEFAULT_OUTPUT_DIRNAME / "index.html"
        if (index.is_file() and combined.is_file() and existing.is_file()
                and existing.stat().st_mtime >= path.stat().st_mtime):
            return
        write_json(index, {
            "version": 1, "source": "native", "agent_harness": "codex",
            "session_id": session_id or path.stem, "role": role, "history": str(path.resolve()),
        })
        debug_export_transcripts(Path(search_root))
    except Exception as exc:
        logger.warning("debug transcript export unavailable: %s", type(exc).__name__)


def debug_export_child_transcript(root: Path, record: dict[str, Any]) -> None:
    """Best-effort pre-release transcript capture for a TT Child in debug mode."""
    if not api_debug_enabled():
        return
    child_id = record.get("thinkthread_id")
    if not isinstance(child_id, str) or not re.fullmatch(r"tt-[A-Za-z0-9-]+", child_id):
        return
    try:
        directory = Path(root) / "host-logs" / "session-diagnostics" / child_id
        raw = _tt_child_raw_history(Path(root), child_id)
        parsed = (read_pi_transcript(raw) if raw is not None
                  else read_session_diagnostics_archive(directory) if (directory / "index.json").is_file()
                  else None)
        if parsed is None:
            return
        identity = {key: record.get(key) for key in
                    ("run_id", "candidate_id", "agent_session_id") if record.get(key)}
        journal = AgentTranscriptSession(
            session_id=f"controller:{record.get('agent_session_id') or child_id}",
            harness="thinkthread", role="controller-journal", source_path="thinkthread resource journal",
            identity=identity, events=_controller_journal_events(record),
            model=str(record.get("model") or ""))
        session = AgentTranscriptSession(
            session_id=record.get("agent_session_id") or parsed.session_id or child_id,
            harness="pi", role="tt-child", source_path=str(raw if raw is not None else directory),
            identity={**identity, "thinkthread_id": child_id},
            events=parsed.events, warnings=parsed.warnings,
            model=parsed.model, thinking=parsed.thinking)
        write_transcript_files(directory, [session, journal], per_session=False,
                               combined_name="transcript.html")
        debug_export_transcripts(root)
    except Exception as exc:
        logger.warning("debug child transcript export unavailable: %s", type(exc).__name__)
