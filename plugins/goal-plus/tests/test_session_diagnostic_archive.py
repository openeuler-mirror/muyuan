"""Incremental native projections survive TT cleanup without carrying private context."""

import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from goal_plus.infrastructure.session_diagnostics import archive_diagnostic_message, collect_child_diagnostics

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["success", "wrong_parent", "hang"])
def test_diagnostic_sender_is_bound_and_bounded(tmp_path, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Pi diagnostics")
    sdk = tmp_path / "sdk.mjs"
    sdk.write_text("""
import {writeFileSync, appendFileSync} from 'node:fs';
writeFileSync(process.env.DIAGNOSTIC_TEST_PID, String(process.pid));
export const AgentPosixClient = {fromEnv: () => ({invoke: async (method, params) => {
  if (process.env.DIAGNOSTIC_TEST_MODE === 'hang') await new Promise(() => {setInterval(() => {}, 1000);});
  if (method === 'self') return {parentThinkthreadId: process.env.DIAGNOSTIC_TEST_MODE === 'wrong_parent' ? 'peer' : 'parent'};
  appendFileSync(process.env.DIAGNOSTIC_TEST_OUTPUT, JSON.stringify({method, params}) + '\\n');
  return {messageId: 'sent'};
}})};
""")
    module = ROOT / "assets/pi/extensions/lib/goal-plus-session-archive.ts"
    script = """
import {pathToFileURL} from 'node:url';
const {sendDiagnosticMessages} = await import(pathToFileURL(process.argv[1]).href);
try { await sendDiagnosticMessages('parent', [{type: 'session_diagnostics'}]); console.log('sent'); }
catch { console.log('unavailable'); }
"""
    result = subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script, str(module)],
                            capture_output=True, text=True, timeout=12,
                            env={**os.environ, "GOAL_PLUS_AGENT_POSIX_SDK_ENTRY": str(sdk),
                                 "THINKTHREAD_PARENT_ID": "parent", "DIAGNOSTIC_TEST_MODE": mode,
                                 "DIAGNOSTIC_TEST_PID": str(tmp_path / "pid"),
                                 "DIAGNOSTIC_TEST_OUTPUT": str(tmp_path / "output")})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("sent" if mode == "success" else "unavailable")
    assert (tmp_path / "output").exists() == (mode == "success")
    if mode == "success":
        sent = json.loads((tmp_path / "output").read_text())
        assert sent["params"]["recipientThinkthreadId"] == "parent"
        assert sent["params"]["wake"] is False
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


@pytest.fixture
def archive_messages(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Pi diagnostics")
    result = subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT,
                             str(ROOT / "assets/pi/extensions/lib/goal-plus-session-archive.ts"), str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _analyze(directory):
    spec = importlib.util.spec_from_file_location("session_audit", ROOT / "scripts/check_session_instructions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit_session(directory, agent_harness="pi", overview="Overview", tools=True, include_content=True)


def test_three_native_deltas_archive_and_replay(tmp_path, archive_messages):
    binding = {"thinkthread_id": "tt-child", "root_thinkthread_id": "tt-root",
               "agent_session_id": "agent-owned", "run_id": "run-owned", "candidate_id": "c001"}
    for number, payload in enumerate(archive_messages):
        message = {"senderThinkthreadId": "tt-child", "messageId": str(number),
                   "text": json.dumps(payload), "truncated": False}
        assert archive_diagnostic_message(tmp_path, binding, message)
        assert archive_diagnostic_message(tmp_path, binding, message)
    directory = tmp_path / "host-logs/session-diagnostics/tt-child"
    index = json.loads((directory / "index.json").read_text())
    assert len(index["batches"]) == 3
    analysis = _analyze(directory)
    assert analysis["tool_analysis"]["status"] == "observed"
    assert analysis["tool_analysis"]["summary"]["calls"] == 3
    assert analysis["tool_analysis"]["summary"]["requests"] == 3
    assert not analysis["errors"]
    assert len(analysis["tool_analysis"]["contracts"]) == 1
    assert "PRIVATE" not in json.dumps(analysis)
    batches = sorted(index["batches"].items(), key=lambda item: item[1]["sequence"])
    assert batches[1][1]["decoded_bytes"] < batches[0][1]["decoded_bytes"]
    del index["batches"][batches[1][0]]
    (directory / "index.json").write_text(json.dumps(index))
    broken = _analyze(directory)
    assert broken["tool_analysis"]["status"] == "unavailable"
    assert {"reason": "missing_archive_batch"} in broken["errors"]


def test_archive_disabled_wrong_sender_and_corruption(tmp_path, archive_messages, monkeypatch):
    binding = {"thinkthread_id": "tt-child", "agent_session_id": "agent-owned"}
    message = {"senderThinkthreadId": "tt-peer", "text": json.dumps(archive_messages[0])}
    assert archive_diagnostic_message(tmp_path, binding, message)
    assert not (tmp_path / "host-logs").exists()
    message["senderThinkthreadId"] = "tt-child"
    monkeypatch.setenv("GOAL_PLUS_SESSION_DIAGNOSTICS", "0")
    assert archive_diagnostic_message(tmp_path, binding, message)
    assert not (tmp_path / "host-logs").exists()
    monkeypatch.delenv("GOAL_PLUS_SESSION_DIAGNOSTICS")
    payload = {**archive_messages[0], "chunk_sha256": "0" * 64}
    message["text"] = json.dumps(payload)
    assert archive_diagnostic_message(tmp_path, binding, message)
    assert not (tmp_path / "host-logs").exists()


def test_cleanup_collector_uses_separate_cursor(tmp_path, archive_messages):
    binding = {"thinkthread_id": "tt-child", "agent_session_id": "agent-owned", "message_cursor": "business"}
    class Client:
        def invoke(self, method, params, **kwargs):
            assert method == "message.receive"
            assert params.get("after") != "business"
            return {"messages": [{"senderThinkthreadId": "tt-child", "text": json.dumps(item)}
                                 for item in archive_messages], "nextCursor": "last", "hasMore": False}
    collect_child_diagnostics(tmp_path, binding, Client())
    index = json.loads((tmp_path / "host-logs/session-diagnostics/tt-child/index.json").read_text())
    assert index["collection_complete"] and len(index["batches"]) == 3
    assert binding["message_cursor"] == "business"


def test_missing_chunk_and_reordered_completion_are_explicit(tmp_path, archive_messages):
    binding = {"thinkthread_id": "tt-child", "agent_session_id": "agent-owned"}
    payload = archive_messages[0]
    data = base64.b64decode(payload["data_base64"])
    split = len(data) // 2
    chunks = [data[:split], data[split:]]
    messages = [{"senderThinkthreadId": "tt-child", "text": json.dumps({
        **payload, "chunk_count": 2, "chunk_index": number,
        "data_base64": base64.b64encode(chunk).decode(), "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
    })} for number, chunk in enumerate(chunks)]
    directory = tmp_path / "host-logs/session-diagnostics/tt-child"
    archive_diagnostic_message(tmp_path, binding, messages[1])
    assert json.loads((directory / "index.json").read_text())["status"] == "partial"
    assert _analyze(directory)["tool_analysis"]["status"] == "unavailable"
    archive_diagnostic_message(tmp_path, binding, messages[0])
    assert _analyze(directory)["tool_analysis"]["status"] == "observed"
    archive_diagnostic_message(tmp_path, binding, messages[0])
    assert len(json.loads((directory / "index.json").read_text())["batches"]) == 1
    batch_path = directory / (payload["batch_sha256"] + ".json")
    batch = json.loads(batch_path.read_text())
    batch["leaf_id"] = "tampered"
    batch_path.write_text(json.dumps(batch))
    assert _analyze(directory)["tool_analysis"]["status"] == "unavailable"


SCRIPT = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
const {SessionArchive, diagnosticEntry} = await import(pathToFileURL(process.argv[1]).href);
delete process.env.GOAL_PLUS_DEBUG;
delete process.env.GOAL_PLUS_SESSION_DIAGNOSTICS;
const entries = [], messages = [];
const append = entry => entries.push({id: String(entries.length), parentId: entries.at(-1)?.id ?? null, ...entry});
append({type: 'message', message: {role: 'user', content: 'PRIVATE USER'}});
const tool = {name: 'goal_plus_status', description: 'Status', parameters: {type: 'object'}};
const serialized = JSON.stringify({registered: [tool], request: [tool]});
const sha256 = createHash('sha256').update(serialized).digest('hex');
append({type: 'custom', customType: 'goal-plus-tool-contracts', data: {version: 1, serialized, sha256}});
const ctx = {cwd: process.argv[2], sessionManager: {getSessionId: () => 'native-session',
  getEntries: () => entries, getLeafId: () => entries.at(-1)?.id, getSessionFile: () => '/native/session.jsonl'}};
const options = {root: '.gp', role: 'worker', sourceRoot: '/installed',
  checkpoint: data => append({type: 'custom', customType: 'goal-plus-session-archive-checkpoint', data}),
  worker: async () => ({
  agentSessionId: 'agent-owned', send: async batch => {messages.push(...batch);},
})};
let archive = new SessionArchive();
for (let i = 0; i < 3; i++) {
  // Retained TT sessions reload the extension in a new Pi process on wake.
  archive = new SessionArchive();
  append({type: 'custom', customType: 'goal-plus-provider-request', data: {version: 1,
    session_id: 'native-session', request_id: `request-${i}`, contracts_sha256: sha256, status: 'observed'}});
  append({type: 'message', message: {role: 'assistant', stopReason: 'toolUse', content: [
    {type: 'thinking', thinking: 'PRIVATE THINKING'}, {type: 'text', text: 'PRIVATE TEXT'},
    {type: 'toolCall', id: `call-${i}`, name: tool.name, arguments: {}},
    {type: 'toolCall', id: `native-${i}`, name: 'bash', arguments: {command: 'PRIVATE COMMAND'}},
  ]}});
  append({type: 'message', message: {role: 'toolResult', toolCallId: `call-${i}`,
    toolName: tool.name, isError: false, content: [{type: 'text', text: 'ready'}]}});
  await archive.flush(ctx, options);
  const count = messages.length;
  await archive.flush(ctx, options);
  assert.equal(messages.length, count);
}
assert.equal(messages.length, 3);
assert.ok(!JSON.stringify(entries.map(diagnosticEntry)).includes('PRIVATE'));
process.env.GOAL_PLUS_SESSION_DIAGNOSTICS = '0';
append({type: 'message', message: {role: 'user', content: 'PRIVATE'}});
await archive.flush(ctx, options);
assert.equal(messages.length, 3);
delete process.env.GOAL_PLUS_SESSION_DIAGNOSTICS;
await archive.flush(ctx, {...options, worker: undefined});
console.log(JSON.stringify(messages));
'''
