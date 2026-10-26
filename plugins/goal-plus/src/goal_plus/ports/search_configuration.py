"""Host policy and model discovery capabilities, separate from source I/O."""

from typing import Any, Protocol
from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider
from goal_plus.domain.search import SearchSpec, StrategySpec


class ModelDiscovery(Protocol):
    def list_available_models(
        self, agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider, query: str | None = None,
    ) -> dict[str, Any]: ...


class WorkerPolicyBinding(Protocol):
    def normalize_worker_policy(
        self, strategy: StrategySpec, worker_policy: dict[str, Any] | None = None,
        *, agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider,
    ) -> dict[str, Any]: ...


class SearchRunBindings(WorkerPolicyBinding, Protocol):
    def validate_host_workspace_provider(self, spec: SearchSpec, agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider) -> None: ...
    def validate_host_strategy(self, strategy: StrategySpec, agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider) -> None: ...
    def strategy_mode(self, strategy: StrategySpec) -> str: ...
