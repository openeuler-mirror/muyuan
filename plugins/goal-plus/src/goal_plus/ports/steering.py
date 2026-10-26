"""Narrow steering storage, diagnostic reads, admission and host capabilities."""

from contextlib import AbstractContextManager
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from goal_plus.domain.adaptive.models import SteeringAttempt, SteeringTarget
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.adaptive.search_diagnostic_guidance import SearchDiagnosticInput, DiagnosticEvaluation
from goal_plus.ports.state import CandidateRecord, RunRecord


class SteeringState(Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def lock_dispatch(self) -> AbstractContextManager[None]: ...
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def read_attempt(self, run_id: str, attempt_key: str) -> SteeringAttempt | None: ...
    def consume_attempt(self, attempt: SteeringAttempt) -> bool:
        """Called under run.lock; never overwrite an existing opportunity."""
        ...
    def list_attempts(self, run_id: str, candidate_id: str) -> tuple[SteeringAttempt, ...]: ...
    def observe_attempt(self, run_id: str, attempt_key: str,
                        observation: Literal["returned", "unknown", "not_sent"]) -> None: ...


class SteeringAuthorization(Protocol):
    def require_search_execution(self, run_id: str) -> object: ...


class SteeringDiagnosticState(SteeringState, Protocol):
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_diagnostic(self, run_id: str, key: str) -> tuple[SearchDiagnosticInput, DiagnosticEvaluation] | None: ...
    def write_diagnostic(self, run_id: str, key: str, input: SearchDiagnosticInput,
                         result: DiagnosticEvaluation, *, mode: str) -> None: ...
    def observe_diagnostic(self, run_id: str, key: str,
                           observation: "SteeringDiagnosticObservation") -> None: ...


class SteeringQueries(Protocol):
    def require_native_execution(self, agent_session_id: str, *, now: float,
                                 native_session_id: str | None = None) -> AgentSessionRecord: ...


class SteeringHost(Protocol):
    def active_call(self, session: AgentSessionRecord) -> SteeringTarget | None:
        """Observe an existing controller only; never start or wake one."""
        ...
    def message(self, target: SteeringTarget, text: str, *, timeout: float,
                before_send: Callable[[], None]) -> None:
        """After host admission, invoke the Executor's guard before controller dispatch.

        A guard exception must propagate without sending. No run lock crosses message I/O.
        """
        ...


@dataclass(frozen=True)
class SteeringResult:
    status: Literal["skipped", "attempted", "error"]
    reason_code: str
    attempt_key: str | None = None
    error_type: str | None = None
    target: SteeringTarget | None = None


class SteeringDiagnosticObservation(BaseModel):
    """First observed admission/execution outcome, not a pending delivery intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    result: SteeringResult
    observed_at: str
