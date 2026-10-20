"""Response diagnostics preserve tool results and protocol stdout."""

import asyncio
import json

from fastmcp import Client
import pytest

from goal_plus import pi_tool, server


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("tool", ["goal_plus_status", "goal_plus_search_get_agent_context"])
def test_mcp_response_size_is_opt_in_and_counts_utf8(
    tmp_path, monkeypatch, capsys, enabled, tool,
):
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1" if enabled else "0")
    payload = {"private_text": "\u4e2d\u6587", "items": [1, 2, 3]}
    owner = server.GoalPlusTools if tool == "goal_plus_status" else server.SearchTools
    monkeypatch.setattr(owner, tool, lambda *args, **kwargs: payload)

    async def call():
        async with Client(server.create_mcp(tmp_path)) as client:
            key = "goal_plus_id" if tool == "goal_plus_status" else "agent_session_id"
            return await client.call_tool(tool, {key: "fixture"})

    result = asyncio.run(call())
    captured = capsys.readouterr()
    assert json.loads(result.content[0].text) == payload
    assert result.structured_content is None
    assert captured.out == ""
    if not enabled:
        assert captured.err == ""
        return
    assert json.loads(captured.err) == {
        "event": "goal_plus_api_response_size",
        "transport": "mcp",
        "tool": tool,
        "format": "json",
        "bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "characters": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    }
    assert "private_text" not in captured.err


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("pretty", [False, True])
@pytest.mark.parametrize("payload", [{"private_text": "\u4e2d\u6587"}, "\u4e2d\u6587"])
def test_pi_cli_measures_serialized_response_without_changing_stdout(
    monkeypatch, capsys, enabled, pretty, payload,
):
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1" if enabled else "0")
    monkeypatch.setattr(pi_tool, "call_pi_tool", lambda *args: payload)
    args = ["goal_plus_status", "--args-json", "{}"]
    if pretty:
        args.append("--pretty")
    assert pi_tool.main(args) == 0
    captured = capsys.readouterr()
    encoded = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    assert captured.out == encoded + "\n"
    if not enabled:
        assert captured.err == ""
        return
    assert json.loads(captured.err) == {
        "event": "goal_plus_api_response_size",
        "transport": "pi-cli",
        "tool": "goal_plus_status",
        "format": "json",
        "bytes": len(encoded.encode("utf-8")),
        "characters": len(encoded),
    }
    assert "private_text" not in captured.err


def test_debug_stderr_failure_does_not_fail_completed_pi_tool(monkeypatch, capsys):
    class BrokenStderr:
        def write(self, text):
            raise OSError("closed diagnostic pipe")

    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    monkeypatch.setattr(pi_tool, "call_pi_tool", lambda *args: {"ok": True})
    with monkeypatch.context() as patch:
        patch.setattr("sys.stderr", BrokenStderr())
        assert pi_tool.main(["goal_plus_status", "--args-json", "{}"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
