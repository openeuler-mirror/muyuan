"""App-server roots receive candidate gates only through exact host binding."""

import os
import json
from pathlib import Path

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, run_host_hook_inprocess, spec_for, start_host_verified_goal


pytestmark = pytest.mark.codex


def test_legacy_role_and_prompt_cannot_bind_a_candidate(tmp_path):
    from goal_plus import goal_plus_stop_hook as hooks

    source = make_project(tmp_path)
    root = tmp_path / ".gp"
    runtime = FileSearchRuntime(root)
    frozen = runtime.freeze_spec(spec_for(source), [source / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(session.agent_session_id, {
        "agent_harness": "codex", "external_id": "bound-thread", "metadata": {"transport": "app-server"},
    })
    before = {path: path.read_bytes() for path in (root / "runs").rglob("*.json")}
    assert hooks._search_candidate_agent_session_id(root, {
        "agent_type": "search_candidate_agent", "agent_id": "foreign-thread",
        "session_id": "foreign-thread", "prompt": session.launch["message"],
        "tool_input": {"agent_session_id": session.agent_session_id},
    }) is None
    assert {path: path.read_bytes() for path in before} == before
    assert not (root / "host-logs/codex-time-advisory/workers").exists()


@pytest.mark.parametrize("metadata", [
    {"status": "completed"}, {"terminal_status": "interrupted"},
    {"subagent_stop_observed_at": "2026-09-12T00:00:00Z"},
])
def test_legacy_terminal_metadata_does_not_prove_candidate_exit(tmp_path, metadata):
    from goal_plus.host_scope import _codex_run_is_durably_quiescent

    source = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(source), [source / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(session.agent_session_id, {"metadata": metadata})
    assert not _codex_run_is_durably_quiescent(runtime.root_dir, run_id, scope={"processes": []})


def test_main_hook_loads_canonical_overview_without_skill(tmp_path):
    source = make_project(tmp_path)
    root = tmp_path / ".gp"
    result = run_host_hook_inprocess({
        "hook_event_name": "UserPromptSubmit", "session_id": "main-overview",
        "prompt": "$goal-plus Verify the local proof.", "cwd": str(source),
    }, cwd=source, env={**os.environ, "GOAL_PLUS_SEARCH_ROOT": str(root)})
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    overview = (Path(__file__).resolve().parents[1] / "assets/goal-plus-instructions.txt").read_text().strip()
    assert overview in context


@pytest.mark.parametrize("native_id", ["bound-native-thread", "foreign-native-thread"])
def test_app_server_candidate_patch_and_role_gates(tmp_path, native_id):
    source = make_project(tmp_path)
    root = tmp_path / ".gp"
    runtime = FileSearchRuntime(root)
    goal = start_host_verified_goal(root, source)
    spec = spec_for(source, max_parallel=1)
    spec = spec.model_copy(update={"strategy": spec.strategy.model_copy(update={"inner_agent": "common"})})
    frozen = runtime.freeze_spec(spec, [source / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(session.agent_session_id, {
        "agent_harness": "codex", "external_id": "bound-native-thread", "metadata": {"transport": "app-server"},
    })
    before_goal = FileGoalPlusRuntime(root).status(goal.goal_plus_id)
    before = {path: path.read_bytes() for path in (root / "runs").rglob("*.json")}

    def hook(tool, arguments):
        return run_host_hook_inprocess({
            "hook_event_name": "PreToolUse", "session_id": native_id,
            "tool_name": tool, "tool_input": arguments, "cwd": str(task.workspace),
        }, cwd=task.workspace, env={
            **os.environ, "GOAL_PLUS_SEARCH_ROOT": str(root),
            "GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID": session.agent_session_id,
        })

    def patch(filename):
        return hook("apply_patch", {"command": (
            f"*** Begin Patch\n*** Update File: {filename}\n@@\n-old\n+new\n*** End Patch"
        )})

    denied = patch("evaluator.py")
    assert "CandidateWriteDenied" in denied.stdout
    scratch = task.workspace / ".tmp"
    scratch.mkdir(exist_ok=True)
    (scratch / "outside").symlink_to(source, target_is_directory=True)
    for body in (
        f"*** Delete File: {source / 'initial_program.py'}",
        f"*** Update File: initial_program.py\n*** Move to: {source / 'initial_program.py'}\n@@\n-old\n+new",
        "*** Add File: .tmp/outside/initial_program.py\n+pass",
        "*** Delete File: .git/config",
    ):
        denied = hook("apply_patch", {"command": f"*** Begin Patch\n{body}\n*** End Patch"})
        assert "CandidateWriteDenied" in denied.stdout
    denied = hook("apply_patch", {"command": "unrecognized transport"})
    assert "CandidateWriteDenied" in denied.stdout
    own = patch("initial_program.py")
    if native_id == "bound-native-thread":
        assert own.returncode == 0 and own.stdout == ""
    else:
        assert "CandidateWriteDenied" in own.stdout
    denied = hook("mcp__goal-plus__goal_plus_search_select", {"run_id": run_id})
    assert "GoalPlusRoleDenied" in denied.stdout
    stopped = run_host_hook_inprocess({
        "hook_event_name": "Stop", "session_id": native_id, "cwd": str(task.workspace),
    }, cwd=task.workspace, env={
        **os.environ, "GOAL_PLUS_SEARCH_ROOT": str(root),
        "GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID": session.agent_session_id,
    })
    assert stopped.returncode == 0 and stopped.stdout == ""
    events = [json.loads(path.read_text()) for path in (root / "host-logs/codex-hook-events").glob("*.json")]
    if native_id == "bound-native-thread":
        assert len(events) == 1
        assert events[0]["agent_session_id"] == session.agent_session_id
        assert events[0]["session_id"] == native_id
        assert events[0]["reason"] == "native_candidate_return"
    else:
        assert events == []
    assert {path: path.read_bytes() for path in before} == before
    assert FileGoalPlusRuntime(root).status(goal.goal_plus_id) == before_goal
