import json

from awo.io import load_traces
from awo.models import ToolCall, Trace


def test_tool_call_accepts_openai_function_shape():
    call = ToolCall.from_dict(
        {
            "id": "call-1",
            "function": {
                "name": "search",
                "arguments": json.dumps({"query": "paper"}),
            },
        }
    )
    assert call.name == "search"
    assert call.arguments == {"query": "paper"}
    assert call.call_id == "call-1"


def test_trace_extracts_calls_and_results_from_messages():
    trace = Trace.from_dict(
        {
            "id": "conversation",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"id": 7}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "c1",
                    "content": '{"value": "ok"}',
                },
            ],
        }
    )
    assert len(trace.calls) == 1
    assert trace.calls[0].result == {"value": "ok"}


def test_load_jsonl(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(
        '{"trace_id":"a","calls":[{"name":"x","arguments":{}}]}\n',
        encoding="utf-8",
    )
    traces = load_traces(path)
    assert [trace.trace_id for trace in traces] == ["a"]
