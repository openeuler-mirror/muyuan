"""Native MCP wait/settlement contract with one worker and a deterministic controller."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope, register_execution_process, finish_execution_process
from goal_plus.infrastructure.state.resource_locks import verifier_resource_lock
from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal
from tests.st.helpers.codex_runner import CodexRunner
from tests.st.hosts import ST_ACTIVE_ENV

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.st, pytest.mark.st_codex, pytest.mark.host_process]


def _events(path):
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # The native process may still be writing the last event.
    return events


def _tool_results(path, tool):
    return [event["item"] for event in _events(path)
            if event.get("type") == "item.completed"
            and event.get("item", {}).get("type") == "mcp_tool_call"
            and event["item"].get("tool") == tool]


def _response(item):
    result = item.get("result")
    if not isinstance(result, dict):
        return {}
    try:
        return json.loads(result["content"][0]["text"])
    except json.JSONDecodeError:
        return {}  # Deliberately rejected duplicate submissions are plain-text errors.


def _wait(process, condition, log_path, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        assert process.poll() is None, f"worker exited; see {log_path}"
        time.sleep(0.2)
    pytest.fail(f"native verifier ST timed out; see {log_path}")


def test_codex_pending_verifier_snapshot_and_recovery(st_project_root, st_log_dir, monkeypatch):
    query_wait_seconds = 30
    client_timeout = query_wait_seconds * 2
    host_overhead_seconds = 180
    deadline_seconds = client_timeout + host_overhead_seconds
    monkeypatch.setenv("GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR", str(st_project_root / "locks"))
    project = make_project(st_project_root)
    (project / "evaluator.py").write_text(
        "import json\nfrom initial_program import VALUE\n"
        "print(json.dumps({'combined_score': VALUE}))\n"
    )
    runtime = FileSearchRuntime(st_project_root / ".gp")
    goal = start_host_verified_goal(runtime.root_dir, project)
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["strategy"].update({"inner_agent": "common", "worker_budget": {
        "max_runtime_seconds": deadline_seconds,
    }})
    data["workspace"]["promotion_mode"] = "artifact_only"
    data["process_verifiers"][0].update({
        "resource_lock": "native-verifier-wait", "timeout_seconds": client_timeout * 2,
    })
    frozen = runtime.freeze_spec(SearchSpec.model_validate(data), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(runtime.root_dir).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task, = runtime.start_batch(run_id, plan.plan_id)
    session = runtime.start_agent_session(run_id, task.candidate_id)
    scope = ensure_execution_scope(root_dir=runtime.root_dir, agent_harness="codex", owner_session_id="verifier-st", run_id=run_id)
    runner = CodexRunner(task.workspace, st_log_dir)
    command = runner._build_base_cmd() + ["--json", "--disable", "plugins", "--disable", "hooks"]
    # Only the module's MCP endpoint is under test; no installed plugin or Main lifecycle.
    mcp = {
        "command": sys.executable,
        "args": ["-m", "goal_plus.server", "--root", str(runtime.root_dir)],
        "tool_timeout_sec": client_timeout,
        "env.PYTHONPATH": str(ROOT / "src"),
        "env.GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED": "1",
        "env.GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR": str(st_project_root / "locks"),
        "env.GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID": session.agent_session_id,
        "env.GOAL_PLUS_SEARCH_ROOT": str(runtime.root_dir),
        "enabled": True,
    }
    for key, value in mcp.items():
        command += ["-c", f"mcp_servers.goal-plus.{key}={json.dumps(value)}"]
    command.append(
        f"run_id={run_id}, candidate_id={task.candidate_id}, agent_session_id={session.agent_session_id}.\n"
        "This is a controlled verifier module ST, not an optimization task. "
        "Call goal_plus_search_get_agent_context, change initial_program.py to VALUE = 1, then call "
        "goal_plus_search_run_verifier with the current session and hypothesis 'submitted value one'. "
        "The controller deliberately holds the verifier resource lock. Wait for the tool response; "
        "do not cancel it. On pending, make exactly one duplicate "
        "goal_plus_search_run_verifier call to check VerifierInProgress. This duplicate is an intentional "
        "negative test, not a new attempt. Then call goal_plus_search_run_verifier with that attempt_id, "
        f"the current run/candidate/session IDs and timeout_seconds={query_wait_seconds}. Repeat wait while pending, "
        "until the result has verification.state=needs_recovery. Report that state and stop. "
        "After submitting, do not edit any file, commit, restore, launch another agent, invoke nested "
        "CLIs, or change runtime/host configuration. Use native goal-plus MCP tools, not a shell client."
    )
    log_path = st_log_dir / f"native-verifier-wait-{run_id}.jsonl"
    print(f"\nNative verifier ST log: {log_path}", flush=True)
    env = {**os.environ, ST_ACTIVE_ENV: "codex_verifier_wait"}
    with log_path.open("w") as log, ExitStack() as resources:
        resources.enter_context(verifier_resource_lock("native-verifier-wait"))
        process = subprocess.Popen(command, cwd=task.workspace, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            thread = _wait(process, lambda: next((event["thread_id"] for event in _events(log_path)
                           if event.get("type") == "thread.started"), None), log_path, host_overhead_seconds)
            session = runtime.bind_agent_handle(session.agent_session_id, {
                "external_id": thread, "metadata": {"transport": "app-server", "scope_id": scope["scope_id"]},
            })
            token = register_execution_process(root_dir=runtime.root_dir, scope_id=scope["scope_id"],
                                               pid=process.pid, label=f"native-session:{session.agent_session_id}", run_id=run_id)
            pending = _wait(process, lambda: runtime.candidate_creation.state.read_candidate(
                run_id, task.candidate_id).verification, log_path, host_overhead_seconds)
            submitted_at = time.monotonic()
            responses = _wait(process, lambda: _tool_results(log_path, "goal_plus_search_run_verifier"),
                              log_path, deadline_seconds)
            response = _response(responses[0])
            assert response["state"] == "pending"
            assert response["candidate_action"] == "wait_for_verifier"
            assert response["verification"]["attempt_id"] == pending.attempt_id
            assert "aggregate_score" not in response
            responses = _wait(process, lambda: (items if len(items := _tool_results(
                log_path, "goal_plus_search_run_verifier")) >= 2 else None), log_path, host_overhead_seconds)
            assert "VerifierInProgress" in json.dumps(responses[1])
            assert runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id).iterations == []
            # Keep the submitted attempt queued beyond the real client's timeout.
            while time.monotonic() - submitted_at <= client_timeout:
                assert process.poll() is None, log_path
                time.sleep(0.2)
            assert any(_response(item).get("attempt_id") == pending.attempt_id
                       for item in _tool_results(log_path, "goal_plus_search_run_verifier"))
            (task.workspace / "initial_program.py").write_text("VALUE = 2\n")
            resources.close()
            _wait(process, lambda: any(_response(item).get("verification", {}).get("state") == "needs_recovery"
                  for item in _tool_results(log_path, "goal_plus_search_run_verifier")
                  if _response(item).get("verification")), log_path, host_overhead_seconds)
            process.wait(timeout=host_overhead_seconds)
            assert process.returncode == 0, log_path
            finish_execution_process(root_dir=runtime.root_dir, scope_id=scope["scope_id"], token=token)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)

    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    iteration, = record.iterations
    assert iteration.score == 1 and iteration.process_passed is True
    assert iteration.artifact_ref == pending.artifact_ref
    assert (task.workspace / "initial_program.py").read_text() == "VALUE = 2\n"
    assert record.verification.state == "needs_recovery"
    SearchTools(runtime).goal_plus_search_run_verifier(
        run_id, task.candidate_id, attempt_id=pending.attempt_id, retry=True,
    )
    restored = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert restored.verification is None
    assert restored.iterations == record.iterations
    assert restored.recovery is None
    assert (task.workspace / "initial_program.py").read_text() == "VALUE = 2\n"
