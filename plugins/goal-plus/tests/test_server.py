from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.utilities.json_schema import dereference_refs
import pytest

from goal_plus import __version__
from goal_plus.goal_plus import FileGoalPlusRuntime
import goal_plus.server as server_module
from goal_plus.server import create_host_mcp, create_mcp
from tests._fastmcp_helpers import tools_by_name
from tests._runtime_helpers import create_candidate, make_project, start_host_verified_goal
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools


def test_host_main_mcp_requires_native_session_for_goal_tools(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project, session_id="main-session")
    runtime = FileGoalPlusRuntime(root)
    runtime.activate_session(
        goal.goal_plus_id,
        {"agent_harness": "codex", "session_id": "main-session"},
    )
    args = {
        "goal_plus_id": goal.goal_plus_id,
        "triage": {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    }

    async def exercise() -> None:
        async with Client(
            create_host_mcp(root, agent_harness="codex", caller_role="main")
        ) as client:
            for metadata in (None, {"threadId": "foreign-session"}):
                denied = await client.call_tool(
                    "goal_plus_record_triage",
                    args,
                    meta=metadata,
                    raise_on_error=False,
                )
                assert denied.is_error
                assert "MainSessionMismatch" in "\n".join(
                    item.text for item in denied.content if item.type == "text"
                )
            allowed = await client.call_tool(
                "goal_plus_record_triage",
                args,
                meta={"threadId": "main-session"},
            )
            assert not allowed.is_error

    asyncio.run(exercise())


def test_search_report_requires_linked_goal_at_mcp_boundary(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    search = FileSearchRuntime(root)
    run_id, _, _ = create_candidate(search, project)

    async def exercise() -> None:
        async with Client(create_mcp(root, caller_role="main")) as client:
            denied = await client.call_tool(
                "goal_plus_search_report",
                {"run_id": run_id},
                raise_on_error=False,
            )
            assert denied.is_error
            assert "linked" in "\n".join(
                item.text for item in denied.content if item.type == "text"
            )

    asyncio.run(exercise())


def test_ready_spec_draft_returns_actionable_tool_error_without_state_change(tmp_path):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal

    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileGoalPlusRuntime(root)
    runtime.record_triage(goal.goal_plus_id, {
        "is_optimization": True, "confidence": "high", "recommended_phase": "search",
    })
    goal_path = root / "goal-plus" / goal.goal_plus_id / "goal.json"
    before = goal_path.read_bytes()
    spec = spec_for(project).model_dump(mode="json", exclude_defaults=True)
    incomplete = {key: value for key, value in spec.items() if key != "process_verifiers"}
    draft = {
        "baseline": {}, "metric": {}, "correctness_gate": {}, "edit_surface": {},
        "search_spec": incomplete, "promotion_rule": "best verified score", "confidence": "high",
    }

    async def exercise():
        async with Client(create_mcp(root, caller_role="main")) as client:
            rejected = await client.call_tool("goal_plus_search_freeze_spec", {
                "goal_plus_id": goal.goal_plus_id, "spec": incomplete, "verifier_artifact_paths": ["evaluator.py"],
            }, raise_on_error=False)
            assert rejected.is_error
            text = "\n".join(item.text for item in rejected.content if item.type == "text")
            assert "process_verifiers" in text
            assert goal_path.read_bytes() == before
            accepted = await client.call_tool("goal_plus_search_freeze_spec", {
                "goal_plus_id": goal.goal_plus_id, "spec": spec, "verifier_artifact_paths": ["evaluator.py"],
            })
            assert not accepted.is_error

    asyncio.run(exercise())
    assert runtime.status(goal.goal_plus_id).spec_draft is None


def test_create_mcp(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".search"

    mcp = create_mcp(runtime_root)

    assert isinstance(mcp, FastMCP)
    assert mcp.version == __version__
    assert not runtime_root.exists()


def test_controller_only_closeout_hides_tools_for_main_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT", "1")
    names = asyncio.run(tools_by_name(create_mcp(tmp_path / ".gp", caller_role="main")))

    assert {
        "goal_plus_search_select",
        "goal_plus_search_promote",
        "goal_plus_search_apply_promotion",
        "goal_plus_record_search_result",
    }.isdisjoint(names)
    assert "goal_plus_status" in names
    assert "goal_plus_search_report" in names

    with pytest.raises(ValueError, match="excluded MCP tools must be public"):
        create_mcp(tmp_path / "bad", excluded_tool_names={"not_a_goal_tool"})


def test_tool_schema_returns_complete_registered_contract_without_state(tmp_path):
    root = tmp_path / ".gp"

    async def exercise():
        async with Client(create_mcp(root, caller_role="main")) as client:
            catalog = {tool.name: tool for tool in await client.list_tools()}
            for name in ("goal_plus_search_freeze_spec", "goal_plus_search_start_batch"):
                response = await client.call_tool("goal_plus_get_tool_schema", {"tool_name": name})
                contract = json.loads(response.content[0].text)
                assert contract == {
                    "name": name,
                    "description": catalog[name].description,
                    "inputSchema": catalog[name].model_dump(by_alias=True)["inputSchema"],
                }
                if name == "goal_plus_search_freeze_spec":
                    spec = dereference_refs(contract["inputSchema"])["properties"]["spec"]["properties"]
                    verifier = spec["process_verifiers"]["items"]
                    assert {"name", "command"} <= set(verifier["required"])
                    assert verifier["properties"]["command"]["items"]["type"] == "string"
                    models = spec["strategy"]["properties"]["models"]["items"]["properties"]
                    assert {"model", "count", "reasoning_effort"} <= set(models)
            for name in ("missing", "goal_plus_search_get_agent_context"):
                rejected = await client.call_tool(
                    "goal_plus_get_tool_schema", {"tool_name": name}, raise_on_error=False,
                )
                assert rejected.is_error
            assert not root.exists()

        async with Client(create_mcp(
            root, caller_role="candidate", caller_agent_session_id="schema-only",
        )) as client:
            assert "goal_plus_get_tool_schema" not in {tool.name for tool in await client.list_tools()}

    asyncio.run(exercise())


def test_public_catalog_has_one_search_spec_and_no_removed_aliases(tmp_path):
    from goal_plus.domain.tool_catalog import PUBLIC_TOOL_NAMES, SESSION_TOOL_NAMES
    tools = asyncio.run(tools_by_name(create_mcp(
        tmp_path / ".gp", caller_role="main", session_controls=lambda *_: None, session_launcher=object(),
    )))
    assert set(tools) <= PUBLIC_TOOL_NAMES
    assert SESSION_TOOL_NAMES <= set(tools)
    assert all(name.startswith("goal_plus_") for name in tools)
    assert sum('"process_verifiers"' in json.dumps(tool.parameters) for tool in tools.values()) == 1
    assert {"run_id", "candidate_id", "call_id", "prompt", "timeout_seconds"} == set(tools["goal_plus_session_run"].parameters["required"])
    assert tools["goal_plus_search_list_allocation_decisions"].parameters["properties"]["status"]["default"] is None
    for name in ("goal_plus_session_open", "goal_plus_session_wake", "goal_plus_session_start",
                 "goal_plus_create", "goal_plus_update_goal", "goal_plus_gate", "goal_plus_save_spec_draft",
                 "goal_plus_link_search_run", "goal_plus_prepare_final_check",
                 "search_freeze_spec", "search_create", "search_start_agent_session"):
        assert name not in tools


def test_cli_exposes_package_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["goal-plus", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        server_module.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"goal-plus {__version__}"


@pytest.mark.parametrize("deferred,worker,auto", [
    (False, True, True), (True, True, True), (True, True, False), (True, False, False),
])
def test_mcp_verifier_submit_and_wait_deliver_results(tmp_path, monkeypatch, deferred, worker, auto):
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, _ = create_candidate(runtime, make_project(tmp_path))
    session = runtime.start_agent_session(run_id, candidate_id, {})
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    view_reads = []

    def execute(command, **kwargs):
        started.set()
        assert release.wait(10)
        return subprocess.CompletedProcess(command, 0, '{"combined_score":1}\n', "")

    facade = SearchTools(runtime)
    verify = facade.goal_plus_search_run_verifier

    def complete(*args, **kwargs):
        try:
            return verify(*args, **kwargs)
        finally:
            if kwargs.get("attempt_id") is None:
                finished.set()

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", execute)
    if auto:
        monkeypatch.setattr(runtime, "should_inject_global_evidence_after_verifier", lambda _: True)
    monkeypatch.setattr(runtime, "get_global_evidence", lambda *args, **kwargs: view_reads.append(args) or [])
    monkeypatch.setattr(facade, "goal_plus_search_run_verifier", complete)
    monkeypatch.setattr(server_module, "SearchTools", lambda *args, **kwargs: facade)
    monkeypatch.setattr(server_module, "VERIFIER_FAST_WAIT_SECONDS", 0 if deferred else 10)

    async def check():
        async with Client(create_mcp(runtime.root_dir)) as client:
            if not deferred:
                release.set()
            try:
                response = await client.call_tool("goal_plus_search_run_verifier", {
                    "run_id": run_id, "candidate_id": candidate_id,
                    "agent_session_id": session.agent_session_id if worker else None,
                    "hypothesis": "One exact attempt",
                })
                result = json.loads(response.content[0].text)
                if deferred:
                    assert result["candidate_action"] == "wait_for_verifier"
                    assert "aggregate_score" not in result
                    assert await asyncio.to_thread(started.wait, 10)
                    assert not finished.is_set()
                    pending = runtime.candidate_creation.state.read_candidate(run_id, candidate_id).verification
                    assert result["attempt_id"] == pending.attempt_id
                    wait_args = {
                        "run_id": run_id, "candidate_id": candidate_id,
                        "attempt_id": pending.attempt_id,
                        "agent_session_id": session.agent_session_id if worker else None,
                        "timeout_seconds": 0,
                    }
                    response = await client.call_tool("goal_plus_search_run_verifier", wait_args)
                    assert json.loads(response.content[0].text)["attempt_id"] == pending.attempt_id
                    response = await client.call_tool("goal_plus_search_run_verifier", {**wait_args, "timeout_seconds": 0.01})
                    assert json.loads(response.content[0].text)["state"] == "pending"
                    invalid = await client.call_tool("goal_plus_search_run_verifier", {**wait_args, "timeout_seconds": -1}, raise_on_error=False)
                    assert invalid.is_error
                    assert not finished.is_set()
                else:
                    assert result["process_passed"] is True
                    assert result["global_evidence_injected"] is True
            finally:
                release.set()
                assert await asyncio.to_thread(finished.wait, 10)
            if deferred:
                response = await client.call_tool("goal_plus_search_run_verifier", wait_args)
                result = json.loads(response.content[0].text)
                assert result["attempt_id"] == pending.attempt_id
                assert result["aggregate_score"] == 1
                assert result.get("global_evidence_injected", False) is (worker and auto)
            assert len(runtime.list_iterations(run_id, candidate_id)) == 1
            assert len(view_reads) == int(worker and auto)

    asyncio.run(check())


def test_create_mcp_registers_search_runtime_tools(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(tools_by_name(mcp))

    history_detail = tools["goal_plus_search_list_history"].parameters["properties"]["detail"]
    assert history_detail["type"] == "boolean"
    assert history_detail["default"] is False

    assert set(tools) == {
        "goal_plus_get_tool_schema",
        "goal_plus_search_freeze_spec",
        "goal_plus_search_create",
        "goal_plus_search_status",
        "goal_plus_search_recovery_status",
        "goal_plus_search_prepare_recovery",
        "goal_plus_search_recover_candidate",
        "goal_plus_search_invalidate_run",
        "goal_plus_search_list_history",
        "goal_plus_search_plan_next",
        "goal_plus_search_start_batch",
        "goal_plus_search_get_agent_context",
        "goal_plus_search_get_global_evidence",
        "goal_plus_search_reference_open",
        "goal_plus_search_stage_shared_tool",
        "goal_plus_search_copy_shared_tool",
        "goal_plus_search_get_evidence_detail",
        "goal_plus_search_get_agent_observability",
        "goal_plus_search_read_shared_cache",
        "goal_plus_search_append_shared_cache",
        "goal_plus_search_run_verifier",
        "goal_plus_search_list_iterations",
        "goal_plus_search_list_allocation_decisions",
        "goal_plus_search_apply_allocation_decision",
        "goal_plus_search_select",
        "goal_plus_search_report",
        "goal_plus_search_promote",
        "goal_plus_search_apply_promotion",
        "goal_plus_status",
        "goal_plus_history",
        "goal_plus_monitor_snapshot",
        "goal_plus_list_models",
        "goal_plus_record_triage",
        "goal_plus_record_work_event",
        "goal_plus_record_search_result",
        "goal_plus_run_final_check",
        "goal_plus_run_work_item",
        "goal_plus_submit_final_check",
        "goal_plus_set_status",
    }


def test_invalidate_run_and_successor_expose_contract_fields(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(tools_by_name(mcp))

    create_properties = tools["goal_plus_search_create"].parameters["properties"]
    invalidation = tools["goal_plus_search_invalidate_run"].parameters

    assert "source_run_id" in create_properties
    assert set(create_properties) == {
        "goal_plus_id",
        "frozen_spec_id",
        "request_id",
        "source_run_id",
    }
    assert "goal_plus_id" in tools["goal_plus_search_freeze_spec"].parameters["required"]
    assert "goal_plus_id" in tools["goal_plus_search_create"].parameters["required"]
    assert "goal_plus_list_models" in tools
    model_tool = tools["goal_plus_list_models"]
    assert model_tool.parameters["properties"]["detail"]["default"] is False
    assert "query" in model_tool.parameters["properties"]
    assert set(invalidation["required"]) == {
        "run_id",
        "reason",
        "summary",
        "evidence",
    }
    assert "verifier_coverage_inadequate" in invalidation["properties"]["reason"][
        "enum"
    ]


def test_mcp_exposes_structured_triage_and_candidate_proposals(tmp_path: Path) -> None:
    tools = asyncio.run(tools_by_name(create_mcp(tmp_path / ".search")))

    triage = tools["goal_plus_record_triage"].parameters["properties"]["triage"]
    proposals = tools["goal_plus_search_start_batch"].parameters["properties"]["proposals"]
    proposal_items = proposals["items"]

    assert set(triage["required"]) == {
        "is_optimization",
        "confidence",
        "recommended_phase",
    }
    assert set(triage["properties"]) == {
        "is_optimization",
        "confidence",
        "recommended_phase",
        "identified_at",
        "scenario",
        "reasons",
        "missing",
    }
    assert proposal_items["required"] == ["intent"]
    assert set(proposal_items["properties"]) == {
        "hypothesis",
        "intent",
        "expected_tradeoff",
        "instructions",
        "metadata",
    }


def test_mcp_returns_large_worker_context_once_as_compact_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "agent_session_id": "agent_001",
        "candidate_task": {"workspace": "/tmp/candidate one"},
    }
    monkeypatch.setattr(
        server_module.SearchTools,
        "goal_plus_search_get_agent_context",
        lambda _self, _agent_session_id: payload,
    )

    async def call_context():
        async with Client(create_mcp(tmp_path / ".search")) as client:
            return await client.call_tool(
                "goal_plus_search_get_agent_context",
                {"agent_session_id": "agent_001"},
            )

    result = asyncio.run(call_context())

    assert result.structured_content is None
    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == payload
    assert result.content[0].text == json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_run_verifier_exposes_optional_agent_session_id(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(tools_by_name(mcp))
    schema = tools["goal_plus_search_run_verifier"].parameters

    assert "agent_session_id" in schema["properties"]
    assert "hypothesis" in schema["properties"]
    assert "toolization_decision" in schema["properties"]
    assert "adopted_tools" not in schema["properties"]
    assert schema["properties"]["scope"]["enum"] == ["process", "promotion"]
    decision_schema = schema["properties"]["toolization_decision"]
    signal_schema = decision_schema["properties"]["signals"]
    assert signal_schema["items"]["enum"] == [
        "repeated_workflow",
        "domain_construction_or_probe",
        "behavior_or_invariant_checker",
        "reproducer_fixture_or_case_generator",
        "parser_trace_or_comparator",
        "peer_setup_or_feedback_reduction",
    ]
    assert signal_schema["maxItems"] == 6
    assert signal_schema["description"] == (
        "Use the six capability-oriented toolization signals."
    )
    exclusion_schema = decision_schema["properties"]["exclusion"]["anyOf"][0]
    assert "no_toolizable_material" in exclusion_schema["enum"]
    assert tools["goal_plus_search_copy_shared_tool"].parameters["required"] == [
        "agent_session_id",
        "tool_id",
        "snapshot_hash",
    ]
    copy_description = tools["goal_plus_search_copy_shared_tool"].description
    assert "精确 id/hash" in copy_description
    assert "临时 inbox" in copy_description
    assert "具体采用契约见 candidate instructions" in copy_description
    assert "adopted_tools" not in copy_description
    assert tools["goal_plus_search_stage_shared_tool"].parameters["required"] == [
        "agent_session_id",
        "name",
        "summary",
        "entrypoint",
        "candidate_relative_source_paths",
    ]
    stage_description = tools["goal_plus_search_stage_shared_tool"].description
    assert ".tmp/tool-drafts/" in stage_description
    assert "只执行" in stage_description
    assert "passing verifier 结算" in stage_description
    for runtime_only_detail in (
        "revision_allowed=true",
        "capability_extension",
        "adoption_fix",
        "candidate 产品实现",
    ):
        assert runtime_only_detail not in stage_description
    assert tools["goal_plus_search_get_global_evidence"].parameters["required"] == [
        "agent_session_id"
    ]
    assert tools["goal_plus_search_get_evidence_detail"].parameters["required"] == [
        "agent_session_id",
        "candidate_id",
        "iteration",
    ]
    assert tools["goal_plus_search_get_evidence_detail"].parameters["properties"]["iteration"][
        "minimum"
    ] == 1


def test_freeze_spec_exposes_complete_nested_search_spec_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(tools_by_name(mcp))

    schema = tools["goal_plus_search_freeze_spec"].parameters
    spec_schema = dereference_refs(schema)["properties"]["spec"]

    assert set(spec_schema["required"]) >= {
        "objective",
        "metric_name",
        "metric_direction",
        "source_path",
        "edit_surface",
        "budget",
        "process_verifiers",
    }
    assert spec_schema["properties"]["edit_surface"]["required"] == ["allow"]
    verifier = spec_schema["properties"]["process_verifiers"]["items"]
    assert set(verifier["required"]) == {"name", "role", "command"}
    assert "ranking_signal" in verifier["properties"]["role"]["enum"]
    assert verifier["properties"]["command"]["type"] == "array"
    strategy = spec_schema["properties"]["strategy"]
    assert "worker_budget" in strategy["properties"]
    model = strategy["properties"]["models"]["items"]
    assert set(model["properties"]) == {"model", "count", "reasoning_effort", "service_tier"}
    worker_budget = strategy["properties"]["worker_budget"]["anyOf"][0]
    assert set(worker_budget["properties"]) == {"max_runtime_seconds", "max_turns"}
    assert set(spec_schema["properties"]["shared_dir"]["properties"]) == {
        "enabled", "max_tools_per_iteration", "max_published_versions_per_family",
        "max_files_per_iteration", "max_path_entries_per_iteration", "max_depth", "max_bytes_per_iteration",
    }
    worker_defaults = strategy["properties"]["worker_launch"]
    assert set(worker_defaults["properties"]) == {"model", "reasoning_effort", "service_tier"}
    assert "evidence_annotator" in strategy["properties"]
    orchestration = strategy["properties"]["orchestration_mode"]
    assert orchestration["default"] == "parallel_loops"
    assert set(orchestration["enum"]) == {
        "rolling_candidates",
        "parallel_loops",
        "adaptive_search",
    }
    assert "verifier" in strategy["properties"]
    selection = strategy["properties"]["selection"]["properties"]
    assert set(selection) == {"ranking_keys", "ranking_tolerance"}
    assert selection["ranking_keys"]["items"]["enum"] == ["hard_score", "quality_score"]
    allocation_policy_set = next(
        item
        for item in strategy["properties"]["allocation"]["anyOf"]
        if "oneOf" in item
    )
    # FastMCP dereferencing drops the OpenAPI discriminator annotation;
    # required policy and per-variant const still enforce version selection.
    assert allocation_policy_set["required"] == ["policy"]
    policies = {
        item["properties"]["policy"]["const"]: item
        for item in allocation_policy_set["oneOf"]
    }
    assert set(policies) == {"swe_search_uct/v1"}
    swe_parameters = policies["swe_search_uct/v1"]["properties"]["parameters"]
    assert set(swe_parameters["properties"]) == {
        "exploration_weight",
        "early_depth_bonus_weight",
        "early_depth_bonus_decay",
        "late_depth_penalty_weight",
        "low_reward_threshold",
        "low_reward_streak",
        "max_children_per_node",
        "max_depth",
    }
    assert "config" not in strategy["properties"]
    assert strategy["properties"]["global_evidence_mode"]["anyOf"][0]["enum"] == [
        "manual", "auto", "independent",
    ]
    assert "search_scheduler" not in strategy["properties"]

    budget = spec_schema["properties"]["budget"]["properties"]
    assert set(budget) == {"max_parallel", "max_candidates"}
    assert "初始创建并实际并行工作" in budget["max_parallel"]["description"]
    assert "null 表示不限制" in budget["max_candidates"]["description"]
    assert "大于等于 max_parallel" in budget["max_candidates"]["description"]

    verifier_description = " ".join(
        tools["goal_plus_search_run_verifier"].description.split()
    )
    assert "VerifierWorkspaceSideEffect" in verifier_description
    assert 'candidate_action="stop_and_report"' in verifier_description


def test_plan_next_exposes_per_round_budget_semantics(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(tools_by_name(mcp))

    plan_tool = tools["goal_plus_search_plan_next"]
    requested_k = plan_tool.parameters["properties"]["requested_k"]

    assert requested_k["default"] == 4
    assert requested_k["exclusiveMinimum"] == 0
    assert "请求初始候选数" in requested_k["description"]
    assert "标准流程必须传入 budget.max_parallel" in requested_k["description"]
    assert "`requested_k` 与" in plan_tool.description
    assert "标准流程必须令两者相等" in plan_tool.description


def test_goal_plus_monitor_snapshot_exposes_read_only_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(tools_by_name(mcp))
    schema = tools["goal_plus_monitor_snapshot"].parameters

    assert "goal_plus_id" in schema["properties"]
    assert "run_id" in schema["properties"]
    assert "stale_after_seconds" in schema["properties"]
    assert "feature_plugins" in schema["properties"]


def test_agent_observability_exposes_read_only_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(tools_by_name(mcp))
    schema = tools["goal_plus_search_get_agent_observability"].parameters

    assert set(schema["required"]) == {"agent_session_id"}
    description = " ".join(tools["goal_plus_search_get_agent_observability"].description.split())
    assert "只读" in description
    assert "reasoning" in description


def test_goal_plus_create_has_no_mode_hint_or_verifier_confirm_tool(
    tmp_path: Path,
) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(tools_by_name(mcp))
    assert "goal_plus_confirm_frozen_verifier" not in tools
    run_schema = tools["goal_plus_run_final_check"].parameters
    submit_schema = tools["goal_plus_submit_final_check"].parameters
    status_schema = tools["goal_plus_set_status"].parameters
    assert set(run_schema["properties"]) == {"goal_plus_id"}
    assert set(submit_schema["required"]) >= {
        "goal_plus_id",
        "check_id",
        "goal_revision",
        "verdict",
        "summary",
    }
    assert submit_schema["properties"]["verdict"]["enum"] == [
        "pass",
        "fail",
        "interrupted",
    ]
    assert status_schema["properties"]["status"]["enum"] == [
        "active",
        "needs_user",
        "blocked",
        "complete",
        "abandoned",
    ]


def test_create_mcp_constructs_runtime_with_configured_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created_runtimes = []
    created_goal_runtimes = []

    class FakeRuntime:
        def __init__(self, root_dir):
            created_runtimes.append(root_dir)

    class FakeGoalRuntime:
        def __init__(self, root_dir):
            created_goal_runtimes.append(root_dir)

    class FakeTools:
        def __init__(self, runtime, goal_runtime, *, candidate_attempt=None):
            self.runtime = runtime
            self.goal_runtime = goal_runtime

        def goal_plus_search_freeze_spec(self, *args, **kwargs):
            return {}

        def goal_plus_search_create(self, *args, **kwargs):
            return {}

        def goal_plus_search_status(self, *args, **kwargs):
            return {}

        def goal_plus_search_list_history(self, *args, **kwargs):
            return {}

        def goal_plus_search_plan_next(self, *args, **kwargs):
            return {}

        def goal_plus_search_start_batch(self, *args, **kwargs):
            return []

        def search_start_agent_session(self, *args, **kwargs):
            return {}

        def search_bind_agent_handle(self, *args, **kwargs):
            return {}

        def search_continue_agent_session(self, *args, **kwargs):
            return {}

        def goal_plus_search_get_agent_context(self, *args, **kwargs):
            return {}

        def goal_plus_search_get_global_evidence(self, *args, **kwargs):
            return []

        def goal_plus_search_get_evidence_detail(self, *args, **kwargs):
            return {}

        def goal_plus_search_run_verifier(self, *args, **kwargs):
            return {}

        def goal_plus_search_list_iterations(self, *args, **kwargs):
            return []

        def goal_plus_search_select(self, *args, **kwargs):
            return {}

        def goal_plus_search_report(self, *args, **kwargs):
            return {}

        def goal_plus_search_promote(self, *args, **kwargs):
            return {}

        def goal_plus_search_apply_promotion(self, *args, **kwargs):
            return {}

    class FakeGoalTools:
        def __init__(self, runtime):
            self.runtime = runtime

        def goal_plus_create(self, *args, **kwargs):
            return {}

        def goal_plus_status(self, *args, **kwargs):
            return {}

        def goal_plus_record_triage(self, *args, **kwargs):
            return {}

        def goal_plus_save_spec_draft(self, *args, **kwargs):
            return {}

        def goal_plus_link_search_run(self, *args, **kwargs):
            return {}

        def goal_plus_record_search_result(self, *args, **kwargs):
            return {}

        def goal_plus_set_status(self, *args, **kwargs):
            return {}

        def goal_plus_gate(self, *args, **kwargs):
            return {}

    monkeypatch.setattr(server_module, "FileSearchRuntime", FakeRuntime)
    monkeypatch.setattr(server_module, "FileGoalPlusRuntime", FakeGoalRuntime)
    monkeypatch.setattr(server_module, "SearchTools", FakeTools)
    monkeypatch.setattr(server_module, "GoalPlusTools", FakeGoalTools)

    mcp = create_mcp(tmp_path / "custom-search")

    assert isinstance(mcp, FastMCP)
    assert created_runtimes == [tmp_path / "custom-search"]
    assert created_goal_runtimes == [tmp_path / "custom-search"]
