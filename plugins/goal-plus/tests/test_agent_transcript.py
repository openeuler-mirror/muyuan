"""Opt-in agent transcript export: parsing, rendering and debug gating."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from goal_plus.infrastructure.agent_transcript import (
    collect_runtime_transcripts,
    debug_export_child_transcript,
    debug_export_codex_transcript,
    export_transcripts,
    read_codex_transcript,
    read_pi_transcript,
    read_session_diagnostics_archive,
    write_transcript_files,
)
from goal_plus.projections.agent_transcript import (
    AgentTranscriptSession,
    TranscriptEvent,
    render_transcript_html,
    render_transcript_jsonl,
)


def _write_lines(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                    encoding="utf-8")
    return path


def _pi_history(tmp_path: Path) -> Path:
    return _write_lines(tmp_path / "pi-session.jsonl", [
        {"type": "session", "version": 3, "id": "sess-1",
         "timestamp": "2026-09-13T11:24:08.809Z", "cwd": "/tmp/work"},
        {"type": "model_change", "id": "m1", "parentId": None,
         "timestamp": "2026-09-13T11:24:14.542Z", "provider": "zai", "modelId": "glm-5.3-flash"},
        {"type": "thinking_level_change", "id": "m2", "parentId": "m1",
         "timestamp": "2026-09-13T11:24:14.542Z", "thinkingLevel": "low"},
        {"type": "message", "id": "e1", "parentId": "m2", "timestamp": "2026-09-13T11:24:15.513Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "继续此 Goal Plus 任务。"}]}},
        {"type": "custom_message", "id": "e2", "parentId": "e1", "timestamp": "2026-09-13T11:24:15.513Z",
         "customType": "goal-plus-native-context", "content": "[GOAL PLUS ACTIVE]"},
        {"type": "custom", "id": "e2b", "parentId": "e2", "timestamp": "2026-09-13T11:24:16.000Z",
         "customType": "goal-plus-provider-request",
         "data": {"version": 1, "session_id": "sess-1", "turn_index": 0,
                  "model": {"provider": "zai", "id": "glm-5.3-flash"}, "status": "observed"}},
        {"type": "message", "id": "e3", "parentId": "e2b", "timestamp": "2026-09-13T11:24:21.206Z",
         "message": {"role": "assistant",
                     "content": [{"type": "thinking", "thinking": "先读状态"},
                                 {"type": "toolCall", "id": "call_1", "name": "goal_plus_status",
                                  "arguments": {"goal_plus_id": "gp_0001"}}],
                     "usage": {"input": 14121, "output": 24}, "stopReason": "toolUse",
                     "model": "glm-5.3-flash"}},
        {"type": "message", "id": "e4", "parentId": "e3", "timestamp": "2026-09-13T11:24:21.221Z",
         "message": {"role": "toolResult", "toolCallId": "call_1", "toolName": "goal_plus_status",
                     "content": [{"type": "text", "text": "{\"status\": \"active\"}"}], "isError": False}},
        {"type": "message", "id": "e5", "parentId": "e4", "timestamp": "2026-09-13T11:31:00.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "稍后追问"}]}},
        # Sibling off the selected branch must never surface.
        {"type": "message", "id": "x9", "parentId": "e2", "timestamp": "2026-09-13T11:24:22.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "OFF_BRANCH_PROMPT"}]}},
        {"type": "custom", "id": "e6", "parentId": "e5", "timestamp": "2026-09-13T11:31:01.000Z",
         "customType": "goal-plus-injection-observation",
         "data": {"kind": "worker_steer", "role": "worker", "chars": 9, "est_tokens": 3,
                  "sha256": "abc", "target_id": "agent-owned"}},
    ])


def test_pi_transcript_keeps_branch_content_only(tmp_path):
    parsed = read_pi_transcript(_pi_history(tmp_path))
    assert parsed.session_id == "sess-1"
    kinds = [event.kind for event in parsed.events]
    assert kinds[:2] == ["session", "meta"]
    assert "OFF_BRANCH_PROMPT" not in " ".join(event.content for event in parsed.events)
    user = next(event for event in parsed.events if event.kind == "user")
    assert "继续此 Goal Plus 任务。" in user.content
    thinking = next(event for event in parsed.events if event.kind == "thinking")
    assert thinking.content == "先读状态"
    call = next(event for event in parsed.events if event.kind == "tool_call")
    assert call.label == "goal_plus_status"
    assert json.loads(call.content) == {"goal_plus_id": "gp_0001"}
    assert call.meta["usage"] == {"input": 14121, "output": 24}
    result = next(event for event in parsed.events if event.kind == "tool_result")
    assert result.label == "goal_plus_status" and result.meta["is_error"] is False
    injection = next(event for event in parsed.events if event.kind == "injection")
    assert injection.label == "goal-plus-native-context"
    assert injection.content == "[GOAL PLUS ACTIVE]"
    ledger = next(event for event in parsed.events if event.label == "injection-ledger worker_steer")
    assert json.loads(ledger.content)["target_id"] == "agent-owned"


def test_pi_leaf_id_selects_other_branch(tmp_path):
    parsed = read_pi_transcript(_pi_history(tmp_path), leaf_id="x9")
    joined = " ".join(event.content for event in parsed.events)
    assert "OFF_BRANCH_PROMPT" in joined
    assert "稍后追问" not in joined



def test_html_is_self_contained_and_jsonl_is_flat(tmp_path):
    parsed = read_pi_transcript(_pi_history(tmp_path))
    hostile_content = '<script>alert("transcript")</script> & <GOAL>'
    parsed.events.append(TranscriptEvent(kind="text", label="hostile content", content=hostile_content))
    session = AgentTranscriptSession(session_id="sess-1", harness="pi", role="candidate",
                                     source_path="pi.jsonl", identity={"candidate_id": "c001"},
                                     events=parsed.events, model=parsed.model, thinking=parsed.thinking)
    page = render_transcript_html([session], generated_at="2026-09-18T00:00:00Z")
    assert page.startswith("<!DOCTYPE html>")
    assert "<script" not in page and page.count("<style>") == 1
    assert "goal-plus-native-context" in page and "class=\"gap\"" in page
    assert "<details open>" in page  # conversation content expands by default
    assert 'class="turn">▶ prompt #1 · launch' in page
    # Usage rides the first part of one provider call only, with spelled-out units.
    assert "context 14,121 tok · output 24 tok · total 14,145 tok · glm-5.3-flash" in page
    # Session header exposes the agent's actual model and thinking level.
    assert "model glm-5.3-flash (zai) · thinking low" in page
    assert page.count("total 14,145 tok") == 1
    assert "▶ prompt #2 · dispatch/continuation" in page
    assert "footprint: model-context" in page and "host-records" in page
    assert "injection-ledger worker_steer" in page  # grouped record volume visible
    assert "chars truncated" in render_transcript_html([AgentTranscriptSession(
        session_id="s", harness="pi", role="main", events=[parsed.events[0]])], max_chars=5)
    # Observation bookkeeping (meta/context, e.g. provider requests) stays collapsed.
    assert page.count("<details><summary>content</summary>") >= 2
    assert "provider request" in page
    assert ">candidate c001/pi</a>" in page and "candidate c001 (pi)" in page
    assert hostile_content not in page
    assert "&lt;script&gt;alert(&quot;transcript&quot;)&lt;/script&gt; &amp; &lt;GOAL&gt;" in page
    lines = [json.loads(line) for line in render_transcript_jsonl([session]).splitlines()]
    assert [line["seq"] for line in lines] == list(range(1, len(parsed.events) + 1))
    assert lines[0]["session_id"] == "sess-1" and lines[0]["role"] == "candidate"


def _codex_rollout(tmp_path: Path) -> Path:
    return _write_lines(tmp_path / "rollout.jsonl", [
        {"timestamp": "2026-09-16T07:03:03.073Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": "cx-1", "cwd": "/tmp", "originator": "codex_exec", "cli_version": "0.154.0"}},
        {"timestamp": "2026-09-16T07:03:03.173Z", "ordinal": 1, "type": "turn_context",
         "payload": {"turn_id": "t1", "model": "gpt-6", "effort": "medium"}},
        {"timestamp": "2026-09-16T07:03:03.185Z", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "role": "user", "id": "m1",
                     "content": [{"type": "input_text", "text": "do the thing"}]}},
        {"timestamp": "2026-09-16T07:03:04.000Z", "ordinal": 3, "type": "response_item",
         "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan first"}]}},
        {"timestamp": "2026-09-16T07:03:04.100Z", "ordinal": 4, "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "arguments": "{\"command\": \"ls\"}",
                     "call_id": "call_9"}},
        {"timestamp": "2026-09-16T07:03:04.200Z", "ordinal": 5, "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_9", "output": "file list"}},
        {"timestamp": "2026-09-16T07:03:04.300Z", "ordinal": 6, "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 5}}}},
        {"timestamp": "2026-09-16T07:03:04.400Z", "ordinal": 7, "type": "event_msg",
         "payload": {"type": "turn_aborted", "reason": "interrupted", "duration_ms": 589}},
        {"timestamp": "2026-09-16T07:03:04.500Z", "ordinal": 8, "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "id": "m2",
                     "content": [{"type": "output_text", "text": "done"}]}},
    ])


def test_codex_transcript_matches_tool_outputs_and_marks_aborts(tmp_path):
    parsed = read_codex_transcript(_codex_rollout(tmp_path))
    assert parsed.session_id == "cx-1"
    labels = [(event.kind, event.label) for event in parsed.events]
    assert ("user", "prompt") in labels
    assert ("thinking", "reasoning") in labels
    assert ("tool_call", "shell") in labels
    assert ("tool_result", "shell") in labels  # matched through call_id
    assert ("error", "turn aborted") in labels
    assert ("assistant", "reply") in labels
    output = next(event for event in parsed.events if event.kind == "tool_result")
    assert output.content == "file list"
    context = next(event for event in parsed.events if event.kind == "context")
    assert json.loads(context.content) == {"input_tokens": 100, "output_tokens": 5}


def _write_archive(directory: Path, entries: list[dict], *, status: str = "recorded") -> Path:
    batch = {"version": 1, "session_id": "child-sess", "stream_id": "s1", "sequence": 0,
             "captured_at": "2026-09-13T11:30:00.000Z", "leaf_id": entries[-1]["id"], "entries": entries}
    digest = hashlib.sha256(
        json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{digest}.json").write_text(json.dumps(batch), encoding="utf-8")
    (directory / "index.json").write_text(json.dumps({
        "version": 1, "source": "thinkthread_messages", "agent_harness": "pi",
        "identity": {"thinkthread_id": "tt-child", "agent_session_id": "agent-owned",
                     "run_id": "run-1", "candidate_id": "c001"},
        "batches": {digest: {"stream_id": "s1", "sequence": 0, "entries": len(entries),
                             "compressed_bytes": 1, "decoded_bytes": 1,
                             "captured_at": "2026-09-13T11:30:00.000Z", "content_sha256": "x"}},
        "pending_batches": {}, "status": status, "session_id": "child-sess"}), encoding="utf-8")
    return directory


def _archive_entries() -> list[dict]:
    return [
        {"id": "a1", "parentId": None, "timestamp": "2026-09-13T11:25:00.000Z", "type": "message",
         "message": {"role": "assistant",
                     "content": [{"type": "thinking", "thinking": "RAW_THINKING_BEFORE_RELEASE"},
                                  {"type": "toolCall", "id": "call_a", "name": "search_run_verifier",
                                  "arguments": {"run_id": "run-1"}}]}},
        {"id": "a2", "parentId": "a1", "timestamp": "2026-09-13T11:25:05.000Z", "type": "message",
         "message": {"role": "toolResult", "toolCallId": "call_a", "toolName": "search_run_verifier",
                     "content": [{"type": "text", "text": "{\"ok\": true}"}], "isError": False}},
        {"id": "a3", "parentId": "a2", "timestamp": "2026-09-13T11:25:06.000Z", "type": "custom",
         "customType": "goal-plus-injection-observation",
         "data": {"kind": "worker_steer", "role": "worker", "chars": 11, "est_tokens": 4}},
    ]


def test_archive_transcript_replays_batches(tmp_path):
    directory = _write_archive(tmp_path / "tt-child", _archive_entries())
    parsed = read_session_diagnostics_archive(directory)
    assert parsed.session_id == "child-sess"
    assert [event.kind for event in parsed.events] == ["session", "thinking", "tool_call", "tool_result", "meta"]
    assert parsed.events[2].label == "search_run_verifier"

    partial = _write_archive(tmp_path / "tt-partial", _archive_entries()[:1], status="partial")
    assert "archive_status_partial" in read_session_diagnostics_archive(partial).warnings


def _runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / ".gp"
    main_history = _pi_history(tmp_path)
    worker_history = _write_lines(tmp_path / "worker.jsonl", [
        {"type": "session", "version": 3, "id": "worker-sess", "timestamp": "2026-09-13T11:30:00.000Z",
         "cwd": "/tmp/w"},
        {"type": "message", "id": "w1", "parentId": None, "timestamp": "2026-09-13T11:30:01.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "worker reply"}]}},
    ])
    rollout = _codex_rollout(tmp_path)

    def write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    write_json(root / "host-logs/session-diagnostics/pi-abc/index.json", {
        "version": 1, "source": "native", "session_id": "sess-1", "agent_harness": "pi",
        "role": "main", "history": str(main_history), "overview": "overview.txt"})
    # A worker whose host-side native index points at the same history as its
    # run record must be exported once, keeping the richer run identity.
    write_json(root / "host-logs/session-diagnostics/pi-worker/index.json", {
        "version": 1, "source": "native", "session_id": "worker-sess", "agent_harness": "pi",
        "role": "worker", "history": str(worker_history), "overview": "overview.txt"})
    write_json(root / "runs/run-1/agent_sessions/agent_worker.json", {
        "agent_session_id": "agent_worker", "candidate_id": "c001", "host": "pi-rpc",
        "host_handle": {"agent_harness": "pi", "external_id": "worker-sess",
                        "metadata": {"session_file": str(worker_history)}}})
    write_json(root / "runs/run-1/agent_sessions/agent_cx.json", {
        "agent_session_id": "agent_cx", "candidate_id": "c002", "host": "codex-direct",
        "host_handle": {"agent_harness": "codex", "external_id": "cx-1",
                        "metadata": {"session_file": str(rollout)}}})
    second_history = _write_lines(tmp_path / "worker2.jsonl", [
        {"type": "session", "version": 3, "id": "worker-2-sess", "timestamp": "2026-09-13T12:00:00.000Z",
         "cwd": "/tmp/w2"},
        {"type": "message", "id": "w2", "parentId": None, "timestamp": "2026-09-13T12:00:01.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "second run reply"}]}},
    ])
    write_json(root / "runs/run-2/agent_sessions/agent_second.json", {
        "agent_session_id": "agent_second", "candidate_id": "c101", "host": "pi-rpc",
        "host_handle": {"agent_harness": "pi", "external_id": "worker-2-sess",
                        "metadata": {"session_file": str(second_history)}}})
    write_json(root / "host-scopes/sc-1/thinkthread-sessions/agent_worker.json", {
        "run_id": "run-1", "candidate_id": "c001", "agent_session_id": "agent_worker",
        "thinkthread_id": "tt-child", "status": "running",
        "call_request": {"prompt": "verify the hypothesis now", "timeout_seconds": 30},
        "spawn_intent": {"model": "glm-5.3-flash"}})
    write_json(root / "host-logs/codex-time-advisory/sent/agent_cx.json", {
        "sent_at": "2026-09-16T07:03:04.350Z", "trigger_tool": "shell",
        "advisory": "Time advisory body"})
    return root


def test_collect_runtime_transcripts_merges_all_sources(tmp_path):
    sessions = collect_runtime_transcripts(_runtime_root(tmp_path))
    by_role = {session.role: session for session in sessions}
    assert set(by_role) >= {"main", "candidate", "controller-journal"}
    assert by_role["main"].session_id == "sess-1"
    assert "继续此 Goal Plus 任务。" in " ".join(event.content for event in by_role["main"].events)
    candidates = [session for session in sessions if session.role == "candidate"]
    assert {session.identity.get("candidate_id") for session in candidates} >= {"c001", "c002"}
    second = next(session for session in candidates if session.identity.get("run_id") == "run-2")
    assert "second run reply" in " ".join(event.content for event in second.events)
    page = render_transcript_html(sessions)
    assert 'class="navgroup">run-1</span>' in page and 'class="navgroup">run-2</span>' in page
    worker_sessions = [session for session in sessions if session.source_path.endswith("worker.jsonl")]
    assert len(worker_sessions) == 1
    assert worker_sessions[0].identity["agent_session_id"] == "agent_worker"
    codex_session = next(session for session in candidates if session.harness == "codex")
    assert codex_session.identity["agent_session_id"] == "agent_cx"
    advisory = [event for event in codex_session.events if event.label == "codex-time-advisory"]
    assert advisory and advisory[0].content == "Time advisory body"
    journal = by_role["controller-journal"]
    prompts = [event for event in journal.events
               if event.kind == "injection" and event.label == "controller prompt"]
    assert prompts and prompts[0].content == "verify the hypothesis now"


def test_export_transcripts_writes_artifacts(tmp_path):
    root = _runtime_root(tmp_path)
    summary = export_transcripts(root)
    output = Path(summary["output"])
    assert output == root / "host-logs" / "agent-transcript"
    for name in ("index.html", "events.jsonl"):
        assert (output / name).is_file()
    session_dirs = [path for path in output.iterdir() if path.is_dir()]
    assert all((path / "transcript.html").is_file() for path in session_dirs)
    assert not list(output.rglob("*.tmp"))
    assert not list(output.rglob("*.txt"))  # html replaced the text view
    assert "main" in (output / "index.html").read_text(encoding="utf-8")


def test_debug_exports_are_opt_in(tmp_path, monkeypatch):
    import os

    rollout = _codex_rollout(tmp_path)
    monkeypatch.delenv("GOAL_PLUS_DEBUG", raising=False)
    debug_export_codex_transcript(tmp_path, rollout)
    assert not (tmp_path / "host-logs" / "agent-transcript").exists()

    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    debug_export_codex_transcript(tmp_path, rollout, session_id="cx-1", role="main")
    exported = tmp_path / "host-logs" / "agent-transcript"
    transcript = exported / "main-cx-1" / "transcript.html"
    assert transcript.is_file()
    assert "do the thing" in transcript.read_text(encoding="utf-8")

    # An unchanged rollout must not be re-rendered on every stop.
    exported_before = transcript.stat().st_mtime_ns
    stale = transcript.stat().st_mtime - 5
    os.utime(rollout, (stale, stale))
    debug_export_codex_transcript(tmp_path, rollout, session_id="cx-1", role="main")
    assert transcript.stat().st_mtime_ns == exported_before
    # A newer, changed source forces a re-export. The explicit mtime offset
    # matters: the local filesystem rounds timestamps into ~4ms ticks.
    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "timestamp": "2026-09-16T07:03:05.000Z", "ordinal": 9, "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "id": "m3",
                        "content": [{"type": "output_text", "text": "re-exported"}]}}) + "\n")
    fresh = transcript.stat().st_mtime + 5
    os.utime(rollout, (fresh, fresh))
    debug_export_codex_transcript(tmp_path, rollout, session_id="cx-1", role="main")
    assert "re-exported" in transcript.read_text(encoding="utf-8")

    archive = _write_archive(tmp_path / "host-logs/session-diagnostics/tt-child", _archive_entries())
    record = {"thinkthread_id": "tt-child", "agent_session_id": "agent-owned",
              "run_id": "run-1", "candidate_id": "c001", "status": "closed",
              "call_request": {"prompt": "final attempt", "timeout_seconds": 60}}
    # Release-time child capture uses the same DEBUG switch.
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    debug_export_child_transcript(tmp_path, record)
    assert (archive / "transcript.html").exists()
    # Disabling DEBUG stops further diagnostic export.
    (archive / "transcript.html").unlink()
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "0")
    debug_export_child_transcript(tmp_path, record)
    assert not (archive / "transcript.html").exists()
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    debug_export_child_transcript(tmp_path, record)
    assert (archive / "transcript.html").is_file()
    archive_html = (archive / "transcript.html").read_text(encoding="utf-8")
    assert "final attempt" in archive_html
    assert "search_run_verifier" in archive_html


def test_stop_hook_debug_wiring(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timezone

    from goal_plus import goal_plus_stop_hook as hook

    rollout = _codex_rollout(tmp_path)
    monkeypatch.delenv("GOAL_PLUS_DEBUG", raising=False)
    hook._record_stop_hook_event(
        tmp_path, {"session_id": "cx-1", "transcript_path": str(rollout)}, event_name="Stop",
        invocation_id="inv_1", started_at=datetime.now(timezone.utc),
        started_monotonic=time.monotonic(), result={"decision": "block", "reason": "waiting"})
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    hook._record_stop_hook_event(
        tmp_path, {"session_id": "cx-1", "transcript_path": str(rollout)}, event_name="Stop",
        invocation_id="inv_2", started_at=datetime.now(timezone.utc),
        started_monotonic=time.monotonic(), result={"decision": "block", "reason": "waiting"})
    exported = tmp_path / "host-logs" / "agent-transcript"
    assert (exported / "main-cx-1" / "transcript.html").is_file()
    assert (exported / "index.html").is_file()
    assert (exported / "events.jsonl").is_file()
    assert (tmp_path / "host-logs" / "codex-hook-events" / "inv_2.json").is_file()


def test_write_transcript_files_rejects_nothing_and_names_sessions(tmp_path):
    session = AgentTranscriptSession(session_id="weird/id spaces!", harness="pi", role="main",
                                     events=[], warnings=["w1"])
    summary = write_transcript_files(tmp_path / "out", [session], include_jsonl=False)
    assert summary["sessions"][0]["name"] == "main-weird-id-spaces"
    assert (tmp_path / "out" / "index.html").is_file()
    assert not (tmp_path / "out" / "events.jsonl").exists()
    assert not list((tmp_path / "out").rglob("*.txt"))


def _tt_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Mirror <instance>/thinkthreads/{root,child}/session layout with both sources."""
    raw_child = _write_lines(tmp_path / "instance/thinkthreads/tt-child/session/active.jsonl", [
        {"type": "session", "version": 3, "id": "child-sess", "timestamp": "2026-09-18T12:00:00.000Z",
         "cwd": "/tt/child"},
        {"type": "model_change", "id": "c1", "parentId": None,
         "timestamp": "2026-09-18T12:00:01.000Z", "provider": "zai", "modelId": "glm-5.3-flash"},
        {"type": "message", "id": "c2", "parentId": "c1", "timestamp": "2026-09-18T12:00:02.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "RAW_THINKING_BEFORE_RELEASE"},
             {"type": "toolCall", "id": "call_x", "name": "search_run_verifier",
              "arguments": {"run_id": "run-1"}}]}},
    ])
    runtime_root = tmp_path / "instance/thinkthreads/tt-root/session/goal-plus"
    archive = _write_archive(runtime_root / "host-logs/session-diagnostics/tt-child", _archive_entries())
    return runtime_root, raw_child


def test_tt_child_prefers_raw_history_and_falls_back_to_archive(tmp_path):
    runtime_root, raw_child = _tt_layout(tmp_path)
    sessions = collect_runtime_transcripts(runtime_root)
    child = next(session for session in sessions if session.role == "tt-child")
    # Retained child: the raw history is preferred while it is still available.
    assert child.source_path == str(raw_child)
    joined = " ".join(event.content for event in child.events)
    assert "RAW_THINKING_BEFORE_RELEASE" in joined
    assert child.model == "glm-5.3-flash (zai)"

    raw_child.unlink()  # release reclaims the child instance directory
    sessions = collect_runtime_transcripts(runtime_root)
    child = next(session for session in sessions if session.role == "tt-child")
    assert child.source_path.endswith("session-diagnostics/tt-child")
    # After release the unified archive still retains the full entry.
    assert "RAW_THINKING_BEFORE_RELEASE" in " ".join(event.content for event in child.events)
    assert "search_run_verifier" in " ".join(event.label for event in child.events)


def test_release_debug_export_grabs_raw_child_history(tmp_path, monkeypatch):
    runtime_root, raw_child = _tt_layout(tmp_path)
    archive = runtime_root / "host-logs/session-diagnostics/tt-child"
    record = {"thinkthread_id": "tt-child", "agent_session_id": "agent-owned",
              "run_id": "run-1", "candidate_id": "c001", "status": "closed",
              "model": "glm-5.3-flash"}
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    debug_export_child_transcript(runtime_root, record)
    exported = (archive / "transcript.html").read_text(encoding="utf-8")
    assert "RAW_THINKING_BEFORE_RELEASE" in exported  # captured before release


def test_released_child_recovers_turns_from_tt_event_store(tmp_path):
    import sqlite3

    runtime_root, raw_child = _tt_layout(tmp_path)
    raw_child.unlink()  # release reclaimed the raw history
    instance = tmp_path / "instance"
    store = instance / "events.sqlite3"
    connection = sqlite3.connect(store)
    connection.execute(
        "CREATE TABLE events (event_id TEXT, stream_id TEXT, sequence INTEGER, namespace TEXT,"
        " kind TEXT, schema_version INTEGER, producer_kind TEXT, producer_id TEXT,"
        " recorded_at_unix_ms INTEGER, payload BLOB, source_key TEXT, source_event_id TEXT)")
    connection.execute(
        "INSERT INTO events VALUES ('e1','s',1,'agent.pi','turn.completed',1,'pi','pi',"
        "1800000000000, ?, 'tt-child:pi:0', NULL)",
        (json.dumps({"text": "RECOVERED_TURN_SUMMARY"}).encode(),))
    connection.execute(
        "INSERT INTO events VALUES ('e2','s',2,'platform.message','accepted',1,'pi','pi',"
        "1799999999000, ?, NULL, NULL)",
        (json.dumps({"senderThinkthreadId": "tt-root", "recipientThinkthreadId": "tt-child",
                     "text": "# Search launch prompt recovered"}).encode(),))
    connection.commit()
    connection.close()

    sessions = collect_runtime_transcripts(runtime_root)
    child = next(session for session in sessions if session.role == "tt-child")
    joined = " ".join(event.content for event in child.events)
    assert "RECOVERED_TURN_SUMMARY" in joined
    assert "# Search launch prompt recovered" in joined
    assert any("TT event store" in warning for warning in child.warnings)
    # Recovery stays ordered: the prompt precedes the turn it launched.
    kinds = [(event.kind, event.label) for event in child.events]
    assert kinds.index(("injection", "controller prompt")) < kinds.index(("assistant", "turn completed"))


def test_concurrent_codex_callbacks_preserve_whole_runtime(tmp_path):
    import os
    import subprocess
    import sys

    root = _runtime_root(tmp_path)
    histories = [_codex_rollout(tmp_path / f'callback-{number}') for number in range(2)]
    code = '''
import sys
from pathlib import Path
from goal_plus.infrastructure.agent_transcript import debug_export_codex_transcript
debug_export_codex_transcript(Path(sys.argv[1]), sys.argv[2], session_id=sys.argv[3], role='subagent')
'''
    processes = [subprocess.Popen(
        [sys.executable, '-c', code, str(root), str(history), f'concurrent-{number}'],
        env={**os.environ, 'GOAL_PLUS_DEBUG': '1'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for number, history in enumerate(histories)]
    for process in processes:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
    output = root / 'host-logs/agent-transcript'
    html = (output / 'index.html').read_text()
    assert 'concurrent-0' in html and 'concurrent-1' in html
    assert 'candidate' in html and 'main' in html
    assert not list(output.rglob('*.tmp'))


def test_debug_cli_does_not_require_wrapper_or_create_output_when_disabled(tmp_path, monkeypatch):
    from goal_plus.pi_tool import main

    root = _runtime_root(tmp_path)
    output = root / 'host-logs/agent-transcript/index.html'
    monkeypatch.delenv('GOAL_PLUS_DEBUG', raising=False)
    assert main(['--root', str(root), 'host_debug_export']) == 0
    assert not output.exists()
    monkeypatch.setenv('GOAL_PLUS_DEBUG', '1')
    assert main(['--root', str(root), 'host_debug_export']) == 0
    assert output.is_file()


def test_debug_export_failure_does_not_fail_host_callback(tmp_path, monkeypatch):
    from goal_plus.infrastructure import agent_transcript

    monkeypatch.setenv('GOAL_PLUS_DEBUG', '1')
    def fail(*args, **kwargs):
        raise PermissionError('read-only diagnostic output')
    monkeypatch.setattr(agent_transcript, 'export_transcripts', fail)
    agent_transcript.debug_export_codex_transcript(tmp_path, _codex_rollout(tmp_path))
    agent_transcript.debug_export_transcripts(tmp_path)
