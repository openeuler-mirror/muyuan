"""Existing host-local scope documents and locks, without lifecycle decisions."""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_json,
)
from goal_plus.ports.clock import Clock
from goal_plus.ports.scope import ScopeHost, ScopeRecord, validate_scope_id
from goal_plus.domain.adaptive.models import AllocationDecision
from goal_plus.domain.search import AgentSessionRecord


class FileScopeState:
    def __init__(self, root_dir: Path | str, clock: Clock) -> None:
        self.directory = Path(root_dir).expanduser().resolve() / "host-scopes"
        self.clock = clock

    def _directory(self, scope_id: str) -> Path:
        return self.directory / validate_scope_id(scope_id)

    def lock(self, scope_id: str):
        return exclusive_file_lock(self._directory(scope_id) / "scope.lock")

    def owner_lock(self, agent_harness: ScopeHost, owner_session_id: str):
        owner_key = hashlib.sha256(f"{agent_harness}\0{owner_session_id}".encode()).hexdigest()[
            :20
        ]
        return exclusive_file_lock(self.directory / f"owner_{owner_key}.lock")

    def cleanup_lock(self, scope_id: str):
        return exclusive_file_lock(self._directory(scope_id) / "cleanup.lock")

    def exists(self, scope_id: str) -> bool:
        return (self._directory(scope_id) / "scope.json").exists()

    def read(self, scope_id: str) -> ScopeRecord:
        path = self._directory(scope_id) / "scope.json"
        if not path.exists():
            raise FileNotFoundError(f"unknown execution scope: {scope_id}")
        payload = load_json(path)
        if not isinstance(payload, dict):
            raise TypeError(f"invalid execution scope: {scope_id}")
        return cast(ScopeRecord, payload)

    def write(self, scope: ScopeRecord) -> None:
        scope["updated_at"] = self.clock.timestamp()
        write_json(self._directory(scope["scope_id"]) / "scope.json", scope)

    def records(self) -> Iterator[ScopeRecord]:
        for path in self.directory.glob("scope_*/scope.json"):
            try:
                scope = load_json(path)
            except (OSError, ValueError):
                continue
            if isinstance(scope, dict):
                yield cast(ScopeRecord, scope)


class FileAllocationCancellationProof:
    def __init__(self, scopes: FileScopeState) -> None:
        self.scopes = scopes

    def require_accepted(
        self, decision: AllocationDecision, sessions: list[AgentSessionRecord],
    ) -> None:
        receipts = [receipt for scope in self.scopes.records()
                    for receipt in scope.get("allocation_cancellations", [])]
        for session in sessions:
            if not session.run_requests and (
                not session.session_handle.external_id
                or session.session_handle.external_id == session.agent_session_id
            ):
                continue
            call_id = session.run_requests[-1].call_id if session.run_requests else None
            if not any(
                receipt.get("status") == "accepted"
                and receipt.get("run_id") == decision.run_id
                and receipt.get("decision_id") == decision.decision_id
                and receipt.get("candidate_id") == session.candidate_id
                and receipt.get("agent_session_id") == session.agent_session_id
                and receipt.get("execution_generation") == session.execution_generation
                and receipt.get("native_session_id") == session.session_handle.external_id
                and receipt.get("call_id") == call_id
                for receipt in receipts
            ):
                raise RuntimeError(
                    f"AllocationCancellationRequired: {session.agent_session_id} needs an exact accepted receipt"
                )
