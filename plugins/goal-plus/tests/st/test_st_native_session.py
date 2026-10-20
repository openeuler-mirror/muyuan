"""Production session actors through an isolated MCP and real scope registration."""

import asyncio
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from uuid import uuid4

from fastmcp import Client
import pytest

from goal_plus.bootstrap import build_scope_process_registry
from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.session_control import NativeSessionControl
from goal_plus.server import create_mcp
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope, execution_scope_snapshot
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.session import NativeSessionInput, SessionProcessInput
from goal_plus.ports.session import ParentCheckpoint, ProviderDispatchRequest
from goal_plus.execution.fork import DispatchGroup
from goal_plus.paths import host_assets_root
from goal_plus.ports.scope import ScopeAttachment
from tests.st.hosts import ST_ACTIVE_ENV, st_model_for_host
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


@pytest.mark.st
@pytest.mark.parametrize("agent_harness,actor", [
    pytest.param("codex", CodexSession, marks=pytest.mark.st_codex),
    pytest.param("pi", PiSession, marks=pytest.mark.st_pi_rpc),
])
def test_fork_keeps_only_completed_checkpoint(tmp_path, st_log_dir, agent_harness, actor):
    timeout = float(os.environ.get("ST_SESSION_PROBE_TIMEOUT", "180"))
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    scope = ensure_execution_scope(root_dir=tmp_path / ".gp", agent_harness=agent_harness, owner_session_id=uuid4().hex)
    registry = build_scope_process_registry(tmp_path / ".gp")
    registration = ProcessRegistration(
        register=lambda pid: registry.register(scope_id=scope["scope_id"], pid=pid, label="fork-probe"),
        finish=lambda token: registry.finish(scope_id=scope["scope_id"], token=token),
    )
    command = ("codex", "app-server", "--stdio") if agent_harness == "codex" else (
        os.environ.get("ST_PI_BINARY", "pi"), "--mode", "rpc", "--session-dir", str(session_dir),
        "--no-extensions", "--no-context-files", "--no-tools",
    )
    config = NativeSessionInput(agent_harness=agent_harness,
        process=SessionProcessInput(command, tmp_path, {**os.environ, ST_ACTIVE_ENV: "fork_checkpoint_st"}),
        model=st_model_for_host(agent_harness), reasoning_effort="low",
        instructions="Answer from this conversation only. Do not use tools.",
        configuration={"sandbox_mode": "read-only", "fork_helper": str(host_assets_root() / "assets/pi/extensions/lib/fork.mjs")}
                      if agent_harness == "pi" else {"sandbox_mode": "read-only"})
    early, late = uuid4().hex, uuid4().hex
    sessions = []
    try:
        parent = actor(config, registration, timeout=timeout)
        sessions.append(parent)
        first = parent.run(f"The early marker is {early}. Remember it. Reply READY.", timeout=timeout)
        assert first.state == "completed"
        checkpoint = first.invocation_id if agent_harness == "codex" else first.observation.metrics_baseline["last_entry_id"]
        second = parent.run(f"The late marker is {late}. Remember it. Reply READY.", timeout=timeout)
        assert second.state == "completed"
        parent_file = None
        if agent_harness == "pi":
            parent_file = Path(parent._command("get_state", timeout=timeout)["data"]["sessionFile"])
            parent_bytes = parent_file.read_bytes()
        requests = []
        for index in (1, 2):
            workspace = tmp_path / f"branch-{index}"
            workspace.mkdir()
            requests.append(ProviderDispatchRequest("fork-probe", index,
                ParentCheckpoint(parent.native_session_id, checkpoint,
                    "completed_turn" if agent_harness == "codex" else "entry", "spec_probe"),
                workspace, {"agent_session_id": str(uuid4())}, {"max_turns": 1}))
        receipts = DispatchGroup(parent).start(requests, timeout=timeout)
        assert len({r.provider_session_id for r in receipts}) == 2
        if parent_file:
            assert parent_file.read_bytes() == parent_bytes
            assert receipts[0].context_projection_digest == receipts[1].context_projection_digest
        # The production host closes the temporary parent app-server before
        # candidate hosts resume the provider-created branch threads.
        parent.close()
        sessions.remove(parent)
        branches = []
        for receipt in receipts:
            process = replace(config.process, cwd=receipt.workspace)
            if agent_harness == "pi":
                process = replace(process, command=(*command, "--session", receipt.native_session_file))
            branch = actor(replace(config, process=process, native_session_id=receipt.provider_session_id),
                           registration, timeout=timeout)
            sessions.append(branch)
            branches.append(branch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(branch.run, "Return the early marker and late marker. Use MISSING for any unknown marker.",
                                   timeout=timeout) for branch in branches]
            results = [future.result() for future in futures]
        assert all(r.state == "completed" and early in r.text and late not in r.text and "MISSING" in r.text for r in results)
        parent_process = config.process
        if agent_harness == "pi" and parent_file is not None:
            parent_process = replace(parent_process, command=(*command, "--session", str(parent_file)))
        parent = actor(replace(config, process=parent_process,
                               native_session_id=receipts[0].parent_native_session_id),
                       registration, timeout=timeout)
        sessions.append(parent)
        parent_result = parent.run("Return the late marker only.", timeout=timeout)
        assert late in parent_result.text
        (st_log_dir / "fork-checkpoint.json").write_text(json.dumps({
            "checkpoint": checkpoint, "receipts": [asdict(r) for r in receipts],
            "branch_results": [asdict(r) for r in results], "parent_result": asdict(parent_result),
        }, default=str, indent=2))
    finally:
        for session in reversed(sessions):
            session.close()
        closed = close_execution_scope(root_dir=tmp_path / ".gp", scope_id=scope["scope_id"], reason="fork probe finished")
        assert closed["state"] == "closed"
        assert all(item["status"] == "finished" for item in closed["processes"])


@pytest.mark.st
@pytest.mark.parametrize("agent_harness,actor", [
    pytest.param("codex", CodexSession, marks=pytest.mark.st_codex),
    pytest.param("pi", PiSession, marks=pytest.mark.st_pi_rpc),
])
def test_native_session_two_turns_with_scope(tmp_path: Path, st_log_dir: Path, agent_harness, actor):
    timeout = float(os.environ.get("ST_SESSION_PROBE_TIMEOUT", "180"))
    marker = uuid4().hex
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=marker,

    )
    scope_id = scope["scope_id"]
    registry = build_scope_process_registry(root)
    registration = ProcessRegistration(
        register=lambda pid: registry.register(
            scope_id=scope_id, pid=pid,  label="native-session-st",
        ),
        finish=lambda token: registry.finish(scope_id=scope_id, token=token),
    )
    model = st_model_for_host("codex" if agent_harness == "codex" else "pi")
    command = ["codex", "app-server", "--stdio"] if agent_harness == "codex" else [
        os.environ.get("ST_PI_BINARY", "pi"), "--mode", "rpc", "--no-session",
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-tools",
    ]
    if agent_harness == "pi" and model:
        command.extend(["--model", model])
    config = NativeSessionInput(
        agent_harness=agent_harness,
        process=SessionProcessInput(tuple(command), tmp_path, {
            **os.environ, ST_ACTIVE_ENV: "native_session_scope_st",
        }),
        model=model, reasoning_effort="low",
        instructions="Answer the text task directly. Do not use tools.",
        configuration={"sandbox_mode": "read-only"},
    )
    session = None
    control = None
    print(f"Native {agent_harness} session evidence: {st_log_dir}", flush=True)
    try:
        session = actor(config, registration, timeout=timeout)
        control = NativeSessionControl(session)

        def lookup(session_id, operation):
            if session_id != session.native_session_id:
                raise ValueError("session identity mismatch")
            return control

        mcp = create_mcp(root, caller_role="main", session_controls=lookup)

        async def exercise():
            async with Client(mcp, timeout=timeout) as client:
                tools = await client.list_tools()
                assert "goal_plus_session_wait_any" in {tool.name for tool in tools}
                await asyncio.to_thread(
                    control.wake,
                    f"Remember marker {marker}. What is 17 + 25? Reply with the number only.",
                    call_id="first", timeout=timeout,
                )
                response = await client.call_tool("goal_plus_session_wait_any", {
                    "calls": [{"session_id": session.native_session_id, "call_id": "first"}],
                    "timeout_seconds": timeout, "detail": True,
                })
                receipt = json.loads(response.content[0].text)
                assert receipt["finished"][0]["state"] == "completed", receipt
                first = receipt["finished"][0]["result"]
                assert first["text"].strip() == "42"
                await asyncio.to_thread(
                    control.wake,
                    "Return the marker from my previous message and the value of 9 * 7.",
                    call_id="second", timeout=timeout,
                )
                response = await client.call_tool("goal_plus_session_wait_any", {
                    "calls": [{"session_id": session.native_session_id, "call_id": "second"}],
                    "timeout_seconds": timeout, "detail": True,
                })
                receipt = json.loads(response.content[0].text)
                assert receipt["finished"][0]["state"] == "completed", receipt
                second = receipt["finished"][0]["result"]
                assert marker in second["text"] and "63" in second["text"]
                assert first["native_session_id"] == second["native_session_id"]
                assert first["invocation_id"] != second["invocation_id"]
                assert first["state"] == second["state"] == "completed"
                assert first["usage"]["input"] >= 0
                assert second["usage"]["output"] > 0
                assert first["observation"]["duration_seconds"] > 0
                if agent_harness == "pi":
                    metrics = second["observation"]["pi_metrics"]
                    assert metrics["usage_total"]["assistantMessages"] == 2
                    assert metrics["usage_total"]["input"] == first["usage"]["input"] + second["usage"]["input"]
                    assert not metrics.get("errors"), metrics
                await client.call_tool("goal_plus_session_close", {"session_id": session.native_session_id})
                return [first, second]

        results = asyncio.run(exercise())
    finally:
        if control is not None:
            control.close()
        elif session is not None:
            session.close()
        closed = close_execution_scope(root_dir=root, scope_id=scope_id, reason="ST finished")
        assert closed["state"] == "closed"
    snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope_id)
    assert all(item["status"] == "finished" for item in snapshot["processes"])
    assert session.rpc.process.returncode is not None
    (st_log_dir / "native-session.json").write_text(json.dumps({
        "agent_harness": agent_harness, "model": model, "results": results,
        "process_returncode": session.rpc.process.returncode,
        "scope_state": snapshot["state"], "candidate_authorization_verified": False,
        "control_contract": "controller wake with goal_plus_session_wait_any/close", "launcher": "test_controller",
    }, indent=2) + "\n")


@pytest.mark.st
@pytest.mark.st_codex
def test_codex_session_candidate_uses_bound_mcp_tools(tmp_path: Path, st_log_dir: Path):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.mcp_client import McpSessionToolBridge
    from goal_plus.runtime import FileSearchRuntime

    timeout = float(os.environ.get("ST_SESSION_PROBE_TIMEOUT", "180"))
    source = make_project(tmp_path)
    (source / "evaluator.py").write_text(
        "import json\nfrom pathlib import Path\nvalues = {}\n"
        "exec(Path('initial_program.py').read_text(), values)\n"
        "print(json.dumps({'combined_score': float(values['VALUE'])}))\n"
    )
    runtime = FileSearchRuntime(tmp_path / ".gp")
    goal = start_host_verified_goal(runtime.root_dir, source)
    spec = spec_for(source, max_parallel=1)
    spec = spec.model_copy(update={"strategy": spec.strategy.model_copy(update={"inner_agent": "common"})})
    frozen = runtime.freeze_spec(spec, [source / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(runtime.root_dir).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    binding = runtime.start_agent_session(run_id, task.candidate_id)
    scope = ensure_execution_scope(
        root_dir=runtime.root_dir, agent_harness="codex", owner_session_id=goal.active_session.session_id,
         run_id=run_id,
    )
    registry = build_scope_process_registry(runtime.root_dir)
    scope_id = scope["scope_id"]
    registration = ProcessRegistration(
        register=lambda pid: registry.register(
            scope_id=scope_id, pid=pid,  label="codex-candidate-session-st",
            attachment=ScopeAttachment("search_run", run_id),
        ),
        finish=lambda token: registry.finish(scope_id=scope_id, token=token),
    )
    bridge = McpSessionToolBridge(
        runtime.root_dir, caller_role="candidate", caller_agent_session_id=binding.agent_session_id,
    )
    config = NativeSessionInput(
        agent_harness="codex", process=SessionProcessInput(
            ("codex", "app-server", "--stdio"), task.workspace,
            {**os.environ, ST_ACTIVE_ENV: "native_candidate_mcp_st"},
        ),
        model=st_model_for_host("codex"), reasoning_effort="low",
        instructions=(
            "You are a Goal Plus candidate. Use your dynamically registered Search tools. "
            "Read goal_plus_search_get_agent_context for your contract and use goal_plus_search_run_verifier "
            "after the requested edit. Complete only the bounded task in each message."
        ),
        configuration={"sandbox_mode": "workspace-write"},
    )
    native = None
    results = []
    try:
        native = CodexSession(config, registration, timeout=timeout, tool_bridge=bridge)
        runtime.bind_agent_handle(binding.agent_session_id, {
            "agent_harness": "codex", "external_id": native.native_session_id,
            "metadata": {"transport": "app-server"},
        })
        for value in (1, 2):
            if value == 2:
                continued = runtime.continue_agent_session(binding.agent_session_id)
                assert continued.agent_session_id == binding.agent_session_id
            result = native.run(
                f"Your agent_session_id is {binding.agent_session_id}. "
                f"Read your context, set VALUE = {value} in initial_program.py, "
                "then call goal_plus_search_run_verifier with your run_id, candidate_id, "
                "agent_session_id and a hypothesis. Report its score and stop.",
                timeout=timeout,
            )
            results.append(asdict(result))
            assert result.state == "completed", result
            record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
            assert record.score_report.aggregate_score == value, result.text
            assert record.iterations[-1].agent_session_id == binding.agent_session_id
        assert len(record.iterations) == 2
        assert results[0]["native_session_id"] == results[1]["native_session_id"]
        assert results[0]["invocation_id"] != results[1]["invocation_id"]
    finally:
        if native is not None:
            native.close()
        closed = close_execution_scope(root_dir=runtime.root_dir, scope_id=scope_id, reason="ST finished")
        assert closed["state"] == "closed"
        (st_log_dir / "candidate-mcp.json").write_text(json.dumps({
            "goal_plus_id": goal.goal_plus_id, "run_id": run_id,
            "agent_session_id": binding.agent_session_id, "results": results,
            "scope_state": closed["state"], "production_launcher_verified": False,
        }, indent=2) + "\n")
