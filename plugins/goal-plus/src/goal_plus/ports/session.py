"""Native session execution facts; no candidate admission or settlement."""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceAttachment
from goal_plus.ports.sandbox import ProcessSandbox

SessionOperation = Literal["wake", "wait", "message", "interrupt", "stop", "close"]
SessionMode = Literal["fresh", "fork"]


@dataclass(frozen=True)
class ParentCheckpoint:
    """A completed, immutable parent boundary eligible for forking."""

    parent_native_session_id: str
    checkpoint_id: str
    checkpoint_kind: str
    frozen_spec_id: str
    source_artifact_ref: ArtifactRef | None = None
    runtime_provider: AgentRuntimeProvider = "direct"
    main_session_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.parent_native_session_id,
                self.checkpoint_id,
                self.checkpoint_kind,
                self.frozen_spec_id,
            )
        ):
            raise ValueError("parent checkpoint fields must be non-empty")

    @property
    def parent_session_id(self) -> str:
        return self.parent_native_session_id


@dataclass(frozen=True, init=False)
class ProviderDispatchRequest:
    """Host-owned inputs for creating one isolated native branch."""

    dispatch_group_id: str
    dispatch_target_index: int
    parent_checkpoint: ParentCheckpoint
    workspace: Path
    worker_identity: dict[str, Any]
    budget: dict[str, Any]
    tool_capabilities: tuple[str, ...] = ()

    def __init__(
        self,
        dispatch_group_id: str | None = None,
        dispatch_target_index: int | None = None,
        parent_checkpoint: ParentCheckpoint | None = None,
        workspace: Path | None = None,
        worker_identity: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        tool_capabilities: tuple[str, ...] = (),
    ) -> None:
        object.__setattr__(self, "dispatch_group_id", dispatch_group_id or "")
        object.__setattr__(self, "dispatch_target_index", dispatch_target_index or 0)
        object.__setattr__(self, "parent_checkpoint", parent_checkpoint)
        object.__setattr__(self, "workspace", workspace or Path())
        object.__setattr__(self, "worker_identity", worker_identity or {})
        object.__setattr__(self, "budget", budget or {})
        object.__setattr__(self, "tool_capabilities", tuple(tool_capabilities))
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.parent_checkpoint is None:
            raise ValueError("dispatch request requires a parent checkpoint")
        if not self.dispatch_group_id.strip() or self.dispatch_target_index < 1:
            raise ValueError("dispatch request requires a positive target identity")
        if not self.workspace.is_absolute():
            raise ValueError("dispatch workspace must be absolute")
        if not self.worker_identity:
            raise ValueError("dispatch request requires worker identity")
        if len(set(self.tool_capabilities)) != len(self.tool_capabilities):
            raise ValueError("fork tool capabilities must be unique")

    @property
    def fork_group_id(self) -> str:
        return self.dispatch_group_id

    @property
    def branch_index(self) -> int:
        return self.dispatch_target_index

    @property
    def checkpoint(self) -> ParentCheckpoint:
        return self.parent_checkpoint


@dataclass(frozen=True, init=False)
class ProviderDispatchReceipt:
    """Observable host receipt for one branch creation attempt."""

    provider_session_id: str | None
    parent_native_session_id: str
    workspace: Path
    launch_state: Literal["started", "failed", "cleaned"]
    failure: str | None = None
    started_at: str | None = None
    closed_at: str | None = None
    context_projection_digest: str | None = None
    native_session_file: str | None = None
    runtime_provider: AgentRuntimeProvider = "direct"
    checkpoint_id: str | None = None
    checkpoint_kind: str | None = None
    workspace_attachment: WorkspaceAttachment | None = None
    request_id: str | None = None
    provider_capabilities: tuple[str, ...] = ()
    model: dict[str, str] | None = None
    profile: str | None = None

    def __init__(
        self,
        provider_session_id: str | None = None,
        parent_native_session_id: str = "",
        workspace: Path | None = None,
        launch_state: Literal["started", "failed", "cleaned"] = "started",
        failure: str | None = None,
        started_at: str | None = None,
        closed_at: str | None = None,
        context_projection_digest: str | None = None,
        native_session_file: str | None = None,
        runtime_provider: AgentRuntimeProvider = "direct",
        checkpoint_id: str | None = None,
        checkpoint_kind: str | None = None,
        workspace_attachment: WorkspaceAttachment | None = None,
        request_id: str | None = None,
        provider_capabilities: tuple[str, ...] = (),
        model: dict[str, str] | None = None,
        profile: str | None = None,
    ) -> None:
        object.__setattr__(self, "provider_session_id", provider_session_id)
        object.__setattr__(self, "parent_native_session_id", parent_native_session_id)
        object.__setattr__(self, "workspace", workspace or Path())
        object.__setattr__(self, "launch_state", launch_state)
        object.__setattr__(self, "failure", failure)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "closed_at", closed_at)
        object.__setattr__(self, "context_projection_digest", context_projection_digest)
        object.__setattr__(self, "native_session_file", native_session_file)
        object.__setattr__(self, "runtime_provider", runtime_provider)
        object.__setattr__(self, "checkpoint_id", checkpoint_id)
        object.__setattr__(self, "checkpoint_kind", checkpoint_kind)
        object.__setattr__(self, "workspace_attachment", workspace_attachment)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "provider_capabilities", tuple(provider_capabilities))
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "profile", profile)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not self.parent_native_session_id:
            raise ValueError("dispatch receipt requires parent identity")
        if self.launch_state == "started" and not self.provider_session_id:
            raise ValueError("started dispatch receipt requires provider identity")
        if self.launch_state == "failed" and not self.failure:
            raise ValueError("failed fork receipt requires failure")
        if self.runtime_provider == "thinkthread" and self.launch_state == "started":
            if not self.checkpoint_id or not self.checkpoint_kind or not self.context_projection_digest:
                raise ValueError("ThinkThread fork receipt requires an exact context boundary")
            if self.workspace_attachment is None or self.workspace_attachment.provider != "thinkthread":
                raise ValueError("ThinkThread fork receipt requires a private Fs attachment")

    @property
    def branch_native_session_id(self) -> str | None:
        return self.provider_session_id

    @property
    def forked_from_native_session_id(self) -> str:
        return self.parent_native_session_id


class DispatchProvider(Protocol):
    def fork(self, request: ProviderDispatchRequest, *, timeout: float) -> ProviderDispatchReceipt: ...


class ProviderDispatchAdapter(Protocol):
    """Provider-specific complete boundary selection and isolated derivation."""

    def prepare_checkpoint(
        self, run_id: str, caller_id: str, checkpoint_id: str = "latest_completed",
    ) -> ParentCheckpoint: ...

    def start_group(
        self, requests: Sequence[ProviderDispatchRequest], *, timeout: float,
        on_receipt: Callable[[ProviderDispatchRequest, ProviderDispatchReceipt], None] | None = None,
        on_submitted: Callable[[ProviderDispatchRequest], None] | None = None,
        on_unknown: Callable[[ProviderDispatchRequest], None] | None = None,
    ) -> tuple[ProviderDispatchReceipt, ...]: ...

    def recover_receipts(self, run_id: str, caller_id: str) -> tuple[ProviderDispatchReceipt, ...]: ...


class SearchForkLauncher(Protocol):
    def fork_search_run(
        self, run_id: str, checkpoint_id: str = "latest_completed",
    ) -> dict[str, Any]: ...


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
    sandbox: ProcessSandbox | None = None


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
class ModelExecutionFailure:
    """Normalized provider/host failure attached to one real invocation."""

    phase: Literal["admission", "spawn", "request", "response", "transport", "unknown"]
    code: str
    message: str
    provider: str | None = None
    model: str | None = None
    endpoint: str | None = None
    credential: str | None = None
    upstream_status: int | None = None
    retryable: bool = False
    source: str | None = None


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
    failure: ModelExecutionFailure | None = None


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
    def close_candidates(self, run_id: str) -> None: ...
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
