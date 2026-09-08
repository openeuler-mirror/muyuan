"""Exercise Pi's actual package-relative launch routing without a model."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.pi


@pytest.mark.parametrize("mode", ["managed", "unprepared", "dev"])
def test_pi_transport_binds_package_not_workspace_or_shell(tmp_path, mode):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Pi transport test")
    package = tmp_path / "package"
    transport = package / ".pi/extensions/lib/goal-plus-transport.ts"
    transport.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / ".pi/extensions/lib/goal-plus-transport.ts", transport)
    if mode == "managed":
        (package / ".goal-plus-runtime.json").write_text(json.dumps({
            "schema": "goal-plus.managed-runtime.v1", "python": "/managed/bin/python", "build": "fixture-build"}))
    env = {**os.environ, "GOAL_PLUS_SOURCE_PATH": "/wrong/source", "GOAL_PLUS_PYTHON": "/wrong/python",
           "GOAL_PLUS_PI_DEV_ROOT": str(package) if mode == "dev" else "/wrong/dev"}
    result = subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", SCRIPT,
                             str(transport), str(package), mode], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


SCRIPT = r'''
import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";
const [path, root, mode] = process.argv.slice(1);
const transport = await import(pathToFileURL(path));
const ctx = {cwd: "/wrong/workspace"};
assert.equal(transport.sourceRoot(ctx), root);
if (mode === "unprepared") {
  assert.throws(() => transport.projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool"));
} else {
  const invocation = transport.projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
  assert.equal(invocation.argsPrefix[0], "-I");
  if (mode === "managed") {
    assert.equal(invocation.command, "/managed/bin/python");
    assert.deepEqual(invocation.argsPrefix.slice(1), ["-m", "goal_plus.installation", "--expected-build", "fixture-build", "--module", "goal_plus.pi_tool"]);
  } else {
    assert.equal(invocation.command, "/wrong/python"); // Explicit development launcher owns this input.
    assert(invocation.argsPrefix[2].includes(root + "/src"));
    assert(!invocation.argsPrefix[2].includes(ctx.cwd));
  }
}
'''
