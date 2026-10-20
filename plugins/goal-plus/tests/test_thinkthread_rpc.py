"""Candidate Message protocol behavior, independent of any pool workflow."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from goal_plus.execution.thinkthread_rpc import PROTOCOL, response_hash, response_payload
from goal_plus.infrastructure.thinkthread_worker_rpc import dispatch_worker_tool, receive_worker_messages
from goal_plus.pi_tool import SEARCH_TOOL_NAMES
from goal_plus.tools import CANDIDATE_TOOL_NAMES
from tests._thinkthread_sdk import StatefulAgentPosix


@pytest.fixture
def rpc_endpoint():
    sdk = StatefulAgentPosix()
    sdk.invoke("thinkthread.spawn", {"initialMessage": "protocol fixture"})
    record = {
        "run_id": "run_rpc", "candidate_id": "c001", "agent_session_id": "agent-1",
        "thinkthread_id": "tt-child-1", "registration_nonce": "registration",
        "dispatch_nonce": "current", "dispatch_nonces": ["prior", "current"],
        "dispatch_index": 2, "active_message_id": "msg-current",
    }
    sdk.enqueue("tt-child-1", {"protocol": PROTOCOL, "type": "registration",
                               "registration_nonce": "registration"})
    return sdk, record


def test_message_replay_chunking_retry_and_ack_use_one_dispatch(tmp_path, monkeypatch, rpc_endpoint):
    sdk, record = rpc_endpoint
    request = worker_request(deadline_unix_ms=int(time.time() * 1000) + 60_000)
    sdk.enqueue("tt-child-1", request)
    calls = []

    def dispatch(*args, **kwargs):
        calls.append(kwargs["request_id"])
        return {"payload": "x" * (70 * 1024)}

    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_worker_rpc.dispatch_worker_tool", dispatch)
    receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    assert calls == ["req-worker-1"]
    chunks = [json.loads(sent["text"]) for sent in sdk.sent]
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1, 2]
    assert {chunk["chunk_count"] for chunk in chunks} == {3}
    record = json.loads(json.dumps(record))
    sdk.enqueue("tt-child-1", request)
    receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    assert calls == ["req-worker-1"]
    record["pending_responses"]["req-worker-1"]["last_attempt_unix"] = 0
    before_retry = len(sdk.sent)
    receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    assert len(sdk.sent) == before_retry + 3
    sdk.enqueue("tt-child-1", {"protocol": PROTOCOL, "type": "response_ack",
        "request_id": "req-worker-1", "response_sha256": chunks[0]["response_sha256"]})
    receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    assert record["pending_responses"] == {}


@pytest.mark.parametrize("invalid", ["truncated", "hash", "session", "annotation"])
def test_invalid_message_never_reaches_candidate_tools(tmp_path, monkeypatch, rpc_endpoint, invalid):
    sdk, record = rpc_endpoint
    receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    request = worker_request(
        agent_session_id="peer" if invalid == "session" else "agent-1",
        tool="goal_plus_thinkthread_annotation_next" if invalid == "annotation" else "goal_plus_search_get_agent_context",
    )
    if invalid == "hash":
        request["content_sha256"] = "0" * 64
    sdk.enqueue("tt-child-1", request)
    if invalid == "truncated":
        sdk.messages["tt-child-1"][-1]["truncated"] = True
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_worker_rpc.dispatch_worker_tool",
                        lambda *args, **kwargs: pytest.fail("invalid request dispatched"))
    if invalid == "truncated":
        receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    else:
        with pytest.raises((ValueError, PermissionError)):
            receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    assert sdk.sent == []


def test_late_prior_dispatch_ack_preserves_current_invocation(tmp_path, rpc_endpoint):
    sdk, record = rpc_endpoint
    sdk.enqueue("tt-child-1", {"protocol": PROTOCOL, "type": "dispatch_ack",
        "dispatch_nonce": "prior", "message_id": "msg-prior"})
    receive_worker_messages(sdk, tmp_path, record, persist=lambda: None)
    assert record["active_message_id"] == "msg-current"
    assert record["acknowledged_messages"] == {"prior": "msg-prior"}


def test_worker_response_hash_is_stable_across_durable_json_replay(
    tmp_path: Path,
) -> None:
    response = {
        "ok": True,
        "result": {
            "state": "queued_at_turn_boundary",
            "request_id": "req-test",
            "message": "finish the turn",
        },
    }
    path = tmp_path / "response.json"
    path.write_text(json.dumps(response, sort_keys=True), encoding="utf-8")
    replayed = json.loads(path.read_text(encoding="utf-8"))

    assert response_hash(response) == response_hash(replayed)
    assert json.loads(response_payload("req-test", response)[0])[
        "response_sha256"
    ] == response_hash(response)

    response["result"]["payload"] = "x" * 100_000
    endpoint = Path(__file__).resolve().parents[1] / "assets/thinkthread-session-rpc.ts"
    checked = subprocess.run(
        ["node", "--input-type=module", "-e", '''
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
const { requestWorkerTool } = await import(process.argv[1]);
const chunks = JSON.parse(readFileSync(0, "utf8"));
let request, acknowledged = false;
const client = { async invoke(method, params) {
    if (method === "message.send") {
        const payload = JSON.parse(params.text);
        if (payload.type === "request") {
            request = payload;
            if (createHash("sha256").update(payload.content_json).digest("hex") !== payload.content_sha256)
                throw Error("request hash mismatch");
        } else if (payload.type === "response_ack") {
            if (payload.response_sha256 !== chunks[0].response_sha256) throw Error("ack hash mismatch");
            acknowledged = true;
        }
        return {};
    }
    return { messages: [...chunks.slice().reverse(), chunks[0]].map(chunk => ({
        senderThinkthreadId: "tt-parent", text: JSON.stringify({...chunk, request_id: request.request_id}),
    })), nextCursor: "done", hasMore: false };
}};
const result = await requestWorkerTool(client, "tt-parent", "agent-1", "goal_plus_search_get_agent_context", {}, new AbortController().signal);
if (!acknowledged) throw Error("response was not acknowledged");
process.stdout.write(JSON.stringify(result));
''', endpoint.as_uri()],
        input=json.dumps([json.loads(chunk) for chunk in response_payload("req-test", response)]),
        text=True, capture_output=True, check=True, timeout=10,
    )
    assert json.loads(checked.stdout) == response["result"]


def test_worker_proxy_tools_are_an_explicit_public_search_subset() -> None:
    assert CANDIDATE_TOOL_NAMES < SEARCH_TOOL_NAMES


def test_dispatch_message_lookup_accepts_candidate_and_annotation_envelopes():
    endpoint = Path(__file__).resolve().parents[1] / "assets/thinkthread-session-rpc.ts"
    checked = subprocess.run(
        ["node", "--input-type=module", "-e", '''
import assert from "node:assert/strict";
const { dispatchMessageId } = await import(process.argv[1]);
const nonce = "11111111-2222-3333-4444-555555555555";
let messages = [];
const client = { async invoke() {
    return {messages, nextCursor: "done", hasMore: false};
}};
const message = text => ({senderThinkthreadId: "parent", truncated: false,
    messageId: "dispatch", text});
for (const suffix of [".", "; queue_generation=1; queue_revision=2.", "\\n", ""]) {
    messages = [message(`agent_session_id=agent; dispatch_nonce=${nonce}${suffix}`)];
    assert.equal(await dispatchMessageId(client, "parent", nonce), "dispatch");
}
for (const text of [`dispatch_nonce=${nonce}extra.`, `old_dispatch_nonce=${nonce}.`]) {
    messages = [message(text)];
    await assert.rejects(dispatchMessageId(client, "parent", nonce), /no exact retained Message/);
}
messages = [message(`dispatch_nonce=${nonce}.`), message(`dispatch_nonce=${nonce};`)];
await assert.rejects(dispatchMessageId(client, "parent", nonce), /ambiguous/);
''', endpoint.as_uri()],
        text=True, capture_output=True, timeout=10,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        (
            "goal_plus_search_get_agent_context",
            {"extra": True},
            "extra",
        ),
        (
            "goal_plus_search_get_global_evidence",
            {"agent_session_id": "agent-1"},
            "agent_session_id",
        ),
        (
            "goal_plus_search_get_evidence_detail",
            {
                "candidate_id": "c002",
                "iteration": 0,
            },
            "iteration",
        ),
        (
            "goal_plus_search_list_iterations",
            {"agent_session_id": "agent-1"},
            "agent_session_id",
        ),
        (
            "goal_plus_search_run_verifier",
            {
                "hypothesis": "Try one change",
                "toolization_decision": {"outcome": "not_needed"},
            },
            "Input should be 'staged' or 'not_applicable'",
        ),
    ],
)
def test_worker_proxy_rejects_parameter_contract_drift(
    tmp_path: Path,
    tool: str,
    params: dict[str, object],
    message: str,
) -> None:
    job = {
        "agent_session_id": "agent-1",
        "run_id": "run_rpc",
        "candidate_id": "c001",
    }

    with pytest.raises((ValueError, PermissionError, RuntimeError), match=message):
        dispatch_worker_tool(
            tmp_path / ".gp",
            job,
            tool,
            params,
            request_id="req-contract",
        )


def worker_request(
    *,
    request_id: str = "req-worker-1",
    agent_session_id: str = "agent-1",
    tool: str = "goal_plus_search_list_iterations",
    params: dict[str, object] | None = None,
    deadline_unix_ms: int | None = None,
) -> dict[str, object]:
    normalized = params or {"agent_session_id": agent_session_id}
    content = {
        "agent_session_id": agent_session_id,
        "params": normalized,
        "tool": tool,
    }
    if deadline_unix_ms is not None:
        content["deadline_unix_ms"] = deadline_unix_ms
    content_json = json.dumps(content, separators=(",", ":"))
    request = {
        "protocol": PROTOCOL,
        "type": "request",
        "request_id": request_id,
        "agent_session_id": agent_session_id,
        "tool": tool,
        "params": normalized,
        "content_json": content_json,
        "content_sha256": hashlib.sha256(content_json.encode()).hexdigest(),
    }
    if deadline_unix_ms is not None:
        request["deadline_unix_ms"] = deadline_unix_ms
    return request
