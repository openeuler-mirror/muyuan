"""The Pi bridge consumes the public MCP contract without a second business schema."""

import asyncio
from pathlib import Path

import pytest

from goal_plus.mcp_client import call_mcp_tool, mcp_manifest, McpSessionToolBridge
from goal_plus.server import create_mcp
from tests._fastmcp_helpers import tools_by_name
from goal_plus.tools import CANDIDATE_TOOL_NAMES, FINAL_CHECKER_TOOL_NAMES
from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.codex, pytest.mark.pi]


def test_pi_manifest_consumes_the_complete_shared_fastmcp_contract(tmp_path):
    manifest = mcp_manifest(tmp_path / ".gp")
    server = asyncio.run(tools_by_name(create_mcp(tmp_path / ".gp")))
    tools = {tool["name"]: tool for tool in manifest["tools"]}
    assert set(tools) == set(server)
    for name, tool in server.items():
        assert tools[name]["inputSchema"] == tool.parameters
        assert tools[name]["description"] == tool.description
    assert manifest["server"]["name"] == "goal-plus"
    assert not (tmp_path / ".gp").exists()
    local = (ROOT / "assets/pi/extensions/lib/goal-plus-schemas.ts").read_text()
    assert "goal_plus_search_freeze_spec:" not in local
    assert "goal_plus_status:" not in local


def test_shared_bridge_validates_schema_before_domain_mutation(tmp_path):
    root = tmp_path / ".gp"
    with pytest.raises(RuntimeError, match="greater than 0"):
        call_mcp_tool(root, "goal_plus_search_plan_next", {"run_id": "missing", "requested_k": 0})
    assert not root.exists()


@pytest.mark.parametrize("role,session_id,visible", [
    ("candidate", "agent-owned", CANDIDATE_TOOL_NAMES),
    ("final_checker", None, FINAL_CHECKER_TOOL_NAMES),
    ("ordinary", None, set()),
])
def test_bound_mcp_lists_only_role_tools(tmp_path, role, session_id, visible):
    root = tmp_path / ".gp"
    server = create_mcp(root, caller_role=role, caller_agent_session_id=session_id,
                        caller_final_check=GoalPlusFinalCheckerIdentity(
                            goal_plus_id="gp_0001", check_id="check", goal_revision=1,
                        ) if role == "final_checker" else None)
    assert set(asyncio.run(tools_by_name(server))) == visible
    assert not root.exists()


def test_bound_candidate_mcp_rejects_peer_identity_before_reading_state(tmp_path):
    from fastmcp import Client

    root = tmp_path / ".gp"

    async def call():
        async with Client(create_mcp(
            root, caller_role="candidate", caller_agent_session_id="agent-owned",
        )) as client:
            return await client.call_tool("goal_plus_search_get_agent_context", {
                "agent_session_id": "agent-peer",
            }, raise_on_error=False)

    result = asyncio.run(call())
    assert result.is_error
    assert "Unexpected keyword argument" in result.content[0].text
    assert not root.exists()


def test_candidate_mcp_binding_requires_host_session_identity(tmp_path):
    with pytest.raises(ValueError, match="candidate MCP requires"):
        create_mcp(tmp_path / ".gp", caller_role="candidate")


def test_native_session_bridge_uses_bound_mcp_schema_and_call(tmp_path):
    root = tmp_path / ".gp"
    bridge = McpSessionToolBridge(root, caller_role="candidate", caller_agent_session_id="owned")
    assert {tool.name for tool in bridge.definitions} == CANDIDATE_TOOL_NAMES
    with pytest.raises(RuntimeError, match="Unexpected keyword argument"):
        bridge.call("goal_plus_search_get_agent_context", {"agent_session_id": "peer"})
    assert not root.exists()
