"""Host-native inference inputs and observations, separate from role acceptance."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import JsonValue

from goal_plus.ports.process import ProcessOutcome
from goal_plus.ports.sandbox import ProcessSandbox


@dataclass(frozen=True)
class InferenceArtifactView:
    view_id: str
    root: Path


@dataclass(frozen=True)
class InferenceRoleCapabilities:
    role: Literal["annotation", "verifier"]
    artifact_views: tuple[InferenceArtifactView, ...] = ()


@dataclass(frozen=True)
class PiInferenceTools:
    names: tuple[str, ...]
    extension: str
    output_env: str


@dataclass(frozen=True)
class StructuredInferenceRequest:
    agent_harness: Literal["codex", "pi"]
    instructions: str
    prompt: str
    output_schema: dict[str, Any]
    model: str | None
    reasoning_effort: str | None
    timeout_seconds: float
    timeout_detail: str
    cancellation_detail: str
    active_check: Literal["before_wait", "after_timeout"] = "before_wait"
    pi_tools: PiInferenceTools | None = None
    runtime_provider: Literal["direct"] = "direct"
    sandbox: ProcessSandbox | None = None


@dataclass(frozen=True)
class InferenceInvocation:
    invocation_id: str
    agent_harness: Literal["codex", "pi"]
    model: str | None
    reasoning_effort: str | None
    started_at: str
    completed_at: str
    elapsed_seconds: float
    runtime_provider: Literal["direct"] = "direct"


@dataclass(frozen=True)
class StructuredInferenceResult:
    process: ProcessOutcome
    stop_detail: str | None
    pid: int | None
    returncode: int | None
    output_text: str | None
    payload: JsonValue | None
    output_error: OSError | ValueError | None
    assistant_message: dict[str, Any] | None
    usage: dict[str, int | float]
    invocation: InferenceInvocation
