"""Exact host registration identities, independent of a tool's action name."""

# These are the server IDs registered by .mcp.json (Codex normalizes hyphens
# in its JavaScript surface). Never discard an arbitrary MCP server namespace.
GOAL_PLUS_MCP_PREFIXES = ("mcp__goal-plus__", "mcp__goal_plus__")


def goal_plus_tool_name(value: str) -> str | None:
    for prefix in GOAL_PLUS_MCP_PREFIXES:
        if value.startswith(prefix):
            action = value[len(prefix):]
            if action and "__" not in action and "." not in action:
                return action
    return None


def native_tool_name(value: str) -> str:
    """Decode only native host namespaces, preserving foreign tool identity."""
    if value.startswith("functions."):
        value = value[len("functions."):]
    if value.startswith("collaboration."):
        return value[len("collaboration."):]
    return value
