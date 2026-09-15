"""Host bridge to the shared MCP schema, validation and tool implementation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from typing import Any
from collections.abc import Callable

from fastmcp import Client

from goal_plus.server import create_mcp
from goal_plus.tools import ToolCallerRole
from goal_plus.ports.session import SessionToolDefinition
from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity
from goal_plus.ports.thinkthread_verification import DeferredCandidateAttempt


async def _call(
    root_dir: Path | str, name: str, arguments: dict[str, Any],
    caller_role: ToolCallerRole | None, caller_agent_session_id: str | None,
    caller_final_check: GoalPlusFinalCheckerIdentity | None, native_session_id: str | None,
    candidate_attempt: DeferredCandidateAttempt | None,
) -> Any:
    async with Client(create_mcp(
        root_dir, caller_role=caller_role, caller_agent_session_id=caller_agent_session_id,
        caller_final_check=caller_final_check, candidate_attempt=candidate_attempt,
    )) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False,
                                       meta={"threadId": native_session_id} if native_session_id else None)
        if result.is_error:
            raise RuntimeError("\n".join(item.text for item in result.content if item.type == "text"))
        if len(result.content) != 1 or result.content[0].type != "text":
            raise RuntimeError("Goal Plus MCP returned an unsupported result representation")
        return json.loads(result.content[0].text)


def call_mcp_tool(
    root_dir: Path | str, name: str, arguments: dict[str, Any], *,
    caller_role: ToolCallerRole | None = None, caller_agent_session_id: str | None = None,
    caller_final_check: GoalPlusFinalCheckerIdentity | None = None, native_session_id: str | None = None,
    candidate_attempt: DeferredCandidateAttempt | None = None,
) -> Any:
    def call():
        return asyncio.run(_call(root_dir, name, arguments, caller_role, caller_agent_session_id,
                                 caller_final_check, native_session_id, candidate_attempt))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return call()
    # Synchronous host callbacks may run inside a host's own event loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(call).result()


async def _manifest(
    root_dir: Path | str, caller_role: ToolCallerRole | None,
    caller_agent_session_id: str | None,
    caller_final_check: GoalPlusFinalCheckerIdentity | None,
) -> dict[str, Any]:
    async with Client(create_mcp(
        root_dir, caller_role=caller_role, caller_agent_session_id=caller_agent_session_id,
        caller_final_check=caller_final_check,
    )) as client:
        initialized = client.initialize_result
        # MCP's discover-era negotiation has no InitializeResult (FastMCP 4).
        server = initialized.serverInfo if initialized is not None else client.server_info
        instructions = initialized.instructions if initialized is not None else client.instructions
        return {
            "server": server.model_dump(mode="json"),
            "instructions": instructions,
            "tools": [tool.model_dump(mode="json", by_alias=True) for tool in await client.list_tools()],
        }


def mcp_manifest(
    root_dir: Path | str, *, caller_role: ToolCallerRole | None = None,
    caller_agent_session_id: str | None = None,
    caller_final_check: GoalPlusFinalCheckerIdentity | None = None,
) -> dict[str, Any]:
    return asyncio.run(_manifest(root_dir, caller_role, caller_agent_session_id, caller_final_check))


class McpSessionToolBridge:
    """A trusted host binds a native session to the actual MCP tool contract."""

    def __init__(
        self, root_dir: Path | str, *, caller_role: ToolCallerRole,
        caller_agent_session_id: str | None = None,
        caller_final_check: GoalPlusFinalCheckerIdentity | None = None,
        on_settled: Callable[[], None] | None = None,
    ) -> None:
        self.root_dir = root_dir
        self.caller_role = caller_role
        self.caller_agent_session_id = caller_agent_session_id
        self.caller_final_check = caller_final_check
        self.on_settled = on_settled
        self.native_session_id: str | None = None
        manifest = mcp_manifest(
            root_dir, caller_role=caller_role, caller_agent_session_id=caller_agent_session_id,
            caller_final_check=caller_final_check,
        )
        self.definitions = tuple(SessionToolDefinition(
            tool["name"], tool.get("description") or tool["name"], tool["inputSchema"],
        ) for tool in manifest["tools"])

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = call_mcp_tool(
            self.root_dir, name, arguments, caller_role=self.caller_role,
            caller_agent_session_id=self.caller_agent_session_id,
            caller_final_check=self.caller_final_check, native_session_id=self.native_session_id,
        )
        if name == "goal_plus_search_run_verifier" and self.on_settled is not None:
            self.on_settled()
        return result
