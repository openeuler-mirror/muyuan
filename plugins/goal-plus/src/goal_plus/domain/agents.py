"""Pure contracts and structural invariants."""

from __future__ import annotations

from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from goal_plus.domain.base import SearchModel

AgentHarnessKind = Literal["codex", "pi"]
AgentRuntimeProvider = Literal["direct", "thinkthread"]


def validate_agent_execution(
    agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider,
) -> None:
    if agent_harness == "codex" and runtime_provider == "thinkthread":
        raise ValueError("Codex does not support ThinkThread Search")
    if (agent_harness, runtime_provider) not in EXECUTION_INNER_AGENT_DEFAULTS:
        raise ValueError("unsupported agent harness/runtime provider combination")


def validate_search_execution(
    agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider,
    workspace_provider: str,
) -> None:
    validate_agent_execution(agent_harness, runtime_provider)
    supported = {"thinkthread"} if runtime_provider == "thinkthread" else {"copy", "git_worktree"}
    if workspace_provider not in supported:
        raise ValueError("workspace provider does not match agent runtime provider")


class AgentSessionHandle(SearchModel):
    agent_harness: AgentHarnessKind = "codex"
    runtime_provider: AgentRuntimeProvider = "direct"
    external_id: str | None = None
    nickname: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def execution_is_supported(self) -> "AgentSessionHandle":
        validate_agent_execution(self.agent_harness, self.runtime_provider)
        return self


class WorkerBudget(SearchModel):
    max_runtime_seconds: int | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)
    on_exceed: Literal["interrupt"] = "interrupt"

    @model_validator(mode="after")
    def require_runtime_or_turn_limit(self) -> "WorkerBudget":
        if self.max_runtime_seconds is None and self.max_turns is None:
            raise ValueError(
                "worker_budget requires max_runtime_seconds or max_turns"
            )
        return self


class WorkerLaunchOptions(SearchModel):
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None

    @field_validator("model", "reasoning_effort", "service_tier")
    @classmethod
    def values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("worker launch option must be non-empty when provided")
        return value


class ModelSpec(SearchModel):
    """User-facing model request, normalized before the spec is frozen."""

    model: str = Field(min_length=1)
    count: int | None = Field(default=None, gt=0)
    provider: str | None = None
    adapter_version: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "model", "provider", "adapter_version", "reasoning_effort", "service_tier"
    )
    @classmethod
    def string_values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model option must be non-empty when provided")
        return value


class SelectedModel(SearchModel):
    """Exact host model bound immutably to one candidate slot."""

    slot: int = Field(ge=1)
    model: str = Field(min_length=1)
    provider: str | None = None
    adapter_version: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)


INNER_AGENT_AUTORESEARCH = "autoresearch"
INNER_AGENT_COMMON = "common"
EXECUTION_INNER_AGENT_DEFAULTS: dict[tuple[AgentHarnessKind, AgentRuntimeProvider], str] = {
    ("codex", "direct"): INNER_AGENT_AUTORESEARCH,
    ("pi", "direct"): INNER_AGENT_AUTORESEARCH,
    ("pi", "thinkthread"): INNER_AGENT_COMMON,
}
WorkspaceLedgerProjection = Literal["none", "results_tsv"]
WORKSPACE_LEDGER_PROJECTION_NONE: WorkspaceLedgerProjection = "none"
WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV: WorkspaceLedgerProjection = "results_tsv"
