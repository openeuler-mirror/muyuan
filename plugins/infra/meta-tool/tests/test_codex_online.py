"""Codex Online learning, recovery and live MCP publication without a model."""
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys

import pytest

from bate.codex.online import OnlineLearner, completed_traces, read_evidence
from bate.codex.server import CatalogService, load_settings
from bate.io import load_traces, load_specs
from bate.naming import template_fingerprint

ROOT = Path(__file__).resolve().parents[1]
TRACES = load_traces(ROOT / "tests/fixtures/branched_traces.json")
ALLOWED = {c.name for t in TRACES for c in t.calls}


def rows(trace, *, complete=True):
    records = [{"type": "session_meta", "payload": {"id": trace.trace_id}},
               {"type": "event_msg", "payload": {"type": "task_started"}}]
    for index, call in enumerate(trace.calls):
        records.extend([
            {"type": "response_item", "payload": {"type": "function_call", "call_id": str(index),
                "name": call.name, "arguments": json.dumps(call.arguments)}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": str(index),
                "output": json.dumps(call.result)}},
        ])
    if complete:
        records.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    return records


def write_rows(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def settings(tmp_path, *, populate=True, approve=True):
    paths = [tmp_path / f"session-{i}.jsonl" for i in range(len(TRACES))]
    if populate:
        for path, trace in zip(paths, TRACES):
            write_rows(path, rows(trace))
    config = {"mode": "online", "catalog": None, "approved_meta_tools": {},
        "servers": {"demo": {"command": sys.executable,
            "args": [str(ROOT / "integrations/codex/demo_server.py")]}},
        "bindings": {name: {"server": "demo", "tool": name, "read_only": True} for name in ALLOWED},
        "online": {"trace_files": [str(p) for p in paths], "state_dir": str(tmp_path / "state"),
                   "auto_admit_read_only": approve, "max_len": 4}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path, load_settings(path), paths


def test_only_completed_turns_train_and_completion_is_not_task_success():
    pending = rows(TRACES[0], complete=False)
    assert completed_traces(pending) == []
    assert completed_traces(pending + [{"type": "event_msg", "payload": {"type": "task_failed"}}]) == []
    imported = completed_traces(rows(TRACES[0]))
    assert len(imported) == 1 and imported[0].success is None
    assert imported[0].metadata["task_success_verified"] is False
    failed = rows(TRACES[0])
    failed[3]["payload"]["output"] = '{"isError":true}'
    assert completed_traces(failed) == []


def test_partial_tail_is_deferred_and_duplicate_sessions_do_not_add_support(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_rows(a, rows(TRACES[0]))
    b.write_bytes(a.read_bytes() + b'{"type":')
    assert len(read_evidence([a, b], ALLOWED)) == 1
    b.write_bytes(a.read_bytes() + b'{broken}\n')
    with pytest.raises(ValueError):
        read_evidence([a, b], ALLOWED)
    write_rows(b, [{"type": "session_meta", "payload": "invalid"}])
    with pytest.raises(ValueError, match="payloads"):
        read_evidence([b], ALLOWED)


def test_unknown_tools_are_boundaries_not_removable_glue(tmp_path):
    path = tmp_path / "trace"
    records = rows(TRACES[0])
    records[4]["payload"]["name"] = "unknown_side_effect"
    write_rows(path, records)
    assert read_evidence([path], ALLOWED) == []


def test_learning_matches_static_and_restart_does_not_reextract(tmp_path):
    _, config, paths = settings(tmp_path)
    async def scenario():
        learner = OnlineLearner(config)
        try:
            learner.restore()
            assert await learner.refresh(ALLOWED)
            expected = load_specs(ROOT / "tests/fixtures/branched_catalog_before_migration.json")
            assert {template_fingerprint(s) for s in learner.specs} == {template_fingerprint(s) for s in expected}
            assert len(learner.specs) == 2
            assert not await learner.refresh(ALLOWED)
            assert learner.extractions == 1
            version = learner.version
            # Broken new input retains both the active version and checkpoint.
            paths[0].write_text(paths[0].read_text() + 'broken\n')
            assert not await learner.refresh(ALLOWED)
            assert learner.version == version and learner.error == "JSONDecodeError"
            write_rows(paths[0], rows(TRACES[0]))
        finally:
            learner.close()
        restarted = OnlineLearner(config)
        try:
            restarted.restore()
            assert restarted.version == version
            assert not await restarted.refresh(ALLOWED)
            assert restarted.extractions == 0
        finally:
            restarted.close()
    asyncio.run(scenario())


def test_writer_lock_and_failed_publish_leave_checkpoint_retryable(tmp_path, monkeypatch):
    import bate.codex.online as online
    _, config, _ = settings(tmp_path)
    learner = OnlineLearner(config)
    try:
        with pytest.raises(ValueError, match="writer"):
            OnlineLearner(config)
        original = online._atomic_json
        def fail_current(path, value):
            if Path(path).name == "current.json":
                raise OSError("disk failure")
            original(path, value)
        monkeypatch.setattr(online, "_atomic_json", fail_current)
        assert not asyncio.run(learner.refresh(ALLOWED))
        assert learner.digest is None and learner.specs == []
        monkeypatch.setattr(online, "_atomic_json", original)
        assert asyncio.run(learner.refresh(ALLOWED))
        assert len(learner.specs) == 2
    finally:
        learner.close()


@pytest.mark.parametrize("field,value", [("min_sessions", 1), ("min_support", True),
                                        ("poll_seconds", 0), ("auto_admit_read_only", "true")])
def test_online_configuration_cannot_silently_weaken_gates(tmp_path, field, value):
    path, _, _ = settings(tmp_path)
    raw = json.loads(path.read_text())
    raw["online"][field] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_settings(path)


def test_online_needs_explicit_auto_admission_and_session_support(tmp_path):
    pytest.importorskip("mcp")
    from mcp.types import Tool, ToolAnnotations
    _, config, paths = settings(tmp_path, approve=False)
    service = CatalogService(config, [])
    service.tools["demo"] = {name: Tool(name=name, inputSchema={"type": "object"},
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False)) for name in ALLOWED}
    service.learner = OnlineLearner(config)
    try:
        asyncio.run(service.refresh())
        assert len(service.learner.specs) == 2 and not service.active
        config["online"]["auto_admit_read_only"] = True
        service.rebuild()
        assert len(service.active) == 2
        first = next(iter(service.active))
        with pytest.raises(ValueError, match="changed"):
            asyncio.run(service.execute(first, {"email": "x"}, "0" * 64))
    finally:
        service.learner.close()


def test_cold_pool_keeps_support_and_session_gates(tmp_path):
    path, config, paths = settings(tmp_path, populate=False)
    for source, trace in zip(paths[:2], TRACES[:2]):
        write_rows(source, rows(trace))
    learner = OnlineLearner(config)
    try:
        assert not asyncio.run(learner.refresh(ALLOWED))
        assert learner.extractions == 0
        assert learner.specs == []
    finally:
        learner.close()


@pytest.mark.parametrize("mode", ["static", "online"])
@pytest.mark.parametrize("enabled", [True, False])
def test_builtin_batches_default_and_opt_out_use_real_read_binding(tmp_path, mode, enabled):
    pytest.importorskip("mcp")
    from mcp.types import CallToolResult, ListToolsResult, Tool, ToolAnnotations
    _, config, _ = settings(tmp_path)
    config["mode"] = mode
    if not enabled:
        config["batch_enabled"] = False
    config["bindings"] = {"read": {"server": "demo", "tool": "read_range", "read_only": True}}
    service = CatalogService(config, [])
    tool = Tool(name="read_range", inputSchema={"type": "object"},
                annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    service.tools["demo"] = {"read_range": tool}
    service.rebuild()
    calls = []
    class Session:
        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=[tool])
        async def call_tool(self, name, arguments):
            calls.append(arguments)
            return CallToolResult(content=[], structuredContent={"text": arguments["path"] + "\n"})
    service.sessions["demo"] = Session()
    ranges = [{"path": "a.py", "offset": 1, "limit": 3}, {"path": "b.py", "offset": 2, "limit": 4}]
    if not enabled:
        assert service.active == {}
        with pytest.raises(ValueError, match="disabled"):
            asyncio.run(service.read_batch(ranges))
        assert calls == []
        return
    assert len(service.active) == service.describe()["built_in_templates"] == 3
    result = asyncio.run(service.read_batch(ranges))
    assert calls == ranges
    assert [row["result"] for row in result["output"]] == [{"text": "a.py\n"}, {"text": "b.py\n"}]
    calls.clear()
    with pytest.raises(ValueError):
        asyncio.run(service.read_batch([{**ranges[0], "limit": 301}, ranges[1]]))
    assert calls == []


def test_cold_server_learns_and_executes_without_mcp_restart(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    path, _, paths = settings(tmp_path, populate=False)
    async def scenario():
        async with AsyncExitStack() as stack:
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=sys.executable, args=["-m", "bate.codex", "serve", "--config", str(path)],
                env={"PYTHONPATH": str(ROOT / "src")}, cwd=str(tmp_path))))
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            before = (await session.list_tools()).tools
            assert {t.name for t in before} == {"bate_catalog", "meta_tool_search", "meta_tool_run"}
            empty = await session.call_tool("meta_tool_search", {})
            assert empty.structuredContent["meta_tools"] == []
            for source, trace in zip(paths, TRACES):
                write_rows(source, rows(trace))
            found = await session.call_tool("meta_tool_search", {"query": "order"})
            assert not found.isError and len(found.structuredContent["meta_tools"]) == 2
            spec = found.structuredContent["meta_tools"][0]
            result = await session.call_tool("meta_tool_run", {"name": spec["name"],
                "fingerprint": spec["fingerprint"], "inputs": {"email": "fresh@example.test"}})
            assert not result.isError
            assert result.structuredContent["output"]["record_id"] == "order:customer:fresh@example.test"
            assert [t.name for t in (await session.list_tools()).tools] == [t.name for t in before]
            status = await session.call_tool("bate_catalog", {})
            assert status.structuredContent["online"]["learned_templates"] == 2
    asyncio.run(asyncio.wait_for(scenario(), timeout=45))
