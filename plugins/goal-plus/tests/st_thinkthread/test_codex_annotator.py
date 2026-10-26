"""Probe the native TT Codex Evidence Annotation Child without a full Circle run."""

import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
import tomllib

from tests.test_install import _load_installer

pytestmark = [pytest.mark.st, pytest.mark.st_codex, pytest.mark.thinkthread_linux]


def test_native_codex_annotator_child_publishes_annotation(thinkthread_root):
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
    # ST_CODEX_PROFILE overlays a Codex NAME.config.toml layer (provider, model and
    # catalog) for non-default model runs, e.g. ST_CODEX_PROFILE=deepseek.
    codex_profile = os.environ.get("ST_CODEX_PROFILE") or None
    config = replace(
        installer._plan_thinkthread_codex_config(package, profile=codex_profile),
        directory=state_root.parent / "codex-config",
    )
    content = tomllib.loads(config.content)
    if codex_profile is None:
        content["model"] = os.environ.get("ST_CODEX_MODEL", "gpt-5.6-terra")
        content["model_reasoning_effort"] = os.environ.get("ST_CODEX_REASONING_EFFORT", "low")
    # The Root starts this MCP during discovery; it plays the parent-side annotation
    # service for the Child, whose goal-plus MCP runs the real annotator surface.
    probe = Path(__file__).with_name("codex_annotator_probe.py").resolve()
    content["mcp_servers"]["annotator-probe"] = {
        **content["mcp_servers"]["goal-plus"],
        "args": ["-I", str(probe)],
    }
    config = replace(config, content="\n".join(installer._render_toml_table(content)) + "\n")
    installer._install_thinkthread_codex_config(config, dry_run=False)
    xdg_config = state_root.parent / "xdg-config"
    profile_path = xdg_config / "thinkthread/profiles/goal-plus-codex-annotator.toml"
    profile_path.parent.mkdir(parents=True)
    profile = installer._preflight_thinkthread_profile(
        "tt", native_agent="codex", codex_config=config,
        profile_path=profile_path, package_root=package, runtime_python=python,
        sdk_entry=Path(environment["GOAL_PLUS_AGENT_POSIX_SDK_ENTRY"]),
    )
    payload = tomllib.loads(profile.content)
    payload["environment"]["read"].append(str(probe))
    profile.path.write_text(installer._render_thinkthread_profile(payload))
    print(f"\nCodex TT annotator probe: {workspace}; build={installer.managed_runtime.read_receipt(package)['build']}", flush=True)
    started = subprocess.run([
        "tt", "run", "--profile", "goal-plus-codex-annotator", "--fs", str(workspace),
        "--state-root", str(state_root), "--detach",
    ], env={**os.environ, "XDG_CONFIG_HOME": str(xdg_config)},
        check=False, capture_output=True, text=True, timeout=60)
    assert started.returncode == 0, started.stderr or started.stdout
    report_path = workspace / ".codex-annotator-report.json"
    deadline = time.monotonic() + int(os.environ.get("ST_CODEX_ANNOTATOR_TIMEOUT", "900"))
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
    tools = report["tools"]
    assert tools[0] == "goal_plus_thinkthread_annotation_next"
    assert tools[-1] == "goal_plus_thinkthread_annotation_next"
    assert "goal_plus_thinkthread_annotation_submit" in tools
    assert "goal_plus_thinkthread_artifact_view_list" in tools
    assert "goal_plus_thinkthread_artifact_view_diff" in tools
    assert report["view_reads"], report
    annotation = report["annotation"]
    assert annotation["description"].strip()
    assert all(token in annotation["description"] for token in report["view_consumption"].values())
    assert annotation["supplemental_evaluation"]
