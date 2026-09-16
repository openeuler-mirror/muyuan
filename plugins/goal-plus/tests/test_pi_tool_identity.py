"""Exercise the actual Pi extension with a host registry and transport double."""

import os
import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.pi


@pytest.fixture(autouse=True)
def mcp_schema_manifest(tmp_path):
    from fastmcp import Client
    from goal_plus.server import create_host_mcp

    async def manifest():
        async with Client(create_host_mcp(tmp_path / ".gp", agent_harness="pi")) as client:
            instructions = client.initialize_result.instructions if client.initialize_result is not None else client.instructions
            return {"instructions": instructions, "tools": [tool.model_dump(mode="json", by_alias=True) for tool in await client.list_tools()]}

    (tmp_path / "mcp-manifest.json").write_text(json.dumps(asyncio.run(manifest())))


@pytest.mark.parametrize("role", ["main", "worker", "final-checker", "ordinary"])
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("diagnostics", [False, True])
def test_pi_tool_ownership_and_registration_gates(tmp_path, role, debug, diagnostics):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Pi extension")
    supported = subprocess.run(
        [node, "--input-type=module", "-e",
         "import {registerHooks} from 'node:module';"], capture_output=True,
    )
    if supported.returncode:
        pytest.skip("Node with synchronous module hooks is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT,
         str(ROOT / "assets/pi/extensions/goal-plus.ts"), str(tmp_path)],
        env={**{key: value for key, value in os.environ.items()
                if not key.startswith(("GOAL_PLUS_", "THINKTHREAD_"))},
             "GOAL_PLUS_PI_ROLE": role,
             "GOAL_PLUS_SESSION_DIAGNOSTICS": "1" if diagnostics else "0",
             "GOAL_PLUS_DEBUG": "1" if debug else "0"},
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ownership checks passed" in result.stdout
    diagnostics = [json.loads(line) for line in result.stderr.splitlines()
                   if line.startswith('{"event":"goal_plus_api_response_size"')]
    if debug and role != "ordinary":
        assert len(diagnostics) == 1
        expected = (
            {"agent_session_id": "worker-session", "candidate_task": {"workspace": str(tmp_path)}}
            if role == "worker" else
            {"goal_plus_id": "gp_0001", "raw_goal": "\u4e2d\u6587", "status": "active",
             "phase": "goal", "goal_revision": 1, "control": {"state": "active", "version": 1},
             "active_session": {"session_id": "pi-test"}}
        )
        text = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        response = {"content": [{"type": "text", "text": text}], "details": expected}
        assert diagnostics[0] == {
            "event": "goal_plus_api_response_size", "transport": "pi-tool",
            "tool": "goal_plus_search_get_agent_context" if role == "worker" else "goal_plus_status",
            "format": "json",
            "bytes": len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()),
            "characters": len(json.dumps(response, ensure_ascii=False, separators=(",", ":"))),
            "text_bytes": len(text.encode()), "details_bytes": len(text.encode()),
            "text_characters": len(text), "details_characters": len(text),
        }
        assert "raw_goal" not in result.stderr
    else:
        assert diagnostics == []


def _run_env_script(tmp_path, script_env):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Pi extension")
    supported = subprocess.run(
        [node, "--input-type=module", "-e", "import {registerHooks} from 'node:module';"],
        capture_output=True,
    )
    if supported.returncode:
        pytest.skip("Node with synchronous module hooks is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT,
         str(ROOT / "assets/pi/extensions/goal-plus.ts"), str(tmp_path)],
        env={**{key: value for key, value in os.environ.items()
                if not key.startswith(("GOAL_PLUS_", "THINKTHREAD_"))},
             **script_env},
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _run_context_script(tmp_path, sdk_mode=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Pi extension")
    supported = subprocess.run(
        [node, "--input-type=module", "-e", "import {registerHooks} from 'node:module';"],
        capture_output=True,
    )
    if supported.returncode:
        pytest.skip("Node with synchronous module hooks is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT,
         str(ROOT / "assets/pi/extensions/goal-plus.ts"), str(tmp_path)],
        env={**{key: value for key, value in os.environ.items()
                if not key.startswith(("GOAL_PLUS_", "THINKTHREAD_"))},
             **({"GOAL_PLUS_PI_ROLE": "worker", "GOAL_PLUS_TT_SDK_TEST": sdk_mode,
                 "GOAL_PLUS_AGENT_POSIX_SDK_ENTRY": "file:///fixture-sdk.mjs", "THINKTHREAD_PARENT_ID": "parent"}
                if sdk_mode else {"GOAL_PLUS_PI_ROLE": "main", "GOAL_PLUS_PI_WORK_RECEIPT_TEST": "1"})},
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_pi_command_context_and_shared_work_item_forwarding(tmp_path):
    result = _run_context_script(tmp_path)
    assert "work receipt checks passed" in result.stdout


def test_pi_gate_distinguishes_paused_goal_from_unauthorized(tmp_path):
    result = _run_env_script(tmp_path, {
        "GOAL_PLUS_PI_ROLE": "main",
        "GOAL_PLUS_ID": "gp_0001",
        "GOAL_PLUS_PI_PAUSED_GATE_TEST": "1",
    })
    assert "paused gate checks passed" in result.stdout


def test_pi_abort_notifies_user_of_paused_goal(tmp_path):
    result = _run_env_script(tmp_path, {
        "GOAL_PLUS_PI_ROLE": "main",
        "GOAL_PLUS_PI_ABORT_NOTIFY_TEST": "1",
    })
    assert "abort notify checks passed" in result.stdout


@pytest.mark.parametrize("sdk_mode", ["compatible", "parent", "capability", "interface"])
def test_thinkthread_sdk_drift_preserves_identity_and_interface_gates(tmp_path, sdk_mode):
    result = _run_context_script(tmp_path, sdk_mode)
    assert "SDK compatibility checks passed" in result.stdout
    assert "SDK differs from the tested baseline" in result.stderr


def test_thinkthread_steering_starts_before_native_user_entry_is_persisted(tmp_path):
    result = _run_context_script(tmp_path, "steering")
    assert "steering lifecycle checks passed" in result.stdout


SCRIPT = r'''
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { readFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
const extensionPath = process.argv[1];
const cwd = process.argv[2];
globalThis.mcpManifest = JSON.parse(readFileSync(cwd + "/mcp-manifest.json", "utf8"));
globalThis.calls = [];
globalThis.status = { goal_plus_id: "gp_0001", raw_goal: "\u4e2d\u6587", status: "active",
  phase: "goal", goal_revision: 1, control: {state: "active", version: 1},
  active_session: {session_id: "pi-test"} };
const mocks = {
  "file:///fixture-sdk.mjs": `
    export const CONTROL_PROTOCOL_VERSION = 999;
    export const CONTRACT_FINGERPRINT = "future-compatible";
    export const AgentPosixClient = {fromEnv() {
      if (process.env.GOAL_PLUS_TT_SDK_TEST === "interface") return {};
      return {async invoke(name, args) {
      if (name === "self") return {
        parentThinkthreadId: process.env.GOAL_PLUS_TT_SDK_TEST === "parent" ? "peer" : "parent",
        capabilities: [{id: process.env.GOAL_PLUS_TT_SDK_TEST === "capability" ? "thinkthread.child" : "thinkthread.message"}],
      };
      if (name === "message.send") {
        globalThis.registration = args;
        (globalThis.ttSent ??= []).push(JSON.parse(args.text));
        return {messageId: "outgoing"};
      }
      if (name === "message.receive" && process.env.GOAL_PLUS_TT_SDK_TEST === "steering") {
        return {messages: globalThis.ttMessages, nextCursor: "cursor", hasMore: false};
      }
      throw Error("unexpected SDK operation");
    } }; } };
  `,
  "@earendil-works/pi-coding-agent": "export const parseArgs = () => ({});",
  "@earendil-works/pi-tui": "export class Box {}; export class Text {};",
  "typebox": "export const Type = {};",
  "./lib/goal-plus-schemas.ts": "export const LooseObject = {}; export const RuntimeToolSchemas = {}; export const RuntimeToolDescriptions = {};",
  "./lib/goal-plus-transport.ts": `
    export const pythonExecutable = "python", runtimeRoot = ".gp";
    export const isRecord = v => v !== null && typeof v === "object";
    export const sourceRoot = ctx => ctx.cwd;
    export const projectModuleInvocation = () => {
      if (globalThis.workReceiptTest) return {command: "fixture-worker", argsPrefix: []};
      throw Error("unexpected process launch");
    };
    export const parseJsonObject = JSON.parse;
    export const commandFailure = () => { throw Error("unexpected command failure"); };
    export async function closeMcpConnections() {}
    export async function runJsonCli(pi, ctx, name, args) {
      if (name === "host_mcp_manifest") return {details: globalThis.mcpManifest};
      globalThis.calls.push({name, args});
      let details = name === "goal_plus_gate"
        ? {decision: "block", reason: "fixture phase gate"} : globalThis.status;
      if (globalThis.workReceiptTest) {
        if (name === "goal_plus_gate") details = {decision: "allow"};
        if (name === "host_execution_scope_open") details = {scope_id: "scope-test"};
        if (name === "goal_plus_record_work_event") {
          const previous = globalThis.status.work_items?.[0];
          const generation = args.event === "rework" ? previous.generation + 1 : previous?.generation ?? 1;
          const item = {work_item_id: args.work_item_id, attempt_id: "attempt-" + generation,
            generation, agent_id: args.agent_id, status: args.event === "result" ? "result_ready" : "launching"};
          globalThis.status = {...globalThis.status, work_items: [item]};
          details = {goal_plus_id: "gp_0001", status: "active", work_item: item};
        }
      }
      return {content: [{type: "text", text: JSON.stringify(details)}], details};
    }
    export async function runHostCommandCli(pi, ctx, name, args) {
      globalThis.calls.push({name, args});
      return {details: globalThis.status};
    }
  `,
};
registerHooks({ resolve(specifier, context, next) {
  if (Object.hasOwn(mocks, specifier)) return {
    url: "data:text/javascript," + encodeURIComponent(mocks[specifier]), shortCircuit: true,
  };
  return next(specifier, context);
}});
const {default: register} = await import(pathToFileURL(extensionPath).href);
const tools = new Map(), sources = new Map(), events = new Map(), commands = new Map();
const pi = {
  registerTool(tool) {
    tools.set(tool.name, tool);
    sources.set(tool.name, {path: extensionPath, source: "package"});
  },
  getAllTools() { return [...sources].map(([name, sourceInfo]) => ({name, sourceInfo})); },
  getActiveTools() { return [...tools.keys()]; },
  setActiveTools() {},
  registerCommand(name, command) {commands.set(name, command);},
  on(name, handler) {events.set(name, handler);},
  sendMessage() {}, sendUserMessage() {}, appendEntry() {}, getThinkingLevel() {return "medium";},
};
const ctx = {cwd, mode: "interactive", hasUI: false, isIdle: () => true,
  isProjectTrusted: () => true,
  sessionManager: {getEntries: () => [], isPersisted: () => false, getSessionId: () => "pi-test"},
  model: {provider: "fixture", id: "fixture-model"},
  ui: {notify() {}, setStatus() {}, setWidget() {}},
};
await register(pi);
if (process.env.GOAL_PLUS_PI_ROLE === 'main') {
  ctx.sessionManager.getSessionFile = () => cwd + '/native-session.jsonl';
  await events.get('agent_end')({messages: [{role: 'assistant', stopReason: 'stop', content: []}]}, ctx);
  const indexPath = cwd + '/.gp/host-logs/session-diagnostics/pi-'
    + createHash('sha256').update('pi-test').digest('hex') + '/index.json';
  assert.equal(existsSync(indexPath), process.env.GOAL_PLUS_SESSION_DIAGNOSTICS !== '0');
  if (existsSync(indexPath)) {
    const index = JSON.parse(readFileSync(indexPath, 'utf8'));
    assert.equal(index.history, cwd + '/native-session.jsonl');
    assert.ok(index.overview.endsWith('/assets/goal-plus-instructions.txt'));
  }
}
{
  const diagnostics = [];
  const savedAppend = pi.appendEntry;
  pi.appendEntry = (customType, data) => diagnostics.push({id: String(diagnostics.length),
    parentId: diagnostics.at(-1)?.id ?? null, type: 'custom', customType, data});
  ctx.sessionManager.getBranch = () => diagnostics;
  ctx.sessionManager.getLeafId = () => diagnostics.at(-1)?.id ?? null;
  ctx.sessionManager.getEntry = id => diagnostics.find(e => e.id === id);
  const payload = {tools: [...tools.values()].map(tool => ({type: 'function', function: {
    name: tool.name, description: tool.description, parameters: tool.parameters,
  }}))};
  events.get('before_provider_request')({payload}, ctx);
  if (process.env.GOAL_PLUS_SESSION_DIAGNOSTICS !== '0' && tools.size > 0) {
    assert.equal(diagnostics.at(-1).data.status, 'observed');
    const catalog = JSON.parse(diagnostics[0].data.serialized);
    for (const tool of catalog.request) {
      const registered = catalog.registered.find(item => item.name === tool.name);
      assert.deepEqual(tool, registered);
      const shared = globalThis.mcpManifest.tools.find(item => item.name === tool.name);
      if (shared) {
        assert.equal(tool.description, shared.description);
        assert.deepEqual(tool.parameters, shared.inputSchema);
      }
    }
  } else if (process.env.GOAL_PLUS_SESSION_DIAGNOSTICS === '0') assert.equal(diagnostics.length, 0);
  pi.appendEntry = savedAppend;
}
if (process.env.GOAL_PLUS_TT_SDK_TEST) {
  const mode = process.env.GOAL_PLUS_TT_SDK_TEST;
  if (mode === "steering") {
    const nonce = "22345678-1234-1234-1234-123456789abc";
    const prompt = "registration_nonce=12345678-1234-1234-1234-123456789abc "
      + "agent_session_id=agent_owned dispatch_nonce=" + nonce + ".";
    globalThis.ttMessages = [{senderThinkthreadId: "parent", messageId: "initial", truncated: false, text: prompt}];
    let injected = [];
    pi.sendUserMessage = (text, options) => injected.push({text, options});
    ctx.isIdle = () => false;
    await events.get("before_agent_start")({prompt, systemPrompt: "worker"}, ctx);
    assert.deepEqual(ctx.sessionManager.getEntries(), []);
    const content_json = JSON.stringify({agent_session_id: "agent_owned", dispatch_nonce: nonce,
      message_id: "initial", prompt: "revised", deadline_unix_ms: Date.now() + 10000});
    globalThis.ttMessages.push({senderThinkthreadId: "parent", messageId: "control", truncated: false,
      text: JSON.stringify({protocol: "goal-plus.thinkthread.v3", type: "session_message", request_id: "control",
        content_json, content_sha256: createHash("sha256").update(content_json).digest("hex")})});
    await events.get("agent_start")({}, ctx);
    for (let n = 0; n < 100 && !globalThis.ttSent.some(item => item.type === "session_message_ack"); n++) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert.deepEqual(injected, [{text: "revised", options: {deliverAs: "steer"}}]);
    assert.equal(globalThis.ttSent.find(item => item.type === "session_message_ack").accepted, true);
    await events.get("session_shutdown")({reason: "test"}, ctx);
    console.log("steering lifecycle checks passed");
    process.exit(0);
  }
  ctx.sessionManager.getEntries = () => [{type: "message", text:
    "registration_nonce=12345678-1234-1234-1234-123456789abc agent_session_id=agent_owned"}];
  const start = () => events.get("session_start")({reason: "reload"}, ctx);
  if (mode === "parent") await assert.rejects(start, /authenticated ThinkThread Child/);
  else if (mode === "capability") await assert.rejects(start, /Message-only Capability/);
  else if (mode === "interface") await assert.rejects(start, /missing invoke/);
  else {
    await start();
    assert.equal(globalThis.registration.recipientThinkthreadId, "parent");
    assert.equal(JSON.parse(globalThis.registration.text).type, "registration");
  }
  console.log("SDK compatibility checks passed");
  process.exit(0);
}
const overview = await events.get("before_agent_start")({prompt: "ordinary task", systemPrompt: "host system"}, ctx);
if (process.env.GOAL_PLUS_PI_ROLE === "main") {
  assert.equal(overview.systemPrompt, "host system\n\n" + globalThis.mcpManifest.instructions);
  const next = await events.get("before_agent_start")({prompt: "continued task", systemPrompt: "rebuilt system"}, ctx);
  assert.equal(next.systemPrompt, "rebuilt system\n\n" + globalThis.mcpManifest.instructions);
  const observations = [];
  pi.appendEntry = (type, data) => observations.push({type, data});
  const debug = process.env.GOAL_PLUS_SESSION_DIAGNOSTICS !== "0";
  ctx.getSystemPrompt = () => {
    assert.equal(debug, true, "disabled diagnostics must not read the system prompt");
    return next.systemPrompt;
  };
  await events.get("turn_start")({turnIndex: 0}, ctx);
  ctx.getSystemPrompt = () => "prompt overwritten by another extension";
  await events.get("turn_start")({turnIndex: 1}, ctx);
  assert.deepEqual(observations.map(item => item.data.overview_present), debug ? [true, false] : []);
  if (debug) {
    assert.equal(observations[0].type, "goal-plus-instructions-observation");
    assert.equal(observations[0].data.session_id, "pi-test");
    assert.equal(observations[0].data.expected_sha256,
      createHash("sha256").update(globalThis.mcpManifest.instructions.trim()).digest("hex"));
  }
  pi.appendEntry = () => {};
} else {
  assert.equal(overview, undefined);
  pi.appendEntry = () => {throw new Error("worker must not observe Main instructions");};
  await events.get("turn_start")({turnIndex: 0}, ctx);
  pi.appendEntry = () => {};
}
for (const tool of globalThis.mcpManifest.tools) {
  if (tools.has(tool.name)) assert.deepEqual(tools.get(tool.name).parameters, tool.inputSchema);
}
for (const action of ["open", "wait_any", "snapshot", "continue", "close"]) {
  assert.equal(tools.has(`pi_search_pool_${action}`), false);
  assert.equal(tools.has(`thinkthread_search_pool_${action}`), false);
}
if (process.env.GOAL_PLUS_PI_ABORT_NOTIFY_TEST === "1") {
  globalThis.workReceiptTest = true;
  const notices = [];
  pi.sendMessage = message => notices.push(message);
  pi.sendUserMessage = () => {};
  await commands.get("goal-plus").handler("fixture", ctx);
  assert.ok(notices.some(item => item.customType === "goal-plus-created"),
    "host start must authorize the session first");
  notices.length = 0;
  await events.get("agent_end")(
    {messages: [{role: "assistant", stopReason: "aborted", content: []}]}, ctx);
  await events.get("agent_settled")({}, ctx);
  const notice = notices.find(item => item.customType === "goal-plus-control");
  assert.ok(notice, "abort must send a user-visible paused notice");
  assert.ok(notice.content.includes("gp_0001") && notice.content.includes("已暂停"));
  assert.ok(notice.display === true);
  console.log("abort notify checks passed");
  process.exit(0);
}
if (process.env.GOAL_PLUS_PI_WORK_RECEIPT_TEST === "1") {
  globalThis.workReceiptTest = true;
  globalThis.status.phase = "goal";
  const pendingContext = [];
  let commandStart;
  pi.sendMessage = (message, options) => {
    if (options?.deliverAs === "nextTurn") pendingContext.push(message);
  };
  pi.sendUserMessage = text => {
    commandStart = (async () => {
      const callsBeforeInput = calls.length;
      await events.get("input")({text, source: "extension"}, ctx);
      assert.equal(calls.length, callsBeforeInput, "host continuation is not a new user event");
      return events.get("before_agent_start")({prompt: text, systemPrompt: "native command system"}, ctx);
    })();
  };
  await commands.get("goal-plus").handler("fixture", ctx);
  assert.ok(commandStart, "the native command must enter prompt loading");
  assert.equal((await commandStart).systemPrompt, "native command system\n\n" + globalThis.mcpManifest.instructions);
  assert.equal(pendingContext.at(-1).details.goal_plus_id, "gp_0001");
  assert.equal(pendingContext.at(-1).details.goal_revision, 1);
  assert.equal(pendingContext.at(-1).details.control_version, 1);
  assert.equal(pendingContext.at(-1).details.session_id, "pi-test");
  globalThis.status = {...globalThis.status, active_session: undefined};
  await events.get("context")({messages: []}, ctx);
  await tools.get("goal_plus_session_run").execute("run", {run_id: "run_1", candidate_id: "c001", call_id: "first", prompt: "task", timeout_seconds: 30}, undefined, undefined, ctx);
  const scope = calls.find(c => c.name === "host_execution_scope_open");
  assert.equal(scope.args.native_session_id, "pi-test");
  assert.equal(scope.args.run_id, "run_1");
  const invoke = () => tools.get("goal_plus_run_work_item").execute("call", {
    goal_plus_id: "gp_0001", work_item_id: "work", task: "implement",
  }, undefined, undefined, ctx);
  await invoke();
  const forwarded = calls.find(c => c.name === "goal_plus_run_work_item");
  assert.deepEqual(forwarded.args, {goal_plus_id: "gp_0001", work_item_id: "work", task: "implement"});
  assert.equal(tools.get("goal_plus_run_work_item").executionMode, "concurrent");
  console.log("work receipt checks passed");
  process.exit(0);
}
const event = name => ({type: "tool_call", toolName: name, toolCallId: "call", input: {}});
const call = name => tools.get(name).execute("call", {}, undefined, undefined, ctx);
if (process.env.GOAL_PLUS_PI_ROLE !== "main") {
  const foreign = name => sources.set(name, {path: cwd + "/foreign.ts", source: "package"});
  if (process.env.GOAL_PLUS_PI_ROLE === "worker") {
    foreign("goal_plus_search_get_agent_context");
    assert.equal((await events.get("tool_call")(event("goal_plus_search_get_agent_context"), ctx)).block, true);
    globalThis.status = {agent_session_id: "worker-session", candidate_task: {workspace: cwd}};
    await call("goal_plus_search_get_agent_context"); // Only the registered callback activates context.
    assert(tools.has("goal_plus_search_run_verifier")); // Dynamic tools also use the registration gate.
    for (const name of ["goal_plus_search_prepare_recovery", "search_emails", "write"]) {
      foreign(name);
      assert.equal(await events.get("tool_call")(event(name), ctx), undefined);
    }
    sources.set("write", {path: "<builtin:write>", source: "builtin"});
    assert.equal((await events.get("tool_call")({ ...event("write"), input: {path: "../outside"}}, ctx)).block, true);
  } else if (process.env.GOAL_PLUS_PI_ROLE === "final-checker") {
    await call("goal_plus_status");
    sources.set("edit", {path: "<builtin:edit>", source: "builtin"});
    assert.equal((await events.get("tool_call")(event("edit"), ctx)).block, true);
    foreign("edit");
    assert.equal(await events.get("tool_call")(event("edit"), ctx), undefined);
    assert.deepEqual([...tools.keys()].sort(), ["goal_plus_status", "goal_plus_submit_final_check"]);
  } else {
    assert.equal(tools.size, 0);
    for (const name of ["goal_plus_search_create", "goal_plus_search_prepare_recovery", "goal_plus_submit_final_check"]) {
      foreign(name);
      assert.equal(await events.get("tool_call")(event(name), ctx), undefined);
    }
  }
  console.log("ownership checks passed");
  process.exit(0);
}
if (process.env.GOAL_PLUS_PI_PAUSED_GATE_TEST === "1") {
  globalThis.status = {...globalThis.status,
    control: {state: "paused", reason: "user_interrupt", version: 2}};
  await assert.rejects(() => call("goal_plus_search_create"),
    /GoalPlusResumeRequired: Goal Plus gp_0001 is paused \(reason=user_interrupt\).*\/goal-plus resume/);
  await call("goal_plus_status"); // Read-only tools remain available while paused.
  globalThis.status = {...globalThis.status,
    control: {state: "active", version: 3}, status: "complete"};
  await assert.rejects(() => call("goal_plus_search_create"),
    /GoalPlusTerminal: Goal Plus gp_0001 is finished \(status=complete\)/);
  console.log("paused gate checks passed");
  process.exit(0);
}
const readOnly = new Set(["goal_plus_search_recovery_status", "goal_plus_status",
  "goal_plus_history",
  "goal_plus_monitor_snapshot", "goal_plus_list_models", "goal_plus_search_status",
  "goal_plus_search_list_history", "goal_plus_search_list_iterations", "goal_plus_search_get_agent_observability"]);
const resourceControls = new Set(["goal_plus_session_wait", "goal_plus_session_interrupt",
  "goal_plus_session_stop", "goal_plus_session_close"]);
const ownMutations = [...tools.keys()].filter(name => !readOnly.has(name) && !resourceControls.has(name));
for (const name of ownMutations) {
  await assert.rejects(() => call(name), /GoalPlusEntrypointRequired/);
}
assert.equal(calls.length, 0); // Rejection precedes model/runtime/process work.

// Both ordinary prefixed tools and exact-name overrides are foreign registrations.
for (const name of ["search_emails", "goal_plus_external", "pi_search_pool_external",
    "thinkthread_search_pool_external", "goal_plus_search_create", "bash", "edit"]) {
  sources.set(name, {path: cwd + "/another-extension.ts", source: "package"});
  assert.equal(await events.get("tool_call")(event(name), ctx), undefined);
}
// Missing source metadata never lends another tool ownership. Our registration
// closure still refuses direct execution without authorization.
sources.delete("goal_plus_search_create");
assert.equal(await events.get("tool_call")(event("goal_plus_search_create"), ctx), undefined);
await assert.rejects(() => call("goal_plus_search_create"), /GoalPlusEntrypointRequired/);
await call("goal_plus_status");
assert.equal(calls.at(-1).name, "goal_plus_status");

await commands.get("goal-plus").handler("fixture", ctx);
assert(calls.some(c => c.name === "goal_plus_host_start"));
calls.length = 0;
// A live Goal must not intercept an effective foreign override either.
for (const name of ["search_emails", "goal_plus_search_create", "bash", "edit"]) {
  assert.equal(await events.get("tool_call")(event(name), ctx), undefined);
}
assert.equal(calls.length, 0);
// Actual native tools still obey the attached Goal phase gate.
sources.set("bash", {path: "<builtin:bash>", source: "builtin"});
assert.equal((await events.get("tool_call")(event("bash"), ctx)).block, true);
assert.equal(calls.at(-1).args.context.tool_name, "bash");
for (const name of ownMutations) {
  await assert.rejects(() => call(name), /fixture phase gate/);
  assert.equal(calls.at(-1).args.context.tool_name, "mcp__goal_plus__" + name);
}
console.log("ownership checks passed");
'''
