"""Narrow host-local observations, not Goal or Search execution authority."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ContextManager, Literal, Protocol, TypedDict

from pydantic import Field

from goal_plus.domain.base import SearchModel
from goal_plus.domain.goal_authorization import GOAL_ID_RE


ScopeHost = Literal["codex", "pi"]


@dataclass(frozen=True)
class ScopeAttachment:
    """Typed operational association, not Goal/Search execution authority."""

    kind: Literal["goal", "search_run"]
    identity: str

    @classmethod
    def from_id(cls, identity: str) -> "ScopeAttachment":
        # Preserve the existing Goal namespace; no storage format or authorization change.
        return cls("goal" if GOAL_ID_RE.fullmatch(identity) else "search_run", identity)

    def require_search_run(self) -> str:
        if self.kind != "search_run":
            raise ValueError("execution operation requires a Search run attachment")
        return self.identity


class ScopeAgentRecord(TypedDict, total=False):
    agent_id: str
    label: str
    native_agent_id: str
    parent_session_id: str
    run_id: str | None
    candidate_id: str
    agent_session_id: str
    execution_generation: int
    status: str
    registered_at: str | None
    updated_at: str
    finished_at: str
    spawn_tool_use_id: str
    spawn_pending: bool
    wake_tool_use_id: str
    wake_generation: int
    wake_pending: bool
    interrupt_observation: list[str | int]


class ScopeProcessRecord(TypedDict, total=False):
    token: str
    pid: int
    process_group_id: int | None
    start_token: str
    witness: dict[str, Any]
    label: str
    run_id: str | None
    kind: Literal["candidate", "internal"]
    status: str
    registered_at: str
    finished_at: str | None


class ScopeRecord(TypedDict, total=False):
    """Existing operational document; optional keys remain absent when not recorded."""

    schema_version: int
    scope_id: str
    host: ScopeHost
    owner_session_id: str
    owner_pid: int
    owner_start_token: str
    generation: int
    activation_id: str | None
    state: Literal["open", "closing", "cleanup_failed", "closed"]
    run_ids: list[str]
    fenced_run_ids: list[str]
    guardian_pid: int | None
    guardian_start_token: str
    native_agent_config: dict[str, str]
    created_at: str
    created_at_ns: int
    updated_at: str
    closed_at: str | None
    close_reason: str
    close_requested_at: str
    codex_agents: list[ScopeAgentRecord]
    codex_observation: dict[str, Any]
    processes: list[ScopeProcessRecord]
    cleanup: dict[str, Any]


class ScopeState(Protocol):
    def lock(self, scope_id: str) -> ContextManager[None]: ...
    def owner_lock(self, host: ScopeHost, owner_session_id: str) -> ContextManager[None]: ...
    def cleanup_lock(self, scope_id: str) -> ContextManager[None]: ...
    def exists(self, scope_id: str) -> bool: ...
    def read(self, scope_id: str) -> ScopeRecord: ...
    def write(self, scope: ScopeRecord) -> None: ...
    def records(self) -> Iterator[ScopeRecord]: ...


class ScopeCleanupServices(Protocol):
    """Host-owned cleanup and read-only quiescence checks, never task transitions."""

    def close_pools(
        self, *, attachment: ScopeAttachment, scope_id: str, timeout_seconds: float,
    ) -> tuple[list[dict[str, Any]], list[str]]: ...

    def close_drainers(self, *, run_id: str, timeout_seconds: float) -> list[str]: ...

    def codex_run_is_quiescent(self, run_id: str) -> bool: ...


def validate_scope_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(char not in allowed for char in value):
        raise ValueError(f"invalid scope_id: {value!r}")
    return value


class ScopeGuardianObservation(SearchModel):
    state: Literal["open", "closing", "cleanup_failed", "closed"]
    owner_pid: int | None = Field(default=None, strict=True, gt=0)
    owner_start_token: str | None = None
    close_reason: str | None = None
