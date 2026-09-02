from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from goal_plus.codex_transcript import read_codex_session_meta
from tests.st.helpers.codex_runner import CodexRunner


pytestmark = [pytest.mark.st, pytest.mark.st_codex]


def _subagent_transcript(task_name: str, parent_thread_id: str) -> Path | None:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    sessions_root = codex_home if codex_home.name == "sessions" else codex_home / "sessions"
    for path in sessions_root.rglob("*.jsonl"):
        meta = read_codex_session_meta(path)
        if not meta or meta.get("parent_thread_id") != parent_thread_id:
            continue
        source = meta.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        agent_path = spawn.get("agent_path") if isinstance(spawn, dict) else None
        if isinstance(agent_path, str) and (
            agent_path == task_name
            or agent_path.endswith(f"/{task_name.lstrip('/')}")
        ):
            return path
    return None


def _mcp_tool_results(path: Path, tool_name: str) -> list[object]:
    results: list[object] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload")
        if (
            event.get("type") != "event_msg"
            or not isinstance(payload, dict)
            or payload.get("type") != "mcp_tool_call_end"
        ):
            continue
        invocation = payload.get("invocation")
        if isinstance(invocation, dict) and invocation.get("tool") == tool_name:
            results.append(payload.get("result"))
    return results


def test_codex_native_child_hooks_preserve_main_binding(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    search_root = st_project_root / ".gp"
    shutil.rmtree(search_root, ignore_errors=True)
    runner = CodexRunner(
        project_root=st_project_root,
        log_dir=st_log_dir,
        default_timeout=600,
    )
    probe_spec_id = "spec_native_child_probe_missing"
    raw_goal = "Return the exact marker native-child-hook-isolation after independent review."
    result = runner.run_streaming(
        (
            "Create exactly one Goal Plus record with goal_plus_create using source_path='.' "
            f"and this raw goal: {raw_goal}\n\n"
            "Record Goal Mode triage. Upsert one required route=subagent work item whose "
            "objective is to call the logical goal-plus MCP search_create once with "
            f"frozen_spec_id='{probe_spec_id}', confirm the expected missing-spec runtime "
            "error, and then return the exact marker native-child-hook-isolation. Execute "
            "the normal dispatch, spawn_agent with fork_turns=none, bind, wait, result, and "
            "accepted lifecycle. Use the returned native task handle without inventing one. "
            "After checking the exact marker, mark the Goal Plus record complete."
        ),
        scenario="codex_hook_context_isolation",
        timeout=600,
    )

    print(f"\n[codex_hook_context_isolation] log: {result.log_path}")
    assert not result.timed_out, result.log_path
    assert result.returncode == 0, result.log_path

    goal_files = sorted(search_root.glob("goal-plus/gp_*/goal.json"))
    assert len(goal_files) == 1, result.log_path
    record = json.loads(goal_files[0].read_text(encoding="utf-8"))
    assert record["status"] == "complete"
    assert len(record["work_items"]) == 1
    work_item = record["work_items"][0]
    assert work_item["status"] == "accepted"
    assert work_item["task_name"]

    active_session = record["active_session"]
    child_transcript = _subagent_transcript(
        work_item["task_name"],
        active_session["session_id"],
    )
    assert child_transcript is not None and child_transcript.is_file()
    child_text = child_transcript.read_text(encoding="utf-8")
    assert record["goal_plus_id"] not in child_text
    assert "goal_plus_context_role=search_candidate" not in child_text
    [probe_result] = _mcp_tool_results(child_transcript, "search_create")
    probe_text = json.dumps(probe_result, ensure_ascii=False)
    assert probe_spec_id in probe_text
    assert "permissionDecision" not in probe_text

    main_transcript = Path(active_session["transcript_path"])
    assert main_transcript.is_file()
    assert main_transcript.resolve() != child_transcript.resolve()
    main_meta = read_codex_session_meta(main_transcript)
    assert main_meta is not None
    assert main_meta.get("thread_source") != "subagent"
