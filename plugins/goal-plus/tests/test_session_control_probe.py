"""Controlled event ordering for the native-session feasibility probe."""

import io
import json
from types import SimpleNamespace

import pytest

from tests.st.test_st_session_control import CodexSessionProbe


def test_session_probe_keeps_early_completion_and_ignores_old_turn(tmp_path):
    def completed(turn_id):
        return {"method": "turn/completed", "params": {
            "threadId": "native-session", "turn": {"id": turn_id, "status": "completed"},
        }}

    def message(turn_id, text):
        return {"method": "item/completed", "params": {
            "threadId": "native-session", "turnId": turn_id,
            "item": {"type": "agentMessage", "text": text},
        }}

    events = [
        message("old-turn", "old result"), completed("old-turn"),
        message("first-turn", "first result"), completed("first-turn"),
        {"id": 1, "result": {"turn": {"id": "first-turn"}}},
        completed("first-turn"),
        {"id": 2, "result": {"turn": {"id": "second-turn"}}},
        message("second-turn", "second result"), completed("second-turn"),
    ]
    process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(b"".join((json.dumps(event) + "\n").encode() for event in events)),
    )
    probe = CodexSessionProbe(process, timeout=1)
    probe.thread_id = "native-session"
    try:
        first = probe.run("first task", "native-session", tmp_path)
        second = probe.run("second task", "native-session", tmp_path)
    finally:
        probe.reader.join(timeout=1)
    assert first == {
        "session_id": "native-session", "invocation_id": "first-turn",
        "status": "completed", "text": "first result",
    }
    assert second == {
        "session_id": "native-session", "invocation_id": "second-turn",
        "status": "completed", "text": "second result",
    }


def test_session_probe_reports_closed_transport_without_waiting_for_deadline():
    probe = CodexSessionProbe(SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO()), timeout=1)
    try:
        with pytest.raises(EOFError, match="stdout closed"):
            probe.initialize()
    finally:
        probe.reader.join(timeout=1)
