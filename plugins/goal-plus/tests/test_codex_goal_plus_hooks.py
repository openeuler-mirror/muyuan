from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from goal_plus.goal_plus import EXPLORATION_MODE_LINES, FileGoalPlusRuntime
from goal_plus.models import GoalPlusLinkedSearch, SearchSpec
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_with_host


HOOK_CLI = [
    sys.executable,
    "-m",
    "goal_plus.server",
    "--goal-plus-host-hook",
]
pytestmark = pytest.mark.codex


def _run_hook(
    tmp_path: Path,
    search_root: Path,
    hook_input: dict,
    **env: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        HOOK_CLI,
        cwd=tmp_path,
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "GOAL_PLUS_SEARCH_ROOT": str(search_root),
            "GOAL_PLUS_PROJECT_ROOT": str(tmp_path),
            **env,
        },
    )


def _additional_context(result: subprocess.CompletedProcess[str], event: str) -> str:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == event
    return specific["additionalContext"]


def _block_reason(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    return payload["reason"]


def _stop_hook_events(search_root: Path) -> list[dict]:
    event_dir = search_root / "host-logs" / "codex-hook-events"
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(event_dir.glob("*.json"))
    ]


def _terminal_report_goal(
    search_root: Path,
    *,
    status: str = "complete",
) -> tuple[FileGoalPlusRuntime, str]:
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Render the terminal Search report")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )
    record = runtime.status(record.goal_plus_id)
    linked = GoalPlusLinkedSearch(
        goal_revision=record.goal_revision,
        frozen_spec_id="spec_terminal",
        run_id="run_terminal",
    )
    runtime._write_record(
        record.model_copy(
            update={"search_tasks": [linked], "linked_search": linked}
        )
    )
    runtime.set_status(
        record.goal_plus_id,
        status=status,
        reason="terminal report fixture",
    )
    return runtime, record.goal_plus_id


def _write_codex_rollout(
    path: Path,
    *,
    turn_id: str,
    agent_path: str | None = None,
    approval_policy: str = "never",
    approvals_reviewer: str = "user",
    permission_profile: dict | None = None,
    active_permission_profile: dict | None = None,
) -> None:
    events: list[dict] = []
    if agent_path is not None:
        events.append(
            {
                "type": "session_meta",
                "payload": {
                    "source": {
                        "subagent": {"thread_spawn": {"agent_path": agent_path}}
                    }
                },
            }
        )
    events.extend(
        [
            {"not": "valid json line"},
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "approval_policy": approval_policy,
                    "approvals_reviewer": approvals_reviewer,
                    "sandbox_policy": {"type": "danger-full-access"},
                    "permission_profile": permission_profile or {"type": "disabled"},
                    "active_permission_profile": active_permission_profile
                    or {"id": ":danger-full-access"},
                    "workspace_roots": [str(path.parent)],
                },
            },
        ]
    )
    path.write_text(
        "{broken\n" + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _codex_search_worker(
    tmp_path: Path,
    *,
    worker_budget: dict | None = None,
) -> tuple[FileSearchRuntime, str, str, str]:
    project = make_project(tmp_path)
    payload = spec_with_host(
        project,
        "codex",
        strategy_name="random",
        max_parallel=1,
    ).model_dump(mode="json")
    payload["strategy"]["worker_budget"] = worker_budget or {
        "max_runtime_seconds": 600,
        "max_turns": 8,
        "on_exceed": "interrupt",
    }
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(SearchSpec.model_validate(payload), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id, {"goal": "test"})
    old_start = datetime.now(timezone.utc) - timedelta(seconds=90)
    runtime._write_agent_session(
        session.model_copy(
            update={
                "created_at": old_start.replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            }
        )
    )
    return runtime, run_id, task.candidate_id, session.agent_session_id


def test_user_prompt_submit_precreates_and_binds_goal(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "transcript_path": "/tmp/codex.jsonl",
            "prompt": "$goal-plus Optimize model throughput",
        },
    )

    context = _additional_context(result, "UserPromptSubmit")
    runtime = FileGoalPlusRuntime(search_root)
    records = list((search_root / "goal-plus").glob("gp_*/goal.json"))
    assert len(records) == 1
    record = runtime.status(records[0].parent.name)
    assert record.raw_goal == (
        "Optimize model throughput\n\n"
        + EXPLORATION_MODE_LINES["autonomous"]
    )
    assert record.active_session is not None
    assert record.active_session.host == "codex"
    assert record.active_session.session_id == "session-codex"
    assert record.policy["execution"]["main_reasoning_effort"] == "max"
    assert record.policy["execution"]["host"]["native_reasoning_effort"] == "max"
    assert record.entrypoint_status == "host_verified"
    assert len(record.host_command_invocations) == 1
    assert record.host_command_invocations[0].command == "$goal-plus"
    assert record.host_command_invocations[0].transport == "codex_user_prompt_submit"
    assert record.goal_plus_id in context
    assert "skill 和 MCP 不能创建或恢复记录" in context
    assert "不要仅因 Goal Plus 处于 active 就恢复工作" in context
    assert "范围、交付物或成功标准" in context
    assert "goal_plus_update_goal" in context
    assert "$goal-plus edit <完整修订目标>" in context
    assert "当前未设置最大运行时间" in context
    assert "不要询问或等待确认" in context
    assert "告知后立即继续执行" in context


@pytest.mark.parametrize(
    "prompt",
    [
        "/goal-plus Wrong host prefix",
        "$Goal-Plus Wrong command case",
        "$goal-plus RESUME",
        "$goal-plus EDIT revised goal",
        "$goal-plus edit",
    ],
)
def test_user_prompt_submit_rejects_non_exact_codex_entrypoints(
    tmp_path: Path,
    prompt: str,
) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": prompt,
        },
    )

    assert "$goal-plus" in _block_reason(result)
    assert not list((search_root / "goal-plus").glob("gp_*/goal.json"))


def test_natural_language_goal_plus_request_does_not_create_state(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "请帮我启动 goal-plus，并使用 goal-plus MCP。",
        },
    )

    assert result.stdout == ""
    assert not search_root.exists()


def test_user_prompt_submit_displays_effective_codex_permissions(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    transcript = tmp_path / "rollout-main.jsonl"
    _write_codex_rollout(transcript, turn_id="turn-main")

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "turn-main",
            "transcript_path": str(transcript),
            "permission_mode": "bypassPermissions",
            "prompt": "$goal-plus Inspect runtime permissions",
        },
    )

    payload = json.loads(result.stdout)
    message = payload["systemMessage"]
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "agent=main" in message
    assert "source=rollout.turn_context" in message
    assert "approval_policy=never" in message
    assert "approvals_reviewer=user" in message
    assert "active_profile=:danger-full-access" in message
    assert "permission_profile=disabled" in message
    assert "enforcement=disabled" in message
    assert "filesystem=unrestricted" in message
    assert "network=enabled" in message
    assert "sandbox=danger-full-access" in message
    assert "TOML" not in message
    assert message in context
    assert "第一条用户可见进度中完整照抄" in context


def test_user_prompt_submit_does_not_infer_stale_permissions(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    transcript = tmp_path / "rollout-stale.jsonl"
    _write_codex_rollout(
        transcript,
        turn_id="previous-turn",
        approval_policy="on-request",
        permission_profile={
            "type": "managed",
            "file_system": {"type": "restricted", "entries": []},
            "network": "restricted",
        },
        active_permission_profile={"id": ":workspace"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "current-turn",
            "transcript_path": str(transcript),
            "permission_mode": "bypassPermissions",
            "prompt": "$goal-plus Inspect runtime permissions",
        },
    )

    message = json.loads(result.stdout)["systemMessage"]
    assert "source=unavailable" in message
    assert "hook_permission_mode=bypassPermissions" in message
    assert "未从 TOML 或环境变量推断" in message
    assert "approval_policy=on-request" not in message
    assert ":workspace" not in message


def test_candidate_first_context_call_displays_its_own_permissions(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    search_root.mkdir()
    transcript = tmp_path / "rollout-worker.jsonl"
    _write_codex_rollout(
        transcript,
        turn_id="turn-worker",
        agent_path="/root/search_agent_20260826_demo_001",
        approvals_reviewer="auto_review",
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "turn_id": "turn-worker",
            "transcript_path": str(transcript),
            "permission_mode": "bypassPermissions",
            "agent_id": "worker-thread",
            "agent_type": "default",
            "tool_name": "functions.exec",
            "tool_input": (
                "await tools.mcp__goal_plus__search_get_agent_context("
                "{agent_session_id:'agent_demo_001'})"
            ),
        },
    )

    message = json.loads(result.stdout)["systemMessage"]
    assert "agent=/root/search_agent_20260826_demo_001" in message
    assert "source=rollout.turn_context" in message
    assert "turn_id=turn-worker" in message
    assert "approvals_reviewer=auto_review" in message
    assert "filesystem=unrestricted" in message
    assert "network=enabled" in message
    context = _additional_context(result, "PreToolUse")
    assert message in context
    assert "最终交接中完整照抄" in context


def test_candidate_permission_display_does_not_require_local_search_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate-worktree"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    transcript = workspace / "rollout-worker.jsonl"
    _write_codex_rollout(
        transcript,
        turn_id="turn-worker",
        agent_path="/root/search_agent_20260826_demo_001",
    )

    result = subprocess.run(
        HOOK_CLI,
        cwd=workspace,
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "turn_id": "turn-worker",
                "transcript_path": str(transcript),
                "tool_name": "mcp__goal_plus__search_get_agent_context",
                "tool_input": {"agent_session_id": "agent_demo_001"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"GOAL_PLUS_SEARCH_ROOT", "GOAL_PLUS_PROJECT_ROOT"}
        },
    )

    assert result.returncode == 0, result.stderr
    message = json.loads(result.stdout)["systemMessage"]
    assert "agent=/root/search_agent_20260826_demo_001" in message
    assert "source=rollout.turn_context" in message
    assert "turn_id=turn-worker" in message


def test_ordinary_subagent_does_not_get_goal_plus_permission_banner(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    search_root.mkdir()
    transcript = tmp_path / "rollout-ordinary-worker.jsonl"
    _write_codex_rollout(
        transcript,
        turn_id="turn-worker",
        agent_path="/root/docs_helper",
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "turn_id": "turn-worker",
            "transcript_path": str(transcript),
            "agent_id": "worker-thread",
            "agent_type": "default",
            "tool_name": "mcp__goal_plus__search_get_agent_context",
            "tool_input": {"agent_session_id": "agent_demo_001"},
        },
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_user_prompt_submit_uses_nested_thread_cwd_for_default_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    workspace = repository / "examples" / "circle-packing" / "workspace"
    workspace.mkdir(parents=True)
    (repository / ".git").mkdir()
    env = dict(os.environ)
    for name in (
        "GOAL_PLUS_PROJECT_ROOT",
        "GOAL_PLUS_SEARCH_ROOT",
        "CLAUDE_PROJECT_DIR",
    ):
        env.pop(name, None)

    result = subprocess.run(
        HOOK_CLI,
        cwd=workspace,
        input=json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-nested",
                "prompt": "$goal-plus Optimize the current directory",
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    context = _additional_context(result, "UserPromptSubmit")
    records = list((workspace / ".gp" / "goal-plus").glob("gp_*/goal.json"))
    assert len(records) == 1
    assert records[0].parent.name in context
    assert not (repository / ".gp").exists()


def test_user_prompt_submit_resume_finds_existing_parent_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    workspace = repository / "examples" / "circle-packing" / "workspace"
    workspace.mkdir(parents=True)
    (repository / ".git").mkdir()
    runtime = FileGoalPlusRuntime(repository / ".gp")
    record = runtime.create_goal("Optimize the repository")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-parent"},
    )
    env = dict(os.environ)
    for name in (
        "GOAL_PLUS_PROJECT_ROOT",
        "GOAL_PLUS_SEARCH_ROOT",
        "CLAUDE_PROJECT_DIR",
    ):
        env.pop(name, None)

    result = subprocess.run(
        HOOK_CLI,
        cwd=workspace,
        input=json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-parent",
                "prompt": "$goal-plus resume",
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    context = _additional_context(result, "UserPromptSubmit")
    assert record.goal_plus_id in context
    resumed = runtime.status(record.goal_plus_id)
    assert resumed.entrypoint_status == "host_verified"
    assert resumed.host_command_invocations[-1].action == "resume"
    assert not (workspace / ".gp").exists()


def test_user_prompt_submit_accepts_explicit_role_models(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": (
                "$goal-plus annotator=gpt-5.6-terra "
                "workers=gpt-5.6-terra,gpt-5.6-sol optimize throughput"
            ),
        },
    )

    context = _additional_context(result, "UserPromptSubmit")
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    record = FileGoalPlusRuntime(search_root).status(record_path.parent.name)
    assert record.raw_goal.startswith("optimize throughput")
    assert record.command_config.main is None
    assert record.command_config.annotator == "gpt-5.6-terra"
    assert record.command_config.workers == "gpt-5.6-terra,gpt-5.6-sol"
    assert record.command_config.worker_directive == "workers"
    assert "显式命令配置" in context
    assert "仅校验当前 Codex session" not in context
    assert "annotator=gpt-5.6-terra" in context
    assert "strategy.evidence_annotator.model" in context
    assert "workers=gpt-5.6-terra,gpt-5.6-sol" in context
    assert "strategy.models" in context
    assert "不要从其他显式角色推导默认值" in context


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (
            "$goal-plus main=gpt-5.6-terra optimize throughput",
            "main= is not supported",
        ),
        (
            "$goal-plus model=gpt-5.6-sol optimize throughput",
            "model= is not supported",
        ),
        (
            "$goal-plus workers=A,B models=A,B optimize throughput",
            "workers= and models= are aliases",
        ),
        (
            "$goal-plus annotator=A,B optimize throughput",
            "annotator= accepts exactly one model",
        ),
        (
            "$goal-plus workers=A,,B optimize throughput",
            "contains an empty model reference",
        ),
    ],
)
def test_user_prompt_submit_blocks_invalid_role_models_before_goal_creation(
    tmp_path: Path,
    prompt: str,
    expected: str,
) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "model": "gpt-5.6-sol",
            "prompt": prompt,
        },
    )

    assert expected in _block_reason(result)
    assert not list((search_root / "goal-plus").glob("gp_*/goal.json"))


def test_user_prompt_submit_blocks_retired_main_before_goal_edit(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    created = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus optimize throughput",
        },
    )
    _additional_context(created, "UserPromptSubmit")
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    goal_id = record_path.parent.name

    edited = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "model": "gpt-5.6-sol",
            "prompt": "$goal-plus edit main=gpt-5.6-terra optimize latency",
        },
    )

    assert "main= is not supported" in _block_reason(edited)
    record = FileGoalPlusRuntime(search_root).status(goal_id)
    assert record.goal_revision == 1
    assert record.raw_goal.startswith("optimize throughput")


def test_user_prompt_submit_materializes_probe_mode_in_raw_goal(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"

    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus mode=probe Check whether vectorization is viable",
        },
    )

    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    record = FileGoalPlusRuntime(search_root).status(record_path.parent.name)
    assert record.raw_goal == (
        "Check whether vectorization is viable\n\n"
        + EXPLORATION_MODE_LINES["probe"]
    )


def test_user_prompt_submit_is_idempotent_for_bound_session(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    hook_input = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-codex",
        "prompt": "$goal-plus Optimize model throughput",
    }

    first = _run_hook(tmp_path, search_root, hook_input)
    second = _run_hook(tmp_path, search_root, hook_input)

    first_context = _additional_context(first, "UserPromptSubmit")
    second_context = _additional_context(second, "UserPromptSubmit")
    records = list((search_root / "goal-plus").glob("gp_*/goal.json"))
    assert len(records) == 1
    assert "当前未设置最大运行时间" in first_context
    assert "当前未设置最大运行时间" not in second_context
    assert "Goal Plus Codex 运行时权限" in first_context
    assert "Goal Plus Codex 运行时权限" not in second_context
    assert "\n".join(
        line
        for line in first_context.splitlines()
        if not line.startswith("这是新创建记录的首轮。")
        and not line.startswith("Goal Plus Codex 运行时权限：")
        and not line.startswith("在本轮第一条用户可见进度中")
    ).rstrip() == second_context


def test_with_final_check_precreates_required_policy(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus-with-final-check Ship the complete feature",
        },
    )

    context = _additional_context(result, "UserPromptSubmit")
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    record = FileGoalPlusRuntime(search_root).status(record_path.parent.name)
    assert record.policy["final_check"]["mode"] == "required"
    assert record.goal_revision == 1
    assert "required" in context
    assert "加载并遵循 goal-plus skill" in context


def test_interrupted_session_restores_then_edits_same_goal(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    initial = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus Implement requirement A",
        },
    )
    initial_context = _additional_context(initial, "UserPromptSubmit")
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    goal_id = record_path.parent.name

    restored = _run_hook(
        tmp_path,
        search_root,
        {"hook_event_name": "SessionStart", "session_id": "session-codex"},
    )
    assert restored.stdout == ""
    detached = FileGoalPlusRuntime(search_root).status(goal_id)
    assert detached.active_session is not None
    assert detached.active_session.state == "detached"

    edited = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus edit Implement requirements A and B",
        },
    )
    edited_context = _additional_context(edited, "UserPromptSubmit")
    records = list((search_root / "goal-plus").glob("gp_*/goal.json"))
    assert len(records) == 1
    record = FileGoalPlusRuntime(search_root).status(goal_id)
    assert record.goal_revision == 2
    assert record.raw_goal == (
        "Implement requirements A and B\n\n"
        + EXPLORATION_MODE_LINES["autonomous"]
    )
    assert [revision.raw_goal for revision in record.goal_revisions] == [
        "Implement requirement A\n\n" + EXPLORATION_MODE_LINES["autonomous"],
        "Implement requirements A and B\n\n"
        + EXPLORATION_MODE_LINES["autonomous"],
    ]
    assert [item.action for item in record.host_command_invocations] == [
        "start",
        "edit",
    ]
    assert "目标修订版：2" in edited_context
    assert initial_context != edited_context


def test_interrupted_session_explicit_resume_keeps_same_revision(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus Implement requirement A",
        },
    )
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    goal_id = record_path.parent.name

    resumed = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus resume",
        },
    )
    context = _additional_context(resumed, "UserPromptSubmit")
    record = FileGoalPlusRuntime(search_root).status(goal_id)
    assert record.goal_revision == 1
    assert record.raw_goal == (
        "Implement requirement A\n\n"
        + EXPLORATION_MODE_LINES["autonomous"]
    )
    assert goal_id in context


def test_codex_interrupted_final_checker_is_recorded_and_can_stop(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal(
        "Ship it",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )
    runtime.prepare_final_check(record.goal_plus_id, "codex")

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": "checker-1",
            "agent_type": "goal_plus_final_checker",
            "task_name": "goal_plus_final_check_r1",
        },
    )
    assert result.stdout == ""
    interrupted = runtime.status(record.goal_plus_id)
    assert interrupted.final_checks[-1].status == "interrupted"
    assert interrupted.next_action.kind == "retry_final_check"  # type: ignore[union-attr]


def test_codex_stop_blocks_goal_mode_until_required_check_passes(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal(
        "Ship it",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-codex",
        },
    )
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "goal_plus_prepare_final_check" in payload["reason"]


def test_interrupted_codex_final_checker_must_submit_before_subagent_stop(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal(
        "Ship it",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )
    request = runtime.prepare_final_check(record.goal_plus_id, "codex")

    interrupted = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": "reviewer",
        },
    )
    assert json.loads(interrupted.stdout)["decision"] == "block"
    assert request["check"]["check_id"] in json.loads(interrupted.stdout)["reason"]

    runtime.submit_final_check(
        record.goal_plus_id,
        check_id=request["check"]["check_id"],
        goal_revision=1,
        verdict="fail",
        summary="Missing evidence",
        findings=[{"requirement": "tests"}],
        evidence=[{"kind": "inspection", "result": "missing"}],
    )
    released = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": "reviewer",
        },
    )
    assert released.stdout == ""


def test_session_start_detaches_bound_goal_until_explicit_resume(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {"hook_event_name": "SessionStart", "session_id": "session-codex"},
    )

    assert result.stdout == ""
    updated = runtime.status(record.goal_plus_id)
    assert updated.active_session is not None
    assert updated.active_session.state == "detached"


def test_post_tool_time_advisory_only_targets_search_candidate_subagent_once(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidate_id, agent_session_id = _codex_search_worker(tmp_path)
    search_root = runtime.root_dir
    context_tool = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__goal-plus__search_get_agent_context",
        "tool_input": {"agent_session_id": agent_session_id},
        "tool_response": {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": agent_session_id,
        },
    }

    main = _run_hook(
        tmp_path,
        search_root,
        {**context_tool, "session_id": "main-session"},
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
    )
    ordinary_subagent = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "agent_id": "ordinary-agent",
            "agent_type": "explorer",
            "tool_name": "read",
            "tool_input": {"path": "README.md"},
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
    )
    final_checker = _run_hook(
        tmp_path,
        search_root,
        {
            **context_tool,
            "agent_id": "reviewer-agent",
            "agent_type": "goal_plus_final_checker",
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
    )
    assert main.stdout == ""
    assert ordinary_subagent.stdout == ""
    assert final_checker.stdout == ""

    mapped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "agent_id": "search-worker-agent",
            "agent_type": "default",
            "tool_name": "functions.exec",
            "tool_input": (
                "const r = await tools.mcp__goal_plus__search_get_agent_context("
                f'{{agent_session_id:"{agent_session_id}"}}); text(r);'
            ),
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
    )
    assert mapped.stdout == ""

    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=agent_session_id,
        hypothesis="Hook-owned worker evidence",
    )
    advisory = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "agent_id": "search-worker-agent",
            "agent_type": "search_candidate_agent",
            "tool_name": "read",
            "tool_input": {"path": "initial_program.py"},
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
    )

    context = _additional_context(advisory, "PostToolUse")
    assert "时间提示（仅供参考）" in context
    assert candidate_id in context
    assert "不强制采取任何动作" in context

    repeated = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "agent_id": "search-worker-agent",
            "agent_type": "search_candidate_agent",
            "tool_name": "edit",
            "tool_input": {"path": "initial_program.py"},
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
    )
    assert repeated.stdout == ""
    evidence = list(
        (search_root / "host-logs" / "codex-time-advisory" / "sent").glob("*.json")
    )
    assert len(evidence) == 1
    payload = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert payload["agent_session_id"] == agent_session_id
    assert payload["candidate_id"] == candidate_id
    assert payload["trigger_tool"] == "read"
    snapshot = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    [subagent] = snapshot["subagents"]
    assert subagent["time_advisory_sent"] is True
    assert subagent["time_advisory"]["agent_session_id"] == agent_session_id


def test_pre_tool_use_blocks_search_before_spec_is_ready(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__search_create",
            "tool_input": {},
        },
    )

    payload = json.loads(result.stdout)
    specific = payload["hookSpecificOutput"]
    assert specific == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Search 工具要求先提供高置信度的冻结 spec draft。",
    }
    assert runtime.status(record.goal_plus_id).hook_counters["pre_tool_use"] == 1


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__goal-plus__goal_plus_create",
        "mcp__goal-plus__goal_plus_record_triage",
        "mcp__goal-plus__search_create",
    ],
)
def test_pre_tool_use_denies_goal_mutation_without_exact_host_command(
    tmp_path: Path,
    tool_name: str,
) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "ordinary-session",
            "tool_name": tool_name,
            "tool_input": {},
        },
    )

    payload = json.loads(result.stdout)
    specific = payload["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "GoalPlusEntrypointRequired" in specific["permissionDecisionReason"]
    assert not search_root.exists()


@pytest.mark.parametrize("terminal_status", ["complete", "blocked", "abandoned"])
def test_pre_tool_use_allows_linked_report_for_attached_terminal_goal(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    search_root = tmp_path / ".gp"
    runtime, goal_plus_id = _terminal_report_goal(
        search_root,
        status=terminal_status,
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__search_report",
            "tool_input": {"run_id": "run_terminal"},
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert runtime.status(goal_plus_id).hook_counters["pre_tool_use"] == 1


@pytest.mark.parametrize(
    ("session_id", "run_id"),
    [
        ("other-session", "run_terminal"),
        ("session-codex", "run_other"),
    ],
)
def test_pre_tool_use_denies_terminal_report_outside_session_run_binding(
    tmp_path: Path,
    session_id: str,
    run_id: str,
) -> None:
    search_root = tmp_path / ".gp"
    _terminal_report_goal(search_root)

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "mcp__goal-plus__search_report",
            "tool_input": {"run_id": run_id},
        },
    )

    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "GoalPlusEntrypointRequired" in specific["permissionDecisionReason"]


def test_pre_tool_use_denies_terminal_report_for_detached_session(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime, goal_plus_id = _terminal_report_goal(search_root)
    runtime.detach_session(
        goal_plus_id,
        session_id="session-codex",
        reason="test detached terminal session",
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__search_report",
            "tool_input": {"run_id": "run_terminal"},
        },
    )

    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "GoalPlusEntrypointRequired" in specific["permissionDecisionReason"]


def test_ordinary_prompt_detaches_goal_and_requires_explicit_resume(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus Implement requirement A",
        },
    )
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    goal_id = record_path.parent.name

    ordinary = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "What is the current branch?",
        },
    )
    assert ordinary.stdout == ""
    record = FileGoalPlusRuntime(search_root).status(goal_id)
    assert record.active_session is not None
    assert record.active_session.state == "detached"

    denied = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__goal_plus_record_triage",
            "tool_input": {"goal_plus_id": goal_id},
        },
    )
    assert "GoalPlusEntrypointRequired" in json.loads(denied.stdout)[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]


def test_pre_tool_use_ignores_unrelated_tool(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "read_mcp_resource",
        },
    )

    assert result.stdout == ""
    assert runtime.status(record.goal_plus_id).hook_counters == {}


def test_unbound_search_candidate_stop_requires_own_verifier(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": "agent-worker",
            "agent_type": "search_candidate_agent",
        },
    )

    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "必须使用自己的 agent_session_id 完成至少一次 search_run_verifier" in payload[
        "reason"
    ]
    assert "父级负责" in payload["reason"]
    assert runtime.status(record.goal_plus_id).hook_counters["subagent_stop"] == 1


def test_search_candidate_stop_is_owned_by_its_verifier_not_parent_next_action(
    tmp_path: Path,
) -> None:
    search_runtime, run_id, candidate_id, agent_session_id = _codex_search_worker(
        tmp_path
    )
    search_root = search_runtime.root_dir
    goal_runtime = FileGoalPlusRuntime(search_root)
    record = goal_runtime.create_goal("Optimize model throughput")
    goal_runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )
    agent_identity = "search-worker-agent"

    mapped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "agent_id": agent_identity,
            "agent_type": "search_candidate_agent",
            "tool_name": "mcp__goal-plus__search_get_agent_context",
            "tool_input": {"agent_session_id": agent_session_id},
        },
    )
    assert mapped.stdout == ""

    before_verifier = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": agent_identity,
        },
    )
    blocked = json.loads(before_verifier.stdout)
    assert blocked["decision"] == "block"
    assert agent_session_id in blocked["reason"]
    assert "search_run_verifier" in blocked["reason"]

    search_runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=agent_session_id,
        hypothesis="Hook-owned worker evidence",
    )
    after_verifier = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": agent_identity,
            "agent_transcript_path": "/tmp/codex-search-worker.jsonl",
            "model": "gpt-5.5",
        },
    )
    assert after_verifier.stdout == ""
    rebound = search_runtime._load_agent_session_by_id(agent_session_id)
    assert rebound.host_handle.external_id == agent_identity
    assert rebound.host_handle.metadata["session_file"] == "/tmp/codex-search-worker.jsonl"
    assert rebound.host_handle.metadata["model"] == "gpt-5.5"
    assert "subagent_stop_observed_at" in rebound.host_handle.metadata

    subagent_events = _stop_hook_events(search_root)
    assert len(subagent_events) == 2
    assert {event["decision"] for event in subagent_events} == {"allow", "block"}
    for event in subagent_events:
        assert event["hook_event_name"] == "SubagentStop"
        assert event["goal_plus_id"] == record.goal_plus_id
        assert event["host_agent_id"] == agent_identity
        assert event["agent_session_id"] == agent_session_id
        assert event["run_id"] == run_id
        assert event["candidate_id"] == candidate_id

    parent_stop = _run_hook(
        tmp_path,
        search_root,
        {"hook_event_name": "Stop", "session_id": "session-codex"},
    )
    parent_payload = json.loads(parent_stop.stdout)
    assert parent_payload["decision"] == "block"
    assert "判断原始目标" in parent_payload["reason"]
    counters = goal_runtime.status(record.goal_plus_id).hook_counters
    assert counters["subagent_stop"] == 2
    assert counters["stop"] == 1


def test_unlinked_search_candidate_stop_does_not_inherit_unrelated_active_goal(
    tmp_path: Path,
) -> None:
    search_runtime, run_id, candidate_id, agent_session_id = _codex_search_worker(
        tmp_path
    )
    search_root = search_runtime.root_dir
    goal_runtime = FileGoalPlusRuntime(search_root)
    unrelated = goal_runtime.create_goal("Unrelated active Goal Plus task")
    goal_runtime.activate_session(
        unrelated.goal_plus_id,
        {"host": "codex", "session_id": "parent-session"},
    )
    agent_identity = "standalone-search-worker"

    mapped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "agent_id": agent_identity,
            "agent_type": "search_candidate_agent",
            "tool_name": "mcp__goal-plus__search_get_agent_context",
            "tool_input": {"agent_session_id": agent_session_id},
        },
    )
    assert mapped.returncode == 0

    stopped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "child-session",
            "agent_id": agent_identity,
        },
    )

    assert stopped.returncode == 0
    assert stopped.stdout == ""
    [event] = _stop_hook_events(search_root)
    assert event["decision"] == "skipped"
    assert event["goal_plus_id"] is None
    assert event["agent_session_id"] == agent_session_id
    assert event["run_id"] == run_id
    assert event["candidate_id"] == candidate_id


def test_search_candidate_autoresearch_lease_blocks_until_runtime_then_releases(
    tmp_path: Path,
) -> None:
    search_runtime, run_id, candidate_id, agent_session_id = _codex_search_worker(
        tmp_path,
        worker_budget={
            "min_runtime_seconds": 300,
            "min_verifier_runs": 1,
            "max_runtime_seconds": 420,
            "on_exceed": "interrupt",
        },
    )
    search_root = search_runtime.root_dir
    goal_runtime = FileGoalPlusRuntime(search_root)
    record = goal_runtime.create_goal("Run one sustained AutoResearch worker")
    goal_runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "parent-session"},
    )
    goal_runtime.link_search_run(
        record.goal_plus_id,
        search_runtime._load_run(run_id).frozen_spec_id,
        run_id,
    )
    agent_identity = "019f-native-autoresearch-worker"
    session = search_runtime._load_agent_session_by_id(agent_session_id)
    transcript_started_at = datetime.now(timezone.utc)
    transcript_dir = (
        tmp_path
        / "codex-home"
        / "sessions"
        / f"{transcript_started_at.year:04d}"
        / f"{transcript_started_at.month:02d}"
        / f"{transcript_started_at.day:02d}"
    )
    transcript_dir.mkdir(parents=True)
    transcript_path = transcript_dir / "codex-autoresearch-worker.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "timestamp": transcript_started_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "type": "session_meta",
                "payload": {
                    "id": agent_identity,
                    "session_id": agent_identity,
                    "agent_path": session.host_handle.task_name,
                    "message": f"agent_session_id={agent_session_id}",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    search_runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=agent_session_id,
        hypothesis="Hook-owned worker evidence",
    )

    early_stop = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "child-session",
            "agent_id": agent_identity,
            "agent_type": "default",
        },
        CODEX_HOME=str(tmp_path / "codex-home"),
    )
    blocked = json.loads(early_stop.stdout)
    assert blocked["decision"] == "block"
    assert "AutoResearch lease" in blocked["reason"]
    assert "不要返回父级" in blocked["reason"]
    assert "不要休眠或忙等" in blocked["reason"]
    assert "沿用已加载的 candidate-local 证据和 Global Evidence" in blocked["reason"]
    assert "search_get_agent_context" not in blocked["reason"]
    assert "search_get_global_evidence" not in blocked["reason"]
    assert "一句话 hypothesis" in blocked["reason"]
    assert "省略 scope 以使用 process verifier" in blocked["reason"]
    assert "继续深度搜索" in blocked["reason"]
    assert "不要通过重复返回来轮询此 hook" in blocked["reason"]
    assert "把局部饱和视为转向信号" in blocked["reason"]
    assert "与已有尝试不同的实质方向" in blocked["reason"]
    assert "或基于连续结果形成具体的结构性饱和证据时才准备返回" not in blocked[
        "reason"
    ]
    assert "立即再次尝试结束" not in blocked["reason"]
    protocol = [
        "沿用已加载",
        "选择不同",
        "实现修改",
        "search_run_verifier",
        "hypothesis",
    ]
    assert all(step in blocked["reason"] for step in protocol)
    assert [blocked["reason"].index(step) for step in protocol] == sorted(
        blocked["reason"].index(step) for step in protocol
    )

    evidence_path = (
        search_root
        / "host-logs"
        / "codex-autoresearch-leases"
        / f"{agent_session_id}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "active"
    assert evidence["blocked_stop_attempts"] == 1
    assert evidence["parent_closeout_after_seconds"] == 375
    assert evidence["min_runtime_seconds"] == 300
    assert evidence["max_runtime_seconds"] == 420
    assert evidence["start_event"] == "SubagentStartTranscript"
    assert evidence["lease_precedes_parent_closeout"] is True
    assert evidence["lease_precedes_parent_hard_deadline"] is True

    evidence["started_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=301)
    ).isoformat().replace("+00:00", "Z")
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    completed_stop = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "child-session",
            "agent_id": agent_identity,
            "agent_type": "default",
        },
        CODEX_HOME=str(tmp_path / "codex-home"),
    )
    assert completed_stop.stdout == ""
    released = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert released["status"] == "released"
    assert released["release_reason"] == "lease_satisfied"
    assert released["elapsed_seconds"] >= 300
    assert released["released_within_parent_closeout_budget"] is True
    assert released["released_within_max_runtime"] is True
    assert released["blocked_stop_attempts"] == 1
    assert released["stop_attempts"] == 2


def test_ordinary_subagent_stop_does_not_inherit_parent_next_action(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Implement a feature")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "session-codex",
            "agent_id": "ordinary-worker",
            "agent_type": "explorer",
        },
    )

    assert result.stdout == ""
    assert runtime.status(record.goal_plus_id).hook_counters["subagent_stop"] == 1


def test_stop_for_terminal_goal_emits_non_llm_stats(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"host": "codex", "session_id": "session-codex"},
    )
    runtime.set_status(
        record.goal_plus_id,
        status="complete",
        reason="final audit passed",
        evidence=[{"kind": "report", "path": "report.md"}],
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-codex",
            "goal_plus_id": record.goal_plus_id,
        },
    )

    assert result.returncode == 0, result.stderr
    message = json.loads(result.stdout)["systemMessage"]
    assert f"goal_plus_id={record.goal_plus_id}" in message
    assert "status=complete" in message
    assert "phase=intake" in message
    assert "search_tasks=0" in message
    assert "stop=1" in message
