"""Opt-in native installer checks in isolated host configuration directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.st


def test_native_install_reinstall_uninstall(tmp_path):
    for binary in ("codex", "pi", "node", "uv"):
        if not shutil.which(binary):
            pytest.skip(f"{binary} is required")
    agent = tmp_path / "pi"
    agent.mkdir()
    (tmp_path / "codex").mkdir()
    settings = agent / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "packages": []}))
    env = {**os.environ, "CODEX_HOME": str(tmp_path / "codex"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
           "GOAL_PLUS_INSTALL_STATE_DIR": str(tmp_path / "state"),
           "PI_CODING_AGENT_DIR": str(agent), "GOAL_PLUS_INSTALL_HOME": str(tmp_path / "managed")}
    def run(args, *, input=None, environment=None, cwd=ROOT):
        __tracebackhide__ = True
        result = subprocess.run(args, cwd=cwd, env=environment or env, input=input,
                                capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run([str(ROOT / "install.sh"), "--codex", "--pi"])
    bindings = tmp_path / "managed/bindings"
    packages = {json.loads(p.read_text())["package"] for p in bindings.glob("*.json")}
    assert len(packages) == 1
    package = Path(packages.pop())
    manifest = json.loads((package / "package.json").read_text())
    assert manifest["pi"]["extensions"] == ["assets/pi/extensions/goal-plus.ts"]
    assert not (package / ".pi").exists()
    inventory = json.loads(run(["codex", "plugin", "list", "--json"]))
    assert len([p for p in inventory["installed"] if p["pluginId"] == "goal-plus@goal-plus"]) == 1
    python = str(package.parent / "venv/bin/python")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    for name in ("python", "python3"):
        executable = foreign / name
        executable.write_text("#!/bin/sh\nexit 86\n")
        executable.chmod(0o755)
    for name in ("goal_plus", "json"):
        (foreign / f"{name}.py").write_text("raise RuntimeError('foreign module loaded')\n")
    poisoned = {**env, "PATH": str(foreign) + os.pathsep + env["PATH"],
                "PYTHONHOME": str(foreign), "PYTHONPATH": str(foreign),
                "GOAL_PLUS_PYTHON": str(foreign / "python")}
    run([python, "-I", "-c", MCP_CHECK, str(package)], environment=poisoned, cwd=foreign)
    # The dev/test interpreter (3.14) defers annotations (PEP 649), so an
    # import removed while still referenced by a def-signature annotation
    # passes the unit suite yet breaks the managed 3.12 runtime at class
    # creation. Importing every module inside the installed venv is the
    # guard for exactly that class of regression.
    run([python, "-I", "-c", IMPORT_ALL_CHECK], environment=poisoned, cwd=foreign)
    pi = str(Path(shutil.which("pi")).resolve())
    run(["node", "--input-type=module", "-e", PI_LOAD_CHECK, str(package), pi, python],
        environment=poisoned, cwd=foreign)

    before = settings.read_bytes()
    declined = run([str(ROOT / "install.sh"), "--codex", "--pi"], input="n\n")
    assert "cancelled" in declined
    assert settings.read_bytes() == before
    confirmed = run([str(ROOT / "install.sh"), "--codex", "--pi"], input="y\n")
    assert "plugin remove goal-plus@goal-plus" in confirmed
    assert "Pi installation is ready" in confirmed
    run([str(ROOT / "install.sh"), "--codex", "--pi", "--yes"])

    run([str(ROOT / "uninstall.sh"), "--codex"])
    assert package.exists()
    assert list(bindings.glob("pi-*.json"))
    assert not list(bindings.glob("codex-*.json"))
    assert not json.loads(run(["codex", "plugin", "list", "--json"]))["installed"]
    stale_build = "stale-unreferenced-release"
    stale_package = package.parent / stale_build / "package"
    stale_package.mkdir(parents=True)
    stale_receipt = {**json.loads((package / ".goal-plus-runtime.json").read_text()),
                     "build": stale_build, "package": str(stale_package)}
    (stale_package / ".goal-plus-runtime.json").write_text(json.dumps(stale_receipt))
    run([str(ROOT / "uninstall.sh"), "--unused-runtime"])
    assert package.exists()
    assert not stale_package.parent.exists()
    run([str(ROOT / "uninstall.sh"), "--pi"])
    assert json.loads(settings.read_text())["packages"] == []
    assert json.loads(settings.read_text())["theme"] == "dark"
    assert package.exists()
    assert not list(bindings.glob("*.json"))
    run([str(ROOT / "uninstall.sh"), "--codex", "--pi"])
    run([str(ROOT / "uninstall.sh"), "--unused-runtime"])
    assert not package.exists()
    dev = tmp_path / "pi-dev"
    run([str(ROOT / "install.sh"), "--pi-dev", "--pi-dev-dir", str(dev)])
    run([str(dev / "bin/pi-goal-plus"), "--help"], environment=poisoned, cwd=foreign)
    dev_python = str(dev / "venv/bin/python")
    run(["node", "--input-type=module", "-e", PI_LOAD_CHECK, str(ROOT), pi, dev_python],
        environment={**poisoned, "GOAL_PLUS_PI_DEV_ROOT": str(ROOT), "GOAL_PLUS_PYTHON": dev_python},
        cwd=foreign)
    run([dev_python, "-I", "-c", IMPORT_ALL_CHECK], environment=poisoned, cwd=foreign)
    run([str(ROOT / "uninstall.sh"), "--pi-dev", "--pi-dev-dir", str(dev)])
    assert not dev.exists()


MCP_CHECK = r'''
import asyncio, json, os, sys
from pathlib import Path
from fastmcp import Client
package = Path(sys.argv[1])
server = json.loads((package / '.mcp.json').read_text())['mcpServers']['goal-plus']
async def check():
    config = {k: server[k] for k in ('command', 'args')}
    config['env'] = dict(os.environ)
    async with Client({'mcpServers': {'goal-plus': config}}) as client:
        tools = await client.list_tools()
        assert 'goal_plus_status' in {tool.name for tool in tools}
asyncio.run(check())
'''

IMPORT_ALL_CHECK = r'''
import importlib, pkgutil, sys
import goal_plus
failed = []
for module in pkgutil.walk_packages(goal_plus.__path__, prefix="goal_plus."):
    try:
        importlib.import_module(module.name)
    except Exception as exc:
        failed.append(f"{module.name}: {type(exc).__name__}: {exc}")
assert not failed, "managed runtime modules failed to import:\n" + "\n".join(failed)
print(f"goal_plus imports OK under {sys.version.split()[0]}")
'''

PI_LOAD_CHECK = r'''
import assert from "node:assert/strict";
import { readFileSync, realpathSync } from "node:fs";
import { findPackageJSON } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
const [packageRoot, pi, python] = process.argv.slice(1);
const piPackageJson = findPackageJSON(pathToFileURL(realpathSync(pi)));
assert(piPackageJson, `Could not locate Pi package.json from ${pi}`);
const piManifest = JSON.parse(readFileSync(piPackageJson, "utf8"));
const piEntry = piManifest.exports?.["."]?.import;
assert.equal(typeof piEntry, "string");
const { discoverAndLoadExtensions } = await import(new URL(piEntry, pathToFileURL(piPackageJson)));
const result = await discoverAndLoadExtensions(
  [join(packageRoot, "assets/pi/extensions/goal-plus.ts")],
  process.cwd(),
  join(packageRoot, ".empty-pi-agent"),
);
assert.deepEqual(result.errors, []);
assert.equal(result.extensions.length, 1);
try {
  assert(result.extensions[0].tools.has("goal_plus_status"));
  const transport = await import(pathToFileURL(join(packageRoot, "assets/pi/extensions/lib/goal-plus-transport.ts")));
  const invocation = transport.projectModuleInvocation({cwd: process.cwd()}, "goal-plus-pi-tool", "goal_plus.pi_tool");
  assert.equal(invocation.command, python);
  const tool = spawnSync(invocation.command, [...invocation.argsPrefix, "--help"], {encoding: "utf8"});
  assert.equal(tool.status, 0, tool.stdout + tool.stderr);
  assert.match(tool.stdout, /usage:/);
} finally {
  for (const handler of result.extensions[0].handlers.get("session_shutdown") ?? []) {
    await handler({type: "session_shutdown", reason: "test_finished"}, {cwd: process.cwd()});
  }
}
'''
