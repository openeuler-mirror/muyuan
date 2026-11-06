import json
import re

import pytest

from bate.io import load_config, load_traces, write_json
from bate.models import SemanticType, ToolCall, Trace
from bate.normalization import NormalizationConfig, RegexSubstitution, ToolRule


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


def test_load_default_normalization_config():
    config = load_config(None)
    assert config == NormalizationConfig()
    assert config.argument_matching == "exact"
    assert config.default_semantic_type is SemanticType.MUTATOR
    assert not config.domain_commutativity


def test_normalization_config_round_trip(tmp_path):
    config = NormalizationConfig(
        argument_matching="structural",
        state_context_fields=("environment.url",),
        regex_substitutions=(RegexSubstitution(r"user-\d+", "user-{UID}"),),
        tool_rules=(ToolRule(
            pattern=r"^lookup$", semantic_type=SemanticType.ACCESSOR,
            domain="directory", placeholder_arguments={"query": "{QUERY}"},
            ignore_arguments=("request_id",), argument_equals={"mode": "search"},
        ),),
    )
    path = tmp_path / "normalization.json"
    write_json(path, config.to_dict())
    assert load_config(path) == config


@pytest.mark.parametrize("payload", [[], None, "not an object", 42])
def test_load_config_rejects_non_object(tmp_path, payload):
    path = tmp_path / "invalid.json"
    write_json(path, payload)
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_config(path)


def test_load_config_rejects_invalid_matching_mode(tmp_path):
    path = tmp_path / "invalid.json"
    write_json(path, {"argument_matching": "approximate"})
    with pytest.raises(ValueError, match="argument_matching"):
        load_config(path)


def test_load_config_rejects_invalid_exact_tool_pattern(tmp_path):
    path = tmp_path / "invalid.json"
    write_json(path, {"structural_exact_tool_patterns": ["["]})
    with pytest.raises(re.error):
        load_config(path)
