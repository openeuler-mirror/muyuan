"""Codex MCP diagnostics work before any Goal or model invocation."""

import asyncio
import json
from pathlib import Path
import sys

from fastmcp import Client
import pytest

from goal_plus.infrastructure import runtime_info
from goal_plus.server import create_host_mcp


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_codex_handshake_and_resource_describe_loaded_runtime(tmp_path, monkeypatch, debug, installed, mode):
    identity = {
        "build": "a" * 64 if installed else None,
        "package": str(tmp_path / "installed-package"),
        "python": sys.executable,
        "capabilities": ["controller_exact_selection"],
    }
    monkeypatch.setattr(runtime_info, "snapshot", lambda: identity)
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1" if debug else "0")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-appear")
    monkeypatch.delenv("GOAL_PLUS_THINKTHREAD_ROLE", raising=False)
    root = tmp_path / ".gp"

    async def exercise():
        mcp = create_host_mcp(root, agent_harness="codex", caller_role="main")
        async with Client(mcp, mode=mode) as client:
            server = client.server_info
            expected_version = f"0.1.0+{identity['build']}" if installed else "0.1.0"
            assert server.version == expected_version
            resources = await client.list_resources()
            assert len(resources) == 1
            resource = resources[0]
            assert str(resource.uri) == runtime_info.RUNTIME_INFO_URI
            assert (identity["build"][:12] if installed else "checkout") in resource.title
            assert "codex_native" in resource.title
            assert f"DEBUG {'on' if debug else 'off'}" in resource.title
            contents = await client.read_resource(runtime_info.RUNTIME_INFO_URI)
            info = json.loads(contents[0].text)
            assert info["installation"] == identity
            assert info["server_version"] == server.version
            assert info["runtime"] == {"root": str(root), "debug_enabled": debug}
            assert info["host"] == {"agent_harness": "codex", "runtime_provider": "direct"}
            assert info["sandbox"]["worker_backend"] == "codex_native"
            assert info["sandbox"]["execution_verified"] is False
            assert "must-not-appear" not in contents[0].text
            assert "goal_plus_runtime_info" not in {tool.name for tool in await client.list_tools()}
    asyncio.run(exercise())
    assert not root.exists()


@pytest.mark.parametrize(("harness", "role"), [("pi", "main"), ("codex", "candidate"), ("codex", "ordinary")])
def test_runtime_resource_is_scoped_to_codex_main(tmp_path, harness, role):
    async def exercise():
        mcp = create_host_mcp(tmp_path / ".gp", agent_harness=harness, caller_role=role,
                              caller_agent_session_id="schema-only" if role == "candidate" else None)
        async with Client(mcp) as client:
            assert await client.list_resources() == []
    asyncio.run(exercise())


def test_codex_thinkthread_resource_reports_provider_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "main")
    info = runtime_info.runtime_info(tmp_path, agent_harness="codex")
    assert info["host"]["runtime_provider"] == "thinkthread"
    assert info["sandbox"]["worker_backend"] == "thinkthread"
    assert info["sandbox"]["worker_policies"] == []
    assert info["sandbox"]["execution_verified"] is False


@pytest.mark.parametrize(("system", "backend", "command"), [
    ("Darwin", "seatbelt", "/usr/bin/sandbox-exec"),
    ("Linux", "bubblewrap", "bwrap"),
    ("Windows", "unsupported", None),
])
@pytest.mark.parametrize("available", [False, True])
def test_pi_backend_information_reports_discovery_not_execution(tmp_path, monkeypatch, system, backend, command, available):
    monkeypatch.delenv("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", raising=False)
    monkeypatch.setattr(runtime_info.platform, "system", lambda: system)
    inspected = []
    def which(value):
        inspected.append(value)
        return "/available/backend" if available else None
    monkeypatch.setattr(runtime_info.shutil, "which", which)
    info = runtime_info.runtime_info(tmp_path / ".gp", agent_harness="pi")
    assert inspected == ([command] if command else [])
    assert info["sandbox"]["worker_backend"] == backend
    assert info["sandbox"]["backend_executable_found"] == bool(command and available)
    assert info["sandbox"]["execution_verified"] is False
    assert info["sandbox"]["main_policy"] == "launcher-process-user"
    assert ("unavailable" in runtime_info.runtime_info_title(info)) == (not command or not available)
    assert not (tmp_path / ".gp").exists()


def test_pi_thinkthread_information_does_not_claim_local_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", "/managed/tt/sdk")
    info = runtime_info.runtime_info(tmp_path, agent_harness="pi")
    assert info["host"]["runtime_provider"] == "thinkthread"
    assert info["sandbox"]["worker_backend"] == "thinkthread"
    assert info["sandbox"]["backend_executable_found"] is None


@pytest.mark.parametrize(("host", "provider", "manager"), [
    ("codex", "direct", "Codex"), ("codex", "thinkthread", "ThinkThread"),
    ("pi", "thinkthread", "ThinkThread"),
])
def test_managed_backend_text_does_not_imply_missing_executable(tmp_path, host, provider, manager):
    info = runtime_info.runtime_info(tmp_path, agent_harness=host, runtime_provider=provider)
    text = runtime_info.runtime_info_text(info)
    assert f"Backend executable: managed by {manager} (not inspected)" in text
    assert "not reported" not in text
    assert info["sandbox"]["execution_verified"] is False


def test_thinkthread_codex_main_resource_uses_real_factory_with_controlled_sdk(tmp_path, monkeypatch):
    from goal_plus.server import create_codex_mcp
    from tests._thinkthread_sdk import IntegratedAgentPosix

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "session-runtime"
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "main")
    monkeypatch.setenv("GOAL_PLUS_ROOT", str(root))
    sdk = IntegratedAgentPosix(workspace, agent="codex")
    monkeypatch.setattr("goal_plus.infrastructure.thinkthread_codex_tools.AgentPosixSdkClient", lambda: sdk)

    async def exercise():
        async with Client(create_codex_mcp()) as client:
            resource = (await client.list_resources())[0]
            assert "thinkthread" in resource.title
            content = await client.read_resource(runtime_info.RUNTIME_INFO_URI)
            info = json.loads(content[0].text)
            assert info["host"] == {"agent_harness": "codex", "runtime_provider": "thinkthread"}
            assert info["runtime"]["root"] == str(root)
            assert info["sandbox"]["execution_verified"] is False

    asyncio.run(exercise())
    assert not root.exists()
    assert not sdk.children
