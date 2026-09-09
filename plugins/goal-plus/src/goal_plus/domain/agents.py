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

AgentHostKind = Literal["codex", "pi-rpc", "thinkthread"]
NativeAgentKind = Literal["codex", "pi"]


def search_worker_host(native_host: NativeAgentKind, workspace_backend: str) -> AgentHostKind:
    if native_host not in {"codex", "pi"}:
        raise ValueError("Search requires a trusted native host: codex or pi")
    if workspace_backend == "thinkthread":
        if native_host != "pi":
            raise ValueError("Codex does not support ThinkThread Search")
        return "thinkthread"
    return "codex" if native_host == "codex" else "pi-rpc"


class AgentHostHandle(SearchModel):
    host: AgentHostKind = "codex"
    external_id: str | None = None
    task_name: str | None = None
    nickname: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerBudget(SearchModel):
    max_runtime_seconds: int | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)
    on_exceed: Literal["interrupt"] = "interrupt"
    min_runtime_seconds: int | None = Field(default=None, gt=0)
    min_verifier_runs: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_runtime_or_turn_limit(self) -> "WorkerBudget":
        if self.max_runtime_seconds is None and self.max_turns is None:
            raise ValueError(
                "worker_budget requires max_runtime_seconds or max_turns"
            )
        if (
            self.min_runtime_seconds is not None
            and self.max_runtime_seconds is None
        ):
            raise ValueError(
                "worker_budget.min_runtime_seconds requires max_runtime_seconds"
            )
        if (
            self.min_runtime_seconds is not None
            and self.max_runtime_seconds is not None
            and self.min_runtime_seconds >= self.max_runtime_seconds
        ):
            raise ValueError(
                "worker_budget.min_runtime_seconds must be less than "
                "max_runtime_seconds"
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
WorkspaceLedgerProjection = Literal["none", "results_tsv"]
WORKSPACE_LEDGER_PROJECTION_NONE: WorkspaceLedgerProjection = "none"
WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV: WorkspaceLedgerProjection = "results_tsv"
