"""Codex Child MCP transport to its authenticated Parent's candidate tools."""

from typing import Any
from threading import Lock
from pathlib import Path
import os

from goal_plus.paths import DEFAULT_RUNTIME_ROOT, host_assets_root
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient, validate_thinkthread_root

# The wire request allows 120 seconds; reserve ten more for SDK startup and binding.
WORKER_BRIDGE_TIMEOUT_SECONDS = 130.0


def create_thinkthread_main_mcp(root_dir: str | Path = DEFAULT_RUNTIME_ROOT):
    from goal_plus.server import create_host_mcp

    configured = os.environ.get("GOAL_PLUS_ROOT")
    if not configured or not Path(configured).is_absolute():
        raise PermissionError("ThinkThread Main requires its host-declared absolute runtime root")
    root = Path(configured).resolve()
    if root.is_relative_to(Path.cwd().resolve()):
        raise PermissionError("ThinkThread runtime root must be outside the attached Root tree")
    if str(root_dir) != DEFAULT_RUNTIME_ROOT and Path(root_dir).resolve() != root:
        raise PermissionError("ThinkThread MCP root differs from its host-declared runtime root")
    validate_thinkthread_root(AgentPosixSdkClient(), agent_harness="codex")
    return create_host_mcp(root, agent_harness="codex", caller_role="main")


def create_thinkthread_candidate_mcp():
    from goal_plus.server import create_mcp, native_mcp_caller

    transport = AgentPosixSdkClient(
        bridge_path=host_assets_root() / "assets/codex/thinkthread-worker-bridge.mts",
    )
    binding = transport.invoke("binding")
    session_id = binding.get("agentSessionId")
    if not isinstance(session_id, str) or not session_id:
        raise PermissionError("ThinkThread Child has no candidate session binding")
    native_thread: str | None = None
    identity_lock = Lock()

    def call(name: str, arguments: dict[str, Any]):
        nonlocal native_thread
        caller = native_mcp_caller()
        with identity_lock:
            if native_thread is not None and caller != native_thread:
                raise PermissionError("ThinkThread MCP native thread changed")
            native_thread = caller
        return transport.invoke("call", {
            "agent_session_id": session_id, "name": name, "arguments": arguments,
        }, timeout_seconds=WORKER_BRIDGE_TIMEOUT_SECONDS)

    return create_mcp(caller_role="candidate", caller_agent_session_id=session_id,
                      candidate_tool_call=call)
