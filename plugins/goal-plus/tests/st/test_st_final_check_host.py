"""A real native checker through the common MCP launcher on each host."""

import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope, close_execution_scope, execution_scope_snapshot
from goal_plus.server import create_host_mcp
from tests._runtime_helpers import start_host_verified_goal
from tests.st.hosts import st_model_for_host


@pytest.mark.st
@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_native_final_check_through_common_mcp(tmp_path, st_log_dir, agent_harness, isolated_pi_config, monkeypatch, request):
    if agent_harness == "codex":
        request.getfixturevalue("installed_codex_config")
    model = st_model_for_host("codex" if agent_harness == "codex" else "pi")
    if agent_harness == "pi":
        checkout = Path(__file__).resolve().parents[2]
        monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(checkout))
        monkeypatch.setenv("GOAL_PLUS_PYTHON", str(checkout / ".goal-plus-dev/venv/bin/python"))
        if not model:
            settings = json.loads((isolated_pi_config / "settings.json").read_text())
            model = settings["defaultProvider"] + "/" + settings["defaultModel"]
    delivery = tmp_path / "delivery.txt"
    delivery.write_text("goal-plus-final-check-smoke\n")
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(
        root, tmp_path, agent_harness=agent_harness, required_final_check=True,
        raw_goal="Verify that delivery.txt contains exactly goal-plus-final-check-smoke followed by a newline. Do not edit files.",
    )
    runtime = FileGoalPlusRuntime(root)
    runtime.record_triage(goal.goal_plus_id, {
        "is_optimization": False, "confidence": "high", "recommended_phase": "goal",
    })
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
         run_id=goal.goal_plus_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": model, "reasoning_effort": "low"} if agent_harness == "codex"
        else {"model_pattern": model, "thinking_level": "low"},
    )
    result = None

    async def exercise():
        async with Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main"), timeout=360) as client:
            response = await client.call_tool("goal_plus_run_final_check", {"goal_plus_id": goal.goal_plus_id},
                                              meta={"threadId": goal.active_session.session_id})
            return json.loads(response.content[0].text)

    print(f"Native {agent_harness} final checker evidence: {st_log_dir}", flush=True)
    try:
        result = asyncio.run(exercise())
        assert result["final_check"]["status"] == "passed", result
        assert result["invocation"]["state"] == "completed", result
        record = runtime.state.read_goal(goal.goal_plus_id)
        assert record.status == "complete"
        assert record.final_checks[-1].evidence
        assert record.final_checks[-1].checker_metadata["native_session_id"] == result["invocation"]["native_session_id"]
        assert delivery.read_text() == "goal-plus-final-check-smoke\n"
    finally:
        closed = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="checker ST finished")
        snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
        (st_log_dir / "native-final-check.json").write_text(json.dumps({
            "agent_harness": agent_harness, "model": model, "result": result,
            "goal": runtime.state.read_goal(goal.goal_plus_id).model_dump(mode="json"),
            "scope_state": closed["state"], "cleanup": snapshot["cleanup"],
            "processes": [{key: item.get(key) for key in ("pid", "label", "status")} for item in snapshot["processes"]],
        }, indent=2) + "\n")
        assert closed["state"] == "closed"
        assert snapshot["cleanup"]["active_count"] == 0 and not snapshot["cleanup"]["errors"]
