from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from goal_plus import codex_models
import pytest

import goal_plus.goal_plus_stop_hook as hook_module
import goal_plus.internal_agents as internal_agents
from goal_plus.domain.goal_commands import (EXPLORATION_MODE_LINES)
from goal_plus.goal_plus import (FileGoalPlusRuntime)
from goal_plus.host_scope import (
    ensure_execution_scope,
    close_execution_scope,
    complete_codex_spawn,
    execution_scope_for_owner,
    execution_scope_snapshot,
    register_codex_agent,
)
from goal_plus.models import GoalPlusLinkedSearch, SearchSpec
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import (
    make_project,
    run_host_hook_inprocess,
    spec_with_strategy,
    start_host_verified_goal,
)

HOOK_CLI = [
    sys.executable,
    "-m",
    "goal_plus.server",
    "--goal-plus-host-hook",
]
pytestmark = pytest.mark.codex


@pytest.mark.parametrize("tool", [
    "goal_plus_search_run_verifier", "goal_plus_search_get_global_evidence", "goal_plus_search_get_agent_context",
    "goal_plus_search_select", "goal_plus_search_status", "goal_plus_search_invalidate_run", "goal_plus_search_report",
])
def test_post_tool_hook_pumps_internal_agents_from_codex_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    calls: list[tuple[Path, str, str]] = []

    def record(root: Path, run_id: str, *, agent_harness: str) -> dict[str, bool]:
        calls.append((root, run_id, agent_harness))
        return {"annotation_started": True, "verifier_started": True}

    monkeypatch.setattr(internal_agents, "kick_internal_agents", record)
    hook_module._handle_post_tool_lifecycle(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__goal_plus__" + tool,
            "tool_input": {"run_id": "run_host_inherited", "candidate_id": "c001"},
            "tool_response": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"run_id": "run_host_inherited", "process_passed": True}),
                }],
            },
        },
    )

    assert calls == [(tmp_path, "run_host_inherited", "codex")]


def test_subagent_stop_kicks_internal_agents_after_terminal_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, str, str]] = []
    hook_input = {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "low",
    }
    monkeypatch.setenv(internal_agents.CODEX_MODEL_ENV, "before-test")
    monkeypatch.setenv(
        internal_agents.CODEX_REASONING_EFFORT_ENV,
        "before-test",
    )

    monkeypatch.setattr(
        hook_module,
        "_handle_stop_event",
        lambda *_args, **_kwargs: {
            "goal_plus_id": "gp_0001",
            "run_id": "run_host_inherited",
            "agent_session_id": "agent_001",
        },
    )
    monkeypatch.setattr(
        hook_module,
        "_record_codex_subagent_terminal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        hook_module,
        "_record_stop_hook_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_agents,
        "kick_internal_agents",
        lambda root, run_id, *, agent_harness: calls.append((root, run_id, agent_harness)),
    )

    hook_module._run_recorded_stop_event(
        tmp_path,
        hook_input,
        event_name="SubagentStop",
    )

    assert calls == [(tmp_path, "run_host_inherited", "codex")]
    assert os.environ[internal_agents.CODEX_MODEL_ENV] == "gpt-5.6-sol"
    assert os.environ[internal_agents.CODEX_REASONING_EFFORT_ENV] == "low"


def test_child_post_tool_hook_recovers_owning_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_root = tmp_path / ".gp"
    candidate = search_root / "runs" / "run_nested" / "workspace" / "c001"
    candidate.mkdir(parents=True)
    observed: list[Path] = []
    hook_input = {
        "hook_event_name": "PostToolUse",
        "agent_type": "search_candidate_agent",
        "tool_name": "exec",
        "tool_input": (
            "await tools."
            "mcp__goal_plus__goal_plus_search_run_verifier({"
            'run_id:"run_nested",agent_session_id:"agent_nested"});'
        ),
    }
    monkeypatch.chdir(candidate)
    monkeypatch.setattr(hook_module, "_read_hook_input", lambda: hook_input)
    monkeypatch.setattr(
        hook_module,
        "_handle_post_tool_lifecycle",
        lambda root, _input: observed.append(root),
    )

    assert hook_module.main() == 0
    assert observed == [search_root]


def _run_hook(
    tmp_path: Path,
    search_root: Path,
    hook_input: dict,
    **env: str,
) -> object:
    return run_host_hook_inprocess(
        hook_input,
        cwd=tmp_path,
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


def _pre_tool_denial(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    return specific["permissionDecisionReason"]


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
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    record = runtime.status(record.goal_plus_id)
    linked = GoalPlusLinkedSearch(
        goal_revision=record.goal_revision,
        frozen_spec_id="spec_terminal",
        run_id="run_terminal",
        selected_candidate_id="c001" if status == "complete" else None,
        result_recorded_at="2026-01-01T00:00:00Z" if status == "complete" else None,
    )
    runtime.state.write_goal(
        record.model_copy(
            update={
                "phase": "final_audit" if status == "complete" else "search",
                "search_tasks": [linked],
                "linked_search": linked,
            }
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
    turn_started_at: str | None = None,
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
    turn_context = {
        "type": "turn_context",
        "payload": {
            "turn_id": turn_id,
        },
    }
    if turn_started_at is not None:
        turn_context["timestamp"] = turn_started_at
    events.extend([{"not": "valid json line"}, turn_context])
    path.write_text(
        "{broken\n" + "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _codex_search_worker(
    tmp_path: Path,
    *,
    worker_budget: dict | None = None,
    adaptive_search: bool = False,
) -> tuple[FileSearchRuntime, str, str, str]:
    project = make_project(tmp_path)
    payload = spec_with_strategy(project, {"name": "random"}, max_parallel=1).model_dump(mode="json")
    payload["strategy"]["worker_budget"] = worker_budget or {
        "max_runtime_seconds": 600,
        "max_turns": 8,
        "on_exceed": "interrupt",
    }
    if adaptive_search:
        payload["budget"]["max_candidates"] = 2
        payload["workspace"] = {"provider": "git_worktree"}
        payload["strategy"].update(
            {
                "orchestration_mode": "adaptive_search",
                "selection": {
                    "ranking_keys": ["quality_score", "hard_score"],
                    "ranking_tolerance": [0.05, 0.0],
                },
                "verifier": {"scores": ["progress_reward", "allocation_utility"]},
                "allocation": {"policy": "swe_search_uct/v1"},
            }
        )
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(SearchSpec.model_validate(payload), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id, {"goal": "test"})
    old_start = datetime.now(timezone.utc) - timedelta(seconds=90)
    runtime.candidate_sessions.state.write_agent_session(
        session.model_copy(
            update={
                "created_at": old_start.replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            }
        )
    )
    return runtime, run_id, task.candidate_id, session.agent_session_id


def _write_codex_subagent_transcript(
    path: Path,
    *,
    parent_session_id: str,
    child_session_id: str,
    agent_path: str | None = None,
    prefix_lines: list[str] | None = None,
) -> None:
    lines = list(prefix_lines or [])
    thread_spawn = {"parent_thread_id": parent_session_id}
    if agent_path is not None:
        thread_spawn["agent_path"] = agent_path
    lines.append(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": child_session_id,
                    "parent_thread_id": parent_session_id,
                    "thread_source": "subagent",
                    "source": {
                        "subagent": {
                            "thread_spawn": thread_spawn,
                        }
                    },
                },
            }
        )
    )
    path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _candidate_session_id(context: str) -> str:
    match = re.search(r"goal_plus_agent_session_id=(agent_\S+)", context)
    assert match is not None, context
    return match.group(1)


def test_user_prompt_submit_precreates_and_binds_goal(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "low",
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
    assert record.active_session.agent_harness == "codex"
    assert record.active_session.session_id == "session-codex"
    assert record.policy["execution"]["main_reasoning_effort"] == "host_selected"
    assert record.policy["execution"]["host"]["native_reasoning_effort"] == "low"
    assert record.entrypoint_status == "host_verified"
    assert len(record.host_command_invocations) == 1
    assert record.host_command_invocations[0].command == "$goal-plus"
    assert record.host_command_invocations[0].transport == "codex_user_prompt_submit"
    assert record.goal_plus_id in context
    assert record.raw_goal in context
    assert record.control.state == "active"
    assert record.phase == "goal"
    assert record.next_action.kind == "execute_goal"
    assert record.triage is None
    assert "当前未设置最大运行时间" in context
    assert "不要询问或等待确认" in context
    assert "告知后立即继续执行" in context

    edit = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "apply_patch",
            "tool_input": {"patch": "*** Begin Patch\n*** Add File: fix.py\n+pass\n*** End Patch"},
        },
    )
    assert edit.returncode == 0, edit.stderr
    assert edit.stdout == ""
    assert runtime.status(record.goal_plus_id).triage is None


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
            "tool_name": "mcp__goal_plus__goal_plus_search_get_agent_context",
            "tool_input": {"agent_session_id": "agent_demo_001"},
        },
    )

    assert "GoalPlusRoleDenied" in _pre_tool_denial(result)
    assert "systemMessage" not in json.loads(result.stdout)


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
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

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


def test_native_session_followup_finds_existing_parent_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    workspace = repository / "examples" / "circle-packing" / "workspace"
    workspace.mkdir(parents=True)
    (repository / ".git").mkdir()
    runtime = FileGoalPlusRuntime(repository / ".gp")
    record = runtime.create_goal("Optimize the repository")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-parent"},
    )
    env = dict(os.environ)
    for name in (
        "GOAL_PLUS_PROJECT_ROOT",
        "GOAL_PLUS_SEARCH_ROOT",
        "CLAUDE_PROJECT_DIR",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

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
    assert resumed.active_session is not None
    assert resumed.active_session.state == "attached"
    assert [item.action for item in resumed.host_command_invocations] == ["resume"]
    assert not (workspace / ".gp").exists()


def test_user_prompt_submit_accepts_explicit_role_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_root = tmp_path / ".gp"

    def probe_ok(_targets: list[dict[str, str]]) -> None:
        return None

    # Keep this a no-LLM test: the probe drives a real `codex exec` per model.
    monkeypatch.setattr(codex_models, "probe_codex_models", probe_ok)

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
    assert "继承当前 Codex Main 的 native model" in context


def test_codex_model_preflight_deduplicates_and_runs_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    calls: list[tuple[str, str]] = []
    calls_lock = threading.Lock()

    def probe(target: dict[str, str]) -> None:
        with calls_lock:
            calls.append(
                (
                    target["model"],
                    target["reasoning_effort"],
                )
            )
        barrier.wait(timeout=2)
        return None

    monkeypatch.setattr(codex_models, "probe_codex_model", probe)
    targets = codex_models.command_codex_model_targets(
        {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "low",
        },
        {
            "annotator": "gpt-5.6-sol",
            "workers": "gpt-5.6-terra,gpt-5.6-terra",
        },
    )

    codex_models.probe_codex_models(targets)

    assert sorted(calls) == [
        ("gpt-5.6-sol", "low"),
        ("gpt-5.6-terra", "low"),
    ]


def test_codex_model_preflight_skips_native_defaults() -> None:
    hook_input = {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "low",
    }

    assert codex_models.command_codex_model_targets(hook_input, None) == []
    assert codex_models.search_spec_codex_model_targets(
        {
            **hook_input,
            "tool_input": {
                "spec": {
                    "strategy": {
                        "models": [],
                        "evidence_annotator": {},
                    }
                }
            },
        }
    ) == []


def test_codex_model_probe_only_uses_native_model_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(codex_models.subprocess, "run", run)

    assert codex_models.probe_codex_model(
        {"model": "gpt-5.6-sol", "reasoning_effort": "low"}
    ) is None
    assert commands[0][commands[0].index("--model") + 1] == "gpt-5.6-sol"
    assert not any("model_provider" in value for value in commands[0])


def test_codex_model_admission_does_not_probe_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)

    probe_called = False

    def reject(_targets: list[dict[str, str]]) -> None:
        nonlocal probe_called
        probe_called = True
        raise AssertionError("provider probing is not part of Goal admission")

    monkeypatch.setattr(codex_models, "probe_codex_models", reject)
    hook_module._handle_user_prompt_submit(
        runtime,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "model": "gpt-5.6-sol",
            "prompt": "$goal-plus workers=gpt-broken optimize throughput",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload.get("decision") != "block"
    assert not probe_called
    assert list((search_root / "goal-plus").glob("gp_*/goal.json"))


def test_codex_search_freeze_preflight_covers_explicit_model_roles() -> None:
    targets = codex_models.search_spec_codex_model_targets(
        {
            "model": "gpt-main",
            "reasoning_effort": "low",
            "tool_input": {
                "spec": {
                    "strategy": {
                        "models": [
                            {"model": "gpt-worker", "reasoning_effort": "medium"},
                            {"model": "gpt-main"},
                        ],
                        "worker_launch": {"model": "gpt-worker"},
                        "evidence_annotator": {
                            "model": "gpt-annotator",
                        },
                    }
                }
            },
        }
    )

    assert {(target["model"], target["reasoning_effort"]) for target in targets} == {
        ("gpt-main", "low"),
        ("gpt-worker", "medium"),
        ("gpt-annotator", "low"),
    }


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
    record = FileGoalPlusRuntime(search_root).status(records[0].parent.name)
    assert record.goal_plus_id in first_context
    assert record.goal_plus_id in second_context
    assert len(record.host_command_invocations) == 1
    assert record.goal_revision == 1


def test_user_prompt_submit_starts_independent_goals_for_different_sessions(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"

    first = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-first",
            "prompt": "$goal-plus Optimize throughput",
        },
    )
    second = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-second",
            "prompt": "$goal-plus Optimize latency",
        },
    )

    first_context = _additional_context(first, "UserPromptSubmit")
    second_context = _additional_context(second, "UserPromptSubmit")
    runtime = FileGoalPlusRuntime(search_root)
    records = [
        runtime.status(path.parent.name)
        for path in sorted((search_root / "goal-plus").glob("gp_*/goal.json"))
    ]
    assert len(records) == 2
    assert {record.active_session.session_id for record in records} == {
        "session-first",
        "session-second",
    }
    assert records[0].goal_plus_id in first_context
    assert records[1].goal_plus_id in second_context


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
    assert "goal-plus" in context


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
    assert goal_id in _additional_context(restored, "SessionStart")
    preserved = FileGoalPlusRuntime(search_root).status(goal_id)
    assert preserved.active_session is not None
    assert preserved.active_session.state == "attached"

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


def test_native_codex_resume_requires_explicit_goal_plus_resume(tmp_path: Path) -> None:
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

    interrupted = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Interrupt",
            "session_id": "session-codex",
        },
    )
    assert interrupted.stdout == ""
    paused = FileGoalPlusRuntime(search_root).status(goal_id)
    assert paused.active_session is not None
    assert paused.active_session.state == "attached"
    assert paused.control.state == "paused"

    resumed = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SessionStart",
            "session_id": "session-codex",
        },
    )
    assert goal_id in _additional_context(resumed, "SessionStart")
    still_paused = FileGoalPlusRuntime(search_root).status(goal_id)
    assert still_paused.active_session is not None
    assert still_paused.control.state == "paused"

    continued = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "Continue from where we stopped.",
        },
    )
    assert continued.stdout == ""
    assert FileGoalPlusRuntime(search_root).status(goal_id).control.state == "paused"
    resumed = _run_hook(tmp_path, search_root, {
        "hook_event_name": "UserPromptSubmit", "session_id": "session-codex",
        "prompt": "$goal-plus resume",
    })
    context = _additional_context(resumed, "UserPromptSubmit")
    record = FileGoalPlusRuntime(search_root).status(goal_id)
    assert record.goal_revision == 1
    assert record.raw_goal == (
        "Implement requirement A\n\n"
        + EXPLORATION_MODE_LINES["autonomous"]
    )
    assert goal_id in context
    assert record.active_session is not None
    assert record.active_session.state == "attached"
    assert [item.action for item in record.host_command_invocations] == ["start", "resume"]


def test_codex_subagent_settlement_and_resume_reuse_scope(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    _run_hook(tmp_path, search_root, {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "low",
        "prompt": "$goal-plus Prepare work",
    })
    runtime = FileGoalPlusRuntime(search_root)
    goal_id = next((search_root / "goal-plus").glob("gp_*/goal.json")).parent.name
    record = runtime.status(goal_id)
    old_scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",
        run_id=goal_id,
        activation_id=f"{goal_id}:{record.control.version}",
        native_agent_config={"model": "gpt-5.6-sol", "reasoning_effort": "low"},
    )
    register_codex_agent(
        root_dir=search_root,
        scope_id=old_scope["scope_id"],
        agent_id="/root/completed_worker",
        label="completed_worker",
        run_id=goal_id,
    )
    parent_transcript = tmp_path / "main.jsonl"
    _write_codex_rollout(parent_transcript, turn_id="turn-main")
    child_transcript = tmp_path / "completed-worker.jsonl"
    _write_codex_subagent_transcript(
        child_transcript,
        parent_session_id="session-codex",
        child_session_id="child-session",
        agent_path="completed_worker",
    )
    stopped = _run_hook(tmp_path, search_root, {
        "hook_event_name": "SubagentStop",
        "session_id": "child-session",
        "agent_id": "unmatched-native-uuid",
        "agent_type": "explorer",
        "transcript_path": str(parent_transcript),
        "agent_transcript_path": str(child_transcript),
    })
    assert stopped.returncode == 0, stopped.stderr
    [terminal] = execution_scope_snapshot(
        root_dir=search_root, scope_id=old_scope["scope_id"]
    )["codex_agents"]
    assert terminal["status"] == "finished"
    [event_path] = (search_root / "host-logs" / "codex-hook-events").glob("*.json")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["native_agent_reconciliation"] == {
        "status": "settled",
        "scope_id": old_scope["scope_id"],
        "agent_id": "/root/completed_worker",
    }
    assert close_execution_scope(
        root_dir=search_root, scope_id=old_scope["scope_id"], reason="aborted"
    )["state"] == "closed"
    _run_hook(tmp_path, search_root, {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-codex",
        "prompt": "$goal-plus resume",
    })

    allowed = _run_hook(tmp_path, search_root, {
        "hook_event_name": "PreToolUse",
        "session_id": "session-codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "low",
        "tool_name": "functions.collaboration.spawn_agent",
        "tool_use_id": "spawn-resumed",
        "tool_input": {"task_name": "worker", "message": "prepare work"},
    })
    assert allowed.stdout == ""
    current = execution_scope_for_owner(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",
    )
    assert current is not None
    assert current["scope_id"] != old_scope["scope_id"]
    assert current["state"] == "open"
    assert current["codex_agents"][0]["spawn_pending"] is True

    second = _run_hook(tmp_path, search_root, {
        "hook_event_name": "PreToolUse",
        "session_id": "session-codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "low",
        "tool_name": "functions.collaboration.spawn_agent",
        "tool_use_id": "spawn-resumed-second",
        "tool_input": {"task_name": "worker_second", "message": "prepare more work"},
    })
    assert second.stdout == ""
    reused = execution_scope_for_owner(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",
    )
    assert reused is not None
    assert reused["scope_id"] == current["scope_id"]
    assert [item["agent_id"] for item in reused["codex_agents"]] == [
        "/root/worker",
        "/root/worker_second",
    ]


def test_bound_codex_final_checker_role_and_stop(tmp_path: Path, monkeypatch) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal(
        "Ship it",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
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
    from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity

    identity = GoalPlusFinalCheckerIdentity(goal_plus_id=record.goal_plus_id, check_id=request["check"]["check_id"], goal_revision=1)
    runtime.goal_completion.bind_native_checker(identity, agent_harness="codex", native_session_id="checker-1",
                                                scope_id="scope-checker", control_version=record.control.version)
    checker_context = {"session_id": "checker-1"}

    main_submission = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__goal_plus_submit_final_check",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert "GoalPlusRoleDenied" in _pre_tool_denial(main_submission)
    monkeypatch.setenv("GOAL_PLUS_FINAL_CHECK_IDENTITY", identity.model_dump_json())

    checker_status = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            **checker_context,
            "tool_name": "mcp__goal-plus__goal_plus_status",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert checker_status.stdout == ""
    checker_mutation = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            **checker_context,
            "tool_name": "mcp__goal-plus__goal_plus_set_status",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert "GoalPlusRoleDenied" in _pre_tool_denial(checker_mutation)

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            **checker_context,
        },
    )
    assert result.stdout == ""
    assert runtime.status(record.goal_plus_id).final_checks[-1].status == "pending"


def test_codex_stop_blocks_goal_mode_until_required_check_passes(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal(
        "Ship it",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
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
    assert "goal_plus_run_final_check" in payload["reason"]


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
        {"agent_harness": "codex", "session_id": "session-codex"},
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


@pytest.mark.host_process
@pytest.mark.parametrize(
    "prefix",
    ["", "collaboration", "functions.", "collaboration.", "functions.collaboration."],
)
def test_post_tool_tracks_and_interrupts_codex_native_agent(
    tmp_path: Path,
    prefix: str,
) -> None:
    tool_name = prefix + "spawn_agent"
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = start_host_verified_goal(search_root, tmp_path, session_id="session-codex")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",

        run_id=record.goal_plus_id,

    )

    launch = {
        "hook_event_name": "PreToolUse", "session_id": "session-codex",
        "tool_name": tool_name, "tool_use_id": "spawn-1",
        "tool_input": {"task_name": "worker", "message": "bounded work"},
    }
    hook_module._validate_codex_main_session(search_root, record.goal_plus_id, launch)
    hook_module._prepare_codex_native_tool(search_root, launch)
    assert execution_scope_snapshot(root_dir=search_root, scope_id=scope["scope_id"])["codex_agents"][0]["spawn_pending"] is True
    spawned = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-codex",
            "tool_name": tool_name,
            "tool_use_id": "spawn-1",
            "tool_input": {"task_name": "worker", "message": "bounded work"},
            "tool_response": '{"task_name":"/root/worker"}',
        },
    )

    assert spawned.returncode == 0, spawned.stderr
    tracked = execution_scope_snapshot(
        root_dir=search_root,
        scope_id=scope["scope_id"],
    )["codex_agents"]
    assert tracked == [
        {
            "agent_id": "/root/worker",
            "label": "worker",
            "run_id": record.goal_plus_id,
            "status": "active",
            "registered_at": tracked[0]["registered_at"],
            "updated_at": tracked[0]["updated_at"],
            "spawn_tool_use_id": "spawn-1",
            "spawn_pending": False,
            "parent_session_id": "session-codex",
            "native_agent_id": "/root/worker",
        }
    ]

    failed_interrupt = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-codex",
            "tool_name": prefix + "interrupt_agent",
            "tool_input": {"target": "/root/worker"},
            "tool_response": {"error": "target is still running"},
        },
    )
    assert failed_interrupt.returncode == 0
    assert "did not return a successful previous_status" in failed_interrupt.stderr
    [still_active] = execution_scope_snapshot(
        root_dir=search_root,
        scope_id=scope["scope_id"],
    )["codex_agents"]
    assert still_active["status"] == "active"

    hook_module._prepare_codex_native_tool(search_root, {
        "hook_event_name": "PreToolUse", "session_id": "session-codex", "tool_name": prefix + "interrupt_agent",
        "tool_use_id": "interrupt-1", "tool_input": {"target": "/root/worker"},
    })
    interrupted = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-codex",
            "tool_name": prefix + "interrupt_agent" if prefix else "functions.collaboration.interrupt_agent",
            "tool_use_id": "interrupt-1",
            "tool_input": {"target": "/root/worker"},
            "tool_response": {"previous_status": "running"},
        },
    )

    assert interrupted.returncode == 0, interrupted.stderr
    [terminal] = execution_scope_snapshot(
        root_dir=search_root,
        scope_id=scope["scope_id"],
    )["codex_agents"]
    assert terminal["status"] == "interrupted"
    assert terminal["finished_at"]

    def native(tool: str, event: str, call_id: str, response=None):
        return _run_hook(tmp_path, search_root, {
            "hook_event_name": event, "session_id": "session-codex", "tool_name": prefix + tool,
            "tool_use_id": call_id, "tool_input": {"target": "/root/worker", "message": "Continue"},
            "tool_response": response,
        })

    assert native("list_agents", "PreToolUse", "stale-list").stdout == ""
    assert native("followup_task", "PreToolUse", "wake-1").stdout == ""
    assert native("followup_task", "PostToolUse", "wake-1", {}).stderr == ""
    completed = {"agents": [{"agent_name": "/root/worker", "agent_status": {"completed": "done"}}]}
    assert native("list_agents", "PostToolUse", "stale-list", completed).stderr == ""
    late_stop = _run_hook(tmp_path, search_root, {
        "hook_event_name": "SubagentStop", "session_id": "child-session", "agent_id": "/root/worker",
        "agent_type": "explorer",
    })
    assert late_stop.returncode == 0, late_stop.stderr
    current = execution_scope_snapshot(root_dir=search_root, scope_id=scope["scope_id"])["codex_agents"][0]
    assert current["status"] == "active"
    assert current["wake_generation"] == 1
    assert current["wake_pending"] is False
    [event_path] = (search_root / "host-logs" / "codex-hook-events").glob("*.json")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["native_agent_reconciliation"]["status"] == "terminal_observation_rejected"
    assert native("list_agents", "PreToolUse", "fresh-list").stdout == ""
    assert native("list_agents", "PostToolUse", "fresh-list", completed).stderr == ""
    assert execution_scope_snapshot(root_dir=search_root, scope_id=scope["scope_id"])["codex_agents"][0]["status"] == "finished"
    assert close_execution_scope(root_dir=search_root, scope_id=scope["scope_id"], reason="test")["state"] == "closed"
    assert "not open" in _pre_tool_denial(native("followup_task", "PreToolUse", "wake-2"))


@pytest.mark.host_process
def test_subagent_stop_uses_agent_transcript_to_finish_canonical_task(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = start_host_verified_goal(search_root, tmp_path, session_id="session-codex")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",

        run_id=record.goal_plus_id,

    )
    register_codex_agent(
        root_dir=search_root,
        scope_id=scope["scope_id"],
        agent_id="/root/worker",
        label="worker",
        run_id=record.goal_plus_id,
    )
    transcript = tmp_path / "worker.jsonl"
    _write_codex_rollout(
        transcript,
        turn_id="turn-worker",
        agent_path="worker",
    )

    stopped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "child-session",
            "agent_id": "native-agent-uuid",
            "agent_type": "explorer",
            "agent_transcript_path": str(transcript),
        },
    )

    assert stopped.returncode == 0, stopped.stderr
    [terminal] = execution_scope_snapshot(
        root_dir=search_root,
        scope_id=scope["scope_id"],
    )["codex_agents"]
    assert terminal["agent_id"] == "/root/worker"
    assert terminal["status"] == "finished"

    register_codex_agent(
        root_dir=search_root, scope_id=scope["scope_id"], agent_id="/root/worker/nested",
        label="nested", run_id=record.goal_plus_id,
    )
    hook_module._prepare_codex_native_tool(search_root, {
        "hook_event_name": "PreToolUse", "session_id": "child-session", "agent_type": "explorer",
        "agent_id": "native-agent-uuid", "agent_transcript_path": str(transcript),
        "tool_name": "followup_task", "tool_use_id": "nested-wake",
        "tool_input": {"target": "nested", "message": "Continue"},
    })
    nested = execution_scope_snapshot(root_dir=search_root, scope_id=scope["scope_id"])["codex_agents"][1]
    assert nested["wake_pending"] is True


@pytest.mark.host_process
def test_subagent_stop_uses_parent_session_to_disambiguate_canonical_task(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    first = start_host_verified_goal(
        search_root,
        tmp_path,
        session_id="session-a",
        invocation_id="codex:a:start",
    )
    second = start_host_verified_goal(
        search_root,
        tmp_path,
        session_id="session-b",
        invocation_id="codex:b:start",
    )
    first_scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-a",
        run_id=first.goal_plus_id,
    )
    second_scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-b",
        run_id=second.goal_plus_id,
    )
    for scope, parent_session_id, run_id, tool_use_id, native_agent_id in (
        (first_scope, "session-a", first.goal_plus_id, "spawn-a", "native-a"),
        (second_scope, "session-b", second.goal_plus_id, "spawn-b", "native-b"),
    ):
        register_codex_agent(
            root_dir=search_root,
            scope_id=scope["scope_id"],
            agent_id="/root/worker",
            label="worker",
            run_id=run_id,
            tool_use_id=tool_use_id,
            parent_session_id=parent_session_id,
        )
        complete_codex_spawn(
            root_dir=search_root,
            scope_id=scope["scope_id"],
            tool_use_id=tool_use_id,
            agent_id=native_agent_id,
        )
    parent_transcript = tmp_path / "main-a.jsonl"
    _write_codex_rollout(parent_transcript, turn_id="turn-main-a")
    transcript = tmp_path / "worker-a.jsonl"
    _write_codex_subagent_transcript(
        transcript,
        parent_session_id="session-a",
        child_session_id="child-a",
        agent_path="worker",
    )

    stopped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "child-a",
            "agent_id": "unmatched-native-uuid",
            "agent_type": "explorer",
            "transcript_path": str(parent_transcript),
            "agent_transcript_path": str(transcript),
        },
    )

    assert stopped.returncode == 0, stopped.stderr
    assert execution_scope_snapshot(
        root_dir=search_root, scope_id=first_scope["scope_id"]
    )["codex_agents"][0]["status"] == "finished"
    assert execution_scope_snapshot(
        root_dir=search_root, scope_id=second_scope["scope_id"]
    )["codex_agents"][0]["status"] == "active"
    [event_path] = (search_root / "host-logs" / "codex-hook-events").glob("*.json")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["native_agent_reconciliation"] == {
        "status": "settled",
        "scope_id": first_scope["scope_id"],
        "agent_id": "/root/worker",
    }


@pytest.mark.host_process
def test_subagent_stop_reports_conflicting_native_and_canonical_identities(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = start_host_verified_goal(search_root, tmp_path, session_id="session-codex")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",
        run_id=record.goal_plus_id,
    )
    register_codex_agent(
        root_dir=search_root,
        scope_id=scope["scope_id"],
        agent_id="/root/worker",
        label="worker",
        run_id=record.goal_plus_id,
    )
    register_codex_agent(
        root_dir=search_root,
        scope_id=scope["scope_id"],
        agent_id="/root/other",
        label="other",
        run_id=record.goal_plus_id,
        tool_use_id="spawn-other",
        parent_session_id="session-codex",
    )
    complete_codex_spawn(
        root_dir=search_root,
        scope_id=scope["scope_id"],
        tool_use_id="spawn-other",
        agent_id="native-other-agent",
    )
    parent_transcript = tmp_path / "main.jsonl"
    _write_codex_rollout(parent_transcript, turn_id="turn-main")
    child_transcript = tmp_path / "worker.jsonl"
    _write_codex_rollout(child_transcript, turn_id="turn-worker", agent_path="worker")

    stopped = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "child-session",
            "agent_id": "native-other-agent",
            "agent_type": "explorer",
            "transcript_path": str(parent_transcript),
            "agent_transcript_path": str(child_transcript),
        },
    )

    assert stopped.returncode == 0, stopped.stderr
    assert [
        item["status"]
        for item in execution_scope_snapshot(
            root_dir=search_root, scope_id=scope["scope_id"]
        )["codex_agents"]
    ] == ["active", "active"]
    [event_path] = (search_root / "host-logs" / "codex-hook-events").glob("*.json")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["native_agent_reconciliation"] == {
        "status": "identity_conflict",
        "scope_id": scope["scope_id"],
        "agent_ids": ["/root/other", "/root/worker"],
    }


@pytest.mark.host_process
def test_aborted_stop_blocks_until_codex_children_are_interrupted(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Abort without leaving native children")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",

        run_id=record.goal_plus_id,

    )
    register_codex_agent(
        root_dir=search_root,
        scope_id=scope["scope_id"],
        agent_id="/root/worker",
        label="/root/worker",
        run_id=record.goal_plus_id,
    )

    blocked = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-codex",
            "stop_reason": "aborted",
        },
    )

    reason = _block_reason(blocked)
    assert "list_agents" in reason
    assert "interrupt_agent" in reason
    assert runtime.status(record.goal_plus_id).status == "active"
    assert runtime.status(record.goal_plus_id).active_session.state == "attached"

    interrupted = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-codex",
            "tool_name": "interrupt_agent",
            "tool_input": {"target": "/root/worker"},
            "tool_response": {"previous_status": "running"},
        },
    )
    assert interrupted.returncode == 0, interrupted.stderr
    released = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-codex",
            "stop_reason": "aborted",
        },
    )
    assert released.returncode == 0, released.stderr
    assert released.stdout == ""
    assert execution_scope_snapshot(
        root_dir=search_root,
        scope_id=scope["scope_id"],
    )["state"] == "closed"
    assert runtime.status(record.goal_plus_id).status == "active"
    assert runtime.status(record.goal_plus_id).active_session.state == "attached"


@pytest.mark.host_process
@pytest.mark.parametrize("stop_reason", ["aborted", None])
def test_stop_closes_scope_for_detached_goal_without_durable_mutation(
    tmp_path: Path,
    stop_reason: str | None,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Abort a detached native session")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    runtime.detach_session(
        record.goal_plus_id,
        session_id="session-codex",
        reason="test_detach",
    )
    before = runtime.status(record.goal_plus_id).model_dump(mode="json")
    scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",

        run_id=record.goal_plus_id,

    )

    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "session-codex",
    }
    if stop_reason is not None:
        hook_input["stop_reason"] = stop_reason
    stopped = _run_hook(
        tmp_path,
        search_root,
        hook_input,
    )

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == ""
    assert execution_scope_snapshot(
        root_dir=search_root,
        scope_id=scope["scope_id"],
    )["state"] == "closed"
    assert runtime.status(record.goal_plus_id).model_dump(mode="json") == before


@pytest.mark.host_process
def test_terminal_goal_stop_waits_for_codex_scope_quiescence(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Finish after child cleanup")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    runtime.set_status(
        record.goal_plus_id,
        status="complete",
        reason="verified",
        evidence=[{"kind": "test", "path": "evidence.json"}],
    )
    scope = ensure_execution_scope(
        root_dir=search_root,
        agent_harness="codex",
        owner_session_id="session-codex",

        run_id=record.goal_plus_id,

    )
    register_codex_agent(
        root_dir=search_root,
        scope_id=scope["scope_id"],
        agent_id="/root/final_worker",
        label="/root/final_worker",
        run_id=record.goal_plus_id,
    )

    blocked = _run_hook(
        tmp_path,
        search_root,
        {"hook_event_name": "Stop", "session_id": "session-codex"},
    )
    assert "interrupt_agent" in _block_reason(blocked)
    assert runtime.status(record.goal_plus_id).active_session.state == "attached"

    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-codex",
            "tool_name": "interrupt_agent",
            "tool_input": {"target": "/root/final_worker"},
            "tool_response": {"previous_status": "running"},
        },
    )
    released = _run_hook(
        tmp_path,
        search_root,
        {"hook_event_name": "Stop", "session_id": "session-codex"},
    )
    assert "systemMessage" in json.loads(released.stdout)
    assert runtime.status(record.goal_plus_id).active_session.state == "detached"


@pytest.mark.parametrize(
    "source", [None, "startup", "resume", "clear", "future", "compact"]
)
def test_session_start_preserves_same_session_without_authorizing_new_session(
    tmp_path: Path,
    source: str | None,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )

    hook_input = {
        "hook_event_name": "SessionStart",
        "session_id": "session-codex",
    }
    if source is not None:
        hook_input["source"] = source
    result = _run_hook(tmp_path, search_root, hook_input)

    assert record.goal_plus_id in _additional_context(result, "SessionStart")
    updated = runtime.status(record.goal_plus_id)
    assert updated.active_session is not None
    assert updated.active_session.state == "attached"
    assert updated.active_session.session_id == "session-codex"

    new_session = _run_hook(
        tmp_path,
        search_root,
        {"hook_event_name": "SessionStart", "session_id": "session-new"},
    )
    assert new_session.stdout == ""
    preserved = runtime.status(record.goal_plus_id)
    assert preserved.active_session is not None
    assert preserved.active_session.state == "attached"
    assert preserved.active_session.session_id == "session-codex"

    denied = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-new",
            "tool_name": "mcp__goal-plus__goal_plus_record_triage",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert "GoalPlusEntrypointRequired" in json.loads(denied.stdout)[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]


@pytest.mark.parametrize("terminal", [False, True])
def test_compaction_restores_overview_without_granting_execution(tmp_path: Path, terminal: bool) -> None:
    search_root = tmp_path / ".gp"
    before = None
    if terminal:
        runtime = FileGoalPlusRuntime(search_root)
        record = runtime.create_goal("Completed work")
        runtime.activate_session(record.goal_plus_id, {"agent_harness": "codex", "session_id": "session-codex"})
        runtime.set_status(record.goal_plus_id, status="complete", reason="done")
        before = runtime.status(record.goal_plus_id).model_dump(mode="json")
    result = _run_hook(tmp_path, search_root, {
        "hook_event_name": "SessionStart", "source": "compact", "session_id": "session-codex",
    })
    overview = (Path(__file__).parents[1] / "assets/goal-plus-instructions.txt").read_text().strip()
    assert overview in _additional_context(result, "SessionStart")
    if terminal:
        assert runtime.status(record.goal_plus_id).model_dump(mode="json") == before
    else:
        assert not search_root.exists()


def test_compaction_session_start_preserves_authorized_goal_turn(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "turn-goal",
            "prompt": "$goal-plus Optimize model throughput",
        },
    )
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    runtime = FileGoalPlusRuntime(search_root)

    compacted = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "session-codex",
            "turn_id": "turn-goal",
        },
    )

    assert record_path.parent.name in _additional_context(compacted, "SessionStart")
    active = runtime.status(record_path.parent.name).active_session
    assert active is not None
    assert active.state == "attached"

    next_tool = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "turn_id": "turn-goal",
            "tool_name": "mcp__goal-plus__goal_plus_record_triage",
            "tool_input": {"goal_plus_id": record_path.parent.name},
        },
    )
    assert next_tool.stdout == ""


@pytest.mark.parametrize("mode", ["plan", "default"])
def test_stop_uses_collaboration_mode_from_exact_native_turn(tmp_path: Path, mode: str) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Finish the task")
    runtime.activate_session(record.goal_plus_id, {"agent_harness": "codex", "session_id": "session-codex"})
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("\n".join(json.dumps({
        "type": "turn_context",
        "payload": {"turn_id": turn, "collaboration_mode": {"mode": value}},
    }) for turn, value in [("older", "plan"), ("current", mode)]))
    result = _run_hook(tmp_path, search_root, {
        "hook_event_name": "Stop", "session_id": "session-codex",
        "turn_id": "current", "transcript_path": str(transcript),
    })
    assert result.returncode == 0, result.stderr
    if mode == "plan":
        assert result.stdout == ""
    else:
        assert json.loads(result.stdout)["decision"] == "block"
    assert runtime.status(record.goal_plus_id).control.state == "active"


def test_child_hooks_do_not_inherit_or_rebind_main_goal_context(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {
            "agent_harness": "codex",
            "session_id": "session-main",
            "transcript_path": "/tmp/main.jsonl",
        },
    )
    main_binding = runtime.status(record.goal_plus_id).active_session
    assert main_binding is not None
    expected_binding = main_binding.model_dump(mode="json")
    transcript = tmp_path / "ordinary-subagent.jsonl"
    _write_codex_subagent_transcript(
        transcript,
        parent_session_id="session-main",
        child_session_id="child-ordinary",
        prefix_lines=["not-json", *[json.dumps({"type": "header"})] * 8],
    )

    for hook_input in (
        {
            "hook_event_name": "SessionStart",
            "session_id": "session-main",
            "transcript_path": str(transcript),
        },
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-main",
            "transcript_path": str(transcript),
            "prompt": "Inspect this isolated work item.",
        },
    ):
        result = _run_hook(tmp_path, search_root, hook_input)
        assert result.returncode == 0
        assert result.stdout == ""

    pre_tool = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-main",
            "transcript_path": str(transcript),
            "tool_name": "mcp__goal-plus__goal_plus_search_create",
            "tool_input": {"frozen_spec_id": "spec_missing"},
        },
    )
    assert "GoalPlusRoleDenied" in _pre_tool_denial(pre_tool)

    child_goal = runtime.create_goal("Child-owned probe")
    post_tool = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-main",
            "transcript_path": str(transcript),
            "tool_name": "mcp__goal-plus__goal_plus_create",
            "tool_response": {"goal_plus_id": child_goal.goal_plus_id},
        },
    )
    assert post_tool.returncode == 0
    assert post_tool.stdout == ""
    assert runtime.status(child_goal.goal_plus_id).active_session is None

    stop = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-main",
            "transcript_path": str(transcript),
        },
    )
    assert stop.returncode == 0
    assert stop.stdout == ""

    active = runtime.status(record.goal_plus_id).active_session
    assert active is not None
    assert active.model_dump(mode="json") == expected_binding
    assert runtime.status(record.goal_plus_id).hook_counters == {}


@pytest.mark.host_process
def test_post_tool_time_advisory_only_targets_bound_candidate_once(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidate_id, agent_session_id = _codex_search_worker(tmp_path)
    search_root = runtime.root_dir
    runtime.bind_agent_handle(agent_session_id, {
        "external_id": "search-worker-agent", "metadata": {"transport": "app-server"},
    })
    context_tool = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__goal-plus__goal_plus_search_get_agent_context",
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
                "session_id": "search-worker-agent",
                "agent_type": "search_candidate_agent",
            "tool_name": "functions.exec",
            "tool_input": (
                "const r = await tools.mcp__goal_plus__goal_plus_search_get_agent_context("
                f'{{agent_session_id:"{agent_session_id}"}}); text(r);'
            ),
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
        GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID=agent_session_id,
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
            "session_id": "search-worker-agent",
            "agent_type": "search_candidate_agent",
            "tool_name": "read",
            "tool_input": {"path": "initial_program.py"},
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
        GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID=agent_session_id,
    )

    context = _additional_context(advisory, "PostToolUse")
    assert context
    assert candidate_id in context

    repeated = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "search-worker-agent",
            "agent_type": "search_candidate_agent",
            "tool_name": "edit",
            "tool_input": {"path": "initial_program.py"},
        },
        GOAL_PLUS_OUTER_DEADLINE_AT="1970-01-01T00:00:00Z",
        GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID=agent_session_id,
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


def test_post_tool_time_advisory_uses_current_codex_turn_budget(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidate_id, agent_session_id = _codex_search_worker(
        tmp_path,
        worker_budget={
            "max_runtime_seconds": 60,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
    )
    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=agent_session_id,
        hypothesis="Current-turn deadline sample",
    )
    runtime.bind_agent_handle(agent_session_id, {
        "external_id": "worker-native-session", "metadata": {"transport": "app-server"},
    })
    transcript = tmp_path / "rollout-worker-deadline.jsonl"
    _write_codex_rollout(
        transcript,
        turn_id="turn-worker-deadline",
        turn_started_at=(datetime.now(timezone.utc) - timedelta(seconds=90))
        .isoformat()
        .replace("+00:00", "Z"),
    )

    advisory = _run_hook(
        tmp_path,
        runtime.root_dir,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "worker-native-session",
            "turn_id": "turn-worker-deadline",
            "transcript_path": str(transcript),
            "tool_name": "read",
            "tool_input": {"path": "initial_program.py"},
        },
        GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID=agent_session_id,
    )

    assert _additional_context(advisory, "PostToolUse")
    evidence_path = (
        runtime.root_dir
        / "host-logs"
        / "codex-time-advisory"
        / "sent"
        / f"{agent_session_id}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["deadline_source"] == "host_worker_deadline"
    assert evidence["remaining_seconds"] == 0


def test_pre_tool_use_blocks_search_before_spec_is_ready(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__goal_plus_search_create",
            "tool_input": {},
        },
    )

    payload = json.loads(result.stdout)
    specific = payload["hookSpecificOutput"]
    assert specific == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "GoalPlusOwnershipMismatch: tool goal_plus_id does not match the host-authorized Goal Plus record",
    }
    assert runtime.status(record.goal_plus_id).hook_counters["pre_tool_use"] == 1


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__goal-plus__goal_plus_create",
        "mcp__goal-plus__goal_plus_record_triage",
        "mcp__goal-plus__goal_plus_search_create",
        "mcp__goal_plus__future_action_without_prefix",
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


def test_tool_schema_lookup_does_not_require_or_create_goal(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    result = _run_hook(tmp_path, search_root, {
        "hook_event_name": "PreToolUse", "session_id": "main-session",
        "tool_name": "mcp__goal_plus__goal_plus_get_tool_schema",
        "tool_input": {"tool_name": "goal_plus_search_freeze_spec"},
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not search_root.exists()


def test_pre_tool_use_gates_nested_invocation_not_code_text(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    nested_mutation = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "ordinary-session",
            "tool_name": "functions.exec",
            "tool_input": (
                "await tools.mcp__goal_plus__goal_plus_set_status("
                "{goal_plus_id: 'gp_9999', status: 'complete'});"
            ),
        },
    )
    ordinary_child_command = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "agent_id": "ordinary-child",
            "tool_name": "functions.exec",
            "tool_input": "await tools.exec_command({cmd: 'rg goal_plus_search_run_verifier src/'});",
        },
    )

    # The outer script is not proof of invocation (it may be a string/comment).
    assert nested_mutation.stdout == ""
    assert ordinary_child_command.stdout == ""
    actual_invocation = _run_hook(
        tmp_path, search_root, {
            "hook_event_name": "PreToolUse", "session_id": "ordinary-session",
            "tool_name": "mcp__goal_plus__goal_plus_set_status",
            "tool_input": {"goal_plus_id": "gp_9999", "status": "complete"},
        },
    )
    assert "GoalPlusEntrypointRequired" in _pre_tool_denial(actual_invocation)
    assert not search_root.exists()


@pytest.mark.parametrize("tool_name", [
    "mcp__codex_apps__gmail__search_emails",
    "mcp__foreign__goal_plus_search_create", "mcp__foreign__goal_plus_create",
    "mcp__foreign__goal_plus_search_prepare_recovery", "mcp__foreign__apply_patch",
    "mcp__foreign__spawn_agent", "foreign.goal_plus_search_create",
    "goal_plus_search_create", "goal_plus_create", "pi_search_pool_open",
    "mcp__goal_plus_extra__goal_plus_search_create",
    "mcp__foreign__mcp__goal_plus__goal_plus_search_create",
])
@pytest.mark.parametrize("child", [False, True])
def test_foreign_tool_identity_does_not_require_goal_plus(tmp_path, tool_name, child):
    search_root = tmp_path / ".gp"
    result = _run_hook(tmp_path, search_root, {
        "hook_event_name": "PreToolUse", "session_id": "ordinary-session",
        "tool_name": tool_name,
        # Arguments cannot supply ownership, even with a convincing name/tag.
        "tool_input": {"tool_source": "goal-plus", "run_id": "run_external"},
        **({"agent_id": "ordinary-child"} if child else {}),
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not search_root.exists()


@pytest.mark.parametrize("tool_name", [
    "mcp__foreign__goal_plus_search_run_verifier", "mcp__foreign__goal_plus_search_status",
    "mcp__foreign__goal_plus_search_get_agent_context", "mcp__foreign__spawn_agent",
    "functions.foreign.interrupt_agent",
    "functions.collaboration.foreign.interrupt_agent",
    "mcp__foreign__functions.collaboration.interrupt_agent",
    "foreign.functions.collaboration.interrupt_agent",
])
def test_foreign_post_tool_does_not_pump_or_bind_goal_plus(tmp_path, monkeypatch, tool_name):
    def unexpected(*args, **kwargs):
        pytest.fail("foreign tool entered Goal Plus lifecycle")

    monkeypatch.setattr(internal_agents, "kick_internal_agents", unexpected)
    monkeypatch.setattr(hook_module, "_codex_scope_for_hook", unexpected)
    hook_module._handle_post_tool_lifecycle(tmp_path / ".gp", {
        "tool_name": tool_name, "tool_input": {"run_id": "run_foreign"},
        "tool_response": {"run_id": "run_foreign", "agent_id": "foreign"},
    })
    assert not (tmp_path / ".gp").exists()


@pytest.mark.parametrize("paused", [False, True])
def test_foreign_tools_leave_attached_goal_unchanged(tmp_path, paused):
    search_root = tmp_path / ".gp"
    record = start_host_verified_goal(search_root, tmp_path, session_id="session-codex")
    runtime = FileGoalPlusRuntime(search_root)
    goal_id = record.goal_plus_id
    runtime.activate_session(goal_id, {"agent_harness": "codex", "session_id": "session-codex"})
    if paused:
        record = runtime.status(goal_id)
        runtime.control_from_host_command(goal_id, invocation=record.host_command_invocations[0].model_copy(
            update={"action": "pause", "invocation_id": "identity-pause"},
        ))
    before = runtime.status(goal_id).model_dump_json()
    for tool in ("goal_plus_search_create", "goal_plus_set_status", "goal_plus_search_run_verifier", "apply_patch", "spawn_agent"):
        result = _run_hook(tmp_path, search_root, {
            "hook_event_name": "PreToolUse", "session_id": "session-codex",
            "tool_name": f"mcp__foreign__{tool}",
            "tool_input": {"goal_plus_id": goal_id, "run_id": "run_foreign"},
        })
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    assert runtime.status(goal_id).model_dump_json() == before


@pytest.mark.parametrize("identity", [
    {"agent_type": "search_candidate_external"},
    {"task_name": "search_candidate_agent"},
    {"task_name": "goal_plus_final_check_external"},
    {"role": "final-checker"},
])
def test_task_name_alone_does_not_confer_goal_plus_role(tmp_path, identity):
    search_root = tmp_path / ".gp"
    event = {"hook_event_name": "PreToolUse", "agent_id": "ordinary-child", **identity}
    ordinary_patch = _run_hook(tmp_path, search_root, {
        **event, "tool_name": "apply_patch", "cwd": str(tmp_path),
        "tool_input": {"command": "*** Begin Patch\n*** Add File: note.txt\n+hello\n*** End Patch"},
    })
    assert ordinary_patch.stdout == ""
    submission = _run_hook(tmp_path, search_root, {
        **event, "tool_name": "mcp__goal_plus__goal_plus_submit_final_check",
        "tool_input": {"goal_plus_id": "gp_0001"},
    })
    assert "GoalPlusRoleDenied" in _pre_tool_denial(submission)
    assert not search_root.exists()


@pytest.mark.parametrize("terminal_status", ["complete", "blocked", "abandoned"])
def test_pre_tool_use_allows_linked_report_for_terminal_goal(
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
            "tool_name": "mcp__goal-plus__goal_plus_search_report",
            "tool_input": {"run_id": "run_terminal"},
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert runtime.status(goal_plus_id).hook_counters["pre_tool_use"] == 1


def test_pre_tool_use_denies_report_without_terminal_run_link(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    _terminal_report_goal(search_root)

    result = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "other-session",
            "tool_name": "mcp__goal-plus__goal_plus_search_report",
            "tool_input": {"run_id": "run_other"},
        },
    )

    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "GoalPlusEntrypointRequired" in specific["permissionDecisionReason"]


def test_pre_tool_use_allows_terminal_report_for_detached_original_session(
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
            "session_id": "later-codex-session",
            "tool_name": "mcp__goal-plus__goal_plus_search_report",
            "tool_input": {"run_id": "run_terminal"},
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert runtime.status(goal_plus_id).hook_counters["pre_tool_use"] == 1


def test_pre_tool_use_allows_terminal_stop_gate_only_for_attached_session(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime, goal_plus_id = _terminal_report_goal(search_root)
    tool_input = {"goal_plus_id": goal_plus_id, "event": "stop", "context": {}}

    allowed = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__goal_plus_gate",
            "tool_input": tool_input,
        },
    )
    denied = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "other-session",
            "tool_name": "mcp__goal-plus__goal_plus_gate",
            "tool_input": tool_input,
        },
    )

    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""
    assert "GoalPlusEntrypointRequired" in _pre_tool_denial(denied)
    assert runtime.status(goal_plus_id).hook_counters["pre_tool_use"] == 1

    wrong_goal = _run_hook(tmp_path, search_root, {
        "hook_event_name": "PreToolUse", "session_id": "session-codex",
        "tool_name": "mcp__goal-plus__goal_plus_gate",
        "tool_input": {**tool_input, "goal_plus_id": "gp_other"},
    })
    assert "GoalPlusOwnershipMismatch" in _pre_tool_denial(wrong_goal)


@pytest.mark.parametrize("terminal", [False, True])
def test_same_turn_user_steering_preserves_authorized_goal_turn(
    tmp_path: Path,
    terminal: bool,
) -> None:
    search_root = tmp_path / ".gp"
    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "turn-goal",
            "prompt": "$goal-plus Implement requirement A",
        },
    )
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    runtime = FileGoalPlusRuntime(search_root)
    if terminal:
        runtime.set_status(
            record_path.parent.name,
            status="complete",
            reason="final audit passed",
        )

    steering = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "turn-goal",
            "prompt": "Use the newly available workers as well.",
        },
    )

    if terminal:
        assert steering.stdout == ""
    else:
        assert record_path.parent.name in _additional_context(steering, "UserPromptSubmit")
    record = runtime.status(record_path.parent.name)
    assert record.active_session is not None
    assert record.active_session.state == "attached"
    assert len(record.host_command_invocations) == 1

    if not terminal:
        next_tool = _run_hook(
            tmp_path,
            search_root,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-codex",
                "turn_id": "turn-goal",
                "tool_name": "mcp__goal-plus__goal_plus_record_triage",
                "tool_input": {"goal_plus_id": record_path.parent.name},
            },
        )
        assert next_tool.stdout == ""


@pytest.mark.parametrize("ordinary_turn_id", [None, "turn-other"])
def test_ordinary_prompt_preserves_same_session_authorization(
    tmp_path: Path,
    ordinary_turn_id: str | None,
) -> None:
    search_root = tmp_path / ".gp"
    _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "turn-goal",
            "prompt": "$goal-plus Implement requirement A",
        },
    )
    record_path = next((search_root / "goal-plus").glob("gp_*/goal.json"))
    goal_id = record_path.parent.name

    ordinary_input = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-codex",
        "prompt": "What is the current branch?",
    }
    if ordinary_turn_id is not None:
        ordinary_input["turn_id"] = ordinary_turn_id
    ordinary = _run_hook(tmp_path, search_root, ordinary_input)
    assert goal_id in _additional_context(ordinary, "UserPromptSubmit")
    record = FileGoalPlusRuntime(search_root).status(goal_id)
    assert record.active_session is not None
    assert record.active_session.state == "attached"
    assert record.active_session.session_id == "session-codex"

    allowed = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-codex",
            "tool_name": "mcp__goal-plus__goal_plus_record_triage",
            "tool_input": {"goal_plus_id": goal_id},
        },
    )
    assert allowed.stdout == ""
    assert FileGoalPlusRuntime(search_root).status(goal_id).hook_counters[
        "pre_tool_use"
    ] == 1


def test_ordinary_prompt_does_not_reactivate_detached_session(tmp_path: Path) -> None:
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
    runtime = FileGoalPlusRuntime(search_root)
    runtime.detach_session(
        goal_id,
        session_id="session-codex",
        reason="test_interrupted_session",
    )

    ordinary = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "Continue the current work.",
        },
    )
    assert ordinary.stdout == ""
    detached = runtime.status(goal_id)
    assert detached.active_session is not None
    assert detached.active_session.state == "detached"
    assert detached.active_session.metadata["detach_reason"] == "test_interrupted_session"

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

    rejected = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "prompt": "$goal-plus resume",
        },
    )
    payload = json.loads(rejected.stdout)
    assert "没有绑定的 Goal Plus 任务" in payload["hookSpecificOutput"]["additionalContext"]
    still_detached = runtime.status(goal_id)
    assert still_detached.active_session is not None
    assert still_detached.active_session.state == "detached"


def test_pre_tool_use_ignores_unrelated_tool(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
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


def test_unbound_search_candidate_can_return_without_completing_goal(tmp_path: Path) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Optimize model throughput")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
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

    assert result.stdout == ""
    assert runtime.status(record.goal_plus_id).status == "active"
    assert runtime.status(record.goal_plus_id).hook_counters["subagent_stop"] == 1


def test_ordinary_subagent_stop_does_not_inherit_parent_next_action(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Implement a feature")
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
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
        {"agent_harness": "codex", "session_id": "session-codex"},
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
    assert "phase=goal" in message
    assert "search_tasks=0" in message
    assert "stop=1" in message

    settled = runtime.status(record.goal_plus_id)
    assert settled.active_session is not None
    assert settled.active_session.state == "detached"
    assert (
        settled.active_session.metadata["detach_reason"]
        == "nonactive_stop_summary_emitted"
    )
    assert "stop_summary_emitted_at" in settled.active_session.metadata

    repeated = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-codex",
            "goal_plus_id": record.goal_plus_id,
        },
    )
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout == ""
    assert runtime.status(record.goal_plus_id).hook_counters["stop"] == 1


def test_stop_prefers_new_active_goal_over_older_terminal_goal(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    terminal = runtime.create_goal("Completed work")
    runtime.activate_session(
        terminal.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
    )
    runtime.set_status(
        terminal.goal_plus_id,
        status="complete",
        reason="final audit passed",
    )
    active = runtime.create_goal("Implement the new active work")
    runtime.activate_session(
        active.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-codex"},
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
    # The compact stop continuation no longer embeds the raw goal; it must
    # still point the agent at the new active Goal Plus, not the older
    # terminal one.
    assert active.goal_plus_id in payload["reason"]
    assert terminal.goal_plus_id not in payload["reason"]
    assert runtime.status(active.goal_plus_id).hook_counters["stop"] == 1
    assert runtime.status(terminal.goal_plus_id).hook_counters == {}


def test_ordinary_prompt_detaches_terminal_goal_before_next_stop(
    tmp_path: Path,
) -> None:
    search_root = tmp_path / ".gp"
    runtime = FileGoalPlusRuntime(search_root)
    record = runtime.create_goal("Completed work")
    runtime.activate_session(
        record.goal_plus_id,
        {
            "agent_harness": "codex",
            "session_id": "session-codex",
            "metadata": {"native_entry_id": "turn-goal"},
        },
    )
    runtime.set_status(
        record.goal_plus_id,
        status="complete",
        reason="final audit passed",
    )

    ordinary = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-codex",
            "turn_id": "turn-other",
            "prompt": "What is the current branch?",
        },
    )
    assert ordinary.stdout == ""
    updated = runtime.status(record.goal_plus_id)
    assert updated.active_session is not None
    assert updated.active_session.state == "detached"

    stop = _run_hook(
        tmp_path,
        search_root,
        {
            "hook_event_name": "Stop",
            "session_id": "session-codex",
        },
    )
    assert stop.stdout == ""
    assert runtime.status(record.goal_plus_id).hook_counters == {}
