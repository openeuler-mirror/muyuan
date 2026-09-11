"""Codex TT uses common candidate schemas without opening Root domain state."""

import asyncio
import json
from pathlib import Path
import subprocess
import pytest

from fastmcp import Client

from goal_plus.server import create_codex_mcp, create_mcp
from goal_plus.tools import CANDIDATE_TOOL_NAMES


def test_codex_tt_entry_requires_the_trusted_child_binding(monkeypatch):
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "candidate")
    calls = []
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.create_thinkthread_candidate_mcp",
                        lambda: calls.append("trusted_binding") or "child_mcp")
    assert create_codex_mcp() == "child_mcp"
    assert calls == ["trusted_binding"]
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "unknown")
    with pytest.raises(PermissionError, match="ThinkThread native role"):
        create_codex_mcp()


def test_codex_tt_main_checks_actual_root_before_opening_domain_state(tmp_path, monkeypatch):
    from tests._thinkthread_sdk import IntegratedAgentPosix

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    runtime = tmp_path / "session-runtime"
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "main")
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(runtime))
    sdk = IntegratedAgentPosix(workspace)
    view = sdk.self_view()
    view["profiles"][0]["agent"] = "codex"
    monkeypatch.setattr(sdk, "self_view", lambda: view)
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.AgentPosixSdkClient", lambda: sdk)
    calls = []
    monkeypatch.setattr("goal_plus.server.create_host_mcp",
                        lambda root, **kwargs: calls.append((root, kwargs)) or "root_mcp")
    assert create_codex_mcp() == "root_mcp"
    assert calls == [(runtime, {"agent_harness": "codex", "caller_role": "main"})]
    calls.clear()
    for field, value in [("parentThinkthreadId", "parent"), ("capabilities", [])]:
        previous = view[field]
        view[field] = value
        with pytest.raises(RuntimeError):
            create_codex_mcp()
        view[field] = previous
    view["profiles"][0]["agent"] = "pi"
    with pytest.raises(RuntimeError, match="select the codex"):
        create_codex_mcp()
    assert calls == []
    assert not runtime.exists()


def test_codex_tt_main_rejects_conflicting_roles_and_runtime_paths(tmp_path, monkeypatch):
    from goal_plus.codex_hook_input import CODEX_CANDIDATE_SESSION_ENV

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "main")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid entry accessed the SDK or domain state")
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.AgentPosixSdkClient", forbidden)
    monkeypatch.setattr("goal_plus.server.create_host_mcp", forbidden)
    monkeypatch.delenv("GOAL_PLUS_ROOT", raising=False)
    with pytest.raises(PermissionError, match="absolute runtime root"):
        create_codex_mcp()
    for root in [".gp", str(workspace / ".gp")]:
        monkeypatch.setenv("GOAL_PLUS_ROOT", root)
        with pytest.raises(PermissionError):
            create_codex_mcp()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(alias / ".gp"))
    with pytest.raises(PermissionError, match="outside"):
        create_codex_mcp()
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(tmp_path / "runtime"))
    with pytest.raises(PermissionError, match="differs"):
        create_codex_mcp(tmp_path / "another-runtime")
    monkeypatch.setenv(CODEX_CANDIDATE_SESSION_ENV, "agent-1")
    with pytest.raises(PermissionError, match="conflicting"):
        create_codex_mcp()


def test_remote_candidate_mcp_uses_shared_schema_and_rejects_peer_before_transport(tmp_path, monkeypatch):
    local = create_mcp(tmp_path / "local", caller_role="candidate", caller_agent_session_id="agent-1")
    def forbidden(*args, **kwargs):
        raise AssertionError("Child bridge opened domain state")
    monkeypatch.setattr("goal_plus.server.FileSearchRuntime", forbidden)
    monkeypatch.setattr("goal_plus.server.FileGoalPlusRuntime", forbidden)
    calls = []
    def call(name, arguments):
        calls.append((name, arguments))
        return {"source": "parent", "session": arguments["agent_session_id"]}
    remote = create_mcp(tmp_path / "absent", caller_role="candidate", caller_agent_session_id="agent-1",
                        candidate_tool_call=call)

    async def scenario():
        async with Client(local) as first, Client(remote) as second:
            local_tools, remote_tools = await first.list_tools(), await second.list_tools()
            assert {tool.name: tool.inputSchema for tool in remote_tools} == {
                tool.name: tool.inputSchema for tool in local_tools if tool.name != "goal_plus_search_wait_verifier"}
            assert {tool.name for tool in remote_tools} == CANDIDATE_TOOL_NAMES - {"goal_plus_search_wait_verifier"}
            result = await second.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "agent-1"})
            assert json.loads(result.content[0].text)["source"] == "parent"
            denied = await second.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "peer"}, raise_on_error=False)
            assert denied.is_error
            invalid = await second.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "agent-1", "extra": True}, raise_on_error=False)
            assert invalid.is_error
            verified = await second.call_tool("goal_plus_search_run_verifier", {
                "agent_session_id": "agent-1", "run_id": "run_1", "candidate_id": "c001",
                "toolization_decision": {"outcome": "not_applicable", "rationale": "No shared tool change.",
                                         "exclusion": "no_toolizable_material"},
            })
            assert verified.structured_content == {"source": "parent", "session": "agent-1"}
    asyncio.run(scenario())
    assert calls[0] == ("goal_plus_search_get_agent_context", {"agent_session_id": "agent-1"})
    assert len(calls) == 2
    assert calls[1][0] == "goal_plus_search_run_verifier"
    assert calls[1][1]["scope"] == "process"
    assert calls[1][1]["toolization_decision"]["outcome"] == "not_applicable"
    assert not (tmp_path / "absent").exists()


def test_codex_worker_binding_uses_only_complete_parent_messages():
    module = Path(__file__).resolve().parents[1] / "assets/codex/thinkthread-worker-bridge.mts"
    checked = subprocess.run(["node", "--input-type=module", "-e", '''
import assert from "node:assert/strict";
const {workerBinding, workerOperation} = await import(process.argv[1]);
const nonce = "11111111-2222-3333-4444-555555555555";
const dispatch = `agent_session_id=agent-1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.`;
let grant = [{id: "thinkthread.message"}], parent = "parent", messages = [], sent = [];
const client = {async invoke(method, params) {
    if (method === "self") return {thinkthreadId: "child", parentThinkthreadId: parent, capabilities: grant};
    if (method === "message.receive") return {messages, nextCursor: "done", hasMore: false};
    sent.push(params); throw Error("unexpected send");
}};
const message = (text, patch = {}) => ({text, senderThinkthreadId: "parent", recipientThinkthreadId: "child",
    messageId: "dispatch", truncated: false, ...patch});
messages = [message(dispatch)];
assert.equal((await workerBinding(client, "parent")).agentSessionId, "agent-1");
assert.deepEqual(sent, []); // Discovery is not native dispatch acknowledgement.
messages.push(message(JSON.stringify({type: "response", context: dispatch.replace("agent-1", "peer")})));
assert.equal((await workerBinding(client, "parent")).agentSessionId, "agent-1");
for (const patch of [{senderThinkthreadId: "peer"}, {recipientThinkthreadId: "peer"}, {truncated: true}]) {
    messages = [message(dispatch, patch)];
    await assert.rejects(workerBinding(client, "parent"), /no retained/);
}
messages = [message(dispatch)];
await assert.rejects(workerOperation(client, "parent", {operation: "call", params: {agent_session_id: "peer"}}), /binding/);
assert.deepEqual(sent, []);
grant.push({id: "thinkthread.fs.self"});
await assert.rejects(workerBinding(client, "parent"), /Message-only/);
grant = [{id: "thinkthread.message"}]; parent = null;
await assert.rejects(workerBinding(client, "parent"), /Message-only/);
''', module.as_uri()], text=True, capture_output=True, timeout=10)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_codex_mcp_forwards_over_the_actual_node_message_bridge(tmp_path, monkeypatch):
    from goal_plus.infrastructure.thinkthread_codex_tools import create_thinkthread_candidate_mcp

    sdk = tmp_path / "sdk.mjs"
    sdk.write_text('''
import {createHash} from "node:crypto";
const nonce = "11111111-2222-3333-4444-555555555555";
export class AgentPosixClient {
    static fromEnv() {return new AgentPosixClient();}
    async invoke(method, params) {
        if (method === "self") return {thinkthreadId: "child", parentThinkthreadId: "parent",
            capabilities: [{id: "thinkthread.message"}]};
        if (method === "message.send") {
            const payload = JSON.parse(params.text);
            if (payload.type === "request") this.request = payload;
            return {messageId: "sent"};
        }
        if (method !== "message.receive") throw Error(method);
        if (!this.request) return {messages: [{senderThinkthreadId: "parent", recipientThinkthreadId: "child",
            messageId: "dispatch", truncated: false,
            text: `agent_session_id=agent-1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.`}],
            nextCursor: "dispatch", hasMore: false};
        const content = {ok: true, result: {tool: this.request.tool, params: this.request.params}};
        const canonical = value => Array.isArray(value) ? value.map(canonical)
            : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
        const response_json = JSON.stringify(canonical(content));
        return {messages: [{senderThinkthreadId: "parent", truncated: false,
            text: JSON.stringify({protocol: "goal-plus.thinkthread.v3", type: "response_chunk",
                request_id: this.request.request_id, chunk_index: 0, chunk_count: 1,
                data_base64: Buffer.from(response_json).toString("base64"),
                chunk_sha256: createHash("sha256").update(response_json).digest("hex"),
                response_sha256: createHash("sha256").update(response_json).digest("hex")})}],
            nextCursor: "response", hasMore: false};
    }
}
''')
    monkeypatch.setenv("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", str(sdk))
    monkeypatch.setenv("THINKTHREAD_PARENT_ID", "parent")
    server = create_thinkthread_candidate_mcp()

    async def scenario():
        async with Client(server) as client:
            absent = await client.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "agent-1"}, raise_on_error=False)
            assert absent.is_error
            for _ in range(2):
                result = await client.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "agent-1"}, meta={"threadId": "native"})
                assert json.loads(result.content[0].text) == {
                    "tool": "goal_plus_search_get_agent_context", "params": {"agent_session_id": "agent-1"}}
            changed = await client.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "agent-1"},
                                             meta={"threadId": "peer"}, raise_on_error=False)
            assert changed.is_error
    asyncio.run(scenario())
