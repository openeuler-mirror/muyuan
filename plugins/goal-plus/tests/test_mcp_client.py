from __future__ import annotations

import pytest

from goal_plus import mcp_client, server


@pytest.mark.parametrize(("tool", "arguments", "payload"), [
    ("goal_plus_status", {"goal_plus_id": "fixture"},
     {"state": "ready", "result": [1, 2], "text": "中文"}),
    ("goal_plus_search_start_batch", {"run_id": "run_1", "plan_id": "plan_1"},
     [{"candidate_id": "c001"}]),
    ("goal_plus_search_start_batch", {"run_id": "run_1", "plan_id": "plan_1"}, []),
    ("goal_plus_search_get_agent_context", {"agent_session_id": "agent_1"},
     {"state": "context"}),
])
def test_call_mcp_tool_decodes_shared_json_results(
    monkeypatch, tmp_path, tool, arguments, payload,
):
    owner = server.GoalPlusTools if tool == "goal_plus_status" else server.SearchTools
    monkeypatch.setattr(owner, tool, lambda *args, **kwargs: payload)

    assert mcp_client.call_mcp_tool(tmp_path, tool, arguments) == payload


def test_call_mcp_tool_preserves_tool_errors(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise ValueError("GoalPlusUnavailable: requested Goal is unavailable")

    monkeypatch.setattr(server.GoalPlusTools, "goal_plus_status", unavailable)

    with pytest.raises(RuntimeError, match="GoalPlusUnavailable: requested Goal is unavailable"):
        mcp_client.call_mcp_tool(tmp_path, "goal_plus_status", {"goal_plus_id": "fixture"})
