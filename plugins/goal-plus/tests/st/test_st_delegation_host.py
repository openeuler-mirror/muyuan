"""Ordinary delegation uses the same MCP launch and native session on both hosts."""

import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
from uuid import uuid4

from fastmcp import Client
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope, close_execution_scope, execution_scope_snapshot
from goal_plus.server import create_host_mcp
from tests._runtime_helpers import start_host_verified_goal
from tests.st.hosts import st_model_for_host


@pytest.mark.st
@pytest.mark.parametrize("reconnect", [False, True], ids=["live", "cold_recovery"])
@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_native_delegation_and_same_session_rework(tmp_path, st_log_dir, agent_harness, reconnect, isolated_pi_config, monkeypatch, request):
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
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, agent_harness=agent_harness)
    runtime = FileGoalPlusRuntime(root)
    runtime.record_triage(goal.goal_plus_id, {"is_optimization": False, "confidence": "high", "recommended_phase": "goal"})
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
         run_id=goal.goal_plus_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": model, "reasoning_effort": "low"} if agent_harness == "codex"
        else {"model_pattern": model, "thinking_level": "low"},
    )
    results = []
    marker = "handoff-" + uuid4().hex

    async def exercise():
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main"), timeout=240))
            for index, value in enumerate(("first", "second"), start=1):
                if reconnect and index == 2:
                    await stack.aclose()
                    client = await stack.enter_async_context(Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main"), timeout=240))
                memory = (f" Remember {marker} privately in this conversation; do not write it to any file."
                          if index == 1 else " Include the exact handoff marker from the preceding turn in your response.")
                response = await client.call_tool("goal_plus_run_work_item", {
                    "goal_plus_id": goal.goal_plus_id, "work_item_id": "delivery",
                    "task": f"Write exactly {value} followed by a newline into delivery.txt. Verify its contents and return."
                            + (memory if reconnect else ""),
                    "max_runtime_seconds": 180,
                }, meta={"threadId": goal.active_session.session_id})
                result = json.loads(response.content[0].text)
                results.append(result)
                assert result["work_item"]["status"] == "result_ready", result
                assert result["work_item"]["generation"] == index
                assert (tmp_path / "delivery.txt").read_text() == value + "\n"
                if reconnect and index == 2:
                    assert marker in result["invocation"]["text"]
            assert results[0]["invocation"]["native_session_id"] == results[1]["invocation"]["native_session_id"]
            assert results[0]["work_item"]["attempt_id"] != results[1]["work_item"]["attempt_id"]
            await client.call_tool("goal_plus_record_work_event", {
                "goal_plus_id": goal.goal_plus_id, "work_item_id": "delivery", "event": "accepted", "summary": "Verified both deliveries.",
            }, meta={"threadId": goal.active_session.session_id})

    print(f"Native {agent_harness} delegation evidence: {st_log_dir}", flush=True)
    try:
        asyncio.run(exercise())
        assert runtime.state.read_goal(goal.goal_plus_id).work_items[0].status == "accepted"
    finally:
        closed = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="delegation ST finished")
        snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
        (st_log_dir / "native-delegation.json").write_text(json.dumps({
            "agent_harness": agent_harness, "model": model, "results": results,
            "goal": runtime.state.read_goal(goal.goal_plus_id).model_dump(mode="json"),
            "scope_state": closed["state"], "cleanup": snapshot["cleanup"],
            "processes": [{key: item.get(key) for key in ("pid", "label", "status")} for item in snapshot["processes"]],
        }, indent=2) + "\n")
        assert closed["state"] == "closed"
        assert snapshot["cleanup"]["active_count"] == 0 and not snapshot["cleanup"]["errors"]
