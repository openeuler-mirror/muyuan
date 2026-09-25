"""Shared final-check identity and native resource wiring, without model calls."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from fastmcp import Client
import pytest

from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity
from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope, close_execution_scope, execution_scope_snapshot
from goal_plus.mcp_client import call_mcp_tool
from goal_plus.server import create_host_mcp, create_mcp
from tests._runtime_helpers import start_host_verified_goal


@pytest.fixture
def pending_check(tmp_path):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, required_final_check=True)
    runtime = FileGoalPlusRuntime(root)
    runtime.record_triage(goal.goal_plus_id, {
        "is_optimization": False, "confidence": "high", "recommended_phase": "goal",
    })
    request = runtime.prepare_final_check(goal.goal_plus_id, "codex")
    identity = GoalPlusFinalCheckerIdentity(**{key: request[key] if key != "check_id" else request["check"][key]
                                               for key in ("goal_plus_id", "check_id", "goal_revision")})
    runtime.goal_completion.bind_native_checker(
        identity, agent_harness="codex", native_session_id="native-owned", scope_id="scope-owned", control_version=goal.control.version,
    )
    return root, runtime, identity


def test_final_checker_mcp_requires_exact_target_and_native_identity(pending_check):
    root, runtime, identity = pending_check
    server = create_mcp(root, caller_role="final_checker", caller_final_check=identity)
    before = runtime.state.read_goal(identity.goal_plus_id).model_dump()

    async def exercise():
        async with Client(server) as client:
            valid_status = {"goal_plus_id": identity.goal_plus_id}
            owned = await client.call_tool("goal_plus_status", valid_status, meta={"threadId": "native-owned"})
            assert json.loads(owned.content[0].text)["goal_plus_id"] == identity.goal_plus_id
            for args, native, reason in (
                ({"goal_plus_id": "gp_peer"}, "native-owned", "FinalCheckerIdentityMismatch"),
                (valid_status, "peer", "FinalCheckerIdentityMismatch"),
                (valid_status, None, "MainSessionMismatch"),
            ):
                result = await client.call_tool("goal_plus_status", args, meta={"threadId": native} if native else None,
                                                raise_on_error=False)
                assert result.is_error
                assert reason in result.content[0].text
                assert runtime.state.read_goal(identity.goal_plus_id).model_dump() == before
            for override in ({"check_id": "peer"}, {"goal_revision": 2}):
                result = await client.call_tool("goal_plus_submit_final_check", {
                    **identity.model_dump(), "verdict": "pass", "summary": "checked", "evidence": [{"file": "delivery"}],
                    **override,
                }, meta={"threadId": "native-owned"}, raise_on_error=False)
                assert result.is_error
                assert runtime.state.read_goal(identity.goal_plus_id).model_dump() == before
            result = await client.call_tool("goal_plus_submit_final_check", {
                **identity.model_dump(), "verdict": "pass", "summary": "checked", "evidence": [{"file": "delivery"}],
                "checker_metadata": {"native_session_id": "forged"},
            }, meta={"threadId": "native-owned"})
            assert not result.is_error

    asyncio.run(exercise())
    completed = runtime.state.read_goal(identity.goal_plus_id)
    assert completed.status == "complete"
    assert completed.final_checks[-1].checker_metadata["native_session_id"] == "native-owned"


def test_old_checker_cannot_submit_after_control_version_changes(pending_check):
    _, runtime, identity = pending_check
    invocation = runtime.state.read_goal(identity.goal_plus_id).host_command_invocations[0]
    runtime.control_from_host_command(identity.goal_plus_id, invocation=invocation.model_copy(update={
        "action": "pause", "invocation_id": "checker-pause", "command": "$goal-plus",
    }))
    before = runtime.state.read_goal(identity.goal_plus_id).model_dump()
    with pytest.raises(PermissionError, match="FinalCheckerIdentityMismatch"):
        runtime.submit_final_check(**identity.model_dump(), verdict="pass", summary="checked",
                                   evidence=[{"file": "delivery"}], native_session_id="native-owned")
    assert runtime.state.read_goal(identity.goal_plus_id).model_dump() == before
    runtime.control_from_host_command(identity.goal_plus_id, invocation=invocation.model_copy(update={
        "action": "resume", "invocation_id": "checker-resume", "command": "$goal-plus",
    }))
    request = runtime.prepare_final_check(identity.goal_plus_id, "codex")
    assert request["check"]["check_id"] != identity.check_id
    assert runtime.state.read_goal(identity.goal_plus_id).final_checks[0].status == "superseded"


def test_combined_check_serializes_active_request_and_retries_failed_verdict(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from goal_plus.final_check_host import NativeFinalCheckHost
    from goal_plus.ports.session import SessionInvocationResult

    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, required_final_check=True)
    host = NativeFinalCheckHost(root, agent_harness="codex", caller=lambda: goal.active_session.session_id)
    host.runtime.record_triage(goal.goal_plus_id, {
        "is_optimization": False, "confidence": "high", "recommended_phase": "goal",
    })
    scope = ensure_execution_scope(root_dir=root, agent_harness="codex",
        owner_session_id=goal.active_session.session_id, run_id=goal.goal_plus_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": "main-model", "reasoning_effort": "low"})
    entered, release = Event(), Event()
    actors = []

    class Checker:
        closed = False

        def __init__(self):
            self.native_session_id = f"checker-{len(actors)}"

        def run(self, prompt, *, timeout):
            entered.set()
            assert release.wait(10)
            current = host.runtime.state.read_goal(goal.goal_plus_id)
            check = current.final_checks[-1]
            host.runtime.submit_final_check(goal.goal_plus_id, check.check_id, current.goal_revision,
                "fail" if len(actors) == 1 else "pass", "inspected", evidence=[{"file": "delivery"}],
                native_session_id=self.native_session_id)
            return SessionInvocationResult(native_session_id=self.native_session_id,
                invocation_id="review", state="completed", text="inspected",
                agent_harness="codex", runtime_provider="direct")

        def close(self):
            self.closed = True

    def open_checker(*args, **kwargs):
        native = Checker()
        actors.append(native)
        host._resources.append(native)
        return native

    monkeypatch.setattr(host, "open_native", open_checker)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(host.check, goal.goal_plus_id)
            try:
                assert entered.wait(5)
                duplicate = host.check(goal.goal_plus_id)
                assert duplicate["state"] == "running"
                assert len(actors) == 1 and not actors[0].closed
            finally:
                release.set()
            failed = running.result(timeout=10)
        assert failed["final_check"]["status"] == "failed"
        passed = host.check(goal.goal_plus_id)
        assert passed["final_check"]["status"] == "passed"
        assert passed["final_check"]["check_id"] != failed["final_check"]["check_id"]
        assert host.check(goal.goal_plus_id)["final_check"] == passed["final_check"]
        assert len(actors) == 2 and all(actor.closed for actor in actors)
    finally:
        release.set()
        host.shutdown()
        close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="test finished")


@pytest.mark.host_process
@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize("verdict", ["pass", "fail", None, "timeout"])
def test_common_checker_launch_binds_submits_and_closes(tmp_path, monkeypatch, agent_harness, verdict):
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, agent_harness=agent_harness, required_final_check=True)
    runtime = FileGoalPlusRuntime(root)
    runtime.record_triage(goal.goal_plus_id, {
        "is_optimization": False, "confidence": "high", "recommended_phase": "goal",
    })
    request = runtime.prepare_final_check(goal.goal_plus_id, agent_harness)
    identity = GoalPlusFinalCheckerIdentity(goal_plus_id=goal.goal_plus_id, check_id=request["check"]["check_id"], goal_revision=1)
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
        run_id=goal.goal_plus_id,  activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": "main-model", "reasoning_effort": "low"} if agent_harness == "codex"
        else {"model_pattern": "provider/main-model", "thinking_level": "low"},
    )
    actors, inputs = [], []
    native_actor = CodexSession if agent_harness == "codex" else PiSession

    def actor(config, registration, **kwargs):
        inputs.append(config)
        config = replace(config, model=None, reasoning_effort=None, process=replace(config.process,
            command=(sys.executable, str(Path(__file__).parent / "fixtures/native_session_rpc.py"), agent_harness, "normal", str(tmp_path / "executed")),
            environment={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        ))
        native = native_actor(config, registration, **kwargs)
        actors.append(native)
        run = native.run

        def review(prompt, **params):
            result = run(prompt, **params)
            if verdict == "timeout":
                raise TimeoutError("test checker timeout")
            if verdict:
                call_mcp_tool(root, "goal_plus_submit_final_check", {
                    **identity.model_dump(), "verdict": verdict, "summary": "delivery inspected", "evidence": [{"file": "delivery"}],
                    "findings": [{"issue": "missing edge case"}] if verdict == "fail" else [],
                }, caller_role="final_checker", caller_final_check=identity, native_session_id=native.native_session_id)
            return result

        native.run = review
        return native

    monkeypatch.setattr("goal_plus.goal_session_host." + native_actor.__name__, actor)

    async def exercise():
        async with Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")) as client:
            denied = await client.call_tool("goal_plus_run_final_check", {"goal_plus_id": identity.goal_plus_id},
                                             meta={"threadId": "peer"}, raise_on_error=False)
            assert denied.is_error and not actors
            result = await client.call_tool("goal_plus_run_final_check", {"goal_plus_id": identity.goal_plus_id},
                                            meta={"threadId": goal.active_session.session_id})
            return json.loads(result.content[0].text)

    try:
        if verdict == "timeout":
            with pytest.raises(Exception, match="test checker timeout"):
                asyncio.run(exercise())
        else:
            result = asyncio.run(exercise())
            assert result["final_check"]["status"] == {"pass": "passed", "fail": "failed"}.get(verdict, "interrupted")
            assert "checker_metadata" not in result["final_check"]
            assert "text" not in result["invocation"]
            assert "observation" not in result["invocation"]
            if verdict == "fail":
                assert result["final_check"]["findings"] == [{"issue": "missing edge case"}]
        record = runtime.state.read_goal(goal.goal_plus_id)
        assert record.status == ("complete" if verdict == "pass" else "active")
        assert record.final_checks[-1].status == {"pass": "passed", "fail": "failed"}.get(verdict, "interrupted")
        assert len(actors) == 1 and actors[0].rpc.process.returncode is not None
        assert inputs[0].model == ("main-model" if agent_harness == "codex" else "provider/main-model")
        if agent_harness == "codex":
            assert inputs[0].configuration["sandbox_mode"] == "read-only"
        else:
            command = inputs[0].process.command
            from goal_plus.tools import FINAL_CHECKER_TOOL_NAMES

            assert set(command[command.index("--tools") + 1].split(",")) == {"read", *FINAL_CHECKER_TOOL_NAMES}
        assert all(item["status"] == "finished" for item in execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"])
    finally:
        for native in actors:
            native.close()
        assert close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="test finished")["state"] == "closed"
