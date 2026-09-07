"""Host policy and model discovery capabilities, separate from source I/O."""

from typing import Any, Protocol
from goal_plus.domain.agents import AgentHostKind
from goal_plus.domain.search import SearchSpec, StrategySpec


class ModelDiscovery(Protocol):
    def list_available_models(
        self, host: AgentHostKind, query: str | None = None,
    ) -> dict[str, Any]: ...


class WorkerPolicyBinding(Protocol):
    def normalize_worker_policy(
        self, strategy: StrategySpec, worker_policy: dict[str, Any] | None = None,
        *, host: AgentHostKind,
    ) -> dict[str, Any]: ...


class SearchRunBindings(WorkerPolicyBinding, Protocol):
    def validate_host_workspace_backend(self, spec: SearchSpec, host: AgentHostKind) -> None: ...
    def validate_host_strategy(self, strategy: StrategySpec, host: AgentHostKind) -> None: ...
    def strategy_mode(self, strategy: StrategySpec) -> str: ...
