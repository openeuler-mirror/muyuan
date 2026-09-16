"""Static host capabilities and asset bindings, without executor construction."""

from goal_plus.domain.agents import (
    AgentHarnessKind,
    AgentRuntimeProvider,
    validate_agent_execution,
    INNER_AGENT_AUTORESEARCH,
    INNER_AGENT_COMMON,
    WORKSPACE_LEDGER_PROJECTION_NONE,
    WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
)
from goal_plus.domain.search import CandidateTask, FrozenSpec
from goal_plus.domain.worker_policy import resolved_inner_agent_id
from goal_plus.ports.agent_hosts import (
    AgentExecutionBinding, AgentExecutionCapabilities, UnsupportedHostCapability,
)
from goal_plus.ports.candidate_host import ResolvedInnerAgentBinding


_EXECUTION_BINDINGS: dict[tuple[AgentHarnessKind, AgentRuntimeProvider], AgentExecutionBinding] = {
    ("codex", "direct"): AgentExecutionBinding("codex", "direct", "codex-app-server-v1", AgentExecutionCapabilities(
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=True,
        supports_service_tier=True,
        supports_usage_metadata=True,
        supported_workspace_providers=frozenset({"copy", "git_worktree"}),
        supported_inner_agents=frozenset(
            {INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON}
        ),
    )),
    ("pi", "direct"): AgentExecutionBinding("pi", "direct", "pi-rpc-v1", AgentExecutionCapabilities(
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=True,
        supports_usage_metadata=True,
        supported_workspace_providers=frozenset({"copy", "git_worktree"}),
        supported_inner_agents=frozenset(
            {INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON}
        ),
    )),
    ("codex", "thinkthread"): AgentExecutionBinding("codex", "thinkthread", "thinkthread-v3-codex", AgentExecutionCapabilities(
        search_enabled=True,
        supported_workspace_providers=frozenset({"thinkthread"}),
        supported_inner_agents=frozenset({INNER_AGENT_COMMON}),
    )),
    ("pi", "thinkthread"): AgentExecutionBinding("pi", "thinkthread", "thinkthread-v3-pi", AgentExecutionCapabilities(
        search_enabled=True,
        supported_workspace_providers=frozenset({"thinkthread"}),
        supported_inner_agents=frozenset(
            {INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON}
        ),
    )),
}


def execution_binding(agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider = "direct") -> AgentExecutionBinding:
    validate_agent_execution(agent_harness, runtime_provider)
    return _EXECUTION_BINDINGS[(agent_harness, runtime_provider)]


def require_execution_enabled(agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider = "direct") -> None:
    binding = execution_binding(agent_harness, runtime_provider)
    if not binding.capabilities.search_enabled:
        raise UnsupportedHostCapability(binding.capabilities.disabled_reason or f"{agent_harness} Search is disabled")


def resolve_inner_agent_binding(
    agent_harness: AgentHarnessKind,
    inner_agent: str,
    runtime_provider: AgentRuntimeProvider = "direct",
) -> ResolvedInnerAgentBinding:
    """Resolve a logical inner-agent id without exposing host roles in specs."""

    adapter = execution_binding(agent_harness, runtime_provider)
    inner_agent_id = inner_agent.strip()
    if inner_agent_id not in adapter.capabilities.supported_inner_agents:
        supported = ", ".join(sorted(adapter.capabilities.supported_inner_agents))
        raise UnsupportedHostCapability(
            f"{agent_harness} execution binding does not support inner_agent={inner_agent_id!r}; "
            f"supported inner agents: {supported or 'none'}"
        )

    prompt_assets: dict[tuple[AgentHarnessKind, str], str] = {
        (
            "codex",
            INNER_AGENT_AUTORESEARCH,
        ): "assets/codex/agents/search_candidate_agent.toml",
        (
            "codex",
            INNER_AGENT_COMMON,
        ): "assets/codex/agents/search_candidate_common_agent.toml",
        (
            "pi",
            INNER_AGENT_AUTORESEARCH,
        ): "assets/pi/prompts/search-candidate-worker.md",
        (
            "pi",
            INNER_AGENT_COMMON,
        ): "assets/pi/prompts/search-candidate-common.md",
    }
    return ResolvedInnerAgentBinding(
        inner_agent_id=inner_agent_id,
        adapter_version=(
            "builtin-autoresearch-git-v1"
            if inner_agent_id == INNER_AGENT_AUTORESEARCH
            else f"builtin-{inner_agent_id}-v1"
        ),
        agent_harness=agent_harness,
        runtime_provider=runtime_provider,
        host_adapter_version=adapter.adapter_version,
        prompt_asset=(None if runtime_provider == "thinkthread" and inner_agent_id == INNER_AGENT_COMMON
                      else prompt_assets.get((agent_harness, inner_agent_id))),
        continuation_mode="native_session",
        workspace_ledger_projection=(
            WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
            if inner_agent_id == INNER_AGENT_AUTORESEARCH
            else WORKSPACE_LEDGER_PROJECTION_NONE
        ),
    )


def candidate_inner_agent_binding(
    frozen: FrozenSpec,
    candidate_task: CandidateTask,
) -> ResolvedInnerAgentBinding:
    worker_policy = candidate_task.strategy_metadata.get("worker_policy", {})
    inner_agent = str(
        worker_policy.get("inner_agent")
        or resolved_inner_agent_id(frozen.spec.strategy, frozen.agent_harness, frozen.runtime_provider)
    )
    return resolve_inner_agent_binding(
        frozen.agent_harness,
        inner_agent,
        frozen.runtime_provider,
    )
