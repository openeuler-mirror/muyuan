"""Probe the native TT Codex extension before enabling Codex Search admission."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib

import pytest

from tests.test_install import _load_installer


pytestmark = [pytest.mark.st, pytest.mark.st_codex, pytest.mark.thinkthread_linux]


@pytest.mark.parametrize("first_dispatch", ["initial_message", "separate_wake"])
def test_native_codex_child_uses_mcp_across_two_retained_turns(thinkthread_root, first_dispatch):
    workspace, state_root = thinkthread_root
    for executable in ("tt", "codex", "node"):
        if not shutil.which(executable):
            pytest.skip(f"missing {executable}")
    installer = _load_installer()
    installed = tomllib.loads(subprocess.run(
        ["tt", "profile", "show", "goal-plus-pi"], check=True, capture_output=True, text=True,
    ).stdout)
    environment = installed["environment"]["root"]
    package = Path(environment["GOAL_PLUS_SOURCE_PATH"])
    python = Path(environment["GOAL_PLUS_PYTHON"])
    installer.managed_runtime.verify(package)
    config = replace(installer._plan_thinkthread_codex_config(package), directory=state_root.parent / "codex-config")
    content = tomllib.loads(config.content)
    content["model"] = os.environ.get("ST_CODEX_MODEL", "gpt-5.6-terra")
    content["model_reasoning_effort"] = "low"
    # The Root starts this MCP during discovery; no prompt is appended to Child invocations.
    probe = Path(__file__).with_name("codex_bridge_probe.py").resolve()
    content["mcp_servers"]["bridge-probe"] = {
        **content["mcp_servers"]["goal-plus"],
        "args": ["-I", str(probe), first_dispatch],
    }
    config = replace(config, content="\n".join(installer._render_toml_table(content)) + "\n")
    installer._install_thinkthread_codex_config(config, dry_run=False)
    xdg_config = state_root.parent / "xdg-config"
    profile_path = xdg_config / "thinkthread/profiles/goal-plus-codex-probe.toml"
    profile_path.parent.mkdir(parents=True)
    profile = installer._preflight_thinkthread_profile(
        "tt", native_agent="codex", codex_config=config,
        profile_path=profile_path, package_root=package, runtime_python=python,
        sdk_entry=Path(environment["GOAL_PLUS_AGENT_POSIX_SDK_ENTRY"]),
    )
    payload = tomllib.loads(profile.content)
    payload["environment"]["read"].append(str(probe))
    profile.path.write_text(installer._render_thinkthread_profile(payload))
    print(f"\nCodex TT bridge probe: {workspace}; build={installer.managed_runtime.read_receipt(package)['build']}", flush=True)
    started = subprocess.run([
        "tt", "run", "--profile", "goal-plus-codex-probe", "--fs", str(workspace),
        "--state-root", str(state_root), "--detach",
    ], env={**os.environ, "XDG_CONFIG_HOME": str(xdg_config)},
        check=False, capture_output=True, text=True, timeout=60)
    assert started.returncode == 0, started.stderr or started.stdout
    report_path = workspace / ".codex-bridge-report.json"
    deadline = time.monotonic() + 600
    report = {}
    next_status_check = 0.0
    while time.monotonic() < deadline:
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text())
            except json.JSONDecodeError:
                continue
            if report.get("stage") == "finished":
                break
        if time.monotonic() >= next_status_check:
            status = json.loads(subprocess.run(
                ["tt", "ps", "--json", "--state-root", str(state_root)],
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout)
            roots = [item for item in status["thinkthreads"] if item["parentId"] is None]
            assert roots and all(item["state"] != "stopped" for item in roots), status
            next_status_check = time.monotonic() + 5
        time.sleep(0.5)
    assert report.get("stage") == "finished", report or f"Root did not start probe: {state_root}"
    assert report["ok"], report
    assert report["cleanup_state"] == "closed"
    assert report["root_id"] != report["child_id"]
    assert len({item["message_id"] for item in report["rounds"]}) == 2
    assert all(item["requests"] for item in report["rounds"])
