"""Real TT Child module check through the product common MCP session launcher."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from fastmcp import Client

from goal_plus.domain.goal import GoalPlusHostCommandInvocation
from goal_plus.domain.search import CandidateProposal
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope
from goal_plus.infrastructure.clock import utc_timestamp
from goal_plus.runtime import FileSearchRuntime
from goal_plus.server import create_host_mcp
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient

from driver import _runtime_project, _runtime_spec


def first_call_prompt(active):
    return (("Run bash sleep 8 first. " if active else "")
            + "Read your context, replace only project/solution.txt with 1 and a newline, "
            "submit goal_plus_search_run_verifier exactly once and end the turn after accepted.")


def crash_after_first_call(root, identity, caller, receipt_path, active=False):
    session = FileSearchRuntime(root).candidate_sessions.session(identity)
    async def invoke():
        async with Client(create_host_mcp(root, agent_harness="pi", caller_role="main")) as client:
            async def call(action, **params):
                if action == "wait":
                    params["detail"] = True
                response = await client.call_tool("goal_plus_session_" + action, params, meta={"threadId": caller})
                return json.loads(response.content[0].text)

            await call("run", run_id=session.run_id, candidate_id=session.candidate_id,
                       call_id="1", timeout_seconds=180, prompt=first_call_prompt(active))
            if active:
                async with asyncio.timeout(180):
                    while True:
                        result = await call("wait", session_id=identity, call_id="1", timeout_seconds=0)
                        assert result["state"] == "running", result
                        if result["native_session_id"] is not None:
                            break
                        await asyncio.sleep(0.1)
            else:
                result = await call("wait", session_id=identity, call_id="1", timeout_seconds=210)
            receipt_path.write_text(json.dumps(result))
            os._exit(0 if result["state"] == ("running" if active else "completed") else 1)

    asyncio.run(invoke())


def run_unified_session(config):
    workspace = Path(config["report_path"]).parent
    project = _runtime_project(workspace)
    root = Path(os.environ["GOAL_PLUS_ROOT"]) / "st-unified-session"
    native_id = config["native_session_id"]
    goals = FileGoalPlusRuntime(root)
    goal_text = "Verify two isolated candidate revisions through the common session interface."
    goal = goals.start_from_host_command(goal_text, source_path=str(project), invocation=GoalPlusHostCommandInvocation(
        agent_harness="pi", transport="pi_extension_command", command="/goal-plus", action="start",
        session_id=native_id, native_entry_id="unified-session-module-st", invocation_id="pi:unified-session-module-st",
        raw_input_sha256=hashlib.sha256(("/goal-plus " + goal_text).encode()).hexdigest(), invoked_at=utc_timestamp(),
    ))
    goal = goals.activate_session(goal.goal_plus_id, {
        "agent_harness": "pi", "session_id": native_id, "attached_at": goal.created_at, "last_seen_at": goal.updated_at,
    })
    runtime = FileSearchRuntime(root)
    spec = _runtime_spec(project, inner_agent="common")
    spec.budget.max_parallel = 1
    spec.strategy.worker_budget = None
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi", goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id, proposals=[CandidateProposal(
        intent="Complete two independently verified revisions in the same Child.",
        hypothesis="The assigned integer gives an exact verifier score.",
        instructions=["Edit solution.txt to the value requested for this invocation, verify once, and return."],
    )])[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    scope = ensure_execution_scope(root_dir=root, agent_harness="pi", owner_session_id=native_id,
         run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}")
    mcp = create_host_mcp(root, agent_harness="pi", caller_role="main")
    receipts = []
    active_recovery = config["scenario"] == "unified_session_active"
    cold = config["scenario"] == "unified_session_cold" or active_recovery
    steering = config["scenario"] == "unified_session_steering"
    controls = config["scenario"] == "unified_session_controls"
    control_receipts = []

    async def exercise():
        async with Client(mcp) as client:
            async def call(action, **args):
                if action == "wait":
                    args["detail"] = True
                reply = await client.call_tool("goal_plus_session_" + action, args, meta={"threadId": native_id})
                return json.loads(reply.content[0].text)

            if cold:
                opened = await call("run", run_id=run_id, candidate_id=task.candidate_id,
                                    call_id="1", timeout_seconds=180, prompt=first_call_prompt(active_recovery))
                assert opened["native_session_id"] == receipts[0]["native_session_id"], opened
            if active_recovery:
                completed = await call("wait", session_id=session.agent_session_id, call_id="1", timeout_seconds=210)
                assert completed["state"] == "completed", completed
                assert completed["native_session_id"] == receipts[0]["native_session_id"]
                receipts[0] = completed
                evidence = runtime.list_iterations(run_id, task.candidate_id)
                assert len(evidence) == 1 and evidence[0]["score"] == 1
            for number in ((2,) if cold else (1, 2)):
                if controls and number == 2:
                    child_id = receipts[0]["native_session_id"]
                    sdk = AgentPosixSdkClient()
                    for operation in ("interrupt", "stop"):
                        call_id = "probe-" + operation
                        await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id=call_id,
                                   timeout_seconds=180, prompt="Do not edit files or call verifier. Run bash sleep 30 once and wait for Main.")
                        async with asyncio.timeout(60):
                            while True:
                                child = await asyncio.to_thread(sdk.invoke, "thinkthread.get", {"id": child_id})
                                if child.get("agentState") == "running":
                                    break
                                await asyncio.sleep(0.1)
                        if operation == "interrupt":
                            await call(operation, session_id=session.agent_session_id, call_id=call_id, timeout_seconds=60)
                        else:
                            await call(operation, session_id=session.agent_session_id)
                            child = await asyncio.to_thread(sdk.invoke, "thinkthread.get", {"id": child_id})
                            assert child["executionState"] == "absent", child
                        outcome = await call("wait", session_id=session.agent_session_id, call_id=call_id, timeout_seconds=60)
                        child = await asyncio.to_thread(sdk.invoke, "thinkthread.get", {"id": child_id})
                        control_receipts.append({"operation": operation, "outcome": outcome, "child": child})
                        assert outcome["state"] == ("interrupted" if operation == "interrupt" else "failed"), outcome
                        assert len(runtime.list_iterations(run_id, task.candidate_id)) == 1
                prompt = (
                    "Read your candidate context. Run bash sleep 8 before editing so Main can send a revised target. "
                    "Then replace only project/solution.txt with 7 followed by a newline, unless Main revises the target. "
                    "Submit goal_plus_search_run_verifier exactly once, then end this turn."
                    if steering and number == 1 else
                    f"Read your candidate context, then replace only project/solution.txt with {number} followed by a newline. "
                    "Submit goal_plus_search_run_verifier exactly once with a concise hypothesis. After the accepted receipt, end this turn."
                )
                await call("run", run_id=run_id, candidate_id=task.candidate_id, call_id=str(number), timeout_seconds=180,
                           prompt=prompt)
                if steering and number == 1:
                    await call("message", session_id=session.agent_session_id, call_id="1", timeout_seconds=60,
                               prompt="Revised target for this invocation: use 1 instead of 7 in project/solution.txt. Verify exactly once.")
                receipt = await call("wait", session_id=session.agent_session_id, call_id=str(number), timeout_seconds=210)
                receipts.append(receipt)
                assert receipt["state"] == "completed", receipt
                evidence = runtime.list_iterations(run_id, task.candidate_id)
                assert len(evidence) == number
                assert evidence[-1]["score"] == number
            await call("close", session_id=session.agent_session_id)

    try:
        if cold:
            receipt_path = workspace / "first-call-before-controller-exit.json"
            subprocess.run([sys.executable, "-E", "-s", __file__, str(root), session.agent_session_id,
                            native_id, str(receipt_path), "active" if active_recovery else "settled"], check=True, timeout=240)
            receipts.append(json.loads(receipt_path.read_text()))
        asyncio.run(exercise())
    finally:
        closed = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="TT session module ST completed")
        (workspace / "unified-session-receipts.json").write_text(json.dumps({
            "receipts": receipts, "controls": control_receipts, "cleanup": closed,
        }, indent=2))
    assert closed["state"] == "closed", closed
    assert receipts[0]["native_session_id"] == receipts[1]["native_session_id"]
    assert receipts[0]["result"]["invocation_id"] != receipts[1]["result"]["invocation_id"]
    assert project.joinpath("solution.txt").read_text() == "0\n"
    assert not root.resolve().is_relative_to(workspace.resolve())
    assert not list(workspace.glob(".gp*"))
    assert not (root / "host-pools" / "thinkthread").exists()
    assert AgentPosixSdkClient().invoke("thinkthread.list")["children"] == []
    return {"scenario": config["scenario"], "native_session_id": receipts[0]["native_session_id"],
            "invocations": [item["result"]["invocation_id"] for item in receipts], "scores": [1, 2],
            "cleanup_state": closed["state"], "root_unchanged": True, "runtime_outside_snapshot": True,
            "steering_verified": steering, "controller_recovered_during_invocation": active_recovery,
            "control_operations": [item["operation"] for item in control_receipts]}


if __name__ == "__main__":
    crash_after_first_call(Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5] == "active")
