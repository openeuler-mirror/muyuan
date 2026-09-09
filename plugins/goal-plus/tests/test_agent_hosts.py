from __future__ import annotations

from io import StringIO

import pytest

import goal_plus.agent_hosts as agent_hosts
from goal_plus.agent_hosts import (
    INNER_AGENT_AUTORESEARCH,
    INNER_AGENT_COMMON,
    UnsupportedHostCapability,
    host_binding, host_model_catalog, candidate_launch_adapter,
    portable_strategy_mode,
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
    assert host_binding("codex").name == "codex"
    assert host_binding("pi-rpc").name == "pi-rpc"
    thinkthread = host_binding("thinkthread")
    assert thinkthread.name == "thinkthread"
    assert thinkthread.capabilities.supported_workspace_backends == frozenset(
        {"thinkthread"}
    )
    assert thinkthread.capabilities.supported_workspace_backends == {"thinkthread"}
    assert thinkthread.adapter_version == "thinkthread-v3-pi"
    assert thinkthread.capabilities.search_enabled is True
    assert thinkthread.capabilities.disabled_reason is None
    assert thinkthread.capabilities.pool.open_tool == "thinkthread_search_pool_open"
    assert thinkthread.capabilities.pool.wait_tool == "thinkthread_search_pool_wait_any"
    assert thinkthread.capabilities.pool.continuation_mode == "native_session"
    assert thinkthread.capabilities.pool.recovery_mode == "supervisor_persisted"
    assert host_binding("codex").capabilities.supports_model_discovery
    assert host_binding("pi-rpc").capabilities.supports_model_discovery
    assert host_binding("codex").capabilities.supported_inner_agents == {
        INNER_AGENT_AUTORESEARCH,
        INNER_AGENT_COMMON,
    }
    assert host_binding("pi-rpc").capabilities.supported_inner_agents == {
        INNER_AGENT_AUTORESEARCH,
        INNER_AGENT_COMMON,
    }
    assert thinkthread.capabilities.supported_inner_agents == {
        INNER_AGENT_AUTORESEARCH,
        INNER_AGENT_COMMON,
    }


def test_inner_agent_binding_separates_logical_adapter_from_native_role() -> None:
    common = resolve_inner_agent_binding("pi-rpc", INNER_AGENT_COMMON)
    assert common.inner_agent_id == "common"
    assert common.prompt_asset == "assets/pi/prompts/search-candidate-common.md"
    assert common.adapter_version == "builtin-common-v1"
    assert common.native_agent_type is None
    assert common.workspace_ledger_projection == "none"

    legacy_codex = resolve_inner_agent_binding(
        "codex",
        INNER_AGENT_AUTORESEARCH,
        legacy_native_agent_type="search_candidate_agent_deep",
    )
    assert legacy_codex.prompt_asset == "assets/codex/agents/search_candidate_agent.toml"
    assert legacy_codex.native_agent_type == "search_candidate_agent_deep"
    assert legacy_codex.workspace_ledger_projection == "results_tsv"

    thinkthread_autoresearch = resolve_inner_agent_binding(
        "thinkthread", INNER_AGENT_AUTORESEARCH
    )
    assert (
        thinkthread_autoresearch.prompt_asset
        == "assets/pi/prompts/search-candidate-worker.md"
    )
    assert thinkthread_autoresearch.native_agent_type is None
    assert thinkthread_autoresearch.adapter_version == "builtin-autoresearch-git-v1"
    assert thinkthread_autoresearch.continuation_mode == "native_session"
    assert thinkthread_autoresearch.workspace_ledger_projection == "results_tsv"

    with pytest.raises(UnsupportedHostCapability, match="inner_agent='custom'"):
        resolve_inner_agent_binding("pi-rpc", "custom")


def test_host_observation_and_discovery_do_not_receive_launch_capabilities(monkeypatch):
    from types import SimpleNamespace

    session = SimpleNamespace(host="codex")
    monkeypatch.setattr(agent_hosts, "collect_codex_observability", lambda value: {"available": value is session})
    assert agent_hosts.collect_host_observability(session) == {"available": True}
    for host in ("codex", "pi-rpc", "thinkthread"):
        catalog = host_model_catalog(host)
        launch = candidate_launch_adapter(host)
        assert not hasattr(catalog, "build_launch_payload")
        assert not hasattr(launch, "list_available_models")
        assert not hasattr(host_binding(host), "collect_observability")


def test_thinkthread_adapter_payload_is_complete_for_pi_profile() -> None:
    adapter = candidate_launch_adapter("thinkthread")

    launch = adapter.build_launch_payload(
        native_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent-run-001",
        short_intent="try",
        one_paragraph_idea="test an isolated branch",
        worker_budget={"max_runtime_seconds": 60},
        worker_launch={ "model": "provider/model"},
    )
    assert launch["tool"] == "thinkthread_child"
    assert launch["profile"] == "self"
    assert launch["fs"] == "private"
    assert launch["capabilities"] == ["thinkthread.message"]
    assert launch["model"] == {"provider": "provider", "model": "model"}
    assert "agent_session_id=agent-run-001" in launch["message"]

    continued = adapter.build_continue_payload(
        native_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent-run-001",
        external_id="tt-child-1",
        task_name=None,
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

    models = host_model_catalog("pi-rpc").list_available_models("terra")

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
def test_codex_adapter_builds_foreground_spawn_payload() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        native_agent_type=None,
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    assert payload["tool"] == "spawn_agent"
    assert payload["agent_type"] == "default"
    assert payload["fork_turns"] == "none"
    assert payload["task_name"] == "search_agent_0001"
    assert "agent_session_id=agent-0001" in payload["message"]
    assert "assigned_worker_budget=host 默认值" in payload["message"]


@pytest.mark.codex
def test_codex_launch_maps_candidate_contract_to_builtin_default_role() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        native_agent_type="search_candidate_agent",
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    message = payload["message"]
    # Codex's built-in default role has no config layer, so it preserves the
    # inherited parent model. The project search role would reload config after
    # inheritance and can clear a runtime-only model before tier validation.
    assert payload["agent_type"] == "default"
    assert "候选 worker，不是搜索编排器" in message
    assert "search_get_agent_context" in message
    assert "candidate_task" in message
    assert "search_run_verifier" not in message
    assert "search_get_global_evidence" not in message


@pytest.mark.codex
def test_codex_launch_preserves_explicit_nondefault_agent_type() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        native_agent_type="search_candidate_agent_deep",
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    assert payload["agent_type"] == "search_candidate_agent_deep"


@pytest.mark.codex
def test_codex_launch_embeds_contract_and_continue_is_compact() -> None:
    adapter = candidate_launch_adapter("codex")
    worker_prompt = "FULL WORKER CONTRACT: write .tmp/handoff.json before returning."

    launch = adapter.build_launch_payload(
        native_agent_type="search_candidate_agent",
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_prompt=worker_prompt,
    )
    continued = adapter.build_continue_payload(
        native_agent_type="search_candidate_agent",
        candidate_id="c001",
        agent_session_id="agent-0001",
        external_id=None,
        task_name=launch["task_name"],
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
        native_agent_type=None,
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


@pytest.mark.codex
def test_codex_adapter_builds_watchdog_budget_payload() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        native_agent_type=None,
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
        "mode": "parent_watchdog",
        "max_runtime_seconds": 600,
        "initial_wait_timeout_ms": 555000,
        "soft_closeout_seconds": 45,
        "closeout_tool": "send_message",
        "closeout_target": "search_agent_0001",
        "closeout_message": (
            "Worker 的截止时间临近。停止启动新工作；如有需要，最后运行一次 "
            "search_run_verifier，更新 .tmp/handoff.json，并返回简洁摘要。"
        ),
        "final_wait_timeout_ms": 45000,
        "on_exceed": "interrupt",
        "interrupt_tool": "interrupt_agent",
        "interrupt_target": "search_agent_0001",
        "max_turns_hint": 8,
    }


@pytest.mark.codex
def test_codex_adapter_rejects_budget_too_short_for_native_waits() -> None:
    adapter = candidate_launch_adapter("codex")

    with pytest.raises(ValueError, match="two native wait intervals"):
        adapter.build_launch_payload(
            native_agent_type=None,
            candidate_id="cand-0001",
            agent_session_id="agent-0001",
            short_intent="try",
            one_paragraph_idea="try",
            worker_budget={
                "max_runtime_seconds": 19,
                "on_exceed": "interrupt",
            },
        )


@pytest.mark.codex
def test_codex_adapter_separates_autoresearch_lease_from_parent_watchdog() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_launch_payload(
        native_agent_type=None,
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "min_runtime_seconds": 300,
            "min_verifier_runs": 1,
            "max_runtime_seconds": 420,
            "on_exceed": "interrupt",
        },
    )

    control = payload["budget_control"]
    assert control["autoresearch_lease"] == {
        "mode": "subagent_stop",
        "min_runtime_seconds": 300,
        "min_verifier_runs": 1,
        "start_event": "native_child_session",
        "release_before_parent_closeout": True,
    }
    assert control["initial_wait_timeout_ms"] == 375000
    assert control["final_wait_timeout_ms"] == 45000
    assert 300 < control["initial_wait_timeout_ms"] / 1000 < 420


@pytest.mark.codex
def test_codex_adapter_rejects_lease_overlapping_parent_closeout() -> None:
    adapter = candidate_launch_adapter("codex")

    with pytest.raises(ValueError, match="before the parent watchdog soft-closeout"):
        adapter.build_launch_payload(
            native_agent_type=None,
            candidate_id="cand-0001",
            agent_session_id="agent-0001",
            short_intent="try",
            one_paragraph_idea="try",
            worker_budget={
                "min_runtime_seconds": 360,
                "max_runtime_seconds": 400,
                "on_exceed": "interrupt",
            },
        )


@pytest.mark.pi
def test_pi_rpc_adapter_builds_worker_payload() -> None:
    adapter = candidate_launch_adapter("pi-rpc")

    payload = adapter.build_launch_payload(
        native_agent_type=None,
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
        cwd="/tmp/project/.search/runs/run_1/candidates/c001/workspace",
        worker_prompt="first call search_get_agent_context",
        process_environment={"TEST_VISIBLE_DEVICES": "5"},
    )

    assert payload["tool"] == "pi_rpc_worker"
    assert payload["agent_session_id"] == "agent_0001"
    assert payload["candidate_id"] == "c001"
    assert payload["root"] == "/tmp/project/.search"
    assert payload["cwd"].endswith("/c001/workspace")
    assert "session_dir" not in payload
    assert payload["continuation"] == "native_session"
    assert payload["session_persistence"] == "cross_process"
    assert payload["environment"] == {"TEST_VISIBLE_DEVICES": "5"}
    assert "search_get_agent_context" in payload["prompt"]
    assert "agent_session_id=agent_0001" in payload["prompt"]
    assert "assigned_worker_budget={'max_runtime_seconds': 600" in payload["prompt"]
    assert payload["budget_control"] == {
        "mode": "pi_rpc_process_watchdog",
        "continuation": "native_session",
        "max_runtime_seconds": 600,
        "max_turns_hint": 8,
        "soft_closeout_seconds": 45,
        "on_exceed": "interrupt",
    }


@pytest.mark.pi
def test_pi_rpc_adapter_exposes_supervisor_autoresearch_lease() -> None:
    adapter = candidate_launch_adapter("pi-rpc")

    payload = adapter.build_launch_payload(
        native_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "min_runtime_seconds": 300,
            "min_verifier_runs": 2,
            "max_runtime_seconds": 420,
            "on_exceed": "interrupt",
        },
    )

    assert payload["budget_control"]["autoresearch_lease"] == {
        "mode": "pool_supervisor",
        "min_runtime_seconds": 300,
        "min_verifier_runs": 2,
        "start_event": "initial_pool_dispatch",
        "cumulative_across_dispatches": True,
    }
    assert payload["budget_control"]["soft_closeout_seconds"] == 120


@pytest.mark.pi
def test_pi_rpc_adapter_maps_native_worker_launch_options() -> None:
    adapter = candidate_launch_adapter("pi-rpc")

    payload = adapter.build_launch_payload(
        native_agent_type=None,
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
    adapter = candidate_launch_adapter("pi-rpc")

    payload = adapter.build_continue_payload(
        native_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        external_id="agent_0001",
        task_name=None,
        short_intent="continue",
        one_paragraph_idea="continue from runtime context",
        root="/tmp/project/.search",
        cwd="/tmp/project/.search/runs/run_1/candidates/c001/workspace",
        worker_prompt="first call search_get_agent_context",
        host_metadata={
            "pi_metrics": {
                "final_last_entry_id": "entry_7",
                "final_entry_count": 7,
                "usage_total": {"input": 120},
                "duration_seconds": 3.5,
                "started_at": "2026-07-19T00:00:00Z",
            }
        },
        process_environment={"TEST_VISIBLE_DEVICES": "5"},
    )

    assert payload["agent_session_id"] == "agent_0001"
    assert payload["session_id"] == "agent_0001"
    assert payload["continuation"] == "native_session"
    assert payload["session_persistence"] == "cross_process"
    assert payload["environment"] == {"TEST_VISIBLE_DEVICES": "5"}
    assert payload["metrics_baseline"] == {
        "last_entry_id": "entry_7",
        "entry_count": 7,
        "usage_total": {"input": 120},
        "duration_seconds": 3.5,
        "started_at": "2026-07-19T00:00:00Z",
    }
    assert "continue_existing_agent_session=true" in payload["prompt"]
    assert "first call search_get_agent_context" not in payload["prompt"]
    assert "沿用已加载的运行时上下文和 Evidence" in payload["prompt"]
    assert "不改变 frozen inner-agent binding" in payload["prompt"]
    assert "deadline" not in payload["prompt"]


@pytest.mark.codex
def test_codex_continue_uses_followup_task_with_watchdog() -> None:
    adapter = candidate_launch_adapter("codex")

    payload = adapter.build_continue_payload(
        native_agent_type="search_candidate_agent",
        candidate_id="cand_0001",
        agent_session_id="agent_0001",
        external_id=None,
        task_name="search_agent_0001",
        short_intent="continue",
        one_paragraph_idea="continue",
        worker_budget={"max_runtime_seconds": 900, "on_exceed": "interrupt"},
    )

    assert payload["tool"] == "followup_task"
    assert payload["target"] == "search_agent_0001"
    assert "continue_existing_agent_session=true" in payload["message"]
    assert "指令：continue" in payload["message"]
    assert payload["budget_control"]["max_runtime_seconds"] == 900
    assert payload["budget_control"]["interrupt_target"] == "search_agent_0001"


@pytest.mark.codex
def test_codex_continue_requires_a_bound_native_handle() -> None:
    adapter = candidate_launch_adapter("codex")

    with pytest.raises(UnsupportedHostCapability, match="bound task name"):
        adapter.build_continue_payload(
            native_agent_type="search_candidate_agent",
            candidate_id="cand_0001",
            agent_session_id="agent_0001",
            external_id=None,
            task_name=None,
            short_intent="continue",
            one_paragraph_idea="continue",
        )
