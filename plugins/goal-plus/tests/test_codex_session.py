"""Codex app-server wire and dynamic tool callback behavior."""

import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from goal_plus.execution.native_session import CodexSession
from goal_plus.execution.session_rpc import SessionTransportError
from goal_plus.ports.session import SessionToolDefinition
from tests._native_session_helpers import session_inputs


pytestmark = [pytest.mark.codex, pytest.mark.host_process]


def test_usage_is_incremental_and_cached_input_is_not_double_counted(session_inputs):
    config, registration, _, _ = session_inputs("codex", "normal")
    session = CodexSession(config, registration, timeout=5)
    try:
        first = session.run("first", timeout=5)
        second = session.run("second", timeout=5)
        assert first.usage == second.usage
        assert second.usage["input"] == 80
        assert second.usage["cacheRead"] == 20
        assert second.usage["output"] == 10
        assert second.usage["totalTokens"] == 110
    finally:
        session.close()


@pytest.mark.parametrize("scenario", ["eof", "callback"])
def test_start_failure_releases_registered_process(session_inputs, scenario):
    config, registration, _, finished = session_inputs("codex", scenario)
    with pytest.raises(SessionTransportError):
        CodexSession(config, registration, timeout=5)
    assert finished == ["token"]


@pytest.mark.parametrize("scenario,accepted", [
    ("tool", True), ("tool_repeat", True), ("tool_foreign", False), ("tool_stale", False),
])
def test_dynamic_tool_bridge_binds_native_invocation(session_inputs, scenario, accepted):
    calls = []

    class Bridge:
        definitions = (SessionToolDefinition("fixture_echo", "Return text", {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }),)

        def call(self, name, arguments):
            calls.append((name, arguments))
            return arguments

    config, registration, _, finished = session_inputs("codex", scenario)
    session = CodexSession(config, registration, timeout=5, tool_bridge=Bridge())
    try:
        result = session.run("call the tool", timeout=5)
        content = json.loads(result.text)
        assert result.state == "completed"
        assert content["success"] is accepted
        assert len(calls) == (1 if accepted else 0)
        if accepted:
            assert json.loads(content["contentItems"][0]["text"]) == {"text": "from-mcp"}
    finally:
        session.close()
    assert finished == ["token"]


def test_interrupt_acknowledgement_does_not_wait_for_active_tool_callback(session_inputs):
    entered, release = threading.Event(), threading.Event()

    class Bridge:
        definitions = (SessionToolDefinition("fixture_echo", "A controlled slow tool", {"type": "object"}),)

        def call(self, name, arguments):
            entered.set()
            assert release.wait(5)
            return arguments

    config, registration, _, finished = session_inputs("codex", "tool")
    session = CodexSession(config, registration, timeout=5, tool_bridge=Bridge())
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            invocation = executor.submit(session.run, "call the tool", timeout=5)
            try:
                assert entered.wait(5)
                session.interrupt(timeout=1)
                assert not invocation.done(), "invocation must await its admitted tool result"
            finally:
                release.set()
            assert invocation.result(timeout=5).state == "interrupted"
    finally:
        release.set()
        session.close()
    assert finished == ["token"]
