"""TT Message requests enter the same role-bound MCP business tools."""

from copy import deepcopy
import hashlib
import json

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.thinkthread_worker_rpc import dispatch_worker_tool
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal
from tests._thinkthread_sdk import IntegratedAgentPosix, thinkthread_spec


def test_session_control_ack_does_not_enter_candidate_tool_admission(tmp_path):
    from goal_plus.infrastructure.thinkthread_worker_rpc import _process_worker_message
    from goal_plus.execution.thinkthread_rpc import PROTOCOL

    record = {"thinkthread_id": "tt-child"}
    _process_worker_message(None, tmp_path, record, {
        "text": json.dumps({"protocol": PROTOCOL, "type": "session_message_ack", "request_id": "r"}),
        "truncated": False,
    }, lambda: pytest.fail("an independent control ack must not mutate candidate RPC state"))
    assert record == {"thinkthread_id": "tt-child"}


@pytest.fixture
def bound_worker(tmp_path, monkeypatch):
    def create(*, inner_agent="common"):
        monkeypatch.chdir(tmp_path)
        project = make_project(tmp_path)
        client = IntegratedAgentPosix(project)
        monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
        root = tmp_path.parent / (tmp_path.name + "-session-runtime")
        goal = start_host_verified_goal(root, project, agent_harness="pi")
        runtime = FileSearchRuntime(root)
        spec = thinkthread_spec(project)
        spec.strategy.inner_agent = inner_agent
        frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi")
        run_id = runtime.create_run(frozen.frozen_spec_id)
        FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
        plan = runtime.plan_next(run_id, requested_k=1)
        task = runtime.start_batch(run_id, plan.plan_id)[0]
        session = runtime.start_agent_session(run_id, task.candidate_id)
        runtime.candidate_sessions.bind_thinkthread_candidate(
            run_id=run_id, candidate_id=task.candidate_id, agent_session_id=session.agent_session_id,
            thinkthread_id="tt-bound-child", branch_id="fsbranch-bound",
        )
        job = {
            "agent_session_id": session.agent_session_id, "run_id": run_id,
            "candidate_id": task.candidate_id, "dispatch_index": 1,
            "thinkthread_id": "tt-bound-child", "fs_branch_id": "fsbranch-bound",
        }
        params = {"hypothesis": "Try one change"}
        return runtime, goal, job, params

    return create


def test_worker_proxy_accepts_only_one_stable_attempt_per_turn(bound_worker):
    runtime, _, job, params = bound_worker()
    accepted = dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="req-one")
    replayed = dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="req-one")
    assert accepted == replayed
    assert accepted["state"] == "accepted"
    assert accepted["attempt_id"].startswith("attempt_")
    assert "toolization_decision" not in accepted
    assert runtime.list_iterations(job["run_id"], job["candidate_id"]) == []
    with pytest.raises(RuntimeError, match="reused with new content"):
        dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier",
                              {**params, "hypothesis": "Different content"}, request_id="req-one")
    with pytest.raises(RuntimeError, match="only one candidate attempt"):
        dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="req-two")


def test_worker_proxy_checks_toolization_before_accepting_attempt(bound_worker):
    runtime, _, job, params = bound_worker(inner_agent="autoresearch")
    params.update({
        "_inner_git_attempt": {
            "protocol": "goal-plus.inner-git.v1", "adapter": "autoresearch-git",
            "attempt_git_head": "a" * 40, "baseline_git_head": "b" * 40, "result_count_before": 0,
        },
        "toolization_decision": {
            "outcome": "staged", "signals": ["behavior_or_invariant_checker"],
            "rationale": "Reusable checker", "tool_names": ["checker"], "exclusion": None,
        },
    })
    before_job = deepcopy(job)
    before_run = runtime.search_run.state.read_run(job["run_id"])
    with pytest.raises(RuntimeError, match="Unexpected keyword argument"):
        dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="bad")
    assert job == before_job
    assert runtime.search_run.state.read_run(job["run_id"]) == before_run
    params.pop("toolization_decision")
    accepted = dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="corrected")
    assert accepted["state"] == "accepted"
    assert job["inner_git_attempt"] == params["_inner_git_attempt"]
    assert "toolization_decision" not in accepted
    assert runtime.search_run.state.read_run(job["run_id"]) == before_run


@pytest.mark.parametrize("mismatch", ["child", "branch", "session", "generation", "promotion"])
def test_deferred_attempt_rejects_invalid_binding_without_acceptance(bound_worker, mismatch):
    runtime, goal, job, params = bound_worker()
    if mismatch == "child":
        job["thinkthread_id"] = "tt-foreign"
    elif mismatch == "branch":
        job["fs_branch_id"] = "fsbranch-foreign"
    elif mismatch == "session":
        params["agent_session_id"] = "agent-foreign"
    elif mismatch == "generation":
        record = runtime.thinkthread_verification.state.read_candidate(job["run_id"], job["candidate_id"])
        record.execution_generation += 1
        runtime.thinkthread_verification.state.write_candidate(job["run_id"], record)
    else:
        params["scope"] = "promotion"
    before_job = deepcopy(job)
    before = {str(path): path.read_bytes() for path in runtime.root_dir.rglob("*.json")}
    with pytest.raises(RuntimeError):
        dispatch_worker_tool(runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="invalid")
    assert job == before_job
    assert {str(path): path.read_bytes() for path in runtime.root_dir.rglob("*.json")} == before


def _pause_bound_goal(runtime, goal):
    FileGoalPlusRuntime(runtime.root_dir).control_from_host_command(goal.goal_plus_id, invocation={
        "agent_harness": "pi", "transport": "pi_extension_command", "command": "/goal-plus",
        "action": "pause", "session_id": "session-test", "native_entry_id": "turn-pause",
        "invocation_id": "pi:test:pause", "invoked_at": goal.created_at,
        "raw_input_sha256": hashlib.sha256(b"/goal-plus pause").hexdigest(),
    })


def test_deferred_attempt_settles_while_goal_paused(bound_worker):
    runtime, goal, job, params = bound_worker()
    _pause_bound_goal(runtime, goal)
    # Worker settlement: a paused Goal still accepts verification of work
    # that already ran; candidate settlement owns the resulting Evidence.
    accepted = dispatch_worker_tool(
        runtime.root_dir, job, "goal_plus_search_run_verifier", params, request_id="paused-settle",
    )
    assert accepted["state"] == "accepted"
    assert accepted["attempt_id"].startswith("attempt_")


def test_worker_message_uses_common_context_and_rejects_peer_and_main_calls(tmp_path):
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileSearchRuntime(root)
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=2), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    own, peer = [runtime.start_agent_session(run_id, task.candidate_id)
                 for task in runtime.start_batch(run_id, plan.plan_id)]
    job = {"agent_session_id": own.agent_session_id, "run_id": run_id, "candidate_id": own.candidate_id}

    result = dispatch_worker_tool(root, job, "goal_plus_search_get_agent_context",
                                  {}, request_id="context")
    assert result["agent_session_id"] == own.agent_session_id
    assert result["candidate_task"]["workspace"] == str(own.workspace)
    before = deepcopy(job)
    before_run = runtime.search_run.state.read_run(run_id)
    with pytest.raises(RuntimeError, match="Unexpected keyword argument"):
        dispatch_worker_tool(root, job, "goal_plus_search_get_agent_context",
                              {"agent_session_id": peer.agent_session_id}, request_id="peer")
    with pytest.raises(RuntimeError, match="(Unknown|unknown|not found)"):
        dispatch_worker_tool(root, job, "goal_plus_search_select", {"run_id": run_id}, request_id="main")
    assert job == before
    assert runtime.search_run.state.read_run(run_id) == before_run

    # A Message envelope does not turn a local session into an admitted TT Child.
    with pytest.raises(RuntimeError, match="ThinkThread"):
        dispatch_worker_tool(root, job, "goal_plus_search_run_verifier", {
            "hypothesis": "One candidate change",
        }, request_id="invalid-workspace_provider")
    assert job == before
    assert runtime.search_run.state.read_run(run_id) == before_run


def test_invalid_dispatch_message_ack_cannot_acknowledge_a_wake(tmp_path):
    import json
    from goal_plus.infrastructure.thinkthread_worker_rpc import _process_worker_message
    from goal_plus.execution.thinkthread_rpc import PROTOCOL

    job = {"dispatch_nonce": "current", "dispatch_nonces": ["current"],
           "active_message_id": "msg-exact"}
    message = {"text": json.dumps({"protocol": PROTOCOL, "type": "dispatch_ack",
               "dispatch_nonce": "current", "message_id": "msg-foreign"})}
    with pytest.raises(PermissionError, match="sent Message"):
        _process_worker_message(None, tmp_path, job, message, lambda: None)
    assert "current" not in job.get("acknowledged_dispatch_nonces", [])
    assert "current" not in job.get("acknowledged_messages", {})

def test_shared_tool_message_reaches_common_facade_with_exact_snapshot(tmp_path, monkeypatch):
    from goal_plus.tools import SearchTools

    calls = []

    def copy_tool(self, agent_session_id, tool_id, snapshot_hash):
        calls.append((agent_session_id, tool_id, snapshot_hash))
        return {"tool_id": tool_id, "copied": True}

    monkeypatch.setattr(SearchTools, "goal_plus_search_copy_shared_tool", copy_tool)
    job = {"agent_session_id": "bound-session"}
    result = dispatch_worker_tool(
        tmp_path, job, "goal_plus_search_copy_shared_tool",
        {"tool_id": "tool-one", "snapshot_hash": "a" * 64}, request_id="copy",
    )
    assert result == {"tool_id": "tool-one", "copied": True}
    assert calls == [("bound-session", "tool-one", "a" * 64)]
