"""Project installation and live session controls, with no model requests."""
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys
import tomllib

import pytest

from bate.codex.files import read_range, MAX_OUTPUT_BYTES
from bate.codex.install import install
from bate.codex.server import CatalogService, load_settings


def test_install_preserves_host_settings_and_is_idempotent(tmp_path):
    host = tmp_path / ".codex/config.toml"
    host.parent.mkdir()
    host.write_text('# preserve me\nmodel = "existing-model"\n[mcp_servers.existing]\ncommand = "existing"\n')
    market = tmp_path / ".agents/plugins/marketplace.json"
    market.parent.mkdir(parents=True)
    other = {"name": "other", "source": {"source": "local", "path": "./other"}}
    market.write_text(json.dumps({"name": "team", "plugins": [other]}))
    result = install(tmp_path)
    generated = tmp_path / ".bate-codex/config.json"
    first = host.read_text(), market.read_text(), generated.read_text()
    install(tmp_path)
    assert first == (host.read_text(), market.read_text(), generated.read_text())
    assert "# preserve me" in host.read_text()
    parsed = tomllib.loads(host.read_text())
    assert parsed["model"] == "existing-model"
    assert parsed["mcp_servers"]["existing"]["command"] == "existing"
    assert parsed["plugins"]["meta-tool@team"]["enabled"]
    assert parsed["marketplaces"]["team"] == {"source_type": "local", "source": str(tmp_path)}
    assert json.loads(market.read_text())["plugins"][0] == other
    config = load_settings(result["config"])
    assert config["session_control"] and not config["start_enabled"]
    assert config["online"]["trace_files"] == []
    plugin = tmp_path / ".bate-codex/plugin"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    assert (plugin / manifest["skills"] / "meta-tool/SKILL.md").is_file()
    assert json.loads((plugin / manifest["mcpServers"]).read_text())["mcpServers"]["bate"]["args"][-1] == result["config"]


def test_install_conflicts_leave_existing_configuration_unchanged(tmp_path):
    host = tmp_path / ".codex/config.toml"
    host.parent.mkdir()
    host.write_text('[mcp_servers.bate]\ncommand = "keep-me"\n')
    with pytest.raises(ValueError, match="already registers"):
        install(tmp_path)
    assert host.read_text() == '[mcp_servers.bate]\ncommand = "keep-me"\n'
    assert not (tmp_path / ".agents/plugins/marketplace.json").exists()
    host.write_text('model = "unterminated\n')
    with pytest.raises(ValueError):
        install(tmp_path)
    assert not (tmp_path / ".bate-codex/config.json").exists()


def test_default_installations_use_distinct_project_cache_identities(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert install(a)["plugin"] != install(b)["plugin"]


def test_install_does_not_follow_host_configuration_symlinks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "outside"
    external.mkdir()
    (project / ".codex").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        install(project)
    assert list(external.iterdir()) == []


def test_project_reader_bounds_paths_files_and_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "a.py"
    source.write_text("first\nsecond\nthird\n")
    assert read_range(project, "a.py", 2, 1)["text"] == "second\n"
    assert read_range(project, str(source), 2, 1)["text"] == "second\n"
    outside = tmp_path / "outside"
    outside.write_text("private")
    (project / "link").symlink_to(outside)
    (project / "dir-link").symlink_to(tmp_path, target_is_directory=True)
    (project / ".env").write_text("private")
    for path in ("../outside", str(outside), "link", "dir-link/outside", ".env"):
        with pytest.raises((OSError, ValueError)):
            read_range(project, path, 1, 3)
    for offset, limit in ((0, 1), (1, 301), (True, 1)):
        with pytest.raises(ValueError):
            read_range(project, "a.py", offset, limit)
    source.write_text("文" * MAX_OUTPUT_BYTES)
    result = read_range(project, "a.py", 1, 1)
    assert result["truncated"] and len(result["text"].encode()) <= MAX_OUTPUT_BYTES
    source.write_bytes(b"\0binary")
    with pytest.raises(ValueError):
        read_range(project, "a.py", 1, 1)


async def connect(stack, config, cwd):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    source = Path(__file__).resolve().parents[1] / "src"
    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
        command=sys.executable, args=["-m", "bate.codex", "serve", "--config", str(config)],
        env={"PYTHONPATH": str(source)}, cwd=str(cwd))))
    session = await stack.enter_async_context(ClientSession(*streams))
    await session.initialize()
    return session


def test_installed_plugin_controls_real_batch_reads_in_one_connection(tmp_path):
    pytest.importorskip("mcp")
    result = install(tmp_path)
    (tmp_path / "a.py").write_text("alpha\n")
    (tmp_path / "b.py").write_text("beta\n")
    config = Path(result["config"])
    # Source checkout test: the isolated installed-wheel probe covers deployment.
    settings = json.loads(config.read_text())
    settings["servers"]["project"]["env"] = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    config.write_text(json.dumps(settings))
    ranges = [{"path": p, "offset": 1, "limit": 1} for p in ("a.py", "b.py")]
    async def scenario():
        async with AsyncExitStack() as stack:
            session = await connect(stack, config, tmp_path)
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert "meta_tool_control" in tools and "meta_tool_read_batch" in tools
            assert tools["meta_tool_control"].annotations.readOnlyHint is False
            async def control(action):
                response = await session.call_tool("meta_tool_control", {"action": action})
                assert not response.isError
                return response.structuredContent
            assert not (await control("status"))["enabled"]
            assert (await session.call_tool("meta_tool_read_batch", {"ranges": ranges})).isError
            failed = await session.call_tool("meta_tool_control", {"action": "online"})
            assert failed.isError and "online_sources_not_configured" in failed.content[0].text
            assert not (await control("status"))["enabled"]
            assert (await control("on"))["enabled"]
            batch = await session.call_tool("meta_tool_read_batch", {"ranges": ranges})
            assert not batch.isError
            assert [r["result"]["text"] for r in batch.structuredContent["output"]] == ["alpha\n", "beta\n"]
            found = (await session.call_tool("meta_tool_search", {})).structuredContent["meta_tools"]
            assert len(found) == 3
            await control("batch_off")
            assert (await session.call_tool("meta_tool_read_batch", {"ranges": ranges})).isError
            assert {t.name for t in (await session.list_tools()).tools} == set(tools)
            await control("batch_on")
            await control("off")
            assert (await session.call_tool("meta_tool_search", {})).structuredContent["meta_tools"] == []
            assert (await session.call_tool("meta_tool_read_batch", {"ranges": ranges})).isError
        # Switches are not silently made persistent across connections.
        async with AsyncExitStack() as stack:
            session = await connect(stack, config, tmp_path)
            assert not (await session.call_tool("meta_tool_control", {"action": "status"})).structuredContent["enabled"]
    asyncio.run(scenario())


def test_online_static_off_do_not_refresh_or_clear_the_learned_catalog(tmp_path):
    from test_codex_online import settings, rows, write_rows, TRACES
    pytest.importorskip("mcp")
    config, _, paths = settings(tmp_path, populate=False)
    raw = json.loads(config.read_text())
    raw.update(mode="static", session_control=True, start_enabled=False,
               catalog=str(tmp_path / "empty.json"))
    (tmp_path / "empty.json").write_text('{"meta_tools": []}')
    config.write_text(json.dumps(raw))
    async def scenario():
        async with AsyncExitStack() as stack:
            session = await connect(stack, config, tmp_path)
            async def control(action):
                response = await session.call_tool("meta_tool_control", {"action": action})
                assert not response.isError
                return response.structuredContent
            await control("online")
            for path, trace in zip(paths, TRACES):
                write_rows(path, rows(trace))
            found = await session.call_tool("meta_tool_search", {})
            spec = found.structuredContent["meta_tools"][0]
            assert len(found.structuredContent["meta_tools"]) == 2
            frozen = await control("static")
            paths[0].write_text("broken\n")
            await session.call_tool("bate_catalog", {})
            assert (await control("status"))["online"] == frozen["online"]
            run = {"name": spec["name"], "fingerprint": spec["fingerprint"], "inputs": {"email": "a@test"}}
            assert not (await session.call_tool("meta_tool_run", run)).isError
            await control("off")
            assert (await session.call_tool("meta_tool_run", run)).isError
            await session.call_tool("bate_catalog", {})
            assert (await control("status"))["online"] == frozen["online"]
            await control("on")
            assert (await control("status"))["mode"] == "static"
            assert not (await session.call_tool("meta_tool_run", run)).isError
    asyncio.run(scenario())


def test_off_waits_for_current_execution_and_blocks_queued_work():
    pytest.importorskip("mcp")
    from mcp.types import CallToolResult, ListToolsResult, Tool, ToolAnnotations
    service = CatalogService({"mode": "static", "session_control": True,
        "timeout_seconds": 10, "approved_meta_tools": {},
        "bindings": {"read": {"server": "files", "tool": "read", "read_only": True}}}, [])
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        tool = Tool(name="read", inputSchema={"type": "object"},
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
        class Session:
            async def list_tools(self, cursor=None):
                return ListToolsResult(tools=[tool])
            async def call_tool(self, name, arguments):
                calls.append(arguments)
                entered.set()
                await release.wait()
                return CallToolResult(content=[], structuredContent={"text": "ok"})
        service.sessions["files"] = Session()
        service.tools["files"] = {"read": tool}
        service.rebuild()
        ranges = [{"path": "a", "offset": 1, "limit": 1}] * 2
        running = asyncio.create_task(service.read_batch(ranges))
        await entered.wait()
        off = asyncio.create_task(service.control("off"))
        await asyncio.sleep(0)
        queued = asyncio.create_task(service.read_batch(ranges))
        await asyncio.sleep(0)
        assert not off.done() and service.enabled
        release.set()
        await running
        assert not (await off)["enabled"]
        with pytest.raises(ValueError, match="disabled"):
            await queued
        assert len(calls) == 2
    asyncio.run(scenario())
