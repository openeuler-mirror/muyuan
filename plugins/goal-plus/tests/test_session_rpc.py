"""Shared transport bounds and resource registration, without a host workflow."""

import pytest

from goal_plus.execution import session_rpc
from goal_plus.execution.process_output import PROCESS_OUTPUT_LIMIT_BYTES
from goal_plus.execution.session_rpc import SessionRpcProcess, SessionTransportError
from goal_plus.ports.process import ProcessRegistration
from tests._native_session_helpers import session_inputs


pytestmark = pytest.mark.host_process


@pytest.mark.parametrize("stage", ["construct", "partial_start"])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_rpc_setup_failure_reaps_process_and_finishes_registered_resource(session_inputs, monkeypatch, stage, error):
    config, registration, _, finished = session_inputs("pi")
    connections = []

    class Connection(session_rpc.SessionRpcConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            connections.append(self)
            if stage == "construct":
                raise error("RPC setup failed")

        def start(self):
            super().start()
            raise error("RPC setup failed")

    monkeypatch.setattr(session_rpc, "SessionRpcConnection", Connection)
    try:
        with pytest.raises(error, match="RPC setup failed"):
            SessionRpcProcess(config.process, registration, lambda _: None)
        assert connections[0].process.poll() is not None
        assert all(not thread.is_alive() for thread in connections[0]._threads)
        assert finished == ([] if stage == "construct" else ["token"])
    finally:
        for connection in connections:
            if connection.process.poll() is None:
                connection.process.kill()
            connection.process.wait(timeout=5)
            connection.finish(timeout=5)
            for stream in (connection.process.stdin, connection.process.stdout, connection.process.stderr):
                stream.close()


def test_registration_rejection_never_executes_native_command(session_inputs):
    config, _, _, finished = session_inputs("codex")

    def reject(pid):
        raise ValueError("scope is closed")

    with pytest.raises(ValueError, match="scope is closed"):
        SessionRpcProcess(config.process, ProcessRegistration(reject, finished.append), lambda _: None)
    assert not (config.process.cwd / "executed").exists()
    assert finished == []


def test_stalled_native_stdin_is_bounded_and_cannot_accept_another_frame(session_inputs):
    config, registration, _, finished = session_inputs("codex", "no_read")
    rpc = SessionRpcProcess(config.process, registration, lambda _: None)
    try:
        with pytest.raises(TimeoutError, match="input timed out"):
            rpc.request({"message": "x" * PROCESS_OUTPUT_LIMIT_BYTES}, timeout=0.1)
        with pytest.raises(SessionTransportError, match="input timed out"):
            rpc.request({"message": "next"}, timeout=1)
    finally:
        rpc.close()
    assert finished == ["token"]
