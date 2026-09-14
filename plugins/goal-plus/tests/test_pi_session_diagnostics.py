"""Exercise the request hook without sending model requests or recording prompts."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_request_contract_observation(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Pi diagnostics")
    module = Path(__file__).resolve().parents[1] / "assets/pi/extensions/lib/goal-plus-session-diagnostics.ts"
    result = subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT, str(module)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


SCRIPT = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {registerSessionDiagnostics} = await import(pathToFileURL(process.argv[1]).href);
const handlers = new Map();
let entries = [];
let reads = 0;
const pi = {on: (name, handler) => handlers.set(name, handler),
  appendEntry: (customType, data) => entries.push({id: String(entries.length), parentId: entries.at(-1)?.id ?? null, type: 'custom', customType, data})};
const ctx = {sessionManager: {getLeafId: () => entries.at(-1)?.id ?? null,
  getEntry: id => {reads++; return entries.find(e => e.id === id);}, getSessionId: () => 'session-a'},
  model: {provider: 'zai', id: 'glm-5.3-flash'}};
const contract = {name: 'goal_plus_save_spec_draft', description: 'Nested draft', parameters: {type: 'object'}};
registerSessionDiagnostics(pi, new Map([[contract.name, contract]]));
const payload = {messages: [{role: 'system', content: 'PRIVATE PROMPT'}],
  tools: [{type: 'function', function: contract}, {type: 'function', function: {name: 'peer', description: 'PRIVATE TOOL'}}]};
const serialized = JSON.stringify(payload);
process.env.GOAL_PLUS_SESSION_DIAGNOSTICS = '0';
process.env.GOAL_PLUS_DEBUG = '1';
handlers.get('before_provider_request')({payload}, ctx);
assert.equal(entries.length, 0);
delete process.env.GOAL_PLUS_SESSION_DIAGNOSTICS;
delete process.env.GOAL_PLUS_DEBUG;
handlers.get('turn_start')({turnIndex: 3});
assert.equal(handlers.get('before_provider_request')({payload}, ctx), undefined);
assert.equal(JSON.stringify(payload), serialized);
assert.equal(entries.length, 2);
const snapshot = JSON.parse(entries[0].data.serialized);
assert.deepEqual(snapshot.request, [contract]);
assert.equal(entries[1].data.turn_index, 3);
assert.equal(entries[1].data.model.id, 'glm-5.3-flash');
assert.equal(entries[1].data.status, 'observed');
assert.equal(entries[1].data.request_bytes, undefined);
assert.ok(!JSON.stringify(entries).includes('PRIVATE'));
Object.defineProperty(payload, 'messages', {get() {throw new Error('request body must not be traversed');}, configurable: true});
const firstReads = reads;
handlers.get('before_provider_request')({payload}, ctx);
assert.equal(entries.length, 3);
assert.equal(reads, firstReads, 'known branch leaf needs no history scan');
assert.notEqual(entries[1].data.request_id, entries[2].data.request_id);
Object.defineProperty(payload, 'messages', {value: [{role: 'system', content: 'PRIVATE PROMPT'}], configurable: true});
// Branch switches require a new snapshot even when the tool catalog is identical.
entries = [];
handlers.get('before_provider_request')({payload}, ctx);
assert.equal(entries.length, 2);
process.env.GOAL_PLUS_DEBUG = '1';
handlers.get('before_provider_request')({payload}, ctx);
assert.equal(entries.at(-1).data.request_bytes, Buffer.byteLength(serialized));
handlers.get('before_provider_request')({payload: {tools: [{functionDeclarations: [contract]}]}}, ctx);
assert.equal(entries.at(-1).data.status, 'unavailable');
handlers.get('before_provider_request')({payload: {tools: [{name: contract.name, description: '', input_schema: {type: 'object'}}]}}, ctx);
assert.equal(entries.at(-1).data.status, 'observed');
pi.appendEntry = () => {throw new Error('disk full');};
assert.doesNotThrow(() => handlers.get('before_provider_request')({payload}, ctx));
'''
