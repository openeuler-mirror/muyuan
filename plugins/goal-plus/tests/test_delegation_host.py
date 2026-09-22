"""Common dispatch, same-session rework and host resource cleanup."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest

from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.delegation_host import NativeDelegationHost
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import (
    close_execution_scope,
    ensure_execution_scope,
    execution_scope_snapshot,
    goal_scope_resume_nonce,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.server import create_host_mcp
from goal_plus.ports.session import NativeMainCaller
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
def test_resume_rebinds_closed_main_scope_for_new_native_work(tmp_path, agent_harness):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, agent_harness=agent_harness)
    runtime = FileGoalPlusRuntime(root)
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness=agent_harness,
        owner_session_id=goal.active_session.session_id,
        run_id=goal.goal_plus_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
    )
    resume_before_close = goal.host_command_invocations[0].model_copy(
        update={"action": "resume", "invocation_id": f"resume-before-close:{agent_harness}"}
    )
    runtime.control_from_host_command(
        goal.goal_plus_id, invocation=resume_before_close,
    )
    assert close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="interrupted")["state"] == "closed"

    host = NativeDelegationHost(
        root, agent_harness=agent_harness, caller=lambda: goal.active_session.session_id
    )
    with pytest.raises(RuntimeError, match="new work is fenced"):
        host.admit_goal(goal.goal_plus_id, goal.active_session.session_id)

    resume_after_close = goal.host_command_invocations[0].model_copy(
        update={"action": "resume", "invocation_id": f"resume-after-close:{agent_harness}"}
    )
    runtime.control_from_host_command(
        goal.goal_plus_id, invocation=resume_after_close,
    )
    _, rebound = host.admit_goal(goal.goal_plus_id, goal.active_session.session_id)
    _, reused = host.admit_goal(goal.goal_plus_id, goal.active_session.session_id)

    assert rebound["scope_id"] != scope["scope_id"]
    assert reused["scope_id"] == rebound["scope_id"]
    assert rebound["state"] == "open"
    assert rebound["activation_id"] == f"{goal.goal_plus_id}:{goal.control.version}"
    assert execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["state"] == "closed"

    runtime.control_from_host_command(
        goal.goal_plus_id, invocation=resume_before_close,
    )
    assert execution_scope_snapshot(
        root_dir=root, scope_id=rebound["scope_id"]
    )["resume_barrier_id"] == resume_after_close.invocation_id
    assert close_execution_scope(
        root_dir=root, scope_id=rebound["scope_id"], reason="interrupted again"
    )["state"] == "closed"
    with pytest.raises(RuntimeError, match="new work is fenced"):
        host.admit_goal(goal.goal_plus_id, goal.active_session.session_id)


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
def test_resume_barrier_covers_search_only_scope(tmp_path, agent_harness):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, agent_harness=agent_harness)
    goals = FileGoalPlusRuntime(root)
    search = FileSearchRuntime(root)
    project = make_project(tmp_path)
    frozen = search.freeze_spec(
        spec_for(project),
        [project / "evaluator.py"],
        goal_plus_id=goal.goal_plus_id,
        agent_harness=agent_harness,
    )
    run_id = search.create_run(frozen.frozen_spec_id)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness=agent_harness,
        owner_session_id=goal.active_session.session_id,
        run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
    )
    invocation = goal.host_command_invocations[0].model_copy(
        update={"action": "resume", "invocation_id": f"search-resume:{agent_harness}"}
    )

    goals.control_from_host_command(goal.goal_plus_id, invocation=invocation)

    observed = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
    assert observed["resume_barrier_id"] == invocation.invocation_id
    closed = close_execution_scope(
        root_dir=root, scope_id=scope["scope_id"], reason="interrupted"
    )
    assert goal_scope_resume_nonce(
        goals.status(goal.goal_plus_id), closed
    ) is None


@pytest.mark.parametrize("event", ["dispatch", "rework", "bind", "message"])
@pytest.mark.parametrize("resumed", [False, True])
def test_dispatch_rejects_revoked_native_main_without_mutation(tmp_path, event, resumed):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path)
    runtime = FileGoalPlusRuntime(root)
    caller = NativeMainCaller("codex", goal.active_session.session_id, goal.control.version)
    invocation = goal.host_command_invocations[0]
    for action in (["pause", "resume"] if resumed else ["pause"]):
        runtime.control_from_host_command(goal.goal_plus_id, invocation=invocation.model_copy(update={
            "action": action, "invocation_id": "delegation-" + action, "command": "$goal-plus",
        }))
    before = runtime.state.read_goal(goal.goal_plus_id).model_dump()
    events = runtime.list_events(goal.goal_plus_id)
    # A stopped Goal reports its authoritative pause reason instead of a
    # misleading entrypoint error; a paused-then-resumed Goal is rejected by
    # session identity.
    with pytest.raises(PermissionError if resumed else RuntimeError,
                       match="MainSessionMismatch" if resumed else "GoalPlusResumeRequired.*reason=user_pause"):
        runtime.goal_dispatch.record_work_event(
            goal.goal_plus_id, "task", event, "Revoked execution.", host="codex", native_main=caller,
        )
    assert runtime.state.read_goal(goal.goal_plus_id).model_dump() == before
    assert runtime.list_events(goal.goal_plus_id) == events


def test_native_late_result_only_records_stale_attempt_audit(tmp_path):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path)
    runtime = FileGoalPlusRuntime(root)
    caller = NativeMainCaller("codex", goal.active_session.session_id, goal.control.version)

    def event(kind, **kwargs):
        return runtime.goal_dispatch.record_work_event(
            goal.goal_plus_id, "task", kind, "Task observation.", host="codex", native_main=caller, **kwargs,
        ).work_items[0]

    first = event("dispatch")
    identity = {"attempt_id": first.attempt_id, "generation": first.generation, "agent_id": "native-owned"}
    event("bind", **identity)
    event("result", **identity)
    second = event("rework")
    before = runtime.state.read_goal(goal.goal_plus_id).model_dump()
    event("result", **identity)
    assert runtime.state.read_goal(goal.goal_plus_id).model_dump() == before
    audit = runtime.list_events(goal.goal_plus_id)[-1]["payload"]
    assert audit["event"] == "stale_result" and audit["current_attempt_id"] == second.attempt_id


@pytest.mark.host_process
@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize("scenario", ["normal", "eof", "cold"])
def test_common_delegation_reuses_session_and_records_new_attempt(tmp_path, monkeypatch, agent_harness, scenario):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, agent_harness=agent_harness)
    runtime = FileGoalPlusRuntime(root)
    runtime.record_triage(goal.goal_plus_id, {"is_optimization": False, "confidence": "high", "recommended_phase": "goal"})
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
         run_id=goal.goal_plus_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": "main-model", "reasoning_effort": "low"} if agent_harness == "codex"
        else {"model_pattern": "provider/main-model", "thinking_level": "low"},
    )
    actors, inputs = [], []
    actor_type = CodexSession if agent_harness == "codex" else PiSession

    def actor(config, registration, **kwargs):
        inputs.append(config)
        config = replace(config, model=None, reasoning_effort=None, process=replace(config.process,
            command=(sys.executable, str(Path(__file__).parent / "fixtures/native_session_rpc.py"), agent_harness,
                     "normal" if scenario == "cold" else scenario, str(tmp_path / "executed")),
            environment={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        ))
        native = actor_type(config, registration, **kwargs)
        actors.append(native)
        return native

    monkeypatch.setattr("goal_plus.goal_session_host." + actor_type.__name__, actor)

    async def exercise():
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")))
            args = {"goal_plus_id": goal.goal_plus_id, "work_item_id": "task", "task": "A bounded task.", "max_runtime_seconds": 30}
            before = runtime.state.read_goal(goal.goal_plus_id).model_dump()
            denied = await client.call_tool("goal_plus_run_work_item", args, meta={"threadId": "peer"}, raise_on_error=False)
            assert denied.is_error and not actors
            assert runtime.state.read_goal(goal.goal_plus_id).model_dump() == before
            if scenario == "eof":
                with pytest.raises(ToolError):
                    await client.call_tool("goal_plus_run_work_item", args, meta={"threadId": goal.active_session.session_id})
                item = runtime.state.read_goal(goal.goal_plus_id).work_items[0]
                assert item.status == "failed" and item.agent_id is None
                return
            results = []
            for index in (1, 2):
                if index == 2 and scenario == "cold":
                    before = runtime.state.read_goal(goal.goal_plus_id).model_dump()
                    async with Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")) as competing:
                        denied = await competing.call_tool("goal_plus_run_work_item", args,
                            meta={"threadId": goal.active_session.session_id}, raise_on_error=False)
                        assert denied.is_error and "WorkSessionStillRunning" in denied.content[0].text
                    assert runtime.state.read_goal(goal.goal_plus_id).model_dump() == before
                    await stack.aclose()
                    assert actors[0].rpc.process.returncode is not None
                    client = await stack.enter_async_context(Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")))
                response = await client.call_tool("goal_plus_run_work_item", args, meta={"threadId": goal.active_session.session_id})
                result = json.loads(response.content[0].text)
                results.append(result)
                assert result["work_item"]["status"] == "result_ready"
                assert result["work_item"]["generation"] == index
                assert result["invocation"]["text"] == f"answer-{1 if scenario == 'cold' else index}"
                assert "result_summary" not in result["work_item"]
                assert "observation" not in result["invocation"]
                assert "usage" not in result["invocation"]
                assert result["work_item"]["agent_id"] == result["invocation"]["native_session_id"]
                assert actors[-1].rpc.process.poll() is None
            assert len(actors) == (2 if scenario == "cold" else 1)
            if scenario == "cold":
                assert inputs[-1].native_session_id == results[0]["invocation"]["native_session_id"]
                if agent_harness == "pi":
                    assert "--session" in inputs[-1].process.command
            assert results[0]["work_item"]["attempt_id"] != results[1]["work_item"]["attempt_id"]
            assert results[0]["invocation"]["native_session_id"] == results[1]["invocation"]["native_session_id"]
            current = runtime.state.read_goal(goal.goal_plus_id).work_items[0]
            assert current.metadata["native_invocation"]["invocation_id"] == results[-1]["invocation"]["invocation_id"]
            assert "text" not in current.metadata["native_invocation"]
            accepted = await client.call_tool("goal_plus_record_work_event", {
                "goal_plus_id": goal.goal_plus_id, "work_item_id": "task", "event": "accepted", "summary": "Reviewed result.",
            }, meta={"threadId": goal.active_session.session_id})
            assert json.loads(accepted.content[0].text)["work_item"]["status"] == "accepted"

    try:
        asyncio.run(exercise())
        assert all(native.rpc.process.returncode is not None for native in actors)
        assert inputs[0].model == ("main-model" if agent_harness == "codex" else "provider/main-model")
        environment = inputs[0].process.environment
        assert environment["GOAL_PLUS_CODEX_ORDINARY_ROLE" if agent_harness == "codex" else "GOAL_PLUS_PI_ROLE"] == "ordinary"
        assert all(item["status"] == "finished" for item in execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"])
    finally:
        for native in actors:
            native.close()
        assert close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="test finished")["state"] == "closed"
