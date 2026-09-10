"""Host-free acceptance for shared process diagnostics and their file adapter."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.bootstrap import build_process_monitor
from goal_plus.execution.process_monitor import ProcessMonitor
from goal_plus.infrastructure.state.process_monitor import FileProcessMonitorSink
from goal_plus.ports.process_monitor import ProcessMonitorIdentity


class Clock:
    def timestamp(self):
        return "2026-09-05T00:00:00Z"


def identity(**kwargs):
    return ProcessMonitorIdentity(
        run_id="run_one", candidate_id="c001", iteration=3, attempt=2,
        host="pi-rpc", model="provider/model", timeout_seconds=30, **kwargs,
    )


@pytest.mark.parametrize("verifier", [False, True])
def test_role_identity_and_event_format_are_preserved(tmp_path, verifier):
    path = tmp_path / "monitor.json"
    monitor = build_process_monitor(
        path, identity(task_kind="llm_verifier" if verifier else None),
        include_event_timestamps=not verifier,
    )
    event = {
        "type": "message_update", "timestamp": 2,
        "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "abc"},
        "message": {"role": "assistant", "stopReason": "stop", "responseId": "r1"},
    }
    stdout = json.dumps(event) + "\nnot json\n[]\n\n"
    monitor.observe(
        "completed", SimpleNamespace(pid=12, returncode=0),
        stdout=stdout, stderr="diagnostic", force=True,
    )
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["run_id"] == "run_one"
    assert data["candidate_id"] == "c001"
    assert data["iteration"] == 3
    assert data["attempt"] == 2
    assert data["pid"] == 12
    assert data["process_returncode"] == 0
    assert data["state"] == "completed"
    assert data["stdout_bytes"] == len(stdout.encode())
    assert data["stderr_tail"] == "diagnostic"
    assert data["json_lines"] == 1
    assert data["non_json_lines"] == 2
    assert data["event_type_counts"] == {"message_update": 1}
    assert data["assistant_event_type_counts"] == {"text_delta": 1}
    assert data["last_events"] == [{
        "type": "message_update", "assistant_event_type": "text_delta",
        "content_index": 0, "delta_bytes": 3, "message_role": "assistant",
        "stop_reason": "stop", "response_id": "r1",
        **({"timestamp": 2} if not verifier else {}),
    }]
    assert data.get("task_kind") == ("llm_verifier" if verifier else None)
    if not verifier:
        assert "task_kind" not in data


def test_monitor_throttles_progress_but_always_records_forced_terminal():
    written = []
    ticks = [100.0]
    monitor = ProcessMonitor(
        identity(), sink=written.append, clock=Clock(), monotonic=lambda: ticks[0],
    )
    process = SimpleNamespace(pid=12, returncode=None)
    monitor.observe("running", process, force=True)
    ticks[0] += 4.9
    monitor.observe("running", process, stdout="not yet")
    assert len(written) == 1
    ticks[0] += 0.1
    monitor.observe("running", process, stdout="updated")
    assert len(written) == 2
    monitor.observe("failed", process, detail="invalid output", force=True)
    assert len(written) == 3
    assert written[-1].state == "failed"
    assert written[-1].detail == "invalid output"
    assert written[-1].elapsed_seconds == 5.0
    assert written[0].state == "running"


def test_unwritable_monitor_is_best_effort_and_retry_is_not_throttled():
    written = []

    def sink(snapshot):
        written.append(snapshot)
        if len(written) == 1:
            raise OSError("read only")

    monitor = ProcessMonitor(identity(), sink=sink, clock=Clock(), monotonic=lambda: 100.0)
    process = SimpleNamespace(pid=12, returncode=None)
    monitor.observe("running", process)
    monitor.observe("terminated", process, detail="closed")
    assert len(written) == 2
    assert written[-1].state == "terminated"


def test_missing_monitor_path_has_no_file_side_effect(tmp_path):
    monitor = build_process_monitor(None, identity())
    monitor.observe("running", SimpleNamespace(), stdout="invalid json", force=True)
    assert not list(tmp_path.iterdir())


def test_output_tail_and_event_summary_are_bounded(tmp_path):
    path = tmp_path / "nested" / "monitor.json"
    monitor = build_process_monitor(path, identity(), include_event_timestamps=True)
    stdout = "\n".join(json.dumps({
        "type": "message_update", "timestamp": True,
        "assistantMessageEvent": {"type": "text_delta", "contentIndex": True, "delta": "\u00e9"},
    }) for _ in range(20))
    monitor.observe(
        "timed_out", SimpleNamespace(pid=12, returncode=-9),
        stdout=stdout, stderr="e" * 3000, detail="d" * 3000, force=True,
    )
    data = json.loads(path.read_text())
    assert data["stdout_tail"] == stdout[-2000:]
    assert data["stderr_tail"] == "e" * 2000
    assert data["detail"] == "d" * 2000
    assert data["stdout_bytes"] == len(stdout.encode())
    assert data["event_type_counts"] == {"message_update": 20}
    assert len(data["last_events"]) == 12
    assert all(item == {
        "type": "message_update", "assistant_event_type": "text_delta", "delta_bytes": 2,
    } for item in data["last_events"])


def test_monitor_sink_preserves_previous_complete_record_on_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "monitor.json"
    monitor = ProcessMonitor(identity(), sink=FileProcessMonitorSink(path), clock=Clock())
    process = SimpleNamespace(pid=12, returncode=None)
    monitor.observe("running", process, stdout="first", force=True)
    before = path.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    monitor.observe("failed", process, stdout="second", force=True)
    assert path.read_bytes() == before
