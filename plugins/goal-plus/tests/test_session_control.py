"""Shared control behavior, exercised against both native wire adapters."""

import pytest
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from goal_plus.execution.session_control import NativeSessionControl
from goal_plus.ports.session import SessionCallRequest, SessionInvocationResult

from tests._native_session_helpers import controlled_session, session_inputs


pytestmark = pytest.mark.host_process


def test_two_calls_keep_native_session_and_exact_results(controlled_session):
    control, finished = controlled_session()
    first = control.wake("first", call_id="one", timeout=5)
    first = control.wait("one", timeout=5)
    assert first.state == "completed"
    assert first.result.text == "answer-1"
    control.wake("second", call_id="two", timeout=5)
    second = control.wait("two", timeout=5)
    assert second.state == "completed"
    assert second.result.text == "answer-2"
    assert first.native_session_id == second.native_session_id
    assert first.result.invocation_id != second.result.invocation_id
    assert control.wait("one", timeout=0) == first
    assert control.wake("first", call_id="one", timeout=5) == first
    with pytest.raises(ValueError, match="different input"):
        control.wake("changed", call_id="one", timeout=5)
    control.close()
    assert finished == ["token"]


def test_wait_timeout_does_not_cancel_and_interrupt_is_exact(controlled_session):
    control, _ = controlled_session("interrupt")
    control.wake("first", call_id="one", timeout=5)
    assert control.wait("one", timeout=0.01).state == "running"
    with pytest.raises(RuntimeError, match="active invocation"):
        control.wake("second", call_id="two", timeout=5)
    control.message("one", "steer", timeout=2)
    control.interrupt("one", timeout=2)
    assert control.wait("one", timeout=5).state == "interrupted"
    control.wake("second", call_id="two", timeout=5)
    with pytest.raises(ValueError, match="current invocation"):
        control.interrupt("one", timeout=2)
    control.interrupt("two", timeout=2)
    assert control.wait("two", timeout=5).state == "interrupted"


def test_stop_fences_future_work_and_preserves_observable_outcome(controlled_session):
    control, finished = controlled_session("interrupt")
    control.wake("first", call_id="one", timeout=5)
    control.message("one", "accepted", timeout=2)
    control.stop()
    result = control.wait("one", timeout=5)
    assert result.state == "failed"
    assert result.error
    assert finished == ["token"]
    with pytest.raises(RuntimeError, match="stopped"):
        control.wake("second", call_id="two", timeout=5)
    assert control.wake("first", call_id="one", timeout=5) == result


def test_invalid_wait_and_unknown_call_do_not_interrupt_current_work(controlled_session):
    control, _ = controlled_session("interrupt")
    control.wake("first", call_id="one", timeout=5)
    for timeout in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="timeout"):
            control.wait("one", timeout=timeout)
    with pytest.raises(KeyError):
        control.wait("missing", timeout=0)
    assert control.wait("one", timeout=0).state == "running"
    control.interrupt("one", timeout=2)
    assert control.wait("one", timeout=5).state == "interrupted"


def test_stop_can_retain_native_history_until_resource_close():
    from types import SimpleNamespace
    from goal_plus.execution.session_control import NativeSessionControl

    resources = {"execution": True, "session": True}
    actor = SimpleNamespace(native_session_id="retained", close=lambda: resources.update(execution=False, session=False))
    control = NativeSessionControl(actor, stop_action=lambda: resources.update(execution=False))
    control.stop()
    assert resources == {"execution": False, "session": True}
    with pytest.raises(RuntimeError, match="stopped"):
        control.wake("new work", call_id="after-stop", timeout=1)
    control.close()
    assert resources == {"execution": False, "session": False}


def test_explicit_reopen_retains_results_and_requires_successful_restart():
    actor = SimpleNamespace(native_session_id="retained", close=Mock(), run=Mock(
        return_value=SessionInvocationResult("retained", "turn-2", "completed", "second", agent_harness="codex", runtime_provider="direct"),
    ))
    restart = Mock(side_effect=[RuntimeError("resource still active"), None])
    control = NativeSessionControl(actor, stop_action=Mock(), reopen_action=restart)
    first = SessionInvocationResult("retained", "turn-1", "completed", "first", agent_harness="codex", runtime_provider="direct")
    control.restore_call(SessionCallRequest("one", "first", 5), first)
    try:
        control.stop()
        with pytest.raises(RuntimeError, match="resource still active"):
            control.reopen()
        with pytest.raises(RuntimeError, match="stopped"):
            control.wake("second", call_id="two", timeout=5)
        control.reopen()
        control.wake("second", call_id="two", timeout=5)
        assert control.wait("two", timeout=5).result.invocation_id == "turn-2"
        assert control.wait("one", timeout=0).result == first
        control.reopen()
        assert restart.call_count == 2
    finally:
        control.close()
    with pytest.raises(RuntimeError, match="closed"):
        control.reopen()


@pytest.mark.parametrize("settled", [False, True])
def test_restoration_preserves_exact_call_without_running_actor_again(settled):
    actor = SimpleNamespace(native_session_id="native", run=Mock(), close=Mock())
    control = NativeSessionControl(actor)
    request = SessionCallRequest("call", "original", 10)
    result = SessionInvocationResult("native", "turn", "completed", "exact answer", agent_harness="codex", runtime_provider="direct")
    release = threading.Event()

    def observe(started):
        started()
        assert release.wait(5)
        return result

    try:
        control.restore_call(request, result if settled else observe)
        assert control.wake("original", call_id="call", timeout=10).state == ("completed" if settled else "running")
        with pytest.raises(ValueError, match="different input"):
            control.wake("changed", call_id="call", timeout=10)
        release.set()
        assert control.wait("call", timeout=5).result == result
        actor.run.assert_not_called()
    finally:
        release.set()
        control.close()
