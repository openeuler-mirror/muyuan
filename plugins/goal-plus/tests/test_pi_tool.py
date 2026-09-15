from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import goal_plus.internal_agents as internal_agents
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import GoalPlusHostCommandInvocation, SearchSpec
from goal_plus.pi_tool import call_pi_host_command, call_pi_tool
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal

pytestmark = pytest.mark.pi


@pytest.mark.host_process
def test_pi_scope_tool_records_current_native_agent_config(tmp_path: Path) -> None:
    from goal_plus.host_scope import close_execution_scope

    root = tmp_path / ".gp"
    goals = FileGoalPlusRuntime(root)
    invocation = GoalPlusHostCommandInvocation(
        agent_harness="pi", transport="pi_extension_command", command="/goal-plus", action="start",
        session_id="pi-main", native_entry_id="1", invocation_id="pi:start",
        raw_input_sha256=hashlib.sha256(b"/goal-plus Test").hexdigest(), invoked_at="2026-09-06T00:00:00Z",
    )
    goal = goals.start_from_host_command("Test", invocation=invocation)
    goals.activate_session(goal.goal_plus_id, {"agent_harness": "pi", "session_id": "pi-main"})
    args = {
            "agent_harness": "pi",
            "goal_plus_id": goal.goal_plus_id,
            "native_session_id": "pi-main",
            "owner_session_id": "pi-main",
            "run_id": goal.goal_plus_id,
            "native_agent_config": {
                "model_pattern": "native-main/example-model",
                "thinking_level": "low",
            },
        }
    result = call_pi_tool(root, "host_execution_scope_open", args)

    assert result["native_agent_config"] == {
        "model_pattern": "native-main/example-model",
        "thinking_level": "low",
    }
    close_execution_scope(root_dir=root, scope_id=result["scope_id"], reason="aborted")
    before = goals.status(goal.goal_plus_id)
    with pytest.raises(RuntimeError, match="new work is fenced"):
        call_pi_tool(root, "host_execution_scope_open", args)
    assert goals.status(goal.goal_plus_id) == before
    for action in ("pause", "resume"):
        goals.control_from_host_command(goal.goal_plus_id, invocation=invocation.model_copy(update={
            "action": action, "invocation_id": f"pi:{action}",
        }))
    resumed = call_pi_tool(root, "host_execution_scope_open", args)
    assert resumed["scope_id"] != result["scope_id"]
    assert resumed["activation_id"] == f"{goal.goal_plus_id}:{goals.status(goal.goal_plus_id).control.version}"
    with pytest.raises(RuntimeError, match="MainSessionMismatch"):
        call_pi_tool(root, "host_execution_scope_open", {**args, "native_session_id": "foreign"})
    close_execution_scope(root_dir=root, scope_id=resumed["scope_id"], reason="test")


def _link_run(
    root: Path,
    project: Path,
    frozen_spec_id: str,
    run_id: str,
) -> None:
    start_host_verified_goal(root, project)
    goals = FileGoalPlusRuntime(root)
    record = goals.require_host_verified_active_goal()
    goals.link_search_run(record.goal_plus_id, frozen_spec_id, run_id)


def test_pi_model_facing_create_and_update_are_deny_only(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        call_pi_tool(tmp_path / ".gp", "goal_plus_create", {"raw_goal": "bad"})
    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        call_pi_tool(
            tmp_path / ".gp",
            "goal_plus_update_goal",
            {
                "goal_plus_id": "gp_0001",
                "raw_goal": "bad",
                "expected_revision": 1,
            },
        )
    assert not (tmp_path / ".gp").exists()


def test_pi_host_command_control_path_creates_verified_record(tmp_path: Path) -> None:
    raw_goal = "max_parallel=2 promotion_mode=artifact_only Test Pi config"
    raw_input = f"/goal-plus {raw_goal}"
    token = "00000000-0000-4000-8000-000000000001"
    capability = tmp_path / ".gp" / "host-entrypoint" / "pi" / f"{token}.json"
    capability.parent.mkdir(parents=True)
    capability.write_text(
        json.dumps({"token": token, "issued_at_ms": int(time.time() * 1000)}),
        encoding="utf-8",
    )
    result = call_pi_host_command(
        tmp_path / ".gp",
        "goal_plus_host_start",
        {
            "raw_goal": raw_goal,
            "source_path": ".",
            "invocation": {
                "agent_harness": "pi",
                "transport": "pi_extension_command",
                "command": "/goal-plus",
                "action": "start",
                "session_id": "pi:test",
                "native_entry_id": "entry-1",
                "invocation_id": "pi:test:entry-1",
                "raw_input_sha256": hashlib.sha256(raw_input.encode()).hexdigest(),
                "invoked_at": "2026-08-28T00:00:00Z",
            },
        },
        capability_token=token,
    )

    assert result["entrypoint_status"] == "host_verified"
    assert result["host_command_invocations"][0]["command"] == "/goal-plus"
    assert result["raw_goal"].startswith("Test Pi config")
    assert result["command_config"]["max_parallel"] == 2
    assert result["command_config"]["promotion_mode"] == "artifact_only"
    assert result["active_session"]["agent_harness"] == "pi"
    assert result["active_session"]["session_id"] == "pi:test"
    assert result["active_session"]["state"] == "attached"
    assert result["phase"] == "goal"
    assert result["next_action"]["kind"] == "execute_goal"
    assert result["triage"] is None
    gate = call_pi_tool(
        tmp_path / ".gp",
        "goal_plus_gate",
        {
            "goal_plus_id": result["goal_plus_id"],
            "event": "pre_tool_use",
            "context": {"tool_name": "edit", "tool_input": {"path": "fix.py"}},
        },
    )
    assert gate["decision"] == "allow"
    assert not capability.exists()


def test_pi_host_command_starts_independent_goals_for_different_sessions(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / ".gp"
    results = []
    for index, session_id in enumerate(("pi:first", "pi:second"), start=1):
        token = f"00000000-0000-4000-8000-{index:012d}"
        capability = runtime_root / "host-entrypoint" / "pi" / f"{token}.json"
        capability.parent.mkdir(parents=True, exist_ok=True)
        capability.write_text(
            json.dumps({"token": token, "issued_at_ms": int(time.time() * 1000)}),
            encoding="utf-8",
        )
        results.append(
            call_pi_host_command(
                runtime_root,
                "goal_plus_host_start",
                {
                    "raw_goal": f"task {index}",
                    "source_path": ".",
                    "invocation": {
                        "agent_harness": "pi",
                        "transport": "pi_extension_command",
                        "command": "/goal-plus",
                        "action": "start",
                        "session_id": session_id,
                        "native_entry_id": f"entry-{index}",
                        "invocation_id": f"pi:test:entry-{index}",
                        "raw_input_sha256": hashlib.sha256(
                            f"/goal-plus task {index}".encode()
                        ).hexdigest(),
                        "invoked_at": "2026-09-05T00:00:00Z",
                    },
                },
                capability_token=token,
            )
        )

    assert [result["goal_plus_id"] for result in results] == ["gp_0001", "gp_0002"]
    assert [result["active_session"]["session_id"] for result in results] == [
        "pi:first",
        "pi:second",
    ]


def test_pi_host_session_event_uses_native_session_identity(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(runtime_root)
    record = runtime.create_goal("Continue in the same native Pi session")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "pi", "session_id": "pi-session-1"},
    )

    token = "00000000-0000-4000-8000-000000000003"
    capability = runtime_root / "host-entrypoint" / "pi" / f"{token}.json"
    capability.parent.mkdir(parents=True)
    capability.write_text(
        json.dumps({"token": token, "issued_at_ms": int(time.time() * 1000)}),
        encoding="utf-8",
    )
    continued = call_pi_host_command(
        runtime_root,
        "goal_plus_host_session_event",
        {
            "goal_plus_id": record.goal_plus_id,
            "session_id": "pi-session-1",
            "event": "session_start",
            "delivery": "auto",
            "interaction": "headless",
        },
        capability_token=token,
    )
    assert continued["decision"] == "wait_for_user"
    assert continued["session_state"] == "attached"

    mismatch_token = "00000000-0000-4000-8000-000000000004"
    mismatch_capability = (
        runtime_root / "host-entrypoint" / "pi" / f"{mismatch_token}.json"
    )
    mismatch_capability.write_text(
        json.dumps(
            {"token": mismatch_token, "issued_at_ms": int(time.time() * 1000)}
        ),
        encoding="utf-8",
    )
    mismatch = call_pi_host_command(
        runtime_root,
        "goal_plus_host_session_event",
        {
            "goal_plus_id": record.goal_plus_id,
            "session_id": "pi-session-2",
            "event": "session_start",
            "delivery": "auto",
            "interaction": "headless",
        },
        capability_token=mismatch_token,
    )
    assert mismatch["decision"] == "stop"
    assert mismatch["reason"] == "session_mismatch"


def test_pi_host_command_rejects_missing_one_time_capability(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing one-time Pi extension capability"):
        call_pi_host_command(
            tmp_path / ".gp",
            "goal_plus_host_start",
            {"raw_goal": "bad", "invocation": {}},
            capability_token="00000000-0000-4000-8000-000000000002",
        )
    assert not list((tmp_path / ".gp" / "goal-plus").glob("gp_*/goal.json"))


def test_pi_host_command_kicks_all_internal_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / ".gp"
    token = "00000000-0000-4000-8000-000000000005"
    capability = runtime_root / "host-entrypoint" / "pi" / f"{token}.json"
    capability.parent.mkdir(parents=True)
    capability.write_text(
        json.dumps({"token": token, "issued_at_ms": int(time.time() * 1000)}),
        encoding="utf-8",
    )
    calls: list[tuple[Path | str, str, str]] = []

    def record(
        root: Path | str,
        run_id: str,
        *,
        agent_harness: str,
    ) -> dict[str, bool]:
        calls.append((root, run_id, agent_harness))
        return {"annotation_started": True, "verifier_started": True}

    monkeypatch.setattr(internal_agents, "kick_internal_agents", record)
    monkeypatch.setattr(
        internal_agents, "kick_pi_internal_agents",
        lambda root, run_id: internal_agents.kick_internal_agents(root, run_id, agent_harness="pi"),
    )
    result = call_pi_host_command(
        runtime_root,
        "goal_plus_host_kick_internal_agents",
        {"run_id": "run_host_inherited"},
        capability_token=token,
    )

    assert result == {
        "ok": True,
        "run_id": "run_host_inherited",
        "annotation_started": True,
        "verifier_started": True,
    }
    assert calls == [(runtime_root, "run_host_inherited", "pi")]
    assert not capability.exists()


def test_pi_tool_calls_context_verifier_and_iterations(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    _link_run(runtime_root, project, frozen.frozen_spec_id, run_id)

    monkeypatch.setenv("GOAL_PLUS_PI_ROLE", "worker")
    monkeypatch.setenv("GOAL_PLUS_AGENT_SESSION_ID", session.agent_session_id)

    context = call_pi_tool(
        runtime_root,
        "goal_plus_search_get_agent_context",
        {"agent_session_id": session.agent_session_id},
    )
    assert context["candidate_task"]["workspace"] == str(task.workspace)
    assert context["candidate_id"] == task.candidate_id
    assert "supplemental_evaluation_enabled" not in context

    assert call_pi_tool(
        runtime_root,
        "goal_plus_search_get_global_evidence",
        {"agent_session_id": session.agent_session_id},
    ) == []

    (task.workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = call_pi_tool(
        runtime_root,
        "goal_plus_search_run_verifier",
        {
            "run_id": run_id,
            "candidate_id": task.candidate_id,
            "agent_session_id": session.agent_session_id,
            "hypothesis": "Raise VALUE once",
        },
    )
    assert report["candidate_id"] == task.candidate_id

    iterations = call_pi_tool(
        runtime_root,
        "goal_plus_search_list_iterations",
        {"agent_session_id": session.agent_session_id},
    )
    assert iterations["items"][0]["agent_session_id"] == session.agent_session_id


def test_pi_worker_tools_are_scoped_to_their_agent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2),
        [project / "evaluator.py"], agent_harness="codex",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    own = runtime.start_agent_session(run_id, tasks[0].candidate_id)
    peer = runtime.start_agent_session(run_id, tasks[1].candidate_id)
    _link_run(runtime_root, project, frozen.frozen_spec_id, run_id)
    monkeypatch.setenv("GOAL_PLUS_PI_ROLE", "worker")
    monkeypatch.setenv("GOAL_PLUS_AGENT_SESSION_ID", own.agent_session_id)

    context = call_pi_tool(
        runtime_root,
        "goal_plus_search_get_agent_context",
        {"agent_session_id": own.agent_session_id},
    )
    assert context["candidate_id"] == tasks[0].candidate_id

    with pytest.raises(PermissionError, match="GoalPlusSessionMismatch"):
        call_pi_tool(
            runtime_root,
            "goal_plus_search_get_agent_context",
            {"agent_session_id": peer.agent_session_id},
        )
    with pytest.raises(PermissionError, match="GoalPlusRoleDenied"):
        call_pi_tool(runtime_root, "goal_plus_search_select", {"run_id": run_id})


def test_pi_main_cannot_submit_final_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOAL_PLUS_PI_ROLE", raising=False)
    with pytest.raises(PermissionError, match="GoalPlusRoleDenied"):
        call_pi_tool(
            tmp_path / ".gp",
            "goal_plus_submit_final_check",
            {
                "goal_plus_id": "gp_missing",
                "check_id": "fc_missing",
                "goal_revision": 1,
                "verdict": "fail",
                "summary": "must be rejected before lookup",
            },
        )


def test_pi_tool_stages_explicit_candidate_drafts(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    runtime_root = tmp_path / ".search"
    runtime = FileSearchRuntime(runtime_root)
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["shared_dir"] = {"enabled": True}
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"], agent_harness="codex",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    _link_run(runtime_root, project, frozen.frozen_spec_id, run_id)
    source = task.workspace / ".tmp" / "tool-drafts" / "trace.py"
    monkeypatch.setenv("GOAL_PLUS_PI_ROLE", "worker")
    monkeypatch.setenv("GOAL_PLUS_AGENT_SESSION_ID", session.agent_session_id)
    source.parent.mkdir(parents=True)
    source.write_text("print('trace')\n", encoding="utf-8")

    staged = call_pi_tool(
        runtime_root,
        "goal_plus_search_stage_shared_tool",
        {
            "agent_session_id": session.agent_session_id,
            "name": "trace-helper",
            "summary": "Collect a bounded diagnostic trace.",
            "entrypoint": "trace.py",
            "candidate_relative_source_paths": [".tmp/tool-drafts/trace.py"],
        },
    )

    assert staged["staged_name"] == "trace-helper"
    assert Path(staged["staging_path"], "trace.py").is_file()


@pytest.mark.parametrize("action", ["open", "wait_any", "snapshot", "continue", "close"])
@pytest.mark.parametrize("prefix", ["pi", "thinkthread"])
def test_pi_tool_rejects_removed_pool_entrypoints(tmp_path: Path, action: str, prefix: str) -> None:
    root = tmp_path / ".gp"
    with pytest.raises(ValueError, match="unsupported pi tool"):
        call_pi_tool(root, f"{prefix}_search_pool_{action}", {"run_id": "run_1"})
    assert not root.exists()




@pytest.mark.parametrize(
    "tool_name",
    [
        "search_abort_agent_session",
        "pi_search_run_candidate",
        "pi_search_run_batch",
        "pi_search_pool_submit",
    ],
)
def test_pi_tool_rejects_removed_tools(tmp_path: Path, tool_name: str) -> None:
    with pytest.raises(ValueError, match="unsupported pi tool"):
        call_pi_tool(tmp_path / ".search", tool_name, {})


def test_pi_tool_exposes_host_only_publication_notification_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SearchTools,
        "search_main_agent_notifications",
        lambda _self, run_id: {"run_id": run_id, "notifications": []},
    )
    monkeypatch.setattr(
        SearchTools,
        "search_ack_main_agent_notification",
        lambda _self, run_id, notification_id: {
            "run_id": run_id,
            "notification_id": notification_id,
            "state": "delivered",
        },
    )

    assert call_pi_tool(
        tmp_path / ".gp",
        "search_main_agent_notifications",
        {"run_id": "run-1"},
    )["notifications"] == []
    assert call_pi_tool(
        tmp_path / ".gp",
        "search_ack_main_agent_notification",
        {"run_id": "run-1", "notification_id": "notification-1"},
    )["state"] == "delivered"
