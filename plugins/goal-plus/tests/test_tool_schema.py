import asyncio
from copy import deepcopy
import json

import pytest
from fastmcp.utilities.json_schema import dereference_refs
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from goal_plus.domain.search_input import SearchSpecInput
from goal_plus.server import create_mcp
from goal_plus.tool_schema import compact_tool_schema
from tests.test_tools import spec_dict


def test_compaction_reuses_definitions_and_preserves_validation():
    schema = {
        "$defs": {
            "Value": {"type": "integer", "minimum": 1, "description": "A positive count."},
            "Pair": {
                "type": "object", "additionalProperties": False,
                "properties": {"left": {"$ref": "#/$defs/Value"}, "right": {"$ref": "#/$defs/Value"}},
                "required": ["left", "right"],
            },
        },
        "type": "object", "additionalProperties": False,
        "properties": {
            "pair": {"$ref": "#/$defs/Pair"},
            "label": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "limited": {"$ref": "#/$defs/Value", "minimum": 3},
            "example": {"type": "object", "default": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        },
        "required": ["pair"],
    }
    original = deepcopy(schema)
    compact = compact_tool_schema(schema)
    assert schema == original
    assert set(compact["$defs"]) == {"Value"}
    assert compact["properties"]["label"]["type"] == ["string", "null"]
    assert compact["properties"]["example"] == schema["properties"]["example"]
    assert compact["$defs"]["Value"]["description"] == "A positive count."
    Draft202012Validator.check_schema(compact)
    before, after = Draft202012Validator(schema), Draft202012Validator(compact)
    for value in (
        {"pair": {"left": 1, "right": 2}},
        {"pair": {"left": 1, "right": 2}, "label": None, "limited": 3},
        {"pair": {"left": 1, "right": 2}, "limited": 2},
        {"pair": {"left": 0, "right": 2}},
        {"pair": {"left": 1}},
        {"pair": {"left": 1, "right": 2, "extra": 1}},
        {"pair": {"left": 1, "right": 2}, "label": 1},
    ):
        assert after.is_valid(value) == before.is_valid(value)


def test_compaction_preserves_single_ref_sibling_constraints():
    schema = {
        "$defs": {"Value": {"type": "integer", "minimum": 3}},
        "type": "object", "properties": {"value": {"$ref": "#/$defs/Value", "minimum": 1}},
    }
    result = compact_tool_schema(schema)
    validator = Draft202012Validator(result)
    assert not validator.is_valid({"value": 2})
    assert validator.is_valid({"value": 3})


@pytest.mark.parametrize("value_schema", [
    {"type": "integer", "exclusiveMinimum": 0, "maximum": 3},
    {"type": "string", "minLength": 1, "pattern": "^a"},
    {"type": "array", "items": {"type": "integer"}, "minItems": 1},
    {"type": "object", "properties": {"a": {"type": "integer"}},
     "required": ["a"], "additionalProperties": False},
])
def test_nullable_compaction_preserves_typed_constraints(value_schema):
    schema = {"anyOf": [value_schema, {"type": "null"}], "default": None}
    compact = compact_tool_schema(schema)
    assert compact["type"] == [value_schema["type"], "null"]
    assert compact["default"] is None
    before, after = Draft202012Validator(schema), Draft202012Validator(compact)
    for value in (None, False, 0, 1, 4, "", "a", "b", [], [1], ["a"], {}, {"a": 1}, {"a": "x"}, {"b": 1}):
        assert after.is_valid(value) == before.is_valid(value)


def test_nullable_compaction_keeps_constraints_that_reject_null():
    schema = {"anyOf": [{"type": "string", "enum": ["auto"]}, {"type": "null"}]}
    compact = compact_tool_schema(schema)
    assert "anyOf" in compact
    validator = Draft202012Validator(compact)
    assert validator.is_valid(None)
    assert not validator.is_valid("other")


def test_freeze_schema_stays_below_codex_structural_compaction_budget(tmp_path):
    tool = next(tool for tool in asyncio.run(create_mcp(tmp_path).list_tools())
                if tool.name == "goal_plus_search_freeze_spec")
    # Codex 0.154.0 tools/src/json_schema/{types,compaction}.rs measures this
    # subset after removing descriptions, before dropping refs or nested fields.
    keys = {"$ref", "type", "enum", "items", "minItems", "properties", "required",
            "additionalProperties", "anyOf", "oneOf", "allOf", "$defs", "definitions"}

    def normalize(node):
        if not isinstance(node, dict):
            return node
        node = dict(node)
        if "const" in node:
            node["enum"] = [node.pop("const")]
        if node.get("type") == "object":
            node.setdefault("properties", {})
        result = {key: value for key, value in node.items() if key in keys}
        for key in ("properties", "$defs", "definitions"):
            if key in result:
                result[key] = {name: normalize(child) for name, child in result[key].items()}
        for key in ("items", "additionalProperties"):
            if key in result:
                result[key] = normalize(result[key])
        for key in ("anyOf", "oneOf", "allOf"):
            if key in result:
                result[key] = [normalize(child) for child in result[key]]
        return result

    normalized = normalize(tool.parameters)
    # The complete adaptive policy contract is served with MCP-only
    # additionalProperties pruning; Pydantic remains authoritative at call time.
    # Budget includes the shared_cache spec surface (sections, verifier
    # update bounds) added by the shared-cache fact plane.
    assert len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()) <= 6_500
    resolved = dereference_refs(normalized)["properties"]["spec"]["properties"]
    assert set(resolved["process_verifiers"]["items"]["required"]) == {"name", "role", "command"}
    assert "count" in resolved["strategy"]["properties"]["models"]["items"]["properties"]


def test_registered_freeze_schema_matches_parameter_validation(tmp_path):
    tool = next(tool for tool in asyncio.run(create_mcp(tmp_path).list_tools())
                if tool.name == "goal_plus_search_freeze_spec")
    validator = Draft202012Validator(tool.parameters)
    for strategy, valid in [
        ({"name": "agent_guided"}, True),
        ({"name": "random"}, True),
        ({"name": "default"}, True),
        ({"name": "random-mode"}, True),
        ({"inner_agent": "common"}, True),
        ({"worker_budget": None}, True),
        ({"worker_budget": {}}, False),
        ({"worker_budget": {"max_turns": None, "max_runtime_seconds": None}}, False),
        ({"worker_budget": {"max_turns": 1}}, True),
        ({"worker_budget": {"max_turns": None, "max_runtime_seconds": 60}}, True),
        ({"worker_budget": {"max_turns": 0}}, False),
        ({"selection": {"ranking_tolerance": [-0.1]}}, False),
        ({"selection": {"ranking_tolerance": [0.1]}}, True),
        ({"selection": {"ranking_keys": ["hard_score", "hard_score"], "ranking_tolerance": [0, 0]}}, False),
        ({"orchestration_mode": "adaptive_search", "allocation": {"features": ["progress_trend/v1"]}}, False),
        ({"orchestration_mode": "adaptive_search",
          "verifier": {"scores": ["progress_reward", "allocation_utility"]},
          "allocation": {"policy": "swe_search_uct/v1"}}, True),
        ({"orchestration_mode": "adaptive_search",
          "verifier": {"scores": ["progress_reward", "allocation_utility"]},
          "allocation": {"policy": "unknown/v1"}}, False),
        ({"orchestration_mode": "adaptive_search",
          "verifier": {"scores": ["progress_reward", "allocation_utility"]},
          "allocation": {"policy": "swe_search_uct/v1", "parameters": {"max_depth": 0}}}, False),
    ]:
        spec = {**spec_dict(), "strategy": strategy}
        arguments = {"goal_plus_id": "gp_0001", "spec": spec, "verifier_artifact_paths": ["evaluator.py"]}
        assert validator.is_valid(arguments) is valid, strategy
        if valid:
            SearchSpecInput.model_validate(spec)
        else:
            with pytest.raises(ValidationError):
                SearchSpecInput.model_validate(spec)
