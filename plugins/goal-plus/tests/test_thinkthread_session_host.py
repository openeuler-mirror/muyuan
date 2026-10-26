"""TT candidate delivery through production common Main MCP controls."""

import asyncio
import json
import os
import threading
from contextlib import AsyncExitStack

from fastmcp import Client
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope
from goal_plus.infrastructure.thinkthread_worker_rpc import dispatch_worker_tool
from goal_plus.runtime import FileSearchRuntime
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.server import create_host_mcp
from goal_plus.session_host import NativeCandidateHost
from tests._runtime_helpers import make_project, start_host_verified_goal
from tests._thinkthread_sdk import IntegratedAgentPosix, thinkthread_spec


def test_backend_scenario_harness_uses_actual_host_controls(tmp_path, monkeypatch):
    from tests.st_thinkthread.candidate_sessions import CandidateSessions

    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    sdk = IntegratedAgentPosix(project)
    for module in ("goal_plus.bootstrap", "goal_plus.infrastructure.thinkthread_sessions",
                   "goal_plus.thinkthread_annotation"):
        monkeypatch.setattr(module + ".AgentPosixSdkClient", lambda: sdk)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-scenario-runtime"))
    spec = thinkthread_spec(project)
    spec.strategy.inner_agent = "common"
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)

    def receive(client, root, record, *, persist):
        if "attempt_request" not in record:
            dispatch_worker_tool(root, record, "goal_plus_search_run_verifier", {
                "hypothesis": "One exact edit",
            }, request_id=f"req-{record['candidate_id']}-{record['dispatch_index']}")
            persist()
        sdk.finish(record["thinkthread_id"])

    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_sessions.receive_worker_messages", receive)
    workers = CandidateSessions(runtime, run_id, {"native_session_id": "scenario-main"})
    try:
        workers.start_all()
        first = {candidate_id: workers.wait(candidate_id) for candidate_id in workers.sessions}
        assert len({result.native_session_id for result in first.values()}) == 2
        workers.start("c001", call_id="2")
        second = workers.wait("c001", call_id="2")
        assert second.native_session_id == first["c001"].native_session_id
        assert second.invocation_id != first["c001"].invocation_id
        assert len(runtime.list_iterations(run_id, "c001")) == 2
    finally:
        assert workers.close()["state"] == "closed"
    assert sdk.children == []


@pytest.mark.parametrize("rollback,reconnect,agent_harness", [
    (False, False, "pi"), (True, False, "pi"), (False, True, "pi"), (True, True, "pi"),
    (False, "active", "pi"), (True, "active", "pi"),
    ("reset_unknown", False, "pi"), ("missing_attempt", False, "pi"),
    (False, "stop", "pi"), (False, "stop_active", "pi"),
    (False, "select_active", "pi"), (False, "select_active", "codex"),
])
def test_common_mcp_starts_retained_child_and_settles_two_attempts(tmp_path, monkeypatch, rollback, reconnect, agent_harness):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    sdk = IntegratedAgentPosix(project, agent=agent_harness)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: sdk)
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_sessions.AgentPosixSdkClient", lambda: sdk)
    monkeypatch.setattr("goal_plus.thinkthread_annotation.AgentPosixSdkClient", lambda: sdk)
    root = tmp_path.parent / (tmp_path.name + "-session-runtime")
    goal = start_host_verified_goal(root, project, agent_harness=agent_harness)
    runtime = FileSearchRuntime(root)
    spec = thinkthread_spec(project)
    spec.strategy.inner_agent = "common"
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness=agent_harness)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
         run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
    )
    accepted_inflight = threading.Event()
    allow_completion = threading.Event()
    if reconnect != "active":
        allow_completion.set()

    def receive(client, root_dir, record, *, persist):
        if reconnect in {"stop_active", "select_active"} and record["call_request"]["call_id"] == "stop-probe":
            accepted_inflight.set()
            return
        if rollback == "missing_attempt":
            sdk.finish(record["thinkthread_id"])
            return
        if "attempt_request" not in record:
            accepted = dispatch_worker_tool(root_dir, record, "goal_plus_search_run_verifier", {
                "hypothesis": "One exact change",
            }, request_id=f"req-{record['dispatch_index']}")
            assert accepted["state"] == "accepted"
            queried = dispatch_worker_tool(root_dir, record, "goal_plus_search_run_verifier", {
                "attempt_id": accepted["attempt_id"],
                "timeout_seconds": 0,
            }, request_id=f"query-{record['dispatch_index']}")
            assert queried == accepted
            assert record["attempt_request"]["attempt_id"] == accepted["attempt_id"]
            assert len(runtime.list_iterations(run_id, task.candidate_id)) == (
                record["dispatch_index"] - (2 if reconnect == "stop_active" and record["dispatch_index"] > 1 else 1)
            )
            persist()
            accepted_inflight.set()
        if allow_completion.is_set():
            sdk.finish(record["thinkthread_id"])
            if agent_harness == "codex":
                # Codex keeps its process alive between completed Message turns.
                sdk.child(record["thinkthread_id"])["executionState"] = "running"

    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_sessions.receive_worker_messages", receive)
    hosts = []

    def host_factory(*args, **kwargs):
        agent_harness = NativeCandidateHost(*args, **kwargs)
        hosts.append(agent_harness)
        return agent_harness

    monkeypatch.setattr("goal_plus.session_host.NativeCandidateHost", host_factory)
    mcp = create_host_mcp(root, agent_harness=agent_harness, caller_role="main")

    async def exercise():
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(Client(mcp))
            async def call(action, **args):
                result = await client.call_tool("goal_plus_session_" + action, args,
                    meta={"threadId": goal.active_session.session_id})
                return json.loads(result.content[0].text)

            for number in (1, 2):
                if number == 2 and reconnect in {"stop", "stop_active"}:
                    if reconnect == "stop_active":
                        accepted_inflight.clear()
                        await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id="stop-probe",
                                   prompt="Wait for Main", timeout_seconds=30)
                        assert await asyncio.to_thread(accepted_inflight.wait, 5)
                    await call("stop", session_id=session.agent_session_id)
                    assert sdk.child("tt-child-1")["executionState"] == "absent"
                    assert len(runtime.list_iterations(run_id, task.candidate_id)) == 1
                    if reconnect == "stop_active":
                        stopped = (await call("wait_any", calls=[
                            {"session_id": session.agent_session_id, "call_id": "stop-probe"},
                        ], timeout_seconds=5))["finished"][0]
                        assert stopped["state"] == "failed"
                    first = (await call("wait_any", calls=[
                        {"session_id": session.agent_session_id, "call_id": "1"},
                    ], timeout_seconds=0))["finished"][0]
                    assert first["state"] == "completed"
                if number == 2 and reconnect is True:
                    peer = NativeCandidateHost(root, agent_harness=agent_harness, caller=lambda: goal.active_session.session_id)
                    with pytest.raises(RuntimeError, match="live controller"):
                        peer.open(session.agent_session_id)
                    # Simulate controller process loss after its durable result;
                    # native Child and scope owner both survive the lost service.
                    lost = hosts[0]._sessions.pop(session.agent_session_id).control
                    lost._executor.shutdown(wait=True)
                    lost.session._lease.__exit__(None, None, None)
                    replacement = create_host_mcp(root, agent_harness=agent_harness, caller_role="main")
                    client = await stack.enter_async_context(Client(replacement))
                    restored = await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id="1", prompt="One bounded attempt", timeout_seconds=30)
                    assert restored["native_session_id"] == "tt-child-1"
                    assert restored["call_id"] == "1"
                if number == 2 and rollback:
                    sdk.forced_verifier_scores = [0]
                    if rollback == "reset_unknown":
                        sdk.completion_unknown_before_once.add("fs.branch.reset")
                await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id=str(number),
                           prompt="One bounded attempt", timeout_seconds=30)
                if number == 1 and reconnect == "active":
                    assert await asyncio.to_thread(accepted_inflight.wait, 5)
                    lost = hosts[0]._sessions.pop(session.agent_session_id).control
                    deadline = lost.session.record["execution_deadline_unix"]
                    # Drop only the controller, as abrupt process death would;
                    # its durable accepted request and live Child both survive.
                    lost.session.actor._closing.set()
                    await asyncio.to_thread(lost._executor.shutdown, wait=True)
                    lost.session._lease.__exit__(None, None, None)
                    assert len(runtime.list_iterations(run_id, task.candidate_id)) == 0
                    client = await stack.enter_async_context(Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")))
                    restored = await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id="1", prompt="One bounded attempt", timeout_seconds=30)
                    assert restored["native_session_id"] == "tt-child-1"
                    assert restored["call_id"] == "1"
                    assert hosts[-1]._sessions[session.agent_session_id].control.session.record["execution_deadline_unix"] == deadline
                    replay = await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id="1",
                                        prompt="One bounded attempt", timeout_seconds=30)
                    assert replay["state"] == "running"
                    allow_completion.set()
                completed = (await call("wait_any", calls=[
                    {"session_id": session.agent_session_id, "call_id": str(number)},
                ], timeout_seconds=10))["finished"][0]
                if rollback == "missing_attempt":
                    # A Child may finish its turn without requesting verification;
                    # the invocation completes with no settled attempt.
                    assert completed["state"] == "completed"
                    assert runtime.list_iterations(run_id, task.candidate_id) == []
                    await call("close", session_id=session.agent_session_id)
                    return
                if number == 2 and rollback == "reset_unknown":
                    assert completed["state"] == "failed"
                    settled = runtime.list_iterations(run_id, task.candidate_id)
                    assert len(settled) == 2
                    lost = hosts[0]._sessions.pop(session.agent_session_id).control
                    lost._executor.shutdown(wait=True)
                    lost.session._lease.__exit__(None, None, None)
                    sdk.completion_unknown_once.add("fs.branch.reset")
                    client = await stack.enter_async_context(Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")))
                    restored = await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id="2", prompt="One bounded attempt", timeout_seconds=30)
                    assert restored["call_id"] == "2"
                    completed = (await call("wait_any", calls=[
                        {"session_id": session.agent_session_id, "call_id": "2"},
                    ], timeout_seconds=10))["finished"][0]
                    assert runtime.list_iterations(run_id, task.candidate_id) == settled
                    assert sum(op == "fs.branch.reset" for op, _ in sdk.operations) == 2
                assert completed["state"] == "completed", completed
                assert completed["native_session_id"] == "tt-child-1"
                assert completed["result_available"] is True
                assert "result" not in completed
                detailed = (await call("wait_any", calls=[
                    {"session_id": session.agent_session_id, "call_id": str(number)},
                ], detail=True))["finished"][0]
                feedback = json.loads(detailed["result"]["text"])
                assert feedback["state"] == "verified"
                assert feedback["candidate_id"] == task.candidate_id
                assert feedback["attempt_iteration"] == number
                assert len(runtime.list_iterations(run_id, task.candidate_id)) == number
            if reconnect == "select_active":
                before = runtime.list_iterations(run_id, task.candidate_id)
                accepted_inflight.clear()
                await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id="stop-probe",
                           prompt="Wait for Main selection", timeout_seconds=30)
                assert await asyncio.to_thread(accepted_inflight.wait, 5)
                if agent_harness == "codex":
                    from goal_plus.host_scope import _codex_run_is_durably_quiescent

                    assert not _codex_run_is_durably_quiescent(root, run_id, scope=scope)
                selected = await client.call_tool("goal_plus_search_select", {"run_id": run_id},
                    meta={"threadId": goal.active_session.session_id})
                assert json.loads(selected.content[0].text)["selected_candidate_id"] == task.candidate_id
                assert runtime.list_iterations(run_id, task.candidate_id) == before
                stopped = (await call("wait_any", calls=[
                    {"session_id": session.agent_session_id, "call_id": "stop-probe"},
                ], timeout_seconds=5))["finished"][0]
                assert stopped["state"] == "failed"
                assert sdk.children == []
                if agent_harness == "codex":
                    from goal_plus.host_scope import close_run_candidates
                    from goal_plus.infrastructure.thinkthread_sessions import session_resource_path

                    resource_path = session_resource_path(root, scope["scope_id"], session.agent_session_id)
                    closed_resource = resource_path.read_text()
                    try:
                        resource_path.unlink()
                        with pytest.raises(RuntimeError, match="run_not_quiescent"):
                            close_run_candidates(root_dir=root, scope_id=scope["scope_id"], run_id=run_id)
                        for field in ("scope_id", "run_id", "candidate_id", "agent_session_id",
                                      "thinkthread_id", "fs_branch_id"):
                            unrelated = {**json.loads(closed_resource), field: "unrelated-resource"}
                            resource_path.write_text(json.dumps(unrelated))
                            with pytest.raises(RuntimeError, match="run_not_quiescent"):
                                close_run_candidates(root_dir=root, scope_id=scope["scope_id"], run_id=run_id)
                    finally:
                        resource_path.write_text(closed_resource)
                    assert close_run_candidates(
                        root_dir=root, scope_id=scope["scope_id"], run_id=run_id,
                    )["active_count"] == 0
            else:
                await call("close", session_id=session.agent_session_id)

    try:
        asyncio.run(exercise())
        assert len([op for op, params in sdk.operations if op == "thinkthread.spawn"
                    and "goal_plus_thinkthread_role=annotator" not in params["initialMessage"]]) == 1
        observed = runtime.candidate_sessions.session(session.agent_session_id)
        if rollback != "missing_attempt":
            assert observed.session_handle.metadata["dispatch_count"] == 2
            assert len({item["invocation_id"] for item in observed.session_handle.metadata["dispatches"]}) == 2
        assert all(child["thinkthreadId"] != "tt-child-1" for child in sdk.children)
        assert not (root / "host-pools" / "thinkthread").exists()
        resources = goal_plus_monitor_snapshot(root, run_id=run_id)["run"]["host_cleanup"]["candidate_sessions"]
        assert resources == {"observed": 1, "states": {"closed": 1}, "complete": True}
        if rollback and rollback != "missing_attempt":
            assert any(op == "fs.branch.reset" for op, _ in sdk.operations)
    finally:
        closed = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="test finished")
        assert closed["state"] == "closed", closed
        assert sdk.children == []
