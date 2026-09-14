"""Shared session contract, exercised unchanged against each native adapter."""

from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from goal_plus.execution.native_session import CodexSession, PiSession
from tests._native_session_helpers import session_inputs


@pytest.mark.host_process
@pytest.mark.parametrize("agent_harness,actor", [("codex", CodexSession), ("pi", PiSession)])
def test_session_two_invocations_early_completion_and_concurrent_close(session_inputs, agent_harness, actor):
    config, registration, registered, finished = session_inputs(agent_harness)
    session = actor(config, registration, timeout=5)
    try:
        first = session.run("first", timeout=5)
        second = session.run("second", timeout=5)
        assert first.native_session_id == second.native_session_id == "session"
        assert first.invocation_id != second.invocation_id
        assert first.text == "answer-1"
        assert second.text == "answer-2"
        assert first.state == second.state == "completed"
        with pytest.raises(RuntimeError, match="active invocation"):
            session.message("idle message", timeout=5)
    finally:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: session.close(), range(2)))
    assert len(registered) == 1
    assert finished == ["token"]
    assert session.rpc.process.poll() is not None


@pytest.mark.host_process
@pytest.mark.parametrize("agent_harness,actor", [("codex", CodexSession), ("pi", PiSession)])
def test_interrupt_returns_exact_invocation_and_session_can_run_again(session_inputs, agent_harness, actor):
    config, registration, _, finished = session_inputs(agent_harness, "interrupt")
    session = actor(config, registration, timeout=5)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            results = []
            for prompt in ("one", "two"):
                invocation = executor.submit(session.run, prompt, timeout=5)
                deadline = time.monotonic() + 5
                while not (config.process.cwd / "active").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                with pytest.raises(RuntimeError, match="active invocation"):
                    session.run("overlap", timeout=5)
                session.message("a message", timeout=5)
                session.interrupt(timeout=5)
                results.append(invocation.result(timeout=5))
            assert all(result.state == "interrupted" for result in results)
            assert results[0].invocation_id != results[1].invocation_id
    finally:
        session.close()
    assert finished == ["token"]
