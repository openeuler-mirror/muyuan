"""Atomic append-only file storage for candidate execution traces."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from goal_plus.domain.candidate_trace import (
    CandidateTraceEvent,
    CandidateTraceIdentity,
    CandidateTraceKind,
    CandidateTraceSnapshot,
)
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.ports.candidate_trace import CandidateTraceReadResult


DEFAULT_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class CandidateTraceSnapshotTooLarge(ValueError):
    pass


class FileCandidateTraceStore:
    def __init__(
        self,
        root_dir: Path | str,
        *,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.max_snapshot_bytes = max_snapshot_bytes

    @staticmethod
    def new_operation_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        return (
            self.root_dir
            / "runs"
            / run_id
            / "candidate-traces"
            / candidate_id
        )

    def write_snapshot(
        self, identity: CandidateTraceIdentity, content: Any
    ) -> CandidateTraceSnapshot:
        serialized = canonical_json(content)
        size = len(serialized.encode("utf-8"))
        if size > self.max_snapshot_bytes:
            raise CandidateTraceSnapshotTooLarge(
                f"candidate trace snapshot is {size} bytes; maximum is "
                f"{self.max_snapshot_bytes} bytes"
            )
        snapshot_id = sha256_text(serialized)
        snapshot = CandidateTraceSnapshot(snapshot_id=snapshot_id, content=content)
        path = self.candidate_dir(identity.run_id, identity.candidate_id) / "snapshots" / f"{snapshot_id}.json"
        if path.exists():
            existing = CandidateTraceSnapshot.model_validate(load_json(path))
            if (
                existing.snapshot_id != snapshot_id
                or canonical_json(existing.content) != serialized
            ):
                raise RuntimeError("candidate trace snapshot hash collision or corruption")
            return existing
        write_json(path, snapshot.model_dump(mode="json"))
        persisted = CandidateTraceSnapshot.model_validate(load_json(path))
        if (
            persisted.snapshot_id != snapshot_id
            or sha256_text(canonical_json(persisted.content)) != snapshot_id
        ):
            raise RuntimeError("candidate trace snapshot verification failed")
        return persisted

    def append_event(
        self,
        identity: CandidateTraceIdentity,
        *,
        occurred_at: str,
        kind: CandidateTraceKind,
        operation_id: str,
        host_call_id: str | None,
        payload: dict[str, Any],
        deduplicate: bool,
    ) -> CandidateTraceEvent:
        directory = self.candidate_dir(identity.run_id, identity.candidate_id)
        with exclusive_file_lock(directory / "trace.lock"):
            events = self.read_events(identity.run_id, identity.candidate_id)
            if deduplicate:
                matches = [
                    event
                    for event in events
                    if event.kind == kind and event.operation_id == operation_id
                ]
                if matches:
                    if len(matches) != 1 or matches[0].payload != payload:
                        raise RuntimeError("conflicting candidate trace event replay")
                    return matches[0]
            sequence = max((event.sequence for event in events), default=0) + 1
            event = CandidateTraceEvent(
                event_id=f"trace_{uuid.uuid4().hex}",
                sequence=sequence,
                occurred_at=occurred_at,
                kind=kind,
                operation_id=operation_id,
                host_call_id=host_call_id,
                payload=payload,
                **identity.model_dump(mode="python"),
            )
            write_json(
                directory / "events" / f"{sequence:08d}-{event.event_id}.json",
                event.model_dump(mode="json", exclude_none=True),
            )
            return event

    def read_events(self, run_id: str, candidate_id: str) -> list[CandidateTraceEvent]:
        directory = self.candidate_dir(run_id, candidate_id) / "events"
        if not directory.is_dir():
            return []
        return [
            CandidateTraceEvent.model_validate(load_json(path))
            for path in sorted(directory.glob("*.json"))
        ]

    def read_snapshot(
        self, run_id: str, candidate_id: str, snapshot_id: str
    ) -> CandidateTraceSnapshot:
        snapshot = CandidateTraceSnapshot.model_validate(
            load_json(
                self.candidate_dir(run_id, candidate_id)
                / "snapshots"
                / f"{snapshot_id}.json"
            )
        )
        if (
            snapshot.snapshot_id != snapshot_id
            or sha256_text(canonical_json(snapshot.content)) != snapshot_id
        ):
            raise ValueError("candidate trace snapshot hash mismatch")
        return snapshot

    def audit_events(
        self, run_id: str, candidate_id: str
    ) -> CandidateTraceReadResult:
        directory = self.candidate_dir(run_id, candidate_id) / "events"
        if not directory.is_dir():
            return CandidateTraceReadResult()
        paths = sorted(directory.glob("*.json"))
        events: list[CandidateTraceEvent] = []
        warnings: list[dict[str, Any]] = []
        for path in paths:
            relative_path = str(path.relative_to(self.root_dir))
            try:
                raw = load_json(path)
            except (OSError, ValueError, TypeError) as exc:
                warnings.append(
                    {
                        "code": "event_unreadable",
                        "source": relative_path,
                        "error_category": type(exc).__name__,
                    }
                )
                continue
            if not isinstance(raw, dict):
                warnings.append(
                    {"code": "event_invalid", "source": relative_path}
                )
                continue
            if raw.get("schema_version") != 1:
                warnings.append(
                    {
                        "code": "unknown_event_schema",
                        "source": relative_path,
                        "schema_version": raw.get("schema_version"),
                    }
                )
                continue
            try:
                event = CandidateTraceEvent.model_validate(raw)
            except (ValueError, TypeError) as exc:
                warnings.append(
                    {
                        "code": "event_invalid",
                        "source": relative_path,
                        "error_category": type(exc).__name__,
                    }
                )
                continue
            if (event.run_id, event.candidate_id) != (run_id, candidate_id):
                warnings.append(
                    {
                        "code": "event_identity_mismatch",
                        "source": relative_path,
                        "event_id": event.event_id,
                    }
                )
                continue
            filename_sequence = path.name.split("-", 1)[0]
            if filename_sequence.isdigit() and int(filename_sequence) != event.sequence:
                warnings.append(
                    {
                        "code": "event_filename_sequence_mismatch",
                        "source": relative_path,
                        "event_id": event.event_id,
                    }
                )
            events.append(event)
        events.sort(key=lambda item: (item.sequence, item.event_id))
        sequences = [event.sequence for event in events]
        duplicates = sorted(
            sequence for sequence in set(sequences) if sequences.count(sequence) > 1
        )
        if duplicates:
            warnings.append(
                {"code": "event_sequence_conflict", "sequences": duplicates}
            )
        if sequences:
            missing = sorted(set(range(1, max(sequences) + 1)) - set(sequences))
            if missing:
                warnings.append(
                    {"code": "event_sequence_gap", "sequences": missing}
                )
        return CandidateTraceReadResult(
            events=events,
            warnings=warnings,
            raw_event_count=len(paths),
        )
