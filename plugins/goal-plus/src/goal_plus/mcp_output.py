"""Encode each MCP tool result once at the shared transport boundary."""

from __future__ import annotations

import json
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import TYPE_CHECKING

from mcp.types import TextContent

if TYPE_CHECKING:
    from fastmcp.tools import Tool


def json_tool(function) -> Tool:
    from fastmcp.tools import Tool

    def encode(value) -> TextContent:
        return TextContent(
            type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    @wraps(function)
    def call(*args, **kwargs):
        return encode(function(*args, **kwargs))

    @wraps(function)
    async def call_async(*args, **kwargs):
        return encode(await function(*args, **kwargs))

    wrapped = call_async if iscoroutinefunction(function) else call
    # Resolve annotations in the business callable's namespace before moving
    # its signature to this transport module, including role-bound signatures.
    parameters = signature(function, eval_str=True)
    wrapped.__signature__ = parameters.replace(return_annotation=TextContent)
    wrapped.__annotations__ = {
        name: parameter.annotation for name, parameter in parameters.parameters.items()
        if parameter.annotation is not parameter.empty
    } | {"return": TextContent}
    # An outputSchema requires structuredContent. JSON TextContent is the sole
    # wire representation, including empty arrays, so do not advertise one.
    return Tool.from_function(wrapped, output_schema=None)
