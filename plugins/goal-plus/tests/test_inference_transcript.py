"""No-session verifier and annotator diagnostics are independent of business state."""

import json
from pathlib import Path

import pytest

from goal_plus.infrastructure.agent_transcript import collect_runtime_transcripts, export_transcripts
from goal_plus.infrastructure.inference_transcript import FileInferenceTranscript, inference_capture_enabled
from goal_plus.ports.inference import (
    InferenceInvocation, InferenceTranscriptIdentity, PiInferenceTools,
    StructuredInferenceRequest, StructuredInferenceResult,
)
from goal_plus.ports.process import ProcessOutcome
from goal_plus.projections.agent_transcript import (
    AgentTranscriptSession, TranscriptEvent, render_transcript_html, render_transcript_jsonl,
)


@pytest.mark.parametrize("role", ["annotator", "verifier"])
@pytest.mark.parametrize("complete", [True, False])
def test_pi_message_stream_is_collapsed_without_losing_events(role: str, complete: bool):
    events = [
        TranscriptEvent(kind="user", label="prompt", content="INPUT_MARKER"),
        TranscriptEvent(kind="meta", label="host event: message_start", content="START_MARKER"),
        TranscriptEvent(kind="meta", label="host event: message_update", content="DELTA_MARKER"),
    ]
    if complete:
        events.extend([
            TranscriptEvent(kind="thinking", label="thinking", content="COMPLETE_THINKING"),
            TranscriptEvent(kind="tool_call", label="artifact_view_read", content="CALL_MARKER"),
            TranscriptEvent(kind="tool_result", label="artifact_view_read", content="RESULT_MARKER"),
            TranscriptEvent(kind="assistant", label="structured output", content="OUTPUT_MARKER"),
        ])
    else:
        events.append(TranscriptEvent(kind="error", label="stderr", content="INTERRUPTED_MARKER"))
    events.append(TranscriptEvent(kind="meta", label="acceptance and capture", content="STATE_MARKER"))
    session = AgentTranscriptSession(session_id="invocation_1", harness="pi", role=role, events=events)
    original_jsonl = render_transcript_jsonl([session])
    page = render_transcript_html([session])
    conversation, raw = page.split('<details class="raw-message-stream">', 1)
    assert raw.startswith("<summary>Raw message stream (2 events)</summary>")
    assert "INPUT_MARKER" in conversation and "STATE_MARKER" in conversation
    assert "DELTA_MARKER" not in conversation and "START_MARKER" not in conversation
    assert "DELTA_MARKER" in raw and "START_MARKER" in raw
    assert '<span class="seq">2</span>' in raw and '<span class="seq">3</span>' in raw
    if complete:
        for marker in ("COMPLETE_THINKING", "CALL_MARKER", "RESULT_MARKER", "OUTPUT_MARKER"):
            assert marker in conversation
    else:
        assert "INTERRUPTED_MARKER" in conversation
    assert render_transcript_jsonl([session]) == original_jsonl
    assert len([json.loads(line) for line in original_jsonl.splitlines()]) == len(events)


@pytest.mark.parametrize("role", ["main", "candidate"])
def test_session_metadata_does_not_create_raw_stream_section(role: str):
    session = AgentTranscriptSession(session_id="session_1", harness="pi", role=role, events=[
        TranscriptEvent(kind="meta", label="runtime start", content="RUNTIME_MARKER"),
        TranscriptEvent(kind="meta", label="tool contracts", content="TOOLS_MARKER"),
        TranscriptEvent(kind="assistant", label="reply", content="REPLY_MARKER"),
    ])
    page = render_transcript_html([session])
    assert '<details class="raw-message-stream">' not in page
    assert all(marker in page for marker in ("RUNTIME_MARKER", "TOOLS_MARKER", "REPLY_MARKER"))


def _request(harness: str, prompt: str) -> StructuredInferenceRequest:
    return StructuredInferenceRequest(
        agent_harness=harness, instructions="Evaluate this settled attempt.",
        prompt=prompt, output_schema={"type": "object", "properties": {"quality_score": {"type": "number"}}},
        model="provider/model", reasoning_effort="low", timeout_seconds=10,
        timeout_detail="timeout", cancellation_detail="cancelled",
        pi_tools=PiInferenceTools(("submit", "artifact_view_read"),
                                  "export default function () {}", "TEST_OUTPUT") if harness == "pi" else None,
    )


def _result(invocation_id: str, harness: str, stdout: str) -> StructuredInferenceResult:
    return StructuredInferenceResult(
        process=ProcessOutcome("process_exited", stdout, ""), stop_detail=None,
        pid=123, returncode=0, output_text='{"quality_score":0.8}',
        payload={"quality_score": 0.8}, output_error=None, assistant_message=None,
        usage={"input_tokens": 42}, invocation=InferenceInvocation(
            invocation_id, harness, "provider/model", "low",
            "2026-09-28T00:00:00Z", "2026-09-28T00:00:01Z", 1.0,
        ),
    )


def test_independent_invocations_keep_exact_target_and_full_context(tmp_path: Path):
    long_prompt = "target text " * 3000
    for role, harness, attempt, iteration in (
        ("verifier", "codex", 1, 14),
        ("verifier", "codex", 2, 14),
        ("annotator", "pi", 1, 15),
    ):
        invocation_id = f"invocation_{role}_{attempt}"
        identity = InferenceTranscriptIdentity(
            role=role, run_id="run_1", task_id=f"task_{role}",
            settlement_id=f"settlement_{iteration}", candidate_id="c001",
            iteration=iteration, attempt=attempt,
            artifact={"commit": f"commit_{iteration}"},
            references=[{"candidate_id": "c002"}] if role == "annotator" else [],
        )
        capture = FileInferenceTranscript(tmp_path, identity)
        request = _request(harness, long_prompt)
        observer = capture.start(request, invocation_id, "2026-09-28T00:00:00Z", request.output_schema)
        assert (observer is not None) == (harness == "pi")
        stdout = ""
        if harness == "pi":
            provider_path = capture.directory / "provider-requests.jsonl"
            provider_path.write_text(json.dumps({
                "type": "provider_request", "sequence": 1,
                "payload": {"messages": [{"role": "user", "content": long_prompt}]},
            }) + "\n", encoding="utf-8")
            stdout = "\n".join(json.dumps(item) for item in (
                {"type": "message_end", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "name": "artifact_view_read", "arguments": {"path": "src/a.py"}}]}},
                {"type": "message_end", "message": {"role": "toolResult", "toolName": "artifact_view_read",
                                                    "content": [{"type": "text", "text": "exact file contents"}]}},
            )) + "\n"
        capture.observe(stdout[:len(stdout) // 2], "")
        capture.finish(_result(invocation_id, harness, stdout))
        capture.accept("invalid score" if attempt == 1 and role == "verifier" else None)

    sessions = collect_runtime_transcripts(tmp_path)
    assert len(sessions) == 3
    assert [(item.role, item.identity["iteration"], item.identity["attempt"])
            for item in sessions] == [("verifier", 14, 1), ("verifier", 14, 2), ("annotator", 15, 1)]
    annotation = sessions[2]
    assert annotation.identity["settlement_id"] == "settlement_15"
    assert any(event.kind == "tool_result" and "exact file contents" in event.content
               for event in annotation.events)
    assert any(event.label == "provider request #1" and long_prompt in event.content
               for event in annotation.events)
    assert "Codex CLI provider request is not observable" in " ".join(sessions[0].warnings)
    html = Path(export_transcripts(tmp_path)["output"], "index.html").read_text(encoding="utf-8")
    assert "verifier c001 / iteration 14 / attempt 2" in html
    assert "annotator c001 / iteration 15 / attempt 1" in html
    assert "target text " * 3000 in html
    assert "chars truncated" not in html
    assert "invalid score" in html
    assert "exact file contents" in html


def test_missing_provider_observation_is_partial(tmp_path: Path):
    capture = FileInferenceTranscript(tmp_path, InferenceTranscriptIdentity(
        "annotator", "run_1", "task_1", "settlement_1", "c001", 1, 1,
    ))
    request = _request("pi", "prompt")
    capture.start(request, "invocation_missing", "2026-09-28T00:00:00Z", request.output_schema)
    capture.finish(_result("invocation_missing", "pi", ""))
    record = json.loads((capture.directory / "record.json").read_text(encoding="utf-8"))
    assert record["capture_status"] == "partial"
    assert "provider request hook data unavailable" in record["warnings"]


def test_full_inference_capture_requires_explicit_host_opt_in(monkeypatch):
    monkeypatch.delenv("GOAL_PLUS_DEBUG", raising=False)
    assert not inference_capture_enabled()
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    assert inference_capture_enabled()
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "0")
    assert not inference_capture_enabled()


def test_worker_link_requires_matching_settlement_and_iteration(tmp_path: Path):
    candidate = tmp_path / "runs" / "run_1" / "candidates" / "c001" / "candidate.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text(json.dumps({"iterations": [
        {"iteration": 7, "settlement_id": "settlement_7", "agent_session_id": "worker_7"},
        {"iteration": 8, "settlement_id": "settlement_8", "agent_session_id": "worker_8"},
    ]}), encoding="utf-8")
    history = tmp_path / "pi-worker.jsonl"
    history.write_text(json.dumps({"type": "session", "id": "worker_7",
                                   "timestamp": "2026-09-28T00:00:00Z"}) + "\n", encoding="utf-8")
    agent = tmp_path / "runs" / "run_1" / "agent_sessions" / "worker_7.json"
    agent.parent.mkdir(parents=True)
    agent.write_text(json.dumps({"agent_session_id": "worker_7", "candidate_id": "c001",
                                 "host": "pi", "session_handle": {"agent_harness": "pi",
                                 "metadata": {"session_file": str(history)}}}), encoding="utf-8")
    capture = FileInferenceTranscript(tmp_path, InferenceTranscriptIdentity(
        "verifier", "run_1", "task_7", "settlement_7", "c001", 7, 1,
    ))
    request = _request("codex", "target")
    capture.start(request, "invocation_worker_7", "2026-09-28T00:00:00Z", request.output_schema)
    capture.finish(_result("invocation_worker_7", "codex", ""))
    sessions = collect_runtime_transcripts(tmp_path)
    verifier = next(item for item in sessions if item.role == "verifier")
    assert verifier.identity["worker_session_id"] == "worker_7"
    page = Path(export_transcripts(tmp_path)["output"], "index.html").read_text(encoding="utf-8")
    assert '<a href="#s1">candidate c001</a>' in page

    candidate.write_text(json.dumps({"iterations": [
        {"iteration": 7, "settlement_id": "different", "agent_session_id": "worker_7"},
    ]}), encoding="utf-8")
    assert next(
        item for item in collect_runtime_transcripts(tmp_path) if item.role == "verifier"
    ).identity["worker_session_id"] is None
    page = Path(export_transcripts(tmp_path)["output"], "index.html").read_text(encoding="utf-8")
    assert "<tr><td>worker</td><td>unavailable</td></tr>" in page
