"""Typed operational diagnostics, separate from task and session state."""

from typing import Literal, Protocol

from pydantic import Field

from goal_plus.domain.agents import AgentHostKind
from goal_plus.domain.base import SearchModel


ProcessMonitorState = Literal[
    "starting", "running", "process_exited", "timed_out", "terminated", "failed", "completed",
]


class ProcessMonitorIdentity(SearchModel):
    run_id: str | None = None
    candidate_id: str | None = None
    iteration: int | None = None
    attempt: int | None = None
    host: AgentHostKind = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int | float | None = None
    task_kind: Literal["llm_verifier"] | None = None


class ProcessEventSummary(SearchModel):
    type: str
    timestamp: int | float | None = None
    assistant_event_type: str | None = None
    content_index: int | None = None
    delta_bytes: int | None = None
    message_role: str | None = None
    stop_reason: str | None = None
    response_id: str | None = None


class ProcessEventSnapshot(SearchModel):
    json_lines: int = 0
    non_json_lines: int = 0
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    assistant_event_type_counts: dict[str, int] = Field(default_factory=dict)
    last_events: list[ProcessEventSummary] = Field(default_factory=list)


class ProcessMonitorSnapshot(ProcessMonitorIdentity, ProcessEventSnapshot):
    schema_version: Literal[1] = 1
    state: ProcessMonitorState = "starting"
    started_at: str
    updated_at: str
    elapsed_seconds: float = 0.0
    pid: int | None = None
    process_returncode: int | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    detail: str | None = None


class ProcessMonitorSink(Protocol):
    def __call__(self, snapshot: ProcessMonitorSnapshot) -> None: ...


class ProcessObservation(Protocol):
    @property
    def pid(self) -> int | None: ...
    @property
    def returncode(self) -> int | None: ...
