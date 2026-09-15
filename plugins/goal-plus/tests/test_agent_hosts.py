from __future__ import annotations

from io import StringIO

import pytest

import goal_plus.agent_hosts as agent_hosts
from goal_plus.agent_hosts import (
    host_model_catalog, candidate_launch_adapter,
)
from goal_plus.domain.agents import (
    INNER_AGENT_AUTORESEARCH,
    INNER_AGENT_COMMON,
)
from goal_plus.domain.worker_policy import portable_strategy_mode
from goal_plus.ports.agent_hosts import UnsupportedHostCapability
from goal_plus.infrastructure.host_bindings import (
    execution_binding,
    resolve_inner_agent_binding,
)


def test_json_line_reader_skips_noise_until_matching_response() -> None:
    class FakeProcess:
        stdout = StringIO(
            'not-json\n{"id": "skip"}\n{"id": "target", "result": {}}\n'
        )

    response = agent_hosts._read_json_line_until(
        FakeProcess(),  # type: ignore[arg-type]
        lambda payload: payload.get("id") == "target",
        timeout_seconds=1,
    )

    assert response == {"id": "target", "result": {}}


def test_host_binding_returns_all_supported_hosts() -> None:
    assert execution_binding("codex").agent_harness == "codex"
    assert execution_binding("pi").agent_harness == "pi"
    thinkthread = execution_binding("pi", "thinkthread")
    assert thinkthread.agent_harness == "pi" and thinkthread.runtime_provider == "thinkthread"
    assert thinkthread.capabilities.supported_workspace_providers == frozenset(
        {"thinkthread"}
    )
    assert thinkthread.capabilities.supported_workspace_providers == {"thinkthread"}
    assert thinkthread.adapter_version == "thinkthread-v3-pi"
    assert thinkthread.capabilities.search_enabled is True
    assert thinkthread.capabilities.disabled_reason is None
    assert thinkthread.capabilities.session_control.run_tool == "goal_plus_session_run"
    assert thinkthread.capabilities.session_control.wait_tool == "goal_plus_session_wait"
    assert thinkthread.capabilities.session_control == execution_binding("pi").capabilities.session_control
    assert execution_binding("codex").capabilities.supports_model_discovery
    assert execution_binding("pi").capabilities.supports_model_discovery
    assert execution_binding("codex").capabilities.supported_inner_agents == {
        INNER_AGENT_AUTORESEARCH,
        INNER_AGENT_COMMON,
    }
    assert execution_binding("pi").capabilities.supported_inner_agents == {
        INNER_AGENT_AUTORESEARCH,
        INNER_AGENT_COMMON,
    }
    assert thinkthread.capabilities.supported_inner_agents == {
        INNER_AGENT_AUTORESEARCH,
        INNER_AGENT_COMMON,
    }


def test_inner_agent_binding_separates_logical_adapter_from_native_role() -> None:
    common = resolve_inner_agent_binding("pi", INNER_AGENT_COMMON)
    assert common.inner_agent_id == "common"
    assert common.prompt_asset == "assets/pi/prompts/search-candidate-common.md"
    assert common.adapter_version == "builtin-common-v1"
    assert common.workspace_ledger_projection == "none"

    codex = resolve_inner_agent_binding(
        "codex",
        INNER_AGENT_AUTORESEARCH,
    )
    assert codex.prompt_asset == "assets/codex/agents/search_candidate_agent.toml"
    assert codex.workspace_ledger_projection == "results_tsv"

    thinkthread_autoresearch = resolve_inner_agent_binding(
        "pi", INNER_AGENT_AUTORESEARCH, "thinkthread"
    )
    assert (
        thinkthread_autoresearch.prompt_asset
        == "assets/pi/prompts/search-candidate-worker.md"
    )
    assert thinkthread_autoresearch.adapter_version == "builtin-autoresearch-git-v1"
    assert thinkthread_autoresearch.continuation_mode == "native_session"
    assert thinkthread_autoresearch.workspace_ledger_projection == "results_tsv"

    with pytest.raises(UnsupportedHostCapability, match="inner_agent='custom'"):
        resolve_inner_agent_binding("pi", "custom")


def test_host_observation_and_discovery_do_not_receive_launch_capabilities(monkeypatch):
    from types import SimpleNamespace

    session = SimpleNamespace(agent_harness="codex", runtime_provider="direct")
    monkeypatch.setattr(agent_hosts, "collect_codex_observability", lambda value: {"available": value is session})
    assert agent_hosts.collect_host_observability(session) == {"available": True}
    for agent_harness, runtime_provider in (("codex", "direct"), ("pi", "direct"), ("pi", "thinkthread")):
        catalog = host_model_catalog(agent_harness, runtime_provider)
        launch = candidate_launch_adapter(agent_harness, runtime_provider)
        assert not hasattr(catalog, "build_launch_payload")
        assert not hasattr(launch, "list_available_models")
        assert not hasattr(execution_binding(agent_harness, runtime_provider), "collect_observability")


def test_thinkthread_adapter_payload_is_complete_for_pi_profile() -> None:
    adapter = candidate_launch_adapter("pi", "thinkthread")

    launch = adapter.build_launch_payload(
        candidate_id="c001",
        agent_session_id="agent-run-001",
        short_intent="try",
        one_paragraph_idea="test an isolated branch",
        worker_budget={"max_runtime_seconds": 60},
        worker_launch={ "model": "provider/model"},
    )
    assert launch["transport"] == "thinkthread"
    assert launch["profile"] == "self"
    assert launch["fs"] == "private"
    assert launch["capabilities"] == ["thinkthread.message"]
    assert launch["model"] == {"provider": "provider", "model": "model"}
    assert "agent_session_id=agent-run-001" in launch["message"]

    continued = adapter.build_continue_payload(
        candidate_id="c001",
        agent_session_id="agent-run-001",
        external_id="tt-child-1",
        short_intent="continue",
        one_paragraph_idea="next attempt",
        worker_budget={"max_runtime_seconds": 60},
        worker_launch={},
        worker_prompt="# initial worker prompt",
    )
    assert continued["thinkthread_id"] == "tt-child-1"
    assert continued["wake"] is True
    assert "continue_existing_agent_session=true" in continued["message"]
    assert "# initial worker prompt" not in continued["message"]
    assert "这是一个新的 candidate dispatch" in continued["message"]
    assert continued["message"].endswith("本轮目标：next attempt")


@pytest.mark.codex
def test_codex_adapter_lists_models_from_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    process = object()
    responses = iter(
        [
            {"id": "goal-plus-initialize", "result": {}},
            {
                "id": "goal-plus-model-list",
                "result": {
                    "data": [
                        {
                            "id": "gpt-5.6-sol",
                            "model": "gpt-5.6-sol",
                            "displayName": "GPT-5.6 Sol",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "high"}
                            ],
                            "inputModalities": ["text", "image"],
                        }
                    ]
                },
            },
        ]
    )
    monkeypatch.setattr(agent_hosts.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent_hosts, "_send_json_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        agent_hosts,
        "_read_json_line_until",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(agent_hosts.ManagedProcess, "terminate", lambda self: None)

    models = host_model_catalog("codex").list_available_models("sol")

    assert models == [
        {
            "model": "gpt-5.6-sol",
            "model_id": "gpt-5.6-sol",
            "provider": "codex",
            "display_name": "GPT-5.6 Sol",
            "reasoning": True,
            "reasoning_efforts": ["high"],
            "input_modalities": ["text", "image"],
            "source": "codex_app_server_model_list",
        }
    ]


@pytest.mark.pi
def test_pi_adapter_lists_models_from_native_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    process = object()
    monkeypatch.setattr(agent_hosts.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent_hosts, "_send_json_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        agent_hosts,
        "_read_json_line_until",
        lambda *args, **kwargs: {
            "id": "goal-plus-model-list",
            "type": "response",
            "success": True,
            "data": {
                "models": [
                    {
                        "provider": "openai-codex",
                        "id": "gpt-5.6-terra",
                        "name": "GPT-5.6 Terra",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 272000,
                        "maxTokens": 128000,
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(agent_hosts.ManagedProcess, "terminate", lambda self: None)

    models = host_model_catalog("pi").list_available_models("terra")

    assert models == [
        {
            "model": "openai-codex/gpt-5.6-terra",
            "model_id": "gpt-5.6-terra",
            "provider": "openai-codex",
            "display_name": "GPT-5.6 Terra",
            "reasoning": True,
            "input_modalities": ["text", "image"],
            "context_window": 272000,
            "max_tokens": 128000,
            "source": "pi_rpc_get_available_models",
        }
    ]


def test_portable_strategy_mode_classifies_all_builtin_aliases() -> None:
    portable = (
        "agent_guided",
        "agent",
        "default",
        "random",
        "random-mode",
        "random_mode",
    )
    for name in portable:
        assert portable_strategy_mode(name) is True
    for name in ("independent_branches", "evolve", "openevolve", "mcts"):
        assert portable_strategy_mode(name) is False


@pytest.mark.codex
def test_codex_adapter_builds_session_input() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    assert payload["transport"] == "app-server"
    assert not {"tool", "agent_type", "fork_turns", "task_name"}.intersection(payload)
    assert "agent_session_id=agent-0001" in payload["message"]
    assert "assigned_worker_budget=host 默认值" in payload["message"]


@pytest.mark.codex
def test_codex_launch_embeds_candidate_contract() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    message = payload["message"]
    assert payload["transport"] == "app-server"
    assert "候选 worker，不是搜索编排器" in message
    assert "goal_plus_search_get_agent_context" in message
    assert "candidate_task" in message
    assert "goal_plus_search_run_verifier" not in message
    assert "goal_plus_search_get_global_evidence" not in message


@pytest.mark.codex
def test_codex_launch_embeds_contract_and_continue_is_compact() -> None:
    adapter = candidate_launch_adapter("codex")
    worker_prompt = "FULL WORKER CONTRACT: write .tmp/handoff.json before returning."

    launch = adapter.build_launch_payload(
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_prompt=worker_prompt,
    )
    continued = adapter.build_continue_payload(
        candidate_id="c001",
        agent_session_id="agent-0001",
        external_id="bound-native-thread",
        short_intent="continue",
        one_paragraph_idea="continue",
        worker_prompt=worker_prompt,
    )

    assert worker_prompt in launch["message"]
    assert worker_prompt not in continued["message"]
    assert "候选 worker，不是搜索编排器" not in launch["message"]
    assert "指令：continue" in continued["message"]


@pytest.mark.codex
def test_codex_adapter_maps_native_worker_launch_options() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_launch={
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "service_tier": "priority",
        },
    )

    assert payload["model"] == "gpt-5.6-terra"
    assert payload["reasoning_effort"] == "high"
    assert payload["service_tier"] == "priority"


@pytest.mark.parametrize("agent_harness,runtime_provider", [("codex", "direct"), ("pi", "direct"), ("pi", "thinkthread")])
def test_candidate_launch_preserves_explicit_session_budget(agent_harness, runtime_provider) -> None:
    adapter = candidate_launch_adapter(agent_harness, runtime_provider)

    payload = adapter.build_launch_payload(
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "max_runtime_seconds": 600,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
    )

    assert payload["budget_control"] == {
        "mode": "session_control",
        "max_runtime_seconds": 600,
        "on_exceed": "interrupt",
        "max_turns_hint": 8,
    }


@pytest.mark.pi
def test_pi_rpc_adapter_builds_worker_payload() -> None:
    adapter = candidate_launch_adapter("pi")

    payload = adapter.build_launch_payload(
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "max_runtime_seconds": 600,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
        root="/tmp/project/.search",
        worker_prompt="first call goal_plus_search_get_agent_context",
    )

    assert payload["transport"] == "native-rpc"
    assert payload["agent_session_id"] == "agent_0001"
    assert payload["candidate_id"] == "c001"
    assert payload["continuation"] == "native_session"
    assert "goal_plus_search_get_agent_context" in payload["prompt"]
    assert "agent_session_id=agent_0001" in payload["prompt"]
    assert "assigned_worker_budget={'max_runtime_seconds': 600" in payload["prompt"]
    assert payload["budget_control"] == {
        "mode": "session_control",
        "max_runtime_seconds": 600,
        "max_turns_hint": 8,
        "on_exceed": "interrupt",
    }


@pytest.mark.parametrize("agent_harness,runtime_provider", [("pi", "direct"), ("pi", "thinkthread")])
def test_native_launch_does_not_prescribe_removed_pool_workflow(agent_harness, runtime_provider) -> None:
    adapter = candidate_launch_adapter(agent_harness, runtime_provider)

    payload = adapter.build_launch_payload(
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "max_runtime_seconds": 420,
            "on_exceed": "interrupt",
        },
    )

    control = payload["budget_control"]
    assert control["max_runtime_seconds"] == 420
    assert control["on_exceed"] == "interrupt"
    assert "autoresearch_lease" not in control
    assert "soft_closeout_seconds" not in control


@pytest.mark.pi
def test_pi_rpc_adapter_maps_native_worker_launch_options() -> None:
    adapter = candidate_launch_adapter("pi")

    payload = adapter.build_launch_payload(
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_launch={
            "model": "openai-codex/gpt-5.6-sol",
            "reasoning_effort": "high",
        },
    )

    assert payload["model_pattern"] == "openai-codex/gpt-5.6-sol"
    assert payload["thinking_level"] == "high"


@pytest.mark.pi
def test_pi_rpc_adapter_builds_cross_process_native_session_continuation() -> None:
    adapter = candidate_launch_adapter("pi")

    payload = adapter.build_continue_payload(
        candidate_id="c001",
        agent_session_id="agent_0001",
        external_id="agent_0001",
        short_intent="continue",
        one_paragraph_idea="continue from runtime context",
        root="/tmp/project/.search",
        worker_prompt="first call goal_plus_search_get_agent_context",
    )

    assert payload["agent_session_id"] == "agent_0001"
    assert payload["session_id"] == "agent_0001"
    assert payload["continuation"] == "native_session"
    assert "continue_existing_agent_session=true" in payload["prompt"]
    assert "first call goal_plus_search_get_agent_context" not in payload["prompt"]
    assert "沿用已加载的运行时上下文和 Evidence" in payload["prompt"]
    assert "不改变 frozen inner-agent binding" in payload["prompt"]
    assert "deadline" not in payload["prompt"]


@pytest.mark.codex
def test_codex_continue_uses_bound_session_and_explicit_budget() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_continue_payload(
        candidate_id="cand_0001",
        agent_session_id="agent_0001",
        external_id="bound-native-thread",
        short_intent="continue",
        one_paragraph_idea="continue",
        worker_budget={"max_runtime_seconds": 900, "on_exceed": "interrupt"},
    )

    assert payload["transport"] == "app-server"
    assert payload["native_session_id"] == "bound-native-thread"
    assert "continue_existing_agent_session=true" in payload["message"]
    assert "指令：continue" in payload["message"]
    assert payload["budget_control"]["max_runtime_seconds"] == 900
    assert payload["budget_control"]["mode"] == "session_control"


@pytest.mark.codex
def test_codex_continue_requires_a_bound_native_handle() -> None:
    adapter = candidate_launch_adapter("codex")

    with pytest.raises(UnsupportedHostCapability, match="bound native session id"):
        adapter.build_continue_payload(
            candidate_id="cand_0001",
            agent_session_id="agent_0001",
            external_id=None,
            short_intent="continue",
            one_paragraph_idea="continue",
        )
