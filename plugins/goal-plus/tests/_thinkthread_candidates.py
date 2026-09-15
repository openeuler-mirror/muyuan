"""Settled candidate inputs for provider and Annotation tests, without a controller."""

from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.host_bindings import candidate_inner_agent_binding
from tests._runtime_helpers import start_host_verified_goal


def bind_candidate(runtime, client, run_id, candidate_id):
    goals = FileGoalPlusRuntime(runtime.root_dir)
    run = runtime.search_run.state.read_run(run_id)
    if goals.authorization.run_owner(run_id) is None:
        goal = start_host_verified_goal(runtime.root_dir, client.project, agent_harness="pi")
        goals.link_search_run(goal.goal_plus_id, run.frozen_spec_id, run_id)
    session = runtime.start_agent_session(run_id, candidate_id)
    child = client.invoke("thinkthread.spawn", {
        "profile": "self", "fs": "private", "fsSnapshotId": run.source_artifact_ref.id,
        "capabilities": ["thinkthread.message"], "initialMessage": session.launch["message"],
    })
    runtime.candidate_sessions.bind_thinkthread_candidate(
        run_id=run_id, candidate_id=candidate_id, agent_session_id=session.agent_session_id,
        thinkthread_id=child["thinkthreadId"], branch_id=child["fs"]["fsBranchId"],
    )
    client.finish(child["thinkthreadId"])
    return runtime.candidate_sessions.session(session.agent_session_id)


def attempt_parameters(runtime, session, *, iteration=1):
    record = runtime.candidate_creation.state.read_candidate(session.run_id, session.candidate_id)
    run = runtime.search_run.state.read_run(session.run_id)
    frozen = runtime.search_run.state.read_frozen_spec(run.frozen_spec_id)
    inner_git_attempt = None
    if candidate_inner_agent_binding(frozen, record.task).workspace_ledger_projection == "results_tsv":
        inner_git_attempt = {
            "protocol": "goal-plus.inner-git.v1", "adapter": "autoresearch-git",
            "attempt_git_head": f"{iteration:040x}", "baseline_git_head": "b" * 40,
            "result_count_before": iteration - 1,
        }
    return {
        "run_id": session.run_id, "candidate_id": session.candidate_id,
        "agent_session_id": session.agent_session_id,
        "thinkthread_id": session.session_handle.external_id,
        "branch_id": session.session_handle.metadata["fs_branch_id"],
        "hypothesis": f"Verify exact candidate attempt {iteration}",
        "attempt_id": f"attempt-{session.candidate_id}-{iteration}",
        "inner_git_attempt": inner_git_attempt,
    }


def verify_candidate(runtime, client, session, *, iteration=1):
    return runtime.verify_thinkthread_candidate_attempt(
        **attempt_parameters(runtime, session, iteration=iteration), client=client,
    )


def close_candidate(client, session):
    ChildDriver(client, SystemClock()).release(
        session.session_handle.external_id, root_id="tt-root",
        branch_id=session.session_handle.metadata["fs_branch_id"], timeout=1,
    )
