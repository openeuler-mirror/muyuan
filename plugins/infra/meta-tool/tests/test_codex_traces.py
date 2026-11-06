from __future__ import annotations

import json
from pathlib import Path

import pytest

from bate.codex.cli import main
from bate.codex.traces import import_files, import_records, tool_output
from bate.io import load_traces
from bate.cli import main as extract
from bate.io import load_specs
from bate.naming import template_fingerprint


def item(kind, **kwargs):
    return {"type": "response_item", "payload": {"type": kind, **kwargs}}


def pair(call_id, name="lookup", arguments=None, output=None):
    return [item("function_call", call_id=call_id, name=name,
                 arguments=json.dumps(arguments or {"key": call_id})),
            item("function_call_output", call_id=call_id,
                 output=json.dumps(output if output is not None else {"id": call_id}))]


def test_rollout_roundtrip_preserves_calls_and_boundaries(tmp_path):
    rows = [{"type": "session_meta", "payload": {"id": "test-session"}},
            item("message", role="user", content=[]),
            *pair("a", arguments={"nested": {"x": 2}}),
            *pair("b", arguments={"id": "a"}),
            item("message", role="user", content=[]), *pair("c")]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    output = tmp_path / "traces.json"
    assert main(["import-traces", "--input", str(path), "--out", str(output)]) == 0
    traces = load_traces(output)
    assert [len(t.calls) for t in traces] == [2, 1]
    assert traces[0].calls[0].arguments == {"nested": {"x": 2}}
    assert traces[0].calls[0].result == {"id": "a"}
    assert [call.call_id for call in traces[0].calls] == ["a", "b"]
    assert [call.turn_id for call in traces[0].calls] == [1, 2]
    assert all(t.success is None for t in traces)
    assert all(t.metadata["session_id"] == "test-session" for t in traces)


@pytest.mark.parametrize("bad", [
    {"isError": True}, {"error": "failed"}, {"exit_code": 1},
    "Process exited with code 2\nFinal output:\nfailed", "Exit code: 137",
])
def test_failures_are_hard_boundaries(bad):
    result = import_records([*pair("a"), *pair("bad", output=bad), *pair("b")], source_id="s")
    assert [[c.call_id for c in t.calls] for t in result.traces] == [["a"], ["b"]]
    assert result.diagnostics["failed_calls"] == 1


def test_missing_result_and_parallel_batch_are_not_linearized():
    a, b = pair("a"), pair("b")
    result = import_records([*pair("before"), a[0], b[0], b[1], a[1], *pair("after")], source_id="s")
    assert [[c.call_id for c in t.calls] for t in result.traces] == [["before"], ["after"]]
    assert result.diagnostics["overlapping_calls"] == 1
    result = import_records([*pair("before"), a[0]], source_id="s")
    assert result.diagnostics["missing_results"] == 1
    assert [c.call_id for t in result.traces for c in t.calls] == ["before"]


@pytest.mark.parametrize("name", ["meta_tool_run", "mcp__bate__mt_12345", "mcp__bate__bate_catalog"])
def test_meta_calls_do_not_inflate_evidence(name):
    result = import_records([*pair("a"), *pair("meta", name=name), *pair("b")], source_id="s")
    assert [[c.call_id for c in t.calls] for t in result.traces] == [["a"], ["b"]]


def test_custom_calls_are_opaque_not_guessed_or_executed():
    code = 'await tools.exec_command({cmd: "echo hello"})'
    result = import_records([
        item("custom_tool_call", call_id="c", name="functions.exec", input=code),
        item("custom_tool_call_output", call_id="c", output="hello"),
    ], source_id="s")
    call = result.traces[0].calls[0]
    assert call.name == "functions.exec"
    assert call.arguments == {"input": code}


def test_bad_arguments_and_unknown_native_calls_break_sequence():
    result = import_records([
        *pair("a"), item("function_call", call_id="bad", name="lookup", arguments="not json"),
        item("function_call_output", call_id="bad", output="x"),
        *pair("b"), item("web_search_call", id="web"), *pair("c"),
    ], source_id="s")
    assert [[c.call_id for c in t.calls] for t in result.traces] == [["a"], ["b"], ["c"]]


def test_exec_json_imports_mcp_but_does_not_invent_shell_arguments():
    def mcp(kind, id, **extra):
        return {"type": kind, "item": {"id": id, "type": "mcp_tool_call",
                "server": "customer", "tool": "lookup", "arguments": {"id": id}, **extra}}
    result = import_records([
        {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
        mcp("item.started", "a"), mcp("item.completed", "a", status="completed",
             result={"content": [{"type": "text", "text": '{"id":"123"}'}]}),
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd"}},
        mcp("item.completed", "b", status="completed", result={"structuredContent": {"id": 2}, "content": []}),
        {"type": "turn.completed"},
    ], source_id="s")
    assert [len(t.calls) for t in result.traces] == [1, 1]
    assert result.traces[0].calls[0].name == "mcp__customer__lookup"
    assert result.traces[0].calls[0].result == {"id": "123"}
    assert result.traces[1].calls[0].result == {"id": 2}
    assert result.diagnostics["lossy_or_unsupported_items"] == 1


def test_mcp_result_representation():
    assert tool_output({"structuredContent": {"a": 1}, "content": []}) == {"a": 1}
    assert tool_output([{"type": "text", "text": "[1,2]"}]) == [1, 2]
    multi = [{"type": "text", "text": "x"}, {"type": "image", "data": "abc"}]
    assert tool_output(multi) == multi


def test_duplicate_inputs_do_not_multiply_support_and_conflicts_fail(tmp_path):
    rows = [{"type": "session_meta", "payload": {"id": "same"}}, *pair("a")]
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    for path in paths:
        path.write_text("\n".join(json.dumps(row) for row in rows))
    result = import_files(paths)
    assert len(result.traces) == 1
    assert result.diagnostics["duplicate_segments_skipped"] == 1
    rows[-1]["payload"]["output"] = '{"different":true}'
    paths[1].write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="Conflicting copies"):
        import_files(paths)


def test_invalid_json_is_not_silently_spliced(tmp_path):
    source = tmp_path / "partial.jsonl"
    source.write_text('{"type":')
    output = tmp_path / "out.json"
    assert main(["import-traces", "--input", str(source), "--out", str(output)]) == 2
    assert not output.exists()
    assert main(["import-traces", "--input", str(source), "--out", str(source)]) == 2
    assert source.read_text() == '{"type":'


def test_codex_import_feeds_branch_aware_not_baseline(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    original = load_traces(fixtures / "branched_traces.json")
    inputs = []
    for trace in original:
        rows = [{"type": "session_meta", "payload": {"id": trace.trace_id}},
                item("message", role="user", content=[])]
        for index, call in enumerate(trace.calls):
            rows.extend(pair(str(index), name=call.name, arguments=call.arguments, output=call.result))
        path = tmp_path / (trace.trace_id + ".jsonl")
        path.write_text("\n".join(json.dumps(row) for row in rows))
        inputs.append(str(path))
    imported = tmp_path / "imported.json"
    assert main(["import-traces", "--input", *inputs, "--out", str(imported)]) == 0
    out = tmp_path / "extracted"
    assert extract(["extract", "--traces", str(imported), "--out", str(out),
                    "--baseline-config", str(fixtures / "branched_config.json"),
                    "--also-raw", "--require-fully-bound", "--no-baseline",
                    "--naming", "stable-short", "--max-len", "4"]) == 0
    actual = load_specs(out / "active_meta_tools.json")
    expected = load_specs(fixtures / "branched_catalog_before_migration.json")
    assert len(actual) == len(expected) == 2
    assert {template_fingerprint(s) for s in actual} == {template_fingerprint(s) for s in expected}
    summary = json.loads((out / "extraction_summary.json").read_text())
    assert summary["extractor"] == "bate-branch-aware"
