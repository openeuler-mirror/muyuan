"""Conservative import of Codex rollout JSONL and exec --json MCP events.

Only explicit call/result pairs are evidence. Native exec summaries are not
lossless tool arguments; JavaScript/custom-tool bodies are never evaluated or
expanded into guessed primitive calls. No files outside the supplied inputs
are discovered, and no trajectory contents are sent over the network.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from ..models import ToolCall, Trace


def parse_json_text(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def tool_output(value: Any) -> Any:
    """Same MCP result representation in training and live execution."""
    value = parse_json_text(value)
    if isinstance(value, Mapping) and "content" in value:
        if value.get("structuredContent") is not None:
            return value["structuredContent"]
        value = value["content"]
    if (isinstance(value, list) and len(value) == 1
            and isinstance(value[0], Mapping) and value[0].get("type") == "text"):
        return parse_json_text(value[0].get("text", ""))
    return value


def failed_output(value: Any) -> bool:
    value = parse_json_text(value)
    if isinstance(value, Mapping):
        return (value.get("isError") is True or value.get("is_error") is True
                or value.get("error") not in (None, False, "")
                or value.get("exit_code") not in (None, 0)
                or value.get("status") in ("failed", "cancelled", "interrupted"))
    if isinstance(value, str):
        # Native exec_command output is sometimes a text envelope.
        codes = re.findall(r"(?im)^(?:Process exited with code|Exit code:)\s*(-?\d+)\s*$", value)
        return any(int(code) != 0 for code in codes)
    return False


def is_meta_tool(name: str) -> bool:
    leaf = name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    return (leaf.startswith(("mt_", "meta_tool_"))
            or leaf in {"bate_catalog", "bate_status"})


@dataclass
class ImportResult:
    traces: list[Trace] = field(default_factory=list)
    diagnostics: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {
            "format": "bate-codex-traces-v1",
            "traces": [trace.to_dict() for trace in self.traces],
            "diagnostics": dict(self.diagnostics),
        }


class _Segments:
    def __init__(self, source: str, session_id: str, result: ImportResult):
        self.source, self.session_id, self.result = source, session_id, result
        self.calls: list[ToolCall] = []
        self.pending: dict[str, ToolCall | None] = {}
        self.seen: set[str] = set()
        self.segment = 0
        self.batch = 0
        self.concurrent = False

    def flush(self) -> None:
        if self.calls:
            self.result.traces.append(Trace(
                trace_id=f"codex:{self.session_id}:{self.segment}",
                calls=tuple(self.calls),
                # Successful primitives do not prove that the user's task passed.
                success=None,
                metadata={"source": self.source, "agent": "codex",
                          "session_id": self.session_id,
                          "evidence": "completed-tool-segment"},
            ))
            self.result.diagnostics["imported_calls"] += len(self.calls)
        self.calls = []
        self.segment += 1

    def boundary(self, reason: str) -> None:
        self.flush()
        self.result.diagnostics[reason] += 1
        self.result.diagnostics["missing_results"] += len(self.pending)
        self.pending.clear()
        self.concurrent = False

    def start(self, item: Mapping) -> None:
        call_id, name = item.get("call_id"), item.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            self.boundary("malformed_calls")
            return
        if call_id in self.seen:
            self.boundary("duplicate_call_ids")
            return
        self.seen.add(call_id)
        if self.pending:
            self.flush()
            self.concurrent = True
            self.result.diagnostics["overlapping_calls"] += 1
        else:
            self.batch += 1
        arguments = ( {"input": item.get("input")}
                     if item.get("type") == "custom_tool_call"
                     else parse_json_text(item.get("arguments")) )
        if is_meta_tool(name) or not isinstance(arguments, Mapping):
            self.flush()
            self.pending[call_id] = None
            self.result.diagnostics["excluded_meta_calls" if is_meta_tool(name)
                                    else "invalid_arguments"] += 1
            return
        self.pending[call_id] = ToolCall(
            name=name, arguments=dict(arguments), call_id=call_id,
            # Conservative observed serial request rounds, not task turn IDs.
            turn_id=self.batch,
            metadata={"codex_item_type": item.get("type", "function_call"),
                      "turn_id_source": "observed_serial_tool_round"},
        )

    def finish(self, call_id: Any, output: Any, *, failed: bool = False) -> None:
        if call_id not in self.pending:
            self.boundary("orphan_results")
            return
        call = self.pending.pop(call_id)
        if self.concurrent or call is None or failed or failed_output(output):
            self.flush()
            self.result.diagnostics["discarded_calls"] += 1
            if failed or failed_output(output):
                self.result.diagnostics["failed_calls"] += 1
        else:
            self.calls.append(replace(call, result=tool_output(output)))
        if not self.pending:
            self.concurrent = False


def import_records(records: Iterable[Mapping], *, source_id: str) -> ImportResult:
    records = list(records)
    rollout = any(row.get("type") in {"response_item", "session_meta"} for row in records)
    result = ImportResult()
    session_id = source_id
    for row in records:
        if row.get("type") == "session_meta":
            session_id = str(row.get("payload", {}).get("id") or source_id)
            break
        if row.get("type") == "thread.started":
            session_id = str(row.get("thread_id") or source_id)
            break
    state = _Segments("codex-rollout" if rollout else "codex-exec-json", session_id, result)
    recognized = False
    for row in records:
        kind = row.get("type", "")
        if rollout:
            if kind in {"compacted", "session_meta"}:
                state.boundary(kind)
                continue
            if kind == "event_msg":
                payload = row.get("payload", {})
                if payload.get("type") in {"task_complete", "task_failed", "turn_aborted"}:
                    state.boundary(payload["type"])
                continue
            if kind != "response_item":
                continue
            item = row.get("payload", {})
            kind = item.get("type", "")
            if kind == "message" and item.get("role") == "user":
                state.boundary("user_messages")
            elif kind in {"function_call", "custom_tool_call"}:
                recognized = True
                state.start(item)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                state.finish(item.get("call_id"), item.get("output"), failed="output" not in item)
            elif kind.endswith("_call"):
                state.boundary("unsupported_calls")
        else:
            if kind in {"thread.started", "turn.started", "turn.completed", "turn.failed"}:
                state.boundary(kind)
                continue
            if kind not in {"item.started", "item.completed"}:
                continue
            item = row.get("item", {})
            item_kind = item.get("type")
            if item_kind == "mcp_tool_call":
                recognized = True
                call_id = item.get("id")
                if kind == "item.started" or call_id not in state.seen:
                    server, tool = item.get("server"), item.get("tool")
                    state.start({"call_id": call_id, "type": "function_call",
                                 "name": f"mcp__{server}__{tool}" if server and tool else None,
                                 "arguments": item.get("arguments")})
                if kind == "item.completed":
                    state.finish(call_id, item.get("result"),
                                 failed=item.get("status") != "completed"
                                 or item.get("error") is not None or item.get("result") is None)
            elif item_kind not in {"agent_message", "reasoning", "todo_list"}:
                # command_execution/file_change omit the original tool contract.
                state.boundary("lossy_or_unsupported_items")
    state.boundary("end_of_input")
    if not recognized:
        result.diagnostics["no_supported_calls"] += 1
    return result


def import_files(paths: Iterable[str | Path]) -> ImportResult:
    combined = ImportResult()
    seen: dict[str, dict] = {}
    for path in paths:
        path = Path(path)
        records = []
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("record must be an object")
                except ValueError as error:
                    raise ValueError(f"{path}:{number}: {error}") from error
                records.append(record)
        fallback_id = hashlib.sha256(path.resolve().as_posix().encode()).hexdigest()[:16]
        imported = import_records(records, source_id=fallback_id)
        combined.diagnostics.update(imported.diagnostics)
        for trace in imported.traces:
            payload = trace.to_dict()
            if trace.trace_id in seen:
                if seen[trace.trace_id] != payload:
                    raise ValueError(f"Conflicting copies of session segment {trace.trace_id}")
                combined.diagnostics["duplicate_segments_skipped"] += 1
                continue
            seen[trace.trace_id] = payload
            combined.traces.append(trace)
    combined.diagnostics["imported_calls"] = sum(len(t.calls) for t in combined.traces)
    return combined
