from __future__ import annotations

import hashlib
import json

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import GoalPlusHostCommandInvocation


def invocation(action, nonce="1", session="main", **extra):
    return GoalPlusHostCommandInvocation(
        agent_harness="codex", transport="codex_user_prompt_submit", command="$goal-plus",
        action=action, session_id=session, native_entry_id=nonce,
        invocation_id=f"{action}:{nonce}",
        raw_input_sha256=hashlib.sha256(f"{action}:{nonce}".encode()).hexdigest(),
        invoked_at="2026-09-04T00:00:00Z", **extra,
    )


@pytest.fixture
def goal(tmp_path):
    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.start_from_host_command("max_duration_seconds=900 A task", invocation=invocation("start"))
    runtime.activate_session(record.goal_plus_id, {"agent_harness": "codex", "session_id": "main"})
    return runtime, record.goal_plus_id


def event(runtime, goal_id, name, **extra):
    return runtime.handle_host_session_event(
        goal_id, agent_harness="codex", session_id="main", event=name,
        delivery="next_user_turn", interaction="headless", **extra,
    )


def test_new_thought_continues_but_interrupted_chat_does_not(goal):
    runtime, goal_id = goal
    assert event(runtime, goal_id, "user_turn").decision == "continue_after_user"
    before = runtime.status(goal_id)
    event(runtime, goal_id, "interrupt")
    stopped = runtime.status(goal_id)
    assert stopped.control.state == "paused"
    assert stopped.control.reason == "user_interrupt"
    assert stopped.active_session.state == "attached"
    assert event(runtime, goal_id, "user_turn").decision == "wait_for_user"
    assert event(runtime, goal_id, "session_start").decision == "wait_for_user"
    assert runtime.gate(goal_id, "stop", {}).decision == "allow"
    resumed = runtime.control_from_host_command(goal_id, invocation=invocation("resume"))
    assert resumed.control.state == "active"
    assert resumed.goal_revision == before.goal_revision
    assert resumed.outer_deadline_at == before.outer_deadline_at
    assert resumed.next_action == before.next_action
    assert runtime.gate(goal_id, "stop", {}).decision == "block"


def test_control_replay_does_not_unpause_a_later_stop(goal):
    runtime, goal_id = goal
    event(runtime, goal_id, "interrupt")
    request = invocation("resume")
    first = runtime.control_from_host_command(goal_id, invocation=request)
    duplicate = runtime.control_from_host_command(goal_id, invocation=request)
    assert duplicate.control == first.control
    runtime.control_from_host_command(goal_id, invocation=invocation("pause"))
    assert runtime.control_from_host_command(goal_id, invocation=request).control.state == "paused"
    resumed = runtime.control_from_host_command(goal_id, invocation=invocation("resume", "2"))
    assert resumed.control.state == "active"


def test_paused_edit_preserves_pause_and_clear_cannot_resume(goal):
    runtime, goal_id = goal
    runtime.control_from_host_command(goal_id, invocation=invocation("pause"))
    edited = runtime.edit_from_host_command(goal_id, "A revised task", expected_revision=1, invocation=invocation("edit"))
    assert edited.control.state == "paused"
    assert edited.goal_revision == 2
    cleared = runtime.control_from_host_command(goal_id, invocation=invocation("clear"))
    assert cleared.status == "abandoned"
    assert cleared.control.state == "closed"
    assert runtime.status(goal_id).raw_goal == edited.raw_goal
    with pytest.raises(RuntimeError, match="terminal|cleared"):
        runtime.control_from_host_command(goal_id, invocation=invocation("resume"))


def test_blocked_is_resumable_and_keeps_binding(goal):
    runtime, goal_id = goal
    runtime.set_status(goal_id, "blocked", reason="unavailable dependency")
    runtime.settle_nonactive_stop(goal_id, session_id="main")
    assert runtime.status(goal_id).active_session.state == "attached"
    assert event(runtime, goal_id, "user_turn").decision == "wait_for_user"
    resumed = runtime.control_from_host_command(goal_id, invocation=invocation("resume"))
    assert resumed.status == "active"
    assert resumed.next_action is not None


def test_foreign_session_cannot_control_goal(goal):
    runtime, goal_id = goal
    before = runtime.status(goal_id)
    for action in ("pause", "resume", "clear"):
        with pytest.raises(RuntimeError, match="session"):
            runtime.control_from_host_command(goal_id, invocation=invocation(action, session="fork"))
    assert runtime.status(goal_id) == before


def test_legacy_blocked_record_stays_closed(goal):
    runtime, goal_id = goal
    path = runtime.goals_dir / goal_id / "goal.json"
    old = json.loads(path.read_text())
    old.pop("control", None)
    old["status"] = "blocked"
    path.write_text(json.dumps(old))
    loaded = runtime.status(goal_id)
    assert loaded.control.state == "closed"
    with pytest.raises(RuntimeError, match="terminal|cleared"):
        runtime.control_from_host_command(goal_id, invocation=invocation("resume"))


def test_resume_cannot_resolve_needs_user(goal):
    runtime, goal_id = goal
    runtime.set_status(goal_id, "needs_user", reason="choose a deployment target")
    resumed = runtime.control_from_host_command(goal_id, invocation=invocation("resume"))
    assert resumed.status == "needs_user"
    assert runtime.gate(goal_id, "stop", {}).decision == "allow"


def test_native_restore_never_fires_a_turn(goal):
    runtime, goal_id = goal
    result = runtime.handle_host_session_event(goal_id, agent_harness="codex", session_id="main", event="session_start", delivery="auto", interaction="interactive")
    assert result.decision == "wait_for_user"


@pytest.mark.parametrize("context", [{"mode": "plan"}, {"has_pending_user_input": True}, {"main_busy": True}])
def test_continuation_yields_to_host_and_user_input(goal, context):
    runtime, goal_id = goal
    assert runtime.gate(goal_id, "stop", context).decision == "allow"
    assert runtime.status(goal_id).control.state == "active"




def test_automatic_resume_compare_and_set_cannot_override_user_pause(goal):
    runtime, goal_id = goal
    event(runtime, goal_id, "execution_lost")
    stopped = runtime.status(goal_id)
    expected = dict(goal_plus_id=goal_id, goal_revision=1, session_id="main", control_version=stopped.control.version)
    runtime.control_from_host_command(goal_id, invocation=invocation("pause"))
    with pytest.raises(RuntimeError, match="AutomaticResumeRejected"):
        runtime.control_from_host_command(goal_id, invocation=invocation("resume", resume_expectation=expected))
    assert runtime.status(goal_id).control.reason == "user_pause"


def test_owned_host_timeout_is_distinct_from_user_interrupt(goal):
    runtime, goal_id = goal
    expectation = dict(goal_plus_id=goal_id, goal_revision=1, session_id="main", control_version=0)
    stopped = runtime.stop_for_host_retry(goal_id, expectation)
    assert stopped.control.reason == "harness_interruption"
    event(runtime, goal_id, "interrupt")
    assert runtime.status(goal_id).control.reason == "harness_interruption"
    expectation["control_version"] = stopped.control.version
    resumed = runtime.control_from_host_command(goal_id, invocation=invocation("resume", resume_expectation=expectation))
    assert resumed.control.state == "active"
    runtime.control_from_host_command(goal_id, invocation=invocation("pause"))
    expectation["control_version"] = runtime.status(goal_id).control.version
    with pytest.raises(RuntimeError, match="HostInterruptionRejected"):
        runtime.stop_for_host_retry(goal_id, expectation)




def test_automatic_resume_cannot_reinterpret_active_or_unknown_stop(goal):
    runtime, goal_id = goal
    expectation = dict(goal_plus_id=goal_id, goal_revision=1, session_id="main", control_version=0)
    with pytest.raises(RuntimeError, match="AutomaticResumeRejected"):
        runtime.control_from_host_command(goal_id, invocation=invocation("resume", resume_expectation=expectation))
    event(runtime, goal_id, "interrupt")
    expectation["control_version"] = runtime.status(goal_id).control.version
    with pytest.raises(RuntimeError, match="AutomaticResumeRejected"):
        runtime.control_from_host_command(goal_id, invocation=invocation("resume", "2", resume_expectation=expectation))


def test_late_final_check_cannot_revive_cleared_goal(goal):
    runtime, goal_id = goal
    runtime.control_from_host_command(goal_id, invocation=invocation("clear"))
    for verdict in ("pass", "fail", "interrupted"):
        with pytest.raises(RuntimeError, match="terminal or cleared"):
            runtime.submit_final_check(goal_id, "old-check", 1, verdict, "late response")
    assert runtime.status(goal_id).control.state == "closed"


def test_completed_goal_can_only_restart_by_real_edit_in_original_session(goal):
    runtime, goal_id = goal
    runtime.set_status(goal_id, "complete", evidence=[{"result": "done"}])
    runtime.settle_nonactive_stop(goal_id, session_id="main")
    with pytest.raises(RuntimeError):
        runtime.control_from_host_command(goal_id, invocation=invocation("resume"))
    edited = runtime.edit_from_host_command(goal_id, "Another task", expected_revision=1, invocation=invocation("edit"))
    assert edited.goal_revision == 2
    assert edited.control.state == "active"


def test_paused_task_rejects_new_dispatch_but_keeps_history_readable(goal):
    runtime, goal_id = goal
    runtime.control_from_host_command(goal_id, invocation=invocation("pause"))
    with pytest.raises(RuntimeError, match="GoalPlusResumeRequired"):
        runtime.record_work_event(goal_id, "new", "dispatch", "must not start", host="codex")
    assert runtime.gate(goal_id, "pre_tool_use", {"tool_name": "goal_plus_search_report"}).decision == "allow"


def test_main_reconciliation_survives_non_search_round_and_requires_new_evidence(tmp_path):
    from tests.test_search_recovery import make_search
    search, run_id, candidate = make_search(tmp_path)
    runtime = FileGoalPlusRuntime(search.root_dir)
    goal = runtime.create_goal("Do ordinary work, then finish Search")
    runtime.link_search_run(goal.goal_plus_id, search.search_run.state.read_run(run_id).frozen_spec_id, run_id)
    recovery = search.prepare_candidate_recovery(run_id, candidate.candidate_id)
    search.recover_candidate(run_id, candidate.candidate_id, recovery.recovery_id)
    runtime.record_triage(goal.goal_plus_id, {"is_optimization": False, "confidence": "high", "recommended_phase": "goal"})
    for _ in range(2):
        gate = runtime.gate(goal.goal_plus_id, "stop", {})
        assert "start_fresh_worker" in gate.continuation_prompt
        assert recovery.recovery_id in gate.continuation_prompt
    with pytest.raises(RuntimeError, match="SearchRecoveryIncomplete"):
        runtime.set_status(goal.goal_plus_id, "complete")
    worker = search.start_agent_session(run_id, candidate.candidate_id)
    search.bind_agent_handle(worker.agent_session_id, {"external_id": "fresh-worker"})
    assert "await_fresh_evidence" in runtime.reconciliation_context(goal.goal_plus_id)
    with pytest.raises(RuntimeError, match="SearchRecoveryIncomplete"):
        runtime.set_status(goal.goal_plus_id, "complete")
    search.run_verifier(run_id, candidate.candidate_id, agent_session_id=worker.agent_session_id, hypothesis="fresh generation verification")
    assert "continue_search" in runtime.reconciliation_context(goal.goal_plus_id)


def test_reconciliation_prompt_projects_actions_without_checkpoint_or_session_history(tmp_path):
    from tests.test_search_recovery import make_search

    search, run_id, candidate = make_search(tmp_path)
    runtime = FileGoalPlusRuntime(search.root_dir)
    goal = runtime.create_goal("Finish Search")
    assert runtime.reconciliation_context(goal.goal_plus_id) == ""
    runtime.link_search_run(goal.goal_plus_id, search.search_run.state.read_run(run_id).frozen_spec_id, run_id)
    recovery = search.prepare_candidate_recovery(run_id, candidate.candidate_id)
    context = runtime.reconciliation_context(goal.goal_plus_id)
    agenda, _ = json.JSONDecoder().raw_decode(context.split("\n", 1)[1])
    assert agenda == [{"run_id": run_id, "candidates": [{
        "candidate_id": candidate.candidate_id,
        "generation": recovery.generation,
        "recovery_id": recovery.recovery_id,
        "next_step": "stop_old_execution_then_restore",
    }]}]
    # Detailed recovery data remains available on the explicit status read.
    details = runtime.reconciliation(goal.goal_plus_id)["runs"][0]["candidates"][0]
    assert details["checkpoint"]
    assert "sessions" in details
