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
    def run(args, *, input=None):
        result = subprocess.run(args, cwd=ROOT, env=env, input=input,
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
    run([python, "-I", "-c", MCP_CHECK, str(package)])
    run(["node", "--input-type=module", "-e", PI_LOAD_CHECK, str(package), str(Path(shutil.which("pi")).resolve())])

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
    run([str(ROOT / "uninstall.sh"), "--pi"])
    assert json.loads(settings.read_text())["packages"] == []
    assert json.loads(settings.read_text())["theme"] == "dark"
    assert package.exists()
    assert not list(bindings.glob("*.json"))
    run([str(ROOT / "uninstall.sh"), "--codex", "--pi"])
    run([str(ROOT / "uninstall.sh"), "--runtime"])
    assert not package.exists()
    dev = tmp_path / "pi-dev"
    run([str(ROOT / "install.sh"), "--pi-dev", "--pi-dev-dir", str(dev)])
    run([str(dev / "bin/pi-goal-plus"), "--help"])
    run([str(ROOT / "uninstall.sh"), "--pi-dev", "--pi-dev-dir", str(dev)])
    assert not dev.exists()


MCP_CHECK = r'''
import asyncio, json, sys
from pathlib import Path
from fastmcp import Client
package = Path(sys.argv[1])
server = json.loads((package / '.mcp.json').read_text())['mcpServers']['goal-plus']
async def check():
    async with Client({'mcpServers': {'goal-plus': {k: server[k] for k in ('command', 'args')}}}) as client:
        tools = await client.list_tools()
        assert 'goal_plus_status' in {tool.name for tool in tools}
asyncio.run(check())
'''

PI_LOAD_CHECK = r'''
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
const [packageRoot, pi] = process.argv.slice(1);
const { loadExtensions } = await import(pathToFileURL(join(dirname(pi), "core/extensions/loader.js")));
const result = await loadExtensions([join(packageRoot, "assets/pi/extensions/goal-plus.ts")], process.cwd());
assert.deepEqual(result.errors, []);
assert.equal(result.extensions.length, 1);
assert(result.extensions[0].tools.has("goal_plus_status"));
const transport = await import(pathToFileURL(join(packageRoot, "assets/pi/extensions/lib/goal-plus-transport.ts")));
const invocation = transport.projectModuleInvocation({cwd: process.cwd()}, "goal-plus-pi-tool", "goal_plus.pi_tool");
assert.equal(invocation.command, join(dirname(packageRoot), "venv/bin/python"));
'''
