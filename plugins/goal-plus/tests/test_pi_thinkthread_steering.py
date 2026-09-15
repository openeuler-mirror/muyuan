"""Pi's actual TT protocol endpoint must steer only its active native turn."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_pi_endpoint_checks_identity_expiry_replay_and_active_turn():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Pi endpoint")
    module = Path(__file__).resolve().parents[1] / "assets/thinkthread-session-rpc.ts"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT, str(module)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


SCRIPT = r'''
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {pathToFileURL} from "node:url";
const {WorkerSteeringReceiver, THINKTHREAD_GOAL_PLUS_PROTOCOL: protocol} = await import(pathToFileURL(process.argv[1]));
let messages = [], replies = [], prompts = [], active = true, loseReply = false, interrupts = 0;
const client = {async invoke(method, params) {
    if (method === "message.receive") return {messages, nextCursor: "cursor"};
    assert.equal(method, "message.send");
    assert.equal(params.wake, false);
    if (loseReply) { loseReply = false; throw Error("lost ack"); }
    replies.push(JSON.parse(params.text));
    return {messageId: "ack"};
}};
const receiver = new WorkerSteeringReceiver(client, "parent", {
    agentSessionId: "agent", dispatchNonce: "dispatch", messageId: "native-message",
}, () => active, prompt => prompts.push(prompt), () => { interrupts++; });
function request(id, patch = {}, sender = "parent", type = "session_message") {
    const content_json = JSON.stringify({agent_session_id: "agent", dispatch_nonce: "dispatch",
        message_id: "native-message", prompt: "revise target", deadline_unix_ms: Date.now() + 10000, ...patch});
    return {senderThinkthreadId: sender, truncated: false, text: JSON.stringify({
        protocol, type, request_id: id, content_json,
        content_sha256: createHash("sha256").update(content_json).digest("hex"),
    })};
}
const first = request("first");
messages = [first]; loseReply = true;
await assert.rejects(receiver.poll(), /lost ack/);
await receiver.poll(); await receiver.poll();
assert.deepEqual(prompts, ["revise target"]);
assert.ok(replies.every(reply => reply.accepted === true));
for (const patch of [{agent_session_id:"peer"}, {dispatch_nonce:"old"}, {message_id:"old"},
                     {deadline_unix_ms:0}, {deadline_unix_ms:"future"}, {prompt:" "}]) {
    messages = [request(JSON.stringify(patch), patch)];
    await receiver.poll();
    assert.equal(replies.at(-1).accepted, false);
}
messages = [request("foreign", {}, "peer")];
const count = replies.length; await receiver.poll(); assert.equal(replies.length, count);
messages = [request("interrupt", {}, "parent", "session_interrupt")];
await receiver.poll(); await receiver.poll();
assert.equal(interrupts, 1);
assert.equal(replies.at(-1).type, "session_interrupt_ack");
assert.equal(replies.at(-1).accepted, true);
for (const patch of [{agent_session_id:"peer"}, {dispatch_nonce:"old"}, {message_id:"old"}, {deadline_unix_ms:0}]) {
    messages = [request("interrupt-" + JSON.stringify(patch), patch, "parent", "session_interrupt")];
    await receiver.poll();
    assert.equal(replies.at(-1).accepted, false);
}
assert.equal(interrupts, 1);
active = false; messages = [request("idle")]; await receiver.poll();
assert.equal(replies.at(-1).accepted, false);
messages = [request("interrupt-idle", {}, "parent", "session_interrupt")]; await receiver.poll();
assert.equal(replies.at(-1).accepted, false);
active = true; receiver.close(); messages = [request("closed")]; await receiver.poll();
assert.deepEqual(prompts, ["revise target"]);
'''
