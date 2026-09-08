"""Exercise the actual Pi extension with a host registry and transport double."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.pi


@pytest.mark.parametrize("role", ["main", "worker", "final-checker", "ordinary"])
def test_pi_tool_ownership_and_registration_gates(tmp_path, role):
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
         str(ROOT / ".pi/extensions/goal-plus.ts"), str(tmp_path)],
        env={**{key: value for key, value in os.environ.items()
                if not key.startswith(("GOAL_PLUS_", "THINKTHREAD_"))},
             "GOAL_PLUS_PI_ROLE": role},
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ownership checks passed" in result.stdout


SCRIPT = r'''
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
const extensionPath = process.argv[1];
const cwd = process.argv[2];
globalThis.calls = [];
globalThis.status = { goal_plus_id: "gp_0001", raw_goal: "fixture", status: "active",
  phase: "goal", goal_revision: 1, control: {state: "active", version: 1},
  active_session: {session_id: "pi-test"} };
const mocks = {
  "@earendil-works/pi-coding-agent": "export const parseArgs = () => ({});",
  "@earendil-works/pi-tui": "export class Box {}; export class Text {};",
  "typebox": "export const Type = {};",
  "./lib/goal-plus-schemas.ts": "export const LooseObject = {}; export const RuntimeToolSchemas = {}; export const RuntimeToolDescriptions = {};",
  "./lib/goal-plus-transport.ts": `
    export const pythonExecutable = "python", runtimeRoot = ".gp";
    export const isRecord = v => v !== null && typeof v === "object";
    export const sourceRoot = ctx => ctx.cwd;
    export const projectModuleInvocation = () => { throw Error("unexpected process launch"); };
    export const parseJsonObject = JSON.parse;
    export const commandFailure = () => { throw Error("unexpected command failure"); };
    export async function runJsonCli(pi, ctx, name, args) {
      globalThis.calls.push({name, args});
      return {details: name === "goal_plus_gate"
        ? {decision: "block", reason: "fixture phase gate"} : globalThis.status};
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
  registerCommand(name, command) {commands.set(name, command);},
  on(name, handler) {events.set(name, handler);},
  sendMessage() {}, appendEntry() {}, getThinkingLevel() {return "medium";},
};
const ctx = {cwd, mode: "interactive", hasUI: false, isIdle: () => true,
  isProjectTrusted: () => true,
  sessionManager: {getEntries: () => [], isPersisted: () => false, getSessionId: () => "pi-test"},
  model: {provider: "fixture", id: "fixture-model"},
  ui: {notify() {}, setStatus() {}, setWidget() {}},
};
register(pi);
const event = name => ({type: "tool_call", toolName: name, toolCallId: "call", input: {}});
const call = name => tools.get(name).execute("call", {}, undefined, undefined, ctx);
if (process.env.GOAL_PLUS_PI_ROLE !== "main") {
  const foreign = name => sources.set(name, {path: cwd + "/foreign.ts", source: "package"});
  if (process.env.GOAL_PLUS_PI_ROLE === "worker") {
    foreign("search_get_agent_context");
    assert.equal((await events.get("tool_call")(event("search_get_agent_context"), ctx)).block, true);
    globalThis.status = {agent_session_id: "worker-session", candidate_task: {workspace: cwd}};
    await call("search_get_agent_context"); // Only the registered callback activates context.
    assert(tools.has("search_run_verifier")); // Dynamic tools also use the registration gate.
    for (const name of ["search_prepare_recovery", "search_emails", "write"]) {
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
    for (const name of ["search_create", "search_prepare_recovery", "goal_plus_submit_final_check"]) {
      foreign(name);
      assert.equal(await events.get("tool_call")(event(name), ctx), undefined);
    }
  }
  console.log("ownership checks passed");
  process.exit(0);
}
const readOnly = new Set(["search_recovery_status", "goal_plus_status",
  "goal_plus_monitor_snapshot", "goal_plus_list_models", "search_status",
  "search_list_history", "search_list_iterations", "search_get_agent_observability"]);
const ownMutations = [...tools.keys()].filter(name => !readOnly.has(name));
for (const name of ownMutations) {
  await assert.rejects(() => call(name), /GoalPlusEntrypointRequired/);
}
assert.equal(calls.length, 0); // Rejection precedes model/runtime/process work.

// Both ordinary prefixed tools and exact-name overrides are foreign registrations.
for (const name of ["search_emails", "goal_plus_external", "pi_search_pool_external",
    "thinkthread_search_pool_external", "search_create", "bash", "edit"]) {
  sources.set(name, {path: cwd + "/another-extension.ts", source: "package"});
  assert.equal(await events.get("tool_call")(event(name), ctx), undefined);
}
// Missing source metadata never lends another tool ownership. Our registration
// closure still refuses direct execution without authorization.
sources.delete("search_create");
assert.equal(await events.get("tool_call")(event("search_create"), ctx), undefined);
await assert.rejects(() => call("search_create"), /GoalPlusEntrypointRequired/);
await call("goal_plus_status");
assert.equal(calls.at(-1).name, "goal_plus_status");

await commands.get("goal-plus").handler("fixture", ctx);
assert(calls.some(c => c.name === "goal_plus_host_start"));
calls.length = 0;
// A live Goal must not intercept an effective foreign override either.
for (const name of ["search_emails", "search_create", "bash", "edit"]) {
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
