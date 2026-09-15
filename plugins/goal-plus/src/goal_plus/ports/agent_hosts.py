"""Narrow host declarations, model discovery and candidate launch translation.

Launch translation returns host inputs; it does not create a native session or
report execution completion. Candidate provenance and scope resource identity
remain separate contracts.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider
from goal_plus.domain.artifacts import WorkspaceProvider
from goal_plus.ports.session import SessionControlTools


class UnsupportedHostCapability(RuntimeError):
    """Raised when a host cannot provide a requested worker capability."""


@dataclass(frozen=True)
class AgentExecutionCapabilities:
    search_enabled: bool = True
    disabled_reason: str | None = None
    supports_model_discovery: bool = False
    supports_model_override: bool = False
    supports_reasoning_effort: bool = False
    supports_service_tier: bool = False
    supports_usage_metadata: bool = False
    supported_workspace_providers: frozenset[WorkspaceProvider] = frozenset()
    supported_inner_agents: frozenset[str] = frozenset()
    session_control: SessionControlTools = field(default_factory=SessionControlTools)


@dataclass(frozen=True)
class AgentExecutionBinding:
    agent_harness: AgentHarnessKind
    runtime_provider: AgentRuntimeProvider
    adapter_version: str
    capabilities: AgentExecutionCapabilities


class HostModelCatalog(Protocol):
    def list_available_models(self, query: str | None = None) -> list[dict[str, Any]]: ...


class CandidateLaunchAdapter(Protocol):
    def build_launch_payload(
        self,
        *,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]: ...

    def build_continue_payload(
        self,
        *,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
