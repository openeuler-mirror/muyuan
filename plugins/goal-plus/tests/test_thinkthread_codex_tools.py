"""Codex TT uses common candidate schemas without opening Root domain state."""

import asyncio
import base64
import json
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

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


def test_codex_tt_entry_dispatches_the_annotator_role(monkeypatch):
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "annotator")
    calls = []
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.create_thinkthread_annotator_mcp",
                        lambda: calls.append("annotator_binding") or "annotator_mcp")
    assert create_codex_mcp() == "annotator_mcp"
    assert calls == ["annotator_binding"]


def test_codex_candidate_mcp_routes_children_by_dispatch_role(monkeypatch):
    # thinkthread.spawn carries no per-child env, so Children sharing the TT profile
    # environment are told apart only by their retained dispatch message.
    from goal_plus.domain.tool_catalog import CandidateToolCapabilities
    from goal_plus.infrastructure import thinkthread_codex_tools as tools

    calls = []
    monkeypatch.setattr(tools, "_worker_bridge_transport", lambda: object())
    monkeypatch.setattr(tools, "_annotator_mcp", lambda transport, session: calls.append(("annotator", session)))
    monkeypatch.setattr(tools, "_verifier_mcp", lambda transport, session: calls.append(("verifier", session)))
    monkeypatch.setattr(tools, "_child_mcp", lambda transport, session, capabilities: calls.append(("candidate", session)))
    capabilities = CandidateToolCapabilities(supplemental=False, shared_tools=False, shared_cache=False, references=False, spec_revision=False)
    for role in (None, "candidate"):
        monkeypatch.setattr(tools, "_child_binding", lambda transport, role=role: ("agent-1", role, capabilities))
        tools.create_thinkthread_candidate_mcp()
        assert calls.pop() == ("candidate", "agent-1")
    monkeypatch.setattr(tools, "_child_binding", lambda transport: ("annotator-run_1", "annotator", None))
    tools.create_thinkthread_candidate_mcp()
    assert calls.pop() == ("annotator", "annotator-run_1")
    monkeypatch.setattr(tools, "_child_binding", lambda transport: ("verifier-task", "verifier", None))
    tools.create_thinkthread_candidate_mcp()
    assert calls.pop() == ("verifier", "verifier-task")
    monkeypatch.setattr(tools, "_child_binding", lambda transport: ("agent-1", "checker", None))
    with pytest.raises(PermissionError, match="unknown dispatch role"):
        tools.create_thinkthread_candidate_mcp()


@pytest.mark.parametrize("capabilities", [
    None,
    {"supplemental": False, "shared_tools": False, "shared_cache": False},
    {"supplemental": "false", "shared_tools": False, "shared_cache": False, "references": False, "spec_revision": False},
    {"supplemental": False, "shared_tools": False, "shared_cache": False, "references": False, "spec_revision": False, "extra": True},
])
def test_codex_candidate_binding_requires_complete_typed_capabilities(monkeypatch, capabilities):
    from goal_plus.infrastructure import thinkthread_codex_tools as tools

    class Transport:
        def invoke(self, operation):
            assert operation == "binding"
            return {"agentSessionId": "agent-1", "thinkthreadRole": "candidate",
                    "candidateCapabilities": capabilities}

    monkeypatch.setattr(tools, "_worker_bridge_transport", Transport)
    with pytest.raises(PermissionError, match="capabilities"):
        tools.create_thinkthread_candidate_mcp()


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
    root_mcp = FastMCP("goal-plus", instructions="base")
    monkeypatch.setattr("goal_plus.server.create_host_mcp",
                        lambda root, **kwargs: calls.append((root, kwargs)) or root_mcp)
    assert create_codex_mcp() is root_mcp
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


def test_codex_tt_main_projects_thinkthread_execution_identity(tmp_path, monkeypatch):
    from goal_plus.infrastructure.thinkthread_codex_tools import (
        THINKTHREAD_MAIN_INSTRUCTIONS,
        create_thinkthread_main_mcp,
    )
    from tests._thinkthread_sdk import IntegratedAgentPosix

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(tmp_path / "session-runtime"))
    sdk = IntegratedAgentPosix(workspace)
    view = sdk.self_view()
    view["profiles"][0]["agent"] = "codex"
    monkeypatch.setattr(sdk, "self_view", lambda: view)
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.AgentPosixSdkClient", lambda: sdk)

    server = create_thinkthread_main_mcp()

    base = (Path(__file__).resolve().parents[1] / "assets/goal-plus-instructions.txt").read_text(encoding="utf-8").strip()
    assert server.instructions == base + "\n" + THINKTHREAD_MAIN_INSTRUCTIONS
    for projection in (
        'agent_harness="codex"',
        'runtime_provider="thinkthread"',
        'workspace.provider="thinkthread"',
    ):
        assert projection in server.instructions
    # Direct mains receive only the shared asset, never the ThinkThread projection.
    assert THINKTHREAD_MAIN_INSTRUCTIONS not in base
    # The projection stays within the 500 character / 1000 byte budget.
    assert len(THINKTHREAD_MAIN_INSTRUCTIONS) <= 500
    assert len(THINKTHREAD_MAIN_INSTRUCTIONS.encode("utf-8")) <= 1000


def test_codex_tt_main_host_start_and_resume_preserve_owning_goal(tmp_path, monkeypatch):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.infrastructure.thinkthread_codex_tools import (
        create_thinkthread_main_mcp,
    )
    from tests._thinkthread_sdk import IntegratedAgentPosix

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    runtime_root = tmp_path / "session-runtime"
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(runtime_root))
    sdk = IntegratedAgentPosix(workspace)
    view = sdk.self_view()
    view["profiles"][0]["agent"] = "codex"
    monkeypatch.setattr(sdk, "self_view", lambda: view)
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.AgentPosixSdkClient", lambda: sdk)
    preflight_calls = []
    monkeypatch.setattr(
        "goal_plus.thinkthread_connectivity.preflight_thinkthread_codex",
        lambda: preflight_calls.append("preflight") or {
            "model": "gpt-5.6-luna",
            "provider": "direct",
            "endpoint": "http://127.0.0.1:18081/v1",
        },
    )

    server = create_thinkthread_main_mcp()

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert "goal_plus_host_start" in tools
            started = await client.call_tool(
                "goal_plus_host_start", {"raw_goal": "max_duration_seconds=300 Optimize the toy project"},
                meta={"threadId": "tt-main-session"},
            )
            assert preflight_calls == ["preflight"]
            payload = json.loads(started.content[0].text)
            assert payload["status"] == "active"
            assert payload["entrypoint_status"] == "host_verified"
            replay = await client.call_tool(
                "goal_plus_host_start", {"raw_goal": "max_duration_seconds=300 Optimize the toy project"},
                meta={"threadId": "tt-main-session"},
            )
            assert json.loads(replay.content[0].text)["goal_plus_id"] == payload["goal_plus_id"]
            assert [item.name for item in (runtime_root / "goal-plus").iterdir()] == [payload["goal_plus_id"]]
            conflicting = await client.call_tool(
                "goal_plus_host_start", {"raw_goal": "A different goal"},
                meta={"threadId": "tt-main-session"}, raise_on_error=False,
            )
            assert conflicting.is_error
            assert "nonterminal" in conflicting.content[0].text
            anonymous = await client.call_tool(
                "goal_plus_host_start", {"raw_goal": "No thread identity"}, raise_on_error=False,
            )
            assert anonymous.is_error
            runtime = FileGoalPlusRuntime(runtime_root)
            before = runtime.set_status(payload["goal_plus_id"], "blocked", reason="Finish the preparation turn")
            assert before.outer_deadline_at is not None
            denied = await client.call_tool(
                "goal_plus_host_resume", {"goal_plus_id": payload["goal_plus_id"]},
                meta={"threadId": "another-main"}, raise_on_error=False,
            )
            assert denied.is_error
            assert "MainSessionMismatch" in denied.content[0].text
            assert runtime.status(payload["goal_plus_id"]) == before
            resumed = await client.call_tool(
                "goal_plus_host_resume", {"goal_plus_id": payload["goal_plus_id"]},
                meta={"threadId": "tt-main-session"},
            )
            assert json.loads(resumed.content[0].text)["status"] == "active"
            after = runtime.status(payload["goal_plus_id"])
            assert after.goal_revision == before.goal_revision
            assert after.outer_deadline_at == before.outer_deadline_at
            assert after.command_config == before.command_config
            assert after.created_at == before.created_at
            replay_resume = await client.call_tool(
                "goal_plus_host_resume", {"goal_plus_id": payload["goal_plus_id"]},
                meta={"threadId": "tt-main-session"},
            )
            assert not replay_resume.is_error
            assert runtime.status(payload["goal_plus_id"]) == after
            return payload["goal_plus_id"]

    goal_plus_id = asyncio.run(scenario())
    record = FileGoalPlusRuntime(runtime_root).status(goal_plus_id)
    invocation = record.host_command_invocations[0]
    assert invocation.agent_harness == "codex"
    assert invocation.transport == "codex_user_prompt_submit"
    assert invocation.command == "$goal-plus"
    assert invocation.action == "start"
    assert invocation.session_id == "tt-main-session"
    assert invocation.invocation_id.startswith("codex-tt:")
    assert record.active_session.session_id == "tt-main-session"
    assert record.active_session.metadata["authorized_by"] == "thinkthread_main_mcp"
    assert record.active_session.metadata["host_invocation_id"] == invocation.invocation_id


def test_codex_tt_main_close_scopes_closes_terminal_goal_scopes(tmp_path, monkeypatch):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.host_scope import ensure_execution_scope, execution_scope_for_run
    from goal_plus.infrastructure.thinkthread_codex_tools import (
        create_thinkthread_main_mcp,
    )
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal
    from tests._thinkthread_sdk import IntegratedAgentPosix

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    runtime_root = tmp_path / "session-runtime"
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(runtime_root))
    sdk = IntegratedAgentPosix(workspace)
    view = sdk.self_view()
    view["profiles"][0]["agent"] = "codex"
    monkeypatch.setattr(sdk, "self_view", lambda: view)
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.AgentPosixSdkClient", lambda: sdk)

    project = make_project(tmp_path)
    goal = start_host_verified_goal(runtime_root, project, session_id="tt-main-session")
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goal_runtime = FileGoalPlusRuntime(runtime_root)
    goal_runtime.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    scope = ensure_execution_scope(
        root_dir=runtime_root, agent_harness="codex",
        owner_session_id="tt-main-session", run_id=run_id,
    )
    assert scope["state"] == "open"
    server = create_thinkthread_main_mcp()

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert "goal_plus_host_close_scopes" in tools
            premature = await client.call_tool(
                "goal_plus_host_close_scopes", {"goal_plus_id": goal.goal_plus_id},
                meta={"threadId": "tt-main-session"}, raise_on_error=False,
            )
            assert premature.is_error and "terminal" in premature.content[0].text
            stranger = await client.call_tool(
                "goal_plus_host_close_scopes", {"goal_plus_id": goal.goal_plus_id},
                meta={"threadId": "tt-other-session"}, raise_on_error=False,
            )
            assert stranger.is_error and "active session" in stranger.content[0].text
            goal_runtime.set_status(goal.goal_plus_id, "abandoned", reason="test complete")
            closed = await client.call_tool(
                "goal_plus_host_close_scopes", {"goal_plus_id": goal.goal_plus_id},
                meta={"threadId": "tt-main-session"},
            )
            payload = json.loads(closed.content[0].text)
            assert payload["status"] == "abandoned"
            assert [item["state"] for item in payload["scopes"]] == ["closed"]
            assert payload["scopes"][0]["run_id"] == run_id
            replay = await client.call_tool(
                "goal_plus_host_close_scopes", {"goal_plus_id": goal.goal_plus_id},
                meta={"threadId": "tt-main-session"},
            )
            assert json.loads(replay.content[0].text)["scopes"][0]["state"] == "closed"

    asyncio.run(scenario())
    assert execution_scope_for_run(root_dir=runtime_root, run_id=run_id)["state"] == "closed"


def test_remote_candidate_mcp_uses_shared_schema_and_rejects_peer_before_transport(tmp_path, monkeypatch):
    from goal_plus.domain.tool_catalog import CandidateToolCapabilities

    local = create_mcp(tmp_path / "local", caller_role="candidate", caller_agent_session_id="agent-1")
    def forbidden(*args, **kwargs):
        raise AssertionError("Child bridge opened domain state")
    monkeypatch.setattr("goal_plus.server.FileSearchRuntime", forbidden)
    monkeypatch.setattr("goal_plus.server.FileGoalPlusRuntime", forbidden)
    calls = []
    def call(name, arguments):
        calls.append((name, arguments))
        return {"source": "parent"}
    remote = create_mcp(tmp_path / "absent", caller_role="candidate", caller_agent_session_id="agent-1",
                        candidate_tool_call=call,
                        candidate_capabilities=CandidateToolCapabilities(
                            supplemental=True, shared_tools=True, shared_cache=True, references=True, spec_revision=True,
                        ))

    async def scenario():
        async with Client(local) as first, Client(remote) as second:
            local_tools, remote_tools = await first.list_tools(), await second.list_tools()
            assert {tool.name: tool.input_schema for tool in remote_tools} == {
                tool.name: tool.input_schema for tool in local_tools}
            assert {tool.name for tool in remote_tools} == CANDIDATE_TOOL_NAMES
            result = await second.call_tool("goal_plus_search_get_agent_context", {})
            assert json.loads(result.content[0].text)["source"] == "parent"
            denied = await second.call_tool("goal_plus_search_get_agent_context", {"agent_session_id": "peer"}, raise_on_error=False)
            assert denied.is_error
            invalid = await second.call_tool("goal_plus_search_get_agent_context", {"extra": True}, raise_on_error=False)
            assert invalid.is_error
            verified = await second.call_tool("goal_plus_search_run_verifier", {
                "toolization_decision": {"outcome": "not_applicable", "rationale": "No shared tool change.",
                                         "exclusion": "no_toolizable_material"},
            })
            assert verified.structured_content is None
            assert len(verified.content) == 1
            assert json.loads(verified.content[0].text) == {"source": "parent"}
    asyncio.run(scenario())
    assert calls[0] == ("goal_plus_search_get_agent_context", {})
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


@pytest.fixture
def codex_candidate_server(tmp_path, monkeypatch):
    from goal_plus.infrastructure.thinkthread_codex_tools import (
        create_thinkthread_candidate_mcp,
    )

    sdk = tmp_path / "sdk.mjs"
    source = '''
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
            text: `agent_session_id=agent-1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.
goal_plus_candidate_capabilities=CAPABILITIES;`}],
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
'''
    monkeypatch.setenv("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", str(sdk))
    monkeypatch.setenv("THINKTHREAD_PARENT_ID", "parent")

    def create(capabilities):
        encoded = base64.urlsafe_b64encode(json.dumps(capabilities).encode()).decode().rstrip("=")
        sdk.write_text(source.replace("CAPABILITIES", encoded))
        return create_thinkthread_candidate_mcp()

    return create


@pytest.mark.parametrize("capabilities,optional_tools", [
    ({"supplemental": False, "shared_tools": False, "shared_cache": False, "references": False, "spec_revision": False}, set()),
    ({"supplemental": True, "shared_tools": False, "shared_cache": False, "references": False, "spec_revision": False},
     {"goal_plus_search_get_evidence_detail"}),
    ({"supplemental": False, "shared_tools": False, "shared_cache": True, "references": False, "spec_revision": False},
     {"goal_plus_search_read_shared_cache", "goal_plus_search_append_shared_cache"}),
])
def test_codex_child_catalog_projects_parent_frozen_capabilities(codex_candidate_server, capabilities, optional_tools):
    server = codex_candidate_server(capabilities)

    async def scenario():
        async with Client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {
                "goal_plus_search_get_agent_context", "goal_plus_search_get_global_evidence",
                "goal_plus_search_run_verifier",
                "goal_plus_search_list_iterations", *optional_tools,
            }
            denied = await client.call_tool("goal_plus_search_reference_open", {
                "source_candidate_id": "c001", "source_iteration": 1,
            }, raise_on_error=False)
            assert denied.is_error

    asyncio.run(scenario())


def test_codex_mcp_forwards_over_the_actual_node_message_bridge(codex_candidate_server):
    server = codex_candidate_server({"supplemental": False, "shared_tools": False, "shared_cache": False, "references": False, "spec_revision": False})

    async def scenario():
        async with Client(server) as client:
            absent = await client.call_tool("goal_plus_search_get_agent_context", {}, raise_on_error=False)
            assert absent.is_error
            for _ in range(2):
                result = await client.call_tool("goal_plus_search_get_agent_context", {}, meta={"threadId": "native"})
                assert json.loads(result.content[0].text) == {
                    "tool": "goal_plus_search_get_agent_context", "params": {}}
            changed = await client.call_tool("goal_plus_search_get_agent_context", {},
                                             meta={"threadId": "peer"}, raise_on_error=False)
            assert changed.is_error
    asyncio.run(scenario())


def test_codex_worker_binding_gates_annotation_tools_by_role():
    module = Path(__file__).resolve().parents[1] / "assets/codex/thinkthread-worker-bridge.mts"
    checked = subprocess.run(["node", "--input-type=module", "-e", '''
import assert from "node:assert/strict";
const {workerBinding, workerOperation} = await import(process.argv[1]);
const nonce = "11111111-2222-3333-4444-555555555555";
const make = (dispatch) => {
    const sent = [];
    const client = {async invoke(method, params) {
        if (method === "self") return {thinkthreadId: "child", parentThinkthreadId: "parent",
            capabilities: [{id: "thinkthread.message"}]};
        if (method === "message.receive") return {messages: [{text: dispatch, senderThinkthreadId: "parent",
            recipientThinkthreadId: "child", messageId: "dispatch", truncated: false}],
            nextCursor: "done", hasMore: false};
        sent.push(params); return {messageId: "sent"};
    }};
    return {client, sent};
};
const annotator = make(`goal_plus_thinkthread_role=annotator; agent_session_id=annotator-run_1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.`);
const binding = await workerBinding(annotator.client, "parent");
assert.equal(binding.agentSessionId, "annotator-run_1");
assert.equal(binding.thinkthreadRole, "annotator");
await assert.rejects(workerOperation(annotator.client, "parent", {operation: "call",
    params: {agent_session_id: "annotator-run_1", name: "goal_plus_search_run_verifier",
        arguments: {agent_session_id: "annotator-run_1"}}}), /Annotation Child tool is not allowed/);
await assert.rejects(workerOperation(annotator.client, "parent", {operation: "call",
    params: {agent_session_id: "annotator-run_1", name: "goal_plus_thinkthread_annotation_next",
        arguments: {agent_session_id: "annotator-run_1"}}}), /must not select a session/);
assert.deepEqual(annotator.sent, []);
const candidate = make(`agent_session_id=agent-1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.`);
await assert.rejects(workerOperation(candidate.client, "parent", {operation: "call",
    params: {agent_session_id: "agent-1", name: "goal_plus_thinkthread_annotation_next", arguments: {}}}),
    /Candidate Child annotation tool is not allowed/);
assert.deepEqual(candidate.sent, []);
const verifier = make(`goal_plus_thinkthread_role=verifier; agent_session_id=verifier-1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.`);
for (const name of ["goal_plus_search_run_verifier", "goal_plus_thinkthread_annotation_next", "bash"]) {
    await assert.rejects(workerOperation(verifier.client, "parent", {operation: "call",
        params: {agent_session_id: "verifier-1", name, arguments: {}}}), /Verifier Child tool is not allowed/);
}
await assert.rejects(workerOperation(candidate.client, "parent", {operation: "call",
    params: {agent_session_id: "agent-1", name: "goal_plus_thinkthread_verifier_submit", arguments: {}}}),
    /Verifier Child tool is not allowed/);
assert.deepEqual(verifier.sent, []);
''', module.as_uri()], text=True, capture_output=True, timeout=10)
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("view_result", [
    [],
    [{"kind": "modified", "path": {"utf8": "initial_program.py"}}],
    None,
    "unexpected",
    [7],
    {},
])
def test_codex_annotator_mcp_exposes_only_annotation_tools_and_forwards(tmp_path, monkeypatch, view_result):
    from goal_plus.infrastructure.thinkthread_codex_tools import (
        create_thinkthread_annotator_mcp,
    )

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
            text: `goal_plus_thinkthread_role=annotator; agent_session_id=annotator-run_1; registration_nonce=${nonce}; dispatch_nonce=${nonce}.`}],
            nextCursor: "dispatch", hasMore: false};
        const viewTools = ["goal_plus_thinkthread_artifact_view_list", "goal_plus_thinkthread_artifact_view_diff"];
        const result = viewTools.includes(this.request.tool) ? VIEW_RESULT
            : {tool: this.request.tool, params: this.request.params};
        const content = {ok: true, result};
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
'''.replace("VIEW_RESULT", json.dumps(view_result)))
    monkeypatch.setenv("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", str(sdk))
    monkeypatch.setenv("THINKTHREAD_PARENT_ID", "parent")
    server = create_thinkthread_annotator_mcp()

    async def scenario():
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {
                "goal_plus_thinkthread_annotation_next",
                "goal_plus_thinkthread_artifact_view_list",
                "goal_plus_thinkthread_artifact_view_stat",
                "goal_plus_thinkthread_artifact_view_pread",
                "goal_plus_thinkthread_artifact_view_diff",
                "goal_plus_thinkthread_annotation_submit",
            }
            assert all(tool.output_schema is None for tool in tools)
            absent = await client.call_tool("goal_plus_thinkthread_annotation_next", {}, raise_on_error=False)
            assert absent.is_error
            claimed = await client.call_tool("goal_plus_thinkthread_annotation_next", {}, meta={"threadId": "native"})
            assert json.loads(claimed.content[0].text) == {
                "tool": "goal_plus_thinkthread_annotation_next", "params": {}}
            viewed = await client.call_tool("goal_plus_thinkthread_artifact_view_pread",
                                            {"view_id": "view-1", "path": "initial_program.py"}, meta={"threadId": "native"})
            assert json.loads(viewed.content[0].text) == {
                "tool": "goal_plus_thinkthread_artifact_view_pread",
                "params": {"view_id": "view-1", "path": "initial_program.py", "offset": 0, "length": 65536}}
            valid_view_result = isinstance(view_result, list) and all(isinstance(item, dict) for item in view_result)
            for name, arguments in [
                ("goal_plus_thinkthread_artifact_view_list", {"view_id": "view-1"}),
                ("goal_plus_thinkthread_artifact_view_diff", {"base_view_id": "view-1", "target_view_id": "view-2"}),
            ]:
                result = await client.call_tool(name, arguments, meta={"threadId": "native"}, raise_on_error=False)
                if valid_view_result:
                    assert not result.is_error, result.content
                    assert result.structured_content is None
                    assert len(result.content) == 1
                    assert json.loads(result.content[0].text) == view_result
                else:
                    assert result.is_error
                    assert "invalid Artifact View entries" in result.content[0].text
            submitted = await client.call_tool("goal_plus_thinkthread_annotation_submit",
                                               {"description": "candidate changed the value"}, meta={"threadId": "native"})
            assert json.loads(submitted.content[0].text) == {
                "tool": "goal_plus_thinkthread_annotation_submit",
                "params": {"description": "candidate changed the value",
                           "supplemental_evaluation": None, "tool_views": []}}
    asyncio.run(scenario())
