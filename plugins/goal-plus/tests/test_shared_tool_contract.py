from __future__ import annotations

import asyncio
from pathlib import Path
import re

import pytest

from goal_plus.server import create_mcp
from tests._fastmcp_helpers import tools_by_name


ROOT = Path(__file__).resolve().parents[1]
PI_EXTENSION = ROOT / ".pi" / "extensions" / "goal-plus.ts"
pytestmark = [pytest.mark.codex, pytest.mark.pi]


def _balanced_region(source: str, start: int) -> tuple[str, int]:
    pairs = {"{": "}", "[": "]", "(": ")"}
    opener = source[start]
    closer = pairs[opener]
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {'"', "'", "`"}:
            quote = char
            index += 1
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return source[start + 1 : index], index + 1
        index += 1
    raise AssertionError(f"unbalanced TypeScript starting at offset {start}")


def _split_top_level(source: str) -> list[str]:
    result: list[str] = []
    start = 0
    depths = {"{": 0, "[": 0, "(": 0}
    closing = {"}": "{", "]": "[", ")": "("}
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char in depths:
            depths[char] += 1
        elif char in closing:
            depths[closing[char]] -= 1
        elif char == "," and not any(depths.values()):
            result.append(source[start:index])
            start = index + 1
        index += 1
    result.append(source[start:])
    return [item.strip() for item in result if item.strip()]


def _named_entries(source: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for item in _split_top_level(source):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", item, re.S)
        assert match, item[:120]
        entries[match.group(1)] = match.group(2).strip()
    return entries


def _pi_tool_contract() -> dict[str, tuple[set[str], set[str]]]:
    source = (PI_EXTENSION.parent / "lib/goal-plus-schemas.ts").read_text(encoding="utf-8")
    marker = "const RuntimeToolSchemas: Record<string, TSchema> ="
    object_start = source.index("{", source.index(marker))
    body, _ = _balanced_region(source, object_start)
    contract: dict[str, tuple[set[str], set[str]]] = {}
    for tool_name, expression in _named_entries(body).items():
        object_call = expression.index("Type.Object(")
        properties_start = expression.index("{", object_call)
        properties_body, _ = _balanced_region(expression, properties_start)
        properties = _named_entries(properties_body)
        fields = set(properties)
        required = {
            name
            for name, value in properties.items()
            if not value.lstrip().startswith("Type.Optional(")
        }
        contract[tool_name] = (fields, required)
    return contract


def _fastmcp_tool_contract(tmp_path: Path) -> dict[str, tuple[set[str], set[str]]]:
    tools = asyncio.run(tools_by_name(create_mcp(tmp_path / ".gp")))
    return {
        name: (
            set(tool.parameters.get("properties", {})),
            set(tool.parameters.get("required", [])),
        )
        for name, tool in tools.items()
    }


def test_pi_typebox_schemas_cover_the_complete_shared_fastmcp_contract(
    tmp_path: Path,
) -> None:
    fastmcp = _fastmcp_tool_contract(tmp_path)
    pi = _pi_tool_contract()
    pi_shared = {
        name: schema
        for name, schema in pi.items()
        if not name.startswith(("pi_", "thinkthread_"))
    }

    # Pi and ThinkThread pool/Artifact View entries are host-local tools, not
    # logical shared-plane APIs, and are deliberately outside this comparison.
    assert set(pi_shared) == set(fastmcp)
    assert {
        name: {
            "fastmcp_fields": sorted(fastmcp[name][0]),
            "pi_fields": sorted(pi_shared[name][0]),
        }
        for name in fastmcp
        if fastmcp[name][0] != pi_shared[name][0]
    } == {}
    assert {
        name: {
            "fastmcp_required": sorted(fastmcp[name][1]),
            "pi_required": sorted(pi_shared[name][1]),
        }
        for name in fastmcp
        if fastmcp[name][1] != pi_shared[name][1]
    } == {}


def test_evidence_annotator_host_is_outside_the_logical_tool_boundary() -> None:
    """The accepted host difference is nested spec data, not a tool-call drift."""
    pi = _pi_tool_contract()

    assert pi["search_freeze_spec"][0] == {
        "goal_plus_id",
        "spec",
        "verifier_artifact_paths",
    }
