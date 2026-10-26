"""Product candidate launcher through MCP; Main orchestration is tested separately."""

import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope, execution_scope_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.ports.scope import SCOPE_PROCESS_TERMINAL_STATUSES
from goal_plus.server import create_host_mcp
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal
from tests.st.hosts import st_model_for_host


@pytest.mark.st
@pytest.mark.parametrize("recover", [False, True, "timeout_resume", "host_restart"],
                         ids=["same_session", "cold_recovery", "timeout_resume", "host_restart"])
@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_product_session_candidate_two_turns(tmp_path, st_log_dir, agent_harness, recover, monkeypatch, request):
    pi_config = (Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
                 if recover == "timeout_resume" else request.getfixturevalue("isolated_pi_config"))
    native_hook_events = []
    if agent_harness == "codex":
        request.getfixturevalue("installed_codex_config")
        from goal_plus.execution.native_session import CodexSession

        original_event = CodexSession._event

        def observe(self, event):
            if "hook" in str(event.get("method", "")).lower():
                native_hook_events.append(event)
            original_event(self, event)

        monkeypatch.setattr(CodexSession, "_event", observe)
    timeout = float(os.environ.get("ST_SESSION_PROBE_TIMEOUT", "180"))
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\nfrom pathlib import Path\nv = {}\n"
        "exec(Path('initial_program.py').read_text(), v)\n"
        "print(json.dumps({'combined_score': float(v['VALUE'])}))\n"
    )
    root = tmp_path / ".gp"
    runtime = FileSearchRuntime(root)
    goal = start_host_verified_goal(root, project, agent_harness=agent_harness)
    spec = spec_for(project, max_parallel=1)
    spec.strategy.inner_agent = "common"
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    model = st_model_for_host("codex" if agent_harness == "codex" else "pi")
    if agent_harness == "pi":
        checkout = Path(__file__).resolve().parents[2]
        monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(checkout))
        monkeypatch.setenv("GOAL_PLUS_PYTHON", str(checkout / ".goal-plus-dev/venv/bin/python"))
        if not model:
            settings = json.loads((pi_config / "settings.json").read_text())
            model = settings["defaultProvider"] + "/" + settings["defaultModel"]
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
         run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": model, "reasoning_effort": "low"} if agent_harness == "codex"
        else {"model_pattern": model, "thinking_level": "low"},
    )
    results = []
    interrupted_calls = []
    context_resumed = False
    memory_token = uuid4().hex
    mcp = create_host_mcp(root, agent_harness=agent_harness, caller_role="main")

    async def exercise():
        nonlocal context_resumed
        active = session
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(Client(mcp, timeout=timeout + 60))
            async def call(action, args):
                result = await client.call_tool(
                    "goal_plus_session_" + action, args,
                    meta={"threadId": goal.active_session.session_id},
                )
                return json.loads(result.content[0].text)

            for value in (1, 2):
                if value == 2 and recover is True:
                    before = record.model_copy(deep=True)
                    (task.workspace / "initial_program.py").write_text("VALUE = 99 # interrupted attempt\n")
                    await stack.aclose()
                    client = await stack.enter_async_context(Client(
                        create_host_mcp(root, agent_harness=agent_harness, caller_role="main"), timeout=timeout + 60,
                    ))
                    for action in ("goal_plus_search_prepare_recovery", "goal_plus_search_recover_candidate"):
                        args = {"run_id": run_id, "candidate_id": task.candidate_id}
                        if action == "goal_plus_search_recover_candidate":
                            args["recovery_id"] = prepared["recovery_id"]
                        response = await client.call_tool(action, args, meta={"threadId": goal.active_session.session_id})
                        prepared = json.loads(response.content[0].text)
                    assert (task.workspace / "initial_program.py").read_text().strip() == "VALUE = 1"
                    active = runtime.start_agent_session(run_id, task.candidate_id)
                    assert active.execution_generation == session.execution_generation + 1
                elif value == 2 and recover == "timeout_resume":
                    before = record.model_copy(deep=True)
                    await call("run", {
                        "run_id": run_id, "candidate_id": task.candidate_id, "call_id": "timeout-probe",
                        "timeout_seconds": 0.1, "prompt": "Run sleep 60, then report that the wait ended.",
                    })
                    interrupted = (await call("wait_any", {
                        "calls": [{"session_id": active.agent_session_id, "call_id": "timeout-probe"}],
                        "timeout_seconds": 30,
                    }))["finished"][0]
                    interrupted_calls.append(interrupted)
                    assert interrupted["state"] == "failed" and "TimeoutError" in interrupted["error"]
                    assert runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id) == before
                elif value == 2 and recover == "host_restart":
                    before = record.model_copy(deep=True)
                    ready = task.workspace / ".tmp/local-ready"
                    await call("run", {
                        "run_id": run_id, "candidate_id": task.candidate_id, "call_id": "local-probe",
                        "timeout_seconds": timeout,
                        "prompt": "Set VALUE = 2 in initial_program.py. Test it locally with Python. "
                                  "Do not call the verifier or commit yet. After the local test passes, "
                                  "write .tmp/local-ready containing the result, then run sleep 120.",
                    })
                    async with asyncio.timeout(timeout):
                        while not ready.exists():
                            await asyncio.sleep(1)
                    local_result = ready.read_text()
                    async with Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")) as competing:
                        with pytest.raises(ToolError, match="SessionExecutionActive"):
                            await competing.call_tool("goal_plus_session_run", {
                                "run_id": run_id, "candidate_id": task.candidate_id, "call_id": "competing",
                                "prompt": "Continue", "timeout_seconds": timeout,
                            }, meta={"threadId": goal.active_session.session_id})
                    await stack.aclose()
                    assert runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id).iterations == before.iterations
                    assert (task.workspace / "initial_program.py").read_text().strip() == "VALUE = 2"
                    client = await stack.enter_async_context(Client(
                        create_host_mcp(root, agent_harness=agent_harness, caller_role="main"), timeout=timeout + 60,
                    ))
                    assert ready.read_text() == local_result
                    with pytest.raises(ToolError, match="SessionCallHistoryUnavailable"):
                        await call("wait_any", {
                            "calls": [{"session_id": active.agent_session_id, "call_id": "local-probe"}],
                            "timeout_seconds": 0,
                        })
                prompt = f"Set VALUE = {value} in initial_program.py, run your verifier, report the score and stop."
                if recover == "host_restart" and value == 2:
                    prompt = "Inspect the preserved local result in .tmp/local-ready and existing code, run your verifier, report the score and stop."
                if recover in {"timeout_resume", "host_restart"}:
                    prompt += (f" Remember this conversation-only token: {memory_token}. Do not write it to files."
                               if value == 1 else " Include the conversation-only token from our first turn in your reply.")
                opened = await call("run", {
                    "run_id": run_id, "candidate_id": task.candidate_id, "call_id": str(value),
                    "timeout_seconds": timeout,
                    "prompt": prompt,
                })
                result = (await call("wait_any", {
                    "calls": [{"session_id": active.agent_session_id, "call_id": str(value)}],
                    "timeout_seconds": timeout + 30,
                }))["finished"][0]
                results.append(result)
                assert result["state"] == "completed", result.get("error") or result
                assert result["native_session_id"] == opened["native_session_id"]
                record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
                assert record.score_report.aggregate_score == value
                assert record.iterations[-1].agent_session_id == active.agent_session_id
                if value == 2 and recover:
                    assert record.iterations[0] == before.iterations[0]
                    assert record.results_ledger[0] == before.results_ledger[0]
                    assert not (task.workspace / "results.tsv").exists()
                    if recover in {"timeout_resume", "host_restart"}:
                        assert record.execution_generation == before.execution_generation
                        assert results[0]["native_session_id"] == result["native_session_id"]
                        detailed = (await call("wait_any", {
                            "calls": [{"session_id": active.agent_session_id, "call_id": "2"}], "detail": True,
                        }))["finished"][0]
                        assert memory_token in detailed["result"]["text"]
                        context_resumed = True
            assert len(record.iterations) == 2
            await call("close", {"session_id": active.agent_session_id})

    print(f"Product {agent_harness} session launcher evidence: {st_log_dir}", flush=True)
    try:
        asyncio.run(exercise())
    finally:
        closed = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="ST finished")
        snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
        hook_events = [json.loads(path.read_text()) for path in (root / "host-logs/codex-hook-events").glob("*.json")]
        (st_log_dir / "product-session.json").write_text(json.dumps({
            "agent_harness": agent_harness, "model": model, "results": results, "scope_state": closed["state"],
            "interrupted_calls": interrupted_calls,
            "context_resumed": context_resumed,
            "production_launcher_verified": len(results) == 2 and all(item["state"] == "completed" for item in results),
            "native_main_orchestration_verified": False,
            "agent_session_id": session.agent_session_id,
            "hook_events": hook_events,
            "native_hook_events": native_hook_events,
            "cleanup": snapshot["cleanup"],
            "processes": [{key: item.get(key) for key in ("pid", "label", "status")} for item in snapshot["processes"]],
        }, indent=2) + "\n")
        assert closed["state"] == "closed"
    snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
    assert snapshot["processes"] and all(item["status"] in SCOPE_PROCESS_TERMINAL_STATUSES for item in snapshot["processes"])
    candidates = [item for item in snapshot["processes"] if item["kind"] == "candidate"]
    assert candidates and all(item["status"] == "finished" for item in candidates)
    if recover in {"timeout_resume", "host_restart"}:
        assert len(candidates) == 2 and candidates[0]["pid"] != candidates[1]["pid"]
    assert snapshot["cleanup"]["active_count"] == 0
    assert not snapshot["cleanup"]["errors"]
    if agent_harness == "codex":
        bound = [event for event in hook_events if event["agent_session_id"] == session.agent_session_id]
        assert len(bound) >= (1 if recover else 2), "installed Stop hooks must observe the exact native candidate binding"
        assert all(event["session_id"] == results[0]["native_session_id"] for event in bound)
