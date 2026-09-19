"""Pure contracts and structural invariants."""

from __future__ import annotations

from typing import (
    Any,
    Literal,
)

from pydantic import (
    ConfigDict,
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


class WorkerBudgetInput(SearchModel):
    model_config = ConfigDict(json_schema_extra={
        "anyOf": [
            {"required": [key], "properties": {key: {"type": "integer"}}}
            for key in ("max_runtime_seconds", "max_turns")
        ],
    })
    max_runtime_seconds: int | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_runtime_or_turn_limit(self) -> "WorkerBudgetInput":
        if self.max_runtime_seconds is None and self.max_turns is None:
            raise ValueError(
                "worker_budget requires max_runtime_seconds or max_turns"
            )
        return self


class WorkerBudget(WorkerBudgetInput):
    on_exceed: Literal["interrupt"] = "interrupt"


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


class ModelRequest(SearchModel):
    """Model choices supplied by Main, without host-owned provenance."""

    model: str = Field(min_length=1)
    count: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None
    service_tier: str | None = None

    @field_validator(
        "model", "reasoning_effort", "service_tier"
    )
    @classmethod
    def string_values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model option must be non-empty when provided")
        return value


class ModelSpec(ModelRequest):
    """Normalized model configuration stored in a frozen spec."""

    provider: str | None = None
    adapter_version: str | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "adapter_version")
    @classmethod
    def provenance_must_be_nonempty(cls, value: str | None) -> str | None:
        return cls.string_values_must_be_nonempty(value)


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
    ("codex", "thinkthread"): INNER_AGENT_COMMON,
    ("pi", "direct"): INNER_AGENT_AUTORESEARCH,
    ("pi", "thinkthread"): INNER_AGENT_COMMON,
}
WorkspaceLedgerProjection = Literal["none", "results_tsv"]
WORKSPACE_LEDGER_PROJECTION_NONE: WorkspaceLedgerProjection = "none"
WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV: WorkspaceLedgerProjection = "results_tsv"
