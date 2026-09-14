"""Disposable Pi bridge probe using the installed Python MCP client library."""

import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def main():
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "goal_plus.server", "--root", os.environ["GOAL_PLUS_PROBE_ROOT"]],
        env=dict(os.environ),
    )
    async with Client(transport, timeout=30) as client:
        if sys.argv[1] == "list":
            return {
                "server": client.initialize_result.serverInfo.model_dump(mode="json"),
                "tools": [tool.model_dump(mode="json") for tool in await client.list_tools()],
            }
        result = await client.call_tool(
            sys.argv[2], json.loads(sys.argv[3]), raise_on_error=False,
        )
        return {
            "content": [item.model_dump(mode="json") for item in result.content],
            "structuredContent": result.structured_content,
            "isError": result.is_error,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
