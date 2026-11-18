"""optimizer 运行的持久 JSONL trace 与证据记录。"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass(frozen=True)
class TraceEvent:
    event: str
    candidate_id: str | None = None
    run_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "trace",
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event": self.event,
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "payload": _jsonable(self.payload),
        }


TraceRecord = TraceEvent


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    candidate_id: str | None = None
    run_id: str | None = None
    status: str = "observed"
    metrics: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)
    evidence_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "evidence",
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "status": self.status,
            "metrics": _jsonable(self.metrics),
            "artifacts": _jsonable(self.artifacts),
            "details": _jsonable(self.details),
        }


Trace = TraceEvent
Evidence = EvidenceRecord


class JsonlRecorder:
    """trace 与 evidence 流共用的小型只追加记录器。"""

    def __init__(self, path: os.PathLike[str] | str):
        self.path = Path(path)

    def append(self, record: Any) -> dict[str, Any]:
        return append_jsonl(self.path, record)

    def __iter__(self):
        return iter_jsonl(self.path)


class TraceWriter(JsonlRecorder):
    def append_event(self, event: str, **kwargs: Any) -> dict[str, Any]:
        return write_trace(self.path, event, **kwargs)


class EvidenceWriter(JsonlRecorder):
    def append_evidence(self, kind: str, **kwargs: Any) -> dict[str, Any]:
        return write_evidence(self.path, kind, **kwargs)


def append_jsonl(path: os.PathLike[str] | str, record: Any) -> dict[str, Any]:
    """追加一个 JSON 对象并 fsync，返回序列化行。"""
    row = record.to_dict() if hasattr(record, "to_dict") else _jsonable(record)
    if not isinstance(row, Mapping):
        raise TypeError("JSONL records must serialize to an object")
    serialized = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return dict(row)


def iter_jsonl(path: os.PathLike[str] | str) -> Iterable[dict[str, Any]]:
    destination = Path(path)
    if not destination.exists():
        return
    with destination.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {destination}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row at {destination}:{line_number} is not an object")
            yield value


def write_trace(
    path: os.PathLike[str] | str,
    event: str,
    *,
    candidate_id: str | None = None,
    run_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return append_jsonl(path, TraceEvent(event, candidate_id, run_id, payload or {}))


def write_evidence(
    path: os.PathLike[str] | str,
    kind: str,
    *,
    candidate_id: str | None = None,
    run_id: str | None = None,
    status: str = "observed",
    metrics: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, str] | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return append_jsonl(
        path,
        EvidenceRecord(
            kind=kind,
            candidate_id=candidate_id,
            run_id=run_id,
            status=status,
            metrics=metrics or {},
            artifacts=artifacts or {},
            details=details or {},
        ),
    )


__all__ = [
    "Evidence",
    "EvidenceRecord",
    "EvidenceWriter",
    "JsonlRecorder",
    "Trace",
    "TraceEvent",
    "TraceRecord",
    "TraceWriter",
    "append_jsonl",
    "iter_jsonl",
    "write_evidence",
    "write_trace",
]
