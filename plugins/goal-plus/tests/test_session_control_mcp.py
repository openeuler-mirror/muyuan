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
        polled = await call(
            "wait_any", calls=[{"session_id": "owned-session", "call_id": "one"}], timeout_seconds=0,
        )
        assert polled["finished"] == []
        assert polled["running"] == [
            {"session_id": "owned-session", "call_id": "one", "state": "running",
             "native_session_id": "session"}
        ]
        await call("message", session_id="owned-session", call_id="one", prompt="steer", timeout_seconds=2)
        with pytest.raises(Exception, match="does not belong"):
            await call("interrupt", session_id="other-session", call_id="one", timeout_seconds=2)
        await call("interrupt", session_id="owned-session", call_id="one", timeout_seconds=2)
        result = await call(
            "wait_any", calls=[{"session_id": "owned-session", "call_id": "one"}], timeout_seconds=5,
        )
        assert result["finished"][0]["state"] == "interrupted"
        assert "result" not in result["finished"][0]
        assert result["running"] == []
        detailed = await call(
            "wait_any", calls=[{"session_id": "owned-session", "call_id": "one"}], detail=True,
        )
        assert detailed["finished"][0] == {
            "session_id": "owned-session", **asdict(control.wait("one", timeout=0)),
        }
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


def test_wait_rejects_timeout_above_stable_exploration_cap(tmp_path):
    def reject_lookup(session_id, operation):
        raise AssertionError("cap must be checked before touching the session control")

    mcp = create_mcp(tmp_path / ".gp", caller_role="main", session_controls=reject_lookup)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(Exception, match="must be <= 900"):
                await client.call_tool("goal_plus_session_wait_any", {
                    "calls": [{"session_id": "owned", "call_id": "one"}], "timeout_seconds": 901,
                })

    asyncio.run(exercise())


def test_wait_accepts_900_second_boundary_without_waiting_for_a_running_call(tmp_path):
    from goal_plus.ports.session import SessionCallSnapshot

    class ImmediateControl:
        def wait(self, call_id, *, timeout):
            return SessionCallSnapshot(call_id=call_id, native_session_id="native", state="completed")

    mcp = create_mcp(tmp_path / ".gp", caller_role="main",
                     session_controls=lambda *_: ImmediateControl())

    async def exercise():
        async with Client(mcp) as client:
            wait_tool = next(
                tool for tool in await client.list_tools()
                if tool.name == "goal_plus_session_wait_any"
            )
            wait_schema = wait_tool.model_dump(by_alias=True)["inputSchema"]
            assert wait_schema["properties"]["timeout_seconds"]["default"] == 900.0
            result = await client.call_tool("goal_plus_session_wait_any", {
                "calls": [{"session_id": "owned", "call_id": "one"}], "timeout_seconds": 900,
            })
            assert json.loads(result.content[0].text)["finished"][0]["state"] == "completed"

    asyncio.run(exercise())


def test_wait_returns_first_finished_call_with_running_roster(tmp_path, controlled_session):
    from goal_plus.ports.session import SessionCallSnapshot

    finished_control, _ = controlled_session("normal")

    class HeldControl:
        def wait(self, call_id, *, timeout):
            return SessionCallSnapshot(call_id=call_id, native_session_id="held-native", state="running")

    controls = {"done-session": finished_control, "slow-session": HeldControl()}
    looked_up = []

    def lookup(session_id, operation):
        assert operation == "wait"
        looked_up.append(session_id)
        return controls[session_id]

    mcp = create_mcp(tmp_path / ".gp", caller_role="main", session_controls=lookup)

    async def exercise():
        async with Client(mcp) as client:
            finished_control.wake("work", call_id="done", timeout=10)
            response = await client.call_tool("goal_plus_session_wait_any", {
                "calls": [
                    {"session_id": "slow-session", "call_id": "slow"},
                    {"session_id": "done-session", "call_id": "done"},
                ],
                "timeout_seconds": 2,
            })
            payload = json.loads(response.content[0].text)
            assert [entry["call_id"] for entry in payload["finished"]] == ["done"]
            assert payload["finished"][0]["session_id"] == "done-session"
            assert payload["finished"][0]["state"] == "completed"
            assert payload["finished"][0]["result_available"] is True
            assert [entry["call_id"] for entry in payload["running"]] == ["slow"]
            assert payload["running"][0]["state"] == "running"
            duplicate = await client.call_tool("goal_plus_session_wait_any", {
                "calls": [
                    {"session_id": "done-session", "call_id": "done"},
                    {"session_id": "done-session", "call_id": "done"},
                ],
            }, raise_on_error=False)
            assert duplicate.is_error

    asyncio.run(exercise())
    assert set(looked_up) == {"done-session", "slow-session"}


def test_wait_does_not_block_another_control_request(tmp_path, controlled_session):
    control, _ = controlled_session("interrupt")
    mcp = create_mcp(tmp_path / ".gp", caller_role="main", session_controls=lambda *_: control)

    async def exercise():
        async with Client(mcp) as client:
            control.wake("work", call_id="active", timeout=10)
            waiting = asyncio.create_task(client.call_tool("goal_plus_session_wait_any", {
                "calls": [{"session_id": "owned", "call_id": "active"}], "timeout_seconds": 8,
            }))
            await asyncio.sleep(0.05)
            await client.call_tool("goal_plus_session_interrupt", {
                "session_id": "owned", "call_id": "active", "timeout_seconds": 2,
            })
            result = await asyncio.wait_for(waiting, timeout=3)
            assert json.loads(result.content[0].text)["finished"][0]["state"] == "interrupted"

    asyncio.run(exercise())
