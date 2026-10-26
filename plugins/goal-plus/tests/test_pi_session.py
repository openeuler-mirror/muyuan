"""Pi RPC settled-event semantics."""

import pytest

from goal_plus.execution.native_session import PiSession
from tests._native_session_helpers import session_inputs


pytestmark = [pytest.mark.pi, pytest.mark.host_process]


def test_metrics_interrupt_cannot_bypass_process_cleanup(session_inputs, monkeypatch):
    config, registration, registered, finished = session_inputs("pi")
    session = PiSession(config, registration, timeout=5)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("metrics interrupted")

    monkeypatch.setattr("goal_plus.execution.native_session.collect_pi_metrics", interrupt)
    with pytest.raises(KeyboardInterrupt, match="metrics interrupted"):
        session.run("task", timeout=5)
    assert finished == ["token"]
    assert session.rpc.process.poll() is not None


def test_agent_end_without_settled_cannot_complete_invocation(session_inputs):
    config, registration, registered, finished = session_inputs("pi", "end_without_settled")
    session = PiSession(config, registration, timeout=5)
    with pytest.raises(TimeoutError, match="invocation timed out"):
        session.run("task", timeout=0.15)
    assert finished == ["token"]
    assert session.rpc.process.poll() is not None


def test_usage_covers_all_messages_and_tracks_incremental_session_totals(session_inputs):
    config, registration, _, _ = session_inputs("pi", "normal")
    session = PiSession(config, registration, timeout=5)
    try:
        first = session.run("first", timeout=5)
        second = session.run("second", timeout=5)
        assert first.usage["input"] == 3
        assert second.usage["input"] == 6
        assert first.usage["assistantMessages"] == second.usage["assistantMessages"] == 2
        metrics = second.observation.pi_metrics
        assert metrics["usage_total"]["input"] == 9
        assert metrics["usage_total"]["assistantMessages"] == 4
        assert metrics["baseline_last_entry_id"] == "1-2"
        assert metrics["final_last_entry_id"] == "2-2"
    finally:
        session.close()


def test_usage_read_failure_keeps_result_and_recovers_without_zero_totals(session_inputs, monkeypatch):
    config, registration, _, _ = session_inputs("pi", "normal")
    session = PiSession(config, registration, timeout=5)
    command = session._command

    def fail_entries(name, **kwargs):
        if name == "get_entries":
            raise TimeoutError("metrics read timed out")
        return command(name, **kwargs)

    try:
        monkeypatch.setattr(session, "_command", fail_entries)
        first = session.run("first", timeout=5)
        assert first.state == "completed"
        assert first.usage == {}
        assert first.observation.pi_metrics["usage_total"] is None
        assert first.observation.pi_metrics["errors"]
        monkeypatch.setattr(session, "_command", command)
        second = session.run("second", timeout=5)
        assert second.usage["scope"] == "since_last_observation"
        assert second.usage["assistantMessages"] == 4
        assert second.observation.pi_metrics["usage_total"]["input"] == 9
    finally:
        session.close()
