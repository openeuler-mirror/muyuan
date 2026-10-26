from __future__ import annotations

from typing import Any


async def tools_by_name(mcp: Any) -> dict[str, Any]:
    """Return registered tools across the FastMCP 2.x and 3.x APIs."""

    list_tools = getattr(mcp, "list_tools", None)
    if callable(list_tools):
        return {tool.name: tool for tool in await list_tools()}
    return dict(await mcp.get_tools())
