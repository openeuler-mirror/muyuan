"""One MCP control contract for both hosts, with caller-owned admission."""

import asyncio
from dataclasses import asdict
import json

from fastmcp import Client
import pytest

from goal_plus.server import create_mcp
from tests._native_session_helpers import controlled_session, session_inputs


pytestmark = pytest.mark.host_process


def test_mcp_control_survives_client_reconnect_and_checks_each_operation(tmp_path, controlled_session):
    control, finished = controlled_session("interrupt")
    operations = []

    def lookup(session_id, operation):
        if session_id != "owned-session":
            raise PermissionError("session does not belong to caller")
        operations.append(operation)
        return control

    mcp = create_mcp(tmp_path / ".gp", caller_role="main", session_controls=lookup)

    async def call(name, **arguments):
        async with Client(mcp) as client:
            result = await client.call_tool("goal_plus_session_" + name, arguments)
            return json.loads(result.content[0].text)

    async def exercise():
        first = control.wake("work", call_id="one", timeout=10)
        assert first.call_id == "one"
        assert first.state == "running"
        assert (await call("wait", session_id="owned-session", call_id="one"))["state"] == "running"
        await call("message", session_id="owned-session", call_id="one", prompt="steer", timeout_seconds=2)
        with pytest.raises(Exception, match="does not belong"):
            await call("interrupt", session_id="other-session", call_id="one", timeout_seconds=2)
        await call("interrupt", session_id="owned-session", call_id="one", timeout_seconds=2)
        result = await call("wait", session_id="owned-session", call_id="one", timeout_seconds=5)
        assert result["state"] == "interrupted"
        assert "result" not in result
        detailed = await call("wait", session_id="owned-session", call_id="one", detail=True)
        assert detailed == asdict(control.wait("one", timeout=0))
        await call("stop", session_id="owned-session")
        assert (await call("close", session_id="owned-session"))["closed"] is True

    asyncio.run(exercise())
    assert set(operations) == {"wait", "message", "interrupt", "stop", "close"}
    assert finished == ["token"]


@pytest.mark.parametrize("role", [None, "ordinary", "candidate", "final_checker"])
def test_control_requires_trusted_main_binding(tmp_path, role):
    from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity

    with pytest.raises(ValueError, match="trusted Main"):
        create_mcp(
            tmp_path / ".gp", caller_role=role,
            caller_agent_session_id="candidate" if role == "candidate" else None,
            caller_final_check=GoalPlusFinalCheckerIdentity(goal_plus_id="gp_1", check_id="check", goal_revision=1)
            if role == "final_checker" else None,
            session_controls=lambda *_: None,
        )


def test_wait_does_not_block_another_control_request(tmp_path, controlled_session):
    control, _ = controlled_session("interrupt")
    mcp = create_mcp(tmp_path / ".gp", caller_role="main", session_controls=lambda *_: control)

    async def exercise():
        async with Client(mcp) as client:
            control.wake("work", call_id="active", timeout=10)
            waiting = asyncio.create_task(client.call_tool("goal_plus_session_wait", {
                "session_id": "owned", "call_id": "active", "timeout_seconds": 8,
            }))
            await asyncio.sleep(0.05)
            await client.call_tool("goal_plus_session_interrupt", {
                "session_id": "owned", "call_id": "active", "timeout_seconds": 2,
            })
            result = await asyncio.wait_for(waiting, timeout=3)
            assert json.loads(result.content[0].text)["state"] == "interrupted"

    asyncio.run(exercise())
