"""Native session execution facts; no candidate admission or settlement."""

from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider


SessionOperation = Literal["wake", "wait", "message", "interrupt", "stop", "close"]


@dataclass(frozen=True)
class SessionControlTools:
    """Public control tools shared by all candidate session adapters."""

    run_tool: str = "goal_plus_session_run"
    wait_tool: str = "goal_plus_session_wait_any"
    message_tool: str = "goal_plus_session_message"
    interrupt_tool: str = "goal_plus_session_interrupt"
    stop_tool: str = "goal_plus_session_stop"
    close_tool: str = "goal_plus_session_close"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class NativeMainCaller:
    agent_harness: AgentHarnessKind
    session_id: str
    control_version: int


@dataclass(frozen=True)
class SessionToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


class SessionToolBridge(Protocol):
    @property
    def definitions(self) -> tuple[SessionToolDefinition, ...]: ...
    def call(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class SessionProcessInput:
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]


@dataclass(frozen=True)
class NativeSessionInput:
    agent_harness: AgentHarnessKind
    process: SessionProcessInput
    model: str | None = None
    reasoning_effort: str | None = None
    native_session_id: str | None = None
    instructions: str | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    runtime_provider: Literal["direct"] = "direct"
    metrics_baseline: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionInvocationObservation:
    started_at: str
    ended_at: str
    duration_seconds: float
    pi_metrics: dict[str, Any] | None = None
    metrics_baseline: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionInvocationResult:
    agent_harness: AgentHarnessKind = field(kw_only=True)
    runtime_provider: AgentRuntimeProvider = field(kw_only=True)
    native_session_id: str
    invocation_id: str
    state: Literal["completed", "interrupted", "failed"]
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    observation: SessionInvocationObservation | None = None


class NativeSession(Protocol):
    @property
    def native_session_id(self) -> str | None: ...
    def run(
        self, prompt: str, *, timeout: float, on_started: Callable[[], None] | None = None,
    ) -> SessionInvocationResult: ...
    def message(self, prompt: str, *, timeout: float) -> None: ...
    def interrupt(self, *, timeout: float) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class SessionCallRequest:
    call_id: str
    prompt: str
    timeout: float


@dataclass(frozen=True)
class SessionCallSnapshot:
    """Controller identity is available before the host acknowledges a native turn."""

    call_id: str
    native_session_id: str | None
    state: Literal["running", "completed", "interrupted", "failed"]
    result: SessionInvocationResult | None = None
    error: str | None = None


class SessionControl(Protocol):
    def wake(self, prompt: str, *, call_id: str, timeout: float) -> SessionCallSnapshot: ...
    def wait(self, call_id: str, *, timeout: float) -> SessionCallSnapshot: ...
    def message(self, call_id: str, prompt: str, *, timeout: float) -> None: ...
    def interrupt(self, call_id: str, *, timeout: float) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class SessionControlLookup(Protocol):
    """Trusted host checks ownership/admission for each operation before returning control."""

    def __call__(self, session_id: str, operation: SessionOperation) -> SessionControl: ...


@dataclass(frozen=True)
class SessionOpenResult:
    session_id: str
    native_session_id: str | None
    agent_harness: AgentHarnessKind
    call_id: str | None = None
    runtime_provider: AgentRuntimeProvider = "direct"


class SessionLauncher(Protocol):
    def apply_allocation_decision(self, run_id: str, decision_id: str) -> dict[str, Any]: ...
    def wake_internal_agents(self, run_id: str) -> None: ...
    def run_candidate(self, run_id: str, candidate_id: str, call_id: str, prompt: str,
                      timeout_seconds: float, new_session: bool = False) -> dict[str, Any]: ...
    def open_candidate(self, run_id: str, candidate_id: str) -> SessionOpenResult: ...
    def open(self, agent_session_id: str) -> SessionOpenResult: ...
    def shutdown(self) -> None: ...


class FinalCheckRunner(Protocol):
    def check(self, goal_plus_id: str) -> dict[str, Any]: ...
    def shutdown(self) -> None: ...


class WorkItemRunner(Protocol):
    def run(self, goal_plus_id: str, work_item_id: str, task: str, max_runtime_seconds: int) -> dict[str, Any]: ...
    def shutdown(self) -> None: ...
