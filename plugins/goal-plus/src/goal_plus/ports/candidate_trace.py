"""Narrow persistence contracts for candidate execution traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from goal_plus.domain.candidate_trace import (
    CandidateTraceEvent,
    CandidateTraceIdentity,
    CandidateTraceKind,
    CandidateTraceSnapshot,
)


class CandidateTraceStore(Protocol):
    def new_operation_id(self, prefix: str) -> str: ...

    def write_snapshot(
        self, identity: CandidateTraceIdentity, content: Any
    ) -> CandidateTraceSnapshot: ...

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
    ) -> CandidateTraceEvent: ...

    def read_events(self, run_id: str, candidate_id: str) -> list[CandidateTraceEvent]: ...
    def read_snapshot(
        self, run_id: str, candidate_id: str, snapshot_id: str
    ) -> CandidateTraceSnapshot: ...


@dataclass(frozen=True)
class CandidateTraceReadResult:
    events: list[CandidateTraceEvent] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    raw_event_count: int = 0


class CandidateTraceArchive(CandidateTraceStore, Protocol):
    def audit_events(
        self, run_id: str, candidate_id: str
    ) -> CandidateTraceReadResult: ...
