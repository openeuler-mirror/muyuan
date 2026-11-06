from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from bate.codex.server import CatalogService, load_settings, run_server, schema_fingerprint
from bate.io import load_specs
from bate.naming import assign_short_names, template_fingerprint
from bate.runtime import MetaToolExecutionError

ROOT = Path(__file__).resolve().parents[1]
SPECS = assign_short_names(load_specs(ROOT / "tests/fixtures/branched_catalog_before_migration.json"))


def configuration(tmp_path, specs=SPECS):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"meta_tools": [s.to_dict() for s in specs]}))
    config = {
        "catalog": "catalog.json",
        "approved_meta_tools": {s.name: template_fingerprint(s) for s in specs},
        "servers": {"demo": {"command": sys.executable,
                              "args": [str(ROOT / "integrations/codex/demo_server.py")]}},
        "bindings": {step.tool: {"server": "demo", "tool": step.tool, "read_only": True}
                     for spec in specs for step in spec.steps},
        "timeout_seconds": 10,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path, config


def fake_service(tmp_path, spec=SPECS[0]):
    path, _ = configuration(tmp_path, [spec])
    service = CatalogService(load_settings(path), [spec])
    service.tools["demo"] = {
        step.tool: Tool(name=step.tool, inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
        for step in spec.steps}
    service.admit()
    return service


@pytest.mark.parametrize("failure", ["unapproved", "fingerprint", "unknown", "mutator", "upstream_mutator", "schema", "guard", "warnings"])
def test_fail_closed_admission(tmp_path, failure):
    spec = SPECS[0]
    if failure == "guard":
        spec = replace(spec, metadata={**spec.metadata, "execution_guard": "confirm_each_run"})
    if failure == "warnings":
        spec = replace(spec, metadata={**spec.metadata, "compiler_warnings": ["bad"]})
    service = fake_service(tmp_path, spec)
    first = spec.steps[0].tool
    if failure == "unapproved":
        service.settings["approved_meta_tools"] = {}
    if failure == "fingerprint":
        service.settings["approved_meta_tools"][spec.name] = "wrong"
    if failure == "unknown":
        del service.settings["bindings"][first]
    if failure == "mutator":
        service.settings["bindings"][first]["read_only"] = False
    if failure == "upstream_mutator":
        service.tools["demo"][first].annotations.readOnlyHint = False
    if failure == "schema":
        service.settings["bindings"][first]["input_schema_sha256"] = "wrong"
    service.admit()
    assert not service.active
    assert service.rejected[0]["reasons"]


def test_schema_fingerprint_is_order_independent():
    assert schema_fingerprint({"a": 1, "b": 2}) == schema_fingerprint({"b": 2, "a": 1})


def test_schema_drift_blocks_before_any_primitive(tmp_path):
    service = fake_service(tmp_path)

    class Session:
        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=[tool.model_copy(update={"inputSchema": {"type": "object", "properties": {"changed": {}}}})
                                         for tool in service.tools["demo"].values()])

        async def call_tool(self, *args):
            raise AssertionError("must not execute after schema drift")

    service.sessions["demo"] = Session()
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(service.execute(SPECS[0].name, {"email": "test@example.test"}))


def test_primitive_error_stops_without_retry(tmp_path):
    service = fake_service(tmp_path)
    calls = []

    class Session:
        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=list(service.tools["demo"].values()))

        async def call_tool(self, name, arguments):
            calls.append(name)
            if len(calls) == 2:
                return CallToolResult(isError=True, content=[TextContent(type="text", text="failed")])
            return CallToolResult(content=[], structuredContent={"customer_id": "live"})

    service.sessions["demo"] = Session()
    with pytest.raises(MetaToolExecutionError) as caught:
        asyncio.run(service.execute(SPECS[0].name, {"email": "test@example.test"}))
    assert len(calls) == 2
    assert len(caught.value.completed_steps) == 1


def test_config_does_not_allow_implicit_servers(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"catalog": "c.json", "servers": {}, "bindings": {}, "approved_meta_tools": []}))
    with pytest.raises(ValueError, match="approved_meta_tools"):
        load_settings(path)


def test_real_stdio_protocol_and_live_data_flow(tmp_path):
    config_path, _ = configuration(tmp_path)

    async def scenario():
        # The test client plays Codex's MCP role; no LLM/API credentials needed.
        async with AsyncExitStack() as stack:
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=sys.executable,
                args=["-m", "bate.codex", "serve", "--config", str(config_path)],
                env={"PYTHONPATH": str(ROOT / "src")}, cwd=str(tmp_path),
            )))
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {"bate_catalog", "meta_tool_search", "meta_tool_run", *(s.name for s in SPECS)}
            for spec in SPECS:
                result = await session.call_tool(spec.name, {"email": "unseen@example.test"})
                assert not result.isError
                body = result.structuredContent
                branch = "invoice" if "lookup_invoice" in [s.tool for s in spec.steps] else "order"
                assert body["output"]["record_id"] == f"{branch}:customer:unseen@example.test"
                assert len(body["completed_steps"]) == 3
            invalid = await session.call_tool(SPECS[0].name, {"not_email": "x"})
            assert invalid.isError
            unknown = await session.call_tool("exec_command", {"cmd": "pwd"})
            assert unknown.isError
            catalog = await session.call_tool("bate_catalog", {})
            assert catalog.structuredContent["native_codex_tools_supported"] is False

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_readiness_check_and_review_are_separate(tmp_path):
    path, config = configuration(tmp_path)
    config["approved_meta_tools"] = {}
    path.write_text(json.dumps(config))
    report = asyncio.run(run_server(path, check=True))
    assert report["active"] == []
    assert len(report["rejected"]) == 2
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    before = path.read_text()
    review = subprocess.run([sys.executable, "-m", "bate.codex", "review", "--catalog", str(tmp_path / "catalog.json")],
                            env=env, capture_output=True, text=True, timeout=10)
    assert review.returncode == 0, review.stderr
    assert json.loads(review.stdout)["review_required"] is True
    assert path.read_text() == before
