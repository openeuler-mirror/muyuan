from __future__ import annotations

from pathlib import Path
import os
import tarfile
from dataclasses import replace

import pytest

from goal_plus.runtime import FileSearchRuntime
from goal_plus.host_recovery import approve_recovery, observe_worker
from goal_plus.tools import SearchTools
from goal_plus.models import ArtifactRef, RunState
from goal_plus.workspaces import get_workspace_provider, UnsupportedWorkspaceCapability
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


def make_search(tmp_path: Path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    return runtime, run_id, task


@pytest.mark.parametrize("tool", [
    "search_recovery_status", "search_prepare_recovery", "search_recover_candidate",
])
@pytest.mark.parametrize("old_revision", [False, True])
def test_recovery_facade_requires_current_goal_run_before_side_effects(tmp_path, tool, old_revision):
    from goal_plus.goal_plus import FileGoalPlusRuntime

    runtime, run_id, task = make_search(tmp_path)
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    start_host_verified_goal(runtime.root_dir, tmp_path / "project")
    goals = FileGoalPlusRuntime(runtime.root_dir)
    goal = goals.require_host_verified_active_goal()
    if old_revision:
        goals.link_search_run(goal.goal_plus_id, runtime._load_run(run_id).frozen_spec_id, run_id)
        goal = goals.update_goal(goal.goal_plus_id, "Revised task", expected_revision=1)
    tools = SearchTools(runtime, goal_runtime=goals)
    args = {"run_id": run_id}
    if tool != "search_recovery_status":
        args["candidate_id"] = task.candidate_id
    if tool == "search_recover_candidate":
        args["recovery_id"] = recovery.recovery_id
    before = runtime._load_candidate_record(run_id, task.candidate_id)
    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        getattr(tools, tool)(**args)
    assert runtime._load_candidate_record(run_id, task.candidate_id) == before
    assert goals.status(goal.goal_plus_id) == goal
    assert not list((runtime.root_dir / "host-recovery").rglob("*.json"))

    if not old_revision:
        goals.link_search_run(goal.goal_plus_id, runtime._load_run(run_id).frozen_spec_id, run_id)
        assert getattr(tools, tool)(**args)


def test_recovery_status_exposes_unsettled_workspace_without_mutation(tmp_path):
    runtime, run_id, task = make_search(tmp_path)
    before = runtime.search_recovery_status(run_id)["candidates"][0]
    assert before["unsettled_workspace_changes"] is False
    (task.workspace / "initial_program.py").write_text("VALUE = 9 # unsettled\n")
    changed = runtime.search_recovery_status(run_id)["candidates"][0]
    assert changed["unsettled_workspace_changes"] is True
    assert changed["checkpoint"] == before["checkpoint"]
    assert changed["recovery"] is None


def test_recovery_cannot_launch_worker_after_original_deadline(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    monkeypatch.setattr(runtime, "_outer_deadline_value", lambda _: "2000-01-01T00:00:00Z")
    with pytest.raises(RuntimeError, match="OuterDeadlineExceeded"):
        runtime.start_agent_session(run_id, task.candidate_id)
    assert not runtime._load_agent_sessions(run_id)


def test_restored_or_main_only_evidence_cannot_bypass_fresh_worker_closeout(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    runtime.run_verifier(run_id, task.candidate_id)
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    with pytest.raises(RuntimeError, match="SearchRecoveryIncomplete"):
        runtime.select(run_id)
    runtime.run_verifier(run_id, task.candidate_id)  # Main is not a recovered worker.
    assert runtime.search_recovery_status(run_id)["candidates"][0]["fresh_recovery_evidence"] is False
    with pytest.raises(RuntimeError, match="SearchRecoveryIncomplete"):
        runtime.select(run_id)
    worker = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(worker.agent_session_id, {"external_id": "fresh"})
    runtime.run_verifier(run_id, task.candidate_id, agent_session_id=worker.agent_session_id, hypothesis="fresh worker proof")
    assert runtime.search_recovery_status(run_id)["candidates"][0]["fresh_recovery_evidence"] is True
    runtime.select(run_id)


def test_finalization_can_select_existing_settlement_without_launching_worker(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    original = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(original.agent_session_id, {"external_id": "original"})
    observe_worker(runtime.root_dir, original, os.getpid())
    runtime.run_verifier(run_id, task.candidate_id, agent_session_id=original.agent_session_id, hypothesis="original worker evidence")
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    monkeypatch.setattr("goal_plus.host_recovery.process_identity", lambda _: None)
    SearchTools(runtime).search_recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    monkeypatch.setattr(runtime, "_outer_deadline_value", lambda _: "2000-01-01T00:00:00Z")
    runtime.select(run_id)
    assert len(runtime._load_agent_sessions(run_id)) == 1  # No new worker after cutoff.


def test_recover_initial_checkpoint_without_creating_another_candidate(tmp_path):
    runtime, run_id, task = make_search(tmp_path)
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.pi_tool import call_pi_tool

    start_host_verified_goal(runtime.root_dir, tmp_path / "project")
    goals = FileGoalPlusRuntime(runtime.root_dir)
    goal = goals.require_host_verified_active_goal()
    goals.link_search_run(goal.goal_plus_id, runtime._load_run(run_id).frozen_spec_id, run_id)
    snapshot = call_pi_tool(runtime.root_dir, "search_recovery_status", {"run_id": run_id})
    assert snapshot["run_id"] == run_id
    assert snapshot["candidates"][0]["checkpoint"]["iteration"] == 0
    original = (task.workspace / "initial_program.py").read_text()
    (task.workspace / "initial_program.py").write_text("broken partial attempt\n")
    prepared = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    assert prepared.point.iteration == 0
    restored = runtime.recover_candidate(run_id, task.candidate_id, prepared.recovery_id)
    assert restored.state == "restored"
    assert (task.workspace / "initial_program.py").read_text() == original
    assert runtime.status(run_id).candidates_total == 1
    assert runtime.recover_candidate(run_id, task.candidate_id, prepared.recovery_id) == restored


def test_settled_point_restores_code_and_ledger_archiving_partial_files(tmp_path):
    runtime, run_id, task = make_search(tmp_path)
    runtime.run_verifier(run_id, task.candidate_id)
    before = runtime._load_candidate_record(run_id, task.candidate_id)
    code = (task.workspace / "initial_program.py").read_text()
    ledger = (task.workspace / "results.tsv").read_text()
    (task.workspace / "initial_program.py").write_text("partial second iteration")
    (task.workspace / "results.tsv").write_text("partial ledger write")
    (task.workspace / "untracked.tmp").write_text("save me")
    (task.workspace / ".tmp").mkdir(exist_ok=True)
    (task.workspace / ".tmp" / "handoff.json").write_text('{"stale": true}')
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    assert recovery.point.artifact_ref == before.iterations[-1].workspace_artifact_after_settlement
    runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    assert (task.workspace / "initial_program.py").read_text() == code
    assert (task.workspace / "results.tsv").read_text() == ledger
    assert not (task.workspace / "untracked.tmp").exists()
    assert not (task.workspace / ".tmp" / "handoff.json").exists()
    after = runtime._load_candidate_record(run_id, task.candidate_id)
    assert after.iterations == before.iterations
    assert after.results_ledger == before.results_ledger
    archive = runtime._candidate_dir(run_id, task.candidate_id) / "recoveries" / recovery.recovery_id / "interrupted-workspace.tar"
    with tarfile.open(archive) as backup:
        assert backup.extractfile("untracked.tmp").read() == b"save me"


def test_prepare_fences_old_sessions_and_requires_host_quiescence(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    session = runtime.start_agent_session(run_id, task.candidate_id)
    observe_worker(runtime.root_dir, session, os.getpid())
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    assert runtime.prepare_candidate_recovery(run_id, task.candidate_id) == recovery
    for call in (
        lambda: runtime.get_agent_context(session.agent_session_id),
        lambda: runtime.continue_agent_session(session.agent_session_id),
        lambda: runtime.run_verifier(run_id, task.candidate_id, agent_session_id=session.agent_session_id, hypothesis="stale"),
        lambda: runtime.start_agent_session(run_id, task.candidate_id),
        lambda: runtime.select(run_id),
    ):
        with pytest.raises(RuntimeError, match="StaleAgentSession|CandidateRecoveryRequired"):
            call()
    tools = SearchTools(runtime)
    with pytest.raises(RuntimeError, match="RecoveryWorkerStillRunning"):
        tools.search_recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    with pytest.raises(RuntimeError, match="RecoveryHostQuiescenceRequired"):
        runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    monkeypatch.setattr("goal_plus.host_recovery.process_identity", lambda _: None)
    tools.search_recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    fresh = runtime.start_agent_session(run_id, task.candidate_id)
    assert fresh.execution_generation == 1
    assert fresh.agent_session_id != session.agent_session_id
    assert runtime.start_agent_session(run_id, task.candidate_id) == fresh
    assert runtime.get_agent_context(fresh.agent_session_id)["resume"]["recovery"]["state"] == "restored"
    with pytest.raises(RuntimeError, match="StaleAgentSession"):
        runtime.get_agent_context(session.agent_session_id)


def test_missing_host_observation_is_not_treated_as_exit(tmp_path):
    runtime, run_id, task = make_search(tmp_path)
    runtime.start_agent_session(run_id, task.candidate_id)
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    with pytest.raises(RuntimeError, match="RecoveryHostObservationMissing"):
        approve_recovery(runtime.root_dir, recovery)


def test_recovery_retry_after_provider_restored_but_record_write_failed(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    expected = (task.workspace / "initial_program.py").read_text()
    (task.workspace / "initial_program.py").write_text("unfinished")
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    original_write = runtime._write_candidate_record

    def fail_last_write(run_id, record):
        if record.recovery and record.recovery.state == "restored":
            raise OSError("simulated crash")
        original_write(run_id, record)

    monkeypatch.setattr(runtime, "_write_candidate_record", fail_last_write)
    with pytest.raises(OSError, match="simulated crash"):
        runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    restored = FileSearchRuntime(runtime.root_dir).recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    assert restored.state == "restored"
    assert (task.workspace / "initial_program.py").read_text() == expected


def test_wrong_recovery_identity_cannot_change_workspace(tmp_path):
    runtime, run_id, task = make_search(tmp_path)
    (task.workspace / "initial_program.py").write_text("untouched")
    runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    with pytest.raises(RuntimeError, match="RecoveryIdentityMismatch"):
        runtime.recover_candidate(run_id, task.candidate_id, "wrong")
    assert (task.workspace / "initial_program.py").read_text() == "untouched"


def test_unsupported_recovery_provider_fails_before_fencing(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    before = runtime._load_candidate_record(run_id, task.candidate_id)
    provider = get_workspace_provider("copy")
    monkeypatch.setattr(type(provider), "capabilities", replace(provider.capabilities, recover=False))
    with pytest.raises(UnsupportedWorkspaceCapability, match="recover"):
        runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    assert runtime._load_candidate_record(run_id, task.candidate_id) == before


def test_provider_neutral_recovery_accepts_snapshot_identity(tmp_path, monkeypatch):
    """Contract fake only: this does not certify a working ThinkThread runtime."""
    runtime, run_id, task = make_search(tmp_path)
    snapshot = ArtifactRef(kind="fs_snapshot", provider="thinkthread", id="snapshot-baseline")
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    record.task.workspace_backend = "thinkthread"
    record.task.workspace_provider_handle = {"child_id": "child-1"}
    record.results_ledger_artifact_ref = snapshot
    runtime._write_candidate_record(run_id, record)
    calls = []

    class SnapshotProvider:
        capabilities = replace(get_workspace_provider("copy").capabilities, recover=True)

        def recover(self, workspace, artifact_ref, **kwargs):
            calls.append((workspace, artifact_ref, kwargs["provider_handle"]))
            return snapshot

        def changed_files(self, *_args):
            return []

        def status(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("goal_plus.runtime.get_workspace_provider", lambda _: SnapshotProvider())
    point = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    result = runtime.recover_candidate(run_id, task.candidate_id, point.recovery_id)
    assert result.restored_artifact_ref == snapshot
    assert calls == [(task.workspace, snapshot, {"child_id": "child-1"})]


def test_recovery_does_not_rollback_other_lanes_or_budget(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    first, peer = runtime.start_batch(run_id, plan.plan_id)
    runtime.run_verifier(run_id, first.candidate_id)
    runtime.run_verifier(run_id, peer.candidate_id)
    peer_before = runtime._load_candidate_record(run_id, peer.candidate_id)
    budget_before = runtime._load_run(run_id).budget_used.copy()
    (first.workspace / "initial_program.py").write_text("partial")
    recovery = runtime.prepare_candidate_recovery(run_id, first.candidate_id)
    runtime.recover_candidate(run_id, first.candidate_id, recovery.recovery_id)
    assert runtime._load_candidate_record(run_id, peer.candidate_id) == peer_before
    assert runtime._load_run(run_id).budget_used == budget_before


def test_complete_settlement_survives_crash_before_best_projection(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    monkeypatch.setattr(runtime, "_update_best_seen", lambda *_: (_ for _ in ()).throw(SystemExit("projection crash")))
    with pytest.raises(SystemExit, match="projection crash"):
        runtime.run_verifier(run_id, task.candidate_id)
    restarted = FileSearchRuntime(runtime.root_dir)
    record = restarted._load_candidate_record(run_id, task.candidate_id)
    assert len(record.iterations) == 1
    assert record.score_report is not None
    recovery = restarted.prepare_candidate_recovery(run_id, task.candidate_id)
    restarted.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    assert len(restarted._load_candidate_record(run_id, task.candidate_id).iterations) == 1
    assert restarted.status(run_id).best_candidate_id == task.candidate_id


def test_ledger_commit_without_settlement_record_rolls_back_as_unfinished(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    original = runtime._write_candidate_record

    def interrupt_record(run_id, record):
        if record.iterations:
            raise SystemExit("settlement crash")
        original(run_id, record)

    monkeypatch.setattr(runtime, "_write_candidate_record", interrupt_record)
    with pytest.raises(SystemExit, match="settlement crash"):
        runtime.run_verifier(run_id, task.candidate_id)
    restarted = FileSearchRuntime(runtime.root_dir)
    recovery = restarted.prepare_candidate_recovery(run_id, task.candidate_id)
    assert recovery.point.iteration == 0
    restarted.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    assert restarted._load_candidate_record(run_id, task.candidate_id).iterations == []


@pytest.mark.parametrize("state", [RunState.PROMOTED, RunState.ABORTED, RunState.FAILED])
def test_terminal_run_cannot_be_recovered(tmp_path, state):
    runtime, run_id, task = make_search(tmp_path)
    run = runtime._load_run(run_id)
    run.state = state
    runtime._write_run(run)
    before = runtime._load_candidate_record(run_id, task.candidate_id)
    with pytest.raises(RuntimeError, match="cannot prepare recovery"):
        runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    assert runtime._load_candidate_record(run_id, task.candidate_id) == before


def test_codex_quiescence_observation_must_follow_this_recovery(tmp_path):
    from goal_plus.host_recovery import observe_codex_registry
    runtime, run_id, task = make_search(tmp_path)
    session = runtime.start_agent_session(run_id, task.candidate_id)
    observe_worker(runtime.root_dir, session, os.getpid())
    payload = {"agents": [{"agent_name": session.host_handle.task_name, "agent_status": {"completed": "done"}}]}
    observe_codex_registry(runtime.root_dir, [session], payload)
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    with pytest.raises(RuntimeError, match="RecoveryWorkerStillRunning"):
        approve_recovery(runtime.root_dir, recovery)
    observe_codex_registry(runtime.root_dir, [session], payload)
    approve_recovery(runtime.root_dir, recovery)
    assert runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id).state == "restored"


def test_verifier_finishing_after_prepare_cannot_settle_or_fail_run(tmp_path, monkeypatch):
    runtime, run_id, task = make_search(tmp_path)
    session = runtime.start_agent_session(run_id, task.candidate_id)
    settle = runtime._settle_process_verifier

    def delayed_settlement(**kwargs):
        runtime.prepare_candidate_recovery(run_id, task.candidate_id)
        return settle(**kwargs)

    monkeypatch.setattr(runtime, "_settle_process_verifier", delayed_settlement)
    with pytest.raises(RuntimeError, match="CandidateRecoveryRequired"):
        runtime.run_verifier(run_id, task.candidate_id,
                             agent_session_id=session.agent_session_id, hypothesis="late")
    assert runtime.status(run_id).state != RunState.FAILED
    assert runtime._load_candidate_record(run_id, task.candidate_id).iterations == []


def test_git_worktree_recovery_preserves_source_and_external_symlink_target(tmp_path):
    import subprocess
    from goal_plus.models import SearchSpec
    from tests.test_workspace_backends import git_commit, git_output

    project = make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    source_head = git_commit(tmp_path, "source baseline")
    runtime = FileSearchRuntime(tmp_path / ".gp")
    data = spec_for(project).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree", "promotion_mode": "artifact_only"}
    frozen = runtime.freeze_spec(SearchSpec.model_validate(data), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.run_verifier(run_id, task.candidate_id)
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("untouched")
    (task.workspace / "external-link").symlink_to(sentinel)
    (task.workspace / "initial_program.py").write_text("partial")
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    assert (task.workspace / "initial_program.py").read_text() == "VALUE = 0\n"
    assert sentinel.read_text() == "untouched"
    assert not (task.workspace / "external-link").exists()
    assert (project / "initial_program.py").read_text() == "VALUE = 0\n"
    assert git_output(tmp_path, "rev-parse", "HEAD") == source_head


def test_real_process_exit_is_required_before_host_approval(tmp_path):
    import subprocess
    import sys

    runtime, run_id, task = make_search(tmp_path)
    session = runtime.start_agent_session(run_id, task.candidate_id)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        observe_worker(runtime.root_dir, session, process.pid)
        recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
        with pytest.raises(RuntimeError, match="RecoveryWorkerStillRunning"):
            approve_recovery(runtime.root_dir, recovery)
    finally:
        process.terminate()
        process.wait(timeout=5)
    approve_recovery(runtime.root_dir, recovery)
    assert runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id).state == "restored"


def test_codex_fenced_worker_cannot_use_tools_but_can_stop(tmp_path):
    import json
    from tests.test_codex_goal_plus_hooks import _run_hook

    runtime, run_id, task = make_search(tmp_path)
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    payload = {
        "agent_id": "native-worker", "agent_type": "search_candidate_agent",
        "tool_name": "search_get_agent_context",
        "tool_input": {"agent_session_id": session.agent_session_id},
    }
    result = _run_hook(tmp_path, runtime.root_dir, {**payload, "hook_event_name": "PreToolUse"})
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "StaleAgentSession" in decision["permissionDecisionReason"]
    # Replacing the model-supplied ID cannot move the same native worker to a
    # newer generation: the first trusted identity mapping remains pinned.
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    from goal_plus.goal_plus_stop_hook import _search_candidate_agent_session_id
    assert _search_candidate_agent_session_id(runtime.root_dir, {
        **payload, "tool_input": {"agent_session_id": "invented-fresh-session"},
    }) == session.agent_session_id
    assert record.execution_generation == 1
    result = _run_hook(tmp_path, runtime.root_dir, {**payload, "hook_event_name": "SubagentStop"})
    assert result.returncode == 0
    assert not result.stdout


@pytest.mark.parametrize("tool", ["search_recovery_status", "search_prepare_recovery", "search_recover_candidate"])
def test_codex_child_cannot_request_main_owned_recovery(tmp_path, tool):
    import json
    from tests.test_codex_goal_plus_hooks import _run_hook

    result = _run_hook(tmp_path, tmp_path / ".gp", {
        "hook_event_name": "PreToolUse", "agent_id": "child", "agent_type": "default",
        "tool_name": f"mcp__goal_plus__{tool}", "tool_input": {},
    })
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("input_key", ["tool_input", "input"])
def test_shared_recovery_gate_checks_goal_run_ownership(tmp_path, input_key):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.models import GoalPlusLinkedSearch

    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    goal = runtime.create_goal("recovery owner")
    goal.search_tasks = [GoalPlusLinkedSearch(
        run_id="owned", goal_revision=goal.goal_revision, linked_at=goal.created_at,
    )]
    runtime._write_record(goal)
    context = {"tool_name": "search_prepare_recovery", input_key: {"run_id": "other"}}
    assert "SearchRecoveryOwnershipRequired" in runtime.gate(goal.goal_plus_id, "pre_tool_use", context).reason
    # Even a valid link cannot grant recovery to an already ended Goal.
    goal.status = "complete"
    runtime._write_record(goal)
    context[input_key] = {"run_id": "owned"}
    assert runtime.gate(goal.goal_plus_id, "pre_tool_use", context).decision == "block"


def test_pi_pool_may_stop_fenced_lane_before_closeout(tmp_path, monkeypatch):
    from goal_plus.pi_pool import _assert_active_pool_close_allowed

    runtime, run_id, task = make_search(tmp_path)
    runtime.start_agent_session(run_id, task.candidate_id)
    monkeypatch.setattr(FileSearchRuntime, "_outer_deadline_value", lambda *_: "2099-01-01T00:00:00Z")
    pool = {"pool_id": "recovery-pool", "run_id": run_id}
    jobs = [{"candidate_id": task.candidate_id, "status": "running"}]
    with pytest.raises(RuntimeError, match="outside the closeout reserve"):
        _assert_active_pool_close_allowed(runtime.root_dir, pool, jobs)
    runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    _assert_active_pool_close_allowed(runtime.root_dir, pool, jobs)
    with pytest.raises(RuntimeError, match="outside the closeout reserve"):
        _assert_active_pool_close_allowed(runtime.root_dir, pool, [*jobs, {"candidate_id": "peer", "status": "running"}])


def test_adaptive_recovery_replays_settled_task_registration_idempotently(tmp_path):
    from tests.adaptive_search.test_scheduler import _start_runtime

    runtime, run_id, tasks = _start_runtime(tmp_path, max_parallel=1)
    task = tasks[0]
    runtime.run_verifier(run_id, task.candidate_id)
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    recovery = runtime.prepare_candidate_recovery(run_id, task.candidate_id)
    runtime.recover_candidate(run_id, task.candidate_id, recovery.recovery_id)
    restored = runtime._load_candidate_record(run_id, task.candidate_id)
    assert restored.iterations == record.iterations
    assert restored.results_ledger == record.results_ledger


def test_unsettled_shared_publication_does_not_survive_as_a_family_head(tmp_path, monkeypatch):
    from goal_plus.shared_dir import SharedDirManager
    from tests.test_shared_dir import _shared_run, _write_tool

    runtime, run_id, sessions = _shared_run(tmp_path)
    worker = sessions[0]
    _write_tool(worker.share_out)
    original = runtime._write_candidate_record

    def interrupt_record(run_id, record):
        if record.iterations:
            raise SystemExit("before settlement commit")
        original(run_id, record)

    monkeypatch.setattr(runtime, "_write_candidate_record", interrupt_record)
    with pytest.raises(SystemExit, match="before settlement commit"):
        runtime.run_verifier(run_id, worker.candidate_id,
                             agent_session_id=worker.agent_session_id, hypothesis="publish helper")
    restarted = FileSearchRuntime(runtime.root_dir)
    manager = SharedDirManager(restarted._run_dir(run_id))
    assert len(manager.pending_tool_ids()) == 1
    session = restarted._load_agent_session_by_id(worker.agent_session_id)
    observe_worker(restarted.root_dir, session, os.getpid())
    recovery = restarted.prepare_candidate_recovery(run_id, worker.candidate_id)
    monkeypatch.setattr("goal_plus.host_recovery.process_identity", lambda _: None)
    SearchTools(restarted).search_recover_candidate(run_id, worker.candidate_id, recovery.recovery_id)
    assert not manager.pending_tool_ids()
    assert not manager.tool_family_catalog(max_published_versions_per_family=2)
    archive = restarted._candidate_dir(run_id, worker.candidate_id) / "recoveries" / recovery.recovery_id / "unsettled-shared-tools.json"
    assert archive.exists()
    assert restarted._load_candidate_record(run_id, worker.candidate_id).iterations == []
