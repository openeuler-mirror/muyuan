"""The Pi adapter keeps one real, role-bound MCP server across tool calls."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests._runtime_helpers import make_project, start_host_verified_goal


ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.pi

MAIN_AGENT_SESSION_ID = "pi-main-session"


@pytest.mark.parametrize(
    ("role", "controller_only_closeout"),
    [("main", False), ("main", True), ("worker", False), ("worker", True)],
)
def test_persistent_pi_mcp_connection_and_shutdown(
    tmp_path, role, controller_only_closeout
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Pi MCP adapter")
    if not (ROOT / "assets/pi/node_modules/@modelcontextprotocol/sdk/package.json").is_file():
        pytest.skip("Pi MCP SDK dependencies require npm ci --prefix assets/pi")
    goal = start_host_verified_goal(
        tmp_path / ".gp", make_project(tmp_path),
        session_id=MAIN_AGENT_SESSION_ID, agent_harness="pi",
    )
    result = subprocess.run([
        node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT,
        str(ROOT / "assets/pi/extensions/lib/goal-plus-mcp.ts"), sys.executable, str(tmp_path),
        goal.goal_plus_id, MAIN_AGENT_SESSION_ID,
    ], env={
        **os.environ, "PYTHONPATH": str(ROOT / "src"), "GOAL_PLUS_PI_ROLE": role,
        "GOAL_PLUS_AGENT_SESSION_ID": "bound-candidate",
        "GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT": (
            "1" if controller_only_closeout else "0"
        ),
    }, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


SCRIPT = r'''
import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";
const [path, python, cwd, goalPlusId, nativeSessionId] = process.argv.slice(1);
const {PiMcpConnection} = await import(pathToFileURL(path));
const connection = new PiMcpConnection(python,
  ["-m", "goal_plus.pi_tool", "--root", cwd + "/.gp", "host_mcp_serve"], cwd);
let pid;
try {
  const manifest = await connection.open();
  assert.equal(manifest.server.name, "goal-plus");
  pid = connection.transport.pid;
  assert(pid > 0);
  if (process.env.GOAL_PLUS_PI_ROLE === "worker") {
    assert(!manifest.instructions);
    assert(!manifest.tools.some(tool => tool.name === "goal_plus_search_select"));
    const result = await connection.call("goal_plus_search_get_agent_context", {agent_session_id: "another-candidate"});
    assert.equal(result.details.ok, false);
    assert.match(result.details.error, /GoalPlusSessionMismatch/);
  } else {
    assert.equal(typeof manifest.instructions, "string");
    assert(manifest.instructions.includes("goal_plus_status"));
    const closeoutTools = ["goal_plus_search_select", "goal_plus_search_promote",
      "goal_plus_search_apply_promotion", "goal_plus_record_search_result"];
    for (const name of closeoutTools) {
      assert.equal(manifest.tools.some(tool => tool.name === name),
        process.env.GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT !== "1");
    }
    const first = await connection.call(
      "goal_plus_monitor_snapshot", {goal_plus_id: goalPlusId}, undefined, nativeSessionId);
    const second = await connection.call(
      "goal_plus_monitor_snapshot", {goal_plus_id: goalPlusId}, undefined, nativeSessionId);
    assert(first.details && second.details);
    assert.equal(first.details.ok, true);
    assert.equal(second.details.ok, true);
    assert.equal(connection.transport.pid, pid);
  }
} finally {
  await connection.close();
}
assert.throws(() => process.kill(pid, 0), {code: "ESRCH"});
await assert.rejects(connection.call("goal_plus_monitor_snapshot", {}), /closed/);
'''
