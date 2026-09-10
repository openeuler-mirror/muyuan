"""Captured inputs for candidate and session monitor projections."""

from dataclasses import dataclass
from typing import Any
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.domain.verifier import VerifierRecord, VerifierTask
from goal_plus.ports.state import CandidateRecord


@dataclass(frozen=True)
class MonitorCandidateInputs:
    candidate: CandidateRecord
    sessions: list[AgentSessionRecord]
    metric_direction: str
    verifier_enabled: bool
    verifier_records: list[VerifierRecord]
    verifier_tasks: list[VerifierTask]
    ledger_projection: str
    results_tsv: dict[str, Any]


@dataclass(frozen=True)
class MonitorSessionInputs:
    session: AgentSessionRecord
    candidate: CandidateRecord | None
    observation: dict[str, Any]
    time_advisory: dict[str, Any] | None
    logs: dict[str, dict[str, Any]]
    latest_output_mtime: float | None
    now: float
    stale_after_seconds: int
